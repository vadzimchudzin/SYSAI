"""Audio preparation with ffmpeg: mono 16 kHz MP3 (speech quality, small upload), split into chunks."""
import json
import math
import pathlib
import shutil
import subprocess
import tempfile


def duration(path: str) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
                         capture_output=True, text=True, check=True).stdout
    value = json.loads(out).get("format", {}).get("duration")
    if value in (None, "N/A"):
        # Browser/live WebM often has no container duration. Decode only in
        # that case; ffmpeg's final audio clock measures the actual recording.
        decoded = subprocess.run(
            ["ffmpeg", "-v", "error", "-nostats", "-i", str(path), "-map", "0:a:0",
             "-progress", "pipe:1", "-f", "null", "-"],
            capture_output=True, text=True, check=True,
        ).stdout
        times = [float(line.partition("=")[2]) / 1_000_000
                 for line in decoded.splitlines() if line.startswith("out_time_us=")
                 and line.partition("=")[2] != "N/A"]
        total = max(times, default=0)
    else:
        total = float(value)
    if not math.isfinite(total) or total <= 0:
        raise ValueError("Не удалось определить длительность аудио")
    return total


def chunk_seconds(total: float, maximum_minutes: float | None = None) -> float:
    """Bound model workload, with smaller parts for lengthy recordings.

    Short recordings remain a single part; no padding, silence trimming or
    volume threshold is applied (quiet speech must not be discarded).
    """
    if not math.isfinite(total) or total <= 0:
        raise ValueError("Некорректная длительность аудио")
    step = 45 if total > 3600 else 60 if total > 600 else min(90, total)
    if maximum_minutes is not None:
        if not math.isfinite(maximum_minutes) or maximum_minutes <= 0:
            raise ValueError("Некорректная длина части аудио")
        # Legacy settings may request smaller parts, never larger than the
        # adaptive safety ceiling. Preserve the extraction minimum of 15 s.
        step = min(step, max(15, maximum_minutes * 60))
    return step


# Cutting in pauses (speech known from the voice detector, vad.py): a part may end this share of its length earlier or
# later than planned, so that the cut falls into a pause instead of a word.
CUT_EARLIER, CUT_LATER = 0.35, 0.15
CUT_MIN_PAUSE = 0.3       # the shortest pause to cut in (s)
CUT_MARGIN = 0.5          # the cut keeps at most this far from the speech on both sides of the pause
CUT_DISTANCE_COST = 0.03  # a longer pause is preferred, but each second away from the planned end costs this much


def _pause_cut(nominal: float, step: float, pauses: list, limit: float):
    """The moment to cut at near ``nominal``: inside a pause of the window, preferring long pauses and a cut close to
    the plan; None when the window has no pause."""
    low, high = nominal - CUT_EARLIER * step, min(nominal + CUT_LATER * step, limit)
    best = None
    for begin, end in pauses:
        if end - begin < CUT_MIN_PAUSE or end <= low or begin >= high:
            continue
        margin = min(CUT_MARGIN, (end - begin) / 2)
        first, last = max(begin + margin, low), min(end - margin, high)
        if first > last:
            continue
        point = min(max(nominal, first), last)
        score = min(end - begin, 2.0) - CUT_DISTANCE_COST * abs(point - nominal)
        if best is None or score > best[0]:
            best = (score, point)
    return None if best is None else best[1]


def plan_chunks(path: str, chunk_minutes: float | None = 30, overlap_seconds: float = 0,
                max_chunk_minutes: float | None = None, speech: list | None = None) -> tuple[float, list[tuple[float, float]]]:
    """Measured duration and the cut plan [(start, seconds)], without cutting anything.

    Cutting a part costs an ffmpeg run (minutes for a long recording on a small server) and a part whose text is already
    saved does not need its audio at all, so the caller cuts only the parts it still has to recognise.

    With ``speech`` (the voice detector's [[start, end, …]]) every cut is moved into a pause near the planned one and
    the parts do not overlap: no word is cut in two, so nothing has to be stitched. Where the detector hears speech for
    the whole window, the cut stays where planned, with the usual overlap.
    """
    total = duration(path)
    if not math.isfinite(overlap_seconds) or overlap_seconds < 0:
        raise ValueError("Некорректное перекрытие частей аудио")
    if chunk_minutes is None:
        step = chunk_seconds(total, max_chunk_minutes)
    else:
        if not math.isfinite(chunk_minutes) or chunk_minutes <= 0:
            raise ValueError("Некорректная длина части аудио")
        step = max(15, chunk_minutes * 60)
    plan = []
    start = 0.0
    if speech is None:
        while start < total:
            plan.append((start, min(step + overlap_seconds, total - start)))
            start += step
        return total, plan
    from .vad import pauses as find_pauses
    silent = find_pauses(speech, total, CUT_MIN_PAUSE)
    while start < total:
        if total - start <= step * (1 + CUT_LATER):  # the rest is one part: no short tail part
            plan.append((start, total - start))
            break
        point = _pause_cut(start + step, step, silent, total)
        if point is None or point <= start + 1.0:
            plan.append((start, min(step + overlap_seconds, total - start)))
            start += step
        else:
            plan.append((start, round(point - start, 3)))
            start = round(point, 3)
    return total, plan


def prepare_chunks(path: str, chunk_minutes: float | None = 30, overlap_seconds: float = 0,
                   max_chunk_minutes: float | None = None) -> tuple[float, list[tuple[float, pathlib.Path]]]:
    """Return measured duration and chunks, all cut at once; None selects adaptive part lengths."""
    total, plan = plan_chunks(path, chunk_minutes, overlap_seconds, max_chunk_minutes)
    work = pathlib.Path(tempfile.mkdtemp(prefix="sysai_"))
    chunks = []
    try:
        for i, (start, seconds) in enumerate(plan):
            dst = work / f"chunk_{i:03d}.mp3"
            extract(path, dst, start, seconds)
            chunks.append((start, dst))
    except Exception:
        shutil.rmtree(work)
        raise
    return total, chunks


# Speech clean-up before recognition: a gentle FFT noise reduction (fans, air conditioning, hum). Measured on the speech
# benchmark (scripts/speech_benchmark.py): stronger chains — loudness normalisation, dynamic normalisation, aggressive
# denoise — made recognition WORSE (WER 10% → 12–14%), the gentle one helped a little. Mono 16 kHz as speech models expect.
CLEANUP = "afftdn=nr=6:nf=-40"


def cleanup_enabled() -> bool:
    try:
        from . import settings_store
        value = settings_store.get("audio_cleanup")
    except Exception:  # noqa: BLE001  no database (tools, early start)
        return True
    return True if value is None else bool(value)


def extract(source, destination, start, seconds, cleanup=None):
    if not math.isfinite(start) or start < 0 or not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("Некорректные границы части аудио")
    cleanup = cleanup_enabled() if cleanup is None else cleanup
    command = ["ffmpeg", "-y", "-v", "error", "-ss", str(start), "-t", str(seconds), "-i", str(source)]
    if cleanup:
        command += ["-af", CLEANUP]
    subprocess.run(command + ["-ac", "1", "-ar", "16000", "-b:a", "32k", str(destination)],
                   check=True, capture_output=True)
    if not pathlib.Path(destination).is_file() or pathlib.Path(destination).stat().st_size == 0:
        raise ValueError("Пустая часть аудио после преобразования")
