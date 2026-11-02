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
import re
import time

from sqlalchemy.exc import IntegrityError

from . import access, bitrix, cases, deadlines, memory as company_memory, llm, meeting_alerts, reminders, settings_store, transcribe, userbot, voice
from .db import (ConversationEscalation, ConversationMessage, Document, Employee, Meeting, Note, PersonMemory, SessionLocal,
                 Task, now)

log = logging.getLogger("sysai.agent")

HISTORY = 40            # messages of one conversation the model sees
MAX_ATTEMPTS = 3
RECENT_FILES_HOURS = 48
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


RIGHTS_RU = access.LEVEL_RU


def rights_of(employee) -> str:
    """'director' | 'reviewer' | 'full' | 'observer' | '' (an ordinary employee); see access.py."""
    level = access.level_of(employee)
    return "" if level == "employee" else level


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
                + tuple(settings_store.get("full_access_chat_ids") or []) + tuple(settings_store.get("observer_chat_ids") or []))
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


def _with_document(msg: dict):
    """A file from a person we serve is kept in the document library (strangers' files are never downloaded). The
    conversation sees the caption, or a system note in brackets that a file came."""
    if not wants(msg) or _seen(msg):
        return None
    with SessionLocal() as s:
        employee = _find_employee(s, msg)
        employee_id = employee.id if employee is not None else None
    try:
        info = cases.intake_document(msg, employee_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("file %s of %s not received: %s", msg["message_id"], msg["user_id"], exc)
        info = {"problem": "failed", "filename": str(msg["document"].get("filename") or "файл")}
    out = {k: v for k, v in msg.items() if k != "document"}
    note = {"too_big": "(прислал файл «{}», он больше 20 МБ — не принят)", "empty": "(прислал пустой файл «{}»)",
            "failed": "(прислал файл «{}», скачать не удалось)"}.get(info.get("problem"), "(прислал файл «{}»)")
    out["text"] = (msg.get("text") or "").strip() or note.format(info.get("filename"))
    out["document_info"] = info
    return out


def handle_incoming(msg: dict) -> None:
    """Called by the Telegram account for each private text, voice message or file. Safe to call twice."""
    if not userbot.ai_mode():
        return
    try:
        if msg.get("voice") is not None:
            msg = _with_transcript(msg)
            if msg is None:
                return
        elif msg.get("document") is not None:
            msg = _with_document(msg)
            if msg is None:
                return
        row_id = _accept(msg)
    except Exception:  # noqa: BLE001
        log.exception("incoming message dropped")
        return
    if row_id is None:
        return
    _link_case(row_id)
    if userbot.quiet_until() is not None:
        return  # the reply waits in the queue until the quiet hours end (see run_pending)
    if _testing():
        process(row_id)
    else:
        from . import pipeline
        pipeline.poke()
        _schedule(chat_of(row_id))


# ----------------------------------------------------------------------------- bursts: an album or a few quick messages
BURST_QUIET = 4.0   # seconds without a new message from this person before answering
BURST_MAX = 15.0    # but never wait longer than this after the first one
BURST_WINDOW = 60   # messages further apart than this are separate conversations
_BURSTS: dict = {}  # chat -> (timer, first_at)
_BURSTS_LOCK = threading.Lock()


def chat_of(row_id: int) -> str:
    with SessionLocal() as s:
        return s.get(ConversationMessage, row_id).chat_id


def _schedule(chat: str) -> None:
    """Answer a person once for what they sent in one go (an album of photos, «сейчас скину» + files)."""
    with _BURSTS_LOCK:
        timer, first = _BURSTS.get(chat, (None, time.time()))
        if timer is not None:
            timer.cancel()
        delay = 0.0 if time.time() - first >= BURST_MAX else BURST_QUIET
        timer = threading.Timer(delay, _burst_due, args=(chat,))
        timer.daemon = True
        _BURSTS[chat] = (timer, first)
        timer.start()


def _burst_due(chat: str) -> None:
    with _BURSTS_LOCK:
        _BURSTS.pop(chat, None)
    with SessionLocal() as s:
        first = (s.query(ConversationMessage.id).filter_by(chat_id=chat, role="user", status="agent_pending")
                 .order_by(ConversationMessage.id).first())
    if first:
        POOL.submit(process, first[0])


def _file_note(docs: list) -> str:
    names = [str(d.get("filename") or "файл") for d in docs]
    shown = ", ".join(f"«{n}»" for n in names[:8]) + (f" и ещё {len(names) - 8}" if len(names) > 8 else "")
    return f"(прислал {len(names)} файл(ов) одним разом: {shown})"


def _merge_burst(chat: str):
    """Unanswered messages of one person become one message: texts joined, files listed together. Returns the row to
    answer (the earliest), or None when nothing is waiting. Messages already being decided on are left alone."""
    with SessionLocal() as s:
        rows = (s.query(ConversationMessage).filter_by(chat_id=chat, role="user", status="agent_pending")
                .order_by(ConversationMessage.id).all())
        fresh = [r for r in rows if not (r.result or {}).get("decision") and not int((r.result or {}).get("attempts") or 0)]
        if fresh:  # one go = sent within a minute of the first one; an older or retried message is answered on its own
            start = fresh[0].created_at
            fresh = [r for r in fresh if r.created_at is None or start is None
                     or (r.created_at - start).total_seconds() <= BURST_WINDOW]
        if len(fresh) < 2:
            return rows[0].id if rows else None
        lead, rest = fresh[0], fresh[1:]
        result = dict(lead.result or {})
        docs = [d for d in ((r.result or {}).get("document") for r in fresh) if d and d.get("document_id")]
        texts = []
        for r in fresh:
            doc = (r.result or {}).get("document")
            text = (r.text or "").strip()
            if doc and text.startswith("(прислал файл"):
                continue  # a bare file: listed once below instead of one bracket per photo
            if text:
                texts.append(text)
        if len(docs) > 1:
            texts.append(_file_note(docs))
        elif docs and not any(t.startswith("(прислал") for t in texts):
            texts.append(_file_note(docs))
        lead.text = "\n".join(texts)[:4000]
        result["documents"] = docs
        if docs:
            result["document"] = docs[0]
        for r in rest:
            if (r.result or {}).get("case") and not result.get("case"):
                result["case"] = r.result["case"]
            other = dict(r.result or {})
            other["merged_into"] = lead.id
            r.result, r.status = other, "agent_merged"
        result["merged"] = [r.id for r in rest]
        lead.result = result
        s.commit()
        return lead.id


def _accept(msg: dict):
    chat = str(msg["user_id"])
    with SessionLocal() as s:
        director = _in_refs("director_chat_ids", msg)
        reviewer = director or _in_refs("approver_chat_ids", msg)
        observer = _in_refs("observer_chat_ids", msg)
        full = reviewer or _in_refs("full_access_chat_ids", msg) or observer
        employee = _find_employee(s, msg)
        wanted = bool(full or employee)
        row = ConversationMessage(
            chat_id=chat, message_id=msg["message_id"], role="user",
            status="agent_pending" if wanted else "agent_ignored",
            text=str(msg["text"]).strip()[:4000] if wanted else "",  # a stranger's words are not kept
            result={"attempts": 0, "name": msg.get("name", "") if wanted else "", "reviewer": reviewer,
                    "director": director, "full_access": full, "observer": observer, "employee_id": employee.id if employee else None,
                    "voice": bool(msg.get("voice_info")), "voice_problem": (msg.get("voice_info") or {}).get("problem"),
                    "username": msg.get("username", "") if wanted else "", "phone": msg.get("phone", "") if wanted else "",
                    "reply_to": msg.get("reply_to"), "document": msg.get("document_info") if wanted else None,
                    "grouped_id": str(msg["grouped_id"]) if msg.get("grouped_id") else None})
        s.add(row)
        try:
            s.commit()
        except IntegrityError:
            s.rollback()
            return None  # already seen (the unread scan hands messages over again)
        return row.id if wanted else None


def _link_case(row_id: int) -> None:
    """Does this message (or file) answer one of the assistant's cases? The case goes on by itself; the conversation
    model is told so it can thank the person instead of acting on it."""
    try:
        with SessionLocal() as s:
            row = s.get(ConversationMessage, row_id)
            result = dict(row.result or {})
            if not result.get("employee_id"):
                return
            text, employee_id = row.text, result["employee_id"]
        document_id = (result.get("document") or {}).get("document_id")
        link = cases.on_message(employee_id, text, result.get("reply_to"), document_id)
        if link:
            with SessionLocal() as s:
                row = s.get(ConversationMessage, row_id)
                result = dict(row.result or {})
                result["case"] = link
                row.result = result
                s.commit()
    except Exception:  # noqa: BLE001
        log.exception("message %s not matched to a case", row_id)


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
        lead = _merge_burst(chat)
        if lead is not None and lead != row_id:
            with SessionLocal() as s:
                if s.get(ConversationMessage, row_id).status != "agent_pending":
                    row_id = lead  # this message went into an earlier one: answer that one
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
    if not employee_view and getattr(task, "target_employee", None):
        view["target_employee"] = task.target_employee.name
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
                           "departments": [{"name": m.department.name, "role": m.role or "",
                                            "responsibilities": m.responsibilities or ""}
                                           for m in employee.department_memberships
                                           if m.department is not None and m.department.active],
                           "tasks": [_task_view(t, True) for t in mine],
                           "done_recently": [t.title for t in tasks if t.status == "done"][:5]}
        meeting_ids = list(dict.fromkeys(t.meeting_id for t in tasks))[:4]
        ctx["meetings"] = [_meeting_view(m) for m in (s.get(Meeting, i) for i in meeting_ids) if m]
        ctx["waiting_for_reviewer"] = [{"id": e.id, "question": e.reason} for e in
                                       s.query(ConversationEscalation).filter_by(chat_id=chat, status="open").all()]
        ctx["notes"] = _notes_view(s, employee.id)
        ctx["assistant_waiting_for_you"] = cases.waiting_view(employee.id)
    ctx["my_cases"] = cases.person_view(employee.id if employee is not None else None, chat)
    # What the company memory knows about this message: an employee sees only their own card and notes.
    query = " ".join(h.text for h in history[-2:] if h.role == "user")[-600:] + " " + (getattr(row, "text", "") or "")
    ctx["known_facts"] = company_memory.for_prompt(query, viewer=None if full else (employee.id if employee else -1))
    found_skills = company_memory.skills_for(getattr(row, "text", "") or "")
    if found_skills:
        ctx["skills"] = found_skills
    if len(result.get("documents") or []) > 1:
        ctx["files_received"] = result["documents"]
    elif result.get("document"):
        ctx["file_received"] = result["document"]
    if result.get("case"):
        ctx["this_message_for_case"] = result["case"]
    if full:
        people = s.query(Employee).filter_by(active=True).order_by(Employee.name).all()
        tasks = s.query(Task).order_by(Task.id.desc()).limit(300).all()
        by_person = collections.defaultdict(list)
        for task in tasks:
            if task.assignee_id and task.executor != "assistant":
                by_person[task.assignee_id].append(task)
        ctx["team"] = [{"id": p.id, "name": p.name, "position": p.position or None,
                        "department": getattr(p, "department", "") or None,
                        "competencies": getattr(p, "competencies", "") or None,
                        "responsibilities": getattr(p, "responsibilities", "") or None,
                        "departments": [{"name": m.department.name, "role": m.role or "",
                                         "responsibilities": m.responsibilities or ""}
                                        for m in p.department_memberships
                                        if m.department is not None and m.department.active],
                        "context": getattr(p, "context", "") or None,
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
        ctx["recent_cases"] = cases.recent_view()
        since = now() - dt.timedelta(hours=RECENT_FILES_HOURS)
        ctx["recent_files"] = [{"id": d.id, "filename": d.filename, "size": d.size, "case_id": d.case_id,
                                "at": d.created_at.isoformat() if d.created_at else None}
                               for d in s.query(Document).filter(Document.chat_id == chat, Document.created_at >= since)
                               .order_by(Document.id.desc()).limit(30)]
    ctx["reminders_pending"] = reminders.pending_view(s, chat, full)
    if reviewer or director:
        ctx["cases_awaiting_decision"] = cases.decisions_view()
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
    "При назначении задачи сначала учитывай прямое указание директора; если исполнителя не назвали, сравнивай смысл задачи с department, competencies, responsibilities и context каждого сотрудника. Не выбирай только по имени или должности. Если подходят несколько сотрудников либо данных мало, задай директору один конкретный уточняющий вопрос и не создавай назначение наугад. Не выдумывай компетенции и загрузку. " 
    "Действия (ID бери только из данных; код проверяет права и ID): "
    '{"type":"set_deadline","task_id":ID,"date":"ГГГГ-ММ-ДД"}: сотрудник назвал конкретную дату, а у его задачи нет срока; '
    '{"type":"task_status","task_id":ID,"status":"in_progress|done|blocked","note":"что сделано или что мешает"}: '
    "человек сообщил, как идёт задача; "
    '{"type":"note","text":"...","employee_id":ID или null,"task_id":ID или null,"kind":"fact|status|commitment|done"}: '
    "запомнить факт о том, кто что сделал, пообещал или сказал (для себя, не для пересылки); "
    '{"type":"ask_reviewer","task_id":ID или null,"question":"суть","message":"точный текст проверяющему"}: сотруднику нужен '
    "ответ, которого нет в данных, или он просит изменить условия задачи (срок, выезд раньше, водитель, машина, состав) — "
    "это решает руководитель, не ты; "
    '{"type":"relay_answer","question_id":ID,"message":"точный текст сотруднику"}: проверяющий ответил на вопрос сотрудника; '
    '{"type":"approve_meeting","meeting_id":ID} и {"type":"reject_meeting","meeting_id":ID}: проверяющий прямо согласился '
    "отправить именно это совещание или отклонил его (если совещаний несколько и неясно какое, спроси); "
    '{"type":"assign_task","task_id":ID,"to":ID сотрудника или "assistant","message":"текст сотруднику, если совещание уже '
    'отправлено"}: проверяющий решил, кто делает задачу (to="assistant" значит, что задачу выполнишь ты сам: график, '
    "таблица, сводка, отчёт, напоминание, запрос статуса; так же повторяют задачу ассистента со статусом "
    "assistant_failed); "
    '{"type":"deliver_files","to":ID сотрудника,"document_ids":[ID из files_received или recent_files],"zip":true|false,'
    '"archive_name":"имя архива","note":"по какому делу"}: человек с полным доступом просит переслать, передать, упаковать '
    "или отправить файлы сотруднику («перешли ему», «запакуй и отправь зипом»). Код соберёт ZIP (если zip=true), "
    "отправит и доложит, когда Telegram подтвердит доставку. «Ему/ей» ищи в recent_cases и в разговоре. Бери все файлы, "
    "которые человек прислал для этого, а не один. ОБЕЩАНИЕ = ДЕЙСТВИЕ: не пиши «перешлю», «упакую», «отправлю», "
    "«передам», если в actions нет deliver_files (или другого действия, которое это делает); если не хватает данных "
    "(кому, какие файлы) — спроси. "
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
    '{"type":"approve_case","case_id":ID} и {"type":"cancel_case","case_id":ID}: руководитель прямо разрешил начать дело, '
    "переслать документ или повторить остановленный шаг из cases_awaiting_decision, либо отменил дело (если дел несколько и "
    "неясно какое, спроси). "
    "Дела ведёт код: если this_message_for_case задан, человек ответил или прислал документ по делу — коротко поблагодари и "
    "скажи, что передашь (кому — в forward_to); сам ничего не пересылай и не обещай сверх этого. Если в нём ambiguous_cases, "
    "спроси, к какому делу относится сообщение, и попроси ответить на твоё сообщение с просьбой. my_cases — дела ассистента, где участвует этот человек или которые он поручил: your_role, статус, last_events "
    "(что и когда было), waiting_for (кого и чего ждём). На вопросы «ну что там?», «как там?», «есть новости?», «где файлы?» "
    "отвечай по my_cases (и recent_cases, если есть): что уже сделано, чего ждёшь и от кого; если дел нет — так и скажи и "
    "спроси, о чём речь. Не выдумывай дел и событий, которых нет в данных. assistant_waiting_for_you — "
    "чего ты ждёшь от этого человека. file_received — человек прислал файл; он сохранён в библиотеке документов. "
    "files_received — человек прислал сразу несколько файлов (альбом): ответь один раз на все вместе. "
    "known_facts — найденное в памяти компании по этому сообщению (компания, отделы, люди, заметки с источником и "
    "уверенностью, фрагменты документов). На вопросы о компании, людях и документах отвечай по known_facts и данным "
    "выше; при низкой уверенности так и скажи; если ответа нет — честно скажи, что не знаешь, и предложи уточнить у "
    "руководителя, не выдумывай. skills — навыки компании для таких ситуаций: следуй им. Новое устойчивое знание о "
    "человеке или работе сохраняй действием note (только факты, без оценок людей). "

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
        "Если сотрудник спрашивает то, чего нет в данных, используй ask_reviewer и скажи, что уточняешь. "
        "Если сам решить не можешь, а человеку это нужно по работе — не спорь и не закрывай тему. "
        "Коротко уточни, чего именно не хватает, и сразу ask_reviewer руководителю/проверяющему: узнай у него. "
        "Так же, если просит выехать раньше, водителя или машину на другую дату, сдвинуть срок или состав: "
        "не утверждай, что «так решено и изменить нельзя». В message — кто просит, какая задача, что нужно, что в протоколе. "
        "Шутки про зарплату и прочее не по делу не эскалируй. Если ты уже "
        "ждёшь ответа проверяющего (waiting_for_reviewer), честно скажи об этом и что вернёшься с ответом. "
        "Сотруднику не раскрывай задачи и данные других людей. Данные команды (team) есть только у проверяющих и "
        "людей с полным доступом. При выборе исполнителя используй responsibilities (реальные обязанности и компетенции), " +
        "должность и текущие задачи. Если есть несколько людей с одинаковым именем, не выбирай только по имени: " +
        "сопоставь смысл поручения с обязанностями; если уверенного единственного совпадения нет, задай уточняющий вопрос " +
        "и не назначай задачу наугад. Простые действия, которые ты реально можешь выполнить доступными действиями, " +
        "не перекладывай на сотрудника только ради передачи: например, сведи данные в таблицу/сводку или подготовь график, " +
        "если для этого есть данные. "
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
    if kind in ("approve_case", "cancel_case"):
        if not reviewer:
            return f"{kind} доступно только руководителю"
        ids = {c["id"] for c in ctx.get("cases_awaiting_decision", [])}
        if kind == "cancel_case":
            return "" if isinstance(action.get("case_id"), int) else "cancel_case: укажи case_id"
        return "" if action.get("case_id") in ids else "approve_case: дело не найдено среди ждущих решения"
    if kind == "deliver_files":
        if not full:
            return "deliver_files доступно только проверяющему и людям с полным доступом"
        team = {p["id"]: p for p in ctx.get("team", [])}
        if action.get("to") not in team:
            return "deliver_files: to должен быть ID сотрудника из команды"
        if not team[action["to"]].get("reachable"):
            return f"deliver_files: у {team[action['to']]['name']} нет Telegram-контакта в справочнике"
        ids = action.get("document_ids")
        if not isinstance(ids, list) or not ids or not all(isinstance(x, int) and not isinstance(x, bool) for x in ids):
            return "deliver_files: document_ids — непустой список ID файлов"
        known = {d["id"] for d in ctx.get("recent_files", [])}
        known |= {d.get("document_id") for d in ctx.get("files_received", []) if isinstance(d, dict)}
        if isinstance(ctx.get("file_received"), dict):
            known.add(ctx["file_received"].get("document_id"))
        missing = [x for x in ids if x not in known]
        if missing:
            return f"deliver_files: файлы {missing[:5]} не найдены среди присланных этим человеком (recent_files)"
        if len(set(ids)) > cases.MAX_DELIVERY_FILES:
            return f"deliver_files: не больше {cases.MAX_DELIVERY_FILES} файлов за раз"
        if action.get("zip") not in (None, True, False):
            return "deliver_files: zip это true или false"
        return ""
    if kind == "team_report":
        if not full:
            return "team_report доступно только проверяющему и людям с полным доступом"
        scope = action.get("scope")
        return "" if scope == "all" or scope in {p["id"] for p in ctx.get("team", [])} else "team_report: scope all или ID сотрудника"
    return f"неизвестное действие {kind!r}"


def _denied(action: dict, employee, reviewer: bool, full: bool) -> bool:
    """The action needs more than this person is allowed (as opposed to a mistake in the action itself)."""
    kind = action.get("type")
    if kind in ("team_report", "assign_task", "deliver_files"):
        return not full
    if kind == "remind":
        return action.get("to") != "me" and not full
    if kind in ("relay_answer", "approve_meeting", "reject_meeting", "approve_case", "cancel_case"):
        return not reviewer
    if kind == "note":
        return action.get("employee_id") not in (None, employee.id if employee else None) and not full
    return False


# ----------------------------------------------------------------------------- «обещал — сделал»
_W = r"(?<![а-яёa-z])({})(?![а-яёa-z])"
PROMISES = (  # (words in the reply, actions that keep the promise, does a running case keep it)
    (_W.format("перешлю|передам|отправлю|скину|пришлю|упакую|запакую|заархивирую"),
     {"deliver_files", "relay_answer", "assign_task", "team_report"}, True),
    (_W.format("напомню"), {"remind"}, False),
    (_W.format("уточню|спрошу|узнаю|запрошу"), {"ask_reviewer", "assign_task", "relay_answer"}, False),
    (_W.format("подготовлю|соберу|сделаю"), {"team_report", "assign_task", "deliver_files", "remind"}, False),
)
_CONDITIONAL = re.compile(r"(?<![а-яё])(если|как только|когда|после того как|могу|можно|хочешь|нужно ли)(?![а-яё])")
# Employee asks to change logistics (car/driver/date), not a joke about pay.
_ORG_NEED = re.compile(
    r"(?i)(?<![а-яёa-z])("
    r"водитель|водител[яюем]|тачк[аиуе]|машин[аыуе]|авто|транспорт|"
    r"выехать|выезд|переезд|перевест"
    r")(?![а-яёa-z])"
)
_ORG_SHIFT = re.compile(
    r"(?i)(?<![а-яёa-z])("
    r"раньше|накануне|за сутки|сутки раньше|"
    r"на\s+\d{1,2}(?!\d)|первого|1[- ]?го|"
    r"сдвин|перенес|не успею|собрать вещи"
    r")"
)
_ORG_PAY_JOKE = re.compile(r"(?i)(?<![а-яёa-z])(зарплат|зп|оклад|надбавк)(?![а-яёa-z])")
_ORG_ASK = re.compile(r"(?i)(?<![а-яёa-z])(нужен|нужна|нужно|дайте|дать раньше)(?![а-яёa-z])")


def organizational_change_request(text: str) -> bool:
    """True when the employee asks to move a trip/car/driver, not when they joke about pay."""
    raw = (text or "").strip()
    if not raw or _ORG_PAY_JOKE.search(raw):
        return False
    shift = bool(_ORG_SHIFT.search(raw))
    need = bool(_ORG_NEED.search(raw) or (shift and _ORG_ASK.search(raw)))
    return need and shift


def _sentences(text: str) -> list:
    return [x.strip() for x in re.split(r"(?<=[.!?…])\s+|\n+", text or "") if x.strip()]


def unbacked_promises(reply: str, actions: list, ctx: dict) -> list:
    """Sentences where the assistant promises to do something («перешлю», «напомню») with no action that does it.
    Offers and conditions («могу переслать», «если пришлёшь — передам») are not promises."""
    kinds = {a.get("type") for a in actions or [] if isinstance(a, dict)}
    case_running = bool(ctx.get("this_message_for_case"))
    bad = []
    for sentence in _sentences(reply):
        low = sentence.lower()
        if _CONDITIONAL.search(low):
            continue
        for pattern, keeps, by_case in PROMISES:
            found = re.search(pattern, low)
            if not found or re.search(r"(?<![а-яё])не\s+" + re.escape(found.group(1)), low):
                continue
            if kinds & keeps or (by_case and case_running):
                continue
            bad.append(sentence)
            break
    return bad


def _drop_sentences(reply: str, bad: list) -> str:
    kept = [x for x in _sentences(reply) if x not in bad]
    return " ".join(kept).strip() or "Понял. Уточни, пожалуйста, что именно сделать и кому."


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
        elif kind == "deliver_files":
            s.commit()
            try:
                case_id = cases.start_delivery(author or "руководителя", action["to"], action["document_ids"],
                                               bool(action.get("zip")), str(action.get("archive_name") or ""),
                                               str(action.get("note") or ""), requested_chat=chat)
            except cases.CaseError as exc:
                raise AgentError(f"deliver_files: {exc}") from exc
            if _testing():
                cases.advance(case_id)
            else:
                cases._pool().submit(cases.advance, case_id)
        elif kind in ("approve_case", "cancel_case"):
            s.commit()
            if kind == "cancel_case":
                cases.cancel(action["case_id"], author)
            elif not cases.approve(action["case_id"], author):
                cases.retry_step(action["case_id"], author)
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
            if (employee is not None and not reviewer and not full
                    and organizational_change_request(text)
                    and not any(a.get("type") == "ask_reviewer" for a in decision["actions"])):
                errors.append(
                    "человек просит то, что ты сам решить не можешь (водитель, машина, выезд раньше): "
                    "нужен ask_reviewer руководителю/проверяющему, узнай у него, не закрывай тему сам"
                )
            stiff = voice.officialese(decision["reply"]) if attempt == 1 else []
            promises = unbacked_promises(decision["reply"], decision["actions"], ctx)
            if not errors and not stiff and not promises:
                break
            if not errors:
                if attempt == 2:  # a promise nobody will keep is not sent: those sentences are dropped
                    log.warning("message %s: unbacked promise dropped: %s", row_id, promises)
                    decision["reply"] = _drop_sentences(decision["reply"], promises)
                    break
                feedback = ""
                if stiff:  # the style: one rewrite, then the reply goes as it is
                    feedback += (f"\nОтвет звучит как служебное письмо ({', '.join(stiff)}). Перепиши reply проще, как "
                                 "коллега в мессенджере; actions и memory оставь.")
                if promises:
                    feedback += ("\nВ reply ты обещаешь сделать: «" + "»; «".join(promises) + "», но в actions нет действия, "
                                 "которое это делает. Либо добавь нужное действие (deliver_files, remind, ask_reviewer, "
                                 "assign_task, team_report), либо убери обещание и честно скажи, что нужно, чтобы это "
                                 "сделать (кому, какие файлы, когда).")
                continue
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
