"""Reminders about meetings set in the web panel ("Напоминания"): the meeting time, a switch and the people to remind.

An alert says "the meeting is at 10:00 on weekdays, remind these people 15 minutes before" (or once, on a date). The
background loop turns an alert that is due into an ordinary reminder (reminders.py), so delivery, retries, "once per person"
and the model-written text are the same as for a reminder somebody asked for in a chat.

The director may remind the same people by hand ("напомни всем о совещании", written or spoken). The alert that has not
fired yet then gives way for exactly those people: `claim` records it, `run_due` leaves them out, `release` takes the claim
back when the director cancels that reminder. Nobody is told twice about one meeting.
"""
import datetime as dt
import logging
import threading

from . import reminders, settings_store, userbot, voice
from .db import Employee, MeetingAlert, SessionLocal, now

log = logging.getLogger("sysai.meeting_alerts")

WEEKDAYS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
WEEKDAYS_LONG = ("в понедельник", "во вторник", "в среду", "в четверг", "в пятницу", "в субботу", "в воскресенье")
LEADS = (0, 5, 10, 15, 30, 60, 120)     # minutes before the meeting offered in the form
DEFAULT_DAYS = [0, 1, 2, 3, 4]
MAX_LEAD = 12 * 60
_lock = threading.Lock()


class AlertError(ValueError):
    """Something in the form cannot be used (Russian message shown in the panel)."""


def lead_label(minutes: int) -> str:
    if minutes <= 0:
        return "в момент начала"
    if minutes == 60:
        return "за 1 час"
    if minutes % 60 == 0 and minutes < 600:
        return f"за {minutes // 60} ч"
    return f"за {minutes} мин"


# ----------------------------------------------------------------------------- when
def _meeting_time(alert) -> dt.time:
    hour, minute = str(alert.meet_time).split(":")[:2]
    return dt.time(int(hour), int(minute))


def meeting_at(alert, day: dt.date):
    """The start of the meeting on this date (service time zone), or None when it does not take place that day."""
    if alert.once_date:
        if day != alert.once_date:
            return None
    elif day.weekday() not in (alert.weekdays or []):
        return None
    return dt.datetime.combine(day, _meeting_time(alert), tzinfo=reminders.zone())


def _times(alert, day: dt.date):
    meeting = meeting_at(alert, day)
    if meeting is None:
        return None, None
    return meeting - dt.timedelta(minutes=int(alert.lead_minutes or 0)), meeting


def _lost(fire, meeting, local_now) -> bool:
    """Too late to remind: the meeting has begun, or the loop noticed the time long after it (the service was down)."""
    return local_now >= meeting or (local_now - fire).total_seconds() > reminders.MAX_LATE


def _settle(alert, local_now) -> None:
    """A new (or just switched on) alert starts with the next occurrence: one whose time has already gone is not "missed"."""
    today = local_now.date()
    for day in (today, today + dt.timedelta(days=1)):
        fire, meeting = _times(alert, day)
        if fire is not None and local_now >= fire and _lost(fire, meeting, local_now):
            if alert.last_fired_on is None or alert.last_fired_on < day:
                alert.last_fired_on = day


# ----------------------------------------------------------------------------- the form
def clean(values: dict) -> dict:
    """Column values from a form (plain dict, lists for several values); AlertError when something cannot be used."""
    title = str(values.get("title") or "").strip()[:120] or "Совещание"
    details = str(values.get("details") or "").strip()[:500]
    try:
        hour, minute = str(values.get("meet_time") or "").strip().split(":")[:2]
        meet = dt.time(int(hour), int(minute))
    except (ValueError, TypeError):
        raise AlertError("Укажите время совещания в формате ЧЧ:ММ") from None
    try:
        lead = int(str(values.get("lead_minutes") if values.get("lead_minutes") not in (None, "") else 15).strip())
    except ValueError:
        raise AlertError("За сколько минут напомнить: целое число") from None
    if not 0 <= lead <= MAX_LEAD:
        raise AlertError(f"Напоминать можно за 0 до {MAX_LEAD} минут")
    once_text = str(values.get("once_date") or "").strip()
    once = None
    if once_text:
        try:
            once = dt.date.fromisoformat(once_text)
        except ValueError:
            raise AlertError("Дата в формате ГГГГ-ММ-ДД") from None
    days = []
    for raw in values.get("weekdays") or []:
        try:
            day = int(raw)
        except (TypeError, ValueError):
            raise AlertError("Неверный день недели") from None
        if not 0 <= day <= 6:
            raise AlertError("Неверный день недели")
        if day not in days:
            days.append(day)
    if once is None and not days:
        raise AlertError("Выберите дни недели или одну дату")
    people = []
    for raw in values.get("recipients") or []:
        try:
            person = int(raw)
        except (TypeError, ValueError):
            continue
        if person not in people:
            people.append(person)
    return {"title": title, "details": details, "meet_time": meet.strftime("%H:%M"), "lead_minutes": lead,
            "weekdays": sorted(days) if once is None else [], "once_date": once, "recipients": people}


def save(values: dict, alert_id: int = 0) -> int:
    data = clean(values)
    local_now = now().astimezone(reminders.zone())
    with SessionLocal() as s:
        known = {row[0] for row in s.query(Employee.id).all()}
        data["recipients"] = [i for i in data["recipients"] if i in known]
        if not data["recipients"]:
            raise AlertError("Выберите, кому напоминать")
        if alert_id:
            alert = s.get(MeetingAlert, alert_id)
            if alert is None:
                raise AlertError("Напоминание не найдено")
        else:
            alert = MeetingAlert(enabled=True)
        for key, value in data.items():
            setattr(alert, key, value)
        if alert.once_date:
            meeting = meeting_at(alert, alert.once_date)
            if meeting <= local_now:
                raise AlertError("Это время уже прошло")
        if alert.enabled:
            _settle(alert, local_now)
        s.add(alert)
        s.commit()
        return alert.id


def toggle(alert_id: int) -> bool:
    """Switches an alert on or off; returns the new state. Switched on, it starts with the next occurrence."""
    local_now = now().astimezone(reminders.zone())
    with SessionLocal() as s:
        alert = s.get(MeetingAlert, alert_id)
        if alert is None:
            raise AlertError("Напоминание не найдено")
        alert.enabled = not alert.enabled
        if alert.enabled:
            _settle(alert, local_now)
        s.commit()
        return bool(alert.enabled)


def delete(alert_id: int) -> None:
    with SessionLocal() as s:
        alert = s.get(MeetingAlert, alert_id)
        if alert is not None:
            s.delete(alert)
            s.commit()


# ----------------------------------------------------------------------------- the director reminded by hand
CLAIM_HORIZON = dt.timedelta(hours=24)   # a hand-made reminder is about a meeting that starts within this time after it


def claim(s, people: list, due, reminder_id: int) -> None:
    """The director reminded these people about a meeting by hand: for each of them the alert whose next meeting (after the
    moment of that reminder, within a day) is closest and has not fired yet gives way, for that meeting's date only.
    `people` are the recipients of the reminder ({"employee_id": ...}); the caller commits."""
    wanted = {p.get("employee_id") for p in people if p.get("employee_id")}
    if not wanted:
        return
    zone = reminders.zone()
    local_now, due_local = now().astimezone(zone), reminders.aware(due).astimezone(zone)
    candidates = []
    for alert in s.query(MeetingAlert).filter(MeetingAlert.enabled.is_(True)).all():
        for day in (due_local.date(), due_local.date() + dt.timedelta(days=1)):
            fire, meeting = _times(alert, day)
            if fire is None or meeting <= due_local or meeting - due_local > CLAIM_HORIZON:
                continue
            if alert.last_fired_on is not None and alert.last_fired_on >= day:
                continue                                   # that occurrence has already been handled
            if local_now >= fire:
                continue                                   # it is due right now or already gone off: nothing to take back
            candidates.append((abs((fire - due_local).total_seconds()), alert.id, day, alert))
            break                                          # the first meeting of this alert after the director's reminder
    taken = set()
    for _, _, day, alert in sorted(candidates, key=lambda c: c[:2]):
        mine = [i for i in (alert.recipients or []) if i in wanted and i not in taken]
        if not mine:
            continue
        keep = [x for x in (alert.skips or []) if x.get("date", "") >= local_now.date().isoformat()]
        alert.skips = keep + [{"date": day.isoformat(), "employee_id": i, "by": reminder_id} for i in mine]
        taken.update(mine)


def release(s, reminder_id: int) -> None:
    """The director cancelled that reminder: the alerts take their people back."""
    for alert in s.query(MeetingAlert).all():
        left = [x for x in (alert.skips or []) if x.get("by") != reminder_id]
        if len(left) != len(alert.skips or []):
            alert.skips = left


# ----------------------------------------------------------------------------- the loop
def _people(s, alert, day: dt.date):
    """(people to write to, names of all active recipients, names left out because the director reminded them, names
    without a Telegram contact)."""
    skipped = {x.get("employee_id") for x in (alert.skips or []) if x.get("date") == day.isoformat()}
    send, everyone, by_director, silent = [], [], [], []
    ids = alert.recipients or []
    for e in s.query(Employee).filter(Employee.id.in_(ids)).order_by(Employee.name).all() if ids else []:
        if not e.active:
            continue
        everyone.append(e.name)
        if e.id in skipped:
            by_director.append(e.name)
        elif not reminders.reachable(e):
            silent.append(e.name)
        else:
            send.append({"ref": reminders.contact(e), "name": e.name, "employee_id": e.id,
                         "user_id": e.telegram_user_id or ""})
    return send, everyone, by_director, silent


def _details(alert, meeting) -> str:
    text = f"начало в {meeting.strftime('%H:%M')}"
    if int(alert.lead_minutes or 0) > 0:
        text += f", {lead_label(int(alert.lead_minutes))} (напоминание)"
    if (alert.details or "").strip():
        text += ". " + alert.details.strip()
    return text


def _fire(s, alert, day, meeting, fire, local_now, stamp):
    """Handles one due occurrence. Returns what the director must be told when it was missed, else None."""
    alert.last_fired_on = day
    late = (local_now - fire).total_seconds()
    send, everyone, by_director, silent = _people(s, alert, day)
    if alert.once_date:
        alert.enabled = False
    if _lost(fire, meeting, local_now):
        alert.last_result = "пропущено: сервис не работал в это время, людям ничего не отправлено"
        return (alert.title, fire.strftime("%Y-%m-%d %H:%M"), int(late // 60), everyone)
    if not send:
        if by_director and not silent:
            alert.last_result = "не отправлено: директор уже напомнил всем"
        elif silent:
            alert.last_result = ("некому писать: нет Telegram-контакта у " + ", ".join(silent))[:300]
        else:
            alert.last_result = "некому писать: нет активных получателей"
        return None
    reminders.create(s, chat="", requested_by="", due=stamp, topic=alert.title, details=_details(alert, meeting),
                     recipients=send)
    result = "отправлено: " + ", ".join(p["name"] for p in send)
    if by_director:
        result += "; уже напомнил директор: " + ", ".join(by_director)
    alert.last_result = result[:300]
    return None


def run_due() -> None:
    """Called by the background loop: the alerts whose time has come become reminders (delivered by reminders.run_due).
    Only the instance that holds the Telegram account does it."""
    if not (userbot.ai_mode() and userbot.ready()):
        return
    with _lock:
        stamp = now()
        local_now = stamp.astimezone(reminders.zone())
        today = local_now.date()
        missed = []
        with SessionLocal() as s:
            for alert in s.query(MeetingAlert).filter(MeetingAlert.enabled.is_(True)).order_by(MeetingAlert.id).all():
                for day in (today, today + dt.timedelta(days=1)):
                    fire, meeting = _times(alert, day)
                    if fire is None or local_now < fire:
                        continue
                    if alert.last_fired_on is not None and alert.last_fired_on >= day:
                        continue
                    lost = _fire(s, alert, day, meeting, fire, local_now, stamp)
                    if lost:
                        missed.append(lost)
                    break  # one occurrence per alert per tick
            s.commit()
    for item in missed:
        _tell_directors(*item)


def _tell_directors(title: str, due_text: str, late_minutes: int, names: list) -> None:
    """The service was not running when a meeting alert was due: the director hears about it (not the people)."""
    from . import agent
    refs = settings_store.get("director_chat_ids") or settings_store.get("approver_chat_ids") or []
    for ref in refs[:3]:
        try:
            text = voice.for_reminder_missed(title, due_text, late_minutes, names)
            sent = userbot.send_message(ref, text, urgent=True)
            agent.record_outgoing(ref, text, sent.get("message_id"), sent.get("user_id") or None)
        except Exception as exc:  # noqa: BLE001
            log.warning("the director %s was not told about the missed alert %r: %s", ref, title, exc)


# ----------------------------------------------------------------------------- the page
def _initials(name: str) -> str:
    parts = [p for p in str(name).split() if p]
    return "".join(p[0] for p in parts[:2]).upper() or "?"


def _day_phrase(day: dt.date, today: dt.date) -> str:
    if day == today:
        return "сегодня"
    if day == today + dt.timedelta(days=1):
        return "завтра"
    return f"{WEEKDAYS_LONG[day.weekday()]}, {day.strftime('%d.%m')}"


def _next_text(alert, local_now) -> str:
    if not alert.enabled:
        return "выключено"
    today = local_now.date()
    for offset in range(0, 15):
        day = today + dt.timedelta(days=offset)
        fire, meeting = _times(alert, day)
        if fire is None or meeting <= local_now:
            continue
        if alert.last_fired_on is not None and alert.last_fired_on >= day:
            continue
        when = f"{_day_phrase(day, today)} в {fire.strftime('%H:%M')}"
        return f"следующее напоминание: {when}" + (f" (совещание в {meeting.strftime('%H:%M')})" if fire != meeting else "")
    return "больше не сработает: дата прошла" if alert.once_date else "нет ближайших дней"


def view(alert, people: dict, local_now) -> dict:
    today = local_now.date()
    names = [people[i] for i in (alert.recipients or []) if i in people]
    skipped_ids = {x.get("employee_id") for x in (alert.skips or []) if x.get("date", "") >= today.isoformat()}
    return {"id": alert.id, "enabled": bool(alert.enabled), "title": alert.title, "details": alert.details or "",
            "meet_time": alert.meet_time, "lead_minutes": int(alert.lead_minutes or 0),
            "lead_text": lead_label(int(alert.lead_minutes or 0)),
            "days": [{"name": WEEKDAYS[i], "on": i in (alert.weekdays or [])} for i in range(7)],
            "once": alert.once_date.strftime("%d.%m.%Y") if alert.once_date else "",
            "once_iso": alert.once_date.isoformat() if alert.once_date else "",
            "weekdays": list(alert.weekdays or []),
            "people": [{"name": p["name"], "initials": _initials(p["name"]), "reachable": p["reachable"]} for p in names],
            "recipient_ids": [i for i in (alert.recipients or [])],
            "silent": [p["name"] for p in names if not p["reachable"]],
            "by_director": [people[i]["name"] for i in skipped_ids if i in people and i in (alert.recipients or [])],
            "next": _next_text(alert, local_now), "last": alert.last_result or ""}


def page_data(edit_id: int = 0) -> dict:
    local_now = now().astimezone(reminders.zone())
    with SessionLocal() as s:
        staff = s.query(Employee).filter(Employee.active.is_(True)).order_by(Employee.name).all()
        people = {e.id: {"id": e.id, "name": e.name, "position": e.position or "", "initials": _initials(e.name),
                         "reachable": reminders.reachable(e)} for e in staff}
        alerts = [view(a, people, local_now) for a in s.query(MeetingAlert).order_by(MeetingAlert.meet_time, MeetingAlert.id).all()]
    return {"alerts": alerts, "employees": list(people.values()),
            "edit": next((a for a in alerts if a["id"] == edit_id), None),
            "weekdays": list(enumerate(WEEKDAYS)), "default_days": DEFAULT_DAYS,
            "leads": [(m, lead_label(m)) for m in LEADS], "lead_label": lead_label}
