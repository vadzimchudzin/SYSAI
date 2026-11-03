"""Сводные таблицы (№27): задачи, дела и совещания с фильтрами и готовые сводные — по людям, по объектам, по срокам —
одной кнопкой в Excel (открывается и в Google Таблицах: «Файл» → «Импорт»).

The filters: the period (this week, this month, last month, the quarter, the year, all time or own dates), the person,
the department, the client or object, the state of the tasks. A task belongs to the period when it was set, is due or was
done in it. «Выполнено в срок» counts the tasks whose day of completion is known (Task.done_at, else the end of the case
that carried it): the tasks closed before this version have no such day and are counted as done only.

The workbook is written here (Office Open XML by hand: no extra package on the server) — frozen header rows, filters,
dates as dates and the links of the panel.
"""
from __future__ import annotations

import collections
import datetime as dt
import io
import re
import zipfile
from xml.sax.saxutils import escape

from sqlalchemy.orm import joinedload, selectinload

from . import config, reminders
from .db import (TASK_DONE, Case, CaseWait, Department, Employee, EmployeeDepartment, Entity, EntityLink, MailMessage,
                 Meeting, Task)

PRESETS = {"week": "Эта неделя", "month": "Этот месяц", "prev_month": "Прошлый месяц", "quarter": "Этот квартал",
           "year": "Этот год", "all": "Всё время", "custom": "Свои даты"}
STATES = {"": "все задачи", "open": "открытые", "overdue": "просроченные", "done": "выполненные"}
TASK_STATUS_RU = {"draft": "черновик протокола", "sent": "отправлена", "awaiting_deadline": "ждёт срока",
                  "no_recipient": "нет получателя", "in_progress": "в работе", "blocked": "мешает", "done": "выполнена",
                  "assistant_todo": "делает ИИ-сотрудник", "assistant_waiting": "ИИ-сотрудник ждёт",
                  "assistant_done": "сделано ИИ-сотрудником", "assistant_failed": "ИИ-сотрудник не смог"}
CASE_KIND_RU = {"ask": "вопрос", "request": "запрос документа", "forward": "передача документа", "mail": "письмо",
                "order": "поручение", "skill": "просьба из чата", "deliver": "доставка", "message": "сообщение"}
CASE_STATUS_RU = {"awaiting_approval": "ждёт одобрения", "suggested": "предложено", "running": "выполняется",
                  "waiting": "ждёт ответа", "attention": "нужно решение", "done": "завершено", "failed": "не удалось",
                  "cancelled": "отменено", "paused": "на паузе", "handed_over": "передано человеку"}
CASE_CLOSED = ("done", "failed", "cancelled", "handed_over")
MEETING_STATUS_RU = {"queued": "в очереди", "transcribing": "расшифровка", "analyzing": "анализ",
                     "awaiting_approval": "ждёт проверки", "sending": "отправка", "done": "отправлено", "error": "ошибка",
                     "rejected": "отклонено"}
SOURCE_RU = {"ticnote": "TicNote", "microphone": "микрофон", "web": "загрузка", "demo": "демо", "api": "API",
             "notion": "Notion"}
BUCKETS = ("Просрочено", "Сегодня", "Завтра", "До конца недели", "Следующая неделя", "Позже", "Без срока")
MAX_ROWS = 20000


# ======================================================================================== the filters
def _date(raw):
    try:
        return dt.date.fromisoformat(str(raw or "").strip()[:10])
    except ValueError:
        return None


def _int(raw):
    raw = str(raw or "").strip()
    return int(raw) if raw.isdigit() else None


def today() -> dt.date:
    return dt.datetime.now(reminders.zone()).date()


def period(preset: str, lo=None, hi=None, at: dt.date | None = None) -> tuple:
    """(first day, last day) of the period; (None, None) is all time."""
    at = at or today()
    if preset == "week":
        start = at - dt.timedelta(days=at.weekday())
        return start, start + dt.timedelta(days=6)
    if preset == "prev_month":
        end = at.replace(day=1) - dt.timedelta(days=1)
        return end.replace(day=1), end
    if preset == "quarter":
        first = dt.date(at.year, (at.month - 1) // 3 * 3 + 1, 1)
        nxt = dt.date(first.year + (first.month + 2) // 12, (first.month + 2) % 12 + 1, 1)
        return first, nxt - dt.timedelta(days=1)
    if preset == "year":
        return dt.date(at.year, 1, 1), dt.date(at.year, 12, 31)
    if preset == "all":
        return None, None
    if preset == "custom":
        lo, hi = _date(lo), _date(hi)
        if lo and hi and lo > hi:
            lo, hi = hi, lo
        return lo, hi
    first = at.replace(day=1)  # this month
    nxt = dt.date(first.year + first.month // 12, first.month % 12 + 1, 1)
    return first, nxt - dt.timedelta(days=1)


def filters(params) -> dict:
    """The filters of the page (query parameters) checked: unknown values are dropped."""
    get = (lambda k: params.get(k)) if hasattr(params, "get") else (lambda k: None)
    preset = str(get("period") or "month")
    preset = preset if preset in PRESETS else "month"
    lo, hi = period(preset, get("from"), get("to"))
    state = str(get("state") or "")
    return {"period": preset, "from": lo, "to": hi, "employee_id": _int(get("employee")),
            "department_id": _int(get("department")), "entity_id": _int(get("entity")),
            "state": state if state in STATES else ""}


def query_string(f: dict) -> str:
    from urllib.parse import urlencode
    data = {"period": f["period"], "state": f["state"]}
    if f["period"] == "custom":
        data.update({"from": f["from"].isoformat() if f["from"] else "", "to": f["to"].isoformat() if f["to"] else ""})
    for key, name in (("employee_id", "employee"), ("department_id", "department"), ("entity_id", "entity")):
        if f.get(key):
            data[name] = f[key]
    return urlencode({k: v for k, v in data.items() if v not in ("", None)})


def _in(day, f) -> bool:
    if day is None:
        return False
    return (f["from"] is None or day >= f["from"]) and (f["to"] is None or day <= f["to"])


def _local_day(moment):
    if moment is None:
        return None
    moment = moment if moment.tzinfo else moment.replace(tzinfo=dt.timezone.utc)
    return moment.astimezone(reminders.zone()).date()


def _panel(path: str) -> str:
    return f"{config.PUBLIC_URL}{path}" if config.PUBLIC_URL else ""


# ======================================================================================== the data
def _departments(s) -> tuple:
    """({employee id: «Отдел продаж, Склад»}, {department id: {employee ids}})"""
    names = {d.id: d.name for d in s.query(Department).all()}
    of_person, members = collections.defaultdict(list), collections.defaultdict(set)
    for m in s.query(EmployeeDepartment).order_by(EmployeeDepartment.sort_order, EmployeeDepartment.id).all():
        if m.department_id in names:
            of_person[m.employee_id].append(names[m.department_id])
            members[m.department_id].add(m.employee_id)
    for e in s.query(Employee).all():
        if not of_person.get(e.id) and (e.department or "").strip():
            of_person[e.id].append(e.department.strip())
    return {k: ", ".join(v) for k, v in of_person.items()}, members


def _entities(s) -> dict:
    """{entity id: the card it lives in now (merged duplicates point to the kept one)}"""
    rows = {e.id: e for e in s.query(Entity).all()}
    out = {}
    for key, e in rows.items():
        seen, cur = set(), e
        while cur.merged_into_id and cur.merged_into_id in rows and cur.id not in seen:
            seen.add(cur.id)
            cur = rows[cur.merged_into_id]
        out[key] = cur
    return out


def _links(s, kind: str, cards: dict) -> dict:
    """{source id: [entity cards]} for one kind of source (task, meeting, document)."""
    out = collections.defaultdict(list)
    for link in s.query(EntityLink).filter(EntityLink.source_type == kind).all():
        card = cards.get(link.entity_id)
        if card is not None and card.active is not False and card not in out[link.source_id]:
            out[link.source_id].append(card)
    return out


def _source(task) -> str:
    if task.meeting_id and task.meeting is not None:
        day = task.meeting.meeting_date or _local_day(task.meeting.created_at)
        return f"Совещание «{task.meeting.title or 'без названия'}»" + (f" {day:%d.%m.%Y}" if day else "")
    quote = str(task.source_quote or "")
    if quote.startswith("Письмо"):
        return "Письмо"
    if quote.startswith("Документ"):
        return "Документ"
    return "Поручение"


def collect(s, f: dict) -> dict:
    """Everything the sheets and the page show, for the filters."""
    at = today()
    depts, members = _departments(s)
    cards = _entities(s)
    task_links, meeting_links = _links(s, "task", cards), _links(s, "meeting", cards)
    people = {e.id: e for e in s.query(Employee).all()}
    cases_of_task = {c.task_id: c for c in s.query(Case).filter(Case.task_id.isnot(None)).all()}
    target = cards[f["entity_id"]].id if f["entity_id"] in cards else (-1 if f["entity_id"] else None)
    allowed = None
    if f["department_id"]:
        allowed = members.get(f["department_id"], set())
    if f["employee_id"]:
        allowed = {f["employee_id"]} if allowed is None else allowed & {f["employee_id"]}
    tasks = []
    for t in s.query(Task).options(joinedload(Task.meeting)).order_by(Task.id).limit(MAX_ROWS).all():
        if allowed is not None and t.assignee_id not in allowed:
            continue
        linked = task_links.get(t.id, [])
        if target is not None and target not in [c.id for c in linked]:
            continue
        case = cases_of_task.get(t.id)
        set_on = _local_day(t.created_at) or (t.meeting.meeting_date if t.meeting is not None else None) or (
            _local_day(t.meeting.created_at) if t.meeting is not None else None)
        done = t.status in TASK_DONE
        done_on = (_local_day(t.done_at) or (_local_day(case.closed_at) if case is not None and case.status == "done"
                                             else None)) if done else None
        if f["period"] != "all" and not (_in(set_on, f) or _in(t.deadline, f) or _in(done_on, f)):
            continue
        overdue = not done and t.deadline is not None and t.deadline < at
        if (f["state"] == "open" and done) or (f["state"] == "done" and not done) or (f["state"] == "overdue" and not overdue):
            continue
        late_days = None
        if overdue:
            late_days = (at - t.deadline).days
        elif done and done_on and t.deadline and done_on > t.deadline:
            late_days = (done_on - t.deadline).days
        person = people.get(t.assignee_id)
        tasks.append({
            "id": t.id, "title": t.title or "", "person": person.name if person else (t.assignee_name or ""),
            "person_id": t.assignee_id, "department": depts.get(t.assignee_id, ""), "set_on": set_on,
            "deadline": t.deadline, "status": TASK_STATUS_RU.get(t.status, t.status or ""), "done": done,
            "done_on": done_on, "overdue": overdue, "late_days": late_days,
            "on_time": (done_on <= t.deadline) if done and done_on and t.deadline else None,
            "source": _source(t), "entities": [c.name for c in linked], "entity_ids": [c.id for c in linked],
            "case": CASE_STATUS_RU.get(case.status, case.status) if case is not None else "",
            "url": _panel(f"/meetings/{t.meeting_id}") if t.meeting_id else "",
            "description": (t.description or "")[:1000],
        })
    return {"tasks": tasks, "cases": _cases(s, f, people, allowed, target, task_links),
            "meetings": _meetings(s, f, allowed, target, meeting_links), "today": at, "people": people,
            "depts": depts, "cards": cards, "target": target}


def _cases(s, f, people, allowed, target, task_links) -> list:
    waits = collections.defaultdict(list)
    for w in s.query(CaseWait).filter(CaseWait.status == "open").all():
        waits[w.case_id].append(w.employee_id)
    out = []
    for c in s.query(Case).options(joinedload(Case.task)).order_by(Case.id).limit(MAX_ROWS).all():
        opened, closed = _local_day(c.created_at), _local_day(c.closed_at)
        still_open = c.status not in CASE_CLOSED
        if f["period"] != "all" and not (_in(opened, f) or _in(closed, f) or (still_open and (
                f["to"] is None or (opened or f["to"]) <= f["to"]))):
            continue
        if f["state"] == "done" and still_open or f["state"] in ("open", "overdue") and not still_open:
            continue
        who = [people[i].name for i in waits.get(c.id, []) if i in people]
        owner = c.task.assignee_id if c.task is not None else None
        if allowed is not None and owner not in allowed and not (set(waits.get(c.id, [])) & allowed):
            continue
        linked = task_links.get(c.task_id, []) if c.task_id else []
        if target is not None and target not in [x.id for x in linked]:
            continue
        days = ((closed or today()) - opened).days if opened else None
        out.append({"id": c.id, "title": c.title or "", "kind": CASE_KIND_RU.get(c.kind or "", c.kind or ""),
                    "status": CASE_STATUS_RU.get(c.status, c.status), "waiting_for": ", ".join(who),
                    "task": c.task.title if c.task is not None else "", "opened": opened, "closed": closed,
                    "days": days, "note": (c.note or "")[:500], "open": still_open, "url": _panel("/progress")})
    return out


def _meetings(s, f, allowed, target, meeting_links) -> list:
    from . import participants
    at = today()
    names = {e.id: e.name for e in s.query(Employee).all()}
    out = []
    for m in (s.query(Meeting).options(selectinload(Meeting.tasks)).order_by(Meeting.meeting_date, Meeting.id)
              .limit(MAX_ROWS).all()):
        day = m.meeting_date or _local_day(m.created_at)
        if f["period"] != "all" and not _in(day, f):
            continue
        ids = participants.employee_ids(m)
        owners = {t.assignee_id for t in m.tasks}
        if allowed is not None and not (set(ids) & allowed or owners & allowed):
            continue
        linked = meeting_links.get(m.id, [])
        if target is not None and target not in [x.id for x in linked]:
            continue
        done = sum(1 for t in m.tasks if t.status in TASK_DONE)
        overdue = sum(1 for t in m.tasks if t.status not in TASK_DONE and t.deadline and t.deadline < at)
        out.append({"id": m.id, "day": day, "title": m.title or "Совещание",
                    "source": SOURCE_RU.get(m.source or "", m.source or ""),
                    "minutes": round((m.duration_sec or 0) / 60) if m.duration_sec else None,
                    "people": participants.count(m) or None,
                    "present": ", ".join([names[i] for i in ids if i in names] + participants.guests(m)),
                    "tasks": len(m.tasks), "done": done, "open": len(m.tasks) - done, "overdue": overdue,
                    "status": MEETING_STATUS_RU.get(m.status or "", m.status or ""),
                    "entities": ", ".join(c.name for c in linked), "url": _panel(f"/meetings/{m.id}")})
    return out


# ------------------------------------------------------------------------------------------------- the pivots
def by_people(data: dict) -> list:
    groups = collections.OrderedDict()
    for t in sorted(data["tasks"], key=lambda t: (t["person"] or "яяя", t["id"])):
        key = t["person_id"] or 0
        g = groups.setdefault(key, {"person": t["person"] or "Без ответственного", "department": t["department"],
                                    "total": 0, "open": 0, "overdue": 0, "done": 0, "on_time": 0, "late": 0,
                                    "late_sum": 0, "late_n": 0, "nearest": None})
        g["total"] += 1
        if t["done"]:
            g["done"] += 1
            if t["on_time"] is True:
                g["on_time"] += 1
            elif t["on_time"] is False:
                g["late"] += 1
        else:
            g["open"] += 1
            if t["overdue"]:
                g["overdue"] += 1
                g["late_sum"] += t["late_days"] or 0
                g["late_n"] += 1
            elif t["deadline"] and (g["nearest"] is None or t["deadline"] < g["nearest"]):
                g["nearest"] = t["deadline"]
    out = []
    for g in groups.values():
        known = g["on_time"] + g["late"]
        out.append({**g, "share": (g["on_time"] / known) if known else None,
                    "avg_late": round(g["late_sum"] / g["late_n"], 1) if g["late_n"] else None})
    return out


def by_entities(s, data: dict, f: dict) -> list:
    groups = {}
    for t in data["tasks"]:
        for eid, name in zip(t["entity_ids"], t["entities"]):
            g = groups.setdefault(eid, {"id": eid, "name": name, "tasks": 0, "open": 0, "overdue": 0, "done": 0,
                                        "meetings": 0, "documents": 0, "letters": 0, "last": None})
            g["tasks"] += 1
            g["done" if t["done"] else "open"] += 1
            g["overdue"] += 1 if t["overdue"] else 0
    cards = data["cards"]
    for link in s.query(EntityLink).filter(EntityLink.source_type.in_(("meeting", "document"))).all():
        card = cards.get(link.entity_id)
        day = _local_day(link.created_at)
        if card is None or (f["period"] != "all" and not _in(day, f)) or (data["target"] is not None and
                                                                          card.id != data["target"]):
            continue
        g = groups.setdefault(card.id, {"id": card.id, "name": card.name, "tasks": 0, "open": 0, "overdue": 0, "done": 0,
                                        "meetings": 0, "documents": 0, "letters": 0, "last": None})
        g["meetings" if link.source_type == "meeting" else "documents"] += 1
        g["last"] = max(filter(None, [g["last"], day]), default=None)
    for row in s.query(MailMessage).filter(MailMessage.entity_id.isnot(None)).all():
        card = cards.get(row.entity_id)
        day = _local_day(row.sent_at or row.created_at)
        if card is None or (f["period"] != "all" and not _in(day, f)) or (data["target"] is not None and
                                                                          card.id != data["target"]):
            continue
        g = groups.setdefault(card.id, {"id": card.id, "name": card.name, "tasks": 0, "open": 0, "overdue": 0, "done": 0,
                                        "meetings": 0, "documents": 0, "letters": 0, "last": None})
        g["letters"] += 1
        g["last"] = max(filter(None, [g["last"], day]), default=None)
    kinds = {"object": "объект", "client": "клиент", "contractor": "подрядчик", "org": "организация"}
    for g in groups.values():
        card = cards.get(g["id"])
        g["kind"] = kinds.get(getattr(card, "kind", ""), "")
    return sorted(groups.values(), key=lambda g: (-g["open"], -g["tasks"], g["name"]))


def bucket(deadline, at: dt.date) -> str:
    if deadline is None:
        return "Без срока"
    if deadline < at:
        return "Просрочено"
    if deadline == at:
        return "Сегодня"
    if deadline == at + dt.timedelta(days=1):
        return "Завтра"
    sunday = at + dt.timedelta(days=6 - at.weekday())
    if deadline <= sunday:
        return "До конца недели"
    if deadline <= sunday + dt.timedelta(days=7):
        return "Следующая неделя"
    return "Позже"


def by_deadlines(data: dict) -> list:
    """The open tasks of each person by how soon they are due; the last row is «Итого»."""
    at = data["today"]
    rows = collections.OrderedDict()
    for t in sorted(data["tasks"], key=lambda t: (t["person"] or "яяя", t["id"])):
        if t["done"]:
            continue
        r = rows.setdefault(t["person_id"] or 0, {"person": t["person"] or "Без ответственного",
                                                  **{b: 0 for b in BUCKETS}, "total": 0})
        r[bucket(t["deadline"], at)] += 1
        r["total"] += 1
    out = list(rows.values())
    if out:
        out.append({"person": "Итого", **{b: sum(r[b] for r in out) for b in BUCKETS},
                    "total": sum(r["total"] for r in out), "sum": True})
    return out


def summary(data: dict) -> dict:
    tasks = data["tasks"]
    done = [t for t in tasks if t["done"]]
    known = [t for t in done if t["on_time"] is not None]
    return {"tasks": len(tasks), "open": sum(1 for t in tasks if not t["done"]),
            "overdue": sum(1 for t in tasks if t["overdue"]), "done": len(done),
            "on_time": sum(1 for t in known if t["on_time"]), "known": len(known),
            "share": (sum(1 for t in known if t["on_time"]) / len(known)) if known else None,
            "meetings": len(data["meetings"]), "cases_open": sum(1 for c in data["cases"] if c["open"]),
            "cases_closed": sum(1 for c in data["cases"] if not c["open"])}


def describe(s, f: dict) -> str:
    """«Этот месяц (01.03.2026 — 31.03.2026) · Анна Смирнова · открытые» — the head of the sheets and the page."""
    parts = [PRESETS[f["period"]] + (f" ({f['from']:%d.%m.%Y} — {f['to']:%d.%m.%Y})" if f["from"] and f["to"] else
                                      (f" (с {f['from']:%d.%m.%Y})" if f["from"] else (f" (по {f['to']:%d.%m.%Y})" if f["to"] else "")))]
    if f["employee_id"]:
        e = s.get(Employee, f["employee_id"])
        parts.append(e.name if e else "сотрудник удалён")
    if f["department_id"]:
        d = s.get(Department, f["department_id"])
        parts.append(f"отдел «{d.name}»" if d else "отдел удалён")
    if f["entity_id"]:
        card = s.get(Entity, f["entity_id"])
        parts.append(f"«{card.name}»" if card else "объект удалён")
    if f["state"]:
        parts.append(STATES[f["state"]])
    return " · ".join(parts)


# ======================================================================================== the workbook
def sheets(s, f: dict) -> list:
    data = collect(s, f)
    head = describe(s, f)
    total = summary(data)
    made = dt.datetime.now(reminders.zone()).strftime("%d.%m.%Y %H:%M")
    summary_rows = [["Отбор", head], ["Сформировано", made], ["", ""],
                    ["Задач", total["tasks"]], ["Открыто", total["open"]], ["Из них просрочено", total["overdue"]],
                    ["Выполнено", total["done"]],
                    ["Выполнено в срок (где известен день выполнения)", total["share"]],
                    ["Совещаний", total["meetings"]], ["Дел в работе", total["cases_open"]],
                    ["Дел завершено", total["cases_closed"]]]
    out = [{"name": "Сводка", "columns": [("Показатель", 46, "text"), ("Значение", 60, "auto")], "rows": summary_rows,
            "plain": True}]
    out.append({"name": "Задачи", "columns": [
        ("№", 7, "int"), ("Задача", 50, "wrap"), ("Ответственный", 24, "text"), ("Отдел", 20, "text"),
        ("Поставлена", 12, "date"), ("Срок", 12, "date"), ("Статус", 18, "text"), ("Выполнена", 12, "date"),
        ("Просрочка, дней", 10, "int"), ("Откуда", 34, "text"), ("Объект / клиент", 26, "text"), ("Дело", 16, "text"),
        ("Описание", 50, "wrap"), ("Ссылка", 14, "link")],
        "rows": [[t["id"], t["title"], t["person"], t["department"], t["set_on"], t["deadline"], t["status"],
                  t["done_on"], t["late_days"], t["source"], ", ".join(t["entities"]), t["case"], t["description"],
                  t["url"]] for t in data["tasks"]]})
    out.append({"name": "Дела", "columns": [
        ("№", 7, "int"), ("Дело", 50, "wrap"), ("Вид", 18, "text"), ("Статус", 18, "text"), ("Ждём от", 24, "text"),
        ("Задача", 40, "wrap"), ("Начато", 12, "date"), ("Закрыто", 12, "date"), ("Дней", 8, "int"),
        ("Заметка", 40, "wrap")],
        "rows": [[c["id"], c["title"], c["kind"], c["status"], c["waiting_for"], c["task"], c["opened"], c["closed"],
                  c["days"], c["note"]] for c in data["cases"]]})
    out.append({"name": "Совещания", "columns": [
        ("Дата", 12, "date"), ("Совещание", 40, "wrap"), ("Откуда запись", 14, "text"), ("Минут", 8, "int"),
        ("Участников", 11, "int"), ("Присутствовали", 40, "wrap"), ("Задач", 8, "int"), ("Выполнено", 11, "int"),
        ("Открыто", 9, "int"), ("Просрочено", 11, "int"), ("Статус", 16, "text"), ("Объекты / клиенты", 30, "text"),
        ("Ссылка", 14, "link")],
        "rows": [[m["day"], m["title"], m["source"], m["minutes"], m["people"], m["present"], m["tasks"], m["done"],
                  m["open"], m["overdue"], m["status"], m["entities"], m["url"]] for m in data["meetings"]]})
    people = by_people(data)
    out.append({"name": "По людям", "columns": [
        ("Сотрудник", 28, "text"), ("Отдел", 22, "text"), ("Задач", 8, "int"), ("Открыто", 9, "int"),
        ("Просрочено", 11, "int"), ("Выполнено", 11, "int"), ("В срок", 8, "int"), ("С опозданием", 13, "int"),
        ("% в срок", 9, "pct"), ("Средняя просрочка, дней", 12, "num"), ("Ближайший срок", 14, "date")],
        "rows": [[g["person"], g["department"], g["total"], g["open"], g["overdue"], g["done"], g["on_time"],
                  g["late"], g["share"], g["avg_late"], g["nearest"]] for g in people]})
    places = by_entities(s, data, f)
    out.append({"name": "По объектам", "columns": [
        ("Объект / клиент", 34, "text"), ("Вид", 12, "text"), ("Задач", 8, "int"), ("Открыто", 9, "int"),
        ("Просрочено", 11, "int"), ("Выполнено", 11, "int"), ("Совещаний", 11, "int"), ("Документов", 12, "int"),
        ("Писем", 8, "int"), ("Последнее упоминание", 14, "date")],
        "rows": [[g["name"], g["kind"], g["tasks"], g["open"], g["overdue"], g["done"], g["meetings"], g["documents"],
                  g["letters"], g["last"]] for g in places]})
    soon = by_deadlines(data)
    out.append({"name": "По срокам", "columns": [("Сотрудник", 28, "text")] + [(b, 12, "int") for b in BUCKETS] + [
        ("Всего открыто", 13, "int")],
        "rows": [[r["person"]] + [r[b] for b in BUCKETS] + [r["total"]] for r in soon],
        "bold_last": bool(soon)})
    return out


def page_data(s, f: dict) -> dict:
    data = collect(s, f)
    return {"summary": summary(data), "people": by_people(data), "deadlines": by_deadlines(data),
            "places": by_entities(s, data, f)[:15], "head": describe(s, f), "buckets": BUCKETS}


def filename(f: dict) -> str:
    stamp = dt.datetime.now(reminders.zone()).strftime("%Y-%m-%d")
    return f"sysai-tables-{stamp}.xlsx"


# ------------------------------------------------------------------------------------------ Office Open XML
_BAD = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")
EPOCH = dt.date(1899, 12, 30)
STYLE = {"head": 1, "date": 2, "pct": 3, "wrap": 4, "bold": 5, "num": 6, "link": 7}


def _col(index: int) -> str:
    name = ""
    index += 1
    while index:
        index, rest = divmod(index - 1, 26)
        name = chr(65 + rest) + name
    return name


def _sheet_name(name: str, used: set) -> str:
    clean = re.sub(r"[\[\]:*?/\\]", " ", name).strip()[:31] or "Лист"
    base, n = clean, 2
    while clean.lower() in used:
        clean = f"{base[:28]} {n}"
        n += 1
    used.add(clean.lower())
    return clean


def _text(value) -> str:
    return escape(_BAD.sub("", str(value))[:32000])


def _cell(ref: str, value, kind: str, style: int | None = None) -> str:
    if value is None or value == "":
        return f'<c r="{ref}" s="{style}"/>' if style else ""
    s_attr = f' s="{style}"' if style else ""
    if kind == "link" and isinstance(value, str) and value.startswith(("http://", "https://")):
        formula = 'HYPERLINK("{}","Открыть")'.format(value.replace('"', '""'))
        return f'<c r="{ref}" s="{STYLE["link"]}" t="str"><f>{_text(formula)}</f><v>Открыть</v></c>'
    if isinstance(value, bool):
        return f'<c r="{ref}"{s_attr} t="b"><v>{int(value)}</v></c>'
    if isinstance(value, dt.datetime):
        value = value.date()
    if isinstance(value, dt.date):
        return f'<c r="{ref}" s="{style or STYLE["date"]}"><v>{(value - EPOCH).days}</v></c>'
    if isinstance(value, (int, float)):
        if kind == "pct":
            return f'<c r="{ref}" s="{style or STYLE["pct"]}"><v>{float(value):.6g}</v></c>'
        return f'<c r="{ref}"{s_attr}><v>{value}</v></c>'
    return f'<c r="{ref}"{s_attr} t="inlineStr"><is><t xml:space="preserve">{_text(value)}</t></is></c>'


def _style_of(kind: str, value) -> int | None:
    if kind == "wrap":
        return STYLE["wrap"]
    if kind == "num" and isinstance(value, float):
        return STYLE["num"]
    if kind == "auto" and isinstance(value, float) and 0 <= value <= 1:
        return STYLE["pct"]
    return None


def _worksheet(sheet: dict) -> str:
    columns, rows = sheet["columns"], sheet["rows"]
    last_col = _col(len(columns) - 1)
    parts = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
             '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
             'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">',
             '<sheetViews><sheetView workbookViewId="0">'
             '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>',
             '<sheetFormatPr defaultRowHeight="15"/><cols>']
    for i, (_, width, _kind) in enumerate(columns):
        parts.append(f'<col min="{i + 1}" max="{i + 1}" width="{width}" customWidth="1"/>')
    parts.append("</cols><sheetData>")
    head = "".join(_cell(f"{_col(i)}1", title, "text", STYLE["head"]) for i, (title, _, _) in enumerate(columns))
    parts.append(f'<row r="1">{head}</row>')
    for r, row in enumerate(rows, start=2):
        bold = sheet.get("bold_last") and r == len(rows) + 1
        cells = []
        for i, (_, _, kind) in enumerate(columns):
            value = row[i] if i < len(row) else None
            style = STYLE["bold"] if bold and not isinstance(value, dt.date) else _style_of(kind, value)
            if kind == "auto" and isinstance(value, float) and 0 <= value <= 1:
                cells.append(_cell(f"{_col(i)}{r}", value, "pct", STYLE["pct"]))
            else:
                cells.append(_cell(f"{_col(i)}{r}", value, kind, style))
        parts.append(f'<row r="{r}">{"".join(cells)}</row>')
    parts.append("</sheetData>")
    if not sheet.get("plain"):
        parts.append(f'<autoFilter ref="A1:{last_col}{max(1, len(rows) + 1)}"/>')
    parts.append("</worksheet>")
    return "".join(parts)


STYLES_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
    '<numFmts count="3"><numFmt numFmtId="164" formatCode="dd.mm.yyyy"/><numFmt numFmtId="165" formatCode="0%"/>'
    '<numFmt numFmtId="166" formatCode="0.0"/></numFmts>'
    '<fonts count="3"><font><sz val="11"/><name val="Calibri"/><family val="2"/></font>'
    '<font><b/><sz val="11"/><name val="Calibri"/><family val="2"/></font>'
    '<font><u/><sz val="11"/><color rgb="FF1155CC"/><name val="Calibri"/><family val="2"/></font></fonts>'
    '<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill>'
    '<fill><patternFill patternType="solid"><fgColor rgb="FFE8EEF5"/><bgColor indexed="64"/></patternFill></fill></fills>'
    '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
    '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
    '<cellXfs count="8">'
    '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
    '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1" applyAlignment="1">'
    '<alignment vertical="center" wrapText="1"/></xf>'
    '<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
    '<xf numFmtId="165" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
    '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf>'
    '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/>'
    '<xf numFmtId="166" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
    '<xf numFmtId="0" fontId="2" fillId="0" borderId="0" xfId="0" applyFont="1"/>'
    '</cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
    '</styleSheet>')


def workbook(book: list) -> bytes:
    """The .xlsx file of the sheets: [{"name", "columns": [(title, width, kind)], "rows": [[…]]}]."""
    used, names = set(), []
    for sheet in book:
        names.append(_sheet_name(sheet["name"], used))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
        overrides = "".join(
            f'<Override PartName="/xl/worksheets/sheet{i + 1}.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            for i in range(len(book)))
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   '<Default Extension="xml" ContentType="application/xml"/>'
                   '<Override PartName="/xl/workbook.xml" '
                   'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                   '<Override PartName="/xl/styles.xml" '
                   'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
                   f'{overrides}</Types>')
        z.writestr("_rels/.rels",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" '
                   'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
                   'Target="xl/workbook.xml"/></Relationships>')
        sheets_xml = "".join(f'<sheet name="{escape(name, {chr(34): "&quot;"})}" sheetId="{i + 1}" r:id="rId{i + 1}"/>'
                             for i, name in enumerate(names))
        defined = "".join(
            f'<definedName name="_xlnm._FilterDatabase" localSheetId="{i}" hidden="1">'
            f"{escape(chr(39) + name.replace(chr(39), chr(39) * 2) + chr(39))}!$A$1:${_col(len(sheet['columns']) - 1)}"
            f"${max(1, len(sheet['rows']) + 1)}</definedName>"
            for i, (name, sheet) in enumerate(zip(names, book)) if not sheet.get("plain"))
        z.writestr("xl/workbook.xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                   'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                   f'<bookViews><workbookView/></bookViews><sheets>{sheets_xml}</sheets>'
                   f'{"<definedNames>" + defined + "</definedNames>" if defined else ""}</workbook>')
        rels = "".join(f'<Relationship Id="rId{i + 1}" '
                       'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
                       f'Target="worksheets/sheet{i + 1}.xml"/>' for i in range(len(book)))
        z.writestr("xl/_rels/workbook.xml.rels",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   f'{rels}<Relationship Id="rId{len(book) + 1}" '
                   'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
                   'Target="styles.xml"/></Relationships>')
        z.writestr("xl/styles.xml", STYLES_XML)
        for i, sheet in enumerate(book):
            z.writestr(f"xl/worksheets/sheet{i + 1}.xml", _worksheet(sheet))
    return buffer.getvalue()


def export(s, f: dict) -> bytes:
    return workbook(sheets(s, f))
