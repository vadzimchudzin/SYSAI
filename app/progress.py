"""The Progress page: the state of every case and task after a meeting, built from the database only.

The director should understand it in half a minute: a summary (created / closed / stuck), what needs a decision first,
then what the assistant is waiting for (from whom, how long, what next), then what is done. The same data feeds the
live refresh: `version()` changes whenever anything shown here changes.
"""
import datetime as dt
import hashlib

from sqlalchemy import func

from . import cases, deadlines, report_archive
from .db import Case, CaseEvent, CaseStep, CaseWait, Document, DocumentTransfer, Employee, Meeting, Task, now

URGENCY = {"attention": 0, "awaiting_approval": 1, "suggested": 1, "paused": 2, "waiting": 3, "running": 4,
           "failed": 5, "handed_over": 6, "cancelled": 7, "done": 8}
PILL = {"done": "green", "failed": "red", "cancelled": "gray", "attention": "red", "awaiting_approval": "amber",
        "suggested": "amber", "waiting": "blue", "running": "blue", "paused": "gray", "handed_over": "clay"}
PERSON_OPEN = ("draft", "sent", "awaiting_deadline", "in_progress", "blocked")


def _aware(moment):
    return cases._aware(moment)


def _ago(moment, at=None) -> str:
    if moment is None:
        return ""
    minutes = int(((at or now()) - _aware(moment)).total_seconds() // 60)
    if minutes < 1:
        return "меньше минуты"
    if minutes < 60:
        return f"{minutes} мин"
    hours = minutes // 60
    if hours < 48:
        return f"{hours} ч {minutes % 60:02d} мин" if hours < 10 else f"{hours} ч"
    return f"{hours // 24} дн"


def _until(moment, at=None) -> str:
    if moment is None:
        return ""
    minutes = int((_aware(moment) - (at or now())).total_seconds() // 60)
    if minutes <= 0:
        return "сейчас"
    return f"через {minutes} мин" if minutes < 60 else f"через {minutes // 60} ч"


def last_meeting(s):
    """The latest meeting whose plan has been approved (that is where cases start); otherwise the latest one."""
    approved = (s.query(Meeting).filter(Meeting.status.in_(report_archive.APPROVED_STATUSES))
                .order_by(Meeting.id.desc()).first())
    return approved or s.query(Meeting).order_by(Meeting.id.desc()).first()


def meetings_menu(s) -> list:
    return [{"id": m.id, "title": m.title or f"Совещание #{m.id}",
             "date": _aware(m.created_at).strftime("%d.%m") if m.created_at else ""}
            for m in s.query(Meeting).order_by(Meeting.id.desc()).limit(12)]


def _scope_query(s, scope: str):
    """(cases query, meeting or None, label)."""
    query = s.query(Case)
    if scope == "open":
        return query.filter(Case.status.in_(cases.OPEN)), None, "Все открытые дела"
    if scope == "week":
        return query.filter(Case.created_at >= now() - dt.timedelta(days=7)), None, "Дела за 7 дней"
    meeting = None
    if scope and scope.isdigit():
        meeting = s.get(Meeting, int(scope))
    if meeting is None:
        meeting = last_meeting(s)
    if meeting is None:
        return query.filter(Case.id < 0), None, "Совещаний пока нет"
    return query.filter(Case.meeting_id == meeting.id), meeting, meeting.title or f"Совещание #{meeting.id}"


def _step_text(s, step, names) -> str:
    a = step.args or {}

    def who(key):
        if a.get(key) not in names:
            e = s.get(Employee, a.get(key)) if a.get(key) else None
            names[a.get(key)] = e.name if e else "?"
        return names[a.get(key)]
    if step.tool == "send_message":
        return f"{'спросить' if a.get('purpose') == 'ask' else 'попросить документ у'} {who('to')}"
    if step.tool == "wait_reply":
        return f"дождаться {'ответа' if a.get('expects') == 'answer' else 'документа'} от {who('from')}"
    if step.tool == "forward_document":
        return f"передать документ {who('to')}"
    if step.tool == "report":
        return "доложить руководителю"
    return "закрыть дело"


def case_card(s, case, at=None) -> dict:
    at = at or now()
    names = {}
    events = list(case.events)
    steps = list(case.steps)
    current = next((x for x in steps if x.status not in ("done", "skipped")), None)
    upcoming = [x for x in steps if current is not None and x.position > current.position and x.tool != "close"]
    wait = (s.query(CaseWait).filter_by(case_id=case.id, status="open").order_by(CaseWait.id).first())
    waiting = None
    if wait is not None:
        person = s.get(Employee, wait.employee_id) if wait.employee_id else None
        waiting = {"who": person.name if person else "?", "what": "ответа" if wait.expects == "answer" else "документа",
                   "expects": wait.expects, "for": _ago(wait.started_at, at),
                   "hours": round((at - _aware(wait.started_at)).total_seconds() / 3600, 1),
                   "reminders": wait.reminders_sent, "escalated": wait.escalated,
                   "next_reminder": _until(wait.next_reminder_at, at) if wait.next_reminder_at and case.status == "waiting" else "",
                   "escalate_in": _until(wait.escalate_at, at) if not wait.escalated and case.status == "waiting" else ""}
    documents = []
    for d in s.query(Document).filter_by(case_id=case.id).order_by(Document.id).all():
        transfers = s.query(DocumentTransfer).filter_by(document_id=d.id).order_by(DocumentTransfer.id).all()
        documents.append({"id": d.id, "filename": d.filename, "sensitive": d.sensitive,
                          "from": d.employee.name if d.employee else "", "sha": d.sha256[:10],
                          "transfers": [{"to": (s.get(Employee, t.to_employee_id).name if t.to_employee_id and
                                                s.get(Employee, t.to_employee_id) else t.to_ref),
                                         "status": t.status} for t in transfers]})
    task = case.task
    stuck = case.status in ("attention", "awaiting_approval", "suggested") or bool(waiting and waiting["escalated"])
    if case.status in cases.OPEN and current is not None:
        now_text = {"waiting": f"ждёт {waiting['what']} от {waiting['who']}" if waiting else _step_text(s, current, names),
                    "attention": case.note or "нужно решение человека",
                    "awaiting_approval": "ждёт одобрения плана", "suggested": "план предложен директору",
                    "paused": "на паузе", "running": _step_text(s, current, names)}.get(case.status, "")
    else:
        now_text = case.note or cases.STATUS_RU.get(case.status, case.status)
    retry = None
    if case.status == "running" and case.retry_at is not None and _aware(case.retry_at) > at:
        last = next((e for e in reversed(events) if e.kind == "retry"), None)
        retry = {"in": _until(case.retry_at, at), "why": (last.text.replace("Повтор позже: ", "") if last else "")}
    return {"id": case.id, "title": case.title, "status": case.status,
            "status_ru": cases.STATUS_RU.get(case.status, case.status), "pill": PILL.get(case.status, "gray"),
            "open": case.status in cases.OPEN, "stuck": stuck, "note": case.note or "",
            "meeting_id": case.meeting_id, "meeting": case.task.meeting.title if task and task.meeting else "",
            "planner": case.planner, "plan": cases.describe_plan(s, case), "now": now_text,
            "next": [_step_text(s, x, names) for x in upcoming][:3],
            "waiting": waiting, "documents": documents, "retry": retry,
            "can": {"approve": case.status in ("awaiting_approval", "suggested") or any(x.status == "awaiting_approval" for x in steps),
                    "retry": case.status == "attention" and any(x.status in ("uncertain", "failed") for x in steps),
                    "pause": case.status in ("running", "waiting", "attention", "awaiting_approval", "suggested"),
                    "resume": case.status == "paused",
                    "answer": bool(waiting and waiting["expects"] == "answer"),
                    "upload": bool(waiting and waiting["expects"] == "document"),
                    "message": case.status in cases.OPEN, "handover": case.status in cases.OPEN,
                    "close": case.status in cases.OPEN},
            "unlinked": cases.unlinked_documents(s, wait.employee_id) if wait is not None and wait.expects == "document" else [],
            "updated": _ago(events[-1].created_at, at) if events else "",
            "events": [{"at": _aware(e.created_at), "kind": e.kind, "text": e.text} for e in events[-30:]]}


def _person_tasks(s, meeting) -> dict:
    if meeting is None:
        return {}
    tasks = [t for t in meeting.tasks if t.executor != "assistant"]
    today = deadlines.today()
    overdue = [t for t in tasks if t.deadline and t.deadline < today and t.status in PERSON_OPEN]
    blocked = [t for t in tasks if t.status == "blocked"]
    nobody = [t for t in tasks if not t.assignee_id or t.status == "no_recipient"]
    attention = []
    for label, items in (("просрочено", overdue), ("заблокировано", blocked), ("нет исполнителя", nobody)):
        for t in items:
            attention.append({"title": t.title, "who": t.assignee.name if t.assignee else (t.assignee_name or "—"),
                              "why": label, "deadline": t.deadline.strftime("%d.%m") if t.deadline else ""})
    return {"total": len(tasks), "done": sum(t.status == "done" for t in tasks),
            "in_work": sum(t.status in ("sent", "in_progress", "awaiting_deadline") for t in tasks),
            "overdue": len(overdue), "blocked": len(blocked), "nobody": len(nobody), "attention": attention[:10]}


def build(s, scope: str = "last") -> dict:
    at = now()
    query, meeting, label = _scope_query(s, scope)
    rows = query.all()
    cards = sorted((case_card(s, c, at) for c in rows), key=lambda c: (URGENCY.get(c["status"], 9), -c["id"]))
    summary = {"created": len(cards), "done": sum(c["status"] == "done" for c in cards),
               "open": sum(c["open"] for c in cards), "stuck": sum(c["stuck"] for c in cards),
               "waiting": sum(c["status"] == "waiting" for c in cards),
               "failed": sum(c["status"] in ("failed", "cancelled") for c in cards),
               "handed_over": sum(c["status"] == "handed_over" for c in cards)}
    groups = [("Нужно ваше решение", [c for c in cards if c["stuck"]]),
              ("В работе у ассистента", [c for c in cards if c["open"] and not c["stuck"]]),
              ("Завершено", [c for c in cards if not c["open"]])]
    return {"scope": scope or "last", "label": label, "meeting": {"id": meeting.id, "title": meeting.title} if meeting else None,
            "summary": summary, "groups": [(t, items) for t, items in groups if items], "people": _person_tasks(s, meeting),
            "employees": [{"id": e.id, "name": e.name} for e in
                          s.query(Employee).filter(Employee.active.is_(True)).order_by(Employee.name).all()],
            "meetings": meetings_menu(s), "version": version(s, scope), "generated": at}


def version(s, scope: str = "last") -> str:
    """Changes whenever the page would show something different (a new event, a status, a task)."""
    query, meeting, _ = _scope_query(s, scope)
    ids = [c.id for c in query.with_entities(Case.id).all()]
    parts = [scope or "last", str(meeting.id if meeting else ""), ",".join(map(str, sorted(ids)))]
    if ids:
        parts.append(str(s.query(func.max(CaseEvent.id)).filter(CaseEvent.case_id.in_(ids)).scalar() or 0))
        parts.append(",".join(f"{i}:{st}" for i, st in s.query(Case.id, Case.status).filter(Case.id.in_(ids)).order_by(Case.id)))
        parts.append(",".join(f"{w.id}:{w.reminders_sent}:{int(w.escalated)}:{w.status}" for w in
                              s.query(CaseWait).filter(CaseWait.case_id.in_(ids)).order_by(CaseWait.id)))
    if meeting is not None:
        parts.append(",".join(f"{t.id}:{t.status}:{t.assignee_id}:{t.deadline}" for t in
                              s.query(Task).filter_by(meeting_id=meeting.id).order_by(Task.id)))
    parts.append(str(int(now().timestamp() // 60)))  # «ждёт 2 ч 15 мин» moves on by itself once a minute
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:16]
