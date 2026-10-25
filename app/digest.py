"""The summary of the assistant's cases for the reviewers: every day or once a week, at a chosen hour.

Built from the database only (no model, nothing invented): what was closed in the period, what needs a decision, what
is in work and who it waits for, what has not moved for a day. It goes to the reviewers (people with full rights who run
the cases); observers and the director are not disturbed. Settings: digest_frequency (off | daily | weekly),
digest_hour, digest_weekday (0 = Monday); digest_last_sent keeps the period already covered so a restart never
sends it twice.
"""
import datetime as dt
import logging
from zoneinfo import ZoneInfo

from . import cases, config, settings_store, userbot
from .db import Case, SessionLocal, now

log = logging.getLogger("sysai.digest")

FREQUENCIES = ("off", "daily", "weekly")
FREQUENCY_RU = {"off": "Не присылать", "daily": "Каждый день", "weekly": "Раз в неделю"}
WEEKDAYS_RU = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")
STALE_HOURS = 24
MAX_LINES = 8


def _tz():
    return ZoneInfo(config.TIMEZONE)


def _period_key(local: dt.datetime, frequency: str) -> str:
    if frequency == "weekly":
        year, week, _ = local.isocalendar()
        return f"{year}-W{week:02d}"
    return local.date().isoformat()


def due(moment=None) -> bool:
    """Is it time to send (the chosen hour has come and this day / week was not covered yet)?"""
    frequency = settings_store.get("digest_frequency") or "off"
    if frequency not in ("daily", "weekly"):
        return False
    local = (moment or now()).astimezone(_tz())
    if local.hour < int(settings_store.get("digest_hour") or 0):
        return False
    if frequency == "weekly" and local.weekday() != int(settings_store.get("digest_weekday") or 0):
        return False
    return settings_store.get("digest_last_sent") != _period_key(local, frequency)


def _ago(moment, at) -> str:
    hours = int((at - cases._aware(moment)).total_seconds() // 3600)
    return f"{hours // 24} дн" if hours >= 48 else f"{hours} ч" if hours >= 1 else "меньше часа"


def build(frequency: str = "daily", moment=None) -> str:
    at = moment or now()
    since = at - dt.timedelta(days=7 if frequency == "weekly" else 1)
    local = at.astimezone(_tz())
    head = (f"Сводка по делам за неделю (по {local.strftime('%d.%m')})" if frequency == "weekly"
            else f"Сводка по делам за сутки ({local.strftime('%d.%m')})")
    with SessionLocal() as s:
        closed = (s.query(Case).filter(Case.closed_at >= since, Case.status.in_(("done", "handed_over")))
                  .order_by(Case.id).all())
        failed = (s.query(Case).filter(Case.closed_at >= since, Case.status.in_(("failed", "cancelled")))
                  .order_by(Case.id).all())
        open_cases = s.query(Case).filter(Case.status.in_(cases.OPEN)).order_by(Case.id).all()
        decide = [c for c in open_cases if c.status in ("attention", "awaiting_approval", "suggested")]
        working = [c for c in open_cases if c not in decide]
        lines = [head, ""]
        if not (closed or failed or open_cases):
            return head + "\n\nЗа этот период дел не было, ничего не висит."
        lines.append(f"✅ Завершено: {len(closed)}")
        lines += [f"  • {c.title}" for c in closed[:MAX_LINES]]
        if len(closed) > MAX_LINES:
            lines.append(f"  … и ещё {len(closed) - MAX_LINES}")
        if decide:
            lines += ["", f"⚠️ Нужно ваше решение: {len(decide)}"]
            lines += [f"  • {c.title} — {c.note or cases.STATUS_RU.get(c.status, c.status)}"[:300] for c in decide[:MAX_LINES]]
        if working:
            lines += ["", f"⏳ В работе: {len(working)}"]
            for c in working[:MAX_LINES]:
                story = cases._case_story(s, c, events=1)
                wait = story["waiting_for"][0] if story["waiting_for"] else None
                what = (f"ждём {wait['expects']} от {wait['person']}" if wait
                        else cases.STATUS_RU.get(c.status, c.status))
                last = c.events[-1].created_at if c.events else c.created_at
                stale = last is not None and (at - cases._aware(last)).total_seconds() > STALE_HOURS * 3600
                lines.append(f"  • {c.title} — {what}" + (f" (без движения {_ago(last, at)})" if stale else ""))
            if len(working) > MAX_LINES:
                lines.append(f"  … и ещё {len(working) - MAX_LINES}")
        if failed:
            lines += ["", f"✖️ Не удалось или отменено: {len(failed)}"]
            lines += [f"  • {c.title}" + (f" — {c.note}" if c.note else "") for c in failed[:MAX_LINES]]
    lines += ["", "Подробно — в кабинете, раздел «Прогресс»."]
    return "\n".join(lines)[:3900]


def recipients() -> list:
    return list(settings_store.get("approver_chat_ids") or [])


def send(frequency: str | None = None, mark: bool = True) -> int:
    """Send the summary to the reviewers now. Returns how many got it."""
    frequency = frequency or settings_store.get("digest_frequency") or "daily"
    if frequency == "off":
        frequency = "daily"
    text = build(frequency)
    delivered = 0
    for ref in recipients():
        try:
            if userbot.ai_mode():
                sent = userbot.send_message(ref, text)
                try:
                    from . import agent
                    agent.record_outgoing(ref, text, sent.get("message_id"), sent.get("user_id"))
                except Exception:  # noqa: BLE001
                    log.exception("digest is not in the conversation history")
            else:
                from . import telegram
                telegram.send_message(ref, text)
            delivered += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("digest for %s not delivered: %s", ref, exc)
    if mark and delivered:
        settings_store.set_many({"digest_last_sent": _period_key(now().astimezone(_tz()), frequency)})
    return delivered


def run_due() -> None:
    """Background step: send when due. Waits for the quiet hours to end; nobody to send to — nothing happens."""
    if not due() or not recipients():
        return
    if userbot.ai_mode() and userbot.quiet_until() is not None:
        return
    send()
