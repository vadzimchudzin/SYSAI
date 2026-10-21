"""Work the AI employee does itself, and the team report built from the database.

A task with executor "assistant" (said at the meeting, or handed over by the reviewer) is done by the model after the
meeting is approved: a chart or a short report is produced, kept as an artifact, and sent to the reviewer. Every number
in a chart must come from the data (computed from the meeting or spoken in it); a chart with an unknown number is
rejected and the reviewer is told what is missing. Nothing here sends a made-up result.
"""
import collections
import concurrent.futures
import datetime as dt
import json
import logging
import pathlib
import re

from . import analyze, config, deadlines, llm, render, report_archive, settings_store, telegram, userbot, visuals, voice
from .db import Artifact, Employee, Meeting, Note, SessionLocal, Task

log = logging.getLogger("sysai.assistant")

POOL = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="assistant")
OPEN = ("draft", "sent", "awaiting_deadline", "in_progress")
MAX_EXCERPT = 14000


class AssistantError(RuntimeError):
    """The task cannot be done with the data at hand (Russian reason, goes to the reviewer)."""


def _testing() -> bool:
    return config.TESTING


# ----------------------------------------------------------------------------- scheduling
def schedule(meeting_id: int) -> None:
    """Queue the assistant's own tasks of an approved meeting."""
    with SessionLocal() as s:
        meeting = s.get(Meeting, meeting_id)
        if meeting is None or meeting.status not in report_archive.APPROVED_STATUSES:
            return
        ids = []
        for task in meeting.tasks:
            if task.executor == "assistant" and task.status in ("draft", "assistant_todo"):
                task.status = "assistant_todo"
                ids.append(task.id)
        s.commit()
    for task_id in ids:
        run_task(task_id) if _testing() else POOL.submit(run_task, task_id)


def run_pending() -> None:
    """After a restart or in the morning: unfinished tasks of the assistant continue."""
    if userbot.quiet_until() is not None:
        return
    with SessionLocal() as s:
        ids = [t.id for t in s.query(Task).filter(Task.executor == "assistant", Task.status == "assistant_todo").all()
               if t.meeting is not None and t.meeting.status in report_archive.APPROVED_STATUSES]
    for task_id in ids:
        run_task(task_id) if _testing() else POOL.submit(run_task, task_id)


def recover() -> None:
    with SessionLocal() as s:
        s.query(Task).filter(Task.status == "assistant_running").update({Task.status: "assistant_todo"})
        # A process may stop after beginning an external send. Never automatically resend an uncertain message.
        s.query(Task).filter(Task.status == "assistant_message_sending").update({Task.status: "assistant_message_uncertain"})
        s.commit()

def retry_uncertain_message(task_id: int, meeting_id: int) -> bool:
    """Explicit, reviewer-confirmed retry; ordinary recovery never repeats an uncertain external send."""
    with SessionLocal() as s:
        task = s.get(Task, task_id)
        meeting = s.get(Meeting, meeting_id)
        if (task is None or meeting is None or task.meeting_id != meeting_id or task.executor != "assistant"
                or task.status != "assistant_message_uncertain" or not task.target_employee_id
                or meeting.status not in report_archive.APPROVED_STATUSES):
            return False
        task.status = "assistant_todo"
        s.commit()
    run_task(task_id) if _testing() else POOL.submit(run_task, task_id)
    return True


# ----------------------------------------------------------------------------- facts for the model
def _seconds(stamp: str):
    match = re.fullmatch(r"(?:(\d+):)?(\d{1,2}):(\d{2})", (stamp or "").strip())
    if not match:
        return None
    hours, minutes, seconds = (int(x or 0) for x in match.groups())
    return hours * 3600 + minutes * 60 + seconds


def _excerpt(segments: list, around) -> str:
    text = analyze.transcript_text(segments)
    if len(text) <= MAX_EXCERPT:
        return text
    if around is not None:
        near = [x for x in segments if abs(float(x.get("start", 0)) - around) <= 900]
        if near:
            return analyze.transcript_text(near)[:MAX_EXCERPT]
    return text[:MAX_EXCERPT]


def computed_numbers(meeting) -> dict:
    """Figures the code itself counts, so charts of them cannot be invented."""
    per_person, priority, speakers = collections.Counter(), collections.Counter(), collections.Counter()
    for task in meeting.tasks:
        if task.executor == "assistant":
            continue
        per_person[task.assignee.name if task.assignee else (task.assignee_name or "не назначен")] += 1
        priority[{"high": "высокий", "medium": "средний", "low": "низкий"}.get(task.priority, task.priority)] += 1
    total = 0
    for segment in meeting.transcript or []:
        length = len(str(segment.get("text") or ""))
        speakers[str(segment.get("speaker") or "?")] += length
        total += length
    share = {name: round(100 * chars / total, 1) for name, chars in speakers.items()} if total else {}
    return {"tasks_per_person": dict(per_person), "tasks_by_priority": dict(priority),
            "talk_share_percent_by_speaker": share, "tasks_total": sum(per_person.values()),
            "meeting_minutes": round(max((float(x.get("end") or 0) for x in meeting.transcript or []), default=0) / 60, 1)}


def _spoken_numbers(text: str) -> set:
    return {round(float(x.replace(",", ".")), 6) for x in re.findall(r"\d+(?:[.,]\d+)?", text)}


def _allowed_numbers(computed: dict, transcript: str) -> set:
    allowed = _spoken_numbers(transcript)

    def walk(value):
        if isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            allowed.add(round(float(value), 6))
    walk(computed)
    allowed.update({0.0})
    return allowed


def _task_facts(s, task, meeting) -> dict:
    segments = meeting.transcript or []
    computed = computed_numbers(meeting)
    excerpt = _excerpt(segments, _seconds(task.source_time))
    target = s.get(Employee, task.target_employee_id) if getattr(task, "target_employee_id", None) else None
    target_info = ({"id": target.id, "name": target.name, "reachable": bool(target.telegram_chat_id)}
                   if target is not None else None)
    return {"task": {"title": task.title, "description": task.description or "", "quote": task.source_quote or "",
                     "quote_time": task.source_time or None},
            "target_employee": target_info, "meeting": voice.meeting_facts(meeting),
            "computed": computed, "transcript_excerpt": excerpt,
            "_allowed": _allowed_numbers(computed, analyze.transcript_text(segments))}


# ----------------------------------------------------------------------------- making the document
def _ask(facts: dict, feedback: str = "") -> dict:
    task = ("Ты сам выполняешь задачу, которую ассистент взял на себя на совещании: сделай то, что просили, по данным "
            "совещания. Если просят график или диаграмму, составь spec; для сводки или отчёта дай разделы. "
            "Если task требует написать сотруднику, это явно указано в target_employee: сформулируй короткое сообщение по сути поручения "
            "и верни kind=message и message_to_send. Это действие отправляет сообщение сразу: если в поручении явно указан будущий момент "
            "(например, «напомнить завтра», «написать через час»), не отправляй его сейчас — верни kind=cannot и объясни, что требуется "
            "запланированная отправка. Не используй это действие для звонков, email или иных каналов. "
            "Не отправляй сообщение, если target_employee отсутствует или reachable=false; верни kind=cannot и объясни, чего не хватает. "
            "Не утверждай, что написал человеку до фактической отправки. "
            "Числа в графике бери только из computed или из того, что названо вслух в тексте совещания, ничего не выдумывай; "
            "если данных не хватает, верни kind=cannot. В message своими словами коротко напиши проверяющему, что сделал. "
            'JSON: {"kind":"chart|report|message|cannot","title":"...","message":"...", "message_to_send":"...",'
            '"chart":{"type":"bar|hbar|pie|line","title":"...","unit":"","labels":[...],"series":[{"name":"","values":[...]}]} или null,'
            '"notes":["вывод по графику, до 4"],"sections":[{"heading":"...","text":"..."}] или [],"reason":"..."}.')
    public = {k: v for k, v in facts.items() if not k.startswith("_")}
    try:
        raw = llm.chat(voice.model(), [{"role": "system", "content": voice.system_prompt(task)},
                                       {"role": "user", "content": "<data>" + json.dumps(public, ensure_ascii=False) + "</data>" + feedback}],
                       json_mode=True, temperature=0.3, max_tokens=3000, timeout=120)
        data = llm.parse_json(raw)
    except llm.LLMError as exc:
        raise AssistantError(f"модель недоступна: {str(exc)[:200]}") from exc
    if not isinstance(data, dict) or data.get("kind") not in ("chart", "report", "message", "cannot"):
        raise AssistantError("модель вернула ответ в неверной форме")
    return data


def _validated(data: dict, facts: dict) -> dict:
    if not str(data.get("message") or "").strip():
        raise AssistantError("в ответе нет текста сообщения")
    if data["kind"] == "cannot":
        return data
    if data["kind"] == "message":
        target = facts.get("target_employee")
        message = str(data.get("message_to_send") or "").strip()
        if not target or not target.get("reachable"):
            raise AssistantError("у задачи нет однозначного адресата с привязанным Telegram")
        if not message:
            raise AssistantError("модель не подготовила текст сообщения сотруднику")
        if len(message) > 3500:
            raise AssistantError("сообщение сотруднику превышает допустимую длину")
        data["message_to_send"] = message
        return data
    if data["kind"] == "chart":
        try:
            spec = visuals.validate(data.get("chart"))
        except visuals.ChartError as exc:
            raise AssistantError(f"график не подошёл: {exc}") from exc
        unknown = sorted({round(v, 6) for v in visuals.values_of(spec)} - facts["_allowed"])
        if unknown:
            raise AssistantError(f"в графике числа, которых нет в данных совещания: {unknown[:5]}")
        data["chart"] = spec
        data["notes"] = [str(x).strip() for x in (data.get("notes") or []) if str(x).strip()][:4]
    else:
        sections = [{"heading": str(x.get("heading") or "").strip(), "text": str(x.get("text") or "").strip()}
                    for x in data.get("sections") or [] if isinstance(x, dict) and str(x.get("text") or "").strip()]
        if not sections:
            raise AssistantError("в отчёте нет разделов")
        data["sections"] = sections[:8]
    return data


def _pdf(html_text: str) -> bytes:
    from weasyprint import HTML
    return HTML(string=html_text, base_url=str(pathlib.Path(__file__).parent)).write_pdf()


def _document(meeting, data: dict, svg: str) -> bytes:
    settings = settings_store.all_settings()
    html_text = render.TPL.get_template("assistant_report.html").render(
        title=str(data.get("title") or (data.get("chart") or {}).get("title") or "Материал ассистента"),
        meeting=meeting, svg=svg, notes=data.get("notes") or [], sections=data.get("sections") or [],
        company=settings.get("company_name") or "", accent=settings.get("accent_color") or "#c98179",
        generated=dt.datetime.now(dt.timezone.utc).astimezone().strftime("%d.%m.%Y %H:%M"))
    return _pdf(html_text)


def _safe_name(text: str) -> str:
    return re.sub(r"[^\w\-]+", "_", text, flags=re.U).strip("_")[:60] or "material"


def _tell_reviewers(message: str, filename=None, pdf=None) -> int:
    delivered = 0
    for ref in settings_store.get("approver_chat_ids") or []:
        try:
            sent = userbot.send_message(ref, message)
            from . import agent
            agent.record_outgoing(ref, message, sent.get("message_id"), sent.get("user_id"))
            if pdf is not None:
                userbot.send_document(ref, filename, pdf, "")
            delivered += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("reviewer %s did not get the assistant's result: %s", ref, exc)
    return delivered


def _notify_reviewers_text(message: str) -> None:
    try:
        if userbot.ai_mode():
            _tell_reviewers(message)
            return
        for ref in settings_store.get("approver_chat_ids") or []:
            try:
                telegram.send_message(ref, render.e(message))
            except Exception as exc:  # noqa: BLE001
                log.warning("reviewer %s did not get assistant task update: %s", ref, exc)
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not notify reviewers about assistant task update: %s", exc)


def send_task_message(target_ref: str, message_text: str) -> dict:
    """Shared existing transport for legacy tasks and the opt-in task supervisor.

    The caller owns durable delivery claims; this function never retries or starts
    another Telegram consumer. Account mode keeps its existing quiet/rate guards.
    """
    if userbot.ai_mode():
        return userbot.send_message(target_ref, message_text)
    if not config.telegram_enabled() or not str(target_ref).lstrip("-").isdigit():
        raise AssistantError("Telegram bot delivery is not configured for this recipient")
    return telegram.send_message(target_ref, render.e(message_text))


def send_task_file(target_ref: str, filename: str, content: bytes, caption: str = "") -> dict:
    """Share the existing safe transport; caller validates stored-artifact ownership."""
    if userbot.ai_mode():
        return userbot.send_document(target_ref, filename, content, caption)
    if not config.telegram_enabled() or not str(target_ref).isdigit():
        raise AssistantError("Telegram bot delivery is not configured for this recipient")
    return telegram.send_document(target_ref, filename, content, render.e(caption))


def run_task(task_id: int) -> None:
    if userbot.quiet_until() is not None:
        return  # started after the quiet hours, see run_pending
    with SessionLocal() as s:
        claimed = s.query(Task).filter(Task.id == task_id, Task.executor == "assistant", Task.status == "assistant_todo").update(
            {Task.status: "assistant_running"}, synchronize_session=False)
        s.commit()
        if not claimed:
            return
        task = s.get(Task, task_id)
        meeting = s.get(Meeting, task.meeting_id)
        facts = _task_facts(s, task, meeting)
        title, meeting_id = task.title, meeting.id
    try:
        data, feedback = None, ""
        for attempt in (1, 2):
            data = _ask(facts, feedback)
            try:
                data = _validated(data, facts)
                break
            except AssistantError as exc:
                if attempt == 2:
                    raise
                feedback = f"\nПрошлый ответ не подошёл: {exc}. Исправь или верни kind=cannot."
        if data["kind"] == "cannot":
            _finish(task_id, "assistant_failed", f"Не удалось выполнить «{title}»: {data.get('reason') or 'не хватает данных'}")
            _tell_reviewers(data["message"].strip())
            return
        if data["kind"] == "message":
            with SessionLocal() as s:
                task = s.get(Task, task_id)
                target = s.get(Employee, task.target_employee_id) if task and task.target_employee_id else None
                if target is None or not target.telegram_chat_id:
                    raise AssistantError("у адресата нет привязанного Telegram")
                target_ref, target_name = target.telegram_chat_id, target.name
            message_text = data["message_to_send"]
            if userbot.ai_mode():
                if not userbot.ready():
                    raise AssistantError("Telegram-аккаунт ИИ-сотрудника не подключён")
            else:
                if not config.telegram_enabled():
                    raise AssistantError("Telegram не подключён, сообщение не отправлено")
                if not str(target_ref).lstrip("-").isdigit():
                    raise AssistantError("В режиме обычного бота для адресата нужен числовой Telegram chat ID")
            # Persist the sending state before the external side effect. If the process crashes now,
            # recovery marks it uncertain instead of sending the same message twice.
            with SessionLocal() as s:
                task = s.get(Task, task_id)
                task.status = "assistant_message_sending"
                s.commit()
            try:
                if userbot.ai_mode():
                    sent = userbot.send_message(target_ref, message_text)
                else:
                    sent = telegram.send_message(target_ref, render.e(message_text))
            except Exception as exc:  # noqa: BLE001
                reason = f"Не удалось подтвердить доставку сообщения {target_name}: {str(exc)[:220]}. Проверьте чат перед повтором."
                _finish(task_id, "assistant_message_uncertain", reason)
                _notify_reviewers_text(reason)
                return
            if not isinstance(sent, dict) or not sent.get("message_id"):
                reason = f"Статус доставки сообщения {target_name} неизвестен. Проверьте чат перед повтором."
                _finish(task_id, "assistant_message_uncertain", reason)
                _notify_reviewers_text(reason)
                return
            _finish(task_id, "assistant_done", f"Ассистент написал {target_name} по поручению «{title}»")
            try:
                from . import agent
                sent_user_id = sent.get("user_id") or (sent.get("chat") or {}).get("id")
                agent.record_outgoing(target_ref, message_text, sent.get("message_id"), sent_user_id)
            except Exception:  # noqa: BLE001
                log.exception("message was sent but could not be added to conversation history for task %s", task_id)
            _notify_reviewers_text(f"Сообщение отправлено {target_name} по поручению «{title}».")
            return
        svg = visuals.svg(data["chart"]) if data["kind"] == "chart" else ""
        with SessionLocal() as s:
            meeting = s.get(Meeting, meeting_id)
            pdf = _document(meeting, data, svg)
            filename = f"{_safe_name(str(data.get('title') or title))}.pdf"
            artifact = Artifact(meeting_id=meeting_id, task_id=task_id, title=str(data.get("title") or title)[:300],
                                filename=filename, mime="application/pdf", data=pdf, svg=svg)
            s.add(artifact)
            s.commit()
        reviewers = settings_store.get("approver_chat_ids") or []
        if reviewers and not _tell_reviewers(data["message"].strip(), filename, pdf):
            raise AssistantError("результат готов, но не дошёл ни до одного проверяющего")
        _finish(task_id, "assistant_done", f"Ассистент выполнил «{title}»: {data.get('title') or data['kind']}")
    except Exception as exc:  # noqa: BLE001
        log.warning("assistant task %s failed: %s", task_id, exc)
        _finish(task_id, "assistant_failed", f"Не удалось выполнить «{title}»: {str(exc)[:300]}")
        try:  # the reviewer must not be left wondering: the failure is told in the assistant's own words
            _tell_reviewers(voice.for_assistant_failure(title, str(exc)[:300]))
        except Exception:  # noqa: BLE001
            log.exception("could not tell the reviewer about the failed task")


def _finish(task_id: int, status: str, note: str) -> None:
    with SessionLocal() as s:
        task = s.get(Task, task_id)
        task.status = status
        s.add(Note(task_id=task_id, meeting_id=task.meeting_id, kind="done" if status == "assistant_done" else "status",
                   text=note[:800], source="system", author="ассистент"))
        s.commit()


# ----------------------------------------------------------------------------- who does what
def team_report(scope="all") -> tuple:
    """A PDF with the state of every person's tasks, straight from the database. Returns (filename, bytes)."""
    today = deadlines.today()
    with SessionLocal() as s:
        query = s.query(Employee).filter_by(active=True).order_by(Employee.name)
        people = [p for p in query.all() if scope == "all" or p.id == scope]
        rows, totals = [], collections.Counter()
        for person in people:
            tasks = s.query(Task).filter(Task.assignee_id == person.id, Task.executor != "assistant").order_by(Task.id.desc()).limit(60).all()
            open_tasks = [t for t in tasks if t.status in OPEN]
            done = [t for t in tasks if t.status == "done"]
            blocked = [t for t in tasks if t.status == "blocked"]
            overdue = [t for t in open_tasks + blocked if t.deadline and t.deadline < today]
            notes = s.query(Note).filter(Note.employee_id == person.id, Note.kind.in_(("done", "status", "commitment", "fact"))) \
                .order_by(Note.id.desc()).limit(5).all()
            rows.append({"name": person.name, "position": person.position or "",
                         "open": [{"title": t.title, "status": t.status, "deadline": deadlines.fmt(t.deadline),
                                   "overdue": bool(t.deadline and t.deadline < today), "meeting": t.meeting.title if t.meeting else ""}
                                  for t in open_tasks + blocked],
                         "done": [t.title for t in done][:8], "counts": (len(open_tasks), len(blocked), len(done), len(overdue)),
                         "notes": [n.text for n in reversed(notes)]})
            totals.update(open=len(open_tasks), blocked=len(blocked), done=len(done), overdue=len(overdue))
        assistant_tasks = [{"title": t.title, "status": t.status} for t in
                           s.query(Task).filter(Task.executor == "assistant").order_by(Task.id.desc()).limit(10).all()]
    charts = []
    active = [r for r in rows if sum(r["counts"][:3])]
    if active:
        charts.append(visuals.svg({"type": "hbar" if len(active) > 6 else "bar", "title": "Задачи по сотрудникам", "unit": "",
                                   "labels": [r["name"] for r in active],
                                   "series": [{"name": "в работе", "values": [r["counts"][0] for r in active]},
                                              {"name": "заблокировано", "values": [r["counts"][1] for r in active]},
                                              {"name": "выполнено", "values": [r["counts"][2] for r in active]}]}))
    if totals["open"] + totals["blocked"] + totals["done"]:
        charts.append(visuals.svg({"type": "pie", "title": "Состояние задач", "unit": "",
                                   "labels": ["в работе", "заблокировано", "выполнено"],
                                   "series": [{"name": "", "values": [totals["open"], totals["blocked"], totals["done"]]}]}))
    settings = settings_store.all_settings()
    html_text = render.TPL.get_template("team_report.html").render(
        rows=rows, totals=totals, charts=charts, assistant_tasks=assistant_tasks, today=deadlines.fmt(today),
        company=settings.get("company_name") or "", accent=settings.get("accent_color") or "#c98179",
        only=(rows[0]["name"] if scope != "all" and rows else ""))
    return f"Кто_что_делает_{today.isoformat()}.pdf", _pdf(html_text)


def send_team_report(chat, scope="all") -> None:
    filename, pdf = team_report(scope)
    sent = userbot.send_document(chat, filename, pdf, "")
    from . import agent
    agent.record_outgoing(str(chat), f"[отправил PDF: {filename}]", sent.get("message_id"), sent.get("user_id") or chat)
