"""Orders from the chat: «поручи Виктору купить перфоратор до пятницы», «узнай у Ольги, когда курьер», «запроси у Анны
акт сверки и передай Борису».

The assistant retells the order and asks «Делаю?»; the order waits here for the answer (24 hours at most). «Да» makes it
a case the Progress page shows (cases.py): the colleague gets the order, the assistant follows it up by the deadline and
reports to the person who gave it; a question or a document request goes the same way as one from a meeting. A
correction («нет, до среды») is a new order that replaces this one. An order to oneself is just a task with a deadline.
"""
import datetime as dt
import logging

from . import cases, skill_stats
from .db import Case, CaseStep, ChatOrder, Employee, Note, SessionLocal, Task, now

log = logging.getLogger("sysai.orders")

PENDING_HOURS = 24
KIND_RU = {"task": "поручить", "ask": "узнать у", "request": "запросить документ у"}


class OrderError(RuntimeError):
    """The order cannot be started (Russian reason)."""


def _date_ru(value) -> str:
    try:
        return dt.date.fromisoformat(str(value)).strftime("%d.%m")
    except (TypeError, ValueError):
        return ""


def describe(kind, to_name: str, title, deadline=None, forward_name: str = "") -> str:
    """«поручить Виктор Иванов: купить перфоратор до 14.11» — for the approval and the Progress page."""
    when = _date_ru(deadline)
    text = f"{KIND_RU.get(kind, 'поручить')} {to_name}: {str(title or '').strip()[:200]}"
    if forward_name:
        text += f", передать {forward_name}"
    return text + (f" до {when}" if when else "")


def describe_order(s, order_id, ok: bool = True) -> str:
    order = s.get(ChatOrder, order_id) if isinstance(order_id, int) else None
    if order is None:
        return f"поручение #{order_id}"
    p = order.payload or {}
    to = s.get(Employee, p.get("to")) if p.get("to") else None
    forward = s.get(Employee, p.get("forward_to")) if p.get("forward_to") else None
    text = describe(p.get("kind"), "себе" if p.get("self") else (to.name if to else "?"), p.get("title"),
                    p.get("deadline"), forward.name if forward else "")
    return ("начать поручение: " if ok else "отменить поручение: ") + text


def remember(chat: str, requested_by: str, employee_id, action: dict) -> int:
    """The checked order waits for «да»; an earlier one of this chat that was not answered is replaced."""
    payload = {k: action.get(k) for k in ("kind", "to", "title", "details", "deadline", "forward_to")
               if action.get(k) not in (None, "")}
    if payload.get("to") == "me":
        payload["to"], payload["self"] = employee_id, True
    with SessionLocal() as s:
        for old in s.query(ChatOrder).filter_by(chat_id=str(chat), status="pending").all():
            old.status, old.decided_at = "replaced", now()
        order = ChatOrder(chat_id=str(chat), requested_by=(requested_by or "")[:120], employee_id=employee_id,
                          payload=payload, status="pending")
        s.add(order)
        s.commit()
        return order.id


def pending_view(s, chat: str):
    """The order this chat was asked to confirm (the model sees it as order_to_confirm), or None."""
    since = now() - dt.timedelta(hours=PENDING_HOURS)
    order = (s.query(ChatOrder).filter(ChatOrder.chat_id == str(chat), ChatOrder.status == "pending",
                                       ChatOrder.created_at >= since).order_by(ChatOrder.id.desc()).first())
    if order is None:
        return None
    p = order.payload or {}
    to = s.get(Employee, p.get("to")) if p.get("to") else None
    forward = s.get(Employee, p.get("forward_to")) if p.get("forward_to") else None
    return {"id": order.id, "kind": p.get("kind"), "to": "себе" if p.get("self") else (to.name if to else None),
            "title": p.get("title"), "details": p.get("details") or None, "deadline": p.get("deadline"),
            "forward_to": forward.name if forward else None, "asked_at": cases._local_time(order.created_at)}


def decide(order_id: int, chat: str, ok: bool, by: str = "") -> dict:
    """«Да» starts the order, «нет» cancels it."""
    with SessionLocal() as s:
        order = s.get(ChatOrder, order_id)
        if order is None or str(order.chat_id) != str(chat):
            raise OrderError("поручение не найдено")
        if order.status != "pending":
            raise OrderError("поручение уже начато или отменено")
        if not ok:
            order.status, order.decided_at = "cancelled", now()
            s.commit()
            skill_stats.record("order", "cancelled", "ответ «нет» на пересказ", chat)
            return {"cancelled": True}
    return start(order_id)


def _person(s, employee_id, what: str) -> Employee:
    person = s.get(Employee, employee_id) if employee_id else None
    if person is None or not person.active:
        raise OrderError(f"{what} больше нет в справочнике")
    return person


def start(order_id: int) -> dict:
    """The task (and, when somebody else does it, the case) of a confirmed order. Returns their ids."""
    with SessionLocal() as s:
        order = s.get(ChatOrder, order_id)
        p = dict(order.payload or {})
        kind = p.get("kind") if p.get("kind") in KIND_RU else "task"
        to = _person(s, p.get("to"), "исполнителя")
        forward = _person(s, p["forward_to"], "получателя документа") if p.get("forward_to") else None
        deadline = dt.date.fromisoformat(p["deadline"]) if p.get("deadline") else None
        requester = order.requested_by or "руководитель"
        title = str(p.get("title") or "").strip()[:500] or "Поручение из чата"
        details = str(p.get("details") or "").strip()[:4000]
        own = bool(p.get("self")) or (order.employee_id is not None and to.id == order.employee_id)
        quote = f"Поручение из чата от {requester}"
        if kind == "task":
            task = Task(meeting_id=None, title=title, description=details, assignee_id=to.id, assignee_name=to.name,
                        deadline=deadline, deadline_source="stated" if deadline else "none", status="sent",
                        executor="person", source_quote=quote)
        else:
            people = [("target" if kind == "ask" else "source", to)] + ([("recipient", forward)] if forward else [])
            case_kind = "ask" if kind == "ask" else ("forward" if forward else "request")
            task = Task(meeting_id=None, title=title, description=details, executor="assistant", action_type=case_kind,
                        assignee_name="ассистент", target_employee_id=to.id, target_employee_name=to.name,
                        counterparts=[{"role": r, "employee_id": e.id, "name": e.name} for r, e in people],
                        deadline=deadline, deadline_source="stated" if deadline else "none",
                        status="assistant_waiting", source_quote=quote)
        s.add(task)
        s.flush()
        order.status, order.decided_at, order.task_id = "started", now(), task.id
        if own:  # an order to oneself: a task with a deadline, nobody to write to
            s.add(Note(employee_id=to.id, task_id=task.id, kind="commitment", source="conversation", author=requester[:80],
                       text=f"Сам(а) поставил(а) себе задачу «{title}»" + (f" до {_date_ru(p['deadline'])}" if deadline else "")))
            s.commit()
            return {"task_id": task.id}
        owner = {"to_chat": str(order.chat_id), "requested_chat": str(order.chat_id), "requested_by": requester[:120]}
        if kind == "task":
            steps = [{"tool": "send_message", "to": to.id, "purpose": "order"},
                     {"tool": "wait_reply", "from": to.id, "expects": "done"}, {"tool": "report"}, {"tool": "close"}]
        else:
            try:
                steps = cases.validate_plan(cases.template_plan(case_kind, people), case_kind, people)
            except cases.PlanError as exc:
                s.rollback()
                raise OrderError(str(exc)) from exc
        refs = {to.id: to.telegram_chat_id or ""}
        if forward is not None:
            refs[forward.id] = forward.telegram_chat_id or ""
        case = Case(task_id=task.id, meeting_id=None, title=title, kind="order" if kind == "task" else case_kind,
                    autonomy="auto", planner="order", status="running")
        for position, step in enumerate(steps):
            args = {k: v for k, v in step.items() if k != "tool"}
            if "to" in args:
                args["to_ref"] = refs.get(args["to"], "")  # the address the order was checked against
            if step["tool"] == "send_message":
                args["requested_by"] = requester[:120]
            if step["tool"] == "report":
                args.update(owner)
            case.steps.append(CaseStep(position=position, tool=step["tool"], args=args))
        s.add(case)
        s.flush()
        order.case_id = case.id
        cases._event(s, case.id, "planned", f"Поручение от {requester}: {cases.describe_plan(s, case)}")
        s.add(Note(employee_id=to.id, task_id=task.id, kind="commitment", source="system", author=requester[:80],
                   text=(f"Поручение от {requester}: «{title}»" + (f" до {_date_ru(p['deadline'])}" if deadline else ""))[:800]))
        s.commit()
        ids = {"case_id": case.id, "task_id": task.id}
    if cases._testing():
        cases.advance(ids["case_id"])
    else:
        cases._pool().submit(cases.advance, ids["case_id"])
    return ids
