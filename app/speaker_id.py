"""Voices through our own voice service (speaker-service/, Vercel): ECAPA voice prints, consented employee samples.

The service turns speech into 192-number voice prints; employees, meetings and the matching stay in this database.
Employee samples are confirmed by a person on the meeting page and kept encrypted; meeting audio goes to the service
only as short parts, for whole-record speaker labelling (diarize.py) and for matching to confirmed samples.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import math
import operator
import pathlib
import subprocess
import tempfile
import time

import httpx
from cryptography.fernet import Fernet, InvalidToken

from . import audio, config

log = logging.getLogger("sysai.speaker_id")
CONSENT_VERSION = "voiceprint-consent-v1"
MIN_SAMPLE_SECONDS = 5.0
MAX_SAMPLE_SECONDS = 30.0


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
        stable.append((profile.get("label"), profile.get("employee_id"), profile.get("sample_id"), profile.get("model_version", ""), value_hash))
    payload = json.dumps(sorted(stable), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# -----------------------------------------------------------------------------
# ECAPA voice prints through our own voice service (speaker-service/ on Vercel).
VOICE_SERVICE_MAX_BYTES = 4 * 1024 * 1024
VOICE_SERVICE_MAX_SEGMENTS = 150  # speech intervals in one /v1/identify request


def _normalise_embedding(values) -> list[float]:
    if not isinstance(values, (list, tuple)) or len(values) != 192:
        raise SpeakerIdentityError("Сервис вернул голосовой профиль неверной размерности")
    try:
        vector = [float(value) for value in values]
    except (TypeError, ValueError):
        raise SpeakerIdentityError("Сервис вернул некорректный голосовой профиль") from None
    if any(not math.isfinite(value) for value in vector):
        raise SpeakerIdentityError("Голосовой профиль содержит некорректные числа")
    norm = math.hypot(*vector)
    if not math.isfinite(norm) or norm < 1e-8:
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
    """Extract transient real speaker features; meeting audio stays only in the encrypted DB."""
    path = pathlib.Path(sample_path)
    raw = path.read_bytes()
    if not raw or len(raw) > VOICE_SERVICE_MAX_BYTES:
        raise SpeakerIdentityError("Нормализованный образец пуст или превышает 4 МБ.")
    try:
        response = httpx.post(
            _voice_service_request_url("/v1/embed"),
            headers=_voice_service_headers(),
            data={"purpose": "segment", "employee_id": str(employee_id)},
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
    if pathname:
        raise SpeakerIdentityError("Сервис сохранил внешнюю копию вопреки режиму segment")
    waveform = payload.get("waveform") or []
    if not isinstance(waveform, list):
        raise SpeakerIdentityError("Некорректная диаграмма")
    try:
        waveform = [float(v) for v in waveform[:128]]
        if any(not math.isfinite(v) or not 0 <= v <= 1 for v in waveform):
            raise ValueError()
    except (TypeError, ValueError):
        raise SpeakerIdentityError("Некорректная диаграмма") from None
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
            if not sample_has_usable_embedding(row):
                continue
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
                "source_audio_sha256": row.source_audio_sha256,
                "content_sha256": row.content_sha256,
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


def embed_speakers(path: str | pathlib.Path, segments: list[dict], min_seconds: float | None = None) -> dict:
    """One voice print per label of this audio part: {"model_version": ..., "speakers": {label: {"embedding", ...}}}.

    segments are [{"speaker", "start", "end"}] in seconds of this part; the service joins up to 30 s of each label and
    skips labels with less than a second of speech (min_seconds asks for shorter ones, down to 0.3 s: a short reply;
    a service that does not know the field keeps its one second)."""
    audio_path = pathlib.Path(path)
    raw = audio_path.read_bytes()
    if not raw or len(raw) > VOICE_SERVICE_MAX_BYTES:
        raise SpeakerIdentityError("Аудиофрагмент пуст или слишком большой для сервиса голосов")
    try:
        response = httpx.post(
            _voice_service_request_url("/v1/identify"),
            headers=_voice_service_headers(),
            data={"segments_json": json.dumps({"segments": segments}, ensure_ascii=False),
                  **({"min_seconds": f"{min(1.0, max(0.3, float(min_seconds))):.2f}"} if min_seconds else {})},
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
            "Сервис голосов не смог сравнить голоса" + (f": {detail[:180]}" if detail else f" (HTTP {exc.response.status_code})")
        ) from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise SpeakerIdentityError("Не удалось связаться с сервисом голосов") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("speakers"), dict):
        raise SpeakerIdentityError("Сервис голосов вернул некорректный ответ")
    return payload


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
    # Refresh DB eligibility so revoked/expired samples cannot survive in a cached profile list.
    live_ids = {p["sample_id"] for p in enrolled_embeddings()}
    profiles = [p for p in profiles if p.get("sample_id") in live_ids]
    if not profiles:
        return segments
    payload_segments = _local_segments_with_end(segments, offset, duration)
    if not payload_segments:
        return segments
    payload = embed_speakers(path, payload_segments)
    speaker_embeddings = payload["speakers"]
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
    threshold = min(0.95, max(0.75, threshold)) if math.isfinite(threshold) else 0.75
    margin_required = min(0.5, max(0.15, margin_required)) if math.isfinite(margin_required) else 0.15
    recognized: dict[str, tuple[int, str, float]] = {}
    for speaker, info in speaker_embeddings.items():
        if not isinstance(info, dict):
            continue
        try:
            observed = _normalise_embedding(info.get("embedding"))
        except SpeakerIdentityError:
            continue
        scored = []
        eligible = {}
        for employee_id, refs in by_employee.items():
            model = payload.get("model_version")
            refs = [r for r in refs if model and r.get("model_version") == model]
            if not refs:
                continue
            # One representative per independent recording; all rivals remain in
            # ambiguity scoring even before they have enough enrollment evidence.
            sources = {}
            for ref in refs:
                key = ref.get("source_audio_sha256")
                if key and key not in sources:
                    sources[key] = ref
            refs = list(sources.values())
            if not refs:
                continue
            similarities = sorted((_cosine_similarity(observed, r["embedding"]) for r in refs), reverse=True)
            score = min(similarities[:READY_INDEPENDENT_SAMPLES])
            eligible[employee_id] = len(refs) >= READY_INDEPENDENT_SAMPLES
            name = str(refs[0].get("employee_name") or f"Сотрудник {employee_id}")
            scored.append((score, employee_id, name))
        scored.sort(reverse=True)
        if not scored:
            continue
        best = scored[0]
        runner_up = scored[1][0] if len(scored) > 1 else -1.0
        if eligible[best[1]] and best[0] >= threshold and best[0] - runner_up >= margin_required:
            recognized[str(speaker)] = (best[1], best[2], best[0])

    # Recheck after an external request: revocation/expiry must win over a cached profile.
    live = enrolled_embeddings()
    live_by_employee = {}
    for profile in live:
        if profile.get("model_version") == payload.get("model_version"):
            live_by_employee.setdefault(profile["employee_id"], set()).add(profile.get("source_audio_sha256"))
    duplicate_ids = {match[0] for match in recognized.values()
                     if sum(other[0] == match[0] for other in recognized.values()) > 1}
    recognized = {speaker: match for speaker, match in recognized.items()
                  if match[0] not in duplicate_ids and len(live_by_employee.get(match[0], set())) >= READY_INDEPENDENT_SAMPLES}

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
        renamed.pop("speaker_confidence", None)
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


# Human-confirmed meeting samples. No synthetic embeddings, no probability claims.
CONFIRMED_CONSENT_VERSION = "meeting-voice-consent-v3"
READY_INDEPENDENT_SAMPLES = 3
VOICE_SAMPLE_RETENTION_DAYS = 180
MAX_DB_AUDIO_BYTES = 1_000_000  # 30s mono, PCM16 16kHz plus WAV header
READINESS_HEURISTIC = (
    "Проверка количества и согласованности образцов, не оценка точности: "
    "нужны разные записи встреч, согласие и настоящие совместимые голосовые признаки. "
    "Калибровка на отложенных примерах ещё не выполнена. "
    "Косинусное сходство не является вероятностью; неизвестные голоса остаются неизвестными."
)


def _finite(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def intervals_overlap(a_start, a_end, b_start, b_end, slack=0.0):
    return min(a_end, b_end) - max(a_start, b_start) > slack


def propose_clear_speaker_clips(transcript, limit=None):
    """Propose candidates, not verified speech. Never infer missing end timestamps.

    Every interval (including unlabelled speech) participates in overlap rejection.
    Quality and identity must still be confirmed by a person after listening.
    """
    intervals = []
    for item in transcript or []:
        if not isinstance(item, dict):
            continue
        start, end = _finite(item.get("start")), _finite(item.get("end"))
        if start is None or start < 0:
            continue
        intervals.append((start, end, item))
    intervals.sort(key=lambda row: row[0])
    candidates, seen = [], set()
    for start, end, item in intervals:
        label = str(item.get("speaker") or "").strip()
        if not label or end is None or not MIN_SAMPLE_SECONDS <= end - start <= MAX_SAMPLE_SECONDS:
            continue
        if item.get("overlap") or item.get("overlapping") or item.get("is_overlap"):
            continue
        unsafe = False
        for other_start, other_end, other in intervals:
            if other is item:
                continue
            # An unbounded turn inside/before this clip is not evidence of clean audio.
            if other_end is None:
                if other_start < end:
                    unsafe = True
                    break
            elif intervals_overlap(start, end, other_start, other_end):
                unsafe = True
                break
        if unsafe:
            continue
        start, end = round(start, 3), round(end, 3)
        key = (start, end, label)
        if key in seen:
            continue
        seen.add(key)
        candidates.append({"speaker": label, "start": start, "end": end,
                           "duration": round(end - start, 3), "text": str(item.get("text") or "")[:280],
                           "speaker_employee_id": item.get("speaker_employee_id")
                           if item.get("speaker_source") == "voiceprint" else None})
    return candidates if limit is None else candidates[:max(0, limit)]


def _wav_info(raw):
    """Validate a bounded canonical waveform and return PCM, duration, quality stats."""
    import io
    import wave
    import array
    if not raw or len(raw) > MAX_DB_AUDIO_BYTES:
        raise SpeakerIdentityError("Образец пуст или превышает лимит 1 МБ.")
    try:
        with wave.open(io.BytesIO(raw), "rb") as wav:
            if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getcomptype()) != (1, 2, 16000, "NONE"):
                raise ValueError("not normalized PCM")
            count = wav.getnframes()
            if count > 480_000:
                raise ValueError("too long")
            pcm = wav.readframes(count)
            if len(pcm) != count * 2:
                raise ValueError("truncated WAV")
    except (ValueError, EOFError, wave.Error):
        raise SpeakerIdentityError("Нужен полный WAV mono PCM16 16 кГц длиной 5–30 секунд.") from None
    duration = count / 16000
    if not MIN_SAMPLE_SECONDS <= duration <= MAX_SAMPLE_SECONDS:
        raise SpeakerIdentityError("Нужен образец 5–30 секунд.")
    values = array.array("h")
    values.frombytes(pcm)
    import sys
    if sys.byteorder != "little":
        values.byteswap()
    rms = math.sqrt(sum(v*v for v in values) / len(values))
    clipped = sum(abs(v) >= 32700 for v in values) / len(values)
    frames = [values[i:i + 320] for i in range(0, len(values), 320)]
    active = sum(math.sqrt(sum(v*v for v in f) / len(f)) >= 200 for f in frames) / len(frames)
    stats = {"rms": round(rms, 2), "active_ratio": round(active, 3), "clipped_ratio": round(clipped, 4)}
    return pcm, duration, stats


def clip_has_speech(path, min_rms=200.0):
    """Signal-quality rejection only, NOT a speech detector or diarization guarantee."""
    try:
        _, _, stats = _wav_info(pathlib.Path(path).read_bytes())
        return stats["rms"] >= min_rms and stats["active_ratio"] >= 0.6 and stats["clipped_ratio"] < 0.01
    except (SpeakerIdentityError, OSError):
        return False


def extract_interval(source, dest, start, end):
    """One canonical extractor for preview/save; duration must match requested bounds."""
    import io
    import wave
    start, end = _finite(start), _finite(end)
    if start is None or end is None or start < 0 or not MIN_SAMPLE_SECONDS <= end - start <= MAX_SAMPLE_SECONDS:
        return None
    output = pathlib.Path(dest)
    try:
        # Keep decoded output bounded. No normalization/AGC to turn silence into noise.
        result = subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-ss", f"{start:.3f}", "-i", str(source),
             "-t", f"{end-start:.3f}", "-vn", "-ac", "1", "-ar", "16000", "-f", "s16le", "pipe:1"],
            check=True, capture_output=True, timeout=30,
        )
        pcm = result.stdout
        actual = len(pcm) / 32000
        if len(pcm) % 2 or len(pcm) > 960000 or abs(actual - (end - start)) > 0.002:
            return None
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(pcm)
        raw = buffer.getvalue()
        _wav_info(raw)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(raw)
        return start, actual
    except (OSError, subprocess.SubprocessError, SpeakerIdentityError):
        output.unlink(missing_ok=True)
        return None


def overlapping_confirmed_sample(session, meeting_id, start, end, content_sha256=""):
    from .db import EmployeeVoiceSample
    if content_sha256:
        row = session.query(EmployeeVoiceSample).filter_by(content_sha256=content_sha256).first()
        if row is not None:
            return row
    if meeting_id is None:
        return None
    return (session.query(EmployeeVoiceSample)
            .filter(EmployeeVoiceSample.source_meeting_id == meeting_id,
                    EmployeeVoiceSample.source_start < end, EmployeeVoiceSample.source_end > start)
            .first())


def _not_expired(row):
    from .db import now
    import datetime as dt
    expires = getattr(row, "expires_at", None)
    if expires is None:
        return False
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=dt.timezone.utc)
    return expires > now()


def sample_has_usable_embedding(row):
    if (getattr(row, "embedding_status", "") != "ready" or not _not_expired(row)
            or not getattr(row, "consent_at", None) or not getattr(row, "consent_version", "")
            or not getattr(row, "confirmed_by", "") or not getattr(row, "encrypted_audio", None)
            or getattr(row, "embedding_dimensions", 0) != 192
            or not getattr(row, "model_version", "")
            or row.model_version in ("unknown", "local-confirmed-v1")
            or not getattr(row, "content_sha256", "")
            or not MIN_SAMPLE_SECONDS <= (getattr(row, "sample_duration", 0) or 0) <= MAX_SAMPLE_SECONDS):
        return False
    quality = getattr(row, "quality_metrics", None) or {}
    if not (quality.get("rms", 0) >= 200 and quality.get("active_ratio", 0) >= 0.6
            and quality.get("clipped_ratio", 1) < 0.01):
        return False
    try:
        _normalise_embedding(json.loads(decrypt_voiceprint(row.encrypted_embedding)))
    except (SpeakerIdentityError, ValueError, TypeError):
        return False
    return True


def independent_sample_count(samples):
    """One distinct PCM clip per independent source recording, never repeated fragments."""
    hashes, sources = set(), set()
    for row in samples:
        if not sample_has_usable_embedding(row):
            continue
        content, source = row.content_sha256, getattr(row, "source_audio_sha256", "")
        if not source or content in hashes or source in sources:
            continue
        hashes.add(content)
        sources.add(source)
    return len(sources)


def identification_readiness(samples_by_employee):
    """Quantity/consistency collection stop, never calibrated recognition readiness.

    Caller includes all active employees, including empty lists. Different recordings,
    compatible model, pairwise within-person agreement and rival separation required.
    """
    details = []
    valid_by_employee = {eid: [r for r in rows if sample_has_usable_embedding(r)]
                         for eid, rows in samples_by_employee.items()}
    # Every embedding is decrypted and measured once: the directory recomputes this on each card open
    # and save. similarity() gives exactly _cosine_similarity() (same sums in the same order).
    def prepared(row):
        vector = _normalise_embedding(json.loads(decrypt_voiceprint(row.encrypted_embedding)))
        return vector, math.sqrt(sum(value ** 2 for value in vector))

    def similarity(first, second):
        (left, left_norm), (right, right_norm) = first, second
        if left_norm < 1e-8 or right_norm < 1e-8:
            return -1.0
        return sum(map(operator.mul, left, right)) / (left_norm * right_norm)

    vectors_by_employee = {eid: [prepared(r) for r in rows] for eid, rows in valid_by_employee.items()}
    for eid, rows in valid_by_employee.items():
        count = independent_sample_count(rows)
        models = {r.model_version for r in rows}
        vectors = vectors_by_employee[eid]
        consistent = len(models) == 1 and all(
            similarity(a, b) >= 0.75 for i, a in enumerate(vectors) for b in vectors[i+1:])
        separated = True
        for other_id, other_rows in valid_by_employee.items():
            if other_id == eid or not separated:
                continue
            for other, other_vector in zip(other_rows, vectors_by_employee[other_id]):
                if other.model_version in models and any(similarity(v, other_vector) > 0.60 for v in vectors):
                    separated = False
                    break
        enough = count >= READY_INDEPENDENT_SAMPLES and consistent and separated
        details.append({"employee_id": eid, "independent_samples": count, "ready": enough})
    enough = bool(details) and all(p["ready"] for p in details)
    return {"ready": enough, "collection_sufficient": enough, "identity_validated": False,
            "calibrated": False, "heuristic": READINESS_HEURISTIC,
            "min_independent_samples": READY_INDEPENDENT_SAMPLES,
            "enrolled_people": sum(bool(rows) for rows in valid_by_employee.values()),
            "ready_people": sum(p["ready"] for p in details), "people": details}


def decrypt_sample_audio(row):
    if not getattr(row, "encrypted_audio", None):
        raise SpeakerIdentityError("В записи нет зашифрованного аудио.")
    try:
        raw = _fernet().decrypt(bytes(row.encrypted_audio))
    except (InvalidToken, ValueError, TypeError):
        raise SpeakerIdentityError("Не удалось расшифровать аудио образца.") from None
    if len(raw) > MAX_DB_AUDIO_BYTES:
        raise SpeakerIdentityError("Образец превышает лимит.")
    return raw


def _accept_embedding(row, embedded):
    vector = _normalise_embedding(embedded.get("embedding"))
    model = str(embedded.get("model_version") or "")
    if not model or len(model) > 120 or model in ("unknown", "local-confirmed-v1"):
        raise SpeakerIdentityError("Нет проверяемой версии голосовой модели.")
    # Meeting enrollment uses purpose=segment: the service must not persist a second copy.
    if embedded.get("sample_blob_path"):
        raise SpeakerIdentityError("Сервис неожиданно сохранил внешнюю копию. Проверьте режим segment.")
    waveform = embedded.get("waveform") or []
    if not isinstance(waveform, list) or any(_finite(v) is None or not 0 <= float(v) <= 1 for v in waveform[:128]):
        raise SpeakerIdentityError("Некорректная диаграмма признаков.")
    row.encrypted_embedding = encrypt_voiceprint(json.dumps(vector))
    row.embedding_dimensions = len(vector)
    row.model_version = model
    row.waveform_data = waveform[:128]
    row.embedding_status = "ready"


def embed_pending_sample(session, row):
    """Explicit authorized retry only. Inference is transient; encrypted DB audio is retained."""
    if not _not_expired(row) or not row.consent_at or not row.confirmed_by or not row.employee.active:
        raise SpeakerIdentityError("Согласие/срок хранения недействительны.")
    if sample_has_usable_embedding(row):
        return True
    row.encrypted_embedding = b""
    row.embedding_dimensions = 0
    row.model_version = ""
    if not config.voice_service_enabled():
        row.embedding_status = "pending"
        return False
    raw = decrypt_sample_audio(row)
    with tempfile.TemporaryDirectory(prefix="sysai_embed_pending_") as tmp:
        path = pathlib.Path(tmp) / "sample.wav"
        path.write_bytes(raw)
        try:
            _accept_embedding(row, embed_voice_sample(path, row.employee_id))
        except SpeakerIdentityError:
            row.embedding_status = "failed"
            row.encrypted_embedding = b""
            return False
    return True


def revoke_employee_voice_data(session, employee_id):
    """Local revocation is immediate; failed legacy remote cleanup remains queued separately. The voice remembered
    between meetings (voice_memory.py) is deleted too; the consent to remember it stays as it was."""
    from .db import EmployeeVoiceMemory, EmployeeVoiceprint, EmployeeVoiceSample, VoiceSampleCleanup
    session.query(EmployeeVoiceMemory).filter_by(employee_id=employee_id).delete(synchronize_session=False)
    rows = session.query(EmployeeVoiceSample).filter_by(employee_id=employee_id).all()
    for row in rows:
        pathname = row.sample_blob_path or ""
        if pathname:
            session.add(VoiceSampleCleanup(employee_id=employee_id, storage_path=pathname))
        session.delete(row)
    profile = session.query(EmployeeVoiceprint).filter_by(employee_id=employee_id).one_or_none()
    if profile is not None:
        session.delete(profile)
    return len(rows)


def purge_expired_samples(session):
    from .db import EmployeeVoiceMemory, EmployeeVoiceSample, VoiceSampleCleanup, now
    rows = session.query(EmployeeVoiceSample).filter(EmployeeVoiceSample.expires_at <= now()).all()
    for row in rows:
        if row.sample_blob_path:
            session.add(VoiceSampleCleanup(employee_id=row.employee_id, storage_path=row.sample_blob_path))
        session.delete(row)
    # voices remembered between meetings (voice_memory.py) have the same term
    session.query(EmployeeVoiceMemory).filter(EmployeeVoiceMemory.expires_at <= now()).delete(synchronize_session=False)
    return len(rows)


def retry_sample_cleanup(session):
    """Authorized operator maintenance. Never delete arbitrary paths from database values."""
    from .db import VoiceSampleCleanup
    for row in session.query(VoiceSampleCleanup).all():
        path = row.storage_path
        try:
            if path.startswith(f"profiles/employee-{row.employee_id}/") and ".." not in path:
                delete_remote_voice_sample(row.employee_id, path)
            else:
                root = (config.DATA_DIR / "voice_samples").resolve()
                candidate = pathlib.Path(path).resolve()
                if not candidate.is_relative_to(root):
                    continue  # preserve unknown references for manual review
                candidate.unlink(missing_ok=True)
            session.delete(row)
        except (SpeakerIdentityError, OSError):
            continue


def store_confirmed_meeting_sample(session, *, employee_id, meeting_id, start, end, wav_path,
                                   consent_version=CONFIRMED_CONSENT_VERSION,
                                   confirmed_by="", access_owner="", source_audio_sha256=""):
    from .db import EmployeeVoiceSample, Employee, Meeting, now
    from sqlalchemy.exc import IntegrityError
    import datetime as dt
    if config.weak_secret_key():
        raise SpeakerIdentityError("Для образцов нужен надёжный постоянный SECRET_KEY.")
    if not confirmed_by or not source_audio_sha256:
        raise SpeakerIdentityError("Нет подтверждения человека или источника записи.")
    employee = session.get(Employee, employee_id)
    if employee is None or not employee.active or session.get(Meeting, meeting_id) is None:
        raise SpeakerIdentityError("Сотрудник или встреча недоступны.")
    path = pathlib.Path(wav_path)
    if path.stat().st_size > MAX_DB_AUDIO_BYTES:
        raise SpeakerIdentityError("Образец превышает лимит 1 МБ.")
    raw = path.read_bytes()
    pcm, duration, quality = _wav_info(raw)
    if not clip_has_speech(path) or abs(duration - (end - start)) > 0.002:
        raise SpeakerIdentityError("Образец слишком тихий, перегружен или не соответствует интервалу.")
    digest = hashlib.sha256(pcm).hexdigest()  # WAV headers/container metadata are not evidence
    if overlapping_confirmed_sample(session, meeting_id, start, end, digest):
        raise SpeakerIdentityError("Этот фрагмент уже сохранён; повтор не является новым образцом.")
    row = EmployeeVoiceSample(
        employee_id=employee_id, sample_blob_path="", encrypted_audio=_fernet().encrypt(raw),
        audio_mime="audio/wav", audio_bytes=len(raw), encrypted_embedding=b"", embedding_status="pending",
        embedding_dimensions=0, model_version="", sample_duration=duration, waveform_data=[],
        source_kind="meeting_confirmed", source_meeting_id=meeting_id, source_start=start, source_end=end,
        content_sha256=digest, source_audio_sha256=source_audio_sha256, quality_metrics=quality,
        confirmed_by=confirmed_by, access_owner=access_owner, consent_version=consent_version,
        consent_at=now(), expires_at=now() + dt.timedelta(days=VOICE_SAMPLE_RETENTION_DAYS),
    )
    # Reservation happens before inference. DB UNIQUE(hash) serializes identical confirmations
    # across workers; no external uploads or duplicate profile creation on retry.
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError as exc:
        if session.query(EmployeeVoiceSample.id).filter_by(content_sha256=digest).first():
            raise SpeakerIdentityError("Этот фрагмент уже сохранён; повтор не является новым образцом.") from exc
        raise
    embed_pending_sample(session, row)
    return row


_last_retention_run = 0.0


def purge_due_voice_samples():
    global _last_retention_run
    if time.monotonic() - _last_retention_run < 3600:
        return
    from .db import SessionLocal
    with SessionLocal() as session:
        purge_expired_samples(session)
        session.commit()
    _last_retention_run = time.monotonic()
