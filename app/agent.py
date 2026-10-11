"""The AI employee's side of every conversation.

People write to the Telegram account; the model answers in its own words and may ask for a few actions. Code decides
whether an action is allowed (who the sender is, which task or meeting it concerns) and applies it. Only directory
employees and reviewers get answers: the account may also be a person's private one, everyone else is ignored.
A reply is never a fixed phrase. When the model is unavailable the message waits and is retried; nobody receives a
made-up answer.
"""
import collections
import concurrent.futures
import datetime as dt
import json
import logging
import threading

from sqlalchemy.exc import IntegrityError

from . import bitrix, deadlines, llm, settings_store, userbot, voice
from .db import ConversationEscalation, ConversationMessage, Employee, Meeting, SessionLocal, Task, now

log = logging.getLogger("sysai.agent")

HISTORY = 20
MAX_ATTEMPTS = 3
RETRY_AFTER = 20  # seconds before a waiting message is picked up again
POOL = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="agent")
_locks = collections.defaultdict(threading.Lock)


class AgentError(RuntimeError):
    """The model's decision cannot be carried out; the message is retried or given up, never answered."""


# ----------------------------------------------------------------------------- who is writing
def _is_reviewer(msg: dict) -> bool:
    return any(userbot.same_person(ref, msg) for ref in settings_store.get("approver_chat_ids") or [])


def _find_employee(s, msg: dict):
    for employee in s.query(Employee).filter_by(active=True).all():
        if employee.telegram_user_id and employee.telegram_user_id == str(msg["user_id"]):
            return employee
        if userbot.same_person(employee.telegram_chat_id, msg):
            if not employee.telegram_user_id:
                employee.telegram_user_id = str(msg["user_id"])
                s.commit()
            return employee
    return None


# ----------------------------------------------------------------------------- intake
def handle_incoming(msg: dict) -> None:
    """Called by the Telegram account for each private text message. Safe to call twice with the same message."""
    if not userbot.ai_mode():
        return
    try:
        row_id = _accept(msg)
    except Exception:  # noqa: BLE001
        log.exception("incoming message dropped")
        return
    if row_id is None:
        return
    if _testing():
        process(row_id)
    else:
        POOL.submit(process, row_id)


def _testing() -> bool:
    from . import config
    return config.TESTING


def _accept(msg: dict):
    chat = str(msg["user_id"])
    with SessionLocal() as s:
        reviewer = _is_reviewer(msg)
        employee = _find_employee(s, msg)
        status = "agent_pending" if (reviewer or employee) else "agent_ignored"
        row = ConversationMessage(chat_id=chat, message_id=msg["message_id"], role="user", status=status,
                                  text=str(msg["text"]).strip()[:4000],
                                  result={"attempts": 0, "name": msg.get("name", ""), "reviewer": reviewer,
                                          "employee_id": employee.id if employee else None})
        s.add(row)
        try:
            s.commit()
        except IntegrityError:
            s.rollback()
            return None  # already seen (the unread scan hands messages over again)
        return row.id if status == "agent_pending" else None


def run_pending() -> None:
    """Retries messages that could not be answered yet (model unavailable, Telegram hiccup)."""
    if not userbot.ai_mode():
        return
    limit = now() - dt.timedelta(seconds=RETRY_AFTER)
    with SessionLocal() as s:
        ids = [r.id for r in s.query(ConversationMessage).filter(
            ConversationMessage.role == "user", ConversationMessage.status == "agent_pending",
            ConversationMessage.created_at < limit).order_by(ConversationMessage.id).all()]
    for row_id in ids:
        POOL.submit(process, row_id) if not _testing() else process(row_id)


def process(row_id: int) -> None:
    with SessionLocal() as s:
        row = s.get(ConversationMessage, row_id)
        if row is None or row.status != "agent_pending":
            return
        chat = row.chat_id
    with _locks[chat]:
        try:
            _process(row_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("message %s not answered yet: %s", row_id, exc)
            with SessionLocal() as s:
                row = s.get(ConversationMessage, row_id)
                if row is not None and row.status == "agent_pending":
                    result = dict(row.result or {})
                    result["attempts"] = int(result.get("attempts", 0)) + 1
                    row.result, row.error = result, str(exc)[:500]
                    if result["attempts"] >= MAX_ATTEMPTS:
                        row.status = "agent_failed"
                    s.commit()


# ----------------------------------------------------------------------------- context for the model
def _task_view(task) -> dict:
    return {"id": task.id, "title": task.title, "description": (task.description or "")[:500],
            "deadline": task.deadline.isoformat() if task.deadline else None,
            "can_set_deadline": task.deadline is None, "priority": task.priority,
            "quote": (task.source_quote or "")[:250], "meeting": task.meeting.title if task.meeting else ""}


def _context(s, row, msg_like: dict, employee, reviewer: bool) -> dict:
    chat = row.chat_id
    history = (s.query(ConversationMessage).filter(
        ConversationMessage.chat_id == chat, ConversationMessage.role.in_(("user", "assistant")),
        ConversationMessage.status.in_(("agent_pending", "agent_done")), ConversationMessage.id < row.id)
        .order_by(ConversationMessage.id.desc()).limit(HISTORY).all())
    history.reverse()
    ctx = {"today": deadlines.today().isoformat(), "sender_name": msg_like.get("name") or None,
           "roles": [r for r, on in (("employee", employee is not None), ("reviewer", reviewer)) if on],
           "history": [{"from": "человек" if h.role == "user" else "ты", "text": h.text[:800]} for h in history]}
    if employee is not None:
        tasks = (s.query(Task).filter(Task.assignee_id == employee.id, Task.status.in_(("sent", "awaiting_deadline")))
                 .order_by(Task.id.desc()).limit(30).all())
        ctx["employee"] = {"name": employee.name, "position": employee.position or None,
                           "tasks": [_task_view(t) for t in tasks]}
        waiting = s.query(ConversationEscalation).filter_by(chat_id=chat, status="open").all()
        ctx["waiting_for_reviewer"] = [{"id": e.id, "question": e.reason} for e in waiting]
    if reviewer:
        meetings = s.query(Meeting).filter_by(status="awaiting_approval").order_by(Meeting.id.desc()).limit(5).all()
        ctx["meetings_awaiting_approval"] = [
            {"id": m.id, "title": m.title or "", "tasks": len(m.tasks), "summary": str((m.report or {}).get("summary") or "")[:400]}
            for m in meetings]
        open_questions = s.query(ConversationEscalation).filter_by(status="open").order_by(ConversationEscalation.id).limit(10).all()
        ctx["open_questions_from_employees"] = [
            {"id": e.id, "employee": _name_for_chat(s, e.chat_id), "question": e.reason,
             "task": s.get(Task, e.task_id).title if e.task_id and s.get(Task, e.task_id) else None} for e in open_questions]
    return ctx


def _name_for_chat(s, chat: str) -> str:
    employee = s.query(Employee).filter_by(telegram_user_id=chat).first()
    return employee.name if employee else chat


ACTIONS_HELP = (
    'Формат ответа: {"reply": "что написать человеку или пустая строка", "actions": [...]}. Доступные действия: '
    '{"type":"set_deadline","task_id":ID,"date":"ГГГГ-ММ-ДД"} (сотрудник назвал конкретную дату, а у задачи ещё нет срока); '
    '{"type":"ask_reviewer","task_id":ID или null,"question":"суть вопроса","message":"точный текст, который ты напишешь проверяющему"} '
    "(сотруднику нужен ответ, которого нет в данных); "
    '{"type":"relay_answer","question_id":ID,"message":"точный текст, который ты напишешь сотруднику с ответом проверяющего"} '
    "(проверяющий ответил на вопрос сотрудника); "
    '{"type":"approve_meeting","meeting_id":ID} и {"type":"reject_meeting","meeting_id":ID} (проверяющий прямо согласился '
    "отправить именно это совещание или отклонил его; если совещаний несколько и неясно какое, спроси). "
    "Используй действие только когда намерение человека ясно. Действия проверяются кодом: ID бери только из данных.")


def _system(ctx: dict) -> str:
    task = (
        "Тебе пишет человек в личные сообщения. Ответь как внимательный коллега: коротко, по делу, своими словами. "
        "На короткие реплики вроде «ок», «понял», «спасибо» ответ может быть совсем коротким, а если отвечать не нужно, "
        "оставь reply пустым. Не пиши «не понял», если смысл можно угадать по истории; если неясно, задай один "
        "конкретный вопрос. Не выдумывай статусы, сроки и обещания. Если сотрудник спрашивает то, чего нет в данных, "
        "используй ask_reviewer и скажи ему, что уточняешь. Если ты уже ждёшь ответа проверяющего (waiting_for_reviewer), "
        "честно скажи, что ждёшь его ответа и вернёшься, когда он ответит. Не раскрывай задачи и данные других людей. "
        + ACTIONS_HELP)
    return voice.system_prompt(task)


def _decide(system: str, ctx: dict, text: str, feedback: str = "") -> dict:
    user = ("<data>" + json.dumps(ctx, ensure_ascii=False) + "</data>\nСообщение человека:\n<data>" + text + "</data>" + feedback)
    try:
        raw = llm.chat(voice.model(), [{"role": "system", "content": system}, {"role": "user", "content": user}],
                       json_mode=True, temperature=0.6, max_tokens=1200, timeout=90)
        data = llm.parse_json(raw)
    except llm.LLMError as exc:
        raise AgentError(f"модель недоступна: {str(exc)[:200]}") from exc
    if not isinstance(data, dict):
        raise AgentError("модель вернула не объект")
    reply = data.get("reply")
    actions = data.get("actions") or []
    if (reply is not None and not isinstance(reply, str)) or not isinstance(actions, list):
        raise AgentError("модель вернула ответ в неверной форме")
    return {"reply": (reply or "").strip(), "actions": [a for a in actions if isinstance(a, dict)]}


# ----------------------------------------------------------------------------- checking actions
def _check(s, action: dict, ctx: dict, employee, reviewer: bool) -> str:
    """Returns an error text or "" when the action may run."""
    kind = action.get("type")
    if kind == "set_deadline":
        if employee is None:
            return "set_deadline доступно только сотруднику"
        ids = {t["id"] for t in ctx["employee"]["tasks"] if t["can_set_deadline"]}
        if action.get("task_id") not in ids:
            return "set_deadline: задача не найдена среди задач без срока"
        try:
            date = dt.date.fromisoformat(str(action.get("date")))
        except ValueError:
            return "set_deadline: дата должна быть в формате ГГГГ-ММ-ДД"
        if date < deadlines.today():
            return "set_deadline: дата уже прошла"
        return ""
    if kind == "ask_reviewer":
        if employee is None:
            return "ask_reviewer доступно только сотруднику"
        if not (settings_store.get("approver_chat_ids") or []):
            return "ask_reviewer: проверяющие не указаны в настройках"
        if not str(action.get("message") or "").strip() or not str(action.get("question") or "").strip():
            return "ask_reviewer: нужны question и message"
        task_ids = {t["id"] for t in ctx["employee"]["tasks"]}
        if action.get("task_id") is not None and action.get("task_id") not in task_ids:
            return "ask_reviewer: задача не найдена среди задач сотрудника"
        if not task_ids:
            return "ask_reviewer: у сотрудника нет задач, вопрос не к чему привязать"
        return ""
    if kind == "relay_answer":
        if not reviewer:
            return "relay_answer доступно только проверяющему"
        ids = {q["id"] for q in ctx.get("open_questions_from_employees", [])}
        if action.get("question_id") not in ids:
            return "relay_answer: вопрос не найден среди открытых"
        return "" if str(action.get("message") or "").strip() else "relay_answer: нужен message"
    if kind in ("approve_meeting", "reject_meeting"):
        if not reviewer:
            return f"{kind} доступно только проверяющему"
        ids = {m["id"] for m in ctx.get("meetings_awaiting_approval", [])}
        return "" if action.get("meeting_id") in ids else f"{kind}: совещание не ждёт проверки"
    return f"неизвестное действие {kind!r}"


def _apply(s, row, action: dict, employee) -> None:
    from . import pipeline
    kind = action["type"]
    if kind == "set_deadline":
        task = s.get(Task, action["task_id"])
        task.deadline, task.deadline_source = dt.date.fromisoformat(action["date"]), "asked"
        if task.status == "awaiting_deadline":
            task.status = "sent"
        if task.bitrix_task_id:
            try:
                bitrix.update_deadline(task)
            except Exception as exc:  # noqa: BLE001
                log.warning("bitrix update failed: %s", exc)
        s.commit()
    elif kind == "ask_reviewer":
        task = s.get(Task, action["task_id"]) if action.get("task_id") is not None else None
        meeting_id = task.meeting_id if task else (
            s.query(Task).filter(Task.assignee_id == employee.id).order_by(Task.id.desc()).first().meeting_id)
        s.add(ConversationEscalation(meeting_id=meeting_id, task_id=task.id if task else None, chat_id=row.chat_id,
                                     reason=str(action["question"]).strip()[:1000], status="open"))
        s.commit()
        delivered = 0
        for ref in settings_store.get("approver_chat_ids") or []:
            try:
                userbot.send_message(ref, str(action["message"]).strip())
                delivered += 1
            except Exception as exc:  # noqa: BLE001
                log.warning("question for reviewer %s not delivered: %s", ref, exc)
        if not delivered:
            raise AgentError("вопрос не дошёл ни до одного проверяющего")
    elif kind == "relay_answer":
        question = s.get(ConversationEscalation, action["question_id"])
        employee_row = s.query(Employee).filter_by(telegram_user_id=question.chat_id).first()
        target = (employee_row.telegram_chat_id if employee_row and employee_row.telegram_chat_id else question.chat_id)
        sent = userbot.send_message(target, str(action["message"]).strip())
        s.add(ConversationMessage(chat_id=question.chat_id, message_id=sent["message_id"], role="assistant",
                                  status="agent_done", text=str(action["message"]).strip()[:4000]))
        question.status = "resolved"
        s.commit()
    elif kind == "approve_meeting":
        meeting = s.get(Meeting, action["meeting_id"])
        revision = int((meeting.options or {}).get("approval_revision", 1))
        if not pipeline.submit_deliver(meeting.id, allowed=("awaiting_approval",), revision=revision):
            raise AgentError("совещание уже обработано")
    elif kind == "reject_meeting":
        meeting = s.get(Meeting, action["meeting_id"])
        pipeline.reject(meeting.id, revision=int((meeting.options or {}).get("approval_revision", 1)))


# ----------------------------------------------------------------------------- one message
def _process(row_id: int) -> None:
    with SessionLocal() as s:
        row = s.get(ConversationMessage, row_id)
        result = dict(row.result or {})
        msg_like = {"name": result.get("name", "")}
        # Roles were decided when the message arrived (the sender's @username is only known then).
        employee = s.get(Employee, result["employee_id"]) if result.get("employee_id") else None
        if employee is not None and not employee.active:
            employee = None
        reviewer = bool(result.get("reviewer"))
        if employee is None and not reviewer:
            row.status = "agent_ignored"
            s.commit()
            return
        ctx = _context(s, row, msg_like, employee, reviewer)
        decision = result.get("decision")
        if decision is None:
            system = _system(ctx)
            feedback = ""
            for attempt in (1, 2):
                decision = _decide(system, ctx, row.text, feedback)
                errors = [e for e in (_check(s, a, ctx, employee, reviewer) for a in decision["actions"]) if e]
                if not errors:
                    break
                if attempt == 2:
                    raise AgentError("; ".join(errors)[:300])
                feedback = "\nПрошлый ответ нельзя выполнить: " + "; ".join(errors) + ". Исправь ответ."
            result["decision"] = decision
            row.result = result
            s.commit()
        if not result.get("actions_done"):
            for action in decision["actions"]:
                _apply(s, row, action, employee)
            result["actions_done"] = True
            row.result = dict(result)
            s.commit()
        if decision["reply"] and not result.get("reply_sent"):
            sent = userbot.send_message(row.chat_id, decision["reply"])
            s.add(ConversationMessage(chat_id=row.chat_id, message_id=sent["message_id"], role="assistant",
                                      status="agent_done", text=decision["reply"][:4000]))
            result["reply_sent"] = True
            row.result = dict(result)
        row.status, row.error = "agent_done", ""
        s.commit()
