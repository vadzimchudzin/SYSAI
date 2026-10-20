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
        value_hash = hashlib.sha256(str(profile.get("voiceprint") or "").encode("utf-8")).hexdigest()
        stable.append((profile.get("label"), profile.get("employee_id"), value_hash))
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
                      offset: float = 0.0) -> list[dict]:
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
