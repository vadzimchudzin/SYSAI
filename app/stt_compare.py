"""«Сравнение движков»: the speech-to-text engines of OpenRouter side by side on the company's own recordings.

A run takes WINDOWS stretches of about WINDOW_SECONDS from one recording, spread over it and cut in pauses (by the
voice detector; on a meeting with a reference — at the borders of its replicas), and gives every stretch to every engine
with the same names and terms (keyterms). For each engine:
- WER against the reference (эталон, diarization_eval.py) when the meeting has one: its corrected text over the same
  stretches — the honest measure;
- disagreement with the other engines (mean word edit distance to each of them, by the longer text): an engine that
  drops or invents words differs from all the others, the one nearest to the others is the consensus;
- missed speech: seconds of voice (by the detector) that no word of the engine covers — for engines with word times;
- names and terms heard, words, time, cost, errors.
The texts are kept, so a person can read them side by side. «Сделать основным» makes an engine the main one.

Settings «stt_compare» keeps the last run: {"meeting_id", "title", "at", "windows", "reference", "engines": [...],
"best"}.
"""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import logging
import pathlib
import shutil
import tempfile
import threading
import time

from . import asr_eval, audio, checkpoints, config, llm, settings_store, speech_fix, vad, verify
from .db import Meeting, SessionLocal

log = logging.getLogger("sysai.stt_compare")

ENGINES = ("elevenlabs/scribe-v2", "google/gemini-3.5-transcribe", "microsoft/mai-transcribe-2",
           "mistralai/voxtral-small-transcribe-3-0", "nvidia/nemotron-3.5-transcribe", "openai/gpt-transcribe",
           "openai/gpt-4o-transcribe", "assemblyai/universal-3-6-pro")
MAX_ENGINES = 12
WINDOWS = 4
WINDOW_SECONDS = 60.0
SNAP_SECONDS = 10.0       # a window border moves at most this far to fall into a pause
TEXT_LIMIT = 3000         # characters of each text kept for reading

_lock = threading.Lock()
_state = {"running": False, "started": None, "finished": None, "meeting_id": None, "step": "", "error": ""}


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def status() -> dict:
    with _lock:
        return copy.deepcopy(_state)


def _update(**fields) -> None:
    with _lock:
        _state.update(fields)


def last() -> dict:
    value = settings_store.get("stt_compare")
    return value if isinstance(value, dict) else {}


def engines_from(text) -> list:
    """Model ids from a text (one per line or comma separated), in order, without repeats."""
    out = []
    for raw in str(text or "").replace(",", "\n").splitlines():
        model = raw.strip()
        if model and model not in out and len(model) <= 200 and all(c.isalnum() or c in "._:/-~" for c in model):
            out.append(model)
    return out[:MAX_ENGINES]


# ------------------------------------------------------------------------------------------------ windows
def _snap(moment: float, pauses: list, total: float) -> float:
    """The middle of the pause nearest to a moment (within SNAP_SECONDS), or the moment itself."""
    best = None
    for begin, end in pauses:
        middle = min(max(moment, begin + min(0.3, (end - begin) / 2)), end - min(0.3, (end - begin) / 2))
        distance = abs(middle - moment)
        if distance <= SNAP_SECONDS and (best is None or distance < best[0]):
            best = (distance, middle)
    return min(max(0.0, best[1] if best else moment), total)


def windows(total: float, speech=None, reference=None, count: int = WINDOWS, seconds: float = WINDOW_SECONDS) -> list:
    """[[start, end]] of the stretches to compare on: spread over the recording, borders in pauses."""
    seconds = min(seconds, total / max(1, count))
    centres = [total * (k + 0.5) / count for k in range(count)]
    out = []
    if reference:
        rows = sorted((float(s["start"]), float(s.get("end") or s["start"])) for s in reference
                      if isinstance(s, dict) and isinstance(s.get("start"), (int, float)))
        for centre in centres:
            if not rows:
                break
            first = min(range(len(rows)), key=lambda i: abs(rows[i][0] - (centre - seconds / 2)))
            last = min(range(first, len(rows)), key=lambda i: abs(rows[i][1] - (centre + seconds / 2)))
            start, end = rows[first][0], max(rows[last][1], rows[first][0] + 1.0)
            if not out or start >= out[-1][1]:
                out.append([round(start, 2), round(min(end, total), 2)])
        return out
    pauses = vad.pauses(speech, total) if speech else []
    for centre in centres:
        start = _snap(max(0.0, centre - seconds / 2), pauses, total)
        end = _snap(min(total, start + seconds), pauses, total)
        if end - start >= 5 and (not out or start >= out[-1][1]):
            out.append([round(start, 2), round(end, 2)])
    return out


def reference_text(reference: list, start: float, end: float) -> str:
    """The corrected words of the replicas of a reference that lie in a window."""
    parts = []
    for item in reference or []:
        if not isinstance(item, dict) or not isinstance(item.get("start"), (int, float)):
            continue
        finish = item.get("end") if isinstance(item.get("end"), (int, float)) else item["start"]
        middle = (float(item["start"]) + float(finish)) / 2
        if start - 0.05 <= middle <= end + 0.05:
            parts.append(str(item.get("text") or ""))
    return " ".join(parts).strip()


# ------------------------------------------------------------------------------------------------ one run
def _missed(speech, words, start: float, end: float) -> float:
    """Seconds of voice in a window that no word covers (pieces of a second and longer)."""
    inside = [[max(start, a), min(end, b), *rest] for a, b, *rest in speech or [] if b > start and a < end]
    spans = [[float(w[1]), float(w[2])] for w in words]
    return sum(g[1] - g[0] for g in verify.gaps(inside, spans))


def _terms(text: str, terms: list) -> int:
    """How many of the names and terms are heard (any case form: the first five letters)."""
    words = " ".join(asr_eval.normalize(text))
    count = 0
    for term in terms:
        stem = speech_fix._base(term).lower().replace("ё", "е")[:5]
        if len(stem) >= 3 and stem in words:
            count += 1
    return count


def _hear(engine: str, clip: bytes, language: str, keyterms: list) -> dict:
    started = time.monotonic()
    result = llm.transcribe_audio(clip, engine, "mp3", language, keyterms=keyterms)
    usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
    cost = usage.get("cost") if isinstance(usage.get("cost"), (int, float)) else 0.0
    return {"text": str(result.get("text") or "").strip(), "words": verify._timed(result, 0.0),
            "seconds": round(time.monotonic() - started, 1), "cost": float(cost)}


def _summary(engines: list, reference: bool) -> list:
    """Every engine's totals; the others' texts are its yardstick for the disagreement."""
    for row in engines:
        texts = row.pop("_texts")
        ok = [k for k, text in enumerate(texts) if text is not None]
        row["words"] = sum(len(asr_eval.normalize(texts[k])) for k in ok)
        others = []
        for other in engines:
            if other is row:
                continue
            pairs = [(asr_eval.normalize(other["_ref"][k]), asr_eval.normalize(texts[k]))
                     for k in ok if other["_ref"][k] is not None]
            # by the longer of the two texts: an engine that heard more is not punished against one that dropped words
            size = sum(max(len(a), len(b)) for a, b in pairs)
            if size:
                others.append(sum(asr_eval.distance(a, b) for a, b in pairs) / size)
        row["disagreement"] = round(sum(others) / len(others), 4) if others else None
    for row in engines:
        row.pop("_ref", None)
    key = (lambda r: (r["wer"] is None, r["wer"] or 0)) if reference else \
        (lambda r: (r["disagreement"] is None, r["disagreement"] or 0))
    return sorted(engines, key=key)


def run(meeting_id: int, engines=ENGINES, progress=None) -> dict:
    """Compare the engines on one meeting's recording; the result is kept in the settings («stt_compare»)."""
    from . import diarization_runs, participants
    progress = progress or (lambda text: None)
    engines = list(engines or ENGINES)[:MAX_ENGINES]
    with SessionLocal() as s:
        meeting = s.get(Meeting, meeting_id)
        if meeting is None:
            raise ValueError("Совещание не найдено.")
        title = ((meeting.report or {}).get("title") or meeting.title or meeting.filename or f"Совещание {meeting_id}")[:120]
        reference = meeting.diarization_reference if isinstance(meeting.diarization_reference, dict) else None
        truth = list(reference.get("transcript") or []) if reference else []
        settings = settings_store.for_meeting(meeting.options)
        if participants.is_set(meeting):
            settings["_participants"] = participants.employee_ids(meeting)
        path, temporary = diarization_runs.recording(meeting)
    workdir = pathlib.Path(tempfile.mkdtemp(prefix="sysai_compare_"))
    try:
        total = audio.duration(path)
        digest = hashlib.sha256()
        with open(path, "rb") as source:
            for block in iter(lambda: source.read(1 << 20), b""):
                digest.update(block)
        progress("Детектор речи")
        speech = vad.speech(path, checkpoints.Store(meeting_id), digest.hexdigest(), progress) or []
        spans = windows(total, speech, truth or None)
        if not spans:
            raise ValueError("Запись слишком короткая для сравнения.")
        keyterms = speech_fix.keyterms(settings) if settings.get("stt_keyterms", True) else []
        language = settings.get("language", "ru")
        refs = [reference_text(truth, a, b) for a, b in spans] if truth else []
        rows = []
        clips = []
        for k, (start, end) in enumerate(spans):
            target = workdir / f"window_{k}.mp3"
            audio.extract(path, target, start, end - start)
            clips.append(target.read_bytes())
        for number, engine in enumerate(engines):
            row = {"model": engine, "errors": [], "seconds": 0.0, "cost": 0.0, "missed": None, "terms": 0,
                   "wer": None, "_texts": [], "_ref": [], "texts": []}
            timed, missed, errors_count, said = True, 0.0, 0, 0
            for k, ((start, end), clip) in enumerate(zip(spans, clips)):
                progress(f"{engine}: отрывок {k + 1} из {len(spans)} (движок {number + 1} из {len(engines)})")
                try:
                    heard = _hear(engine, clip, language, keyterms)
                except Exception as exc:  # noqa: BLE001  one engine must not stop the others
                    row["errors"].append(str(exc)[:200])
                    row["_texts"].append(None)
                    row["_ref"].append(None)
                    row["texts"].append("")
                    timed = False
                    continue
                row["seconds"] += heard["seconds"]
                row["cost"] += heard["cost"]
                row["_texts"].append(heard["text"])
                row["_ref"].append(heard["text"])
                row["texts"].append(heard["text"][:TEXT_LIMIT])
                row["terms"] += _terms(heard["text"], keyterms)
                if heard["words"]:
                    missed += _missed(speech, [[w, a + start, b + start] for w, a, b in heard["words"]], start, end)
                else:
                    timed = False
                if refs:
                    ref_words = asr_eval.normalize(refs[k])
                    errors_count += asr_eval.distance(ref_words, asr_eval.normalize(heard["text"]))
                    said += len(ref_words)
            ok = [text for text in row["_texts"] if text is not None]
            if ok and timed:
                row["missed"] = round(missed, 1)
            if refs and said and len(ok) == len(spans):
                row["wer"] = round(errors_count / said, 4)
            row["seconds"] = round(row["seconds"], 1)
            row["cost"] = round(row["cost"], 4)
            rows.append(row)
        ranked = _summary(rows, bool(refs))
        best = next((row["model"] for row in ranked if not row["errors"]), "")
        result = {"meeting_id": meeting_id, "title": title, "at": _now(), "windows": spans, "reference": bool(refs),
                  "references": [text[:TEXT_LIMIT] for text in refs], "keyterms": len(keyterms),
                  "speech_seconds": round(sum(vad.speech_in(speech, a, b) for a, b in spans), 1),
                  "engines": ranked, "best": best}
        settings_store.set_many({"stt_compare": result})
        return result
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        if temporary:
            pathlib.Path(path).unlink(missing_ok=True)


def start(meeting_id: int, engines=ENGINES) -> bool:
    """Start a comparison in the background; False when one is running already."""
    with _lock:
        if _state["running"]:
            return False
        _state.update(running=True, started=_now(), finished=None, meeting_id=meeting_id, step="", error="")
    if config.TESTING:
        _run(meeting_id, engines)
    else:
        threading.Thread(target=_run, args=(meeting_id, engines), name="stt-compare", daemon=True).start()
    return True


def _run(meeting_id: int, engines) -> None:
    try:
        run(meeting_id, engines, progress=lambda text: _update(step=text))
    except Exception as exc:  # noqa: BLE001  shown on the page
        log.warning("engine comparison on meeting %s failed: %s", meeting_id, exc)
        _update(error=str(exc)[:300])
    finally:
        _update(running=False, finished=_now(), step="")


def candidates(limit: int = 40) -> list:
    """Meetings to compare on: those with a reference first, then the newest; only those whose recording is kept."""
    from . import audio_archive
    out = []
    with SessionLocal() as s:
        meetings = (s.query(Meeting).filter(Meeting.duration_sec.isnot(None), Meeting.duration_sec >= 60)
                    .order_by(Meeting.id.desc()).limit(300).all())
        for meeting in meetings:
            local = bool(meeting.audio_path) and pathlib.Path(str(meeting.audio_path)).is_file()
            try:
                stored = audio_archive.view(meeting).get("state") == "stored"
            except Exception:  # noqa: BLE001
                stored = False
            if not (local or stored or meeting.source == "ticnote"):
                continue
            out.append({"id": meeting.id, "title": ((meeting.report or {}).get("title") or meeting.title
                                                     or meeting.filename or f"Совещание {meeting.id}")[:100],
                        "date": meeting.meeting_date, "duration": meeting.duration_sec,
                        "reference": isinstance(meeting.diarization_reference, dict)})
    out.sort(key=lambda row: (not row["reference"], -row["id"]))
    return out[:limit]
