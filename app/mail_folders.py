"""Отделы и папки секретаря: кому в отделе идут его письма и куда они ложатся в самом ящике.

The company structure («Отделы») is the default: every active department gets the letters of its work. Per department
the «Секретарь» page keeps who receives them in Telegram (the one responsible for the mail of the department; by
default its head), the folder of the box they go to (by default the name of the department) and a few words on what
letters it gets (a hint for the sorting). The sorting (mail.py) names the department of each letter by its meaning;
when no rule «кто чем занимается» fits and the sorting is sure, the letter goes to the responsible person of that
department (mail_route.py); when it is not sure, the department is a hint and the letter waits for a leader.

The folders: the sorted letters are moved in the box itself (IMAP MOVE, the flags and «прочитано» kept) — the special
folders first («Рассылки» for newsletters, by sender or words), then the folder of the department the letter went to
(or the sorting named). A letter that waits for a decision stays in the inbox; passed on later, it moves after it.
Made for Yandex 360 (the parts of a folder name are separated by «|», the names are in modified UTF-7) and works with
any IMAP server that has MOVE or UIDPLUS. Nothing is deleted and no folder is ever removed.
"""
import collections
import datetime as dt
import hashlib
import json
import logging
import re
import threading
import types

from . import mail, settings_store
from .db import Department, Employee, MailMessage, SessionLocal, now

log = logging.getLogger("sysai.mail_folders")

MAX_PART = 100           # letters in one part of a folder name
MAX_DEPTH = 4            # «Отделы/Снабжение/…» — not deeper
MAX_RULES = 30
MAX_DEPARTMENTS = 40     # offered to the sorting
RETRY_MINUTES = 15       # a box or a letter that refused is tried again after this
MAX_TRIES = 3            # a letter that cannot be moved is left where it is after so many tries
LABEL_DAYS = 7           # mail.label_pending looks this far back: such letters get their labels before they move
# the folders of the box itself: a folder of the secretary on the top level is never one of them
RESERVED = {"inbox", "входящие", "sent", "sent items", "sent messages", "отправленные", "spam", "junk", "junk e-mail",
            "спам", "trash", "deleted", "deleted items", "deleted messages", "удалённые", "удаленные", "корзина", "drafts",
            "черновики", "outbox", "исходящие", "[gmail]"}
INBOX_NAMES = ("inbox", "входящие")
_FORBIDDEN = re.compile(r'[\x00-\x1f\x7f*%"\\|]')
_LOCK = threading.Lock()


class FolderError(ValueError):
    """A folder or a setting that cannot be saved (Russian, for the page)."""


# ----------------------------------------------------------------------------- the names of the folders
def clean_path(value, *, top: bool = True) -> str:
    """«Отделы / Бухгалтерия» → «Отделы/Бухгалтерия»; "" stays "". `top`: the path starts at the top of the box, so its
    first part is not one of the folders of the box itself («Входящие», «Спам», …)."""
    parts = []
    for raw in str(value or "").split("/"):
        part = " ".join(raw.split())
        if not part:
            continue
        if _FORBIDDEN.search(part):
            raise FolderError(f"в имени папки «{part[:40]}» есть недопустимые знаки (* % \" \\ |)")
        if len(part) > MAX_PART:
            raise FolderError(f"имя папки «{part[:40]}…» длиннее {MAX_PART} знаков")
        if part in (".", ".."):
            raise FolderError("имя папки — не «.» и не «..»")
        parts.append(part)
    if len(parts) > MAX_DEPTH:
        raise FolderError(f"не больше {MAX_DEPTH} уровней вложенных папок")
    if parts and top and parts[0].casefold() in RESERVED:
        raise FolderError(f"«{parts[0]}» — служебная папка ящика, выберите другое имя")
    return "/".join(parts)


def clean_root(value) -> str:
    """Where the folders are made: "" — the top level; «Входящие» (INBOX) — inside the inbox; or a folder of its own."""
    parts = [" ".join(p.split()) for p in str(value or "").split("/") if p.strip()]
    if parts and parts[0].casefold() in INBOX_NAMES:
        rest = clean_path("/".join(parts[1:]), top=False)
        return "/".join(["INBOX"] + ([rest] if rest else []))
    return clean_path(value)


def root() -> str:
    try:
        return clean_root(settings_store.get("mail_folders_root") or "")
    except FolderError:
        return ""


def default_folder(name: str) -> str:
    """The folder of a department by its name («Снабжение/логистика» → «Снабжение-логистика»)."""
    return " ".join(_FORBIDDEN.sub(" ", str(name or "").replace("/", "-")).split())[:MAX_PART]


def path_of(root_path: str, folder: str) -> str:
    """The whole path of a folder of the secretary ("" — none, or a name the box would not take)."""
    try:
        folder = clean_path(folder, top=not root_path)
    except FolderError:
        return ""
    return "/".join(p for p in (root_path, folder) if p) if folder else ""


def parts(path: str) -> list:
    return [p for p in str(path or "").split("/") if p]


def shown(path: str) -> str:
    """«INBOX/Бухгалтерия» → «Входящие / Бухгалтерия» for the page."""
    items = parts(path)
    if items and items[0] == "INBOX":
        items[0] = "Входящие"
    return " / ".join(items) or "Входящие"


# ----------------------------------------------------------------------------- the departments
def _confs() -> dict:
    raw = settings_store.get("mail_departments")
    return raw if isinstance(raw, dict) else {}


def conf_of(department, confs=None) -> dict:
    """{"on", "to", "folder", "letters"} of a department, the defaults where nothing was saved."""
    c = (confs if confs is not None else _confs()).get(str(department.id))
    c = c if isinstance(c, dict) else {}
    folder = c.get("folder")
    to = c.get("to")
    return {"on": c.get("on", True) is not False, "to": to if isinstance(to, int) and not isinstance(to, bool) else None,
            "folder": default_folder(department.name) if folder is None else str(folder),
            "letters": str(c.get("letters") or "")}


def _active(s) -> list:
    return s.query(Department).filter(Department.active.is_(True)).order_by(Department.name).all()


def accepts(department, confs=None) -> bool:
    """The department gets letters by their meaning (it is on in «Отделы» of the «Секретарь» page)."""
    return department is not None and bool(department.active) and conf_of(department, confs)["on"]


def responsible(s, department, confs=None):
    """Who of the department gets its letters: the chosen one (while still working), else its head with Telegram."""
    from . import doc_intake
    c = conf_of(department, confs)
    if c["to"]:
        person = s.get(Employee, c["to"])
        if person is not None and person.active:
            return person
    return doc_intake._head_of(department)


def for_sorting(s) -> list:
    """The departments offered to the sorting: what letters each one gets, what it does and who is in it."""
    confs, out = _confs(), []
    for d in _active(s):
        c = conf_of(d, confs)
        if not c["on"]:
            continue
        people = []
        for m in d.memberships:
            e = m.employee
            if e is None or not e.active:
                continue
            role = " ".join(str(m.role or e.position or "").split())[:60]
            what = " ".join(str(m.responsibilities or e.responsibilities or "").split())[:120]
            people.append(e.name + (f" — {role}" if role else "") + (f": {what}" if what else ""))
            if len(people) >= 6:
                break
        item = {"id": d.id, "name": d.name, "letters": c["letters"] or None,
                "about": " ".join(str(d.description or "").split())[:300] or None, "people": people or None}
        out.append({k: v for k, v in item.items() if v is not None})
        if len(out) >= MAX_DEPARTMENTS:
            break
    return out


def named(s, row, confs=None):
    """The department the sorting named for the letter: (department, sure, why) or None."""
    sort = row.sort or {}
    raw = sort.get("department_id")
    if not isinstance(raw, int) or isinstance(raw, bool):
        return None
    d = s.get(Department, raw)
    if not accepts(d, confs):
        return None
    return d, bool(sort.get("department_sure")), str(sort.get("department_why") or "")


def person_department(s, person, confs=None):
    """The department whose folder gets a letter passed to this person: where the person receives the mail of the
    department, else heads it, else is in it (the first by name), else the department written on the card."""
    if person is None:
        return None
    confs = _confs() if confs is None else confs
    active = _active(s)
    for d in active:
        if conf_of(d, confs)["to"] == person.id:
            return d
    mine = sorted((m for m in person.department_memberships if m.department is not None and m.department.active),
                  key=lambda m: m.department.name)
    for m in mine:
        if m.is_primary and not conf_of(m.department, confs)["to"]:
            return m.department
    if mine:
        return next((m.department for m in mine if m.is_primary), mine[0].department)
    name = " ".join(str(person.department or "").split()).casefold()
    return next((d for d in active if d.name.casefold() == name), None) if name else None


def route_department(s, route: dict, confs=None):
    """The department a route went to: named by it (a rule, the meaning), else the one of the person."""
    raw = route.get("department_id")
    if isinstance(raw, int) and not isinstance(raw, bool):
        d = s.get(Department, raw)
        if d is not None and d.active:
            return d
    name = str(route.get("department") or "").strip()
    if name:
        d = s.query(Department).filter(Department.name == name).first()
        if d is not None and d.active:
            return d
    if route.get("to_id"):
        return person_department(s, s.get(Employee, int(route["to_id"])), confs)
    return None


def people(s) -> list:
    """Whom «ответственный за почту» can be: the working employees (those without Telegram are marked)."""
    return [{"id": e.id, "name": e.name, "tg": bool(e.telegram_chat_id), "position": e.position or ""}
            for e in s.query(Employee).filter(Employee.active.is_(True)).order_by(Employee.name).limit(1000)]


def departments(s) -> list:
    """The departments for the page: what is saved, who gets the letters now and what is wrong with it."""
    from . import doc_intake
    confs, top = _confs(), root()
    out = []
    for d in _active(s):
        c = conf_of(d, confs)
        head = doc_intake._head_of(d)
        chosen = s.get(Employee, c["to"]) if c["to"] else None
        person = responsible(s, d, confs)
        members = [m.employee for m in d.memberships if m.employee is not None and m.employee.active]
        warn = ""
        if c["to"] and (chosen is None or not chosen.active):
            warn = "выбранного сотрудника больше нет в справочнике — письма получает руководитель"
        if person is None:
            warn = warn or "в отделе нет никого с Telegram — письма отдела будут ждать решения"
        elif not person.telegram_chat_id:
            warn = f"у {person.name} нет Telegram — письма отдела будут ждать решения"
        heads = {m.employee_id for m in d.memberships if m.is_primary}
        default = (("руководитель — " if head.id in heads else "по умолчанию — ") + head.name if head is not None
                   else "руководитель (в отделе нет никого с Telegram)")
        path = path_of(top, c["folder"]) if c["folder"] else ""
        out.append({"id": d.id, "name": d.name, "on": c["on"], "to": chosen.id if chosen is not None and chosen.active else None,
                    "default": default, "person": person.name if person is not None else "",
                    "folder": c["folder"], "path": path, "shown": shown(path) if path else "",
                    "letters": c["letters"], "members": [e.id for e in members], "size": len(members), "warn": warn})
    return out


def save_departments(form) -> int:
    """The «Отделы» form: per department — on, who gets its letters, the folder, what letters. Nothing is saved when a
    value is wrong."""
    confs = {k: dict(v) for k, v in _confs().items() if isinstance(v, dict)}
    top, saved = root(), 0
    with SessionLocal() as s:
        for d in _active(s):
            if str(form.get(f"present_{d.id}") or "") != "1":
                continue
            old = confs.get(str(d.id)) or {}
            raw = str(form.get(f"to_{d.id}") or "").strip()
            to = None
            if raw:
                person = s.get(Employee, int(raw)) if raw.isdigit() else None
                if person is None or not person.active:
                    raise FolderError(f"отдел «{d.name}»: такого сотрудника нет в справочнике")
                if not person.telegram_chat_id and old.get("to") != person.id:
                    raise FolderError(f"отдел «{d.name}»: у {person.name} нет Telegram-контакта — письма не дойдут")
                to = person.id
            try:
                folder = clean_path(form.get(f"folder_{d.id}"), top=not top)
            except FolderError as exc:
                raise FolderError(f"отдел «{d.name}»: {exc}") from None
            confs[str(d.id)] = {"on": str(form.get(f"on_{d.id}") or "") in ("1", "on", "true"), "to": to,
                                "folder": None if folder == default_folder(d.name) else folder,
                                "letters": " ".join(str(form.get(f"letters_{d.id}") or "").split())[:300]}
            saved += 1
    updates = {"mail_departments": confs}
    if str(form.get("present_auto") or "") == "1":
        updates["mail_departments_auto"] = str(form.get("mail_departments_auto") or "") in ("1", "on", "true")
    settings_store.set_many(updates)
    return saved


# ----------------------------------------------------------------------------- the special folders
def folder_rules() -> list:
    raw = settings_store.get("mail_folder_rules")
    out = []
    for r in raw if isinstance(raw, list) else []:
        if isinstance(r, dict) and str(r.get("folder") or "").strip():
            out.append({"kind": str(r.get("kind") or ""), "senders": str(r.get("senders") or ""),
                        "keywords": str(r.get("keywords") or ""), "folder": str(r["folder"])})
    return out


def _as_rule(rule: dict):
    return types.SimpleNamespace(kind=rule["kind"] or None, entity_id=None, senders=rule["senders"],
                                 keywords=rule["keywords"])


def rule_condition(rule: dict) -> str:
    from . import mail_route
    return mail_route.rule_condition(None, _as_rule(rule))


def save_folder_rule(form) -> None:
    """A special folder: letters of a type, from some senders or with some words → the folder (checked first)."""
    from . import mail_route
    kind = str(form.get("kind") or "").strip()
    if kind and kind not in mail.kinds():
        raise FolderError("выберите тип письма из списка")
    try:
        senders = mail_route._senders(str(form.get("senders") or ""))
    except mail_route.RouteError as exc:
        raise FolderError(str(exc)) from None
    keywords = ", ".join(mail_route._split(form.get("keywords")))[:300]
    if not (kind or senders or keywords):
        raise FolderError("укажите тип письма, отправителя или слова — иначе в эту папку ляжет вся почта")
    folder = clean_path(form.get("folder"), top=not root())
    if not folder:
        raise FolderError("укажите папку")
    rules = folder_rules()
    if len(rules) >= MAX_RULES:
        raise FolderError(f"не больше {MAX_RULES} особых папок")
    rules.append({"kind": kind, "senders": senders, "keywords": keywords, "folder": folder})
    settings_store.set_many({"mail_folder_rules": rules})


def folder_rule_action(index: int, action: str) -> None:
    rules = folder_rules()
    if not 0 <= index < len(rules):
        raise FolderError("правило не найдено")
    if action == "delete":
        rules.pop(index)
    elif action == "up":
        if index > 0:
            rules[index - 1], rules[index] = rules[index], rules[index - 1]
    else:
        raise FolderError("неизвестное действие")
    settings_store.set_many({"mail_folder_rules": rules})


def save_settings(form) -> str:
    """«Папки в ящике»: on and where the folders are made. Switched on with a connected box — the folders are made
    right away (the message tells what came of it)."""
    top = clean_root(form.get("mail_folders_root"))
    on = str(form.get("mail_folders_on") or "") in ("1", "on", "true")
    settings_store.set_many({"mail_folders_on": on, "mail_folders_root": top})
    if not on or not mail.configured():
        return ""
    try:
        return create_now()
    except FolderError as exc:
        raise FolderError(f"сохранено, но папки в ящике не созданы: {exc}") from None


# ----------------------------------------------------------------------------- where a letter belongs
def _snapshot(s) -> dict:
    snap = {"confs": _confs(), "rules": folder_rules(), "root": root(),
            "names": {d.id: d.name for d in _active(s)}}
    snap["sig"] = hashlib.sha1(json.dumps([snap["confs"], snap["rules"], snap["root"], sorted(snap["names"].items())],
                                          ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()[:12]
    return snap


def _sig(route: dict, sort: dict, kind, snap) -> str:
    """What the folder of a letter depends on: when none of it changed, the letter is not looked at again."""
    key = [route.get("status"), route.get("to_id"), route.get("department_id"), route.get("department"), kind,
           sort.get("department_id"), bool(sort.get("department_sure")), snap["sig"]]
    return hashlib.sha1(json.dumps(key, ensure_ascii=False, default=str).encode()).hexdigest()[:12]


def target(s, row, snap=None) -> tuple:
    """(the folder of the box the letter belongs in — "" for the folder it is read from, why)."""
    from . import mail_route
    snap = snap or _snapshot(s)
    hay = None
    for rule in snap["rules"]:
        if not (rule["kind"] or rule["senders"] or rule["keywords"]):
            continue
        hay = mail_route._hay(row) if hay is None else hay
        if mail_route.fits(_as_rule(rule), row, hay):
            path = path_of(snap["root"], rule["folder"])
            if path:
                return path, f"особая папка: {rule_condition(rule)}"
    route = row.route or {}
    status = route.get("status") or ""
    if status in mail_route.WAITING or status == "planned":
        return "", "ждёт решения, кому передать"
    d, why = None, ""
    if status == "sent":
        d = route_department(s, route, snap["confs"])
        why = f"передано: отдел «{d.name}»" if d is not None else ""
    if d is None:
        found = named(s, row, snap["confs"])
        if found and found[1]:
            d, why = found[0], f"по смыслу письма: отдел «{found[0].name}»"
    if d is None:
        return "", ""
    c = conf_of(d, snap["confs"])
    path = path_of(snap["root"], c["folder"]) if c["on"] and c["folder"] else ""
    return (path, why) if path else ("", f"у отдела «{d.name}» нет папки")


def all_folders(s, snap=None) -> list:
    """The folders of the secretary: of the departments (by name) and the special ones."""
    snap = snap or _snapshot(s)
    out = []
    for d in _active(s):
        c = conf_of(d, snap["confs"])
        if c["on"] and c["folder"]:
            out.append(path_of(snap["root"], c["folder"]))
    out += [path_of(snap["root"], r["folder"]) for r in snap["rules"]]
    return list(dict.fromkeys(p for p in out if p))


# ----------------------------------------------------------------------------- the work in the box
def _error(exc) -> str:
    return (str(exc) if isinstance(exc, mail.MailError) else mail._short(exc))[:300] or "ошибка почты"


def _state(cfg: dict, **values) -> None:
    state = dict(settings_store.get("mail_folders_state") or {})
    state.update(box=mail._box_key(cfg), at=mail._iso(now()), **values)
    settings_store.set_many({"mail_folders_state": state})


def place_pending(days: int = 7, limit: int = 20, force: bool = False, wait: bool = False) -> int:
    """Background loop: move the sorted letters into their folders of the box. Returns how many it tried to move.
    `force` — the box and the letters that refused are tried again right away."""
    if not settings_store.get("mail_folders_on"):
        return 0
    cfg = mail.connection()
    if not mail.configured(cfg):
        return 0
    if not _LOCK.acquire(timeout=120 if wait else 0):
        return 0
    try:
        return _place(cfg, days, limit, force)
    finally:
        _LOCK.release()


def _place(cfg: dict, days: int, limit: int, force: bool) -> int:
    state = settings_store.get("mail_folders_state") or {}
    failed = mail._from_iso(state.get("error_at")) if state.get("box") == mail._box_key(cfg) and state.get("ok") is False else None
    if failed is not None and not force and now() - failed < dt.timedelta(minutes=RETRY_MINUTES):
        return 0
    since = now() - dt.timedelta(days=days)
    label_since = now() - dt.timedelta(days=LABEL_DAYS)
    retry = mail._iso(now() - dt.timedelta(minutes=RETRY_MINUTES))
    labels_first = bool(settings_store.get("mail_labels"))
    moves, why = [], {}
    with SessionLocal() as s:
        snap = _snapshot(s)
        # the columns first (every half a minute): the whole letter is read only when its folder may change
        query = (s.query(MailMessage.id, MailMessage.place, MailMessage.route, MailMessage.sort, MailMessage.kind,
                         MailMessage.created_at)
                 .filter(MailMessage.direction == "in", MailMessage.sort_status == "done", MailMessage.uid > 0,
                         MailMessage.mailbox == cfg["address"].lower(), MailMessage.folder == cfg["folder"],
                         MailMessage.created_at >= since)
                 .order_by(MailMessage.id).limit(2000).all())
        for mail_id, place, route, sort, kind, created in query:
            place, route, sort = dict(place or {}), route or {}, sort or {}
            if place.get("status") == "gone" or not route or route.get("status") == "planned":
                continue
            if labels_first and not sort.get("box") and not place.get("folder") and mail._aware(created) >= label_since:
                continue  # the labels go first: they are put by the UID of the inbox
            sig = _sig(route, sort, kind, snap)
            if place.get("status") == "error":
                if not force and (int(place.get("tries") or 0) >= MAX_TRIES or str(place.get("at") or "") > retry):
                    continue
            elif place.get("sig") == sig and not force:
                continue
            row = s.get(MailMessage, mail_id)
            path, reason = target(s, row, snap)
            here = str(place.get("folder") or "")
            if path == here:
                row.place = {**place, "folder": here, "status": "ok", "sig": sig, "error": "", "tries": 0,
                             "why": reason if here else ""}
                continue
            if len(moves) < limit:
                moves.append({"id": row.id, "uid": row.uid, "uidvalidity": row.uidvalidity or "",
                              "message_id": row.message_id or "", "source": parts(here), "target": parts(path)})
                why[row.id] = (path, reason, sig)
        s.commit()
    if not moves:
        return 0
    try:
        result = mail._box(cfg).place(moves, [])
    except Exception as exc:  # noqa: BLE001  the box refused: shown on the page, tried again later
        error = _error(exc)
        log.warning("mail folders: %s", error)
        _state(cfg, ok=False, error=error, error_at=mail._iso(now()))
        return len(moves)
    moved, at, done = result.get("moved") or {}, mail._iso(now()), 0
    with SessionLocal() as s:
        for m in moves:
            row = s.get(MailMessage, m["id"])
            if row is None:
                continue
            place, (path, reason, sig) = dict(row.place or {}), why[m["id"]]
            outcome = str(moved.get(m["id"]) or "error: сервер не ответил")
            if outcome == "ok":
                row.place = {"folder": path, "status": "ok", "at": at, "why": reason, "sig": sig, "error": "", "tries": 0,
                             "was": place.get("folder") or ""}
                done += 1
            elif outcome == "gone":
                row.place = {**place, "status": "gone", "at": at,
                             "error": "письма нет там, где оно было: его переложили или удалили в самом ящике"}
            else:
                row.place = {**place, "folder": place.get("folder") or "", "status": "error", "at": at, "sig": sig,
                             "want": path, "error": outcome.removeprefix("error: ")[:300],
                             "tries": int(place.get("tries") or 0) + 1}
        s.commit()
    old = settings_store.get("mail_folders_state") or {}
    _state(cfg, ok=True, error="", error_at="", delimiter=result.get("delimiter") or "",
           moved=int(old.get("moved") or 0) + done, last={"at": at, "moved": done, "tried": len(moves)},
           created=(list(old.get("created") or []) + list(result.get("created") or []))[-50:])
    return len(moves)


def create_now() -> str:
    """«Создать папки в ящике»: the missing folders of the departments and the special ones are made."""
    cfg = mail.connection()
    if not mail.configured(cfg):
        raise FolderError("сначала подключите почту («Подключение»)")
    with SessionLocal() as s:
        paths = all_folders(s)
    if not paths:
        raise FolderError("папок нет: у отделов не указаны папки и особых папок нет")
    try:
        result = mail._box(cfg).place([], [parts(p) for p in paths])
    except Exception as exc:  # noqa: BLE001
        error = _error(exc)
        _state(cfg, ok=False, error=error, error_at=mail._iso(now()))
        raise FolderError(error) from None
    created = list(result.get("created") or [])
    old = settings_store.get("mail_folders_state") or {}
    _state(cfg, ok=True, error="", error_at="", delimiter=result.get("delimiter") or "",
           created=(list(old.get("created") or []) + created)[-50:])
    return f"папки в ящике готовы: {len(paths)}" + (f", создано новых: {len(created)}" if created else " — все уже были")


def apply_soon() -> None:
    """«Разложить письма за 30 дней»: in the background (tests: right away), a hundred letters at a time."""
    def job():
        for i in range(20):
            try:
                if not place_pending(days=30, limit=100, force=i == 0, wait=True):
                    break
            except Exception:  # noqa: BLE001
                log.exception("mail folders: the letters of 30 days not placed")
                break
    if mail._testing():
        job()
    else:
        threading.Thread(target=job, daemon=True, name="mail-folders").start()


def view(s) -> dict:
    """«Папки в ящике» on the page: the folders with the letters put into them in 30 days, the special folders, the
    last work with the box."""
    snap = _snapshot(s)
    cfg = mail.connection()
    state = settings_store.get("mail_folders_state") or {}
    same = state.get("box") == mail._box_key(cfg)
    counts, problems = collections.Counter(), collections.Counter()
    since = now() - dt.timedelta(days=30)
    for (place,) in (s.query(MailMessage.place).filter(MailMessage.direction == "in", MailMessage.created_at >= since,
                                                       MailMessage.place.isnot(None)).limit(5000)):
        if not isinstance(place, dict):
            continue
        if place.get("status") == "ok" and place.get("folder"):
            counts[place["folder"]] += 1
        elif place.get("status") in ("error", "gone"):
            problems[place["status"]] += 1
    folders = all_folders(s, snap)
    typed = "/".join("Входящие" if i == 0 and p == "INBOX" else p for i, p in enumerate(parts(snap["root"])))
    return {"on": bool(settings_store.get("mail_folders_on")), "root": typed,
            "where": shown(snap["root"]) if snap["root"] else "верхний уровень ящика",
            "folders": [{"path": p, "shown": shown(p), "count": counts.get(p, 0)} for p in folders],
            "other": [{"path": p, "shown": shown(p), "count": n} for p, n in counts.most_common() if p not in folders],
            "rules": [{"index": i, "condition": rule_condition(r), "folder": r["folder"],
                       "shown": shown(path_of(snap["root"], r["folder"]) or r["folder"])} for i, r in enumerate(snap["rules"])],
            "moved": sum(counts.values()), "errors": problems.get("error", 0), "gone": problems.get("gone", 0),
            "state": {"at": mail.local(mail._from_iso(state.get("at"))) if same else None,
                      "ok": state.get("ok") if same else None, "error": state.get("error", "") if same else "",
                      "delimiter": state.get("delimiter", "") if same else ""}}
