"""Where in a recording someone speaks: the Silero VAD detector (MIT, github.com/snakers4/silero-vad) on the CPU.

The detector gives every 32 ms of audio the probability that a voice is heard. Recognition uses it three ways:
- the recording is cut into parts in pauses, never in the middle of a word (audio.plan_chunks);
- after recognition, voice with no words of the transcript over it is heard again by a second engine (verify.py);
- «Субтитры сделал…» and other phrases a recognition engine invents over silence are dropped only where no voice is.

The model is a 2 MB ONNX file in app/models, run by onnxruntime on one thread: about 10 seconds per hour of audio on a
core. Several 30-second stretches are run side by side as one batch (shorter stretches lose accuracy). Audio is decoded
by ffmpeg in 8-minute blocks: two hours of samples at once would take 460 MB of memory.

The detector is an aid: when it is unavailable (no onnxruntime, a damaged file) recognition works as before.
"""
import logging
import pathlib
import subprocess
import threading
import time

from . import checkpoints

log = logging.getLogger("sysai.vad")

MODEL = pathlib.Path(__file__).resolve().parent / "models" / "silero_vad.onnx"
RATE = 16000
WINDOW = 512             # samples per decision: 32 ms
CONTEXT = 64             # samples of the previous window the model hears with each one
FRAME = WINDOW / RATE
STREAM_SECONDS = 30.0    # one batch row is a continuous stretch at least this long
BATCH = 16
BLOCK_SECONDS = 480.0    # decoded at a time (16 rows of 30 s; a multiple of WINDOW samples)
THRESHOLD = 0.5          # speech starts above this probability…
NEG_THRESHOLD = 0.35     # …and ends only below this one (hysteresis: a quiet syllable does not end it)
MIN_SPEECH = 0.25        # shorter bursts (a click, a cough) are not speech
MIN_SILENCE = 0.3        # a shorter dip inside speech is not a pause
PAD = 0.1                # seconds added to both sides of a stretch of speech
VERSION = 1              # bump when the detector or its parameters change: saved results are not reused then

_lock = threading.Lock()
_session = None


class VADError(RuntimeError):
    pass


def available() -> bool:
    """True when the model file and onnxruntime are present."""
    if not MODEL.is_file():
        return False
    try:
        import numpy  # noqa: F401
        import onnxruntime  # noqa: F401
    except Exception:  # noqa: BLE001  an optional aid: no package, no detector
        return False
    return True


def _model():
    global _session
    with _lock:
        if _session is None:
            import onnxruntime as ort
            options = ort.SessionOptions()
            options.intra_op_num_threads = 1  # a small server: leave the other cores to the web requests
            options.inter_op_num_threads = 1
            options.log_severity_level = 3
            _session = ort.InferenceSession(str(MODEL), sess_options=options, providers=["CPUExecutionProvider"])
        return _session


def _blocks(path):
    """Mono 16 kHz samples (float32, −1…1) of a recording, BLOCK_SECONDS at a time."""
    import numpy as np
    command = ["ffmpeg", "-v", "error", "-nostdin", "-i", str(path), "-map", "0:a:0", "-ac", "1", "-ar", str(RATE),
               "-f", "s16le", "-"]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    size = int(BLOCK_SECONDS * RATE) * 2
    produced = False
    try:
        while True:
            raw = process.stdout.read(size)
            if not raw:
                break
            produced = True
            if len(raw) % 2:
                raw = raw[:-1]
            yield np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    finally:
        process.stdout.close()
        if process.poll() is None:
            process.kill()
        code = process.wait()
    if code != 0 and not produced:
        raise VADError(f"ffmpeg не прочитал запись (код {code})")


def _run(samples, session):
    """Speech probability of every whole 32 ms window of one block."""
    import numpy as np
    frames = len(samples) // WINDOW
    if frames == 0:
        return np.zeros(0, dtype=np.float32)
    rows = min(BATCH, max(1, frames // int(STREAM_SECONDS / FRAME)))
    length = -(-frames // rows)
    padded = np.zeros(rows * length * WINDOW, dtype=np.float32)
    padded[:frames * WINDOW] = samples[:frames * WINDOW]
    streams = padded.reshape(rows, length, WINDOW)
    state = np.zeros((2, rows, 128), dtype=np.float32)
    context = np.zeros((rows, CONTEXT), dtype=np.float32)
    rate = np.array(RATE, dtype=np.int64)
    out = np.zeros((rows, length), dtype=np.float32)
    for k in range(length):
        window = np.concatenate([context, streams[:, k, :]], axis=1)
        probability, state = session.run(None, {"input": window, "state": state, "sr": rate})
        out[:, k] = probability[:, 0]
        context = window[:, -CONTEXT:]
    return out.reshape(-1)[:frames]


def probabilities(path, progress=None):
    """Speech probability of every 32 ms of a recording (a numpy array)."""
    import numpy as np
    session = _model()
    parts, seconds = [], 0.0
    for block in _blocks(path):
        parts.append(_run(block, session))
        seconds += len(block) / RATE
        if progress:
            progress(f"Детектор речи: {int(seconds // 60)} мин")
    return np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)


def regions(probs, threshold=THRESHOLD, neg_threshold=NEG_THRESHOLD, min_speech=MIN_SPEECH,
            min_silence=MIN_SILENCE, pad=PAD) -> list:
    """[[start, end, mean probability]] of the stretches of speech, in seconds (Silero's own hysteresis rule)."""
    found, start, quiet, triggered = [], 0, None, False
    count = len(probs)
    for index in range(count):
        value = float(probs[index])
        if value >= threshold:
            quiet = None
            if not triggered:
                triggered, start = True, index
            continue
        if triggered and value < neg_threshold:
            if quiet is None:
                quiet = index
            if (index - quiet) * FRAME < min_silence:
                continue
            if (quiet - start) * FRAME >= min_speech:
                found.append((start, quiet))
            triggered, quiet = False, None
    if triggered and (count - start) * FRAME >= min_speech:
        found.append((start, count))
    total = count * FRAME
    out = []
    for first, last in found:
        begin, end = max(0.0, first * FRAME - pad), min(total, last * FRAME + pad)
        score = float(sum(float(probs[k]) for k in range(first, last)) / max(1, last - first))
        if out and begin <= out[-1][1]:
            previous = out[-1]
            span = (previous[1] - previous[0]) + (end - begin)
            previous[2] = round((previous[2] * (previous[1] - previous[0]) + score * (end - begin)) / max(span, 1e-9), 2)
            previous[1] = round(max(previous[1], end), 2)
        else:
            out.append([round(begin, 2), round(end, 2), round(score, 2)])
    return out


def detect(path, progress=None) -> list:
    return regions(probabilities(path, progress))


def fingerprint(audio_digest: str) -> str:
    return checkpoints.fingerprint({"vad": VERSION, "audio": audio_digest, "threshold": THRESHOLD,
                                    "neg": NEG_THRESHOLD, "min_speech": MIN_SPEECH, "min_silence": MIN_SILENCE,
                                    "pad": PAD})


def speech(path, checkpoint=None, audio_digest: str = "", progress=None):
    """Stretches of speech [[start, end, score]] of a recording, saved with the meeting's other results; None when
    the detector is unavailable or failed (recognition then works without it)."""
    if not available():
        return None
    key = fingerprint(audio_digest or str(path))
    cached = checkpoint.get("vad", "0", key) if checkpoint else None
    if isinstance(cached, dict) and isinstance(cached.get("speech"), list):
        return cached["speech"]
    started = time.monotonic()
    try:
        found = detect(path, progress)
    except Exception as exc:  # noqa: BLE001  an aid: recognition goes on without it
        log.warning("voice activity detection failed: %s", exc)
        return None
    if checkpoint:
        try:
            checkpoint.put("vad", "0", key, {"speech": found, "seconds": round(time.monotonic() - started, 1)})
        except Exception:  # noqa: BLE001
            log.warning("voice activity result was not saved", exc_info=True)
    return found


def pauses(speech_regions, total: float, minimum: float = MIN_SILENCE) -> list:
    """[[start, end]] of the silences between the stretches of speech (and before the first, after the last)."""
    out, at = [], 0.0
    for item in sorted(speech_regions or [], key=lambda r: r[0]):
        if item[0] - at >= minimum:
            out.append([round(at, 2), round(item[0], 2)])
        at = max(at, item[1])
    if total - at >= minimum:
        out.append([round(at, 2), round(total, 2)])
    return out


def speech_in(speech_regions, start: float, end: float) -> float:
    """Seconds of speech between two moments."""
    return sum(max(0.0, min(end, item[1]) - max(start, item[0])) for item in speech_regions or [])


def total_speech(speech_regions) -> float:
    return sum(max(0.0, item[1] - item[0]) for item in speech_regions or [])
