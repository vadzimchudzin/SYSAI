"""The AI employee's side of every conversation.

People write to the Telegram account; the model answers in its own words and may ask for checked actions. Code decides
whether an action is allowed (who the sender is, which task or meeting it concerns) and applies it. Only directory
employees, reviewers and contacts with full access get answers: the account may also be a private one, everyone else is
ignored and their texts are not even stored. A reply is never a fixed phrase. When the model is unavailable the message
waits and is retried; nobody receives a made-up answer.

Who may do what
- employee: sees only their own tasks; sets a deadline, reports progress, writes notes about their own work, asks the
  reviewer.
- full access (a setting): additionally sees the whole team, every task and note, asks for reports, writes notes.
- reviewer: all of it, plus approves or rejects meetings, answers employees' questions and assigns tasks.
"""
import collections
import concurrent.futures
import datetime as dt
import json
import logging
import threading
import time

from sqlalchemy.exc import IntegrityError

from . import bitrix, deadlines, llm, meeting_alerts, reminders, settings_store, transcribe, userbot, voice
from .db import (ConversationEscalation, ConversationMessage, Employee, Meeting, Note, PersonMemory, SessionLocal,
                 Task, now)

log = logging.getLogger("sysai.agent")

HISTORY = 40            # messages of one conversation the model sees
MAX_ATTEMPTS = 3
RETRY_AFTER = 20        # seconds before a waiting message is picked up again
TALKING_HOURS = 8       # within this time a conversation is "going on": no new greeting
MAX_REPLY = 3500
OPEN_STATUSES = ("draft", "sent", "awaiting_deadline", "in_progress", "blocked")
POOL = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="agent")
_locks = collections.defaultdict(threading.Lock)


class AgentError(RuntimeError):
    """The model's decision cannot be carried out; the message is retried or given up, never answered."""


def _testing() -> bool:
    from . import config
    return config.TESTING


def _aware(moment):
    if moment is not None and moment.tzinfo is None:
        return moment.replace(tzinfo=dt.timezone.utc)
    return moment


# ----------------------------------------------------------------------------- who is writing
def _in_refs(setting: str, msg: dict) -> bool:
    return any(userbot.same_person(ref, msg) for ref in settings_store.get(setting) or [])


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


ROSTER_TTL = 20  # seconds the list of people we serve is kept in memory
_ROSTER = {"at": 0.0, "refs": (), "employees": ()}


def invalidate_roster() -> None:
    _ROSTER["at"] = 0.0


def _roster() -> dict:
    if time.time() - _ROSTER["at"] > ROSTER_TTL:
        with SessionLocal() as s:
            people = tuple((u or "", c or "") for u, c in
                           s.query(Employee.telegram_user_id, Employee.telegram_chat_id).filter_by(active=True).all())
        refs = (tuple(settings_store.get("director_chat_ids") or []) + tuple(settings_store.get("approver_chat_ids") or [])
                + tuple(settings_store.get("full_access_chat_ids") or []))
        _ROSTER.update(at=time.time(), refs=refs, employees=people)
    return _ROSTER


def wants(msg: dict) -> bool:
    """Cheap pre-check for the Telegram account (same rule as `_accept`, but from memory): does the app serve this sender?
    Strangers are dropped before any database work, which also keeps a private account's other chats untouched."""
    roster = _roster()
    if any(userbot.same_person(ref, msg) for ref in roster["refs"]):
        return True
    uid = str(msg["user_id"])
    return any((known and known == uid) or userbot.same_person(chat, msg) for known, chat in roster["employees"])


# ----------------------------------------------------------------------------- intake
def _seen(msg: dict) -> bool:
    with SessionLocal() as s:
        return s.query(ConversationMessage.id).filter_by(chat_id=str(msg["user_id"]), message_id=msg["message_id"]).first() is not None


def _with_transcript(msg: dict):
    """A voice message becomes text before anything else: strangers' voices are never downloaded, a message the unread scan
    hands over again is not transcribed twice. The text of a voice that could not be understood is a note of the system
    (the model is told, and answers the person in its own words)."""
    voice_info = msg["voice"]
    if not wants(msg) or _seen(msg):
        return None
    out = {k: v for k, v in msg.items() if k != "voice"}
    problem = None
    if voice_info.get("too_long"):
        text, problem = f"(голосовое сообщение длиннее {userbot.MAX_VOICE_SECONDS // 60} минут, распознавать не стал)", "too_long"
    else:
        try:
            text = transcribe.voice_text(voice_info["fetch"](), voice_info.get("mime", ""), voice_info.get("seconds", 0)).strip()
            if not text:
                text, problem = "(в голосовом сообщении нет речи)", "empty"
        except Exception as exc:  # noqa: BLE001
            log.warning("voice message %s of %s not recognised: %s", msg["message_id"], msg["user_id"], exc)
            text, problem = "(голосовое сообщение, речь распознать не удалось)", "failed"
    out["text"] = text
    out["voice_info"] = {"seconds": voice_info.get("seconds", 0), "problem": problem}
    return out


def handle_incoming(msg: dict) -> None:
    """Called by the Telegram account for each private text or voice message. Safe to call twice with the same message."""
    if not userbot.ai_mode():
        return
    try:
        if msg.get("voice") is not None:
            msg = _with_transcript(msg)
            if msg is None:
                return
        row_id = _accept(msg)
    except Exception:  # noqa: BLE001
        log.exception("incoming message dropped")
        return
    if row_id is None:
        return
    if userbot.quiet_until() is not None:
        return  # the reply waits in the queue until the quiet hours end (see run_pending)
    if _testing():
        process(row_id)
    else:
        from . import pipeline
        pipeline.poke()
        POOL.submit(process, row_id)


def _accept(msg: dict):
    chat = str(msg["user_id"])
    with SessionLocal() as s:
        director = _in_refs("director_chat_ids", msg)
        reviewer = director or _in_refs("approver_chat_ids", msg)
        full = reviewer or _in_refs("full_access_chat_ids", msg)
        employee = _find_employee(s, msg)
        wanted = bool(full or employee)
        row = ConversationMessage(
            chat_id=chat, message_id=msg["message_id"], role="user",
            status="agent_pending" if wanted else "agent_ignored",
            text=str(msg["text"]).strip()[:4000] if wanted else "",  # a stranger's words are not kept
            result={"attempts": 0, "name": msg.get("name", "") if wanted else "", "reviewer": reviewer,
                    "director": director, "full_access": full, "employee_id": employee.id if employee else None,
                    "voice": bool(msg.get("voice_info")), "voice_problem": (msg.get("voice_info") or {}).get("problem"),
                    "username": msg.get("username", "") if wanted else "", "phone": msg.get("phone", "") if wanted else ""})
        s.add(row)
        try:
            s.commit()
        except IntegrityError:
            s.rollback()
            return None  # already seen (the unread scan hands messages over again)
        return row.id if wanted else None


def record_outgoing(ref: str, text: str, message_id, user_id) -> None:
    """Everything the account sends to a person (task messages, drafts, files) becomes part of that conversation, so
    the next reply knows what was said. Also teaches the directory the person's numeric Telegram id."""
    if not user_id or message_id is None:
        return
    chat = str(user_id)
    with SessionLocal() as s:
        employee = s.query(Employee).filter_by(telegram_chat_id=str(ref)).first()
        if employee is not None and not employee.telegram_user_id:
            employee.telegram_user_id = chat
        s.add(ConversationMessage(chat_id=chat, message_id=message_id, role="assistant", status="agent_done",
                                  text=str(text)[:4000], result={}))
        try:
            s.commit()
        except IntegrityError:
            s.rollback()


def run_pending() -> None:
    """Retries messages that could not be answered yet (model unavailable, Telegram hiccup)."""
    if not userbot.ai_mode():
        return
    if userbot.quiet_until() is not None:
        return
    limit = now() - dt.timedelta(seconds=RETRY_AFTER)
    with SessionLocal() as s:
        ids = [r.id for r in s.query(ConversationMessage).filter(
            ConversationMessage.role == "user", ConversationMessage.status == "agent_pending",
            ConversationMessage.created_at < limit).order_by(ConversationMessage.id).all()]
    for row_id in ids:
        process(row_id) if _testing() else POOL.submit(process, row_id)


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


# ----------------------------------------------------------------------------- what the model gets to see
def _task_view(task, employee_view: bool) -> dict:
    view = {"id": task.id, "title": task.title, "description": (task.description or "")[:400],
            "status": task.status, "deadline": task.deadline.isoformat() if task.deadline else None,
            "can_set_deadline": task.deadline is None, "priority": task.priority,
            "meeting": task.meeting.title if task.meeting else "", "meeting_id": task.meeting_id}
    if employee_view:
        view["quote"] = (task.source_quote or "")[:250]
    return view


def _meeting_view(meeting, brief: bool = False) -> dict:
    report = meeting.report if isinstance(meeting.report, dict) else {}
    view = {"id": meeting.id, "title": meeting.title or "", "status": meeting.status,
            "date": (meeting.meeting_date.isoformat() if meeting.meeting_date else None),
            "summary": str(report.get("summary") or "")[: (300 if brief else 700)]}
    if not brief:
        view["decisions"] = [x.get("text") if isinstance(x, dict) else x for x in report.get("decisions") or []][:6]
    return view


def _notes_view(s, employee_id=None, limit=15) -> list:
    query = s.query(Note).order_by(Note.id.desc())
    if employee_id is not None:
        query = query.filter(Note.employee_id == employee_id)
    names = {}
    out = []
    for note in query.limit(limit).all():
        if note.employee_id not in names:
            owner = s.get(Employee, note.employee_id) if note.employee_id else None
            names[note.employee_id] = owner.name if owner else None
        out.append({"about": names[note.employee_id], "kind": note.kind, "text": note.text[:300],
                    "when": _aware(note.created_at).date().isoformat() if note.created_at else None})
    out.reverse()
    return out


def _context(s, row, result: dict, employee, reviewer: bool, full: bool, director: bool = False,
             director_mode: bool = False) -> dict:
    chat = row.chat_id
    history = (s.query(ConversationMessage).filter(
        ConversationMessage.chat_id == chat, ConversationMessage.role.in_(("user", "assistant")),
        ConversationMessage.status.in_(("agent_pending", "agent_done")), ConversationMessage.id < row.id)
        .order_by(ConversationMessage.id.desc()).limit(HISTORY).all())
    history.reverse()
    last = _aware(history[-1].created_at) if history and history[-1].created_at else None
    minutes = int((now() - last).total_seconds() // 60) if last else None
    memory = s.get(PersonMemory, chat)
    ctx = {"today": deadlines.today().isoformat(), "now": reminders.now_view(), "sender_name": result.get("name") or None,
           "voice_message": bool(result.get("voice")), "voice_problem": result.get("voice_problem") or None,
           "roles": [r for r, on in (("employee", employee is not None), ("full_access", full and not reviewer),
                                     ("reviewer", reviewer), ("director", director)) if on],
           "director_mode": director_mode,
           "conversation": {"already_talking": minutes is not None and minutes < TALKING_HOURS * 60,
                            "minutes_since_last_message": minutes, "messages_so_far": len(history)},
           "memory": (memory.summary if memory else "") or None,
           "history": [{"from": "человек" if h.role == "user" else "ты", "text": h.text[:900]} for h in history]}
    if employee is not None:
        tasks = (s.query(Task).filter(Task.assignee_id == employee.id, Task.status.in_(OPEN_STATUSES + ("done",)))
                 .order_by(Task.id.desc()).limit(30).all())
        mine = [t for t in tasks if t.status in OPEN_STATUSES and t.status != "draft"]
        ctx["employee"] = {"name": employee.name, "position": employee.position or None,
                           "responsibilities": getattr(employee, "responsibilities", "") or None,
                           "tasks": [_task_view(t, True) for t in mine],
                           "done_recently": [t.title for t in tasks if t.status == "done"][:5]}
        meeting_ids = list(dict.fromkeys(t.meeting_id for t in tasks))[:4]
        ctx["meetings"] = [_meeting_view(m) for m in (s.get(Meeting, i) for i in meeting_ids) if m]
        ctx["waiting_for_reviewer"] = [{"id": e.id, "question": e.reason} for e in
                                       s.query(ConversationEscalation).filter_by(chat_id=chat, status="open").all()]
        ctx["notes"] = _notes_view(s, employee.id)
    if full:
        people = s.query(Employee).filter_by(active=True).order_by(Employee.name).all()
        tasks = s.query(Task).order_by(Task.id.desc()).limit(300).all()
        by_person = collections.defaultdict(list)
        for task in tasks:
            if task.assignee_id and task.executor != "assistant":
                by_person[task.assignee_id].append(task)
        ctx["team"] = [{"id": p.id, "name": p.name, "position": p.position or None,
                        "responsibilities": getattr(p, "responsibilities", "") or None,
                        "reachable": reminders.reachable(p),
                        "open_tasks": [_task_view(t, False) for t in by_person[p.id] if t.status in OPEN_STATUSES][:8],
                        "done": sum(t.status == "done" for t in by_person[p.id]),
                        "blocked": sum(t.status == "blocked" for t in by_person[p.id])} for p in people]
        ctx["unassigned_tasks"] = [_task_view(t, False) for t in tasks if t.assignee_id is None
                                   and t.executor != "assistant" and t.status in ("draft", "no_recipient")][:15]
        ctx["assistant_tasks"] = [{"id": t.id, "title": t.title, "status": t.status, "meeting_id": t.meeting_id}
                                  for t in tasks if t.executor == "assistant"][:15]
        ctx["notes"] = _notes_view(s, None, 25)
        ctx["recent_meetings"] = [_meeting_view(m, True) for m in
                                  s.query(Meeting).order_by(Meeting.id.desc()).limit(4).all()]
    ctx["reminders_pending"] = reminders.pending_view(s, chat, full)
    if reviewer:
        waiting = s.query(Meeting).filter_by(status="awaiting_approval").order_by(Meeting.id.desc()).limit(5).all()
        ctx["meetings_awaiting_approval"] = [{**_meeting_view(m, True), "tasks": len(m.tasks)} for m in waiting]
        questions = s.query(ConversationEscalation).filter_by(status="open").order_by(ConversationEscalation.id).limit(10).all()
        ctx["open_questions_from_employees"] = [
            {"id": q.id, "employee": _name_for_chat(s, q.chat_id), "question": q.reason,
             "task": (s.get(Task, q.task_id).title if q.task_id and s.get(Task, q.task_id) else None)} for q in questions]
    return ctx


def _name_for_chat(s, chat: str) -> str:
    employee = s.query(Employee).filter_by(telegram_user_id=chat).first()
    return employee.name if employee else chat


ACTIONS_HELP = (
    'Формат ответа: {"reply": "что написать человеку или пустая строка", "actions": [...], "memory": "необязательно"}. '
    "Действия (ID бери только из данных; код проверяет права и ID): "
    '{"type":"set_deadline","task_id":ID,"date":"ГГГГ-ММ-ДД"}: сотрудник назвал конкретную дату, а у его задачи нет срока; '
    '{"type":"task_status","task_id":ID,"status":"in_progress|done|blocked","note":"что сделано или что мешает"}: '
    "человек сообщил, как идёт задача; "
    '{"type":"note","text":"...","employee_id":ID или null,"task_id":ID или null,"kind":"fact|status|commitment|done"}: '
    "запомнить факт о том, кто что сделал, пообещал или сказал (для себя, не для пересылки); "
    '{"type":"ask_reviewer","task_id":ID или null,"question":"суть","message":"точный текст проверяющему"}: сотруднику нужен '
    "ответ, которого нет в данных; "
    '{"type":"relay_answer","question_id":ID,"message":"точный текст сотруднику"}: проверяющий ответил на вопрос сотрудника; '
    '{"type":"approve_meeting","meeting_id":ID} и {"type":"reject_meeting","meeting_id":ID}: проверяющий прямо согласился '
    "отправить именно это совещание или отклонил его (если совещаний несколько и неясно какое, спроси); "
    '{"type":"assign_task","task_id":ID,"to":ID сотрудника или "assistant","message":"текст сотруднику, если совещание уже '
    'отправлено"}: проверяющий решил, кто делает задачу (to="assistant" значит, что задачу выполнишь ты сам: график, '
    "таблица, сводка, отчёт, напоминание, запрос статуса; так же повторяют задачу ассистента со статусом "
    "assistant_failed); "
    '{"type":"team_report","scope":"all или ID сотрудника"}: человек с полным доступом просит отчёт о том, кто что делает '
    "(код соберёт красивый PDF из базы и пришлёт после твоего ответа, в reply коротко скажи главное). "
    '{"type":"remind","to":"me" или "all" или "reviewers" или [ID сотрудников из команды],"at":"ГГГГ-ММ-ДД ЧЧ:ММ" или null,'
    '"topic":"о чём напомнить, коротко","details":"время и место, что подготовить, если назвали","meeting":true или false}: '
    "человек просит кому-то напомнить (например, о совещании). meeting=true, если напоминание о совещании, встрече или "
    "планёрке: напоминание, которое поставлено в личном кабинете и ещё не сработало, тогда снимается для этих людей, чтобы "
    "не писать им дважды (для остальных тем meeting не указывай). at это время напоминания по now.local (сегодня в 15:00, за час до "
    "совещания, завтра утром считай сам от now), null значит сразу. to=me: себе, это может любой; остальным (all это "
    "все сотрудники из справочника, reviewers это проверяющие) напоминают только проверяющий и люди с полным доступом. "
    "Текст напоминания потом напишешь сам, в reply скажи кому и когда напомнишь; если неясно кому, когда или о чём, "
    'сначала спроси; '
    '{"type":"cancel_reminder","reminder_id":ID}: отменить напоминание из reminders_pending. '
    "Поле memory: если узнал что-то важное о человеке или его работе, верни обновлённую краткую память (до 1200 символов, "
    "сохрани важное из прошлой памяти), иначе не добавляй поле.")


def _system() -> str:
    task = (
        "Тебе пишет человек в личные сообщения. Веди живой разговор, как внимательный коллега, своими словами. "
        "Приветствие: здоровайся только когда знакомство начинается или после долгого перерыва "
        "(conversation.already_talking=false). Если already_talking=true, не здоровайся и не представляйся, "
        "продолжай с того места, где остановились, как в живой переписке. Не повторяй то, что уже говорил. "
        "Ты можешь вести долгие содержательные разговоры: обсуждать задачи, подход, сроки, риски, помогать думать вслух, "
        "задавать уточняющие вопросы по делу. Опирайся на историю, память (memory), заметки (notes) и данные о задачах. "
        "Короткие реплики вроде «ок», «понял», «спасибо» не требуют вопросов: отвечай коротко и естественно или "
        "оставь reply пустым. Не пиши «не понял», если смысл ясен из истории; если неясно, задай один конкретный вопрос. "
        "Не выдумывай статусы, сроки и обещания. Не обещай то, чего нет среди действий: любое обещание "
        "«сделаю», «передам», «поставлю» должно быть действием в actions, иначе не обещай. "
        "Если сотрудник спрашивает то, чего нет в данных, используй ask_reviewer и скажи, что уточняешь. Если ты уже "
        "ждёшь ответа проверяющего (waiting_for_reviewer), честно скажи об этом и что вернёшься с ответом. "
        "Сотруднику не раскрывай задачи и данные других людей. Данные команды (team) есть только у проверяющих и "
        "людей с полным доступом. При выборе исполнителя используй responsibilities (реальные обязанности и компетенции), "должность и текущие задачи. Если есть несколько людей с одинаковым именем, не выбирай только по имени: "сопоставь смысл поручения с обязанностями; если уверенного единственного совпадения нет, задай уточняющий вопрос "и не назначай задачу наугад. Простые действия, которые ты реально можешь выполнить доступными действиями, "не перекладывай на сотрудника только ради передачи: например, сведи данные в таблицу/сводку или подготовь график, "если для этого есть данные. "
        "Ты подчиняешься директору (roles содержит director). Просьбы уровня директора: что делают, чем заняты и как "
        "работают другие люди, отчёты по команде, поручения, напоминания другим, любые указания «сделай то-то». Их "
        "выполняют для директора: ему можно рассказывать всё, что есть в данных. Если director_mode=false, их выполняют "
        "также проверяющие и люди с полным доступом (roles содержит reviewer или full_access). Всем остальным, а при "
        "director_mode=true всем, кроме директора, такие просьбы ты не выполняешь и данных о других людях не раскрываешь. "
        "Откажи мягко, коротко и своими словами, чтобы человек не почувствовал стену: скажи, что такие просьбы ты по "
        "запросам коллег не выполняешь и с ними лучше к руководителю, и сразу предложи то, что можешь сделать для него "
        "самого (его задачи, статусы, напоминание ему самому). Не пиши «нет доступа», «недостаточно прав», «запрещено», "
        "«не уполномочен», не объясняй, как устроены роли, и не называй, кто директор. "
        "Если voice_message=true, текст это автоматическая расшифровка голосового сообщения: возможны ошибки в именах, "
        "числах и времени. Когда от точности зависит действие (кому писать, во сколько напомнить), назови в reply, что "
        "именно ты понял и сделал, чтобы человек мог поправить. Если voice_problem задан (too_long, failed, empty), текст "
        "в скобках это пометка системы, а не слова человека: скажи по-человечески, что голосовое не удалось разобрать, и "
        "попроси повторить или написать. "
        + ACTIONS_HELP)
    return voice.system_prompt(task)


def _decide(system: str, ctx: dict, text: str, feedback: str = "") -> dict:
    user = ("<data>" + json.dumps(ctx, ensure_ascii=False) + "</data>\nСообщение человека:\n<data>" + text + "</data>" + feedback)
    try:
        raw = llm.chat(voice.model(), [{"role": "system", "content": system}, {"role": "user", "content": user}],
                       json_mode=True, temperature=0.6, max_tokens=1800, timeout=90)
        data = llm.parse_json(raw)
    except llm.LLMError as exc:
        raise AgentError(f"модель недоступна: {str(exc)[:200]}") from exc
    if not isinstance(data, dict):
        raise AgentError("модель вернула не объект")
    reply, actions, memory = data.get("reply"), data.get("actions") or [], data.get("memory")
    if (reply is not None and not isinstance(reply, str)) or not isinstance(actions, list) \
            or (memory is not None and not isinstance(memory, str)):
        raise AgentError("модель вернула ответ в неверной форме")
    return {"reply": (reply or "").strip()[:MAX_REPLY], "actions": [a for a in actions if isinstance(a, dict)],
            "memory": (memory or "").strip()[:1200]}


# ----------------------------------------------------------------------------- checking actions (no database here)
def _known_task_ids(ctx: dict, *, own_only: bool = False) -> set:
    ids = {t["id"] for t in (ctx.get("employee") or {}).get("tasks", [])}
    if not own_only:
        for person in ctx.get("team", []):
            ids |= {t["id"] for t in person["open_tasks"]}
        ids |= {t["id"] for t in ctx.get("unassigned_tasks", [])}
    return ids


def _check(action: dict, ctx: dict, employee, reviewer: bool, full: bool) -> str:
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
        return "" if date >= deadlines.today() else "set_deadline: дата уже прошла"
    if kind == "task_status":
        if action.get("status") not in ("in_progress", "done", "blocked"):
            return "task_status: статус in_progress, done или blocked"
        ids = _known_task_ids(ctx, own_only=not full)
        return "" if action.get("task_id") in ids else "task_status: задача не найдена среди доступных"
    if kind == "note":
        if not str(action.get("text") or "").strip():
            return "note: нужен text"
        if action.get("kind", "fact") not in ("fact", "status", "commitment", "done"):
            return "note: kind fact, status, commitment или done"
        employee_id = action.get("employee_id")
        if employee is not None and not full:
            if employee_id not in (None, employee.id):
                return "note: сотрудник может писать заметки только о себе"
            if action.get("task_id") is not None and action["task_id"] not in _known_task_ids(ctx, own_only=True):
                return "note: задача не найдена среди задач сотрудника"
        elif employee_id is not None and employee_id not in {p["id"] for p in ctx.get("team", [])}:
            return "note: сотрудник не найден в команде"
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
        return "" if task_ids else "ask_reviewer: у сотрудника нет задач, вопрос не к чему привязать"
    if kind == "relay_answer":
        if not reviewer:
            return "relay_answer доступно только проверяющему"
        if action.get("question_id") not in {q["id"] for q in ctx.get("open_questions_from_employees", [])}:
            return "relay_answer: вопрос не найден среди открытых"
        return "" if str(action.get("message") or "").strip() else "relay_answer: нужен message"
    if kind in ("approve_meeting", "reject_meeting"):
        if not reviewer:
            return f"{kind} доступно только проверяющему"
        ids = {m["id"] for m in ctx.get("meetings_awaiting_approval", [])}
        return "" if action.get("meeting_id") in ids else f"{kind}: совещание не ждёт проверки"
    if kind == "assign_task":
        if not (reviewer and full):
            return "assign_task доступно только проверяющему"
        task = next((t for t in ctx.get("unassigned_tasks", []) if t["id"] == action.get("task_id")), None)
        if task is None:
            task = next((t for person in ctx.get("team", []) for t in person["open_tasks"] if t["id"] == action.get("task_id")), None)
        again = next((t for t in ctx.get("assistant_tasks", []) if t["id"] == action.get("task_id")
                      and t["status"] in ("assistant_failed", "assistant_done")), None)
        if task is None and again is None:
            return "assign_task: задача не найдена среди доступных"
        target = action.get("to")
        if target != "assistant" and target not in {p["id"] for p in ctx.get("team", [])}:
            return 'assign_task: to должен быть ID сотрудника из команды или "assistant"'
        if target != "assistant" and task is not None and task.get("status") in ("sent", "in_progress", "blocked", "awaiting_deadline") \
                and not str(action.get("message") or "").strip():
            return "assign_task: совещание уже отправлено, нужен message сотруднику"
        return ""
    if kind == "remind":
        if not str(action.get("topic") or "").strip():
            return "remind: нужен topic (о чём напомнить)"
        if action.get("meeting") not in (None, True, False):
            return "remind: meeting это true или false"
        to = action.get("to")
        if to != "me":
            if not full:
                return 'remind: сотрудник может попросить напомнить только себе (to: "me")'
            team = {p["id"]: p for p in ctx.get("team", [])}
            if to == "all":
                if not any(p.get("reachable") for p in team.values()):
                    return "remind: ни у кого из сотрудников нет Telegram-контакта в справочнике"
            elif to == "reviewers":
                if not (settings_store.get("approver_chat_ids") or []):
                    return "remind: проверяющие не указаны в настройках"
            elif isinstance(to, list) and to and all(isinstance(x, int) and not isinstance(x, bool) for x in to):
                missing = [x for x in to if x not in team]
                if missing:
                    return f"remind: сотрудники {missing} не найдены в команде"
                silent = [team[x]["name"] for x in to if not team[x].get("reachable")]
                if silent:
                    return f"remind: у {', '.join(silent)} нет Telegram-контакта в справочнике, написать нельзя"
            else:
                return 'remind: to это "me", "all", "reviewers" или список ID сотрудников'
        try:
            reminders.parse_when(action.get("at"))
        except reminders.WhenError as exc:
            return f"remind: {exc}"
        return ""
    if kind == "cancel_reminder":
        ids = {r["id"] for r in ctx.get("reminders_pending", [])}
        return "" if action.get("reminder_id") in ids else "cancel_reminder: напоминание не найдено среди ожидающих"
    if kind == "team_report":
        if not full:
            return "team_report доступно только проверяющему и людям с полным доступом"
        scope = action.get("scope")
        return "" if scope == "all" or scope in {p["id"] for p in ctx.get("team", [])} else "team_report: scope all или ID сотрудника"
    return f"неизвестное действие {kind!r}"


def _denied(action: dict, employee, reviewer: bool, full: bool) -> bool:
    """The action needs more than this person is allowed (as opposed to a mistake in the action itself)."""
    kind = action.get("type")
    if kind in ("team_report", "assign_task"):
        return not full
    if kind == "remind":
        return action.get("to") != "me" and not full
    if kind in ("relay_answer", "approve_meeting", "reject_meeting"):
        return not reviewer
    if kind == "note":
        return action.get("employee_id") not in (None, employee.id if employee else None) and not full
    return False


DECLINE_GENTLY = ("\nЭти действия тебе недоступны: такие просьбы по запросам коллег ты не выполняешь. Не выполняй их и убери "
                  "из actions. В reply мягко, коротко и по-человечески скажи, что такие просьбы ты не выполняешь и с ними "
                  "лучше к руководителю, и предложи, чем можешь помочь этому человеку сам. Без слов «нет доступа», «нет "
                  "прав», «запрещено».")


# ----------------------------------------------------------------------------- applying actions (short sessions)
def _remember(s, text: str, *, employee_id=None, task_id=None, meeting_id=None, kind="fact", source="conversation", author=""):
    s.add(Note(employee_id=employee_id, task_id=task_id, meeting_id=meeting_id, kind=kind, text=str(text).strip()[:800],
               source=source, author=author[:80]))


def _employee_ref(s, chat: str):
    employee = s.query(Employee).filter_by(telegram_user_id=chat).first()
    return (employee.telegram_chat_id if employee and employee.telegram_chat_id else chat)


def _apply(action: dict, chat: str, employee_id, author: str, sender: dict | None = None, full: bool = False) -> None:
    from . import assistant, pipeline
    kind = action["type"]
    with SessionLocal() as s:
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
            _remember(s, f"Срок по задаче «{task.title}»: {action['date']}", employee_id=task.assignee_id, task_id=task.id,
                      kind="commitment", author=author)
            s.commit()
        elif kind == "task_status":
            task = s.get(Task, action["task_id"])
            task.status = action["status"]
            note = str(action.get("note") or "").strip()
            label = {"in_progress": "в работе", "done": "выполнена", "blocked": "заблокирована"}[action["status"]]
            _remember(s, f"Задача «{task.title}»: {label}" + (f". {note}" if note else ""), employee_id=task.assignee_id,
                      task_id=task.id, kind="done" if action["status"] == "done" else "status", author=author)
            s.commit()
        elif kind == "note":
            target = action.get("employee_id") if action.get("employee_id") is not None else employee_id
            _remember(s, action["text"], employee_id=target, task_id=action.get("task_id"),
                      kind=action.get("kind", "fact"), author=author)
            s.commit()
        elif kind == "ask_reviewer":
            task = s.get(Task, action["task_id"]) if action.get("task_id") is not None else None
            meeting_id = task.meeting_id if task else (
                s.query(Task).filter(Task.assignee_id == employee_id).order_by(Task.id.desc()).first().meeting_id)
            s.add(ConversationEscalation(meeting_id=meeting_id, task_id=task.id if task else None, chat_id=chat,
                                         reason=str(action["question"]).strip()[:1000], status="open"))
            _remember(s, f"Вопрос проверяющему: {str(action['question']).strip()}", employee_id=employee_id,
                      task_id=task.id if task else None, kind="status", source="system", author=author)
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
            target = _employee_ref(s, question.chat_id)
            employee_chat = question.chat_id
            text = str(action["message"]).strip()
            sent = userbot.send_message(target, text)
            s.add(ConversationMessage(chat_id=employee_chat, message_id=sent["message_id"], role="assistant",
                                      status="agent_done", text=text[:4000], result={}))
            question.status = "resolved"
            _remember(s, f"Ответ проверяющего на вопрос «{question.reason[:120]}»: {text}", employee_id=employee_id,
                      kind="status", source="system", author=author)
            s.commit()
        elif kind in ("approve_meeting", "reject_meeting"):
            meeting = s.get(Meeting, action["meeting_id"])
            revision = int((meeting.options or {}).get("approval_revision", 1))
            meeting_id = meeting.id
            s.commit()
            if kind == "approve_meeting":
                if not pipeline.submit_deliver(meeting_id, allowed=("awaiting_approval",), revision=revision):
                    raise AgentError("совещание уже обработано")
            else:
                pipeline.reject(meeting_id, revision=revision)
        elif kind == "remind":
            people = reminders.recipients_for(s, action["to"], chat, sender)
            if not people:
                raise AgentError("некому напоминать: у адресатов нет контактов или это сам автор просьбы")
            due = max(reminders.parse_when(action.get("at")), now() + dt.timedelta(seconds=reminders.SEND_DELAY))
            reminder_id = reminders.create(s, chat=chat, requested_by=author, due=due, topic=str(action["topic"]),
                                           details=str(action.get("details") or ""), recipients=people)
            if action.get("meeting") is True and full:  # the alert from the panel that has not fired gives way to this
                meeting_alerts.claim(s, people, due, reminder_id)
            names = ", ".join(p["name"] or p["ref"] for p in people)
            _remember(s, f"Напоминание «{str(action['topic']).strip()[:120]}» для: {names}, на {reminders.local_text(due)}",
                      employee_id=employee_id, kind="commitment", source="system", author=author)
            s.commit()
            pipeline.poke()
        elif kind == "cancel_reminder":
            if not reminders.cancel(s, action["reminder_id"]):
                raise AgentError("напоминание уже отправлено или отменено")
            meeting_alerts.release(s, action["reminder_id"])
            s.commit()
        elif kind == "assign_task":
            task = s.get(Task, action["task_id"])
            target = action["to"]
            delivered = task.status in ("sent", "in_progress", "blocked", "awaiting_deadline")
            meeting_id = task.meeting_id
            if target == "assistant":
                task.executor, task.assignee_id, task.assignee_name = "assistant", None, "ассистент"
                task.status = "assistant_todo"
                _remember(s, f"Задача «{task.title}» поручена ассистенту", task_id=task.id, meeting_id=meeting_id,
                          kind="commitment", source="system", author=author)
                s.commit()
                assistant.schedule(meeting_id)
            else:
                person = s.get(Employee, target)
                task.executor, task.assignee_id, task.assignee_name = "person", person.id, person.name
                ref = person.telegram_chat_id
                if delivered or task.status.startswith("assistant_"):
                    task.status = "sent"
                _remember(s, f"Задача «{task.title}» поручена: {person.name}", employee_id=person.id, task_id=task.id,
                          meeting_id=meeting_id, kind="commitment", source="system", author=author)
                s.commit()
                message = str(action.get("message") or "").strip()
                if message and ref:
                    userbot.send_message(ref, message)


def _save_memory(chat: str, employee_id, summary: str) -> None:
    if not summary:
        return
    with SessionLocal() as s:
        memory = s.get(PersonMemory, chat) or PersonMemory(chat_id=chat)
        memory.summary, memory.employee_id = summary, employee_id
        s.merge(memory)
        s.commit()


# ----------------------------------------------------------------------------- one message
def _store(row_id: int, result: dict) -> None:
    with SessionLocal() as s:
        s.get(ConversationMessage, row_id).result = dict(result)
        s.commit()


def _process(row_id: int) -> None:
    from . import assistant
    with SessionLocal() as s:  # 1. read, then let go of the connection while the model thinks
        row = s.get(ConversationMessage, row_id)
        result, chat, text = dict(row.result or {}), row.chat_id, row.text
        employee = s.get(Employee, result["employee_id"]) if result.get("employee_id") else None
        if employee is not None and not employee.active:
            employee = None
        reviewer, full = bool(result.get("reviewer")), bool(result.get("full_access"))
        director = bool(result.get("director"))
        director_mode = bool(settings_store.get("director_chat_ids"))
        if employee is None and not full:
            row.status = "agent_ignored"
            s.commit()
            return
        if director_mode and not director:
            full = False  # once there is a director, the view of the whole team and the orders are theirs alone
        employee_id = employee.id if employee else None
        author = result.get("name") or (employee.name if employee else "")
        decision = result.get("decision")
        ctx = _context(s, row, result, employee, reviewer, full, director, director_mode) if decision is None else None
    if decision is None:  # 2. decide (checked actions; a bad answer is explained to the model once)
        system, feedback = _system(), ""
        for attempt in (1, 2):
            decision = _decide(system, ctx, text, feedback)
            errors = [e for e in (_check(a, ctx, employee, reviewer, full) for a in decision["actions"]) if e]
            if not errors:
                break
            if attempt == 2:
                raise AgentError("; ".join(errors)[:300])
            feedback = "\nПрошлый ответ нельзя выполнить: " + "; ".join(errors) + ". Исправь ответ."
            if any(_denied(a, employee, reviewer, full) for a in decision["actions"]):
                feedback += DECLINE_GENTLY
        result["decision"] = decision
        _store(row_id, result)
    if not result.get("actions_done"):  # 3. act
        for action in decision["actions"]:
            _apply(action, chat, employee_id, author,
                   {"user_id": chat, "username": result.get("username", ""), "phone": result.get("phone", "")}, full=full)
        _save_memory(chat, employee_id, decision.get("memory", ""))
        result["actions_done"] = True
        _store(row_id, result)
    if decision["reply"] and not result.get("reply_sent"):  # 4. answer
        sent = userbot.send_message(chat, decision["reply"])
        with SessionLocal() as s:
            s.add(ConversationMessage(chat_id=chat, message_id=sent["message_id"], role="assistant", status="agent_done",
                                      text=decision["reply"][:4000], result={}))
            try:
                s.commit()
            except IntegrityError:
                s.rollback()
        result["reply_sent"] = True
        _store(row_id, result)
    for action in decision["actions"]:  # 5. things that follow the reply
        if action["type"] == "team_report" and not result.get("sent_team_report"):
            assistant.send_team_report(chat, action["scope"])
            result["sent_team_report"] = True
            _store(row_id, result)
    with SessionLocal() as s:
        row = s.get(ConversationMessage, row_id)
        row.status, row.error, row.result = "agent_done", "", dict(result)
        s.commit()
