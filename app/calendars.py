"""Календарь (№26): сроки задач и совещания — в привычном календаре, состав встречи — из календаря компании.

Out. Every employee has a secret calendar address (ICS) with the deadlines of their tasks and their regular meetings
from «Напоминания»; the director has one address with everything. Google, Outlook, Яндекс and Apple calendars subscribe
to such an address by the link and refresh it by themselves (Google — a few times a day, Outlook — every few hours).
«Сменить ссылки» makes every old address stop working.

In. The secret ICS address of the company calendar (Google: «Закрытый адрес в формате iCal», Outlook: «Опубликовать
календарь», Яндекс: «Экспорт»). It is read every 15 minutes; a meeting of the calendar with invited people held at the
time of a recording gives the recording its list of participants (participants.py) — before the regular meetings of
«Напоминания». The invited are matched to the employees by the work e-mail of their card, else by the name (also written
in Latin letters); the others become guests; those who declined, rooms and resources are left out.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import logging
import re
import secrets
import threading
import zoneinfo

import httpx

from . import config, reminders, settings_store
from .db import CalendarEvent, Employee, MeetingAlert, SessionLocal, Task, now

log = logging.getLogger("sysai.calendars")

POLL_MINUTES = 15
RETRY_MINUTES = 60          # after an error the address is tried again less often
PAST_DAYS = 60              # the meetings of the calendar kept for recordings made earlier
AHEAD_DAYS = 90
MAX_EVENTS = 3000
MAX_BYTES = 15_000_000
MAX_STEPS = 20000           # a recurrence is never walked further than this
FEED_PAST_DAYS = 30         # done tasks and past meetings stay in the feed this long
FEED_OPEN_PAST_DAYS = 365   # an open task with a deadline long gone still shows (it is overdue)
MEETING_MINUTES = 60
NEAR_MINUTES = 180          # a recording that starts this close to a meeting of the calendar belongs to it
DONE = ("done", "assistant_done")
TASK_STATUS_RU = {"draft": "черновик протокола", "sent": "отправлена", "awaiting_deadline": "ждёт срока",
                  "no_recipient": "нет получателя", "in_progress": "в работе", "blocked": "мешает",
                  "done": "выполнена", "assistant_todo": "делает ИИ-сотрудник", "assistant_waiting": "ИИ-сотрудник ждёт",
                  "assistant_done": "сделано ИИ-сотрудником", "assistant_failed": "ИИ-сотрудник не смог"}
WEEKDAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
# Outlook writes Windows names of the time zones
WINDOWS_ZONES = {
    "russian standard time": "Europe/Moscow", "belarus standard time": "Europe/Minsk", "fle standard time": "Europe/Kyiv",
    "e. europe standard time": "Europe/Chisinau", "gtb standard time": "Europe/Bucharest",
    "central european standard time": "Europe/Warsaw", "central europe standard time": "Europe/Budapest",
    "w. europe standard time": "Europe/Berlin", "romance standard time": "Europe/Paris", "gmt standard time": "Europe/London",
    "utc": "UTC", "coordinated universal time": "UTC", "kaliningrad standard time": "Europe/Kaliningrad",
    "russia time zone 3": "Europe/Samara", "ekaterinburg standard time": "Asia/Yekaterinburg",
    "n. central asia standard time": "Asia/Novosibirsk", "north asia standard time": "Asia/Krasnoyarsk",
    "north asia east standard time": "Asia/Irkutsk", "yakutsk standard time": "Asia/Yakutsk",
    "vladivostok standard time": "Asia/Vladivostok", "central asia standard time": "Asia/Almaty",
    "west asia standard time": "Asia/Tashkent", "caucasus standard time": "Asia/Yerevan",
    "georgian standard time": "Asia/Tbilisi", "azerbaijan standard time": "Asia/Baku", "turkey standard time": "Europe/Istanbul",
    "israel standard time": "Asia/Jerusalem", "arabian standard time": "Asia/Dubai", "china standard time": "Asia/Shanghai",
    "tokyo standard time": "Asia/Tokyo", "eastern standard time": "America/New_York", "central standard time": "America/Chicago",
    "mountain standard time": "America/Denver", "pacific standard time": "America/Los_Angeles",
}
_LOCK = threading.Lock()


class CalendarError(ValueError):
    """The calendar cannot be read or saved (a Russian message for the page)."""


def _aware(moment):
    if moment is None:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=dt.timezone.utc)


# ============================================================================== out: the addresses for subscription
def feed_on() -> bool:
    return bool(settings_store.get("calendar_feed_on"))


def _salt() -> str:
    salt = str(settings_store.get("calendar_salt") or "")
    if not salt:
        salt = secrets.token_hex(8)
        settings_store.set_many({"calendar_salt": salt})
    return salt


def token(key) -> str:
    """The secret part of an address: «all» for the director's one, else the employee id."""
    message = f"calendar:{_salt()}:{key}".encode()
    return hmac.new(config.SECRET_KEY.encode(), message, hashlib.sha256).hexdigest()[:32]


def reset_links() -> None:
    """«Сменить ссылки»: every address given before stops working."""
    settings_store.set_many({"calendar_salt": secrets.token_hex(8)})


def owner(s, value: str):
    """Whose address this is: "all", an active employee or None."""
    value = str(value or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{32}", value):
        return None
    if hmac.compare_digest(value, token("all")):
        return "all"
    for e in s.query(Employee).filter(Employee.active.is_(True)).order_by(Employee.id).all():
        if hmac.compare_digest(value, token(e.id)):
            return e
    return None


def base_url(fallback: str = "") -> str:
    return (config.PUBLIC_URL or str(fallback or "")).rstrip("/")


def link(key, base: str = "") -> str:
    return f"{base_url(base)}/calendar/feed/{token(key)}.ics"


def links(s, base: str = "") -> list:
    """The rows of the page: each active employee with the address and whether it can be sent to them."""
    out = []
    for e in s.query(Employee).filter(Employee.active.is_(True)).order_by(Employee.org_order, Employee.name).all():
        open_tasks = (s.query(Task).filter(Task.assignee_id == e.id, Task.deadline.isnot(None),
                                           Task.status.notin_(DONE + ("draft",))).count())
        out.append({"id": e.id, "name": e.name, "position": e.position or "", "url": link(e.id, base),
                    "telegram": bool(reminders.contact(e)), "email": e.email or "", "tasks": open_tasks})
    return out


def _esc(text) -> str:
    text = str(text or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
    return re.sub(r"\r\n|\r|\n", "\\\\n", text)


def _fold(line: str) -> str:
    """Lines of an ICS file are at most 75 octets: longer ones go on after a line break and a space."""
    raw = line.encode()
    if len(raw) <= 75:
        return line
    parts, current, size = [], "", 0
    for ch in line:
        width = len(ch.encode())
        if size + width > (75 if not parts else 74):
            parts.append(current)
            current, size = "", 0
        current += ch
        size += width
    parts.append(current)
    return "\r\n ".join(parts)


def _offset(delta: dt.timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    sign = "-" if minutes < 0 else "+"
    minutes = abs(minutes)
    return f"{sign}{minutes // 60:02d}{minutes % 60:02d}"


def _rule_day(day: dt.date) -> str:
    nth = -1 if (day + dt.timedelta(days=7)).month != day.month else (day.day - 1) // 7 + 1
    return f"{nth}{WEEKDAYS[day.weekday()]}"


def _vtimezone(zone) -> list:
    """The time zone of the service for the calendars: its offset, and its summer time when it has one."""
    name = getattr(zone, "key", "") or "UTC"
    year = dt.date.today().year - 1
    moment = dt.datetime(year, 1, 1, tzinfo=dt.timezone.utc)
    end = dt.datetime(year + 1, 1, 1, tzinfo=dt.timezone.utc)
    previous = moment.astimezone(zone).utcoffset()
    changes = []
    while moment < end:
        moment += dt.timedelta(hours=1)
        current = moment.astimezone(zone).utcoffset()
        if current != previous:
            changes.append((moment, previous, current))
            previous = current
    lines = ["BEGIN:VTIMEZONE", f"TZID:{name}"]
    if not changes:
        local = dt.datetime(1970, 1, 1)
        label = dt.datetime(year, 7, 1, tzinfo=dt.timezone.utc).astimezone(zone).tzname() or name
        lines += ["BEGIN:STANDARD", f"DTSTART:{local:%Y%m%dT%H%M%S}", f"TZOFFSETFROM:{_offset(previous)}",
                  f"TZOFFSETTO:{_offset(previous)}", f"TZNAME:{label}", "END:STANDARD"]
    else:
        smallest = min(change[2] for change in changes)
        for at, before, after in changes[:2]:
            local = (at + before).replace(tzinfo=None)
            kind = "STANDARD" if after == smallest else "DAYLIGHT"
            label = at.astimezone(zone).tzname() or name
            lines += [f"BEGIN:{kind}", f"DTSTART:{local:%Y%m%dT%H%M%S}",
                      f"RRULE:FREQ=YEARLY;BYMONTH={local.month};BYDAY={_rule_day(local.date())}",
                      f"TZOFFSETFROM:{_offset(before)}", f"TZOFFSETTO:{_offset(after)}", f"TZNAME:{label}", f"END:{kind}"]
    lines.append("END:VTIMEZONE")
    return lines


def _panel(path: str, base: str) -> str:
    root = base_url(base)
    return f"{root}{path}" if root else ""


def _task_source(task) -> str:
    if task.meeting_id and task.meeting is not None:
        day = task.meeting.meeting_date or (task.meeting.created_at.date() if task.meeting.created_at else None)
        return f"совещание «{task.meeting.title or 'без названия'}»" + (f" {day:%d.%m.%Y}" if day else "")
    quote = str(task.source_quote or "")
    if quote.startswith("Письмо"):
        return "письмо"
    if quote.startswith("Документ"):
        return "документ"
    return "поручение"


def _task_event(task, stamp: str, everyone: bool, base: str) -> list:
    done = task.status in DONE
    title = ("✓ " if done else "") + "Срок: " + (task.title or "задача")
    if everyone and (task.assignee_name or task.assignee):
        title += " — " + (task.assignee.name if task.assignee is not None else task.assignee_name)
    lines = [str(task.description or "").strip()[:1500], f"Статус: {TASK_STATUS_RU.get(task.status, task.status)}",
             f"Откуда: {_task_source(task)}"]
    if task.deadline_quote:
        lines.append(f"Срок со слов: «{task.deadline_quote[:300]}»")
    url = _panel(f"/meetings/{task.meeting_id}", base) if task.meeting_id else _panel("/progress", base)
    if url:
        lines.append(url)
    out = ["BEGIN:VEVENT", f"UID:task-{task.id}@sysai", f"DTSTAMP:{stamp}",
           f"DTSTART;VALUE=DATE:{task.deadline:%Y%m%d}", f"DTEND;VALUE=DATE:{task.deadline + dt.timedelta(days=1):%Y%m%d}",
           f"SUMMARY:{_esc(title[:300])}", f"DESCRIPTION:{_esc(chr(10).join(x for x in lines if x))}",
           "TRANSP:TRANSPARENT", "CATEGORIES:SYSAI"]
    if url:
        out.append(f"URL:{url}")
    return out + ["END:VEVENT"]


def _alert_start(alert, zone) -> dt.datetime | None:
    from . import meeting_alerts
    if alert.once_date:
        return meeting_alerts.meeting_at(alert, alert.once_date)
    days = [d for d in alert.weekdays or [] if isinstance(d, int) and 0 <= d <= 6]
    if not days:
        return None
    created = _aware(alert.created_at)
    day = created.astimezone(zone).date() if created else dt.date.today() - dt.timedelta(days=FEED_PAST_DAYS)
    for shift in range(7):
        moment = meeting_alerts.meeting_at(alert, day + dt.timedelta(days=shift))
        if moment is not None:
            return moment
    return None


def _alert_event(alert, zone, stamp: str, names: dict, base: str) -> list:
    start = _alert_start(alert, zone)
    if start is None:
        return []
    local = start.astimezone(zone)
    name = getattr(zone, "key", "") or "UTC"
    when = (f"DTSTART:{start.astimezone(dt.timezone.utc):%Y%m%dT%H%M%SZ}" if name == "UTC"
            else f"DTSTART;TZID={name}:{local:%Y%m%dT%H%M%S}")
    people = [names[int(x)] for x in alert.recipients or [] if str(x).isdigit() and int(x) in names]
    text = [str(alert.details or "").strip()[:1500]]
    if people:
        text.append("Участники: " + ", ".join(people))
    url = _panel("/reminders", base)
    if url:
        text.append(url)
    out = ["BEGIN:VEVENT", f"UID:meeting-{alert.id}@sysai", f"DTSTAMP:{stamp}", when,
           f"DURATION:PT{MEETING_MINUTES}M", f"SUMMARY:{_esc((alert.title or 'Совещание')[:300])}",
           f"DESCRIPTION:{_esc(chr(10).join(x for x in text if x))}", "CATEGORIES:SYSAI"]
    if not alert.once_date:
        days = sorted({d for d in alert.weekdays or [] if isinstance(d, int) and 0 <= d <= 6})
        out.append("RRULE:FREQ=WEEKLY;BYDAY=" + ",".join(WEEKDAYS[d] for d in days))
    return out + ["END:VEVENT"]


def feed(s, who, base: str = "") -> str:
    """The ICS text of one address: the deadlines of the tasks and the regular meetings."""
    zone = reminders.zone()
    stamp = now().astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    everyone = who == "all"
    title = "SYSAI — сроки и совещания" if everyone else f"SYSAI — {who.name}"
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//SYSAI//Calendar 1.0//RU", "CALSCALE:GREGORIAN",
             "METHOD:PUBLISH", f"X-WR-CALNAME:{_esc(title)}", f"X-WR-TIMEZONE:{getattr(zone, 'key', 'UTC')}",
             "REFRESH-INTERVAL;VALUE=DURATION:PT1H", "X-PUBLISHED-TTL:PT1H"]
    if (getattr(zone, "key", "") or "UTC") != "UTC":
        lines += _vtimezone(zone)
    today = dt.date.today()
    query = s.query(Task).filter(Task.deadline.isnot(None), Task.status != "draft",
                                 Task.deadline >= today - dt.timedelta(days=FEED_OPEN_PAST_DAYS))
    if not everyone:
        query = query.filter(Task.assignee_id == who.id)
    for task in query.order_by(Task.deadline, Task.id).limit(2000).all():
        if task.status in DONE and task.deadline < today - dt.timedelta(days=FEED_PAST_DAYS):
            continue
        lines += _task_event(task, stamp, everyone, base)
    names = {e.id: e.name for e in s.query(Employee).all()}
    for alert in s.query(MeetingAlert).filter(MeetingAlert.enabled.is_(True)).order_by(MeetingAlert.id).all():
        ids = {int(x) for x in alert.recipients or [] if str(x).isdigit()}
        if not everyone and who.id not in ids:
            continue
        if alert.once_date and alert.once_date < today - dt.timedelta(days=FEED_PAST_DAYS):
            continue
        lines += _alert_event(alert, zone, stamp, names, base)
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"


HOWTO = ("Google: «Другие календари» → «+» → «Добавить по URL». Outlook: «Добавить календарь» → «Подписаться из "
         "Интернета». Яндекс: «+» у списка календарей → «Подписка». iPhone: «Настройки» → «Календарь» → «Учётные записи» "
         "→ «Новая» → «Другое» → «Подписной календарь».")


def subscribe_text(e, url: str) -> str:
    from . import voice
    ty = ((voice.address_of(e) or {}).get("form") or "вы") == "ты"
    head = ("Сроки твоих задач и совещания теперь можно видеть в своём календаре. Ссылка для подписки — только твоя, "
            "не пересылай её:" if ty else
            "Сроки ваших задач и совещания теперь можно видеть в своём календаре. Ссылка для подписки — только ваша, "
            "не пересылайте её:")
    return f"{head}\n{url}\n\n{HOWTO}"


def send_links(base: str = "", only=None) -> tuple:
    """«Разослать ссылки»: each active employee with Telegram gets their own address. (sent, failed, skipped)"""
    from . import userbot
    sent = failed = skipped = 0
    with SessionLocal() as s:
        people = s.query(Employee).filter(Employee.active.is_(True)).order_by(Employee.id).all()
        rows = [(e, reminders.contact(e), link(e.id, base)) for e in people if only is None or e.id in only]
    for e, ref, url in rows:
        if not ref:
            skipped += 1
            continue
        text = subscribe_text(e, url)
        try:
            if userbot.ai_mode():
                result = userbot.send_message(ref, text)
                try:
                    from . import agent
                    agent.record_outgoing(ref, text, result.get("message_id"), result.get("user_id"))
                except Exception:  # noqa: BLE001
                    log.exception("calendar link is not in the conversation history")
            else:
                from . import telegram
                telegram.send_message(ref, text)
            sent += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("calendar link for employee %s not delivered: %s", e.id, exc)
            failed += 1
    return sent, failed, skipped


# ============================================================================== in: reading an ICS calendar
def _unfold(text: str) -> list:
    lines = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        elif raw.strip():
            lines.append(raw)
    return lines


def _split(line: str):
    """«NAME;PARAM=a;PARAM="b:c":value» → (NAME, {PARAM: value}, value); None when it is not a property."""
    quoted, cut = False, -1
    for index, ch in enumerate(line):
        if ch == '"':
            quoted = not quoted
        elif ch == ":" and not quoted:
            cut = index
            break
    if cut <= 0:
        return None
    head, value = line[:cut], line[cut + 1:]
    pieces, current, quoted = [], "", False
    for ch in head:
        if ch == '"':
            quoted = not quoted
            current += ch
        elif ch == ";" and not quoted:
            pieces.append(current)
            current = ""
        else:
            current += ch
    pieces.append(current)
    params = {}
    for piece in pieces[1:]:
        if "=" in piece:
            key, raw = piece.split("=", 1)
            params[key.strip().upper()] = raw.strip().strip('"')
    return pieces[0].strip().upper(), params, value


def _text(value: str) -> str:
    out, index = [], 0
    while index < len(value):
        ch = value[index]
        if ch == "\\" and index + 1 < len(value):
            nxt = value[index + 1]
            out.append("\n" if nxt in "nN" else nxt)
            index += 2
            continue
        out.append(ch)
        index += 1
    return "".join(out).strip()


def _zone(tzid: str, fixed: dict):
    tzid = str(tzid or "").strip().strip('"')
    if not tzid:
        return reminders.zone()
    for name in (tzid, WINDOWS_ZONES.get(tzid.casefold(), ""), "/".join(tzid.split("/")[-2:])):
        if not name:
            continue
        try:
            return zoneinfo.ZoneInfo(name)
        except (zoneinfo.ZoneInfoNotFoundError, ValueError):
            continue
    if tzid in fixed:
        return fixed[tzid]
    return reminders.zone()


def _when(value: str, params: dict, fixed: dict):
    """A date (all day) or an aware moment."""
    value = value.strip()
    if params.get("VALUE", "").upper() == "DATE" or re.fullmatch(r"\d{8}", value):
        return dt.date(int(value[:4]), int(value[4:6]), int(value[6:8]))
    match = re.fullmatch(r"(\d{8})T(\d{2})(\d{2})(\d{2})?(Z?)", value)
    if not match:
        raise ValueError(f"bad date {value!r}")
    day = dt.datetime.strptime(match.group(1), "%Y%m%d")
    moment = day.replace(hour=int(match.group(2)), minute=int(match.group(3)), second=int(match.group(4) or 0))
    if match.group(5):
        return moment.replace(tzinfo=dt.timezone.utc)
    return moment.replace(tzinfo=_zone(params.get("TZID", ""), fixed))


def _duration(value: str) -> dt.timedelta:
    match = re.fullmatch(r"([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", value.strip())
    if not match:
        return dt.timedelta(minutes=MEETING_MINUTES)
    weeks, days, hours, minutes, seconds = (int(x or 0) for x in match.groups()[1:])
    span = dt.timedelta(weeks=weeks, days=days, hours=hours, minutes=minutes, seconds=seconds)
    return -span if match.group(1) == "-" else span


def _person(value: str, params: dict) -> dict:
    email = value.strip()
    if email.lower().startswith("mailto:"):
        email = email[7:]
    email = email.strip().lower() if "@" in email else ""
    name = _text(params.get("CN", "")).strip().strip('"')
    if name.lower() == email:
        name = ""
    return {"name": name[:120], "email": email[:200], "status": params.get("PARTSTAT", "").upper()[:20],
            "kind": params.get("CUTYPE", "INDIVIDUAL").upper()[:20]}


def parse(text: str) -> list:
    """The meetings of an ICS file (VEVENT): their own times, recurrence and the invited."""
    fixed, events = {}, []
    current, depth, zone_id, zone_offset, in_standard = None, 0, "", None, False
    for line in _unfold(text):
        item = _split(line)
        if item is None:
            continue
        name, params, value = item
        if name == "BEGIN":
            kind = value.strip().upper()
            if kind == "VEVENT" and current is None:
                current, depth = {"props": []}, 0
            elif current is not None:
                depth += 1  # VALARM and the like inside a meeting
            elif kind == "VTIMEZONE":
                zone_id, zone_offset = "", None
            elif kind == "STANDARD":
                in_standard = True
            continue
        if name == "END":
            kind = value.strip().upper()
            if current is not None:
                if kind == "VEVENT" and depth == 0:
                    events.append(current)
                    current = None
                else:
                    depth = max(0, depth - 1)
            elif kind == "STANDARD":
                in_standard = False
            elif kind == "VTIMEZONE" and zone_id and zone_offset is not None:
                fixed[zone_id] = dt.timezone(zone_offset)
            continue
        if current is not None:
            if depth == 0:
                current["props"].append((name, params, value))
        elif name == "TZID":
            zone_id = value.strip()
        elif name == "TZOFFSETTO" and in_standard:
            match = re.fullmatch(r"([+-])(\d{2})(\d{2})", value.strip())
            if match:
                span = dt.timedelta(hours=int(match.group(2)), minutes=int(match.group(3)))
                zone_offset = -span if match.group(1) == "-" else span
    out = []
    for raw in events:
        try:
            event = _event(raw["props"], fixed)
        except (ValueError, KeyError) as exc:
            log.info("calendar: a meeting skipped (%s)", exc)
            continue
        if event is not None:
            out.append(event)
    return out


def _event(props: list, fixed: dict):
    event = {"uid": "", "title": "", "location": "", "status": "", "start": None, "end": None, "duration": None,
             "rrule": None, "exdates": [], "rdates": [], "recurrence_id": None, "attendees": [], "organizer": None}
    for name, params, value in props:
        if name == "UID":
            event["uid"] = value.strip()[:300]
        elif name == "SUMMARY":
            event["title"] = _text(value)[:300]
        elif name == "LOCATION":
            event["location"] = _text(value)[:300]
        elif name == "STATUS":
            event["status"] = value.strip().upper()
        elif name == "DTSTART":
            event["start"] = _when(value, params, fixed)
        elif name == "DTEND":
            event["end"] = _when(value, params, fixed)
        elif name == "DURATION":
            event["duration"] = _duration(value)
        elif name == "RRULE":
            event["rrule"] = {k.strip().upper(): v.strip() for k, v in
                              (part.split("=", 1) for part in value.split(";") if "=" in part)}
        elif name in ("EXDATE", "RDATE"):
            for piece in value.split(","):
                if piece.strip():
                    try:
                        event["exdates" if name == "EXDATE" else "rdates"].append(_when(piece, params, fixed))
                    except ValueError:
                        continue
        elif name == "RECURRENCE-ID":
            event["recurrence_id"] = _when(value, params, fixed)
        elif name == "ATTENDEE":
            event["attendees"].append(_person(value, params))
        elif name == "ORGANIZER":
            event["organizer"] = _person(value, params)
    if event["start"] is None:
        return None
    start = event["start"]
    if event["end"] is not None and type(event["end"]) is type(start) and event["end"] > start:
        span = event["end"] - start
    elif event["duration"] is not None and event["duration"] > dt.timedelta(0):
        span = event["duration"]
    else:
        span = dt.timedelta(days=1) if isinstance(start, dt.date) and not isinstance(start, dt.datetime) else \
            dt.timedelta(minutes=MEETING_MINUTES)
    event["span"] = span
    return event


def _day_of(value) -> dt.date:
    return value.date() if isinstance(value, dt.datetime) else value


def _same(a, b) -> bool:
    if isinstance(a, dt.datetime) and isinstance(b, dt.datetime):
        return a == b
    return _day_of(a) == _day_of(b)


def _at(day: dt.date, start):
    if isinstance(start, dt.datetime):
        return dt.datetime.combine(day, start.timetz().replace(tzinfo=None), tzinfo=start.tzinfo)
    return day


def _month_days(year: int, month: int, rule: dict, start) -> list:
    last = (dt.date(year + month // 12, month % 12 + 1, 1) - dt.timedelta(days=1)).day
    days = []
    if rule.get("BYMONTHDAY"):
        for raw in rule["BYMONTHDAY"].split(","):
            try:
                number = int(raw)
            except ValueError:
                continue
            number = last + number + 1 if number < 0 else number
            if 1 <= number <= last:
                days.append(dt.date(year, month, number))
    elif rule.get("BYDAY"):
        for raw in rule["BYDAY"].split(","):
            match = re.fullmatch(r"([+-]?\d{1,2})?(MO|TU|WE|TH|FR|SA|SU)", raw.strip().upper())
            if not match:
                continue
            weekday = WEEKDAYS.index(match.group(2))
            matching = [dt.date(year, month, d) for d in range(1, last + 1) if dt.date(year, month, d).weekday() == weekday]
            if match.group(1):
                nth = int(match.group(1))
                if 1 <= abs(nth) <= len(matching):
                    days.append(matching[nth - 1] if nth > 0 else matching[nth])
            else:
                days.extend(matching)
    elif _day_of(start).day <= last:
        days.append(dt.date(year, month, _day_of(start).day))
    return sorted(set(days))


def _candidates(event: dict):
    """The starts of a recurrent meeting in order, from its first one (RRULE: DAILY, WEEKLY, MONTHLY, YEARLY)."""
    rule, start = event["rrule"], event["start"]
    first = _day_of(start)
    freq = rule.get("FREQ", "").upper()
    try:
        interval = max(1, int(rule.get("INTERVAL") or 1))
    except ValueError:
        interval = 1
    byday = [WEEKDAYS.index(m.group(1)) for m in
             (re.fullmatch(r"[+-]?\d{0,2}(MO|TU|WE|TH|FR|SA|SU)", x.strip().upper()) for x in rule.get("BYDAY", "").split(","))
             if m]
    months = [int(x) for x in rule.get("BYMONTH", "").split(",") if x.strip().isdigit()]
    if freq == "DAILY":
        step = 0
        while True:
            day = first + dt.timedelta(days=step * interval)
            if (not byday or day.weekday() in byday) and (not months or day.month in months):
                yield day
            step += 1
    elif freq == "WEEKLY":
        days = sorted(set(byday or [first.weekday()]))
        monday = first - dt.timedelta(days=first.weekday())
        week = 0
        while True:
            for weekday in days:
                day = monday + dt.timedelta(days=week * 7 + weekday)
                if day >= first and (not months or day.month in months):
                    yield day
            week += interval
    elif freq == "MONTHLY":
        step = 0
        while True:
            index = first.month - 1 + step * interval
            year, month = first.year + index // 12, index % 12 + 1
            if not months or month in months:
                for day in _month_days(year, month, rule, start):
                    if day >= first:
                        yield day
            step += 1
    elif freq == "YEARLY":
        step = 0
        while True:
            year = first.year + step * interval
            for month in months or [first.month]:
                if rule.get("BYDAY") or rule.get("BYMONTHDAY"):
                    days = _month_days(year, month, rule, start)
                else:
                    try:
                        days = [dt.date(year, month, first.day)]
                    except ValueError:
                        days = []
                for day in days:
                    if day >= first:
                        yield day
            step += 1
    else:
        yield first


def _until(rule: dict, fixed: dict):
    raw = rule.get("UNTIL")
    if not raw:
        return None
    try:
        return _when(raw, {}, fixed)
    except ValueError:
        return None


def _past(moment, until) -> bool:
    if until is None:
        return False
    if isinstance(until, dt.datetime) and isinstance(moment, dt.datetime):
        return moment > until
    return _day_of(moment) > _day_of(until)


def occurrences(events: list, since: dt.datetime, until: dt.datetime) -> list:
    """Every meeting held between the two moments: the recurrences walked, the moved and cancelled ones applied."""
    moved, masters = {}, {}
    for event in events:
        if event["recurrence_id"] is not None:
            moved.setdefault(event["uid"], []).append(event)
        else:
            masters.setdefault(event["uid"], event)
    for uid, changed in moved.items():  # a moved meeting written without its people keeps those of the series
        master = masters.get(uid)
        for event in changed if master is not None else []:
            if not event["attendees"] and event["organizer"] is None:
                event["attendees"], event["organizer"] = master["attendees"], master["organizer"]
            event["title"] = event["title"] or master["title"]
    out = []

    def keep(event, start):
        end = start + event["span"]
        lo, hi = (start, end) if isinstance(start, dt.datetime) else (
            dt.datetime.combine(start, dt.time(), tzinfo=reminders.zone()),
            dt.datetime.combine(_day_of(end), dt.time(), tzinfo=reminders.zone()))
        if hi >= since and lo <= until:
            out.append({**{k: event[k] for k in ("uid", "title", "location", "attendees", "organizer")},
                        "start": start, "end": end, "all_day": not isinstance(start, dt.datetime)})

    for event in events:
        if event["status"] == "CANCELLED" and event["recurrence_id"] is None:
            continue
        if event["recurrence_id"] is not None:
            if event["status"] != "CANCELLED":
                keep(event, event["start"])
            continue
        exceptions = list(event["exdates"]) + [m["recurrence_id"] for m in moved.get(event["uid"], [])]
        if not event["rrule"]:
            starts = [event["start"]] + list(event["rdates"])
        else:
            rule = event["rrule"]
            limit_until = _until(rule, {})
            try:
                count = int(rule.get("COUNT") or 0)
            except ValueError:
                count = 0
            starts, made = [], 0
            for steps, day in enumerate(_candidates(event)):
                if steps > MAX_STEPS:
                    break
                start = _at(day, event["start"])
                if _past(start, limit_until):
                    break
                made += 1
                if count and made > count:
                    break
                moment = start if isinstance(start, dt.datetime) else dt.datetime.combine(start, dt.time(),
                                                                                          tzinfo=reminders.zone())
                if moment > until:
                    break
                starts.append(start)
            starts += list(event["rdates"])
        for start in starts:
            if any(_same(start, gone) for gone in exceptions):
                continue
            keep(event, start)
    out.sort(key=lambda o: o["start"] if isinstance(o["start"], dt.datetime)
             else dt.datetime.combine(o["start"], dt.time(), tzinfo=reminders.zone()))
    return out


# ----------------------------------------------------------------------------------------------- reading the address
def configured() -> bool:
    return bool(settings_store.get("calendar_ics_url"))


def address() -> str:
    from . import mail
    return mail.unseal(str(settings_store.get("calendar_ics_url") or ""))


def save_address(url: str, by: str = "") -> None:
    """The form of the page: an empty field keeps the address saved; «webcal://» is read as https."""
    from . import mail
    url = str(url or "").strip()
    if not url:
        if not configured():
            raise CalendarError("Вставьте закрытый адрес календаря в формате iCal (ICS).")
        return
    if url.lower().startswith("webcal://"):
        url = "https://" + url[9:]
    if not re.match(r"https?://\S+$", url, re.I):
        raise CalendarError("Адрес календаря должен начинаться с https:// (или webcal://).")
    settings_store.set_many({"calendar_ics_url": mail.seal(url[:2000]), "calendar_state": {"by": str(by)[:120]}})


def forget() -> None:
    """«Отключить»: the address and the meetings read from it are removed."""
    settings_store.set_many({"calendar_ics_url": "", "calendar_state": {}})
    with SessionLocal() as s:
        s.query(CalendarEvent).delete()
        s.commit()


def fetch(url: str) -> str:
    if url.lower().startswith("webcal://"):
        url = "https://" + url[9:]
    try:
        with httpx.Client(timeout=25, follow_redirects=True, headers={"User-Agent": "SYSAI calendar"}) as client:
            reply = client.get(url)
    except httpx.HTTPError as exc:
        raise CalendarError(f"Календарь не открылся: {type(exc).__name__}. Проверьте адрес и что сервис доступен.") from exc
    if reply.status_code in (401, 403, 404, 410):
        raise CalendarError(f"Календарь не открылся ({reply.status_code}): адрес неверный или его сменили — "
                            "возьмите закрытый адрес iCal заново.")
    if reply.status_code >= 400:
        raise CalendarError(f"Сервис календаря ответил ошибкой {reply.status_code}. Попробуем позже.")
    if len(reply.content) > MAX_BYTES:
        raise CalendarError("Календарь слишком большой (больше 15 МБ): подключите отдельный календарь совещаний.")
    text = reply.text
    if "BEGIN:VCALENDAR" not in text[:5000].upper():
        raise CalendarError("По адресу не календарь: нужна ссылка в формате iCal / ICS (обычно кончается на .ics).")
    return text


def sync() -> dict:
    """Read the address now and keep the meetings of the last 60 and the next 90 days."""
    url = address()
    if not url:
        raise CalendarError("Адрес календаря не задан.")
    with _LOCK:
        state = dict(settings_store.get("calendar_state") or {})
        state["checked_at"] = now().isoformat(timespec="seconds")
        try:
            events = parse(fetch(url))
        except CalendarError as exc:
            state.update(ok=False, error=str(exc)[:300])
            settings_store.set_many({"calendar_state": state})
            raise
        at = now()
        found = occurrences(events, at - dt.timedelta(days=PAST_DAYS), at + dt.timedelta(days=AHEAD_DAYS))
        found = [o for o in found if not o["all_day"]][:MAX_EVENTS]  # whole-day marks are not meetings
        with SessionLocal() as s:
            s.query(CalendarEvent).delete()
            for o in found:
                people = [p for p in o["attendees"] if p["kind"] not in ("ROOM", "RESOURCE")]
                s.add(CalendarEvent(uid=o["uid"] or "", title=o["title"] or "", location=o["location"] or "",
                                    starts_at=o["start"].astimezone(dt.timezone.utc),
                                    ends_at=o["end"].astimezone(dt.timezone.utc), organizer=o["organizer"],
                                    attendees=people[:200]))
            s.commit()
        state.update(ok=True, error="", events=len(found), with_people=sum(1 for o in found if o["attendees"]),
                     read=len(events))
        settings_store.set_many({"calendar_state": state})
        return state


def run_due() -> None:
    """Background loop: read the calendar every 15 minutes (once an hour after an error)."""
    if not configured():
        return
    state = settings_store.get("calendar_state") or {}
    last = None
    if state.get("checked_at"):
        try:
            last = _aware(dt.datetime.fromisoformat(str(state["checked_at"])))
        except ValueError:
            last = None
    wait = RETRY_MINUTES if state.get("ok") is False else POLL_MINUTES
    if last is not None and now() - last < dt.timedelta(minutes=wait):
        return
    try:
        sync()
    except CalendarError as exc:
        log.info("calendar not read: %s", exc)


def status() -> dict:
    state = dict(settings_store.get("calendar_state") or {})
    state["configured"] = configured()
    if state.get("checked_at"):
        try:
            state["checked_local"] = _aware(dt.datetime.fromisoformat(state["checked_at"])).astimezone(
                reminders.zone()).strftime("%d.%m %H:%M")
        except ValueError:
            state["checked_local"] = ""
    return state


# ------------------------------------------------------------------------------------------- the invited → people
_LATIN = {"а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z", "и": "i", "й": "y",
          "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f",
          "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu",
          "я": "ya", "і": "i", "ў": "u"}


def latin(text: str) -> str:
    out = "".join(_LATIN.get(ch, ch) for ch in str(text or "").lower())
    return out.replace("kh", "h").replace("iy", "y").replace("yy", "y")


def _plain(text: str) -> str:
    text = latin(re.sub(r"[^\w]+", " ", str(text or "").lower())).replace("ks", "x")
    return re.sub(r"(?:ei|ey|ii|iy|yi)\b", "y", text)


def _near(a: str, b: str) -> bool:
    """The same name written in Latin letters a little differently: «yuri» and «yury», «aleksei» and «alexey»."""
    if a == b:
        return True
    if min(len(a), len(b)) < 4 or abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) <= 1
    short, long_ = (a, b) if len(a) < len(b) else (b, a)
    return any(long_[:i] + long_[i + 1:] == short for i in range(len(long_)))


def _name_of(person: dict) -> str:
    if person.get("name"):
        return person["name"]
    local = (person.get("email") or "").split("@")[0]
    return re.sub(r"[._\-+]+", " ", local).strip()


def match_person(person: dict, employees: list):
    """The employee an invited person is: by the e-mail of the card, else by the name (unique only)."""
    from . import analyze
    email = (person.get("email") or "").lower()
    if email:
        hits = [e for e in employees if (e.email or "").strip().lower() == email]
        if len(hits) == 1:
            return hits[0]
    name = _name_of(person)
    if not name:
        return None
    hit = analyze.match_employee(name, employees)
    if hit is not None:
        return hit
    if re.search(r"[a-z]", name.lower()):  # written in Latin letters: compare with the names transliterated
        words = [w for w in _plain(name).split() if len(w) > 1]
        if not words:
            return None
        hits = []
        for e in employees:
            known = set(_plain(" ".join([e.name or "", (e.aliases or "").replace(",", " ")])).split())
            if all(any(_near(k, w) for k in known) for w in words):
                hits.append(e)
        return hits[0] if len(hits) == 1 else None
    return None


def people_of(s, event) -> tuple:
    """(employee ids, guest names, unknown e-mails) of a meeting of the calendar: the organizer and the invited who
    did not decline."""
    employees = s.query(Employee).filter(Employee.active.is_(True)).order_by(Employee.id).all()
    ids, guests, unknown = [], [], []
    everyone = ([event.organizer] if isinstance(event.organizer, dict) else []) + list(event.attendees or [])
    seen = set()
    for person in everyone:
        if not isinstance(person, dict) or person.get("status") == "DECLINED" or person.get("kind") in ("ROOM", "RESOURCE"):
            continue
        key = (person.get("email") or person.get("name") or "").lower()
        if not key or key in seen:
            continue
        seen.add(key)
        hit = match_person(person, employees)
        if hit is not None:
            if hit.id not in ids:
                ids.append(hit.id)
            continue
        name = person.get("name") or person.get("email") or ""
        if name:
            guests.append(name[:80])
            if person.get("email"):
                unknown.append(person["email"])
    return ids, guests, unknown


def _between(s, lo: dt.datetime, hi: dt.datetime) -> list:
    """The meetings starting between two moments (kept in UTC: SQLite keeps no zone, Postgres compares moments)."""
    lo, hi = lo.astimezone(dt.timezone.utc), hi.astimezone(dt.timezone.utc)
    rows = (s.query(CalendarEvent).filter(CalendarEvent.starts_at >= lo, CalendarEvent.starts_at < hi)
            .order_by(CalendarEvent.starts_at, CalendarEvent.id).all())
    return [r for r in rows if lo <= _aware(r.starts_at) < hi]


def events_on(s, day: dt.date) -> list:
    lo = dt.datetime.combine(day, dt.time(), tzinfo=reminders.zone())
    return _between(s, lo, lo + dt.timedelta(days=1))


def match(s, meeting):
    """The meeting of the calendar this recording is (with invited people), or None when it is not clear."""
    from . import participants
    day = meeting.meeting_date
    if day is None:
        return None
    candidates = [r for r in events_on(s, day) if r.attendees or r.organizer]
    if not candidates:
        return None
    started = participants.started_at(meeting)
    if started is not None:
        def distance(row):
            lo, hi = _aware(row.starts_at), _aware(row.ends_at) or _aware(row.starts_at)
            if lo - dt.timedelta(minutes=30) <= started <= hi:
                return abs((started - lo).total_seconds()) / 1000  # inside the meeting: before any other
            return abs((started - lo).total_seconds())
        best = min(candidates, key=distance)
        if distance(best) <= NEAR_MINUTES * 60:
            return best
        return None
    if len(candidates) == 1:
        return candidates[0]
    title = str(meeting.title or "").casefold()
    named = [r for r in candidates if (r.title or "").strip() and r.title.strip().casefold() in title]
    return named[0] if len(named) == 1 else None


def people_for(s, meeting):
    """(the meeting of the calendar, employee ids, guests) for a recording, or None: no meeting, or none of the
    invited is an employee (then the regular meeting of «Напоминания» is asked)."""
    if not configured():
        return None
    row = match(s, meeting)
    if row is None:
        return None
    ids, guests, _ = people_of(s, row)
    if not ids:
        return None
    return row, ids, guests


def upcoming(s, days: int = 7, limit: int = 40) -> list:
    """The meetings of the next days for the page, with whom the invited were taken for."""
    at = now()
    zone = reminders.zone()
    names = {e.id: e.name for e in s.query(Employee).all()}
    out = []
    for row in _between(s, at - dt.timedelta(hours=2), at + dt.timedelta(days=days)):
        start = _aware(row.starts_at)
        ids, guests, unknown = people_of(s, row)
        out.append({"title": row.title or "Встреча", "when": start.astimezone(zone).strftime("%d.%m %H:%M"),
                    "people": [names[i] for i in ids if i in names], "guests": guests, "unknown": unknown})
        if len(out) >= limit:
            break
    return out


def event_label(row) -> str:
    start = _aware(row.starts_at)
    return start.astimezone(reminders.zone()).strftime("%d.%m %H:%M") if start else ""
