"""«Сравнить способы»: every way of labelling the voices (diarize.METHODS) on the recordings of the references.

A reference (diarization_eval.py) keeps the automatic transcript its corrections started from. A run takes that
transcript without its speakers, labels it by one way, names the voices the cheap way (introductions and addresses, no
model) and measures the result against the corrected transcript. The voice service is asked once per reference; its
voice prints are then clustered with every threshold of diarize.THRESHOLDS, so the best threshold over all references
can be chosen. The labels of the parts («chunks») need no recording.

Meeting.diarization_reference["runs"] = {
    "chunks": {"metrics": {...}, "at": "..."},
    "voice": {"thresholds": {"0.50": {...}, ...}, "at": "...", "seconds": 12.3} | {"error": "...", "at": "..."},
    "gemini": {"metrics": {...}, "at": "...", "seconds": 40.1} | {"error": "...", "at": "..."},
}
"""
from __future__ import annotations

import copy
import datetime as dt
import logging
import os
import pathlib
import tempfile
import threading
import time

import httpx

from . import (audio_archive, config, diarization_eval, diarize, llm, participants, settings_store, speaker_id, speakers,
               storage, voice_memory)
from .db import Employee, Meeting, SessionLocal

log = logging.getLogger("sysai.diarization_runs")

ORDER = ("chunks", "sound", "voice", "gemini")
_lock = threading.Lock()
_state = {"running": False, "started": None, "finished": None, "done": 0, "total": 0, "current": "", "step": "",
          "errors": []}


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def status() -> dict:
    with _lock:
        return copy.deepcopy(_state)


def _update(**fields) -> None:
    with _lock:
        _state.update(fields)


def _compact(metrics: dict) -> dict:
    """The metrics without the per-person table and the label matching: enough for the comparison and small."""
    return {key: value for key, value in metrics.items() if key not in ("speakers", "mapping")}


def _usable(reference) -> bool:
    return isinstance(reference, dict) and isinstance(reference.get("transcript"), list)


# ------------------------------------------------------------------------------------------------- one reference
def _named(segments: list, allowed) -> list:
    """Names by introductions and addresses only: the same cheap step for every way, so the names compare too."""
    try:
        with SessionLocal() as s:
            query = s.query(Employee).filter(Employee.active.is_(True))
            if allowed is not None:
                query = query.filter(Employee.id.in_(list(allowed) or [-1]))
            named, _ = speakers.name_speakers(segments, query.all(), use_model=False)
        return named
    except Exception:  # noqa: BLE001  without names the voices are still measured
        log.warning("naming a comparison run failed", exc_info=True)
        return segments


def recording(meeting) -> tuple[str, bool]:
    """(path to the recording of a meeting, True when it is a temporary copy to delete afterwards): the file on the
    server, a copy from the storage of recordings or the recording downloaded from TicNote again."""
    local = str(meeting.audio_path or "")
    if local and pathlib.Path(local).is_file():
        return local, False
    try:
        url = audio_archive.link(meeting)
    except storage.StorageError as exc:
        raise diarize.DiarizationError(f"Хранилище записей недоступно: {exc}") from exc
    if url:
        handle, name = tempfile.mkstemp(prefix="sysai_reference_", suffix=".mp3")
        os.close(handle)
        try:
            with httpx.stream("GET", url, timeout=120, follow_redirects=True) as response:
                response.raise_for_status()
                with open(name, "wb") as out:
                    for chunk in response.iter_bytes(1 << 20):
                        out.write(chunk)
        except httpx.HTTPError as exc:
            pathlib.Path(name).unlink(missing_ok=True)
            raise diarize.DiarizationError("Не удалось скачать запись из хранилища.") from exc
        return name, True
    if meeting.source == "ticnote":
        from . import ticnote_ingest
        try:
            path = ticnote_ingest.restore_audio(meeting.id)
        except Exception as exc:  # noqa: BLE001  TicNote down or the key gone
            raise diarize.DiarizationError(f"Не удалось скачать запись с TicNote: {exc}") from exc
        if path and pathlib.Path(path).is_file():
            return path, False
    raise diarize.DiarizationError("Записи этого совещания нет ни на сервере, ни в хранилище — сравнивать не на чем.")


_recording = recording


def _measure(truth, base, labels, allowed, details=None, known=None) -> dict:
    labelled = voice_memory.rename(diarize.apply(base, labels, details), known or {})
    return _compact(diarization_eval.evaluate(truth, _named(labelled, allowed)))


def _remembered(base, groups, threshold, people, total, allowed, memory, meeting_id) -> tuple:
    """(labels of one threshold, voices recognised by the prints from the other meetings — as at a new meeting)."""
    found = {}
    labels = diarize.voice_labels(base, groups, threshold, people, total, found=found)
    known = voice_memory.recognise(found, meeting_id=meeting_id, allowed=allowed, memory=memory) if memory else {}
    return labels, known


def _run_way(name, path, base, truth, total, settings, people, allowed, progress, meeting_id=None) -> dict:
    started = time.monotonic()
    try:
        if name == "voice":
            groups = diarize.voice_groups(path, base, total, progress)
            memory = voice_memory.prints(allowed, exclude_meeting=meeting_id) if voice_memory.enabled() else {}
            result = {"thresholds": {}}
            for threshold in diarize.THRESHOLDS:
                labels, known = _remembered(base, groups, threshold, people, total, allowed, memory, meeting_id)
                result["thresholds"][f"{threshold:.2f}"] = _measure(truth, base, labels, allowed, known=known)
        elif name == "sound":
            details, found = {}, {}
            labels = diarize.sound_labels(path, base, total, people, progress, details, found)
            memory = voice_memory.prints(allowed, exclude_meeting=meeting_id) if voice_memory.enabled() else {}
            known = voice_memory.recognise(found, meeting_id=meeting_id, allowed=allowed, memory=memory) if memory else {}
            result = {"metrics": _measure(truth, base, labels, allowed, details, known)}
        else:
            details = {}  # the replicas the model cut where a second voice begins are measured as cut
            labels = diarize.gemini_labels(path, base, total, settings, people, progress, details)
            result = {"metrics": _measure(truth, base, labels, allowed, details)}
    except (diarize.DiarizationError, speaker_id.SpeakerIdentityError, llm.LLMError, OSError, ValueError) as exc:
        return {"error": str(exc)[:300], "at": _now()}
    except Exception as exc:  # noqa: BLE001  ffmpeg and friends: the other ways still run
        log.exception("comparison run %s failed", name)
        return {"error": f"Сбой: {type(exc).__name__}", "at": _now()}
    return dict(result, at=_now(), seconds=round(time.monotonic() - started, 1))


def run_reference(meeting_id: int, methods=ORDER, progress=None) -> dict:
    """Run the ways on one reference and keep the results in it; returns the new runs."""
    with SessionLocal() as s:
        meeting = s.get(Meeting, meeting_id)
        reference = meeting.diarization_reference if meeting is not None else None
        if not _usable(reference):
            raise diarize.DiarizationError("У совещания нет эталона.")
        truth = copy.deepcopy(reference["transcript"])
        base = diarize.anonymous(reference.get("auto") or [])
        stamp = reference.get("at")
        total = max([float(meeting.duration_sec or 0)] + [row[2] for row in diarize._rows(base, 0.0)])
        allowed = participants.employee_ids(meeting) if participants.is_set(meeting) else None
        people = participants.count(meeting)
        settings = settings_store.for_meeting(meeting.options)
    runs = {}
    if "chunks" in methods:
        runs["chunks"] = {"metrics": _compact(diarization_eval.evaluate(truth, _named(base, allowed))), "at": _now()}
    ready = []
    for name in (name for name in ORDER if name in methods and name != "chunks"):
        reason = diarize.unavailable(name)
        if reason:
            runs[name] = {"error": reason, "at": _now()}
        else:
            ready.append(name)
    if ready:
        try:
            path, temporary = _recording(meeting)
        except diarize.DiarizationError as exc:
            runs.update({name: {"error": str(exc), "at": _now()} for name in ready})
        else:
            try:
                for name in ready:
                    runs[name] = _run_way(name, path, base, truth, total, settings, people, allowed, progress,
                                          meeting_id)
            finally:
                if temporary:
                    pathlib.Path(path).unlink(missing_ok=True)
    with SessionLocal() as s:
        meeting = s.get(Meeting, meeting_id)
        reference = meeting.diarization_reference if meeting is not None else None
        if not _usable(reference) or reference.get("at") != stamp:
            raise diarize.DiarizationError("Эталон изменился во время сравнения — запустите сравнение ещё раз.")
        meeting.diarization_reference = dict(reference, runs=dict(reference.get("runs") or {}, **runs))
        s.commit()
    return runs


# -------------------------------------------------------------------------------------------- all references
def reference_ids() -> list:
    with SessionLocal() as s:
        rows = s.query(Meeting).filter(Meeting.diarization_reference.isnot(None)).order_by(Meeting.id).all()
        return [m.id for m in rows if _usable(m.diarization_reference)]


def _title(meeting_id: int) -> str:
    with SessionLocal() as s:
        meeting = s.get(Meeting, meeting_id)
        if meeting is None:
            return f"Совещание {meeting_id}"
        return ((meeting.report or {}).get("title") or meeting.title or meeting.filename or f"Совещание {meeting_id}")[:120]


def start(methods=ORDER) -> bool:
    """Start the comparison in the background; False when it is running already or there are no references."""
    ids = reference_ids()
    if not ids:
        return False
    with _lock:
        if _state["running"]:
            return False
        _state.update(running=True, started=_now(), finished=None, done=0, total=len(ids), current="", step="", errors=[])
    if config.TESTING:
        _run_all(ids, methods)
    else:
        threading.Thread(target=_run_all, args=(ids, methods), name="diarization-compare", daemon=True).start()
    return True


def _run_all(ids: list, methods) -> None:
    try:
        for number, meeting_id in enumerate(ids):
            title = _title(meeting_id)
            _update(done=number, current=title, step="")
            try:
                run_reference(meeting_id, methods, progress=lambda text: _update(step=text))
            except Exception as exc:  # noqa: BLE001  one reference must not stop the others
                log.warning("comparison on reference %s failed: %s", meeting_id, exc)
                with _lock:
                    _state["errors"].append(f"{title}: {str(exc)[:200]}")
    finally:
        _update(running=False, done=len(ids), finished=_now(), current="", step="")


# --------------------------------------------------------------------------------------------------- showing
def _rank(total: dict | None) -> tuple:
    """Better first: more speech with the right speaker, then fewer extra voices and merged people."""
    if not total or total.get("accuracy") is None:
        return (-1.0, 0)
    merges = total.get("merges") or 0
    return (total["accuracy"], -(int(total.get("extra") or 0) + (len(merges) if isinstance(merges, list) else int(merges))))


def threshold_key(value) -> str:
    return f"{float(value):.2f}"


def overview(references: list, method: str, threshold: float) -> dict:
    """The ways side by side over all references (each a reference dict with «runs»).

    {"ways": [{"name", "title", "hint", "total", "count", "errors", "threshold", "current"}], "sweep": [...],
     "best": name, "best_threshold": "0.55", "measured": n}"""
    runs = [ref.get("runs") or {} for ref in references if _usable(ref)]
    current_key = threshold_key(threshold)
    sweep = []
    for value in diarize.THRESHOLDS:
        key = threshold_key(value)
        results = [run["voice"]["thresholds"][key] for run in runs
                   if isinstance((run.get("voice") or {}).get("thresholds"), dict) and key in run["voice"]["thresholds"]]
        sweep.append({"key": key, "value": value, "total": diarization_eval.combine(results), "count": len(results),
                      "current": key == current_key})
    measured = [row for row in sweep if row["total"]]
    # equal results: the threshold nearest to the one in use, not an extreme of the range
    best_threshold = (max(measured, key=lambda row: (*_rank(row["total"]), -abs(row["value"] - threshold)))["key"]
                      if measured else "")
    for row in sweep:
        row["best"] = row["key"] == best_threshold
    ways = []
    for name in ORDER:
        errors = [run[name]["error"] for run in runs if (run.get(name) or {}).get("error")]
        way = {"name": name, "title": diarize.METHODS[name], "hint": diarize.HINTS[name], "errors": errors,
               "current": name == method, "threshold": ""}
        if name == "voice":
            chosen = next((row for row in sweep if row["key"] == current_key and row["total"]), None)
            chosen = chosen or next((row for row in sweep if row["best"]), None)
            way.update(total=chosen["total"] if chosen else None, count=chosen["count"] if chosen else 0,
                       threshold=chosen["key"] if chosen else "")
        else:
            results = [run[name]["metrics"] for run in runs if isinstance((run.get(name) or {}).get("metrics"), dict)]
            way.update(total=diarization_eval.combine(results), count=len(results))
        ways.append(way)
    ranked = [way for way in ways if way["total"]]
    same = len({way["count"] for way in ranked}) == 1 and len(ranked) > 1
    best = max(ranked, key=lambda way: _rank(way["total"]))["name"] if same else ""
    for way in ways:
        way["best"] = way["name"] == best
    return {"ways": ways, "sweep": sweep, "best": best, "best_threshold": best_threshold,
            "measured": sum(1 for run in runs if run)}


def brief(reference, threshold: float = diarize.DEFAULT_THRESHOLD) -> list:
    """The ways on one reference, for its meeting page: [{"title", "accuracy", "threshold", "error"}]."""
    runs = (reference or {}).get("runs") if isinstance(reference, dict) else None
    out = []
    for name in ORDER:
        run = (runs or {}).get(name)
        if not isinstance(run, dict):
            continue
        item = {"name": name, "title": diarize.METHODS[name], "accuracy": None, "threshold": "", "error": run.get("error", "")}
        if isinstance(run.get("metrics"), dict):
            item["accuracy"] = run["metrics"].get("accuracy")
        elif isinstance(run.get("thresholds"), dict) and run["thresholds"]:
            key, metrics = max(run["thresholds"].items(),
                               key=lambda pair: (*_rank(pair[1]), -abs(float(pair[0]) - threshold)))
            item.update(accuracy=metrics.get("accuracy"), threshold=key)
        out.append(item)
    return out
