"""Reminders for people, asked of the AI employee in a chat, usually by voice ("напомни всем, что сегодня совещание").

The model decides who, when and about what (the action `remind` in agent.py); the code checks the request, stores it and
delivers it: the text of every reminder is written by the model at the moment of sending. A reminder survives restarts
(it lives in the database), is delivered to each person once, and is retried when Telegram or the model hiccups.
Reminders somebody asked for explicitly are not held back by the quiet hours: they are sent at the requested time.
"""
import datetime as dt
import logging
import re
import threading
import zoneinfo

from . import config, settings_store, userbot, voice
from .db import ConversationMessage, Employee, Reminder, SessionLocal, now

log = logging.getLogger("sysai.reminders")

MAX_AHEAD_DAYS = 90
MAX_ATTEMPTS = 5
RETRY_AFTER = 60          # seconds before a failed delivery is tried again (grows with every attempt)
SEND_DELAY = 4            # a reminder "now" leaves a few seconds later, so the answer to the person goes first
STALE_SENDING = 300       # a delivery that has been "sending" this long was interrupted: take it again
MAX_LATE = 20 * 60        # seconds: a reminder noticed later than this (the service was down or asleep) is not sent to people
TALKING_HOURS = 8
_lock = threading.Lock()


class WhenError(ValueError):
    """The time of a reminder cannot be used (Russian message for the model and the person)."""


def zone():
    try:
        return zoneinfo.ZoneInfo(config.TIMEZONE)
    except zoneinfo.ZoneInfoNotFoundError:
        return dt.timezone.utc


def aware(moment):
    if moment is not None and moment.tzinfo is None:
        return moment.replace(tzinfo=dt.timezone.utc)
    return moment


def local_text(moment) -> str:
    return aware(moment).astimezone(zone()).strftime("%Y-%m-%d %H:%M")


WEEKDAYS = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")


def now_view() -> dict:
    """What the model needs to turn "сегодня в 15:00" or "через час" into a date: the local time of the service."""
    local = dt.datetime.now(zone())
    return {"local": local.strftime("%Y-%m-%d %H:%M"), "weekday": WEEKDAYS[local.weekday()], "timezone": str(config.TIMEZONE)}


def parse_when(value, now_utc=None):
    """"ГГГГ-ММ-ДД ЧЧ:ММ" in the service time zone becomes a UTC moment; nothing at all means "now"."""
    moment = now_utc or now()
    if value is None or str(value).strip() == "":
        return moment
    text = str(value).strip().replace("T", " ")
    parsed = None
    for pattern in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            parsed = dt.datetime.strptime(text, pattern)
            break
        except ValueError:
            continue
    if parsed is None:
        raise WhenError("время напоминания нужно в формате ГГГГ-ММ-ДД ЧЧ:ММ (по времени сервиса) или null для «сейчас»")
    due = parsed.replace(tzinfo=zone()).astimezone(dt.timezone.utc)
    if due < moment - dt.timedelta(minutes=2):
        raise WhenError("это время уже прошло")
    if due > moment + dt.timedelta(days=MAX_AHEAD_DAYS):
        raise WhenError(f"напоминания ставятся не дальше чем на {MAX_AHEAD_DAYS} дней")
    return max(due, moment)


# ----------------------------------------------------------------------------- who gets the reminder
def contact(employee) -> str:
    return (employee.telegram_chat_id or employee.telegram_user_id or "").strip()


def reachable(employee) -> bool:
    return bool(contact(employee))


def recipients_for(s, to, chat: str, sender: dict | None = None) -> list:
    """The people a request names ("me", "all", "reviewers" or employee ids), without the person who asks, and without
    duplicates. Anyone without a Telegram contact is skipped (the agent checks that before it promises)."""
    people, seen = [], set()

    def add(ref, name, employee_id, *, user_id=""):
        ref = str(ref or "").strip()
        if not ref or ref in seen:
            return
        seen.add(ref)
        people.append({"ref": ref, "name": name or "", "employee_id": employee_id, "user_id": str(user_id or "")})

    me = s.query(Employee).filter_by(telegram_user_id=str(chat)).first()
    if to == "me":
        add(chat, me.name if me else "", me.id if me else None, user_id=chat)
        return people
    if to == "reviewers":
        for ref in settings_store.get("approver_chat_ids") or []:
            known = s.query(Employee).filter_by(telegram_chat_id=str(ref)).first()
            add(ref, known.name if known else "", known.id if known else None, user_id=known.telegram_user_id if known else "")
    else:
        query = s.query(Employee).filter_by(active=True).order_by(Employee.name)
        if to != "all":
            query = query.filter(Employee.id.in_([int(x) for x in to]))
        for e in query.all():
            if reachable(e):
                add(contact(e), e.name, e.id, user_id=e.telegram_user_id)
    me_msg = dict(sender or {}, user_id=chat)
    return [p for p in people if p["user_id"] != str(chat) and p["ref"] != str(chat)
            and not userbot.same_person(p["ref"], me_msg)]


def create(s, *, chat: str, requested_by: str, due, topic: str, details: str, recipients: list) -> int:
    row = Reminder(chat_id=str(chat), requested_by=(requested_by or "")[:120], due_at=due, topic=topic.strip()[:300],
                   details=(details or "").strip()[:1500], recipients=list(recipients), delivered=[], status="pending")
    s.add(row)
    s.flush()
    return row.id


def pending_view(s, chat: str, everyone: bool) -> list:
    """The waiting reminders as the model sees them: everyone's for people with full access, otherwise only one's own."""
    query = s.query(Reminder).filter(Reminder.status.in_(("pending", "sending")))
    if not everyone:
        query = query.filter(Reminder.chat_id == str(chat))
    return [{"id": r.id, "to": [p.get("name") or p["ref"] for p in r.recipients or []], "at": local_text(r.due_at),
             "topic": r.topic} for r in query.order_by(Reminder.due_at).limit(12).all()]


def cancel(s, reminder_id: int) -> bool:
    changed = s.query(Reminder).filter(Reminder.id == reminder_id, Reminder.status == "pending").update(
        {"status": "cancelled"}, synchronize_session=False)
    return bool(changed)


# ----------------------------------------------------------------------------- delivery
def _already_talking(s, user_id: str) -> bool:
    if not user_id:
        return False
    since = now() - dt.timedelta(hours=TALKING_HOURS)
    return s.query(ConversationMessage).filter(ConversationMessage.chat_id == str(user_id),
                                               ConversationMessage.created_at >= since).first() is not None


def run_due() -> None:
    """Called by the background loop: delivers every reminder that is due. Only the instance that holds the Telegram
    account does it, so two copies of the service during a deploy never send twice."""
    if not (userbot.ai_mode() and userbot.ready()):
        return
    with _lock:
        stamp = now()
        with SessionLocal() as s:
            s.query(Reminder).filter(Reminder.status == "sending",
                                     Reminder.claimed_at < stamp - dt.timedelta(seconds=STALE_SENDING)).update(
                {"status": "pending"}, synchronize_session=False)
            s.commit()
            ids = [r.id for r in s.query(Reminder).filter(Reminder.status == "pending", Reminder.due_at <= stamp)
                   .order_by(Reminder.due_at).limit(20).all()]
        for reminder_id in ids:
            try:
                _deliver(reminder_id, noticed=stamp)
            except Exception:  # noqa: BLE001
                log.exception("reminder %s failed", reminder_id)


def _deliver(reminder_id: int, noticed=None) -> None:
    """`noticed` is the moment the loop saw the reminder was due (not the moment this one's turn came): a long queue
    must not make a punctual reminder look late."""
    from . import agent
    noticed = noticed or now()
    with SessionLocal() as s:
        claimed = s.query(Reminder).filter(Reminder.id == reminder_id, Reminder.status == "pending").update(
            {"status": "sending", "claimed_at": now()}, synchronize_session=False)
        s.commit()
        if not claimed:
            return
        row = s.get(Reminder, reminder_id)
        topic, details, by, chat = row.topic, row.details, row.requested_by, row.chat_id
        people, delivered = list(row.recipients or []), list(row.delivered or [])
        # due_at is the moment this delivery was expected: the original time, or the time of a retry (a failed attempt
        # moves it a few minutes ahead). Either way, noticing it long after means the service was not running.
        late = (noticed - aware(row.due_at)).total_seconds()
        if late > MAX_LATE:
            row.status = "missed"
            row.error = f"не отправлено вовремя: опоздание {int(late // 60)} мин, оставшимся людям ничего не ушло"
            due_text = local_text(row.due_at)
            s.commit()
            missed = True
        else:
            missed = False
    if missed:
        log.warning("reminder %s is %d minutes late: not sent, the person who asked is told", reminder_id, int(late // 60))
        _report_missed(reminder_id, topic, due_text, int(late // 60), chat,
                       [p.get("name") or p["ref"] for p in people if p["ref"] not in delivered])
        return
    failures = {}
    for person in people:
        if person["ref"] in delivered:
            continue
        try:
            with SessionLocal() as s:
                talking = _already_talking(s, person.get("user_id", ""))
            text = voice.for_reminder(topic, details, by, person.get("name", ""), person["ref"] == chat, talking)
            sent = userbot.send_message(person["ref"], text, urgent=True)
            agent.record_outgoing(person["ref"], text, sent.get("message_id"), sent.get("user_id") or person.get("user_id") or None)
            delivered.append(person["ref"])
        except Exception as exc:  # noqa: BLE001
            failures[person["ref"]] = str(exc)[:160]
            log.warning("reminder %s to %s not delivered: %s", reminder_id, person["ref"], exc)
        with SessionLocal() as s:  # progress is saved after every person: a restart never repeats a message
            s.get(Reminder, reminder_id).delivered = list(delivered)
            s.commit()
    with SessionLocal() as s:
        row = s.get(Reminder, reminder_id)
        row.attempts = int(row.attempts or 0) + (1 if failures else 0)
        if not failures:
            row.status, row.sent_at, row.error = "sent", now(), ""
        elif row.attempts >= MAX_ATTEMPTS:
            row.status, row.error = "failed", "; ".join(f"{k}: {v}" for k, v in failures.items())[:500]
        else:
            row.status, row.error = "pending", "; ".join(f"{k}: {v}" for k, v in failures.items())[:500]
            row.due_at = now() + dt.timedelta(seconds=RETRY_AFTER * row.attempts)
        final, status = row.status in ("sent", "failed"), row.status
        s.commit()
    if final and chat and (len(people) > 1 or failures or (people and people[0]["ref"] != chat)):
        _report(reminder_id, status)


def _report_missed(reminder_id: int, topic: str, due_text: str, late_minutes: int, chat: str, names: list) -> None:
    """The service was down when the reminder was due: people get nothing (a message about a meeting that is over would
    only confuse), the person who asked is told and can ask again if it still matters."""
    if not chat:
        return
    from . import agent
    try:
        text = voice.for_reminder_missed(topic, due_text, late_minutes, names)
        sent = userbot.send_message(chat, text, urgent=True)
        agent.record_outgoing(chat, text, sent.get("message_id"), sent.get("user_id") or chat)
    except Exception as exc:  # noqa: BLE001
        log.warning("report on the missed reminder %s not sent: %s", reminder_id, exc)


def _report(reminder_id: int, status: str) -> None:
    """Tells the person who asked who got the reminder (only when it was not just a reminder to themselves)."""
    from . import agent
    with SessionLocal() as s:
        row = s.get(Reminder, reminder_id)
        done = set(row.delivered or [])
        delivered = [p.get("name") or p["ref"] for p in row.recipients or [] if p["ref"] in done]
        failed = [{"who": p.get("name") or p["ref"], "reason": (row.error or "")[:120]} for p in row.recipients or []
                  if p["ref"] not in done]
        topic, chat = row.topic, row.chat_id
    try:
        text = voice.for_reminder_report(topic, delivered, failed)
        sent = userbot.send_message(chat, text, urgent=True)
        agent.record_outgoing(chat, text, sent.get("message_id"), sent.get("user_id") or chat)
    except Exception as exc:  # noqa: BLE001
        log.warning("report on reminder %s not sent: %s", reminder_id, exc)
