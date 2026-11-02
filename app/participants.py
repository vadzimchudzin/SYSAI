"""Who was at a meeting (task «Кто говорит» №4).

A new meeting takes its people from its regular meeting in «Напоминания» (an alert has the time, the days and the
people to remind); on the meeting page the list is corrected: people added or removed, guests written in. The list then
works everywhere a name is chosen for a voice: names come only from the participants, the number of participants is a
hint for the whole-record speaker labelling (diarize.py), and the protocol says «Присутствовали».

Meeting.participants = {"people": [{"employee_id": 3}, {"guest": "Иван, подрядчик"}],
                        "source": "alert" | "manual", "alert_id": 5, "alert_title": "Планёрка", "by": "...", "at": "..."}
None means nobody has set it yet (no matching alert): then every active employee may be named, as before.
"""
from __future__ import annotations

import datetime as dt
import re

from . import meeting_alerts, reminders
from .db import Employee, MeetingAlert

MAX_PEOPLE = 80
GUEST_LENGTH = 80
NEAR_MINUTES = 180  # a recording that starts this close to an alert's time belongs to it


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def entries(meeting) -> list:
    data = meeting.participants if isinstance(getattr(meeting, "participants", None), dict) else {}
    return [item for item in data.get("people") or [] if isinstance(item, dict)][:MAX_PEOPLE]


def is_set(meeting) -> bool:
    return isinstance(getattr(meeting, "participants", None), dict)


def employee_ids(meeting) -> list:
    out = []
    for item in entries(meeting):
        try:
            value = int(item.get("employee_id"))
        except (TypeError, ValueError):
            continue
        if value not in out:
            out.append(value)
    return out


def guests(meeting) -> list:
    return [str(item["guest"]).strip()[:GUEST_LENGTH] for item in entries(meeting)
            if isinstance(item.get("guest"), str) and str(item["guest"]).strip()]


def count(meeting) -> int:
    return len(employee_ids(meeting)) + len(guests(meeting))


def _store(meeting, ids, guest_names, source: str, by: str = "", alert=None) -> None:
    people = [{"employee_id": int(value)} for value in dict.fromkeys(ids)]
    seen = set()
    for name in guest_names:
        name = re.sub(r"\s+", " ", str(name or "")).strip()[:GUEST_LENGTH]
        if name and name.casefold() not in seen:
            seen.add(name.casefold())
            people.append({"guest": name})
    data = {"people": people[:MAX_PEOPLE], "source": source, "by": str(by or "")[:120], "at": _now()}
    previous = meeting.participants if isinstance(meeting.participants, dict) else {}
    if alert is not None:
        data.update(alert_id=alert.id, alert_title=alert.title or "Совещание")
    elif previous.get("alert_id"):
        data.update(alert_id=previous.get("alert_id"), alert_title=previous.get("alert_title", ""))
    meeting.participants = data


# ------------------------------------------------------------------------------------------ from the reminders
def _started_at(meeting):
    """When the recording began, when it is known (TicNote gives it; a microphone recording starts when it is made)."""
    raw = (meeting.options or {}).get("recorded_at") if isinstance(meeting.options, dict) else None
    if raw:
        try:
            moment = dt.datetime.fromisoformat(str(raw))
        except ValueError:
            moment = None
        if moment is not None:
            return moment if moment.tzinfo else moment.replace(tzinfo=dt.timezone.utc)
    if meeting.source == "microphone" and meeting.created_at:
        created = meeting.created_at
        return created if created.tzinfo else created.replace(tzinfo=dt.timezone.utc)
    return None


def match_alert(session, meeting):
    """The regular meeting this recording is, or None when it is not clear.

    Alerts held on the meeting's date are the candidates; with one candidate it is that one; with several, the one
    whose time is nearest to the start of the recording (within three hours), else the one named in the title."""
    day = meeting.meeting_date
    if day is None:
        return None
    candidates = []
    for alert in session.query(MeetingAlert).order_by(MeetingAlert.id).all():
        try:
            at = meeting_alerts.meeting_at(alert, day)
        except (TypeError, ValueError):
            continue
        if at is not None and alert.recipients:
            candidates.append((alert, at))
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0][0]
    started = _started_at(meeting)
    if started is not None:
        started = started.astimezone(reminders.zone())
        alert, at = min(candidates, key=lambda pair: abs((pair[1] - started).total_seconds()))
        if abs((at - started).total_seconds()) <= NEAR_MINUTES * 60:
            return alert
    title = str(meeting.title or "").casefold()
    named = [alert for alert, _ in candidates if (alert.title or "").strip() and alert.title.strip().casefold() in title]
    return named[0] if len(named) == 1 else None


def ensure(session, meeting) -> bool:
    """Give a meeting without a list the people of its regular meeting. True when the list was set now."""
    if is_set(meeting):
        return False
    alert = match_alert(session, meeting)
    if alert is None:
        return False
    active = {row.id for row in session.query(Employee.id).filter(Employee.active.is_(True)).all()}
    ids = [int(value) for value in alert.recipients or [] if str(value).isdigit() and int(value) in active]
    if not ids:
        return False
    _store(meeting, ids, [], "alert", alert=alert)
    return True


# ------------------------------------------------------------------------------------------------- corrections
def set_people(session, meeting, ids, guest_names, by: str = "") -> None:
    """The list chosen on the meeting page: active employees by id, guests by name."""
    active = {row.id for row in session.query(Employee.id).filter(Employee.active.is_(True)).all()}
    chosen = []
    for value in ids:
        try:
            value = int(value)
        except (TypeError, ValueError):
            continue
        if value in active and value not in chosen:
            chosen.append(value)
    _store(meeting, chosen, guest_names, "manual", by)


def add(meeting, employee_id=None, guest: str = "", by: str = "") -> bool:
    """Somebody was named as a voice of this meeting, so they were there. True when the list grew.

    A meeting without a list keeps having none: nobody chose who was there, and one name is not the whole list."""
    if not is_set(meeting):
        return False
    ids, names = employee_ids(meeting), guests(meeting)
    if employee_id is not None:
        employee_id = int(employee_id)
        if employee_id in ids:
            return False
        ids.append(employee_id)
    elif guest and guest.strip().casefold() not in {name.casefold() for name in names}:
        names.append(guest)
    else:
        return False
    source = (meeting.participants or {}).get("source") or "manual"
    _store(meeting, ids, names, source, by or (meeting.participants or {}).get("by", ""))
    return True


# ------------------------------------------------------------------------------------------------------ showing
def view(session, meeting) -> dict:
    """What the meeting page shows: the people with names, the guests and where the list came from."""
    ids = employee_ids(meeting)
    rows = {row.id: row for row in session.query(Employee).filter(Employee.id.in_(ids)).all()} if ids else {}
    people = [{"id": value, "name": rows[value].name, "position": rows[value].position or "",
               "active": bool(rows[value].active)} for value in ids if value in rows]
    data = meeting.participants if isinstance(meeting.participants, dict) else {}
    if not data:
        origin = "Список не задан: имена голосам подбираются из всех сотрудников."
    elif data.get("source") == "alert":
        origin = f"Из напоминания «{data.get('alert_title') or 'Совещание'}»."
    else:
        origin = "Исправлено вручную" + (f" · {data.get('by')}" if data.get("by") else "") + "."
        if data.get("alert_title"):
            origin += f" Сначала было из напоминания «{data['alert_title']}»."
    return {"set": bool(data), "people": people, "guests": guests(meeting), "count": len(people) + len(guests(meeting)),
            "ids": [person["id"] for person in people], "origin": origin}


def present(session, meeting) -> list:
    """«Присутствовали»: the names, guests marked as such; empty when the list is not set."""
    info = view(session, meeting)
    return [person["name"] for person in info["people"]] + [f"{name} (гость)" for name in info["guests"]]


def allowed_people(session, meeting) -> list:
    """The employees whose names may be given to the voices of this meeting."""
    query = session.query(Employee).filter(Employee.active.is_(True))
    ids = employee_ids(meeting)
    if is_set(meeting):
        query = query.filter(Employee.id.in_(ids or [-1]))
    return query.order_by(Employee.id).all()
