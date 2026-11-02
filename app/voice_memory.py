"""Voices remembered between meetings, without samples: a voice confirmed at one meeting is recognised at the next.

A confirmed voice is a person chosen by hand («Кто это?», a replica's speaker) or a name the conversation proves
surely (an introduction: speaker_confidence ≥ MEMORY_CONFIDENCE). Its print — the average voice print of that
person's clear replicas, made by the voice service — is kept encrypted, without any audio, for the retention term
(db.EmployeeVoiceMemory). Prints are made only for an employee who agreed (Employee.voice_memory_consent_at) and only
while «Запоминать голоса между встречами» is on; withdrawing the consent or «Забыть голос» deletes them, and so does
deleting the meeting they came from.

At a meeting labelled by the voice service (diarize.voice_labels gives the average print of every voice), a voice is
named when it is like the person's prints from at least MIN_MEETINGS other meetings by the threshold and clearly more
than like anyone else's (MARGIN). A name found so is a voice match («голос совпал»), never a guess: a voice that is
not like anybody stays «Спикер N».
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import math
import pathlib
import threading
import time

from . import config, settings_store, speaker_id
from .db import Employee, EmployeeVoiceMemory, Meeting, SessionLocal, now

log = logging.getLogger("sysai.voice_memory")

CONSENT_VERSION = "voice-memory-v1"
MEMORY_CONFIDENCE = 0.85   # a name from the conversation this sure (an introduction) is a confirmed voice
DEFAULT_THRESHOLD = 0.62
MARGIN = 0.08              # the person's voice must be this much more alike than the next person's
MIN_MEETINGS = 2           # prints from this many other meetings are needed to recognise a person
LATEST = 6                 # the latest prints of a person that are compared
MIN_SECONDS = 8.0          # clear speech of a person needed to remember the voice of a meeting
MAX_SECONDS = 120.0        # …and the most used
MIN_REPLICA = 2.5          # a replica shorter than this is not clear enough to remember
MIN_VOICE = 6.0            # speech of a voice at a meeting needed to recognise it
SOON = 20.0                # seconds to wait for more corrections before remembering a meeting
_pending: set = set()
_lock = threading.Lock()


def enabled() -> bool:
    return settings_store.get("voice_memory") is True and config.voice_service_enabled()


def threshold() -> float:
    try:
        value = float(settings_store.get("voice_memory_threshold"))
    except (TypeError, ValueError):
        return DEFAULT_THRESHOLD
    return min(0.95, max(0.4, value)) if math.isfinite(value) else DEFAULT_THRESHOLD


def consented(employee) -> bool:
    return bool(getattr(employee, "active", False) and getattr(employee, "voice_memory_consent_at", None))


def _number(value, default=0.0) -> float:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


def _unit(vector) -> list | None:
    try:
        values = [float(x) for x in vector]
    except (TypeError, ValueError):
        return None
    norm = math.sqrt(sum(x * x for x in values))
    return [x / norm for x in values] if values and norm > 1e-8 else None


def _dot(a, b) -> float:
    return sum(x * y for x, y in zip(a, b)) if len(a) == len(b) else -1.0


# ------------------------------------------------------------------------------------------------- remembering
def confirmed(transcript: list, employees) -> dict:
    """{employee id: {"how": "manual" | "context", "rows": [replica indexes]}} — the voices a meeting confirms."""
    by_name = {}
    for employee in employees:
        by_name.setdefault(employee.name, []).append(employee)
    out = {}
    for index, seg in enumerate(transcript or []):
        if not isinstance(seg, dict) or seg.get("speaker_guest"):
            continue
        source, eid = seg.get("speaker_source"), seg.get("speaker_employee_id")
        if source == "manual" and isinstance(eid, int) and not isinstance(eid, bool):
            entry = out.setdefault(eid, {"how": "manual", "rows": []})
            entry["how"] = "manual"
            entry["rows"].append(index)
        elif not source and _number(seg.get("speaker_confidence")) >= MEMORY_CONFIDENCE:
            same = by_name.get(str(seg.get("speaker") or ""))
            if same and len(same) == 1:
                out.setdefault(same[0].id, {"how": "context", "rows": []})["rows"].append(index)
    return out


def _clear(transcript: list, rows: list) -> list:
    """The replicas of one person good to remember: long enough, not said over somebody, longest first."""
    spans = []
    for index, seg in enumerate(transcript):
        if isinstance(seg, dict):
            start, end = _number(seg.get("start"), -1.0), _number(seg.get("end"), -1.0)
            if end > start >= 0:
                spans.append((index, start, end, str(seg.get("speaker") or "")))
    mine = set(rows)
    picked, used = [], 0.0
    candidates = sorted((span for span in spans if span[0] in mine), key=lambda span: span[1] - span[2])
    for index, start, end, label in candidates:
        seg = transcript[index]
        if end - start < MIN_REPLICA or seg.get("overlap") or len(str(seg.get("text") or "").split()) <= 3:
            continue
        if any(other[0] not in mine and other[1] < end - 0.2 and other[2] > start + 0.2 for other in spans):
            continue  # somebody else speaks at the same time
        picked.append(index)
        used += end - start
        if used >= MAX_SECONDS:
            break
    return sorted(picked) if used >= MIN_SECONDS else []


def _recording_hash(path) -> str:
    import hashlib
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def remember(meeting_id: int, path=None, by: str = "") -> dict:
    """Make the prints of the voices this meeting confirms, for the employees who agreed: {employee id: seconds}."""
    if not enabled():
        return {}
    from . import diarization_runs, diarize
    with SessionLocal() as s:
        meeting = s.get(Meeting, meeting_id)
        if meeting is None or not meeting.transcript:
            return {}
        transcript = [seg for seg in meeting.transcript]
        people = s.query(Employee).filter(Employee.active.is_(True)).all()
        found = confirmed(transcript, people)
        wanted = {eid: entry for eid, entry in found.items()
                  if any(e.id == eid and consented(e) for e in people)}
        chosen = {eid: _clear(transcript, entry["rows"]) for eid, entry in wanted.items()}
        chosen = {eid: rows for eid, rows in chosen.items() if rows}
        if not chosen:
            return {}
        temporary = False
        if path is None or not pathlib.Path(str(path)).is_file():
            path, temporary = diarization_runs.recording(meeting)
        total = _number(meeting.duration_sec)
    try:
        segments = []
        for eid, rows in chosen.items():
            for index in rows:
                seg = transcript[index]
                segments.append({"speaker": f"employee-{eid}", "start": seg.get("start"), "end": seg.get("end"),
                                 "text": seg.get("text")})
        segments.sort(key=lambda seg: _number(seg.get("start")))
        groups = diarize.voice_groups(path, segments, total)
    finally:
        if temporary:
            pathlib.Path(path).unlink(missing_ok=True)
    out = {}
    with SessionLocal() as s:
        for eid, rows in chosen.items():
            mine = [g for g in groups if g.get("label") == f"employee-{eid}" and "short" not in g]
            if not mine:
                continue
            seconds = sum(max(0.1, _number(g.get("seconds"))) for g in mine)
            centre = _unit([sum(max(0.1, _number(g.get("seconds"))) * g["embedding"][k] for g in mine)
                            for k in range(len(mine[0]["embedding"]))])
            employee = s.get(Employee, eid)
            if centre is None or employee is None or not consented(employee):  # the consent may be gone meanwhile
                continue
            s.query(EmployeeVoiceMemory).filter_by(employee_id=eid, meeting_id=meeting_id).delete()
            s.add(EmployeeVoiceMemory(
                employee_id=eid, meeting_id=meeting_id, model_version=mine[0].get("model") or "",
                encrypted_embedding=speaker_id.encrypt_voiceprint(json.dumps([round(x, 6) for x in centre])),
                seconds=round(seconds, 1), replicas=len(rows), how=wanted[eid]["how"], confirmed_by=str(by or "")[:120],
                consent_version=CONSENT_VERSION,
                expires_at=now() + dt.timedelta(days=speaker_id.VOICE_SAMPLE_RETENTION_DAYS)))
            out[eid] = round(seconds, 1)
        s.commit()
    if out:
        log.info("meeting %s: voices remembered for employees %s", meeting_id, sorted(out))
    return out


def remember_soon(meeting_id: int, by: str = "") -> None:
    """Remember a meeting after its corrections settle (one run for several «Кто это?» in a row)."""
    if not enabled():
        return
    if config.TESTING:
        remember_safely(meeting_id, by)
        return
    with _lock:
        if meeting_id in _pending:
            return
        _pending.add(meeting_id)

    def later():
        time.sleep(SOON)
        with _lock:
            _pending.discard(meeting_id)
        remember_safely(meeting_id, by)
    threading.Thread(target=later, name=f"voice-memory-{meeting_id}", daemon=True).start()


def remember_safely(meeting_id: int, by: str = "", path=None) -> dict:
    try:
        return remember(meeting_id, path=path, by=by)
    except Exception as exc:  # noqa: BLE001  remembering is a bonus, never an error of the meeting
        log.warning("remembering the voices of meeting %s failed: %s", meeting_id, exc)
        return {}


HISTORY = 40                # the latest meetings looked through for the voice of a person who just agreed


def history(employee_id: int, limit: int = LATEST) -> list:
    """The latest meetings that confirm the person's voice, with a kept recording and no print yet (ids)."""
    from . import voice_relabel
    with SessionLocal() as s:
        employee = s.get(Employee, employee_id)
        if employee is None or not consented(employee):
            return []
        people = s.query(Employee).filter(Employee.active.is_(True)).all()
        done = {mid for (mid,) in s.query(EmployeeVoiceMemory.meeting_id).filter_by(employee_id=employee_id)}
        out = []
        for meeting in s.query(Meeting).order_by(Meeting.id.desc()).limit(HISTORY):
            if meeting.id in done or not meeting.transcript or meeting.source == "demo":
                continue
            if employee_id in confirmed(meeting.transcript, people) and voice_relabel.has_recording(meeting):
                out.append(meeting.id)
            if len(out) >= limit:
                break
    return out


def remember_history(employee_id: int, by: str = "") -> None:
    """After the consent: the voice is remembered from the person's latest confirmed meetings (in the background)."""
    if not enabled():
        return

    def work():
        for meeting_id in history(employee_id):
            remember_safely(meeting_id, by)
    if config.TESTING:
        work()
        return
    threading.Thread(target=work, name=f"voice-memory-history-{employee_id}", daemon=True).start()


# ------------------------------------------------------------------------------------------------- recognising
def prints(allowed=None, exclude_meeting: int | None = None) -> dict:
    """{employee id: {"name", "prints": [(meeting id, vector, model)]}} of the employees who agreed (latest first)."""
    out = {}
    with SessionLocal() as s:
        rows = (s.query(EmployeeVoiceMemory, Employee).join(Employee, Employee.id == EmployeeVoiceMemory.employee_id)
                .filter(Employee.active.is_(True), Employee.voice_memory_consent_at.isnot(None))
                .order_by(EmployeeVoiceMemory.created_at.desc(), EmployeeVoiceMemory.id.desc()).all())
        moment = now()
        for row, employee in rows:
            if allowed is not None and employee.id not in allowed:
                continue
            if exclude_meeting is not None and row.meeting_id == exclude_meeting:
                continue
            expires = row.expires_at
            if expires is not None and (expires if expires.tzinfo else expires.replace(tzinfo=dt.timezone.utc)) <= moment:
                continue
            try:
                vector = _unit(json.loads(speaker_id.decrypt_voiceprint(row.encrypted_embedding)))
            except (speaker_id.SpeakerIdentityError, ValueError, TypeError):
                vector = None
            if vector is None:
                continue
            entry = out.setdefault(employee.id, {"name": employee.name, "prints": []})
            if len(entry["prints"]) < LATEST:
                entry["prints"].append((row.meeting_id, vector, row.model_version or ""))
    return out


def recognise(found: dict, meeting_id: int | None = None, allowed=None, memory: dict | None = None,
              limit: float | None = None) -> dict:
    """{label: {"employee_id", "name", "similarity", "meetings"}} for the voices of a meeting that are remembered."""
    centres = (found or {}).get("centres") or {}
    if not centres:
        return {}
    if memory is None:
        if not enabled():
            return {}
        memory = prints(allowed, exclude_meeting=meeting_id)
    if not memory:
        return {}
    limit = threshold() if limit is None else limit
    model = (found or {}).get("model") or ""
    seconds = (found or {}).get("seconds") or {}
    scores = {}
    for label, centre in centres.items():
        if _number(seconds.get(label), MIN_VOICE) < MIN_VOICE:
            continue
        for eid, person in memory.items():
            if allowed is not None and eid not in allowed:
                continue
            mine = [(mid, vector) for mid, vector, version in person["prints"]
                    if (not model or not version or version == model) and mid != meeting_id]
            if len({mid for mid, _ in mine}) < MIN_MEETINGS:
                continue
            alike = sorted((_dot(centre, vector) for _, vector in mine), reverse=True)
            scores[(label, eid)] = (sum(alike) / len(alike), len(alike))
    ranked = sorted(scores.items(), key=lambda item: -item[1][0])
    known, used = {}, set()
    for (label, eid), (score, count) in ranked:
        if label in known or eid in used or score < limit:
            continue
        rival = max((value[0] for (other_label, other), value in scores.items()
                     if other_label == label and other != eid), default=-1.0)
        if score - rival < MARGIN:
            continue
        known[label] = {"employee_id": eid, "name": memory[eid]["name"], "similarity": round(score, 3), "meetings": count}
        used.add(eid)
    return known


def rename(segments: list, known: dict) -> list:
    """The replicas of the recognised voices get the person (a voice match: «голос совпал»)."""
    if not known:
        return segments
    out = []
    for seg in segments:
        match = known.get(seg.get("speaker")) if isinstance(seg, dict) and seg.get("speaker_source") != "manual" else None
        if match is None:
            out.append(seg)
            continue
        item = {k: v for k, v in seg.items() if k not in ("speaker_confidence",)}
        item.update(speaker=match["name"], speaker_employee_id=match["employee_id"], speaker_source="voiceprint",
                    speaker_similarity=match["similarity"], speaker_memory=True)
        out.append(item)
    return out


# ------------------------------------------------------------------------------------------------ the employee
def set_consent(session, employee, agreed: bool, by: str = "") -> int:
    """Record or withdraw the consent; withdrawing deletes the prints. Returns the prints deleted."""
    if agreed:
        if employee.voice_memory_consent_at is None:
            employee.voice_memory_consent_at = now()
            employee.voice_memory_consent_by = str(by or "")[:120]
        return 0
    employee.voice_memory_consent_at = None
    employee.voice_memory_consent_by = ""
    return forget(session, employee.id)


def forget(session, employee_id: int, meeting_id: int | None = None) -> int:
    query = session.query(EmployeeVoiceMemory).filter_by(employee_id=employee_id)
    if meeting_id is not None:
        query = query.filter_by(meeting_id=meeting_id)
    return query.delete(synchronize_session=False)


def forget_meeting(session, meeting_id: int) -> int:
    return session.query(EmployeeVoiceMemory).filter_by(meeting_id=meeting_id).delete(synchronize_session=False)


def purge_expired(session) -> int:
    return session.query(EmployeeVoiceMemory).filter(EmployeeVoiceMemory.expires_at <= now()).delete(
        synchronize_session=False)


HOW_RU = {"manual": "подтверждён вручную", "context": "представился в разговоре"}


def card(session, employee) -> dict:
    """What the employee's «Голос» tab says about the remembered voice."""
    rows = (session.query(EmployeeVoiceMemory).filter_by(employee_id=employee.id)
            .order_by(EmployeeVoiceMemory.created_at.desc(), EmployeeVoiceMemory.id.desc()).all())
    titles = dict(session.query(Meeting.id, Meeting.title).filter(
        Meeting.id.in_([row.meeting_id for row in rows if row.meeting_id is not None])).all()) if rows else {}
    meetings = {row.meeting_id for row in rows if row.meeting_id is not None}
    on, service = settings_store.get("voice_memory") is True, config.voice_service_enabled()
    consent = employee.voice_memory_consent_at
    if not consent:
        state, label = "none", "нет согласия"
    elif not (on and service):
        state, label = "off", "выключено"
    elif len(meetings) >= MIN_MEETINGS:
        state, label = "ready", "узнаётся"
    else:
        state, label = "partial", f"{len(meetings)} из {MIN_MEETINGS} встреч"
    return {"on": on, "service": service, "state": state, "label": label,
            "consent": consent, "consent_by": employee.voice_memory_consent_by or "",
            "prints": [{"meeting_id": row.meeting_id, "title": titles.get(row.meeting_id) or "",
                        "seconds": round(row.seconds or 0), "how": HOW_RU.get(row.how, row.how or ""),
                        "at": row.created_at, "expires": row.expires_at} for row in rows],
            "meetings": len(meetings), "need": MIN_MEETINGS, "ready": len(meetings) >= MIN_MEETINGS,
            "pct": min(100, round(100 * len(meetings) / MIN_MEETINGS)), "days": speaker_id.VOICE_SAMPLE_RETENTION_DAYS}
