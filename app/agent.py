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

from . import (access, agent_skills, cases, deadlines, dialog_memory, doc_intake, doc_templates, entities,
               memory as company_memory, llm, orders, reminders, settings_store, transcribe, userbot, voice)
from .db import (ConversationEscalation, ConversationMessage, Document, Employee, Meeting, Note, PersonMemory, SessionLocal,
                 Task, now)

log = logging.getLogger("sysai.agent")

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


def _departments_view(p) -> list:
    return [{"name": m.department.name, "role": m.role or "", "responsibilities": m.responsibilities or ""}
            for m in p.department_memberships if m.department is not None and m.department.active]


def _refer_to(s, employee) -> list:
    """Whom to send a person with a request beyond their rights: the heads of their departments, otherwise the reviewers
    (the director is never named)."""
    out, names = [], set()
    for m in (employee.department_memberships if employee is not None else []):
        department = m.department
        if department is None or not department.active:
            continue
        head = next((x for x in department.memberships if x.is_primary and x.employee is not None and x.employee.active
                     and x.employee_id != employee.id), None)
        if head is not None and head.employee.name not in names and access.level_of(head.employee) != "director":
            out.append({"name": head.employee.name, "role": f"руководитель: {department.name}"})
            names.add(head.employee.name)
    if not out:
        for e in s.query(Employee).filter_by(active=True).order_by(Employee.name).all():
            if (employee is None or e.id != employee.id) and access.level_of(e) == "reviewer":
                out.append({"name": e.name, "role": "проверяющий"})
            if len(out) >= 3:
                break
    return out


def _context(s, row, result: dict, employee, reviewer: bool, full: bool, director: bool = False,
             director_mode: bool = False, person=None) -> dict:
    chat = row.chat_id
    person = person or agent_skills.person_of(s, employee, reviewer, full, director)
    # The whole conversation (dialog_memory.py): the fresh part word for word, a retelling of the older part, and the
    # old messages this one seems to speak about.
    talk = dialog_memory.window(s, chat, row.id)
    history = talk["fresh"]
    last = _aware(history[-1].created_at) if history and history[-1].created_at else None
    minutes = int((now() - last).total_seconds() // 60) if last else None
    memory = s.get(PersonMemory, chat)
    ctx = {"today": deadlines.today().isoformat(), "now": reminders.now_view(), "sender_name": result.get("name") or None,
           "voice_message": bool(result.get("voice")), "voice_problem": result.get("voice_problem") or None,
           "roles": [r for r, on in (("employee", employee is not None), ("full_access", full and not reviewer),
                                     ("reviewer", reviewer), ("director", director)) if on],
           "director_mode": director_mode,
           "conversation": {"already_talking": minutes is not None and minutes < TALKING_HOURS * 60,
                            "minutes_since_last_message": minutes, "messages_so_far": talk["total"]},
           "memory": (memory.summary if memory else "") or None}
    older = talk["total"] - len(history)
    if older > 0:
        ctx["conversation"]["older_not_shown"] = older
        if talk["summary"]:
            ctx["dialog_memory"] = talk["summary"]
        earlier = dialog_memory.related(s, chat, getattr(row, "text", "") or "", history[0].id if history else row.id)
        if earlier:
            ctx["from_earlier"] = earlier
    ctx["history"] = [{"from": "человек" if h.role == "user" else "ты", "text": dialog_memory.clip(h.text)}
                      for h in history]
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
        address = voice.address_of(employee)
        if address:
            ctx["employee"]["address"] = address  # how to address this person (the card)
        meeting_ids = list(dict.fromkeys(t.meeting_id for t in tasks if t.meeting_id))[:4]
        ctx["meetings"] = [_meeting_view(m) for m in (s.get(Meeting, i) for i in meeting_ids) if m]
        ctx["waiting_for_reviewer"] = [{"id": e.id, "question": e.reason} for e in
                                       s.query(ConversationEscalation).filter_by(chat_id=chat, status="open").all()]
        ctx["notes"] = _notes_view(s, employee.id)
        ctx["assistant_waiting_for_you"] = cases.waiting_view(employee.id)
    ctx["my_cases"] = cases.person_view(employee.id if employee is not None else None, chat)
    # What the company memory knows about this message: an employee sees only their own card and notes.
    query = " ".join(h.text for h in history[-2:] if h.role == "user")[-600:] + " " + (getattr(row, "text", "") or "")
    ctx["known_facts"] = company_memory.for_prompt(query, viewer=None if full else (employee.id if employee else -1))
    about = entities.for_prompt(s, query, full)
    if about:
        ctx["about_entities"] = about
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
                        "departments": _departments_view(p),
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
    scope = person.order_scope
    if not full and scope in ("all", "department"):  # people they may give orders to (department: with their tasks)
        people = [p for p in s.query(Employee).filter_by(active=True).order_by(Employee.name).all()
                  if scope == "all" or p.id in person.team]
        tasks = (s.query(Task).filter(Task.assignee_id.in_([p.id for p in people]), Task.executor != "assistant",
                                      Task.status.in_(OPEN_STATUSES)).order_by(Task.id.desc()).limit(200).all()
                 if scope == "department" and people else [])
        ctx["team"] = [{"id": p.id, "name": p.name, "position": p.position or None,
                        "department": getattr(p, "department", "") or None,
                        "responsibilities": getattr(p, "responsibilities", "") or None,
                        "departments": _departments_view(p), "reachable": reminders.reachable(p),
                        **({"open_tasks": [_task_view(t, False) for t in tasks if t.assignee_id == p.id][:8]}
                           if scope == "department" else {})} for p in people]
    ctx["order_scope"] = scope
    if scope != "all":
        hint = _refer_to(s, employee)
        if hint:
            ctx["refer_to"] = hint
    off = [agent_skills.SKILLS[n].title for n in agent_skills.SKILLS  # the card and the Skills page
           if n not in agent_skills.PAIRED and not agent_skills.allowed_skill(n, person)]
    if off:
        ctx["skills_off"] = off
    waiting_order = orders.pending_view(s, chat)
    if waiting_order:
        ctx["order_to_confirm"] = waiting_order
    if agent_skills.allowed_skill("make_document", person):  # the templates of the panel and the draft waiting for «ок»
        templates = doc_templates.view(s)
        if templates:
            ctx["document_templates"] = templates
        draft = doc_templates.pending_view(s, chat)
        if draft:
            ctx["document_draft"] = draft
    silences = cases.silence_view({"user_id": chat, "username": result.get("username") or "",
                                   "phone": result.get("phone") or ""})
    if silences:
        ctx["silence_to_decide"] = silences  # you told this person that somebody is silent: their answer decides
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


FORMAT_JSON = ('Формат ответа: {"reply": "что написать человеку или пустая строка", "actions": [...], '
               '"memory": "необязательно"}. ')
ASSIGN_RULES = (
    "При назначении задачи сначала учитывай прямое указание директора; если исполнителя не назвали, сравнивай смысл "
    "задачи с department, competencies, responsibilities и context каждого сотрудника. Не выбирай только по имени или "
    "должности. Если подходят несколько сотрудников либо данных мало, задай директору один конкретный уточняющий вопрос "
    "и не создавай назначение наугад. Не выдумывай компетенции и загрузку. ")
ORDER_RULES = (
    "Поручения из чата: новую работу коллеге (сделать к сроку, узнать у него, запросить документ) оформляй действием "
    "order, а не обещанием. Сначала убедись, что ясно кому, что и (для задачи) к какому сроку; если не хватает одного "
    "из этого — задай один короткий уточняющий вопрос. С order в reply перескажи поручение и спроси подтверждение "
    "(«Понял: Виктору — купить перфоратор до 14.11. Делаю?»); пока человек не ответил «да», не пиши, что поручение "
    "передано. order_to_confirm — твой пересказ, который ждёт ответа: «да» — confirm_order с ok=true, «нет» — ok=false, "
    "поправка («нет, до среды», «лучше Ольге») — новый order с исправлением и снова «Делаю?». После confirm_order "
    "коротко скажи, кому передал и что сообщишь, когда будет готово. assistant_waiting_for_you с expects=done — "
    "поручение, которое выполняет этот человек: когда он пишет, что сделал или что мешает, отметь task_status по task_id. "
    "silence_to_decide — дела, по которым ты сообщил этому человеку, что сотрудник молчит: «напомни ещё», «займусь "
    "сам», «отмени» — это silence_choice с case_id этого дела. ")
DOC_RULES = (
    "Документы по шаблону: document_templates — шаблоны компании (письма, акты, служебки) с полями fields. Просьбу "
    "подготовить такой документ выполняй действием make_document: значения всех полей бери из сообщения, разговора, "
    "known_facts и team (hint подсказывает, что в поле), не выдумывай; если чего-то нет — задай один короткий вопрос о "
    "недостающем без действия. С make_document в reply коротко скажи, что ниже черновик, и попроси проверить: «ок» — "
    "сохранишь (и отправишь, если назвали кому), или пусть напишет, что поправить. document_draft — черновик, который "
    "ждёт ответа: «ок» — confirm_document с ok=true, «не надо» — ok=false, поправка — новый make_document с "
    "исправленными values. Если подходящего шаблона нет, так и скажи и не обещай документ. "
    "Входящие документы: intake в file_received (и в каждом из files_received) — что код разобрал в файле: doc_type, "
    "requisites (номер, дата, контрагент, УНП, суммы — только подтверждённое цитатой из файла), deadlines (сроки), "
    "problems (не сходятся суммы или НДС, неверный УНП). Если человек прислал документ без вопроса — одной-двумя "
    "фразами скажи, что это (тип, номер, от кого, сумма), и назови problems, если они есть; ничего не добавляй от себя. "
    "route.to — кому код сам передаст файл по правилам компании после твоего ответа (route.tasks — и поставит задачи по "
    "срокам с напоминанием): так и скажи, deliver_files для этого не нужен. Если человек сам сказал, кому передать, — "
    "deliver_files по его словам, правило тогда не сработает. route.to нет — не обещай передать; если неясно, что с "
    "файлом делать, спроси. intake.pending — файл ещё разбирается: скажи, что посмотришь и напишешь, без «передам». ")
GENERAL_RULES = ORDER_RULES + DOC_RULES + (
    "Память разговора: history — последние сообщения целиком; dialog_memory — краткий пересказ более ранней части "
    "разговора; from_earlier — старые сообщения, которые код нашёл по словам этого сообщения; recalled — что нашлось, "
    "когда ты сам попросил вспомнить. На «как договаривались», «тот акт», «как в прошлый раз» отвечай по ним и "
    "называй, когда это было. Если нужного там нет, а conversation.older_not_shown больше нуля, попроси вспомнить "
    "(поиск по всей истории чата); если не нашлось и там, честно скажи, что не помнишь, и попроси напомнить. Не "
    "выдумывай того, чего не было в разговоре. "
    "Дела ведёт код: если this_message_for_case задан, человек ответил или прислал документ по делу — коротко "
    "поблагодари и скажи, что передашь (кому — в forward_to); сам ничего не пересылай и не обещай сверх этого. Если "
    "в нём ambiguous_cases, спроси, к какому делу относится сообщение, и попроси ответить на твоё сообщение с "
    "просьбой. my_cases — дела ассистента, где участвует этот человек или которые он поручил: your_role, статус, "
    "last_events (что и когда было), waiting_for (кого и чего ждём). На вопросы «ну что там?», «как там?», «есть "
    "новости?», «где файлы?» отвечай по my_cases (и recent_cases, если есть): что уже сделано, чего ждёшь и от "
    "кого; если дел нет — так и скажи и спроси, о чём речь. Не выдумывай дел и событий, которых нет в данных. "
    "assistant_waiting_for_you — чего ты ждёшь от этого человека. file_received — человек прислал файл; он сохранён "
    "в библиотеке документов. files_received — человек прислал сразу несколько файлов (альбом): ответь один раз на "
    "все вместе. known_facts — найденное в памяти компании по этому сообщению (компания, отделы, люди, заметки с "
    "источником и уверенностью, фрагменты документов). На вопросы о компании, людях и документах отвечай по "
    "known_facts и данным выше; при низкой уверенности так и скажи; если ответа нет — честно скажи, что не знаешь, "
    "и предложи уточнить у руководителя, не выдумывай. about_entities — карточки объектов и клиентов, о которых "
    "говорит сообщение: другие написания, УНП, адрес, контакты, facts (у каждого источник), а для тех, кто видит "
    "команду, — совещания, открытые задачи и документы по ним. Отвечай в контексте этого объекта или клиента и называй "
    "источник («по совещанию 12.11», «в счёте № 15»); чего в карточке нет, не выдумывай. "
    "skills — навыки компании для таких ситуаций: следуй им. "
    "Новое устойчивое знание о человеке или работе сохраняй действием note (только факты, без оценок людей). "
    "skills_with_approval — действия, которые для этого человека выполняются только после одобрения руководителя "
    "(или только предлагаются ему): проси их как обычно, но в reply не говори, что уже сделал, а скажи, что передал "
    "руководителю на одобрение. ")
MEMORY_JSON = ("Поле memory: если узнал что-то важное о человеке или его работе, верни обновлённую краткую память (до 1200 "
               "символов, сохрани важное из прошлой памяти), иначе не добавляй поле.")
RECALL_JSON = ('{"type":"recall","query":"что искать"}: вспомнить — найти в полной истории этого чата старые сообщения, '
               "когда нужного нет в history, dialog_memory и from_earlier; reply тогда оставь пустым: найденное придёт "
               "в recalled, и тебя попросят ответить ещё раз; ")
# The skills (agent_skills.py) in the JSON answer: what the model may ask for and when.
ACTIONS_HELP = (FORMAT_JSON + ASSIGN_RULES + "Действия (ID бери только из данных; код проверяет права и ID): "
                + agent_skills.json_help() + RECALL_JSON + GENERAL_RULES + MEMORY_JSON)
# The same skills as function calls (OpenRouter tools); the reply is the text of the answer.
TOOLS_HELP = (
    ASSIGN_RULES +
    "Действия выполняй вызовом функций (tools); ID бери только из данных, код проверит права и ID. Здесь actions — "
    "это вызовы функций, а reply — текст твоего ответа человеку: пиши его обычным текстом в том же ответе (пустой "
    "текст, если отвечать не нужно). ОБЕЩАНИЕ = ДЕЙСТВИЕ: не пиши «перешлю», «напомню», «уточню», «передам», если не "
    "вызываешь функцию, которая это делает; если не хватает данных — спроси. Функций, которых нет в списке, у этого "
    "человека нет: такие просьбы мягко отклоняй, как сказано выше. "
    + GENERAL_RULES +
    "Вспомнить — функция recall_conversation. Если узнал что-то важное о человеке или его работе, вызови update_memory "
    "с обновлённой краткой памятью.")
_NO_TOOLS = set()  # models whose providers refused function calls in this process: they answer in JSON


def _system(transport: str = "json") -> str:
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
        "Сотруднику не раскрывай задачи и данные других людей. Данные команды (team) есть только у проверяющих, "
        "людей с полным доступом и у тех, кто может поручать другим (order_scope all или department: тогда team — "
        "те, кому они могут поручать). При выборе исполнителя используй responsibilities (реальные обязанности и компетенции), " +
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
        "Поручения (order) и напоминания другим людям, кроме того, ограничены order_scope этого человека: all — любому "
        "из team, department — только людям из team (это его отдел), self — только себе (to=\"me\"); в этих пределах "
        "выполняй их для любого человека, за пределами — нет. skills_off — что для этого человека выключено: такие "
        "просьбы не выполняй. "
        "Откажи мягко, коротко и своими словами, чтобы человек не почувствовал стену: скажи, что такие просьбы ты по "
        "запросам коллег не выполняешь и с ними лучше к руководителю (если есть refer_to — назови, к кому именно), и "
        "сразу предложи то, что можешь сделать для него самого (его задачи, статусы, напоминание ему самому). Не пиши "
        "«нет доступа», «недостаточно прав», «запрещено», «не уполномочен», не объясняй, как устроены роли, и не "
        "называй, кто директор. "
        "Если voice_message=true, текст это автоматическая расшифровка голосового сообщения: возможны ошибки в именах, "
        "числах и времени. Когда от точности зависит действие (кому писать, во сколько напомнить), назови в reply, что "
        "именно ты понял и сделал, чтобы человек мог поправить. Если voice_problem задан (too_long, failed, empty), текст "
        "в скобках это пометка системы, а не слова человека: скажи по-человечески, что голосовое не удалось разобрать, и "
        "попроси повторить или написать. "
        + (TOOLS_HELP if transport == "tools" else ACTIONS_HELP))
    return voice.system_prompt(task)


def _user_message(ctx: dict, text: str, feedback: str) -> str:
    return "<data>" + json.dumps(ctx, ensure_ascii=False) + "</data>\nСообщение человека:\n<data>" + text + "</data>" + feedback


def _decision(data) -> dict:
    if not isinstance(data, dict):
        raise AgentError("модель вернула не объект")
    reply, actions, memory = data.get("reply"), data.get("actions") or [], data.get("memory")
    if (reply is not None and not isinstance(reply, str)) or not isinstance(actions, list) \
            or (memory is not None and not isinstance(memory, str)):
        raise AgentError("модель вернула ответ в неверной форме")
    return {"reply": (reply or "").strip()[:MAX_REPLY],
            "actions": [agent_skills.normalize(a) for a in actions if isinstance(a, dict)],
            "memory": (memory or "").strip()[:1200]}


TOOL_ACCEPTED = json.dumps({"ok": True, "note": "принято: код проверит права и выполнит после ответа"}, ensure_ascii=False)
RECALL_NAME = agent_skills.RECALL_TOOL["function"]["name"]
RECALL_DONE = json.dumps({"ok": False, "note": "уже искал в истории чата: ответь по найденному"}, ensure_ascii=False)
RECALLED_NOTE = ("\nТы попросил вспомнить: найденное в полной истории чата — в recalled (пустой found — ничего не "
                 "нашлось). Теперь ответь человеку целиком с учётом найденного; recall больше не проси.")


def _take_recall(decision: dict):
    """Takes the requests to remember out of the actions: what to look for, or None when there are none."""
    asked = [a for a in decision["actions"] if a.get("type") == "recall"]
    decision["actions"] = [a for a in decision["actions"] if a.get("type") != "recall"]
    return " ".join(str(a.get("query") or "").strip() for a in asked).strip()[:300] if asked else None


def _recall(ctx: dict, query: str, recall) -> dict:
    """Looks through the whole chat once per message; what was found stays in ctx (a second try sees it too)."""
    found = []
    if recall is not None and query:
        try:
            found = recall(query) or []
        except Exception as exc:  # noqa: BLE001  nothing found is an answer too
            log.warning("recall %r failed: %s", query, exc)
    ctx["recalled"] = {"query": query, "found": found}
    return ctx["recalled"]


def _ask_json(system: str, ctx: dict, text: str, feedback: str, model: str) -> dict:
    messages = [{"role": "system", "content": system}, {"role": "user", "content": _user_message(ctx, text, feedback)}]
    return _decision(llm.parse_json(llm.chat(model, messages, json_mode=True, temperature=0.6, max_tokens=1800,
                                             timeout=90)))


def _decide_json(system: str, ctx: dict, text: str, feedback: str, model: str, recall) -> dict:
    decision = _ask_json(system, ctx, text, feedback, model)
    query = _take_recall(decision)
    if query is None or "recalled" in ctx:
        return decision
    _recall(ctx, query, recall)
    again = _ask_json(system, ctx, text, feedback + RECALLED_NOTE, model)
    _take_recall(again)
    again["memory"] = again["memory"] or decision["memory"]
    again["recalled"] = query
    return again


def _as_call(call: dict) -> dict:
    return {"id": call["id"], "type": "function", "function": {"name": call["name"], "arguments": call["arguments"]}}


def _query_of(calls: list) -> str:
    words = []
    for call in calls:
        raw = call.get("arguments")
        try:
            args = json.loads(raw) if isinstance(raw, str) and raw.strip() else (raw if isinstance(raw, dict) else {})
        except json.JSONDecodeError:
            args = {}
        words.append(str(args.get("query") or "").strip() if isinstance(args, dict) else "")
    return " ".join(w for w in words if w)[:300]


def _unique(actions: list) -> list:
    seen, out = set(), []
    for action in actions:
        mark = json.dumps(action, ensure_ascii=False, sort_keys=True, default=str)
        if mark not in seen:
            seen.add(mark)
            out.append(action)
    return out


def _decide_tools(system: str, ctx: dict, text: str, feedback: str, model: str, person, recall) -> dict:
    messages = [{"role": "system", "content": system}, {"role": "user", "content": _user_message(ctx, text, feedback)}]
    tools = agent_skills.tools(person or agent_skills.Person())
    if "recalled" not in ctx:
        tools = tools + [agent_skills.RECALL_TOOL]
    actions, memory, reply, recalled = [], "", "", None
    for _ in range(2):  # the answer, or a look through the chat and then the answer
        out = llm.chat(model, messages, tools=tools, tool_choice="auto", temperature=0.6, max_tokens=1800, timeout=90)
        if isinstance(out, str):  # the model answered in JSON anyway
            decision = _decision(llm.parse_json(out))
            _take_recall(decision)
            decision["actions"] = _unique(actions + decision["actions"])
            decision["memory"] = decision["memory"] or memory[:1200]
            if recalled is not None:
                decision["recalled"] = recalled
            return decision
        calls = out.get("tool_calls") or []
        asked = [c for c in calls if c["name"] == RECALL_NAME]
        found_actions, found_memory = agent_skills.from_calls([c for c in calls if c["name"] != RECALL_NAME])
        actions += found_actions
        memory = found_memory or memory
        reply = str(out.get("content") or "").strip()
        if not asked or recalled is not None or "recalled" in ctx:
            break
        recalled = _query_of(asked)
        found = _recall(ctx, recalled, recall)
        messages.append({"role": "assistant", "content": out.get("content") or "", "tool_calls": [_as_call(c) for c in calls]})
        messages += [{"role": "tool", "tool_call_id": c["id"],
                      "content": (json.dumps(found, ensure_ascii=False) if c["name"] == RECALL_NAME else TOOL_ACCEPTED)}
                     for c in calls]
        tools = [t for t in tools if t["function"]["name"] != RECALL_NAME]
        calls, reply = [], ""  # the answer comes once the model has read what was found
    if calls and not reply:  # only the calls came: the model writes the reply once it sees them accepted
        messages.append({"role": "assistant", "content": "", "tool_calls": [_as_call(c) for c in calls]})
        messages += [{"role": "tool", "tool_call_id": c["id"],
                      "content": TOOL_ACCEPTED if c["name"] != RECALL_NAME else RECALL_DONE} for c in calls]
        again = llm.chat(model, messages, tools=tools, tool_choice="none", temperature=0.6, max_tokens=1200, timeout=90)
        if isinstance(again, str):
            try:
                parsed = llm.parse_json(again)
                again = parsed.get("reply", again) if isinstance(parsed, dict) else again
            except llm.LLMError:
                pass
            reply = str(again or "").strip()
        else:
            reply = str(again.get("content") or "").strip()
    decision = {"reply": reply[:MAX_REPLY], "actions": _unique(actions), "memory": memory[:1200]}
    if recalled is not None:
        decision["recalled"] = recalled
    return decision


def _decide(system: str, ctx: dict, text: str, feedback: str = "", *, model: str | None = None,
            transport: str = "json", person=None, recall=None) -> dict:
    """One answer of the model: {"reply", "actions", "memory"} (actions are not checked here). `recall(query)` looks
    through the whole history of the chat when the model asks to remember (once per message)."""
    model = model or voice.model()
    try:
        if transport != "tools":
            return _decide_json(system, ctx, text, feedback, model, recall)
        return _decide_tools(system, ctx, text, feedback, model, person, recall)
    except llm.ToolsUnsupported:
        raise
    except llm.LLMError as exc:
        raise AgentError(f"модель недоступна: {str(exc)[:200]}") from exc


# ----------------------------------------------------------------------------- checking actions (agent_skills.py)
def _person(employee, reviewer: bool, full: bool, director: bool = False):
    return agent_skills.Person(employee=employee, reviewer=bool(reviewer), full=bool(full), director=bool(director))


def _known_task_ids(ctx: dict, *, own_only: bool = False) -> set:
    return agent_skills._known_task_ids(ctx, own_only=own_only)


def _check(action: dict, ctx: dict, employee, reviewer: bool, full: bool) -> str:
    """Returns an error text or "" when the action may run."""
    return agent_skills.check(action, ctx, _person(employee, reviewer, full))


def _denied(action: dict, employee, reviewer: bool, full: bool) -> bool:
    """The action needs more than this person is allowed (as opposed to a mistake in the action itself)."""
    return agent_skills.denied(action, _person(employee, reviewer, full))


# ----------------------------------------------------------------------------- «обещал — сделал»
_W = r"(?<![а-яёa-z])({})(?![а-яёa-z])"
PROMISES = (  # (words in the reply, actions that keep the promise, does a running case keep it)
    (_W.format("перешлю|передам|отправлю|скину|пришлю|упакую|запакую|заархивирую"),
     {"deliver_files", "relay_answer", "assign_task", "team_report", "order", "confirm_order", "make_document",
      "confirm_document"}, True),
    (_W.format("напомню"), {"remind", "order", "confirm_order"}, False),
    (_W.format("уточню|спрошу|узнаю|запрошу"), {"ask_reviewer", "assign_task", "relay_answer", "order", "confirm_order"},
     False),
    (_W.format("подготовлю|соберу|сделаю|составлю"), {"team_report", "assign_task", "deliver_files", "remind",
                                                      "make_document"}, False),
    (_W.format("поручу|назначу|поставлю задачу"), {"order", "confirm_order", "assign_task"}, False),
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
    for received in [ctx.get("file_received")] + list(ctx.get("files_received") or []):  # the code routes these itself
        route = ((received or {}).get("intake") or {}).get("route") or {} if isinstance(received, dict) else {}
        if route.get("to") and not route.get("done") and not route.get("already_theirs"):
            kinds.add("deliver_files")
        if route.get("tasks"):
            kinds |= {"assign_task", "remind"}
    orders_of_person = {w.get("task_id") for w in ctx.get("assistant_waiting_for_you") or []
                        if isinstance(w, dict) and w.get("expects") == "done"}
    # «передам» after «сделал» on an order from the chat is kept by the case: it reports to whoever gave the order
    case_running = bool(ctx.get("this_message_for_case")) or any(
        a.get("type") == "task_status" and a.get("task_id") in orders_of_person and a.get("status") in ("done", "blocked")
        for a in actions or [] if isinstance(a, dict))
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
                  "лучше к руководителю (если есть refer_to — назови, к кому), и предложи, чем можешь помочь этому "
                  "человеку сам. Без слов «нет доступа», «нет прав», «запрещено».")
ORDER_CONFIRM = ("\nВ actions есть order, но reply не спрашивает подтверждения. Перескажи поручение коротко (кому, что, "
                 "срок) и спроси «Делаю?»: без «да» оно не начнётся.")


# ----------------------------------------------------------------------------- applying actions (agent_skills.py)
def _apply(action: dict, chat: str, employee_id, author: str, sender: dict | None = None, full: bool = False) -> None:
    agent_skills.execute(action, chat, employee_id, author, sender, full=full)


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


def decide(ctx: dict, text: str, person, *, model: str | None = None, transport: str | None = None,
           approvers=None, label: str = "", recall=None) -> dict:
    """The model's answer to one message, checked: every action against the data and the person's rights, promises
    against the actions, the style once. A wrong answer is explained to the model once; still wrong: AgentError.
    The real conversation and the checking dialogs (agent_checks.py) both answer through here. `recall(query)` finds
    old messages of the chat when the model asks to remember (dialog_memory.recall; nothing found when not given)."""
    model = model or voice.model()
    transport = transport or agent_skills.transport()
    if transport == "tools" and model in _NO_TOOLS:
        transport = "json"
    employee, reviewer, full = person.employee, person.reviewer, person.full
    feedback = ""
    for attempt in (1, 2):
        try:
            decision = _decide(_system(transport), ctx, text, feedback, model=model, transport=transport, person=person,
                               recall=recall)
        except llm.ToolsUnsupported as exc:
            log.info("%s answers in JSON: %s", model, exc)
            _NO_TOOLS.add(model)
            transport = "json"
            decision = _decide(_system(transport), ctx, text, feedback, model=model, transport=transport, person=person,
                               recall=recall)
        decision["actions"] = [agent_skills.personal(a, person) for a in decision["actions"]]
        errors = [e for e in (agent_skills.check(a, ctx, person, approvers) for a in decision["actions"]) if e]
        if (employee is not None and not reviewer and not full
                and organizational_change_request(text)
                and not any(a.get("type") == "ask_reviewer" for a in decision["actions"])):
            errors.append(
                "человек просит то, что ты сам решить не можешь (водитель, машина, выезд раньше): "
                "нужен ask_reviewer руководителю/проверяющему, узнай у него, не закрывай тему сам"
            )
        stiff = voice.officialese(decision["reply"]) if attempt == 1 else []
        if attempt == 1 and voice.questions(decision["reply"]) > 1:
            stiff.append("несколько вопросов сразу")
        if attempt == 1 and len(decision["reply"]) > voice.LONG_REPLY:
            stiff.append("слишком длинно")
        promises = unbacked_promises(decision["reply"], decision["actions"], ctx)
        unconfirmed = any(a.get("type") == "order" for a in decision["actions"]) and "?" not in decision["reply"]
        if not errors and not stiff and not promises and not unconfirmed:
            break
        if not errors:
            if attempt == 2:
                if promises:  # a promise nobody will keep is not sent: those sentences are dropped
                    log.warning("%s: unbacked promise dropped: %s", label or "message", promises)
                    decision["reply"] = _drop_sentences(decision["reply"], promises)
                if unconfirmed:  # an order is never started without the person's «да»
                    decision["reply"] = (decision["reply"].rstrip() + " Делаю?").strip()
                break
            feedback = ORDER_CONFIRM if unconfirmed else ""
            if stiff:  # the style: one rewrite, then the reply goes as it is
                feedback += (f"\nОтвет написан тяжело ({', '.join(stiff)}). Перепиши reply проще и короче, как "
                             "коллега в мессенджере, с одним вопросом за раз; actions и memory оставь.")
            if promises:
                feedback += ("\nВ reply ты обещаешь сделать: «" + "»; «".join(promises) + "», но в actions нет действия, "
                             "которое это делает. Либо добавь нужное действие (deliver_files, remind, ask_reviewer, order, "
                             "assign_task, team_report), либо убери обещание и честно скажи, что нужно, чтобы это "
                             "сделать (кому, какие файлы, когда).")
            continue
        if attempt == 2:
            raise AgentError("; ".join(errors)[:300])
        feedback = "\nПрошлый ответ нельзя выполнить: " + "; ".join(errors) + ". Исправь ответ."
        if any(agent_skills.denied(a, person) for a in decision["actions"]):
            feedback += DECLINE_GENTLY
    decision["transport"] = transport
    return decision


def _received_ids(result: dict) -> list:
    docs = list(result.get("documents") or []) or ([result["document"]] if result.get("document") else [])
    return [d["document_id"] for d in docs if isinstance(d, dict) and d.get("document_id")]


def _prepare_documents(row_id: int) -> None:
    """The files of this message are read and analyzed before the answer (doc_intake.py): the model can say what they
    are, what does not add up and where they go. A file answering a case is the case's business."""
    with SessionLocal() as s:
        result = dict(s.get(ConversationMessage, row_id).result or {})
    ids = _received_ids(result)
    if not ids or result.get("decision") is not None or result.get("case") or result.get("documents_prepared"):
        return
    try:
        views = doc_intake.prepare(ids, row_id)
    except Exception:  # noqa: BLE001  the answer goes without the analysis
        log.exception("message %s: files not analyzed", row_id)
        views = {}
    for d in [result.get("document")] + list(result.get("documents") or []):
        if isinstance(d, dict) and views.get(d.get("document_id")):
            d["intake"] = views[d["document_id"]]
    result["documents_prepared"] = True
    _store(row_id, result)


def _process(row_id: int) -> None:
    from . import assistant
    _prepare_documents(row_id)
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
        author = result.get("name") or (employee.name if employee else "")
        person = agent_skills.person_of(s, employee, reviewer, full, director)  # with the rights of the employee card
        decision = result.get("decision")
        ctx = (_context(s, row, result, employee, reviewer, full, director, director_mode, person=person)
               if decision is None else None)
    if decision is None:  # 2. decide (checked actions; a bad answer is explained to the model once)
        ctx.update(agent_skills.approval_hint(person))
        model = voice.model()
        decision = decide(ctx, text, person, model=model, label=f"message {row_id}",
                          recall=lambda query: dialog_memory.recall(chat, query, row_id))
        result["decision"], result["model"], result["transport"] = decision, model, decision.get("transport")
        _store(row_id, result)
    if not result.get("actions_done"):  # 3. act (each action once, even when a later one fails and all is retried)
        sender = {"user_id": chat, "username": result.get("username", ""), "phone": result.get("phone", "")}
        outcomes = list(result.get("outcomes") or [])
        for action in decision["actions"][len(outcomes):]:
            outcomes.append(agent_skills.run(action, chat, person, author, sender))
            result["outcomes"] = outcomes
            _store(row_id, result)
        _save_memory(chat, person.employee_id, decision.get("memory", ""))
        result["actions_done"] = True
        _store(row_id, result)
    if decision["reply"] and not result.get("reply_sent"):  # 4. answer
        sent = userbot.send_message(chat, decision["reply"], answering=True)
        with SessionLocal() as s:
            s.add(ConversationMessage(chat_id=chat, message_id=sent["message_id"], role="assistant", status="agent_done",
                                      text=decision["reply"][:4000], result={}))
            try:
                s.commit()
            except IntegrityError:
                s.rollback()
        result["reply_sent"] = True
        _store(row_id, result)
    outcomes = result.get("outcomes") or []
    for index, action in enumerate(decision["actions"]):  # 5. things that follow the reply
        done = index >= len(outcomes) or (outcomes[index] or {}).get("done")
        if action["type"] == "team_report" and done and not result.get("sent_team_report"):
            assistant.send_team_report(chat, action["scope"])
            result["sent_team_report"] = True
            _store(row_id, result)
        if action["type"] == "make_document" and done and not result.get("sent_document_draft"):
            doc_templates.show_pending(chat)  # the draft file follows the reply that asks to check it
            result["sent_document_draft"] = True
            _store(row_id, result)
    if result.get("documents_prepared") and not result.get("documents_routed"):  # the files go where the rules say
        doc_intake.after_reply(_received_ids(result), decision["actions"], by=author)
        result["documents_routed"] = True
        _store(row_id, result)
    with SessionLocal() as s:
        row = s.get(ConversationMessage, row_id)
        row.status, row.error, row.result = "agent_done", "", dict(result)
        s.commit()
    dialog_memory.schedule(chat)  # the older part of the conversation is retold in the background
