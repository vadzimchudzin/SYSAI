"""«Переразметить голоса»: an old meeting gets its speakers again from its kept recording, by a whole-record way.

Only who speaks changes: the text, the times, the report and the tasks stay. A replica a person corrected keeps that
person — only its automatic label (speaker_auto) becomes the new one, so «Вернуть автоматическую разметку» shows the
new labelling. The result is shown first as «было → стало» and saved on request; until the transcript changes again,
the labelling before it can be brought back.

Meeting.options["relabel"] = {"state": "running" | "ready" | "error" | "saved", "method", "at", "by", "progress",
    "error", ready: "base" (fingerprint of the transcript it was made for), "patches", "auto" (patches of
    transcript_auto), "hints" (the analysis of the names, speaker_hints.py), "summary", "pairs", "changes";
    saved: "summary", "previous": {"after", "patches", "auto", "hints"}}
A patch is {"i": replica index, "set": {field: value}, "drop": [field]} over FIELDS only.
"""
from __future__ import annotations

import bisect
import collections
import copy
import datetime as dt
import itertools
import logging
import pathlib
import re
import threading

from . import (audio_archive, config, diarization_runs, diarize, llm, participants, settings_store, speaker_hints,
               speaker_id, speakers, transcript_edit, voice_diagram, voice_memory)
from .db import Employee, Meeting, SessionLocal

log = logging.getLogger("sysai.voice_relabel")

WAYS = ("sound", "voice", "gemini")
FIELDS = ("speaker", "speaker_source", "speaker_employee_id", "speaker_similarity", "speaker_confidence",
          "speaker_memory", "speaker_guest", "speaker_assigned_by", "speaker_auto", "overlap")
STALE = dt.timedelta(minutes=45)     # a run that long was lost with a restart of the server
CHANGES_SHOWN = 150
PAIRS_SHOWN = 12
QUOTE = 90
MASK = "\u2063"                      # an invisible mark: corrected replicas are not named again by the conversation
_running: set = set()
_lock = threading.Lock()


class RelabelError(ValueError):
    """The meeting cannot be relabelled, or the result cannot be saved; the message is shown."""


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def state(meeting) -> dict:
    data = (meeting.options or {}).get("relabel") if isinstance(meeting.options, dict) else None
    data = copy.deepcopy(data) if isinstance(data, dict) else {}
    if data.get("state") == "running":
        try:
            started = dt.datetime.fromisoformat(str(data.get("at")))
        except ValueError:
            started = None
        if started is None or _now() - started > STALE:
            data.update(state="error", error="Переразметка прервалась (сервер перезапускался) — запустите её ещё раз.")
    return data


def _store(meeting, data: dict | None) -> None:
    options = dict(meeting.options or {})
    if data is None:
        options.pop("relabel", None)
    else:
        options["relabel"] = data
    meeting.options = options


def way(settings) -> str:
    """The whole-record way to use: the chosen one, else the first that can run here ("" when none can)."""
    chosen = diarize.method_of(settings)
    for name in ((chosen,) if chosen in WAYS else ()) + WAYS:
        if not diarize.unavailable(name):
            return name
    return ""


def has_recording(meeting) -> bool:
    local = str(meeting.audio_path or "")
    if local and pathlib.Path(local).is_file():
        return True
    return bool(audio_archive.view(meeting).get("playable")) or meeting.source == "ticnote"


def unavailable(meeting, settings=None) -> str:
    """Why the meeting cannot be relabelled now ("" when it can)."""
    if not meeting.transcript:
        return "Расшифровки ещё нет."
    if meeting.status in ("queued", "transcribing", "analyzing"):
        return "Совещание ещё обрабатывается."
    if not has_recording(meeting):
        return "Записи этого совещания нет ни на сервере, ни в хранилище — переразметить не по чему."
    if not way(settings if settings is not None else settings_store.for_meeting(meeting.options)):
        return "Нужны модели разметки голосов на сервере, свой сервис голосов или ключ OpenRouter (Gemini слушает запись)."
    return ""


# ------------------------------------------------------------------------------------------------ the proposal
def _number_of(label) -> int | None:
    match = re.fullmatch(r"Спикер (\d{1,4})", str(label or "").strip())
    return int(match.group(1)) if match else None


def _numbered(labels: list, taken: set) -> tuple[list, dict]:
    """«Спикер N» of the new labelling without the numbers corrected replicas use: (labels, old -> new)."""
    free = (n for n in itertools.count(1) if n not in taken)
    mapping, out = {}, []
    for label in labels:
        if label is None:
            out.append(None)
            continue
        if label not in mapping:
            mapping[label] = f"Спикер {next(free)}"
        out.append(mapping[label])
    return out, mapping


def _quote(text) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= QUOTE else text[:QUOTE - 1].rstrip() + "…"


def _number(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def patches(before: list, after: list) -> list:
    out = []
    for index, (old, new) in enumerate(zip(before or [], after or [])):
        if not isinstance(old, dict) or not isinstance(new, dict):
            continue
        put = {key: copy.deepcopy(new[key]) for key in FIELDS if key in new and (key not in old or old[key] != new[key])}
        drop = [key for key in FIELDS if key in old and key not in new]
        if put or drop:
            out.append({"i": index, "set": put, "drop": drop})
    return out


def patched(transcript: list, changes: list) -> list:
    result = copy.deepcopy(list(transcript or []))
    for patch in changes or []:
        index = patch.get("i") if isinstance(patch, dict) else None
        if not isinstance(index, int) or not 0 <= index < len(result) or not isinstance(result[index], dict):
            raise RelabelError("Новая разметка не подходит к расшифровке — запустите переразметку ещё раз.")
        for key in patch.get("drop") or []:
            if key in FIELDS:
                result[index].pop(key, None)
        result[index].update({key: value for key, value in (patch.get("set") or {}).items() if key in FIELDS})
    return result


def _automatic(segment: dict) -> dict:
    """The automatic labelling of a replica: what it says, or what a corrected one keeps in speaker_auto."""
    if segment.get("speaker_source") == "manual":
        return dict(segment.get("speaker_auto") or {})
    return {key: segment[key] for key in FIELDS if key in segment and key not in ("speaker_auto", "overlap")}


def _auto_after(auto: list, after: list) -> list:
    """transcript_auto (the replicas before the first correction) with the new automatic labels, matched by time."""
    order = sorted((_number(seg.get("start")), index) for index, seg in enumerate(after) if isinstance(seg, dict))
    starts = [start for start, _ in order]
    out = []
    for seg in auto:
        if not isinstance(seg, dict):
            out.append(seg)
            continue
        a, b = _number(seg.get("start")), _number(seg.get("end"), _number(seg.get("start")))
        k = bisect.bisect_right(starts, a) - 1
        best, overlap = None, -1.0
        for _, index in order[max(0, k - 2):k + 4]:
            other = after[index]
            c, d = _number(other.get("start")), _number(other.get("end"), _number(other.get("start")))
            shared = min(b, d) - max(a, c)
            if shared > overlap or (best is None and abs(c - a) < 0.05):
                best, overlap = index, shared
        if best is None:
            out.append(seg)
            continue
        fresh = {key: value for key, value in seg.items() if key not in FIELDS or key == "overlap"}
        fresh.update(_automatic(after[best]))
        out.append(fresh)
    return out


def propose(transcript: list, path, method: str, settings: dict, total: float, employees, people: int = 0,
            auto: list | None = None, meeting_id: int | None = None, allowed=None, progress=None) -> dict:
    """The new labelling of a transcript, as patches and «было → стало» (nothing is saved)."""
    work = [dict(seg) if isinstance(seg, dict) else seg for seg in transcript]
    details, known = {}, {}
    if method == "voice":
        groups = diarize.voice_groups(path, work, total, progress)
        found = {}
        labels = diarize.voice_labels(work, groups, diarize.threshold_of(settings), people, total, found=found)
        known = voice_memory.recognise(found, meeting_id=meeting_id, allowed=allowed)
    elif method == "sound":
        found = {}  # replicas are not cut here: a relabelling changes only who speaks
        labels = diarize.sound_labels(path, work, total, people, progress, details, found, cut=False)
        known = voice_memory.recognise(found, meeting_id=meeting_id, allowed=allowed)
    elif method == "gemini":
        labels = diarize.gemini_labels(path, work, total, settings, people, progress, details)
    else:
        raise RelabelError("Неизвестный способ разметки.")
    manual = {index for index, seg in enumerate(work) if isinstance(seg, dict) and seg.get("speaker_source") == "manual"}
    taken = {_number_of(work[index].get("speaker")) for index in manual} - {None}
    labels, renamed = _numbered(list(labels) + [None] * (len(work) - len(labels)), taken)
    known = {renamed[label]: match for label, match in known.items() if label in renamed}
    overlap = details.get("overlap") or set()
    fresh = []
    for index, seg in enumerate(work):
        label = labels[index]
        if not isinstance(seg, dict):
            fresh.append(seg)
            continue
        if seg.get("speaker_source") == "manual":  # the person stays; the automatic label is the new one
            item = dict(seg)
            if speakers.anonymous(item.get("speaker")):  # a voice the person kept unnamed is not named here
                item["speaker"] = MASK + str(item.get("speaker"))
            if label:
                auto_label = known.get(label)
                item["speaker_auto"] = ({"speaker": auto_label["name"], "speaker_employee_id": auto_label["employee_id"],
                                         "speaker_source": "voiceprint", "speaker_similarity": auto_label["similarity"],
                                         "speaker_memory": True} if auto_label else {"speaker": label})
        elif seg.get("speaker_source") == "voiceprint" or not label:
            item = dict(seg)
        else:
            item = {key: value for key, value in seg.items() if key not in diarize.NAME_KEYS and key != "speaker_memory"}
            item["speaker"] = label
        if index in overlap:
            item["overlap"] = True
        fresh.append(item)
    fresh = voice_memory.rename(fresh, known)
    analysis = {}
    named, _ = speakers.name_speakers(fresh, employees, found=analysis)
    after = []
    for index, seg in enumerate(named):  # a corrected replica comes back as the person left it
        if index in manual:
            after.append(dict(fresh[index], speaker=work[index].get("speaker")))
        else:
            after.append(seg)
    changes, pairs = [], collections.Counter()
    for index, (old, new) in enumerate(zip(transcript, after)):
        if not isinstance(old, dict) or not isinstance(new, dict):
            continue
        a, b = voice_diagram.label_of(old), voice_diagram.label_of(new)
        if a != b:
            pairs[(a, b)] += 1
            if len(changes) < CHANGES_SHOWN:
                changes.append({"seq": index, "start": old.get("start"), "text": _quote(old.get("text")),
                                "before": a, "after": b})
    voices = lambda rows: len({voice_diagram.label_of(seg) for seg in rows if isinstance(seg, dict)})  # noqa: E731
    changed = sum(pairs.values())
    summary = {"method": method, "title": diarize.METHODS.get(method, method), "replicas": len(transcript),
               "changed": changed, "manual": len(manual), "before": voices(transcript), "after": voices(after),
               "remembered": len(known), "named": len(analysis.get("mapping") or {})}
    return {"patches": patches(transcript, after),
            "auto": patches(auto, _auto_after(auto, after)) if auto is not None else None,
            "hints": analysis, "summary": summary,
            "pairs": [{"before": a, "after": b, "count": n} for (a, b), n in pairs.most_common(PAIRS_SHOWN)],
            "changes": changes}


# --------------------------------------------------------------------------------------------- the background
def start(meeting_id: int, by: str = "") -> str:
    """Start relabelling in the background; returns the way. RelabelError says why it cannot start."""
    with SessionLocal() as s:
        meeting = s.get(Meeting, meeting_id)
        if meeting is None:
            raise RelabelError("Совещание не найдено.")
        if state(meeting).get("state") == "running":
            raise RelabelError("Переразметка уже идёт.")
        settings = settings_store.for_meeting(meeting.options)
        reason = unavailable(meeting, settings)
        if reason:
            raise RelabelError(reason)
        name = way(settings)
        _store(meeting, {"state": "running", "method": name, "title": diarize.METHODS.get(name, name),
                         "at": _now().isoformat(timespec="seconds"), "by": str(by or "")[:120],
                         "progress": "Готовлю запись"})
        s.commit()
    with _lock:
        _running.add(meeting_id)
    if config.TESTING:
        _run(meeting_id, name)
    else:
        threading.Thread(target=_run, args=(meeting_id, name), name=f"relabel-{meeting_id}", daemon=True).start()
    return name


def _progress(meeting_id: int, text: str) -> None:
    try:
        with SessionLocal() as s:
            meeting = s.get(Meeting, meeting_id)
            data = state(meeting) if meeting is not None else {}
            if data.get("state") == "running":
                _store(meeting, dict(data, progress=str(text)[:200]))
                s.commit()
    except Exception:  # noqa: BLE001  progress is only a hint
        log.debug("relabel progress of meeting %s not saved", meeting_id, exc_info=True)


def _finish(meeting_id: int, started: str, data: dict) -> None:
    with SessionLocal() as s:
        meeting = s.get(Meeting, meeting_id)
        if meeting is None:
            return
        current = (meeting.options or {}).get("relabel") or {}
        if current.get("state") != "running" or current.get("at") != started:
            return  # cancelled or started again meanwhile
        _store(meeting, dict({key: current[key] for key in ("method", "title", "at", "by") if key in current}, **data))
        s.commit()


def _run(meeting_id: int, method: str) -> None:
    started = ""
    try:
        with SessionLocal() as s:
            meeting = s.get(Meeting, meeting_id)
            started = ((meeting.options or {}).get("relabel") or {}).get("at", "")
            transcript = copy.deepcopy(list(meeting.transcript or []))
            auto = copy.deepcopy(meeting.transcript_auto) if meeting.transcript_auto is not None else None
            base = transcript_edit.fingerprint(meeting.transcript)
            allowed = set(participants.employee_ids(meeting)) if participants.is_set(meeting) else None
            people = participants.count(meeting)
            settings = settings_store.for_meeting(meeting.options)
            total = max([_number(meeting.duration_sec)] + [row[2] for row in diarize._rows(transcript, 0.0)])
            query = s.query(Employee).filter(Employee.active.is_(True))
            if allowed is not None:
                query = query.filter(Employee.id.in_(list(allowed) or [-1]))
            employees = query.all()
            for employee in employees:
                _ = employee.name, employee.aliases, employee.position
            s.expunge_all()  # the meeting and the people are read once; the run writes through other sessions
        _progress(meeting_id, "Скачиваю запись")
        path, temporary = diarization_runs.recording(meeting)
        try:
            proposal = propose(transcript, path, method, settings, total, employees, people, auto=auto,
                               meeting_id=meeting_id, allowed=allowed, progress=lambda text: _progress(meeting_id, text))
        finally:
            if temporary:
                pathlib.Path(path).unlink(missing_ok=True)
        _finish(meeting_id, started, dict(proposal, state="ready", base=base))
    except (RelabelError, diarize.DiarizationError, speaker_id.SpeakerIdentityError, llm.LLMError, OSError,
            ValueError) as exc:
        log.warning("relabelling meeting %s failed: %s", meeting_id, exc)
        _finish(meeting_id, started, {"state": "error", "error": str(exc)[:300]})
    except Exception as exc:  # noqa: BLE001  ffmpeg and friends: the meeting stays as it was
        log.exception("relabelling meeting %s failed", meeting_id)
        _finish(meeting_id, started, {"state": "error", "error": f"Сбой: {type(exc).__name__}"})
    finally:
        with _lock:
            _running.discard(meeting_id)


# ----------------------------------------------------------------------------------------------- the decision
def save(meeting, by: str = "") -> int:
    """Save the new labelling; returns the replicas whose speaker changed."""
    data = state(meeting)
    if data.get("state") != "ready":
        raise RelabelError("Сохранять нечего: новая разметка не готова.")
    if data.get("base") != transcript_edit.fingerprint(meeting.transcript):
        raise RelabelError("Расшифровку изменили после переразметки — запустите переразметку ещё раз.")
    before = list(meeting.transcript or [])
    after = patched(before, data.get("patches"))
    previous = {"after": transcript_edit.fingerprint(after), "patches": patches(after, before),
                "hints": (meeting.options or {}).get("speaker_hints")}
    if meeting.transcript_auto is not None and data.get("auto") is not None:
        auto_after = patched(meeting.transcript_auto, data["auto"])
        previous["auto"] = patches(auto_after, meeting.transcript_auto)
        meeting.transcript_auto = auto_after
    meeting.transcript = after
    speaker_hints.save(meeting, data.get("hints") or {})
    _store(meeting, {"state": "saved", "method": data.get("method"), "title": data.get("title"),
                     "at": _now().isoformat(timespec="seconds"), "by": str(by or "")[:120],
                     "summary": data.get("summary") or {}, "previous": previous})
    return int((data.get("summary") or {}).get("changed") or 0)


def revert(meeting) -> int:
    """Bring back the labelling before the saved relabelling; returns the replicas changed back."""
    data = state(meeting)
    previous = data.get("previous") if data.get("state") == "saved" else None
    if not previous:
        raise RelabelError("Возвращать нечего.")
    if previous.get("after") != transcript_edit.fingerprint(meeting.transcript):
        raise RelabelError("После переразметки расшифровку уже правили — прежнюю разметку не вернуть.")
    count = sum(1 for patch in previous.get("patches") or [] if "speaker" in (patch.get("set") or {}))
    meeting.transcript = patched(meeting.transcript, previous.get("patches"))
    if previous.get("auto") is not None and meeting.transcript_auto is not None:
        meeting.transcript_auto = patched(meeting.transcript_auto, previous["auto"])
    options = dict(meeting.options or {})
    options.pop("relabel", None)
    if previous.get("hints") is not None:
        options["speaker_hints"] = previous["hints"]
    else:
        options.pop("speaker_hints", None)
    meeting.options = options
    return count


def close(meeting) -> None:
    """«Отменить» a ready labelling or hide a finished one (a running one is not stopped)."""
    if state(meeting).get("state") == "running":
        raise RelabelError("Переразметка ещё идёт.")
    _store(meeting, None)


def view(meeting, settings=None) -> dict:
    """What the meeting page shows: the button, a run in progress, «было → стало» or what was saved."""
    data = state(meeting)
    reason = unavailable(meeting, settings)
    out = {"state": data.get("state") or "", "reason": reason, "can": not reason and data.get("state") != "running",
           "title": data.get("title") or diarize.METHODS.get(way(settings or {}) or "", ""),
           "progress": data.get("progress") or "", "error": data.get("error") or "",
           "summary": data.get("summary") or {}, "pairs": data.get("pairs") or [], "changes": data.get("changes") or [],
           "at": data.get("at") or "", "by": data.get("by") or ""}
    if out["state"] == "ready":
        out["stale"] = data.get("base") != transcript_edit.fingerprint(meeting.transcript)
    if out["state"] == "saved":
        previous = data.get("previous") or {}
        out["revertable"] = previous.get("after") == transcript_edit.fingerprint(meeting.transcript)
    return out
