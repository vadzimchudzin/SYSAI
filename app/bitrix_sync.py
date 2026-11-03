"""Битрикс24 ↔ SYSAI (№28): the tasks and the clients are the same in both places.

Tasks. What SYSAI gives to people (meetings, orders from the chat, documents, letters) is put into Bitrix24 for the
person with «ID в Bitrix24». For a linked task the status, the deadline, the title and the person follow the side where
they changed: `BitrixLink.base` keeps the values each side had at the last sync, so a field that differs from it changed
on that side (both changed: Bitrix24 wins and the log says so). New tasks set in Bitrix24 for our people come here as
tasks without a meeting. Clients. The companies of the CRM ↔ the cards of «Объекты и клиенты»: the name, the УНП and
the kind both ways; the phones, e-mails, site and contact persons of the CRM as a block of the card's contacts; new
client and contractor cards made here go to the CRM. Everything goes through the incoming webhook (rights: task, user,
crm) every 10 minutes in the background loop, and by the button on «Интеграции»."""
import datetime as dt
import logging
import re
import threading
import time

from sqlalchemy import or_

from . import bitrix, config, entities, reminders, settings_store
from .db import BitrixLink, Employee, Entity, SessionLocal, Task, now

log = logging.getLogger("sysai.bitrix_sync")

POLL_MINUTES = 10
RETRY_MINUTES = 30
USERS_HOURS = 24
MAX_PAGES = 10          # one run reads at most 500 changed tasks, companies or contacts; the next run goes on
MAX_CREATE = 20         # tasks or cards put into Bitrix24 (or legacy tasks looked up) by one run
MAX_FETCH = 20          # companies fetched by one run for the contact persons whose company is not linked yet
MAX_ERRORS = 3          # this many failed calls in a row: the portal is down or the webhook lost its rights
LOG_KEEP = 30
TOGGLES = ("bitrix_enabled", "bitrix_sync_tasks", "bitrix_import_tasks", "bitrix_sync_contacts")
SCOPES = {"task": "задачи", "user": "пользователи", "crm": "CRM"}
OPEN_LOCAL = ("sent", "awaiting_deadline", "no_recipient")
LIVE_LOCAL = ("sent", "awaiting_deadline", "in_progress", "blocked")
STATUS_RU = {"open": "ждёт выполнения", "in_progress": "в работе", "blocked": "отложена", "done": "выполнена"}
FIELD_RU = {"title": "название", "deadline": "срок", "status": "статус", "responsible": "исполнитель",
            "name": "название", "unp": "УНП", "kind": "тип"}
TASK_FIELDS = ("title", "deadline", "status", "responsible")
CARD_FIELDS = ("name", "unp", "kind")
TASK_SELECT = ["ID", "TITLE", "STATUS", "DEADLINE", "RESPONSIBLE_ID", "CHANGED_DATE", "CREATED_DATE", "DESCRIPTION"]
COMPANY_SELECT = ["ID", "TITLE", "COMPANY_TYPE", "PHONE", "EMAIL", "WEB", "ADDRESS", "ADDRESS_CITY", "COMMENTS",
                  "DATE_MODIFY"]
CONTACT_SELECT = ["ID", "NAME", "LAST_NAME", "SECOND_NAME", "POST", "COMPANY_ID", "PHONE", "EMAIL", "DATE_MODIFY"]
KIND_TO_TYPE = {"client": "CUSTOMER", "contractor": "SUPPLIER", "org": "OTHER"}
TYPE_TO_KIND = {"CUSTOMER": "client", "SUPPLIER": "contractor"}
BLOCK_START, BLOCK_END = "[Битрикс24]", "[/Битрикс24]"
_BLOCK = re.compile(re.escape(BLOCK_START) + r".*?" + re.escape(BLOCK_END), re.S)
_GONE = ("not found", "не найден", "TASK_NOT_FOUND", "ACCESS_DENIED", "not exist", "Not found")
_LOCK = threading.Lock()


class Abort(RuntimeError):
    """The run stops: the portal does not answer or the webhook is refused."""


# ----------------------------------------------------------------------------------------------- the switches
def configured() -> bool:
    return bool(config.bitrix_url())


def _on(key: str) -> bool:
    return bool(settings_store.get(key))


def active() -> bool:
    return configured() and any(_on(k) for k in TOGGLES)


def _iso(moment=None) -> str:
    return (moment or now()).isoformat(timespec="seconds")


def _parse(value):
    """A date-time of the portal («2026-03-02T18:00:00+03:00») or of the state; None for an empty or a broken one."""
    if not value:
        return None
    try:
        moment = dt.datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=reminders.zone())


def switched(updates: dict) -> None:
    """The switches of «Интеграции» were saved: a switch turned on starts from now (old tasks are not taken), the
    clients start from the whole CRM."""
    state = dict(settings_store.get("bitrix_state") or {})
    stamp = _iso()
    for key, marks in (("bitrix_enabled", ("create_since",)), ("bitrix_sync_tasks", ("tasks_changed",)),
                       ("bitrix_import_tasks", ("import_since", "tasks_changed")),
                       ("bitrix_sync_contacts", ("contacts_since",))):
        if not updates.get(key):
            continue
        fresh = not _on(key)
        for mark in marks:
            if not state.get(mark) or (fresh and mark != "tasks_changed"):
                state[mark] = stamp
        if key == "bitrix_sync_contacts" and fresh:
            state.pop("companies_changed", None)  # the whole CRM is read again
            state.pop("contacts_changed", None)
    settings_store.set_many({**updates, "bitrix_state": state})


# ----------------------------------------------------------------------------------------------- the portal
def _call(method: str, params: dict) -> dict:
    if not config.TESTING:
        time.sleep(0.4)  # the portal takes about two calls a second
    return bitrix.call_full(method, params)


def _result(method: str, params: dict):
    return _call(method, params).get("result")


def _list(method: str, params: dict, key: str = "", pages: int = MAX_PAGES) -> tuple:
    """The items of a list method page by page: (items, more) — more: the run stopped before the end."""
    items, start = [], 0
    for _ in range(pages):
        data = _call(method, {**params, "start": start})
        result = data.get("result")
        if key and isinstance(result, dict):
            result = result.get(key)
        items.extend(result or [])
        nxt = data.get("next")
        if not nxt:
            return items, False
        start = int(nxt)
    return items, True


def _gone(exc) -> bool:
    text = str(exc)
    return any(word.lower() in text.lower() for word in _GONE)


def _norm(row) -> dict:
    """tasks.task.* answer in camelCase, the CRM in UPPER_CASE: one shape for both."""
    return {re.sub(r"[^a-z]", "", str(k).lower()): v for k, v in (row or {}).items()} if isinstance(row, dict) else {}


def check() -> str:
    """«Проверить»: who the webhook is and which rights it has."""
    who = bitrix.check()
    try:
        scopes = set(_result("scope", {}) or [])
    except bitrix.BitrixError:
        scopes = set()
    missing = [ru for name, ru in SCOPES.items() if scopes and name not in scopes]
    return who + (f". Не хватает прав вебхука: {', '.join(missing)} — добавьте их в настройках вебхука."
                  if missing else "")


# ----------------------------------------------------------------------------------------------- the people
def _words(text: str) -> set:
    return {w for w in re.findall(r"[а-яёa-z]+", str(text or "").lower().replace("ё", "е")) if len(w) > 1}


def refresh_users() -> list:
    """The users of the portal for matching the employees (kept in the settings, read once a day)."""
    rows, _ = _list("user.get", {"FILTER": {"ACTIVE": True}}, pages=20)
    users = []
    for row in rows:
        r = _norm(row)
        name = " ".join(str(r.get(k) or "").strip() for k in ("name", "secondname", "lastname")).split()
        users.append({"id": str(r.get("id") or ""), "name": " ".join(name)[:200],
                      "email": str(r.get("email") or "").strip().lower()[:200],
                      "position": str(r.get("workposition") or "").strip()[:200]})
    users = [u for u in users if u["id"]]
    state = dict(settings_store.get("bitrix_state") or {})
    state["users_at"] = _iso()
    settings_store.set_many({"bitrix_users": users[:2000], "bitrix_state": state})
    return users


def match_employees(users=None) -> list:
    """«ID в Bitrix24» of the employees without one: the same work e-mail, else the same name and surname (only when
    one user fits). Returns the names of the matched employees."""
    users = users if users is not None else settings_store.get("bitrix_users") or []
    matched = []
    with SessionLocal() as s:
        people = s.query(Employee).filter(Employee.active.is_(True)).order_by(Employee.id).all()
        taken = {str(e.bitrix_user_id) for e in people if e.bitrix_user_id}
        for e in people:
            if e.bitrix_user_id:
                continue
            free = [u for u in users if u["id"] not in taken]
            fits = [u for u in free if e.email and u.get("email") and u["email"] == e.email.strip().lower()]
            if not fits:
                mine = [_words(n) for n in [e.name] + e.alias_list()]
                fits = [u for u in free if len(_words(u["name"])) >= 2
                        and any(_words(u["name"]) <= words for words in mine)]
            if len(fits) == 1:
                e.bitrix_user_id = fits[0]["id"]
                taken.add(fits[0]["id"])
                matched.append(e.name)
        s.commit()
    return matched


def save_matching(pairs: dict) -> int:
    """The table «Сотрудники ↔ пользователи Битрикс24»: {employee_id: user id or ""}."""
    changed = 0
    with SessionLocal() as s:
        for employee_id, user_id in pairs.items():
            e = s.get(Employee, int(employee_id))
            value = re.sub(r"[^0-9]", "", str(user_id or ""))[:40]
            if e is not None and (e.bitrix_user_id or "") != value:
                e.bitrix_user_id = value
                changed += 1
        s.commit()
    return changed


# ----------------------------------------------------------------------------------------------- the tasks
def _category_local(status: str) -> str:
    if status in ("done", "assistant_done"):
        return "done"
    return status if status in ("in_progress", "blocked") else "open"


def _category_remote(status) -> str:
    return {"3": "in_progress", "4": "done", "5": "done", "6": "blocked", "7": "blocked"}.get(str(status or ""), "open")


def local_task(task) -> dict:
    return {"title": " ".join((task.title or "").split())[:500],
            "deadline": task.deadline.isoformat() if task.deadline else "",
            "status": _category_local(task.status),
            "responsible": str(task.assignee.bitrix_user_id or "") if task.assignee else ""}


def remote_task(row) -> dict:
    r = _norm(row)
    moment = _parse(r.get("deadline"))
    return {"title": " ".join(str(r.get("title") or "").split())[:500],
            "deadline": moment.date().isoformat() if moment else "",
            "status": _category_remote(r.get("status")), "responsible": str(r.get("responsibleid") or "")}


def _status_methods(old: str, new: str) -> list:
    """How Bitrix24 is moved from one status to another (it has a method for each step, not a field)."""
    if new == old:
        return []
    back = ["tasks.task.renew"] if old in ("done", "blocked") else []
    if new == "done":
        return ["tasks.task.complete"]
    if new == "in_progress":
        return back + ["tasks.task.start"]
    if new == "blocked":
        return (["tasks.task.renew"] if old == "done" else []) + ["tasks.task.defer"]
    return back or (["tasks.task.pause"] if old == "in_progress" else [])


def _deadline_out(day: str) -> str:
    return f"{day}T18:00:00" if day else ""


def _plain_text(text: str) -> str:
    """The description of Bitrix24 without its BB-codes ([B], [URL=…], [USER=5]…)."""
    return re.sub(r"\[/?[A-Za-z*]+(=[^\]]*)?\]", "", str(text or "")).strip()


def _apply_task(s, task, field: str, value: str, notes: list) -> bool:
    """A change made in Bitrix24 → the task here. False when it cannot be taken (a person not in the directory)."""
    if field == "title":
        if value:
            task.title = value[:500]
    elif field == "deadline":
        task.deadline = dt.date.fromisoformat(value) if value else None
        task.deadline_source = "manual" if value else "none"
        if value and task.status == "awaiting_deadline":
            task.status = "sent"
    elif field == "status":
        if value != "open":
            task.status = value
        elif task.status not in OPEN_LOCAL:
            task.status = "sent"
    elif field == "responsible":
        person = (s.query(Employee).filter(Employee.bitrix_user_id == value, Employee.active.is_(True)).first()
                  if value else None)
        if person is None:
            notes.append(f"задача «{task.title[:80]}»: в Битрикс24 исполнитель — пользователь {value or '—'}, его нет "
                         "среди сотрудников (заполните «ID в Bitrix24»)")
            return False
        task.assignee_id, task.assignee_name = person.id, person.name
        task.assignee = person
    return True


def _push_task(remote_id: str, fields: dict, old_status: str) -> None:
    """Changes made here → Bitrix24."""
    updates = {}
    if fields.get("title"):
        updates["TITLE"] = fields["title"]
    if "deadline" in fields:
        updates["DEADLINE"] = _deadline_out(fields["deadline"])
    if fields.get("responsible"):
        updates["RESPONSIBLE_ID"] = fields["responsible"]
    if updates:
        _call("tasks.task.update", {"taskId": remote_id, "fields": updates})
    if "status" in fields:
        for method in _status_methods(old_status, fields["status"]):
            _call(method, {"taskId": remote_id})


def _merge_task(s, link, task, remote: dict, notes: list, counts: dict) -> None:
    """Three-way: a field that changed in Bitrix24 comes here (it wins when both changed), one that changed only
    here goes there."""
    base = dict(link.base or {})
    seen_local, seen_remote = dict(base.get("local") or {}), dict(base.get("remote") or {})
    mine = local_task(task)
    to_push = {}
    for field in TASK_FIELDS:
        here_changed = mine[field] != seen_local.get(field)
        there_changed = remote is not None and remote[field] != seen_remote.get(field)
        if there_changed:
            if here_changed and mine[field] != remote[field]:
                notes.append(f"задача «{task.title[:80]}»: {FIELD_RU[field]} изменили и здесь, и в Битрикс24 — "
                             "взято из Битрикс24")
            if mine[field] != remote[field] and _apply_task(s, task, field, remote[field], notes):
                counts["tasks_in"] = counts.get("tasks_in", 0) + 1
            seen_remote[field] = remote[field]
            seen_local[field] = local_task(task)[field]
        elif here_changed:
            if field == "responsible" and not mine[field]:
                seen_local[field] = mine[field]  # the new person has no Bitrix24 ID: nothing to send
                notes.append(f"задача «{task.title[:80]}»: у исполнителя нет «ID в Bitrix24» — в Битрикс24 исполнитель "
                             "прежний")
                continue
            to_push[field] = mine[field]
    if to_push:
        _push_task(link.remote_id, to_push, seen_remote.get("status") or "open")
        seen_local.update(to_push)
        seen_remote.update(to_push)
        counts["tasks_out"] = counts.get("tasks_out", 0) + 1
    link.base = {"local": seen_local, "remote": seen_remote}
    link.error, link.synced_at = "", now()


def _task_description(task) -> str:
    return task.description or ""


def _create_tasks(s, state: dict, counts: dict, notes: list) -> None:
    """Tasks given here since «Ставить задачи в Битрикс24» was on (the person has a Bitrix24 ID) → Bitrix24."""
    since = _parse(state.get("create_since"))
    if since is None:
        return
    since = since.astimezone(dt.timezone.utc)
    rows = (s.query(Task).join(Employee, Task.assignee_id == Employee.id)
            .filter(or_(Task.bitrix_task_id == "", Task.bitrix_task_id.is_(None)), Task.executor == "person",
                    Task.status.in_(LIVE_LOCAL), Employee.bitrix_user_id.isnot(None), Employee.bitrix_user_id != "",
                    Task.created_at >= since)
            .order_by(Task.id).limit(MAX_CREATE).all())
    for task in rows:
        try:
            remote_id = bitrix.create_task(task, task.meeting)
            if not remote_id:
                raise bitrix.BitrixError("Битрикс24 не вернул номер задачи")
            task.bitrix_task_id = remote_id
            mine = local_task(task)
            for method in _status_methods("open", mine["status"]):
                _call(method, {"taskId": remote_id})
            s.add(BitrixLink(kind="task", local_id=task.id, remote_id=str(remote_id),
                             base={"local": mine, "remote": dict(mine)}))
            s.commit()
            counts["tasks_created"] = counts.get("tasks_created", 0) + 1
        except bitrix.BitrixError as exc:
            s.rollback()
            _failed(state, exc)
            notes.append(f"задача «{task.title[:80]}» не поставлена в Битрикс24: {str(exc)[:160]}")


def _adopt_legacy(s, state: dict, counts: dict, notes: list) -> None:
    """Tasks put into Bitrix24 before the sync was on (only the number is known): linked, and what changed in
    Bitrix24 since then comes here."""
    linked = {l.local_id for l in s.query(BitrixLink.local_id).filter(BitrixLink.kind == "task")}
    rows = (s.query(Task).filter(Task.bitrix_task_id.isnot(None), Task.bitrix_task_id != "")
            .order_by(Task.id.desc()).limit(500).all())
    todo = [t for t in rows if t.id not in linked][:MAX_CREATE]
    for task in todo:
        if s.query(BitrixLink.id).filter_by(kind="task", remote_id=str(task.bitrix_task_id)).first():
            continue
        mine = local_task(task)
        link = BitrixLink(kind="task", local_id=task.id, remote_id=str(task.bitrix_task_id),
                          base={"local": mine, "remote": dict(mine)})
        s.add(link)
        try:
            row = (_result("tasks.task.get", {"taskId": task.bitrix_task_id, "select": TASK_SELECT}) or {}).get("task")
        except bitrix.BitrixError as exc:
            if not _gone(exc):
                s.rollback()
                _failed(state, exc)
                return
            row = None
            link.state, link.error = "gone", "задачи нет в Битрикс24"
        if row:
            remote = remote_task(row)
            seen_local, seen_remote = dict(mine), dict(mine)  # what differs was changed in Bitrix24 (never sent)
            rank = {"open": 0, "in_progress": 1, "blocked": 1, "done": 2}
            if rank[mine["status"]] > rank[remote["status"]]:
                # the status here moved further (it was never sent before): it goes there
                seen_remote["status"], seen_local["status"] = remote["status"], ""
            link.base = {"local": seen_local, "remote": seen_remote}
            _merge_task(s, link, task, remote, notes, counts)
        s.commit()
        counts["tasks_linked"] = counts.get("tasks_linked", 0) + 1


def _import_task(s, row, state: dict, counts: dict) -> None:
    """A new task set in Bitrix24 for one of our people → a task here (without a meeting)."""
    r = _norm(row)
    since, created = _parse(state.get("import_since")), _parse(r.get("createddate"))
    if since is None or created is None or created < since:
        return
    remote = remote_task(row)
    person = (s.query(Employee).filter(Employee.bitrix_user_id == remote["responsible"], Employee.active.is_(True))
              .first() if remote["responsible"] else None)
    if person is None or remote["status"] == "done" or not remote["title"]:
        return
    remote_id = str(r.get("id"))
    task = s.query(Task).filter(Task.bitrix_task_id == remote_id).first()
    if task is None:
        task = Task(meeting_id=None, title=remote["title"], description=_plain_text(r.get("description"))[:4000],
                    assignee_id=person.id, assignee_name=person.name, executor="person",
                    deadline=dt.date.fromisoformat(remote["deadline"]) if remote["deadline"] else None,
                    deadline_source="manual" if remote["deadline"] else "none",
                    status="sent" if remote["status"] == "open" else remote["status"],
                    source_quote=f"Задача из Битрикс24 №{remote_id}", bitrix_task_id=remote_id)
        s.add(task)
        s.flush()
        task.assignee = person
        counts["tasks_imported"] = counts.get("tasks_imported", 0) + 1
    s.add(BitrixLink(kind="task", local_id=task.id, remote_id=remote_id,
                     base={"local": local_task(task), "remote": remote}))


def _sync_tasks(s, state: dict, counts: dict, notes: list) -> None:
    if _on("bitrix_enabled"):
        _create_tasks(s, state, counts, notes)
    if not (_on("bitrix_sync_tasks") or _on("bitrix_import_tasks")):
        return
    if _on("bitrix_sync_tasks"):
        _adopt_legacy(s, state, counts, notes)
    mark = state.get("tasks_changed") or _iso()
    rows, more = _list("tasks.task.list", {"filter": {">=CHANGED_DATE": mark}, "select": TASK_SELECT,
                                           "order": {"CHANGED_DATE": "asc", "ID": "asc"}}, key="tasks")
    latest = _parse(mark)
    pulled = set()
    for row in rows:
        r = _norm(row)
        remote_id = str(r.get("id") or "")
        changed = _parse(r.get("changeddate"))
        if changed is not None and (latest is None or changed > latest):
            latest = changed
        if not remote_id:
            continue
        link = s.query(BitrixLink).filter_by(kind="task", remote_id=remote_id).first()
        if link is None:
            if _on("bitrix_import_tasks"):
                _import_task(s, row, state, counts)
                s.commit()
            continue
        if link.state != "ok" or not _on("bitrix_sync_tasks"):
            continue
        task = s.get(Task, link.local_id)
        if task is None:
            link.state = "gone"
            continue
        _merge_task(s, link, task, remote_task(row), notes, counts)
        pulled.add(link.id)
        s.commit()
    if latest is not None:
        state["tasks_changed"] = _iso(latest)
    state["tasks_more"] = more
    if not _on("bitrix_sync_tasks"):
        return
    errors = 0
    for link in s.query(BitrixLink).filter(BitrixLink.kind == "task", BitrixLink.state == "ok").all():
        if link.id in pulled:
            continue
        task = s.get(Task, link.local_id)
        if task is None:
            link.state = "gone"
            continue
        seen = (link.base or {}).get("local") or {}
        if local_task(task) == {f: seen.get(f) for f in TASK_FIELDS}:
            continue
        try:
            _merge_task(s, link, task, None, notes, counts)
            s.commit()
            errors = 0
        except bitrix.BitrixError as exc:
            s.rollback()
            if _gone(exc):
                link = s.get(BitrixLink, link.id)
                link.state, link.error = "gone", "задачи нет в Битрикс24"
                s.commit()
                notes.append(f"задача «{task.title[:80]}»: в Битрикс24 её больше нет — больше не сверяется")
                continue
            errors += 1
            _failed(state, exc, errors)


# ----------------------------------------------------------------------------------------------- the clients
def _multi(values) -> list:
    out = []
    for v in values or []:
        value = str(v.get("VALUE") or "").strip() if isinstance(v, dict) else ""
        if value and value not in out:
            out.append(value[:120])
    return out


def block(data: dict) -> str:
    """The part of the card's contacts that comes from the CRM (replaced at every sync, the rest is left alone)."""
    lines = []
    if data.get("phones"):
        lines.append("Тел.: " + ", ".join(data["phones"][:5]))
    if data.get("emails"):
        lines.append("Почта: " + ", ".join(data["emails"][:5]))
    if data.get("web"):
        lines.append("Сайт: " + ", ".join(data["web"][:3]))
    for p in sorted((data.get("people") or {}).values(), key=lambda p: p.get("name") or "")[:10]:
        bits = [p.get("name") or "контакт"] + ([f"({p['post']})"] if p.get("post") else [])
        bits += (p.get("phones") or [])[:2] + (p.get("emails") or [])[:2]
        lines.append("• " + " ".join(bits))
    if not lines:
        return ""
    text = "\n".join(lines)[:900]
    return f"{BLOCK_START}\n{text}\n{BLOCK_END}"


def own_contacts(contacts: str) -> str:
    """The contacts of the card written here (without the block of the CRM)."""
    return _BLOCK.sub("", str(contacts or "")).strip()


def with_block(contacts: str, text: str) -> str:
    rest = own_contacts(contacts)
    if not text:
        return rest[:2000]
    rest = rest[:max(0, 2000 - len(text) - 2)].rstrip()
    return (rest + "\n\n" + text if rest else text)[:2000]


def _card_kind(kind: str) -> str:
    return kind if kind in ("client", "contractor") else "org"


def local_card(e) -> dict:
    return {"name": entities.key(e.name), "unp": re.sub(r"\D", "", e.unp or ""), "kind": _card_kind(e.kind)}


def remote_card(row: dict, unp: str) -> dict:
    return {"name": entities.key(str(row.get("TITLE") or "")), "unp": re.sub(r"\D", "", unp or ""),
            "kind": TYPE_TO_KIND.get(str(row.get("COMPANY_TYPE") or "").upper(), "org")}


def _requisites(ids: list) -> dict:
    """{company id: {"id": requisite id, "unp"}} — the УНП (ИНН) is in the requisites of the company."""
    if not ids:
        return {}
    rows, _ = _list("crm.requisite.list", {"filter": {"ENTITY_TYPE_ID": 4, "@ENTITY_ID": ids},
                                           "select": ["ID", "ENTITY_ID", "RQ_INN"]}, pages=min(12, len(ids) // 50 + 2))
    out = {}
    for row in rows:
        company = str(row.get("ENTITY_ID") or "")
        if company and company not in out:
            out[company] = {"id": str(row.get("ID") or ""), "unp": re.sub(r"\D", "", str(row.get("RQ_INN") or ""))}
    return out


def _apply_card(s, e, field: str, row: dict, remote: dict, notes: list) -> bool:
    if field == "name":
        name = " ".join(str(row.get("TITLE") or "").split())[:300]
        if len(entities.key(name)) < entities.MIN_NAME:
            return False
        for other in s.query(Entity).filter(Entity.merged_into_id.is_(None), Entity.id != e.id):
            if entities.key(name) in entities._names(other):
                notes.append(f"компания «{name}»: такое название уже у карточки «{other.name}» — объедините их")
                return False
        old = e.name
        e.name = name
        e.aliases = entities._clean_aliases(list(e.aliases or []) + [old], name)
        entities._source(e, "name", {"type": "bitrix", "id": row.get("ID")})
        entities._invalidate()
    elif field == "unp":
        e.unp = remote["unp"][:20]
        entities._source(e, "unp", {"type": "bitrix", "id": row.get("ID")})
    elif field == "kind":
        e.kind = remote["kind"]
    return True


def _push_card(link, e, fields: dict) -> None:
    updates = {}
    if "name" in fields:
        updates["TITLE"] = e.name
    if "kind" in fields:
        updates["COMPANY_TYPE"] = KIND_TO_TYPE.get(fields["kind"], "OTHER")
    if updates:
        _call("crm.company.update", {"id": link.remote_id, "fields": updates})
    if "unp" in fields and fields["unp"]:
        data = dict(link.data or {})
        if data.get("requisite"):
            _call("crm.requisite.update", {"id": data["requisite"], "fields": {"RQ_INN": fields["unp"]}})
        else:
            data["requisite"] = _add_requisite(link.remote_id, fields["unp"])
            link.data = data


def _add_requisite(company_id: str, unp: str) -> str:
    presets = _result("crm.requisite.preset.list", {"select": ["ID", "NAME", "ACTIVE"]}) or []
    preset = next((p for p in presets if str(p.get("ACTIVE") or "Y") == "Y"), None)
    if preset is None:
        raise bitrix.BitrixError("в CRM нет шаблона реквизитов — УНП не записан")
    new = _result("crm.requisite.add", {"fields": {"ENTITY_TYPE_ID": 4, "ENTITY_ID": company_id,
                                                   "PRESET_ID": preset["ID"], "NAME": "Реквизиты", "RQ_INN": unp,
                                                   "ACTIVE": "Y"}})
    return str(new or "")


def _refresh_block(s, link) -> None:
    e = entities.resolve(s, s.get(Entity, link.local_id))
    if e is not None:
        e.contacts = with_block(e.contacts, block(link.data or {}))


def _take_company(s, row: dict, requisite: dict | None, notes: list, counts: dict):
    """A company of the CRM: its card here (found by the УНП or the name, else a new one) and what changed."""
    remote_id = str(row.get("ID") or "")
    if not remote_id:
        return None
    requisite = requisite or {}
    remote = remote_card(row, requisite.get("unp", ""))
    link = s.query(BitrixLink).filter_by(kind="company", remote_id=remote_id).first()
    if link is not None and link.state != "ok":
        return None
    e = entities.resolve(s, s.get(Entity, link.local_id)) if link else None
    if link is not None and e is None:
        link.state, link.error = "gone", "карточку удалили здесь"  # deleted on purpose: it does not come back
        return None
    address = ", ".join(x for x in (str(row.get("ADDRESS") or "").strip(), str(row.get("ADDRESS_CITY") or "").strip())
                        if x)
    if link is None:
        name = " ".join(str(row.get("TITLE") or "").split())[:300]
        e = entities.get_or_create(s, name, remote["kind"], source="bitrix", unp=remote["unp"],
                                   origin={"type": "bitrix", "id": remote_id})
        if e is None:
            return None
        e = entities.resolve(s, e)
        other = s.query(BitrixLink).filter_by(kind="company", local_id=e.id).first()
        if other is not None:
            notes.append(f"в CRM две компании для карточки «{e.name}» (ID {other.remote_id} и {remote_id}) — "
                         "объедините их в Битрикс24")
            return None
        _fill_blanks(e, remote, address, remote_id)
        link = BitrixLink(kind="company", local_id=e.id, remote_id=remote_id, data={},
                          base={"local": local_card(e), "remote": remote})
        s.add(link)
        s.flush()
        counts["cards_linked"] = counts.get("cards_linked", 0) + 1
    else:
        base = dict(link.base or {})
        seen_local, seen_remote = dict(base.get("local") or {}), dict(base.get("remote") or {})
        mine = local_card(e)
        for field in CARD_FIELDS:
            if remote[field] == seen_remote.get(field) or (field == "unp" and not remote["unp"]):
                continue
            if mine[field] != seen_local.get(field) and mine[field] != remote[field]:
                notes.append(f"«{e.name}»: {FIELD_RU[field]} изменили и здесь, и в CRM — взято из CRM")
            if mine[field] != remote[field] and _apply_card(s, e, field, row, remote, notes):
                counts["cards_in"] = counts.get("cards_in", 0) + 1
            seen_remote[field] = remote[field]
            seen_local[field] = local_card(e)[field]
        before = local_card(e)["unp"]
        _fill_blanks(e, remote, address, remote_id)
        if local_card(e)["unp"] != before:
            seen_local["unp"] = local_card(e)["unp"]
        link.base = {"local": seen_local, "remote": seen_remote}
    data = dict(link.data or {})
    data.update(phones=_multi(row.get("PHONE")), emails=_multi(row.get("EMAIL")), web=_multi(row.get("WEB")))
    if requisite.get("id"):
        data["requisite"] = requisite["id"]
    link.data = data
    e.contacts = with_block(e.contacts, block(data))
    link.synced_at = now()
    return link


def _fill_blanks(e, remote: dict, address: str, remote_id: str) -> None:
    """What the card does not have yet comes from the CRM (what it has is not overwritten)."""
    if not e.unp and remote["unp"]:
        e.unp = remote["unp"][:20]
        entities._source(e, "unp", {"type": "bitrix", "id": remote_id})
    if address and not e.address:
        e.address = address[:300]
        entities._source(e, "address", {"type": "bitrix", "id": remote_id})


def _person(row: dict) -> dict:
    name = " ".join(str(row.get(k) or "").strip() for k in ("LAST_NAME", "NAME", "SECOND_NAME")).split()
    return {"name": " ".join(name)[:120] or "контакт", "post": str(row.get("POST") or "").strip()[:120],
            "phones": _multi(row.get("PHONE"))[:3], "emails": _multi(row.get("EMAIL"))[:3]}


def _take_contact(s, row: dict, fetched: list, notes: list, counts: dict) -> None:
    """A contact person of the CRM → the block of its company's card (moved to another company: out of the old one)."""
    contact_id = str(row.get("ID") or "")
    company_id = str(row.get("COMPANY_ID") or "")
    if not contact_id:
        return
    company_id = "" if company_id in ("0", "None") else company_id
    target = s.query(BitrixLink).filter_by(kind="company", remote_id=company_id).first() if company_id else None
    if company_id and target is None:
        if len(fetched) >= MAX_FETCH:
            return  # the next run takes it
        fetched.append(company_id)
        try:
            company = _result("crm.company.get", {"id": company_id})
            if company:
                target = _take_company(s, company, _requisites([company_id]).get(company_id), notes, counts)
        except bitrix.BitrixError as exc:
            log.info("company %s of contact %s not read: %s", company_id, contact_id, exc)
        if target is None:
            return
    if target is not None and target.state != "ok":
        return
    mine = s.query(BitrixLink).filter_by(kind="contact", remote_id=contact_id).first()
    if mine is not None:
        old = s.query(BitrixLink).filter_by(kind="company", local_id=mine.local_id, state="ok").first()
        if old is not None and (target is None or old.id != target.id):
            data = dict(old.data or {})
            people = dict(data.get("people") or {})
            if people.pop(contact_id, None) is not None:
                data["people"] = people
                old.data = data
                _refresh_block(s, old)
    if target is None:  # no company any more: only out of the old card
        if mine is not None:
            s.delete(mine)
        return
    data = dict(target.data or {})
    people = dict(data.get("people") or {})
    people[contact_id] = _person(row)
    data["people"] = people
    target.data = data
    _refresh_block(s, target)
    if mine is None:
        s.add(BitrixLink(kind="contact", local_id=target.local_id, remote_id=contact_id, base={}))
    else:
        mine.local_id = target.local_id
    counts["contacts"] = counts.get("contacts", 0) + 1


def _phones_emails(text: str) -> tuple:
    emails = re.findall(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", text or "")
    rest = text or ""
    for email in emails:
        rest = rest.replace(email, " ")
    phones = [" ".join(p.split()) for p in re.findall(r"\+?\d[\d\s()\-]{7,}\d", rest)]
    return phones[:5], emails[:5]


def _push_new_cards(s, state: dict, counts: dict, notes: list) -> None:
    """Client and contractor cards made here since the sync was on → companies of the CRM."""
    since = _parse(state.get("contacts_since"))
    if since is None:
        return
    since = since.astimezone(dt.timezone.utc)
    linked = {l.local_id for l in s.query(BitrixLink.local_id).filter(BitrixLink.kind == "company")}
    rows = (s.query(Entity).filter(Entity.merged_into_id.is_(None), Entity.active.is_(True),
                                   Entity.kind.in_(("client", "contractor")),
                                   or_(Entity.source.is_(None), Entity.source != "bitrix"),
                                   Entity.created_at >= since).order_by(Entity.id).limit(200).all())
    for e in [e for e in rows if e.id not in linked][:MAX_CREATE]:
        phones, emails = _phones_emails(own_contacts(e.contacts))
        fields = {"TITLE": e.name, "COMPANY_TYPE": KIND_TO_TYPE[e.kind], "COMMENTS": (e.description or "")[:2000]}
        if e.address:
            fields["ADDRESS"] = e.address
        if phones:
            fields["PHONE"] = [{"VALUE": p, "VALUE_TYPE": "WORK"} for p in phones]
        if emails:
            fields["EMAIL"] = [{"VALUE": m, "VALUE_TYPE": "WORK"} for m in emails]
        try:
            remote_id = str(_result("crm.company.add", {"fields": fields}) or "")
            if not remote_id:
                raise bitrix.BitrixError("CRM не вернула номер компании")
            link = BitrixLink(kind="company", local_id=e.id, remote_id=remote_id, data={},
                              base={"local": local_card(e), "remote": local_card(e)})
            s.add(link)
            s.commit()
            counts["cards_created"] = counts.get("cards_created", 0) + 1
        except bitrix.BitrixError as exc:
            s.rollback()
            _failed(state, exc)
            notes.append(f"карточка «{e.name}» не попала в CRM: {str(exc)[:160]}")
            continue
        unp = re.sub(r"\D", "", e.unp or "")
        if unp:
            try:
                link = s.get(BitrixLink, link.id)
                link.data = {"requisite": _add_requisite(remote_id, unp)}
                s.commit()
            except bitrix.BitrixError as exc:
                s.rollback()
                notes.append(f"«{e.name}»: УНП не записан в CRM — {str(exc)[:160]}")


def _sync_contacts(s, state: dict, counts: dict, notes: list) -> None:
    mark = state.get("companies_changed") or ""
    rows, more = _list("crm.company.list", {"order": {"DATE_MODIFY": "ASC", "ID": "ASC"}, "select": COMPANY_SELECT,
                                            "filter": {">=DATE_MODIFY": mark} if mark else {}})
    found = _requisites([str(r.get("ID")) for r in rows if r.get("ID")][:500])
    latest = _parse(mark)
    pulled = set()
    for row in rows:
        changed = _parse(row.get("DATE_MODIFY"))
        if changed is not None and (latest is None or changed > latest):
            latest = changed
        link = _take_company(s, row, found.get(str(row.get("ID"))), notes, counts)
        if link is not None:
            pulled.add(link.id)
        s.commit()
    if latest is not None:
        state["companies_changed"] = _iso(latest)
    mark = state.get("contacts_changed") or ""
    people, more_people = _list("crm.contact.list", {"order": {"DATE_MODIFY": "ASC", "ID": "ASC"},
                                                     "select": CONTACT_SELECT,
                                                     "filter": {">=DATE_MODIFY": mark} if mark else {}})
    latest, fetched = _parse(mark), []
    for row in people:
        changed = _parse(row.get("DATE_MODIFY"))
        if changed is not None and (latest is None or changed > latest):
            latest = changed
        _take_contact(s, row, fetched, notes, counts)
        s.commit()
    if latest is not None:
        state["contacts_changed"] = _iso(latest)
    state["contacts_more"] = more or more_people
    errors = 0
    for link in s.query(BitrixLink).filter(BitrixLink.kind == "company", BitrixLink.state == "ok").all():
        if link.id in pulled:
            continue
        e = entities.resolve(s, s.get(Entity, link.local_id))
        if e is None:
            continue
        base = dict(link.base or {})
        seen_local, seen_remote = dict(base.get("local") or {}), dict(base.get("remote") or {})
        mine = local_card(e)
        to_push = {f: mine[f] for f in CARD_FIELDS if mine[f] != seen_local.get(f) and (f != "unp" or mine[f])}
        if not to_push:
            continue
        try:
            _push_card(link, e, to_push)
            seen_local.update(to_push)
            seen_remote.update(to_push)
            link.base = {"local": seen_local, "remote": seen_remote}
            link.synced_at = now()
            s.commit()
            counts["cards_out"] = counts.get("cards_out", 0) + 1
            errors = 0
        except bitrix.BitrixError as exc:
            s.rollback()
            if _gone(exc):
                link = s.get(BitrixLink, link.id)
                link.state, link.error = "gone", "компании нет в CRM"
                s.commit()
                continue
            errors += 1
            _failed(state, exc, errors)
    _push_new_cards(s, state, counts, notes)


# ----------------------------------------------------------------------------------------------- the run
def _failed(state: dict, exc, in_a_row: int = 1) -> None:
    state["last_item_error"] = str(exc)[:300]
    if in_a_row >= MAX_ERRORS:
        raise Abort(str(exc)[:300])


def _log(state: dict, lines: list) -> None:
    stamp = now().astimezone(reminders.zone()).strftime("%d.%m %H:%M")
    entries = [{"at": stamp, "text": t[:300]} for t in lines]
    state["log"] = (entries + list(state.get("log") or []))[:LOG_KEEP]


def _summary(counts: dict) -> str:
    parts = [(counts.get("tasks_created"), "поставлено задач в Битрикс24"),
             (counts.get("tasks_out"), "обновлено задач там"), (counts.get("tasks_in"), "изменений из Битрикс24"),
             (counts.get("tasks_imported"), "новых задач оттуда"), (counts.get("tasks_linked"), "связано прежних задач"),
             (counts.get("cards_linked"), "связано компаний CRM"), (counts.get("cards_in"), "правок карточек из CRM"),
             (counts.get("cards_out"), "правок карточек в CRM"), (counts.get("cards_created"), "новых компаний в CRM"),
             (counts.get("contacts"), "контактных лиц")]
    return ", ".join(f"{label}: {n}" for n, label in parts if n) or "изменений нет"


def sync(force_users: bool = False) -> dict:
    """One sync now (the background loop and «Синхронизировать сейчас»)."""
    if not configured():
        raise bitrix.BitrixError("Вебхук Битрикс24 не задан.")
    with _LOCK:
        state = dict(settings_store.get("bitrix_state") or {})
        counts, notes = {}, []
        state["checked_at"] = _iso()
        try:
            users_at = _parse(state.get("users_at"))
            if force_users or users_at is None or now() - users_at > dt.timedelta(hours=USERS_HOURS):
                refresh_users()
                state["users_at"] = _iso()
                matched = match_employees()
                if matched:
                    notes.append("ID в Bitrix24 подставлен: " + ", ".join(matched[:10]))
            with SessionLocal() as s:
                _sync_tasks(s, state, counts, notes)
                if _on("bitrix_sync_contacts"):
                    _sync_contacts(s, state, counts, notes)
            state.update(ok=True, error="")
        except (bitrix.BitrixError, Abort) as exc:
            state.update(ok=False, error=str(exc)[:300])
            log.info("bitrix sync stopped: %s", exc)
        state["counts"], state["summary"] = counts, _summary(counts)
        if counts or notes or state.get("ok") is False:
            _log(state, ([state["summary"]] if counts else []) + notes[:10] +
                 ([f"ошибка: {state['error']}"] if state.get("ok") is False else []))
        settings_store.set_many({"bitrix_state": state})
        return state


def run_due() -> None:
    """Background loop: every 10 minutes while something is switched on (every 30 after an error)."""
    if not active():
        return
    state = settings_store.get("bitrix_state") or {}
    last = _parse(state.get("checked_at"))
    wait = RETRY_MINUTES if state.get("ok") is False else POLL_MINUTES
    if last is not None and now() - last < dt.timedelta(minutes=wait):
        return
    sync()


def status() -> dict:
    state = dict(settings_store.get("bitrix_state") or {})
    state["configured"] = configured()
    moment = _parse(state.get("checked_at"))
    state["checked_local"] = moment.astimezone(reminders.zone()).strftime("%d.%m %H:%M") if moment else ""
    with SessionLocal() as s:
        state["linked_tasks"] = s.query(BitrixLink).filter_by(kind="task", state="ok").count()
        state["linked_cards"] = s.query(BitrixLink).filter_by(kind="company", state="ok").count()
        people = s.query(Employee).filter(Employee.active.is_(True)).order_by(Employee.name).all()
        state["people"] = [{"id": e.id, "name": e.name, "bitrix": e.bitrix_user_id or "", "email": e.email or ""}
                           for e in people]
    state["users"] = settings_store.get("bitrix_users") or []
    return state
