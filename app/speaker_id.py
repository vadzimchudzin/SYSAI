"""Optional pyannoteAI speaker identification using consented, encrypted employee voiceprints.

SYSAI stores only the encrypted reusable template. Reference samples are staged in a temporary
directory and removed at the end of enrollment; meeting chunks are uploaded only when at least
one active voiceprint and a provider key are configured.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import math
import pathlib
import subprocess
import time
import uuid

import httpx
from cryptography.fernet import Fernet, InvalidToken

from . import audio, config

log = logging.getLogger("sysai.speaker_id")
BASE_URL = "https://api.pyannote.ai/v1"
CONSENT_VERSION = "voiceprint-consent-v1"
MATCH_THRESHOLD = 65.0
MIN_SAMPLE_SECONDS = 5.0
MAX_SAMPLE_SECONDS = 30.0
MAX_SAMPLE_BYTES = 16 * 1024 * 1024


class SpeakerIdentityError(RuntimeError):
    """Configuration, audio preparation, or external provider failure."""


def _fernet() -> Fernet:
    # A stable key derived from the application's secret; a key rotation requires re-enrollment.
    derived = base64.urlsafe_b64encode(hashlib.sha256(config.SECRET_KEY.encode("utf-8")).digest())
    return Fernet(derived)


def encrypt_voiceprint(value: str) -> bytes:
    if not isinstance(value, str) or not value.strip():
        raise SpeakerIdentityError("Провайдер не вернул голосовой шаблон")
    return _fernet().encrypt(value.encode("utf-8"))


def decrypt_voiceprint(value: bytes) -> str:
    try:
        return _fernet().decrypt(bytes(value)).decode("utf-8")
    except (InvalidToken, ValueError, TypeError, UnicodeDecodeError):
        raise SpeakerIdentityError(
            "Не удалось расшифровать голосовой шаблон. Проверьте, не менялся ли SECRET_KEY."
        ) from None


def profiles_fingerprint(profiles: list[dict]) -> str:
    """Invalidate transcription checkpoints when the set of enrolled templates changes."""
    stable = []
    for profile in profiles:
        material = profile.get("voiceprint")
        if material is None:
            material = json.dumps(profile.get("embedding"), ensure_ascii=False, separators=(",", ":"))
        value_hash = hashlib.sha256(str(material or "").encode("utf-8")).hexdigest()
        stable.append((profile.get("label"), profile.get("employee_id"), profile.get("sample_id"), value_hash))
    payload = json.dumps(sorted(stable), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def enrolled_voiceprints() -> list[dict]:
    """Fetch active employees' voiceprints only when voice-identification is explicitly configured."""
    if not config.pyannoteai_key():
        return []
    from .db import Employee, EmployeeVoiceprint, SessionLocal

    profiles = []
    with SessionLocal() as session:
        rows = (session.query(EmployeeVoiceprint)
                .join(Employee, Employee.id == EmployeeVoiceprint.employee_id)
                .filter(Employee.active.is_(True))
                .order_by(Employee.id)
                .all())
        for row in rows:
            try:
                template = decrypt_voiceprint(row.encrypted_voiceprint)
            except SpeakerIdentityError:
                log.warning("Skipping unreadable voiceprint for employee id %s", row.employee_id)
                continue
            profiles.append({
                "label": f"employee-{row.employee_id}",
                "employee_id": row.employee_id,
                "employee_name": row.employee.name,
                "voiceprint": template,
            })
    return profiles


def normalize_sample(source_path: str | pathlib.Path, normalized_path: str | pathlib.Path) -> float:
    """Convert one clean speaker sample to mono 16 kHz WAV and enforce the 5–30 s window."""
    source, target = pathlib.Path(source_path), pathlib.Path(normalized_path)
    try:
        duration = audio.duration(str(source))
    except Exception as exc:
        raise SpeakerIdentityError("Не удалось прочитать длительность аудио образца") from exc
    if duration < MIN_SAMPLE_SECONDS:
        raise SpeakerIdentityError("Нужен образец речи не короче 5 секунд")
    if duration > MAX_SAMPLE_SECONDS:
        raise SpeakerIdentityError("Образец должен быть не длиннее 30 секунд")
    try:
        subprocess.run(
            ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
             "-i", str(source), "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000",
             "-c:a", "pcm_s16le", str(target)],
            capture_output=True, text=True, check=True, timeout=45,
        )
        normalized_duration = audio.duration(str(target))
    except Exception as exc:
        raise SpeakerIdentityError("Не удалось нормализовать образец. Попробуйте WAV или MP3.") from exc
    if not MIN_SAMPLE_SECONDS <= normalized_duration <= MAX_SAMPLE_SECONDS + 0.1:
        raise SpeakerIdentityError("После конвертации длительность образца должна быть 5–30 секунд")
    return round(normalized_duration, 2)


def _headers() -> dict:
    key = config.pyannoteai_key()
    if not key:
        raise SpeakerIdentityError("Ключ pyannoteAI не задан")
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def _json_response(response: httpx.Response, action: str) -> dict:
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError:
        # Never include authorization headers or raw audio/template material in an error message.
        detail = response.text[:250]
        key = config.pyannoteai_key()
        if key:
            detail = detail.replace(key, "[hidden]")
        raise SpeakerIdentityError(
            f"pyannoteAI: операция «{action}» завершилась с HTTP {response.status_code}: {detail}"
        ) from None
    try:
        payload = response.json()
    except ValueError:
        raise SpeakerIdentityError(f"pyannoteAI: некорректный JSON при операции «{action}»") from None
    if not isinstance(payload, dict):
        raise SpeakerIdentityError(f"pyannoteAI: некорректный ответ при операции «{action}»")
    return payload


def _upload_media(path: str | pathlib.Path, purpose: str) -> str:
    """Upload bytes to the provider's temporary media area and return its media:// reference."""
    media_url = f"media://sysai-{purpose}-{uuid.uuid4().hex}"
    headers = _headers()
    try:
        response = httpx.post(
            f"{BASE_URL}/media/input", headers=headers, json={"url": media_url}, timeout=30
        )
        payload = _json_response(response, "подготовка загрузки аудио")
        upload_url = payload.get("url")
        if not isinstance(upload_url, str) or not upload_url.startswith("https://"):
            raise SpeakerIdentityError("pyannoteAI не выдал временный URL для загрузки аудио")
        with pathlib.Path(path).open("rb") as audio_file:
            upload = httpx.put(
                upload_url, content=audio_file.read(),
                headers={"Content-Type": "application/octet-stream"}, timeout=60,
            )
        try:
            upload.raise_for_status()
        except httpx.HTTPStatusError:
            raise SpeakerIdentityError(
                f"pyannoteAI не принял аудио (HTTP {upload.status_code})"
            ) from None
    except httpx.TimeoutException:
        raise SpeakerIdentityError("Истекло время ожидания при передаче аудио в pyannoteAI") from None
    except httpx.HTTPError as exc:
        raise SpeakerIdentityError(f"Не удалось связаться с pyannoteAI: {str(exc)[:160]}") from None
    return media_url


def _wait_for_job(job_id: str, action: str, timeout_seconds: float = 150.0) -> dict:
    deadline = time.monotonic() + timeout_seconds
    headers = _headers()
    while time.monotonic() < deadline:
        try:
            response = httpx.get(f"{BASE_URL}/jobs/{job_id}", headers=headers, timeout=20)
            job = _json_response(response, f"проверка {action}")
        except httpx.TimeoutException:
            time.sleep(2)
            continue
        except httpx.HTTPError as exc:
            raise SpeakerIdentityError(f"Связь с pyannoteAI прервалась при операции «{action}»: {str(exc)[:150]}") from None
        status = job.get("status")
        if status == "succeeded":
            output = job.get("output")
            if isinstance(output, dict):
                return output
            raise SpeakerIdentityError(f"pyannoteAI: операция «{action}» завершилась без результата")
        if status == "failed":
            error = job.get("error") or {}
            message = str(error.get("message") or error.get("type") or "ошибка провайдера") if isinstance(error, dict) else str(error)
            raise SpeakerIdentityError(f"pyannoteAI: операция «{action}» не выполнена: {message[:200]}")
        time.sleep(2)
    raise SpeakerIdentityError(f"Истекло время ожидания pyannoteAI при операции «{action}»")


def _job_id(payload: dict, action: str) -> str:
    job_id = payload.get("jobId") or payload.get("job_id")
    if not isinstance(job_id, str) or not job_id:
        raise SpeakerIdentityError(f"pyannoteAI не вернул ID задачи для операции «{action}»")
    return job_id


def create_voiceprint(sample_path: str | pathlib.Path, employee_id: int) -> str:
    """Create the reusable voiceprint after the caller has captured explicit consent."""
    media_url = _upload_media(sample_path, f"employee-{employee_id}")
    try:
        response = httpx.post(
            f"{BASE_URL}/voiceprint", headers=_headers(), json={"url": media_url}, timeout=30
        )
        payload = _json_response(response, "создание голосового шаблона")
    except httpx.TimeoutException:
        raise SpeakerIdentityError("Истекло время ожидания при создании голосового шаблона") from None
    result = _wait_for_job(_job_id(payload, "создание голосового шаблона"), "создание голосового шаблона")
    template = result.get("voiceprint")
    if not isinstance(template, str) or not template.strip():
        raise SpeakerIdentityError("pyannoteAI не вернул голосовой шаблон")
    return template


def _identify_audio(path: str | pathlib.Path, profiles: list[dict]) -> dict:
    if not profiles:
        return {}
    media_url = _upload_media(path, "meeting-chunk")
    api_voiceprints = [
        {"label": str(profile["label"]), "voiceprint": str(profile["voiceprint"])}
        for profile in profiles
    ]
    try:
        response = httpx.post(
            f"{BASE_URL}/identify",
            headers=_headers(),
            json={
                "url": media_url,
                "voiceprints": api_voiceprints,
                "matching": {"threshold": MATCH_THRESHOLD, "exclusive": True},
            },
            timeout=30,
        )
        payload = _json_response(response, "идентификация говорящих")
    except httpx.TimeoutException:
        raise SpeakerIdentityError("Истекло время ожидания при идентификации говорящих") from None
    except httpx.HTTPError as exc:
        raise SpeakerIdentityError(f"Не удалось связаться с pyannoteAI: {str(exc)[:150]}") from None
    return _wait_for_job(_job_id(payload, "идентификация говорящих"), "идентификация говорящих")


def identify_segments(path: str | pathlib.Path, segments: list[dict], profiles: list[dict],
                      offset: float = 0.0, duration: float | None = None) -> list[dict]:
    """Rename only spans explicitly matched to one enrolled template with a score >= threshold."""
    if not segments or not profiles or not config.pyannoteai_key():
        return segments
    result = _identify_audio(path, profiles)
    identified = result.get("identification")
    if not isinstance(identified, list):
        raise SpeakerIdentityError("pyannoteAI не вернул список идентификаций")
    profile_by_label = {str(profile["label"]): profile for profile in profiles}
    confidence_by_label = {}
    voiceprint_rows = result.get("voiceprints")
    for row in voiceprint_rows if isinstance(voiceprint_rows, list) else []:
        if not isinstance(row, dict):
            continue
        matched = row.get("match")
        scores = row.get("confidence")
        if isinstance(matched, str) and isinstance(scores, dict):
            try:
                confidence_by_label[matched] = float(scores.get(matched))
            except (TypeError, ValueError):
                pass
    spans = []
    for item in identified:
        if not isinstance(item, dict):
            continue
        label = item.get("match")
        if not isinstance(label, str) or label not in profile_by_label:
            continue
        try:
            start, end = float(item["start"]), float(item["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if end <= start:
            continue
        profile = profile_by_label[label]
        score = confidence_by_label.get(label)
        if score is None or score < MATCH_THRESHOLD:
            continue
        spans.append((start, end, profile, score))
    if len(segments) == 1 and duration is not None and duration > 0:
        # If ASR returned one fallback segment for an entire chunk, do not label
        # the whole chunk from a short voice match near its start.
        minimum_coverage = float(duration) * 0.8
        spans = [span for span in spans if span[1] - span[0] >= minimum_coverage]
    updated_segments = []
    for segment in segments:
        local_start = float(segment.get("start", 0) or 0) - float(offset or 0)
        matches = [span for span in spans if span[0] - 0.35 <= local_start <= span[1] + 0.35]
        employee_ids = {span[2]["employee_id"] for span in matches}
        if len(employee_ids) != 1:
            updated_segments.append(segment)
            continue
        selected = max(matches, key=lambda item: item[3])
        profile, score = selected[2], selected[3]
        renamed = dict(segment)
        renamed.update({
            "speaker": profile["employee_name"],
            "speaker_employee_id": profile["employee_id"],
            "speaker_source": "voiceprint",
            "speaker_confidence": round(max(0.0, min(100.0, score)), 1),
        })
        updated_segments.append(renamed)
    return updated_segments


# -----------------------------------------------------------------------------
# Local ECAPA embeddings through the isolated Vercel API. The legacy pyannote
# helpers above are retained only for backward-compatible data/tests; new SYSAI
# flows use the Vercel methods below and do not call pyannoteAI.
VOICE_SERVICE_MAX_BYTES = 4 * 1024 * 1024


def _normalise_embedding(values) -> list[float]:
    if not isinstance(values, (list, tuple)) or len(values) != 192:
        raise SpeakerIdentityError("Сервис вернул голосовой профиль неверной размерности")
    try:
        vector = [float(value) for value in values]
    except (TypeError, ValueError):
        raise SpeakerIdentityError("Сервис вернул некорректный голосовой профиль") from None
    if any(not math.isfinite(value) for value in vector):
        raise SpeakerIdentityError("Голосовой профиль содержит некорректные числа")
    norm = math.sqrt(sum(value * value for value in vector))
    if norm < 1e-8:
        raise SpeakerIdentityError("Не удалось получить признаки голоса")
    return [round(value / norm, 8) for value in vector]


def _voice_service_request_url(path: str) -> str:
    if not config.voice_service_enabled():
        raise SpeakerIdentityError("Сервис идентификации голоса ещё не настроен на сервере")
    return f"{config.voice_service_url().rstrip('/')}/{path.lstrip('/')}"


def _voice_service_headers() -> dict[str, str]:
    token = config.voice_service_token()
    if not token:
        raise SpeakerIdentityError("Не задан ключ доступа к сервису идентификации голоса")
    return {"X-SYSAI-Voice-Token": token}


def embed_voice_sample(sample_path: str | pathlib.Path, employee_id: int) -> dict:
    """Create a local model embedding and store the consented WAV in private Vercel Blob."""
    path = pathlib.Path(sample_path)
    raw = path.read_bytes()
    if not raw or len(raw) > VOICE_SERVICE_MAX_BYTES:
        raise SpeakerIdentityError("Нормализованный образец пуст или превышает 4 МБ.")
    try:
        response = httpx.post(
            _voice_service_request_url("/v1/embed"),
            headers=_voice_service_headers(),
            data={"purpose": "profile", "employee_id": str(employee_id)},
            files={"audio": (path.name, raw, "audio/wav")},
            timeout=110,
        )
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPStatusError as exc:
        detail = ""
        try:
            detail = str(exc.response.json().get("detail") or "")
        except Exception:
            pass
        raise SpeakerIdentityError(
            "Vercel не принял образец голоса" + (f": {detail[:180]}" if detail else f" (HTTP {exc.response.status_code})")
        ) from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise SpeakerIdentityError("Не удалось связаться с сервисом идентификации Vercel") from exc
    embedding = _normalise_embedding(payload.get("embedding"))
    pathname = payload.get("sample_blob_path")
    if not isinstance(pathname, str) or not pathname.startswith(f"profiles/employee-{employee_id}/"):
        raise SpeakerIdentityError("Vercel не вернул корректный путь приватного образца")
    waveform = payload.get("waveform")
    if not isinstance(waveform, list) or not waveform:
        raise SpeakerIdentityError("Vercel не вернул данные звуковой диаграммы")
    try:
        waveform = [round(min(1.0, max(0.0, float(value))), 4) for value in waveform[:128]]
    except (TypeError, ValueError):
        raise SpeakerIdentityError("Данные звуковой диаграммы некорректны") from None
    if any(not math.isfinite(value) for value in waveform):
        raise SpeakerIdentityError("Данные звуковой диаграммы некорректны")
    try:
        sample_duration = float(payload.get("duration_seconds"))
    except (TypeError, ValueError):
        sample_duration = audio.duration(str(path))
    if not MIN_SAMPLE_SECONDS <= sample_duration <= MAX_SAMPLE_SECONDS:
        raise SpeakerIdentityError("Сервис вернул неожиданную длительность образца")
    return {
        "embedding": embedding,
        "sample_blob_path": pathname,
        "waveform": waveform,
        "duration_seconds": round(sample_duration, 3),
        "model_version": str(payload.get("model_version") or "unknown")[:120],
    }


def delete_remote_voice_sample(employee_id: int, pathname: str) -> None:
    """Remove a private reference blob; caller validates ownership from the DB row."""
    if not pathname:
        return
    try:
        response = httpx.request(
            "DELETE",
            _voice_service_request_url("/v1/sample"),
            headers=_voice_service_headers(),
            json={"employee_id": int(employee_id), "pathname": pathname},
            timeout=25,
        )
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise SpeakerIdentityError(f"Vercel не удалил образец (HTTP {exc.response.status_code})") from exc
    except httpx.HTTPError as exc:
        raise SpeakerIdentityError("Не удалось удалить образец из Vercel Blob") from exc


def enrolled_embeddings() -> list[dict]:
    """Load and decrypt active employee embeddings from the shared SYSAI database."""
    if not config.voice_service_enabled():
        return []
    from .db import Employee, EmployeeVoiceSample, SessionLocal

    profiles = []
    with SessionLocal() as session:
        rows = (
            session.query(EmployeeVoiceSample)
            .join(Employee, Employee.id == EmployeeVoiceSample.employee_id)
            .filter(Employee.active.is_(True))
            .order_by(Employee.id, EmployeeVoiceSample.created_at)
            .all()
        )
        for row in rows:
            try:
                value = decrypt_voiceprint(row.encrypted_embedding)
                embedding = _normalise_embedding(json.loads(value))
            except (SpeakerIdentityError, json.JSONDecodeError, TypeError):
                log.warning("Skipping unreadable local voice sample id %s", row.id)
                continue
            profiles.append({
                "label": f"employee-{row.employee_id}",
                "employee_id": row.employee_id,
                "employee_name": row.employee.name,
                "sample_id": row.id,
                "model_version": row.model_version,
                "embedding": embedding,
            })
    return profiles


def _cosine_similarity(first: list[float], second: list[float]) -> float:
    if not first or len(first) != len(second):
        return -1.0
    left_norm = math.sqrt(sum(float(value) ** 2 for value in first))
    right_norm = math.sqrt(sum(float(value) ** 2 for value in second))
    if left_norm < 1e-8 or right_norm < 1e-8:
        return -1.0
    return sum(float(a) * float(b) for a, b in zip(first, second)) / (left_norm * right_norm)


def _local_segments_with_end(segments: list[dict], offset: float, duration: float | None) -> list[dict]:
    """Estimate conservative end times from text length while clipping at the next turn."""
    ordered = sorted(segments, key=lambda item: float(item.get("start", 0) or 0))
    starts = []
    for item in ordered:
        try:
            starts.append(float(item.get("start", 0) or 0) - float(offset or 0))
        except (TypeError, ValueError):
            starts.append(0.0)
    prepared = []
    for index, item in enumerate(ordered):
        try:
            local_start = float(item.get("start", 0) or 0) - float(offset or 0)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(local_start):
            continue
        local_start = max(0.0, local_start)
        next_start = next((value for value in starts[index + 1:] if value > local_start + 0.02), None)
        words = max(1, len(str(item.get("text") or "").split()))
        guess = max(1.05, min(8.0, words / 2.5 + 0.25))
        local_end = local_start + guess
        if next_start is not None:
            local_end = min(local_end, max(local_start, next_start - 0.04))
        if duration is not None and math.isfinite(float(duration)):
            local_end = min(local_end, float(duration))
        if local_end - local_start < 0.95:
            continue
        prepared.append({
            "speaker": str(item.get("speaker") or ""),
            "start": round(local_start, 3),
            "end": round(local_end, 3),
        })
    return [item for item in prepared if item["speaker"]]


def identify_embedded_segments(
    path: str | pathlib.Path,
    segments: list[dict],
    profiles: list[dict],
    offset: float = 0.0,
    duration: float | None = None,
) -> list[dict]:
    """Get one embedding per diarized speaker and match conservatively to local enrolled samples."""
    if not segments or not profiles or not config.voice_service_enabled():
        return segments
    payload_segments = _local_segments_with_end(segments, offset, duration)
    if not payload_segments:
        return segments
    audio_path = pathlib.Path(path)
    raw = audio_path.read_bytes()
    if not raw or len(raw) > VOICE_SERVICE_MAX_BYTES:
        raise SpeakerIdentityError("Аудиофрагмент слишком большой для сервиса идентификации")
    try:
        response = httpx.post(
            _voice_service_request_url("/v1/identify"),
            headers=_voice_service_headers(),
            data={"segments_json": json.dumps({"segments": payload_segments}, ensure_ascii=False)},
            files={"audio": (audio_path.name, raw, "audio/mpeg" if audio_path.suffix.lower() == ".mp3" else "audio/wav")},
            timeout=110,
        )
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPStatusError as exc:
        detail = ""
        try:
            detail = str(exc.response.json().get("detail") or "")
        except Exception:
            pass
        raise SpeakerIdentityError(
            "Vercel не смог сравнить голоса" + (f": {detail[:180]}" if detail else f" (HTTP {exc.response.status_code})")
        ) from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise SpeakerIdentityError("Не удалось связаться с сервисом идентификации Vercel") from exc

    speaker_embeddings = payload.get("speakers")
    if not isinstance(speaker_embeddings, dict):
        raise SpeakerIdentityError("Vercel вернул некорректный ответ идентификации")
    by_employee: dict[int, list[dict]] = {}
    for profile in profiles:
        try:
            employee_id = int(profile["employee_id"])
            embedding = _normalise_embedding(profile.get("embedding"))
        except (KeyError, TypeError, ValueError, SpeakerIdentityError):
            continue
        by_employee.setdefault(employee_id, []).append({**profile, "embedding": embedding})

    try:
        threshold = float(config.VOICE_MATCH_THRESHOLD)
        margin_required = float(config.VOICE_MATCH_MARGIN)
    except (TypeError, ValueError):
        threshold, margin_required = 0.55, 0.08
    threshold = min(0.95, max(-1.0, threshold))
    margin_required = min(0.5, max(0.0, margin_required))
    recognized: dict[str, tuple[int, str, float]] = {}
    for speaker, info in speaker_embeddings.items():
        if not isinstance(info, dict):
            continue
        try:
            observed = _normalise_embedding(info.get("embedding"))
        except SpeakerIdentityError:
            continue
        scored = []
        for employee_id, refs in by_employee.items():
            similarities = sorted(
                (_cosine_similarity(observed, ref["embedding"]) for ref in refs),
                reverse=True,
            )
            if not similarities:
                continue
            # Two strong reference samples reduce over-reliance on a bad/outlier sample.
            top = similarities[:2]
            score = sum(top) / len(top)
            name = str(refs[0].get("employee_name") or f"Сотрудник {employee_id}")
            scored.append((score, employee_id, name))
        scored.sort(reverse=True)
        if not scored:
            continue
        best = scored[0]
        runner_up = scored[1][0] if len(scored) > 1 else -1.0
        if best[0] >= threshold and best[0] - runner_up >= margin_required:
            recognized[str(speaker)] = (best[1], best[2], best[0])

    result = []
    for segment in segments:
        speaker = str(segment.get("speaker") or "")
        match = recognized.get(speaker)
        if match is None:
            result.append(segment)
            continue
        employee_id, employee_name, similarity = match
        renamed = dict(segment)
        renamed.update({
            "speaker": employee_name,
            "speaker_employee_id": employee_id,
            "speaker_source": "voiceprint",
            "speaker_similarity": round(max(-1.0, min(1.0, similarity)), 3),
        })
        # Kept for compatibility with report normalization; UI displays cosine similarity, not a probability.
        renamed["speaker_confidence"] = renamed["speaker_similarity"]
        result.append(renamed)
    return result


def extract_confirmed_speaker_sample(
    audio_path: str | pathlib.Path,
    transcript: list[dict],
    speaker: str,
    destination: str | pathlib.Path,
    maximum_seconds: float = 24.0,
) -> tuple[float, float, float] | None:
    """Concatenate clean, non-overlapping turns from a human-confirmed speaker into a short WAV."""
    source = pathlib.Path(audio_path)
    output = pathlib.Path(destination)
    if not source.is_file():
        return None
    ordered = sorted(
        [item for item in transcript if str(item.get("speaker") or "") == speaker],
        key=lambda item: float(item.get("start", 0) or 0),
    )
    intervals = []
    total = 0.0
    for index, item in enumerate(ordered):
        try:
            start = float(item.get("start", 0) or 0)
            end = float(item.get("end") or 0)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(start) or not math.isfinite(end) or end <= start:
            continue
        # Long diarization spans often include pauses; cap each turn before concatenation.
        end = min(end, start + 7.0)
        if end - start < 1.0:
            continue
        take = min(end - start, maximum_seconds - total)
        if take < 0.8:
            break
        intervals.append((start, start + take))
        total += take
        if total >= maximum_seconds:
            break
    if total < MIN_SAMPLE_SECONDS or not intervals:
        return None
    output.parent.mkdir(parents=True, exist_ok=True)
    filters = []
    labels = []
    for index, (start, end) in enumerate(intervals):
        labels.append(f"[s{index}]")
        filters.append(
            f"[0:a]atrim=start={start:.3f}:end={end:.3f},asetpts=PTS-STARTPTS,"
            f"aresample=16000,aformat=sample_fmts=s16:channel_layouts=mono[s{index}]"
        )
    filters.append("".join(labels) + f"concat=n={len(labels)}:v=0:a=1[out]")
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", str(source),
             "-filter_complex", ";".join(filters), "-map", "[out]",
             "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(output)],
            check=True, capture_output=True, timeout=45,
        )
        actual = audio.duration(str(output))
    except Exception as exc:
        log.warning("Could not prepare confirmed speaker sample: %s", exc)
        output.unlink(missing_ok=True)
        return None
    if not MIN_SAMPLE_SECONDS <= actual <= MAX_SAMPLE_SECONDS:
        output.unlink(missing_ok=True)
        return None
    return (actual, intervals[0][0], intervals[-1][1])
