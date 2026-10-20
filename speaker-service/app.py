"""SYSAI speaker embedding microservice for Vercel.

The service only extracts voice embeddings and stores consented reference samples
in private Vercel Blob. Employee matching and the authoritative employee records
remain in SYSAI's main database on Render.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import io
import json
import logging
import os
import pathlib
import subprocess
import tempfile
import threading
import urllib.request
import uuid
from typing import Any

import numpy as np
from vercel import blob
import soundfile as sf
from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

log = logging.getLogger("sysai.speaker_service")
logging.basicConfig(level=logging.INFO)

MODEL_REVISION = "a9cb932"
MODEL_REPO = "https://huggingface.co/vedk00/ecapa-voxceleb-speaker-embedding-onnx"
MODEL_URL = f"{MODEL_REPO}/resolve/{MODEL_REVISION}/model/ecapa-speaker-v1.onnx"
FBANK_URL = f"{MODEL_REPO}/resolve/{MODEL_REVISION}/model/fbank-80x201-f32.bin"
MODEL_VERSION = "speechbrain-ecapa-voxceleb-onnx-a9cb932"
EMBEDDING_DIM = 192
SAMPLE_RATE = 16_000
MAX_UPLOAD_BYTES = 4 * 1024 * 1024
PROFILE_MIN_SECONDS = 5.0
PROFILE_MAX_SECONDS = 30.0
SEGMENT_MIN_SECONDS = 1.0
SEGMENT_MAX_SECONDS = 300.0
MAX_SEGMENTS = 150
MAX_SPEAKERS = 30

CACHE_DIR = pathlib.Path(os.getenv("VOICE_MODEL_DIR", "/tmp/sysai-voice-model"))
_MODEL = None
_FBANK = None
_MODEL_LOCK = threading.Lock()

app = FastAPI(
    title="SYSAI Speaker Identity API",
    version="0.1.0",
    description="Private, self-hosted-compatible speaker embedding API for SYSAI.",
)


def _check_token(value: str | None) -> None:
    expected = os.getenv("SYSAI_VOICE_SERVICE_TOKEN", "")
    if not expected:
        raise HTTPException(status_code=503, detail="Voice service token is not configured")
    if not value or not hmac.compare_digest(value, expected):
        raise HTTPException(status_code=401, detail="Unauthorized")


def _cached_download(url: str, filename: str, max_bytes: int) -> pathlib.Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    target = CACHE_DIR / filename
    if target.exists() and target.is_file() and 0 < target.stat().st_size <= max_bytes:
        return target
    temp = target.with_suffix(target.suffix + ".part")
    try:
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "SYSAI-Speaker-Service/0.1", "Accept": "application/octet-stream"},
        )
        with urllib.request.urlopen(request, timeout=90) as response, temp.open("wb") as out:
            length = response.headers.get("Content-Length")
            if length and int(length) > max_bytes:
                raise RuntimeError("Model artifact is larger than the allowed download size")
            total = 0
            while True:
                block = response.read(1024 * 1024)
                if not block:
                    break
                total += len(block)
                if total > max_bytes:
                    raise RuntimeError("Model artifact is larger than the allowed download size")
                out.write(block)
        if temp.stat().st_size < 1024:
            raise RuntimeError("Downloaded model artifact is empty or incomplete")
        temp.replace(target)
        return target
    except Exception:
        temp.unlink(missing_ok=True)
        raise


def _load_model():
    global _MODEL, _FBANK
    if _MODEL is not None and _FBANK is not None:
        return _MODEL, _FBANK
    with _MODEL_LOCK:
        if _MODEL is not None and _FBANK is not None:
            return _MODEL, _FBANK
        try:
            import onnxruntime as ort

            model_path = _cached_download(MODEL_URL, "ecapa-speaker-v1.onnx", 128 * 1024 * 1024)
            fbank_path = _cached_download(FBANK_URL, "fbank-80x201-f32.bin", 1024 * 1024)
            fbank = np.fromfile(fbank_path, dtype="<f4")
            if fbank.size != 80 * 201:
                raise RuntimeError("Unexpected fbank table size")
            # The model package stores 80 mel filters by 201 FFT bins.
            fbank = fbank.reshape(80, 201).astype(np.float32, copy=False)
            options = ort.SessionOptions()
            options.intra_op_num_threads = 1
            options.inter_op_num_threads = 1
            options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
            session = ort.InferenceSession(
                str(model_path),
                sess_options=options,
                providers=["CPUExecutionProvider"],
            )
            _MODEL = session
            _FBANK = fbank
            log.info("Loaded speaker model %s; inputs=%s outputs=%s",
                     MODEL_VERSION,
                     [(item.name, item.shape, item.type) for item in session.get_inputs()],
                     [(item.name, item.shape, item.type) for item in session.get_outputs()])
            return _MODEL, _FBANK
        except Exception as exc:
            log.exception("Could not load speaker embedding model")
            raise HTTPException(
                status_code=503,
                detail="Speaker model could not be loaded. Check runtime logs.",
            ) from exc


def _decode_audio(raw: bytes) -> tuple[np.ndarray, int]:
    """Decode supported audio to mono float32. FFmpeg is bundled for MP3/other formats."""
    try:
        samples, sample_rate = sf.read(io.BytesIO(raw), dtype="float32", always_2d=True)
        if samples.size and sample_rate > 0:
            mono = samples.mean(axis=1, dtype=np.float32)
            if sample_rate != SAMPLE_RATE:
                from scipy.signal import resample_poly
                from math import gcd
                divisor = gcd(int(sample_rate), SAMPLE_RATE)
                mono = resample_poly(mono, SAMPLE_RATE // divisor, int(sample_rate) // divisor).astype(np.float32)
            return np.nan_to_num(mono, copy=False), SAMPLE_RATE
    except Exception:
        pass

    # MP3 may not be supported by the system libsndfile. Decode via the bundled ffmpeg binary.
    try:
        import imageio_ffmpeg

        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        command = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
            "-i", "pipe:0", "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE),
            "-f", "wav", "-acodec", "pcm_s16le", "pipe:1",
        ]
        result = subprocess.run(command, input=raw, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=25, check=False)
        if result.returncode != 0 or not result.stdout:
            raise ValueError("Unsupported audio format")
        samples, sample_rate = sf.read(io.BytesIO(result.stdout), dtype="float32", always_2d=True)
        mono = samples.mean(axis=1, dtype=np.float32)
        if sample_rate != SAMPLE_RATE:
            from scipy.signal import resample_poly
            from math import gcd
            divisor = gcd(int(sample_rate), SAMPLE_RATE)
            mono = resample_poly(mono, SAMPLE_RATE // divisor, int(sample_rate) // divisor).astype(np.float32)
        return np.nan_to_num(mono, copy=False), SAMPLE_RATE
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=422, detail="Could not decode audio; send WAV or MP3") from exc


def _fbank_features(samples: np.ndarray, filters: np.ndarray) -> np.ndarray:
    """Build SpeechBrain-compatible log-mel features without importing PyTorch."""
    if samples.ndim != 1 or samples.size < 400:
        raise HTTPException(status_code=422, detail="Audio is too short for speaker embedding")
    frame_length, hop_length, n_fft = 400, 160, 400
    # Equivalent to a periodic Hann window; pad both sides to center analysis windows.
    window = (0.5 - 0.5 * np.cos(2.0 * np.pi * np.arange(frame_length) / frame_length)).astype(np.float32)
    padded = np.pad(samples.astype(np.float32, copy=False), (frame_length // 2, frame_length // 2), mode="reflect")
    frames = np.lib.stride_tricks.sliding_window_view(padded, frame_length)[::hop_length]
    spectrum = np.fft.rfft(frames * window[None, :], n=n_fft, axis=1)
    power = (spectrum.real * spectrum.real + spectrum.imag * spectrum.imag).astype(np.float32)
    mel_energy = power @ filters.T
    features = np.log(np.maximum(mel_energy, 1e-10)).astype(np.float32)
    # SpeechBrain model uses sentence-level mean subtraction per mel bin.
    features -= features.mean(axis=0, keepdims=True)
    return np.ascontiguousarray(features, dtype=np.float32)


def _run_embedding(samples: np.ndarray) -> list[float]:
    session, filters = _load_model()
    features = _fbank_features(samples, filters)
    inputs = session.get_inputs()
    if not inputs:
        raise HTTPException(status_code=503, detail="Speaker model has no input")
    primary = inputs[0]
    shape = primary.shape
    if len(shape) == 3 and shape[1] == 80:
        tensor = features.T[None, :, :]
    elif len(shape) == 3:
        tensor = features[None, :, :]
    elif len(shape) == 2:
        tensor = features
    else:
        raise HTTPException(status_code=503, detail="Unsupported speaker model input shape")

    feed: dict[str, np.ndarray] = {primary.name: np.ascontiguousarray(tensor, dtype=np.float32)}
    for extra in inputs[1:]:
        name = extra.name.lower()
        if "len" not in name:
            raise HTTPException(status_code=503, detail="Unsupported speaker model input")
        if "int64" in extra.type:
            value = np.asarray([features.shape[0]], dtype=np.int64)
        else:
            value = np.asarray([1.0], dtype=np.float32)
        feed[extra.name] = value
    outputs = session.run(None, feed)
    candidates = [np.asarray(output, dtype=np.float32).reshape(-1)
                  for output in outputs if np.asarray(output).size == EMBEDDING_DIM]
    if not candidates:
        sizes = [int(np.asarray(output).size) for output in outputs]
        raise HTTPException(status_code=503, detail=f"Unexpected speaker embedding output size: {sizes}")
    vector = candidates[0]
    norm = float(np.linalg.norm(vector))
    if not np.isfinite(norm) or norm < 1e-8:
        raise HTTPException(status_code=422, detail="Audio did not produce a usable voice embedding")
    vector = vector / norm
    return [float(round(value, 8)) for value in vector]


def _duration(samples: np.ndarray) -> float:
    return float(samples.size / SAMPLE_RATE)


def _waveform(samples: np.ndarray, bins: int = 96) -> list[float]:
    chunks = np.array_split(samples, min(bins, max(1, samples.size)))
    peaks = [float(np.max(np.abs(chunk))) if chunk.size else 0.0 for chunk in chunks]
    maximum = max(peaks, default=0.0)
    if maximum > 0:
        peaks = [round(value / maximum, 4) for value in peaks]
    return peaks


def _encode_wav(samples: np.ndarray) -> bytes:
    """Encode a short mono clip as PCM16 WAV for private reference storage."""
    buffer = io.BytesIO()
    sf.write(buffer, np.asarray(samples, dtype=np.float32), SAMPLE_RATE, format="WAV", subtype="PCM_16")
    return buffer.getvalue()


def _slice_speech(samples: np.ndarray, segments: list[dict[str, Any]]) -> dict[str, np.ndarray]:
    grouped: dict[str, list[np.ndarray]] = {}
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        speaker = str(segment.get("speaker", "")).strip()
        if not speaker:
            continue
        try:
            start, end = float(segment["start"]), float(segment["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if not np.isfinite(start) or not np.isfinite(end) or end <= start:
            continue
        start_index = max(0, min(samples.size, int(start * SAMPLE_RATE)))
        end_index = max(start_index, min(samples.size, int(end * SAMPLE_RATE)))
        if end_index - start_index < int(SEGMENT_MIN_SECONDS * SAMPLE_RATE):
            continue
        grouped.setdefault(speaker, []).append(samples[start_index:end_index])
    clips: dict[str, np.ndarray] = {}
    for speaker, parts in grouped.items():
        joined = np.concatenate([part for part in parts]) if parts else np.empty(0, dtype=np.float32)
        # Keep inference bounded while combining multiple turns from the same diarized speaker.
        clips[speaker] = joined[:int(30 * SAMPLE_RATE)]
    return clips


@app.get("/")
@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "service": "sysai-speaker-identity",
        "model": MODEL_VERSION,
        "model_loaded": _MODEL is not None,
        "blob_configured": bool(os.getenv("BLOB_READ_WRITE_TOKEN") or os.getenv("VERCEL_OIDC_TOKEN")),
    }


@app.get("/v1/model-info")
def model_info(
    x_sysai_voice_token: str | None = Header(default=None, alias="X-SYSAI-Voice-Token"),
) -> dict[str, Any]:
    """Authenticated model diagnostic; it never returns credentials."""
    _check_token(x_sysai_voice_token)
    session, filters = _load_model()
    return {
        "ok": True,
        "model": MODEL_VERSION,
        "embedding_dimensions": EMBEDDING_DIM,
        "filterbank_shape": list(filters.shape),
        "inputs": [{"name": item.name, "shape": item.shape, "type": item.type} for item in session.get_inputs()],
        "outputs": [{"name": item.name, "shape": item.shape, "type": item.type} for item in session.get_outputs()],
    }


@app.post("/v1/embed")
async def embed(
    audio: UploadFile = File(...),
    purpose: str = Form("segment"),
    employee_id: int | None = Form(None),
    x_sysai_voice_token: str | None = Header(default=None, alias="X-SYSAI-Voice-Token"),
) -> dict[str, Any]:
    _check_token(x_sysai_voice_token)
    if purpose not in {"profile", "segment"}:
        raise HTTPException(status_code=422, detail="purpose must be profile or segment")
    if purpose == "profile" and (employee_id is None or employee_id < 1):
        raise HTTPException(status_code=422, detail="employee_id is required for a profile sample")

    raw = await audio.read(MAX_UPLOAD_BYTES + 1)
    if not raw or len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Audio file is empty or exceeds the 4 MB limit")
    samples, sample_rate = _decode_audio(raw)
    duration = _duration(samples)
    minimum = PROFILE_MIN_SECONDS if purpose == "profile" else SEGMENT_MIN_SECONDS
    maximum = PROFILE_MAX_SECONDS if purpose == "profile" else SEGMENT_MAX_SECONDS
    if duration < minimum or duration > maximum:
        raise HTTPException(
            status_code=422,
            detail=f"Audio duration must be between {minimum:g} and {maximum:g} seconds",
        )
    embedding = await asyncio.to_thread(_run_embedding, samples)

    sample_blob_path = None
    if purpose == "profile":
        if not (os.getenv("BLOB_READ_WRITE_TOKEN") or os.getenv("VERCEL_OIDC_TOKEN")):
            raise HTTPException(status_code=503, detail="Private Vercel Blob storage is not configured")
        try:
            uploaded = await blob.put_blob(
                f"profiles/employee-{employee_id}/{uuid.uuid4().hex}.wav",
                raw,
                access="private",
                content_type="audio/wav",
                add_random_suffix=True,
            )
            sample_blob_path = uploaded.pathname
        except Exception as exc:
            # Keep provider internals and credentials out of client responses, but include a safe
            # error class/status so an authenticated SYSAI caller can distinguish auth/config
            # failures from SDK/runtime failures without access to Vercel runtime logs.
            log.exception("Could not store private reference audio for employee id %s", employee_id)
            diagnostic = type(exc).__name__
            status_code = getattr(exc, "status_code", None)
            error_code = getattr(exc, "code", None)
            if isinstance(status_code, int):
                diagnostic += f"; status={status_code}"
            if isinstance(error_code, str) and error_code:
                diagnostic += f"; code={error_code[:80]}"
            # AttributeError messages name a missing SDK attribute/object, not credentials or
            # request contents, and are needed to diagnose SDK-version mismatches.
            if isinstance(exc, AttributeError) and str(exc):
                diagnostic += f"; info={str(exc)[:120]}"
            raise HTTPException(
                status_code=502,
                detail=f"Could not store the voice sample in private Blob ({diagnostic})",
            ) from exc

    return {
        "embedding": embedding,
        "dimensions": EMBEDDING_DIM,
        "model_version": MODEL_VERSION,
        "sample_rate": sample_rate,
        "duration_seconds": round(duration, 3),
        "waveform": _waveform(samples) if purpose == "profile" else None,
        "sample_blob_path": sample_blob_path,
    }


class IdentifyRequestMeta(BaseModel):
    segments: list[dict[str, Any]] = Field(default_factory=list)


@app.post("/v1/identify")
async def identify(
    audio: UploadFile = File(...),
    segments_json: str = Form(...),
    x_sysai_voice_token: str | None = Header(default=None, alias="X-SYSAI-Voice-Token"),
) -> dict[str, Any]:
    """Embed grouped speaker turns from a diarized chunk in one request."""
    _check_token(x_sysai_voice_token)
    raw = await audio.read(MAX_UPLOAD_BYTES + 1)
    if not raw or len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Audio file is empty or exceeds the 4 MB limit")
    try:
        payload = json.loads(segments_json)
        segments = payload.get("segments", payload) if isinstance(payload, dict) else payload
        if not isinstance(segments, list) or len(segments) > MAX_SEGMENTS:
            raise ValueError("Invalid segment list")
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail="segments_json must contain a list of segments") from exc
    samples, _ = _decode_audio(raw)
    duration = _duration(samples)
    if duration < 1 or duration > SEGMENT_MAX_SECONDS:
        raise HTTPException(status_code=422, detail="Chunk duration must be between 1 and 300 seconds")
    clips = _slice_speech(samples, segments)
    if len(clips) > MAX_SPEAKERS:
        raise HTTPException(status_code=422, detail="Too many distinct speakers in one chunk")
    results = {}
    blob_available = bool(os.getenv("BLOB_READ_WRITE_TOKEN") or os.getenv("VERCEL_OIDC_TOKEN"))
    for speaker, clip in clips.items():
        clip_duration = _duration(clip)
        if clip_duration < SEGMENT_MIN_SECONDS:
            continue
        try:
            item = {
                "embedding": await asyncio.to_thread(_run_embedding, clip),
                "duration_seconds": round(clip_duration, 3),
                "turn_count": sum(1 for part in segments if isinstance(part, dict) and str(part.get("speaker", "")).strip() == speaker),
            }
            # A human can map this unknown speaker after the meeting, when the source meeting
            # audio has already been deleted by Render. Save only a clean short clip privately.
            if blob_available and PROFILE_MIN_SECONDS <= clip_duration <= PROFILE_MAX_SECONDS:
                try:
                    sample_bytes = await asyncio.to_thread(_encode_wav, clip)
                    uploaded = await blob.put_blob(
                        f"candidates/{uuid.uuid4().hex}/{uuid.uuid4().hex}.wav",
                        sample_bytes,
                        access="private",
                        content_type="audio/wav",
                        add_random_suffix=True,
                    )
                    item["sample_blob_path"] = uploaded.pathname
                    item["waveform"] = _waveform(clip)
                except Exception:
                    log.exception("Could not store temporary private speaker candidate clip")
            results[speaker] = item
        except HTTPException:
            raise
    return {"model_version": MODEL_VERSION, "speakers": results}


class PromoteCandidateRequest(BaseModel):
    employee_id: int = Field(ge=1)
    candidate_path: str = Field(min_length=1, max_length=600)


@app.post("/v1/promote")
async def promote_candidate(
    payload: PromoteCandidateRequest,
    x_sysai_voice_token: str | None = Header(default=None, alias="X-SYSAI-Voice-Token"),
) -> dict[str, str]:
    _check_token(x_sysai_voice_token)
    if not payload.candidate_path.startswith("candidates/") or ".." in payload.candidate_path:
        raise HTTPException(status_code=400, detail="Invalid candidate path")
    try:
        result = await blob.copy_blob(
            payload.candidate_path,
            f"profiles/employee-{payload.employee_id}/{uuid.uuid4().hex}.wav",
            access="private",
            content_type="audio/wav",
            add_random_suffix=True,
        )
        try:
            await blob.delete_blob(payload.candidate_path)
        except Exception:
            log.warning("Promoted candidate but could not remove temporary blob %s", payload.candidate_path)
        return {"sample_blob_path": result.pathname}
    except Exception as exc:
        log.exception("Could not promote candidate sample for employee id %s", payload.employee_id)
        raise HTTPException(status_code=502, detail="Could not promote candidate sample") from exc


@app.delete("/v1/candidate")
async def delete_candidate(
    pathname: str,
    x_sysai_voice_token: str | None = Header(default=None, alias="X-SYSAI-Voice-Token"),
) -> dict[str, bool]:
    _check_token(x_sysai_voice_token)
    if not pathname.startswith("candidates/") or ".." in pathname:
        raise HTTPException(status_code=400, detail="Invalid candidate path")
    try:
        await blob.delete_blob(pathname)
    except Exception as exc:
        log.exception("Could not delete temporary speaker candidate")
        raise HTTPException(status_code=502, detail="Could not delete candidate sample") from exc
    return {"ok": True}


class DeleteSampleRequest(BaseModel):
    employee_id: int = Field(ge=1)
    pathname: str = Field(min_length=1, max_length=500)


@app.delete("/v1/sample")
async def delete_sample(
    payload: DeleteSampleRequest,
    x_sysai_voice_token: str | None = Header(default=None, alias="X-SYSAI-Voice-Token"),
) -> dict[str, bool]:
    _check_token(x_sysai_voice_token)
    expected_prefix = f"profiles/employee-{payload.employee_id}/"
    if not payload.pathname.startswith(expected_prefix) or ".." in payload.pathname:
        raise HTTPException(status_code=400, detail="Sample path does not belong to the employee")
    try:
        await blob.delete_blob(payload.pathname)
    except Exception as exc:
        log.exception("Could not delete private reference audio for employee id %s", payload.employee_id)
        raise HTTPException(status_code=502, detail="Could not delete the voice sample") from exc
    return {"ok": True}
