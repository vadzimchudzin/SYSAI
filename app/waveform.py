"""Loudness outline of a recording for the voice diagram on the meeting page.

The original audio is deleted after processing, so the outline is computed once, while the file is still on disk,
and kept in the database: a few thousand integers 0..100 are enough to draw the wave at any zoom of the page.
"""
import array
import logging
import math
import random
import subprocess
import sys
import threading

log = logging.getLogger("sysai.waveform")

VERSION = 1
RATE = 4000                 # Hz after resampling: the loudness envelope of speech needs no more
WINDOW = 80                 # samples per elementary peak (20 ms at RATE)
BINS_PER_SECOND = 2
MIN_BINS, MAX_BINS = 600, 4000
MAX_STORED_BINS = 10000     # anything longer in the database is not an outline written by this module


def bins_for(duration: float) -> int:
    """Two values per second, between 600 (short talks still draw smoothly) and 4000 (hours stay compact)."""
    duration = duration if isinstance(duration, (int, float)) and math.isfinite(duration) and duration > 0 else 0
    return int(min(MAX_BINS, max(MIN_BINS, math.ceil(duration * BINS_PER_SECOND))))


def _percentile(sorted_values: list, share: float) -> float:
    if not sorted_values:
        return 0.0
    return float(sorted_values[min(len(sorted_values) - 1, max(0, int(share * (len(sorted_values) - 1))))])


def outline(window_peaks, duration: float, synthetic: bool = False) -> dict | None:
    """Resample elementary peaks to the stored number of bins and scale them to 0..100.

    The level is relative to the loud part of this recording (98th percentile), and the steady room noise
    (5th percentile, capped) is subtracted so that pauses look like pauses, not like a uniform grey band."""
    count = len(window_peaks)
    if not count or not math.isfinite(duration) or duration <= 0:
        return None
    bins = bins_for(duration)
    values = []
    for index in range(bins):
        lo = index * count // bins
        hi = max(lo + 1, (index + 1) * count // bins)
        chunk = window_peaks[lo:hi]
        values.append(math.sqrt(sum(float(v) * float(v) for v in chunk) / len(chunk)) if chunk else 0.0)
    ordered = sorted(values)
    loud = _percentile(ordered, 0.98)
    if loud <= 0:
        return {"v": VERSION, "duration": round(duration, 2), "peaks": [0] * bins, **({"synthetic": True} if synthetic else {})}
    floor = min(_percentile(ordered, 0.05), loud * 0.25)
    span = max(loud - floor, 1e-9)
    peaks = [int(round(100 * min(1.0, max(0.0, (v - floor) / span)) ** 0.8)) for v in values]
    result = {"v": VERSION, "duration": round(duration, 2), "peaks": peaks}
    if synthetic:
        result["synthetic"] = True
    return result


def compute(path, duration_hint: float | None = None, timeout: float | None = None) -> dict | None:
    """{"v", "duration", "peaks"} of an audio file, or None when ffmpeg cannot decode it.

    The file is streamed through ffmpeg as 4 kHz mono PCM and reduced to one peak per 20 ms on the fly, so a
    three-hour recording needs about a megabyte of memory."""
    if timeout is None:
        hint = duration_hint if isinstance(duration_hint, (int, float)) and math.isfinite(duration_hint) and duration_hint > 0 else 3600
        timeout = min(1800.0, 60.0 + hint * 0.5)
    command = ["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-map", "0:a:0", "-vn", "-sn", "-dn",
               "-ac", "1", "-ar", str(RATE), "-f", "s16le", "pipe:1"]
    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError:
        log.warning("ffmpeg is not available: no waveform")
        return None
    timer = threading.Timer(timeout, process.kill)
    timer.daemon = True
    timer.start()
    peaks, carry, total = array.array("H"), b"", 0
    step = WINDOW * 2
    try:
        while True:
            block = process.stdout.read(1 << 16)
            if not block:
                break
            data = carry + block
            usable = len(data) - len(data) % step
            carry = data[usable:]
            if not usable:
                continue
            samples = array.array("h")
            samples.frombytes(data[:usable])
            if sys.byteorder == "big":
                samples.byteswap()
            total += len(samples)
            for start in range(0, len(samples), WINDOW):
                chunk = samples[start:start + WINDOW]
                peaks.append(max(max(chunk), -min(chunk)))
        if len(carry) >= 2:
            samples = array.array("h")
            samples.frombytes(carry[:len(carry) - len(carry) % 2])
            if sys.byteorder == "big":
                samples.byteswap()
            total += len(samples)
            peaks.append(max(max(samples), -min(samples)))
        process.wait()
    finally:
        timer.cancel()
        if process.poll() is None:
            process.kill()
            process.wait()
        if process.stdout:
            process.stdout.close()
    if process.returncode != 0 or not peaks or not total:
        log.warning("waveform: ffmpeg could not decode %s (exit %s)", path, process.returncode)
        return None
    return outline(peaks, total / RATE)


def synthetic(segments, duration: float, seed: int = 7) -> dict | None:
    """An illustrative outline for the demo meeting, which has a transcript but no recording.

    Speech is drawn where the transcript has words and quiet room noise elsewhere; the result is marked synthetic."""
    try:
        duration = float(duration)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(duration) or duration <= 0:
        return None
    rng = random.Random(seed)
    windows = int(math.ceil(duration * 50))  # one value per 20 ms, like a decoded recording
    values = [rng.uniform(200, 700) for _ in range(windows)]
    voices: dict = {}
    for segment in segments or []:
        if not isinstance(segment, dict):
            continue
        try:
            start, end = float(segment.get("start") or 0), float(segment.get("end") or 0)
        except (TypeError, ValueError):
            continue
        if not (math.isfinite(start) and math.isfinite(end)) or end <= start:
            continue
        speaker = str(segment.get("speaker") or "")
        loudness = voices.setdefault(speaker, rng.uniform(9000, 16000))
        phase = rng.uniform(0, math.pi)
        for index in range(max(0, int(start * 50)), min(windows, int(end * 50))):
            t = index / 50
            syllables = 0.55 + 0.45 * abs(math.sin(t * 5.3 + phase))
            phrases = 0.3 + 0.7 * abs(math.sin(t * 0.45 + phase * 2))
            breath = 0.08 if math.sin(t * 0.9 + phase * 3) > 0.93 else 1.0  # short pauses between phrases
            values[index] = max(values[index], loudness * syllables * phrases * breath * rng.uniform(0.7, 1.0))
    return outline(values, duration, synthetic=True)


def clean(stored) -> dict | None:
    """A stored outline that is safe to draw, or None (old meetings, foreign or damaged data)."""
    if not isinstance(stored, dict) or stored.get("v") != VERSION:
        return None
    peaks, duration = stored.get("peaks"), stored.get("duration")
    if not isinstance(peaks, list) or not peaks or len(peaks) > MAX_STORED_BINS:
        return None
    if not isinstance(duration, (int, float)) or isinstance(duration, bool) or not math.isfinite(duration) or duration <= 0:
        return None
    if any(not isinstance(v, int) or isinstance(v, bool) or not 0 <= v <= 100 for v in peaks):
        return None
    return {"duration": float(duration), "peaks": peaks, "synthetic": bool(stored.get("synthetic"))}
