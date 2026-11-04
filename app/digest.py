"""The morning brief (every day) and the summary of the assistant's cases (once a week), at a chosen time.

Built from the database only (no model, nothing invented). The morning brief (№21) is one screen by importance: what
is burning (overdue tasks, due today or tomorrow, blocked), who is silent (the assistant waits for an answer longer than
the silence rule of that person, a deadline nobody named), what waits for a decision (protocols to approve, cases
stopped or waiting for approval, questions of the employees, tasks without a recipient); every line links to its page
in the panel when the service knows its address (PUBLIC_URL). The weekly summary: what was closed, what needs a
decision, what is in work and who it waits for, what has not moved for a day.

Who gets it: the people chosen in the settings (digest_recipients, employee ids with Telegram), otherwise the reviewers.
Settings: digest_frequency (off | daily | weekly), digest_hour and digest_minute, digest_weekday (0 = Monday);
digest_last_sent keeps the period already covered so a restart never sends it twice. On the days off and at night
(the quiet time) it waits for the next working morning.
"""
import datetime as dt
import html
import logging
from zoneinfo import ZoneInfo

from . import cases, config, settings_store, userbot
from .db import (Case, CaseWait, ConversationEscalation, DeadlineRequest, Employee, Meeting, Note, SessionLocal, Task,
                 now)

log = logging.getLogger("sysai.digest")

FREQUENCIES = ("off", "daily", "weekly")
FREQUENCY_RU = {"off": "Не присылать", "daily": "Каждое утро — что горит и кто молчит",
                "weekly": "Раз в неделю — сводка по делам"}
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
    if (local.hour, local.minute) < (int(settings_store.get("digest_hour") or 0),
                                     int(settings_store.get("digest_minute") or 0)):
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


# ----------------------------------------------------------------------------- the morning brief (№21)
BRIEF_LINES = 5        # a section shows at most this many lines (the rest: «… и ещё N»)
DEADLINE_SILENCE_HOURS = 24
LIVE_TASKS = ("sent", "in_progress", "blocked", "awaiting_deadline")


def _plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    return few if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else many


def _span(hours: float) -> str:
    hours = int(hours)
    if hours >= 48:
        days = hours // 24
        return f"{days} {_plural(days, 'день', 'дня', 'дней')}"
    return f"{hours} {_plural(hours, 'час', 'часа', 'часов')}" if hours >= 1 else "меньше часа"


def _days(n: int) -> str:
    return f"{n} {_plural(n, 'день', 'дня', 'дней')}"


def _task_page(s, task) -> str:
    if task.meeting_id:
        return f"/meetings/{task.meeting_id}#task-{task.id}"
    case = s.query(Case.id).filter(Case.task_id == task.id).order_by(Case.id.desc()).first()
    return f"/progress#case-{case[0]}" if case else "/progress"


def _who(task) -> str:
    return (task.assignee.name if task.assignee is not None else task.assignee_name) or "без исполнителя"


def _burning(s, today: dt.date) -> list:
    """Overdue, due today or tomorrow, blocked: (order, text, page), the most urgent first."""
    out = []
    tasks = (s.query(Task).filter(Task.status.in_(LIVE_TASKS), Task.executor != "assistant")
             .order_by(Task.deadline, Task.id).limit(500).all())
    for t in tasks:
        late = (today - t.deadline).days if t.deadline else None
        if t.status == "blocked":
            why = (s.query(Note.text).filter(Note.task_id == t.id, Note.archived.is_(False))
                   .order_by(Note.id.desc()).first())
            state = "мешает" + (f": {why[0][:120]}" if why else "")
            if late is not None and late > 0:
                state += f" (просрочено на {_days(late)})"
            order = (0, -(late or 0))
        elif late is None or late < -1:
            continue
        elif late > 0:
            state, order = f"просрочено на {_days(late)} (срок {t.deadline.strftime('%d.%m')})", (1, -late)
        else:
            state, order = ("срок сегодня" if late == 0 else "срок завтра"), (2, -late)
        out.append((order, f"{_who(t)} — {t.title[:140]}: {state}", _task_page(s, t)))
    out.sort(key=lambda x: x[0])
    return out


def _silent(s, at) -> list:
    """The assistant waits for these people longer than their silence rule (or a deadline nobody named): (hours, text,
    page), the longest first."""
    out = []
    for wait in s.query(CaseWait).filter(CaseWait.status == "open").order_by(CaseWait.id).limit(300):
        case = s.get(Case, wait.case_id)
        if case is None or case.status not in ("waiting", "attention"):
            continue
        person = s.get(Employee, wait.employee_id) if wait.employee_id else None
        hours = (at - cases._aware(wait.started_at)).total_seconds() / 3600 if wait.started_at else 0
        rule = cases.silence_rule(s, person)
        if not (wait.escalated or (wait.expects != "done" and hours >= float(rule.get("hours") or 0) > 0)):
            continue
        what = ("не отписался о выполнении, ждём" if wait.expects == "done"
                else f"нет {cases.WAIT_RU.get(wait.expects, 'ответа')}")
        extra = f", напоминаний: {wait.reminders_sent}" if wait.reminders_sent else ""
        out.append((hours, f"{person.name if person else wait.chat_ref} — {what} {_span(hours)}: «{case.title[:120]}»{extra}",
                    f"/progress#case-{case.id}"))
    since = at - dt.timedelta(hours=DEADLINE_SILENCE_HOURS)
    for ask in (s.query(DeadlineRequest).filter(DeadlineRequest.resolved.is_(False), DeadlineRequest.asked_at <= since)
                .order_by(DeadlineRequest.id).limit(100)):
        t = ask.task
        if t is None or t.status in ("done", "assistant_done") or t.deadline is not None:
            continue
        hours = (at - cases._aware(ask.asked_at)).total_seconds() / 3600
        out.append((hours, f"{_who(t)} — не назвал срок, спросили {_span(hours)} назад: «{t.title[:120]}»",
                    _task_page(s, t)))
    out.sort(key=lambda x: -x[0])
    return out


def _decisions(s) -> list:
    """What waits for a person who decides: (text, page)."""
    out = []
    for m in s.query(Meeting).filter_by(status="awaiting_approval").order_by(Meeting.id).limit(20):
        n = len(m.tasks)
        out.append((f"Протокол «{(m.title or 'без названия')[:120]}» ждёт утверждения"
                    + (f" ({n} {_plural(n, 'задача', 'задачи', 'задач')})" if n else ""), f"/meetings/{m.id}"))
    for c in (s.query(Case).filter(Case.status.in_(("attention", "awaiting_approval", "suggested")))
              .order_by(Case.id).limit(30)):
        out.append((f"{c.title[:140]} — {(c.note or cases.STATUS_RU.get(c.status, c.status))[:160]}", f"/progress#case-{c.id}"))
    for q in s.query(ConversationEscalation).filter_by(status="open").order_by(ConversationEscalation.id).limit(20):
        e = s.query(Employee).filter_by(telegram_user_id=q.chat_id).first() or s.query(Employee).filter_by(
            telegram_chat_id=q.chat_id).first()
        out.append((f"{e.name if e else q.chat_id} спрашивает: «{q.reason[:160]}» — ответьте мне в чате, передам",
                    f"/meetings/{q.meeting_id}"))
    lost = s.query(Task).filter(Task.status == "no_recipient").order_by(Task.id.desc()).all()
    if lost:
        out.append((f"Задачи без получателя: {len(lost)} — назначьте исполнителя или укажите его Telegram",
                    f"/meetings/{lost[0].meeting_id}" if lost[0].meeting_id else "/employees"))
    failed = s.query(Task).filter(Task.status == "assistant_failed").order_by(Task.id.desc()).limit(10).all()
    for t in failed[:3]:
        out.append((f"Ассистент не смог: {t.title[:140]}", _task_page(s, t)))
    from . import mail_route  # letters nobody was found for (№24)
    letters = mail_route.waiting(s)
    for x in letters[:3]:
        hint = f" (подсказка: {x['hint']})" if x["hint"] else ""
        out.append((f"Письмо «{x['subject'][:120]}» от {x['from'][:60]} — кому передать?{hint}", f"/mail/{x['id']}"))
    if len(letters) > 3:
        out.append((f"Ещё писем ждут решения, кому передать: {len(letters) - 3}", "/mail?only=unrouted"))
    return out


def _line(text: str, page: str | None, as_html: bool) -> str:
    if not as_html:
        return f"• {text}"
    url = f"{config.PUBLIC_URL}{page}" if config.PUBLIC_URL and page else ""
    return f'• <a href="{html.escape(url, quote=True)}">{html.escape(text)}</a>' if url else f"• {html.escape(text)}"


def brief(moment=None) -> dict:
    """The morning brief: {"text": plain, "html": with the links of the panel, "count": lines that need attention}."""
    at = moment or now()
    local = at.astimezone(_tz())
    today = local.date()
    with SessionLocal() as s:
        burning = [(t, p) for _, t, p in _burning(s, today)]
        silent = [(t, p) for _, t, p in _silent(s, at)]
        decide = _decisions(s)
        closed = s.query(Case).filter(Case.closed_at >= at - dt.timedelta(days=1),
                                      Case.status.in_(("done", "handed_over"))).count()
        working = s.query(Case).filter(Case.status.in_(cases.OPEN)).count()
    head = f"Утренняя сводка · {WEEKDAYS_RU[local.weekday()][:2]} {local.strftime('%d.%m')}"
    sections = (("🔥 Горит", burning, "/progress"), ("🤐 Молчат", silent, "/progress"),
                ("⚠️ Ждёт решения", decide, "/progress"))

    def render(as_html: bool) -> str:
        lines = [html.escape(head) if as_html else head]
        for title, rows, more in sections:
            if not rows:
                continue
            lines += ["", f"{title} ({len(rows)})"]
            lines += [_line(text, page, as_html) for text, page in rows[:BRIEF_LINES]]
            if len(rows) > BRIEF_LINES:
                lines.append(_line(f"… и ещё {len(rows) - BRIEF_LINES}", more, as_html))
        if not (burning or silent or decide):
            lines += ["", "Ничего не горит, никто не молчит, решений не ждёт."]
        tail = f"За сутки закрыто дел: {closed}. В работе: {working}."
        lines += ["", html.escape(tail) if as_html else tail]
        if as_html and config.PUBLIC_URL:
            lines.append(f'<a href="{html.escape(config.PUBLIC_URL)}/progress">Все дела — в кабинете</a>')
        return "\n".join(lines)[:3900]
    return {"text": render(False), "html": render(True), "count": len(burning) + len(silent) + len(decide)}


def recipients() -> list:
    """The people chosen in the settings (with Telegram), otherwise the reviewers."""
    chosen = [int(x) for x in settings_store.get("digest_recipients") or [] if str(x).isdigit()]
    if chosen:
        with SessionLocal() as s:
            refs = [e.telegram_chat_id for e in s.query(Employee).filter(Employee.id.in_(chosen), Employee.active.is_(True))
                    .order_by(Employee.name) if (e.telegram_chat_id or "").strip()]
        if refs:
            return refs
    return list(settings_store.get("approver_chat_ids") or [])


def send(frequency: str | None = None, mark: bool = True) -> int:
    """Send the morning brief (daily) or the weekly summary now. Returns how many got it."""
    frequency = frequency or settings_store.get("digest_frequency") or "daily"
    if frequency == "off":
        frequency = "daily"
    if frequency == "weekly":
        text = build(frequency)
        marked = html.escape(text)
    else:
        made = brief()
        text, marked = made["text"], made["html"]
    links = bool(config.PUBLIC_URL) and marked != html.escape(text)
    delivered = 0
    for ref in recipients():
        try:
            if userbot.ai_mode():
                # the hour and the day the director chose; with the links of the panel when there are any
                sent = (userbot.send_message(ref, marked, urgent=True, html=True) if links
                        else userbot.send_message(ref, text, urgent=True))
                try:
                    from . import agent
                    agent.record_outgoing(ref, text, sent.get("message_id"), sent.get("user_id"))
                except Exception:  # noqa: BLE001
                    log.exception("digest is not in the conversation history")
            else:
                from . import telegram
                telegram.send_message(ref, marked)  # the bot speaks HTML
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
