"""Access of agents — only in the settings of SYSAI, never a confirmation in Telegram (ОПО-2.x).

A profile («Инженер ОПО», «Руководитель ОПО», «ТО», «Наблюдатель» or the company's own) says what an agent may do;
an agent has one profile and personal exceptions. SYSAI turns that into the configuration of the agent's harness
(Hermes: approvals off, the forbidden commands, MCP servers, the gateway, the hub folders), signs the document with
Ed25519 and the synchronizer on the computer applies it. The MCP servers check the same signed rules themselves, so a
replaced or broken harness gets no more than the settings allow. An agent never gets more than its person."""
import base64
import copy
import fnmatch
import hashlib
import json
import ntpath
import re
from types import SimpleNamespace

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from .. import config
from ..db import Department, SessionLocal, User, now
from . import journal
from .models import NetAccessProfile, NetAgent, NetAgentAccess

SECTIONS = ("commands", "folders", "programs", "mcp", "data", "internet", "models", "schedule", "chat", "skills", "files")
SECTION_RU = {"commands": "Команды на ПК", "folders": "Папки", "programs": "Программы", "mcp": "MCP-инструменты",
              "data": "Данные SYSAI", "internet": "Интернет", "models": "Нейронки", "schedule": "Дела по расписанию",
              "chat": "Кто может писать агенту", "skills": "Хаб: навыки и инструменты", "files": "Хранилище"}
TIERS = ("cheap", "mid", "strong", "vision", "auto")
MODEL_TIERS = ("cheap", "mid", "strong", "vision")  # a real model behind each, chosen in the settings
TEXT_TIERS = ("cheap", "mid", "strong")
TIER_RU = {"cheap": "дешёвая", "mid": "средняя", "strong": "сильная", "vision": "зрение (сканы, фото)",
           "auto": "авто: модель под запрос выбирает Jev"}
# sysai/auto: the routing policy of Jev Router (ОПО-5.8) among the models of the agent's other tiers
COST_TIERS = ("low", "medium", "high")
COST_RU = {"low": "экономнее", "medium": "поровну цена и сила", "high": "сильнее, когда это окупается"}
SCOPES = ("own", "department", "all")
SCOPE_RU = {"own": "свои", "department": "отдела", "all": "все"}
LEVELS = ("none", "read", "write")
LEVEL_RU = {"none": "нет", "read": "чтение", "write": "чтение и запись"}
INTERNET = ("off", "list", "all")
INTERNET_RU = {"off": "выключен", "list": "только список сайтов", "all": "весь"}
MCP_SERVERS = ("sysai", "plc-kit", "machine-expert", "pic-index", "doc-kit", "cad")
SYSAI_TOOLS = ("whoami", "departments", "people", "hub_search", "hub_get", "hub_take", "hub_drop", "hub_propose",
               "files_list", "files_find", "files_read", "files_write", "notify_me", "log_event")
# Sections of the shared storage (files.py): «Общее» and one per department; «свой отдел» is the agent's department.
# Nothing by default: the storage is opened to an agent only here, in its profile or personal exceptions.
FILES_COMMON = "Общее"
FILES_OWN = "свой отдел"
# In «Папки»: the whole computer. The person's own rights stay the limit — Windows does not let the agent where the
# person may not go (system folders, other users), the rules narrow it further.
WHOLE_PC = "*"
# Hermes asks the synchronizer before each of its own tools that touch the computer (a pre_tool_call shell hook, fail
# closed): `sysai-agent hook` checks the call by the signed rules (agents/sync/sysai_agent/hook.py).
HOOK_COMMAND = "sysai-agent hook"
HOOK_TIMEOUT = 30

# Never allowed, whatever the profile says (on top of the harness's own hard blocklist).
ALWAYS_DENY = ("format *:*", "diskpart*", "bcdedit*", "vssadmin delete*", "cipher /w*", "rm -rf /*", "rm -rf ~*",
               "del /s /q c:\\*", "rd /s /q c:\\*", "Remove-Item -Recurse -Force C:\\*", "reg delete hklm*",
               "shutdown *", "Stop-Computer*", "Restart-Computer*", "Set-MpPreference*-Disable*", "net user * /add*")

EMPTY = {
    "commands": {"run": False, "deny": []},
    "folders": {"read": [], "write": [], "deny": []},
    "programs": [],
    "mcp": {"sysai": ["whoami", "hub_search", "hub_get", "log_event"]},
    "data": {"projects": "own", "to_base": "none", "tags": "none", "questions": False, "people": False},
    "internet": {"mode": "off", "sites": []},
    "models": {"tiers": ["cheap"], "budget_day_usd": 1.0, "budget_project_usd": 0.0},
    "schedule": [],
    "chat": {"owner": True, "others": []},
    "skills": {"departments": ["*"], "propose": False},
    "files": {"read": [], "write": []},
}

_ENGINEER = {
    "commands": {"run": True, "deny": ["git push --force*", "Remove-Item * -Recurse*", "del /s *"]},
    "folders": {"read": [WHOLE_PC], "write": [WHOLE_PC], "deny": []},
    "programs": ["EcoStruxure Machine Expert", "ODA File Converter", "Excel", "Acrobat Reader"],
    "mcp": {"sysai": ["*"], "plc-kit": ["*"], "machine-expert": ["*"], "pic-index": ["*"], "doc-kit": ["*"], "cad": ["*"]},
    "data": {"projects": "own", "to_base": "read", "tags": "write", "questions": True, "people": True},
    "internet": {"mode": "all", "sites": []},
    "models": {"tiers": ["cheap", "mid", "strong", "vision", "auto"], "main": "auto", "workers": "cheap",
               "auto_cost": "low", "budget_day_usd": 3.0, "budget_project_usd": 30.0},
    "schedule": [{"when": "каждые 15 минут", "task": "Проверить новые пакеты ТО по моим проектам"},
                 {"when": "пн–пт 09:00", "task": "Сводка: что сделано вчера и что ждёт меня"}],
    "chat": {"owner": True, "others": []},
    "skills": {"departments": ["*"], "propose": True},
}
_LEAD = copy.deepcopy(_ENGINEER)
_LEAD["data"].update(projects="department")
_LEAD["models"].update(budget_day_usd=5.0, budget_project_usd=60.0)
_LEAD["schedule"] = [{"when": "пн–пт 09:00", "task": "Сводка по проектам отдела: сроки, вопросы в ТО, правки"}]
_TO = {
    "commands": {"run": True, "deny": ["Remove-Item * -Recurse*", "del /s *"]},
    "folders": {"read": [WHOLE_PC], "write": [WHOLE_PC], "deny": []},
    "programs": ["AutoCAD", "ODA File Converter", "Excel", "Acrobat Reader"],
    "mcp": {"sysai": ["*"], "cad": ["*"]},
    "data": {"projects": "department", "to_base": "write", "tags": "read", "questions": True, "people": True},
    "internet": {"mode": "all", "sites": []},
    "models": {"tiers": ["cheap", "mid", "vision", "auto"], "main": "auto", "workers": "cheap", "auto_cost": "low",
               "budget_day_usd": 2.0, "budget_project_usd": 0.0},
    "schedule": [],
    "chat": {"owner": True, "others": []},
    "skills": {"departments": ["*"], "propose": True},
}
_VIEWER = copy.deepcopy(EMPTY)
_VIEWER["mcp"] = {"sysai": ["whoami", "departments", "hub_search", "hub_get", "notify_me", "log_event"]}
_VIEWER["data"].update(tags="read")
_VIEWER["folders"] = {"read": [WHOLE_PC], "write": [], "deny": []}

BUILTIN = (
    ("opo-engineer", "Инженер ОПО", "Свои проекты ПЛК: чертежи ТО, теги, генерация и сборка в Machine Expert.", _ENGINEER),
    ("opo-lead", "Руководитель ОПО", "Все проекты отдела, проверка тегов и кода, больший бюджет.", _LEAD),
    ("to", "ТО", "Технический отдел: база чертежей и ответы на вопросы ОПО.", _TO),
    ("viewer", "Наблюдатель", "Только смотреть: без команд на ПК, интернета и записи.", _VIEWER),
)


# The first built-in folders and sites were placeholders of the ОПО department; the agent sees the whole computer and
# the internet now, and the rules narrow them. A section of a built-in profile still exactly as it first was (nobody
# changed it) gets the new default once.
_OPO_FOLDERS = {"read": ["D:\\ОПО", "D:\\ТО"], "write": ["D:\\ОПО\\Проекты"], "deny": ["C:\\Windows", "C:\\Program Files"]}
_OPO_SITES = {"mode": "list", "sites": ["se.com", "schneider-electric.com", "sysat.by"]}
FIRST_DEFAULTS = {
    "opo-engineer": {"folders": _OPO_FOLDERS, "internet": _OPO_SITES},
    "opo-lead": {"folders": _OPO_FOLDERS, "internet": _OPO_SITES},
    "to": {"folders": {"read": ["D:\\ТО"], "write": ["D:\\ТО\\Выгрузка"], "deny": ["C:\\Windows", "C:\\Program Files"]},
           "internet": {"mode": "list", "sites": ["sysat.by"]}},
    "viewer": {"folders": {"read": [], "write": [], "deny": []}},
}


class AccessError(Exception):
    pass


# ----------------------------------------------------------------------------- rules
def _lines(value, limit: int = 100, length: int = 300) -> list:
    if isinstance(value, str):
        value = value.splitlines()
    if not isinstance(value, (list, tuple)):
        return []
    out = []
    for item in value:
        text = str(item).strip()
        if text and text not in out:
            out.append(text[:length])
    return out[:limit]


def _money(value, default: float) -> float:
    try:
        return round(min(10000.0, max(0.0, float(value))), 2)
    except (TypeError, ValueError):
        return default


def normalize(rules) -> dict:
    """Every section present, of the right type; unknown keys dropped."""
    rules = rules if isinstance(rules, dict) else {}
    out = copy.deepcopy(EMPTY)
    c = rules.get("commands") if isinstance(rules.get("commands"), dict) else {}
    out["commands"] = {"run": bool(c.get("run", False)), "deny": _lines(c.get("deny"))}
    f = rules.get("folders") if isinstance(rules.get("folders"), dict) else {}
    out["folders"] = {k: _lines(f.get(k), 50) for k in ("read", "write", "deny")}
    out["programs"] = _lines(rules.get("programs"), 50, 120)
    m = rules.get("mcp") if isinstance(rules.get("mcp"), dict) else EMPTY["mcp"]
    out["mcp"] = {}
    for server, tools in list(m.items())[:20]:
        server = re.sub(r"[^a-z0-9-]", "", str(server).lower())[:40]
        if server:
            out["mcp"][server] = _lines(tools, 100, 64) or ["*"]
    d = rules.get("data") if isinstance(rules.get("data"), dict) else {}
    out["data"] = {"projects": d.get("projects") if d.get("projects") in SCOPES else "own",
                   "to_base": d.get("to_base") if d.get("to_base") in LEVELS else "none",
                   "tags": d.get("tags") if d.get("tags") in LEVELS else "none",
                   "questions": bool(d.get("questions", False)), "people": bool(d.get("people", False))}
    i = rules.get("internet") if isinstance(rules.get("internet"), dict) else {}
    out["internet"] = {"mode": i.get("mode") if i.get("mode") in INTERNET else "off", "sites": _lines(i.get("sites"), 100, 120)}
    mo = rules.get("models") if isinstance(rules.get("models"), dict) else {}
    tiers = [t for t in TIERS if t in (mo.get("tiers") or [])]
    if not any(t in TEXT_TIERS for t in tiers):
        tiers = [t for t in tiers if t != "auto"]  # Jev picks among the models of the other tiers: none — nothing to pick
    main, workers = _main_and_workers(tiers, mo.get("main"), mo.get("workers"))
    out["models"] = {"tiers": tiers, "main": main, "workers": workers,
                     "auto_cost": mo.get("auto_cost") if mo.get("auto_cost") in COST_TIERS else "low",
                     "budget_day_usd": _money(mo.get("budget_day_usd"), 1.0),
                     "budget_project_usd": _money(mo.get("budget_project_usd"), 0.0)}
    sched = []
    for item in (rules.get("schedule") or [])[:30] if isinstance(rules.get("schedule"), list) else []:
        if isinstance(item, dict) and str(item.get("task") or "").strip():
            sched.append({"when": str(item.get("when") or "").strip()[:80], "task": str(item["task"]).strip()[:300]})
    out["schedule"] = sched
    ch = rules.get("chat") if isinstance(rules.get("chat"), dict) else {}
    out["chat"] = {"owner": bool(ch.get("owner", True)), "others": _lines(ch.get("others"), 50, 64)}
    sk = rules.get("skills") if isinstance(rules.get("skills"), dict) else {}
    out["skills"] = {"departments": _lines(sk.get("departments"), 50, 100) or ["*"], "propose": bool(sk.get("propose", False))}
    fi = rules.get("files") if isinstance(rules.get("files"), dict) else {}
    out["files"] = {k: _lines(fi.get(k), 50, 100) for k in ("read", "write")}
    return out


def _main_and_workers(tiers: list, main=None, workers=None) -> tuple[str, str]:
    """The model the agent thinks and plans with (Hermes «model») and the one its sub-agents get for the parts of a
    task (Hermes «delegation», delegate_task). Only levels the profile allows; by default the middle one plans and the
    cheap one does the parts."""
    usable = [t for t in tiers if t != "vision"] or list(tiers) or ["cheap"]
    if main not in usable:
        main = next((t for t in ("mid", "strong", "cheap") if t in usable), usable[0])
    if workers not in usable:
        workers = "cheap" if "cheap" in usable else main
    return main, workers


def _plain(tiers: list, order: tuple, default: str) -> str:
    """A tier with a model of its own for the helpers of Hermes (pictures, squeezing a long conversation): Jev's choice
    per request does not pay off there."""
    return next((t for t in order if t in tiers), default)


def merge(base: dict, over) -> dict:
    """Personal exceptions over the profile: a section given as a dict changes only its keys, otherwise it replaces."""
    out = copy.deepcopy(base)
    for key, value in (over or {}).items() if isinstance(over, dict) else ():
        if key not in SECTIONS:
            continue
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = {**out[key], **copy.deepcopy(value)}
        else:
            out[key] = copy.deepcopy(value)
    return normalize(out)


def cap_by_person(rules: dict, role: str, department: str = "") -> dict:
    """Never more than the person: only a superuser sees every project of the company and opens any section of the
    storage; the agent of anyone else reads and writes at most «Общее» and its own department."""
    out = copy.deepcopy(rules)
    if role != "superuser" and out["data"]["projects"] == "all":
        out["data"]["projects"] = "department"
    if role != "superuser":
        mine = {FILES_COMMON.lower(), FILES_OWN.lower()} | ({department.lower()} if department else set())
        for part in ("read", "write"):
            out["files"][part] = [w for w in out["files"][part] if w.lower() in mine]
    deny = list(out["commands"]["deny"])
    for pattern in ALWAYS_DENY:
        if pattern not in deny:
            deny.append(pattern)
    out["commands"]["deny"] = deny
    return out


def tool_allowed(rules: dict, server: str, tool: str) -> bool:
    tools = (rules.get("mcp") or {}).get(server) or []
    return "*" in tools or tool in tools


def command_denied(rules: dict, command: str) -> str:
    """The first forbidden pattern the command matches ("" = allowed). The same check the MCP servers on the computer
    run (case-insensitive, «*» is any text)."""
    if not rules["commands"]["run"]:
        return "*"
    text = " ".join((command or "").split()).lower()
    for pattern in rules["commands"]["deny"]:
        if fnmatch.fnmatchcase(text, " ".join(pattern.split()).lower()):
            return pattern
    return ""


def path_allowed(rules: dict, path: str, write: bool = False) -> bool:
    """Inside a «read» (or «write») folder and outside every forbidden one; Windows paths, any case; «*» — the whole
    computer."""
    def norm(p):  # as the synchronizer on the computer does (sysai_agent/rules.py): «..» collapsed — no way out
        text = str(p or "").strip()
        if not text:
            return ""
        text = ntpath.normpath(text.replace("/", "\\"))
        return re.sub(r"\\+", r"\\", text).rstrip("\\").lower()
    target = norm(path)
    if not target:
        return False

    def inside(root):
        if str(root or "").strip() == WHOLE_PC:
            return True
        root = norm(root)
        return bool(root) and (target == root or target.startswith(root + "\\"))
    if any(inside(p) for p in rules["folders"]["deny"]):
        return False
    roots = rules["folders"]["write"] if write else rules["folders"]["read"] + rules["folders"]["write"]
    return any(inside(p) for p in roots)


# ----------------------------------------------------------------------------- profiles
def ensure_builtin(session=None) -> None:
    own = session is None
    s = session or SessionLocal()
    try:
        rows = {p.code: p for p in s.query(NetAccessProfile).all()}
        for code, name, description, rules in BUILTIN:
            p = rows.get(code)
            if p is None:
                s.add(NetAccessProfile(code=code, name=name, description=description, rules=normalize(rules), builtin=True,
                                       updated_by="SYSAI"))
            elif p.builtin and isinstance(p.rules, dict):
                first = FIRST_DEFAULTS.get(code) or {}
                stale = [k for k, v in first.items() if p.rules.get(k) == v]
                if stale:
                    was, fresh = normalize(p.rules), normalize(rules)
                    p.rules = {**was, **{k: copy.deepcopy(fresh[k]) for k in stale}}
                    p.version = (p.version or 0) + 1
                    p.updated_at, p.updated_by = now(), "SYSAI"
                    journal.log("access.profile", f"profile:{code}", {"версия": p.version, "изменения": journal.changes(
                        {k: was[k] for k in stale}, {k: p.rules[k] for k in stale})}, actor="SYSAI", session=s)
        s.flush()
        if own:
            s.commit()
    finally:
        if own:
            s.close()


def default_profile_code(agent_name: str) -> str:
    if agent_name.startswith("opo-"):
        return "opo-engineer"
    if agent_name.startswith("to-"):
        return "to"
    return "viewer"


def profiles() -> list:
    ensure_builtin()
    with SessionLocal() as s:
        rows = s.query(NetAccessProfile).order_by(NetAccessProfile.builtin.desc(), NetAccessProfile.name).all()
        used = {}
        for row in s.query(NetAgentAccess).all():
            used[row.profile_id] = used.get(row.profile_id, 0) + 1
        return [{"id": p.id, "code": p.code, "name": p.name, "description": p.description, "rules": normalize(p.rules),
                 "version": p.version, "builtin": p.builtin, "updated_at": p.updated_at, "updated_by": p.updated_by,
                 "agents": used.get(p.id, 0)} for p in rows]


def save_profile(profile_id, name: str, description: str, rules: dict, by: str = "", code: str = "") -> int:
    rules = normalize(rules)
    name = (name or "").strip()[:120]
    if not name:
        raise AccessError("Нужно название профиля")
    with SessionLocal() as s:
        if profile_id:
            p = s.get(NetAccessProfile, profile_id)
            if p is None:
                raise AccessError("Нет такого профиля")
        else:
            code = re.sub(r"[^a-z0-9-]", "-", (code or "").lower()).strip("-")[:40]
            if not code:
                from .agents import slug
                code = slug(name)[:40] or "profile"
            if s.query(NetAccessProfile).filter(NetAccessProfile.code == code).first():
                raise AccessError(f"Профиль с кодом {code} уже есть")
            p = NetAccessProfile(code=code, name=name, rules=EMPTY, version=0, builtin=False)
            s.add(p)
            s.flush()
        before = normalize(p.rules)
        diff = journal.changes({k: before[k] for k in SECTIONS}, {k: rules[k] for k in SECTIONS})
        if diff or not p.version:
            p.version = (p.version or 0) + 1
        p.name, p.description, p.rules = name, (description or "").strip()[:500], rules
        p.updated_at, p.updated_by = now(), by
        journal.log("access.profile", f"profile:{p.code}", {"версия": p.version, "изменения": diff}, actor=by, session=s)
        s.commit()
        return p.id


def delete_profile(profile_id: int, by: str = "") -> None:
    with SessionLocal() as s:
        p = s.get(NetAccessProfile, profile_id)
        if p is None:
            return
        if p.builtin:
            raise AccessError("Встроенный профиль не удаляется — его можно изменить")
        if s.query(NetAgentAccess).filter(NetAgentAccess.profile_id == p.id).count():
            raise AccessError("Профиль назначен агентам: сначала смените им профиль")
        journal.log("access.profile.delete", f"profile:{p.code}", None, actor=by, session=s)
        s.delete(p)
        s.commit()


KEEP = object()


def set_agent_access(agent_id: int, profile_id=KEEP, overrides=None, by: str = "") -> None:
    """The profile (KEEP leaves it as it is, None takes it away) and the personal exceptions of one agent."""
    if overrides is not None and not isinstance(overrides, dict):
        raise AccessError("Личные исключения — объект JSON по разделам, например {\"models\": {\"budget_day_usd\": 5}}")
    unknown = [k for k in (overrides or {}) if k not in SECTIONS]
    if unknown:
        raise AccessError("Неизвестные разделы: " + ", ".join(unknown) + ". Разделы: " + ", ".join(SECTIONS))
    with SessionLocal() as s:
        agent = s.get(NetAgent, agent_id)
        if agent is None:
            raise AccessError("Нет такого агента")
        if profile_id is not KEEP and profile_id and s.get(NetAccessProfile, profile_id) is None:
            raise AccessError("Нет такого профиля")
        row = s.query(NetAgentAccess).filter(NetAgentAccess.agent_id == agent_id).first()
        if row is None:
            row = NetAgentAccess(agent_id=agent_id, overrides={})
            s.add(row)
        before = {"профиль": row.profile_id, "исключения": row.overrides or {}}
        if profile_id is not KEEP:
            row.profile_id = profile_id or None
        row.overrides = overrides or {}
        row.updated_at, row.updated_by = now(), by
        journal.log("access.agent", agent.name, journal.changes(before, {"профиль": row.profile_id, "исключения": row.overrides}),
                    actor=by, agent_id=agent.id, session=s)
        s.commit()


# ----------------------------------------------------------------------------- the document an agent gets
def _canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _signing_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(hashlib.sha256(b"sysai-net-access:" + config.SECRET_KEY.encode()).digest())


def public_key() -> dict:
    raw = _signing_key().public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return {"alg": "Ed25519", "key": base64.b64encode(raw).decode(), "key_id": hashlib.sha256(raw).hexdigest()[:16]}


def sign(doc: dict) -> dict:
    key = public_key()
    return {"doc": doc, "alg": "Ed25519", "key_id": key["key_id"],
            "sig": base64.b64encode(_signing_key().sign(_canonical(doc))).decode()}


def verify(signed: dict, key_b64: str) -> bool:
    """What the synchronizer and the MCP servers on the computer do before trusting the rules."""
    try:
        Ed25519PublicKey.from_public_bytes(base64.b64decode(key_b64)).verify(base64.b64decode(signed["sig"]), _canonical(signed["doc"]))
        return True
    except Exception:  # noqa: BLE001  any damage or another key: not trusted
        return False


def _yaml_scalar(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)  # a JSON string is a valid YAML double-quoted scalar


def to_yaml(obj, indent: int = 0) -> str:
    """A small YAML writer for the harness configuration (no PyYAML on the server)."""
    pad = "  " * indent
    lines = []
    if isinstance(obj, dict):
        if not obj:
            return pad + "{}"
        for key, value in obj.items():
            k = key if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", str(key)) else json.dumps(str(key), ensure_ascii=False)
            if isinstance(value, (dict, list)) and value:
                lines.append(f"{pad}{k}:")
                lines.append(to_yaml(value, indent + 1))
            else:
                lines.append(f"{pad}{k}: " + ("{}" if value == {} else "[]" if value == [] else _yaml_scalar(value)))
    elif isinstance(obj, list):
        if not obj:
            return pad + "[]"
        for item in obj:
            if isinstance(item, (dict, list)) and item:
                inner = to_yaml(item, indent + 1).lstrip()
                lines.append(f"{pad}- {inner}")
            else:
                lines.append(f"{pad}- " + _yaml_scalar(item))
    else:
        lines.append(pad + _yaml_scalar(obj))
    return "\n".join(lines)


def hook_tools(rules: dict) -> list:
    """Hermes's own tools the synchronizer checks before each call (`sysai-agent hook`): commands, code and writing
    always (the forbidden patterns; the SYSAI folder and the agent's settings belong to the synchronizer), reading when
    not the whole computer is open, the internet when it is not open whole, the screen and processes without commands.
    What is open whole is not asked about: every check is a process start on the computer."""
    folders, net = rules["folders"], rules["internet"]["mode"]
    tools = ["terminal", "execute_code", "write_file", "patch"]
    if not rules["commands"]["run"]:
        tools += ["process_manage", "computer_use"]
    if folders["deny"] or WHOLE_PC not in folders["read"] + folders["write"]:
        tools += ["read_file", "search_files"]
    if net != "all":
        tools += ["web_search", "web_extract", "browser_.*"]
    if net != "all" or "read_file" in tools:
        tools.append("vision_analyze")  # a picture by its address or from a folder
    return tools


def render_hermes(agent: str, rules: dict, server: str, tools=None, cache=()) -> dict:
    """The Hermes configuration made of the rules. Native keys where Hermes has them (the forbidden commands —
    approvals.deny); folders, the internet and the rest of the commands are checked by the synchronizer's hook before
    each of Hermes's own tools (hooks.pre_tool_call → `sysai-agent hook`), the hub MCP servers by its wrapper. The
    «sysai» part (schedule, programs, chat) stays with the synchronizer.

    The main model thinks, plans and hands parts of a task to sub-agents (delegate_task) on the «workers» model —
    that is how a strong model leads cheap ones inside one agent. MCP servers from the hub (tools) are started by the
    synchronizer's wrapper «sysai-agent mcp <name>», which checks the same signed rules.

    `cache` — the tiers whose model caches a prompt only by marks (Claude, Kimi): Hermes puts the marks only for the
    addresses it knows, so the SYSAI provider declares `prompt_caching` for them (ОПО-5.5). Hermes never falls back to
    providers of its own (`fallback_providers: []`): every request goes through SYSAI, the spare models are its own.
    Memory is the profile's own, built in (ОПО-6.5)."""
    server = (server or "").rstrip("/")
    tiers = rules["models"]["tiers"] or ["cheap"]
    main, workers = _main_and_workers(tiers, rules["models"].get("main"), rules["models"].get("workers"))
    deny = rules["commands"]["deny"] if rules["commands"]["run"] else ["*"]
    # only what can start: the built-in server and the hub MCP servers the synchronizer installs; a name allowed in
    # the rules without such a tool (not in the hub, «по запросу» and not taken yet) would only be a broken server
    mcp = {}
    if "sysai" in rules["mcp"]:
        mcp["sysai"] = {"url": f"{server}/mcp", "headers": {"Authorization": "Bearer ${SYSAI_AGENT_TOKEN}"}}
    hub_tools = []
    for tool in tools or []:
        spec = tool.get("spec") or {}
        hub_tools.append({"name": tool["name"], "kind": tool["kind"], "version": tool["version"],
                          "platform": spec.get("platform", "any")})
        if tool["kind"] == "mcp" and tool["name"] in rules["mcp"]:
            if spec.get("url"):
                mcp[tool["name"]] = {"url": spec["url"]}
            else:
                mcp[tool["name"]] = {"command": "sysai-agent", "args": ["mcp", tool["name"]],
                                     "env": {"SYSAI_ACCESS_FILE": "${SYSAI_HOME}/access.json", "SYSAI_URL": server,
                                             "SYSAI_TOOL_DIR": "${SYSAI_HOME}/tools/" + tool["name"]}}
    cfg = {
        "model": {"provider": "custom", "base_url": f"{server}/v1", "default": f"sysai/{main}",
                  "api_key": "${SYSAI_AGENT_TOKEN}"},
        # «sysai» in the body tells the gateway who asks (the trace); the gateway takes it out before OpenRouter
        "fallback_providers": [],
        "delegation": {"model": f"sysai/{workers}", "base_url": f"{server}/v1", "api_key": "${SYSAI_AGENT_TOKEN}",
                       "max_concurrent_children": 3, "max_spawn_depth": 1,  # flat: workers do not delegate further
                       "fallback_providers": [],
                       "request_overrides": {"extra_body": {"sysai": {"role": "workers"}}}},
        "auxiliary": {"vision": {"provider": "custom", "base_url": f"{server}/v1",
                                 "model": "sysai/" + _plain(tiers, ("vision", "mid", "strong", "cheap"), main),
                                 "extra_body": {"sysai": {"role": "vision"}}},
                      "compression": {"provider": "custom", "base_url": f"{server}/v1",
                                      "model": "sysai/" + _plain(tiers, ("cheap", "mid", "strong"), main),
                                      "extra_body": {"sysai": {"role": "compression"}}}},
        "approvals": {"mode": "off", "deny": deny},
        # the rules reach Hermes's own tools too: files, the internet, commands (fail closed — no answer, no action)
        "hooks": {"pre_tool_call": [{"command": HOOK_COMMAND, "matcher": "|".join(hook_tools(rules)),
                                     "timeout": HOOK_TIMEOUT, "fail_closed": True}]},
        "memory": {"memory_enabled": True, "user_profile_enabled": True, "provider": ""},
        "security": {"redact_secrets": True},
        "mcp_servers": mcp,
        "skills": {"external_dirs": ["${SYSAI_HOME}/hub"],
                   **({"create_dir": "${SYSAI_HOME}/proposals"} if rules["skills"]["propose"] else {})},
        "sysai": {"agent": agent, "terminal": rules["commands"]["run"], "folders": rules["folders"],
                  "programs": rules["programs"], "internet": rules["internet"], "schedule": rules["schedule"],
                  "chat": rules["chat"], "hub_tools": hub_tools, "files": rules["files"]},
    }
    marked = [t for t in TIERS if t in cache and t in tiers]
    if marked:
        cfg["providers"] = {"sysai": {"api": f"{server}/v1", "api_key": "${SYSAI_AGENT_TOKEN}", "transport": "openai_chat",
                                      "discover_models": False,
                                      "models": {f"sysai/{t}": {"prompt_caching": True} for t in marked}}}
    return cfg


def cache_tiers(rules: dict) -> list:
    """The agent's tiers whose model caches by marks; sysai/auto too when Jev may pick one of them."""
    from . import gateway
    tiers = rules["models"]["tiers"]
    out = [t for t in MODEL_TIERS if t in tiers and gateway.cache_marks(gateway.model_for(t))]
    if "auto" in tiers and any(t in TEXT_TIERS for t in out):
        out.append("auto")
    return out


def _profile_of(s, row):
    profile = s.get(NetAccessProfile, row.profile_id) if row is not None and row.profile_id else None
    return profile


def document(agent_id: int, server: str = "", session=None) -> dict:
    """Everything the computer needs: effective rules, the harness configuration and the revision ("rev") the
    synchronizer reports back once applied."""
    own = session is None
    s = session or SessionLocal()
    try:
        agent = s.get(NetAgent, agent_id)
        if agent is None:
            raise AccessError("Нет такого агента")
        row = s.query(NetAgentAccess).filter(NetAgentAccess.agent_id == agent_id).first()
        profile = _profile_of(s, row)
        user = s.get(User, agent.user_id)
        dept = s.get(Department, agent.department_id) if agent.department_id else None
        base = normalize(profile.rules) if profile else normalize(EMPTY)
        rules = cap_by_person(merge(base, row.overrides if row else {}), user.role if user else "user",
                              dept.name if dept else "")
        from . import hub
        ctx = SimpleNamespace(agent_id=agent.id, department_id=agent.department_id, name=agent.name,
                              role=user.role if user else "user")
        from . import soul
        tools = hub.delivered_tools(ctx, rules)
        cache = cache_tiers(rules)
        character = soul.for_agent(agent.name, (user.display_name or user.username) if user else "",
                                   user.username if user else "", dept.name if dept else "")
        offer = synchronizer_offer()
        from . import desktop
        window = desktop.offer(offer["auto_update"])
        person = (user.display_name or user.username) if user else ""
        rev = hashlib.sha256(_canonical({"agent": agent.name, "harness": agent.harness, "rules": rules,
                                         "tools": [(t["name"], t["version"]) for t in tools], "cache": cache,
                                         "soul": soul.digest(character),
                                         "sync": [offer["version"], offer["auto_update"]],
                                         "desktop": [window["sha256"], window["electron"]["sha256"]],
                                         "person": person})).hexdigest()[:12]
        server = (server or config.PUBLIC_URL or "").rstrip("/")
        doc = {"agent": agent.name, "agent_id": agent.id, "user": user.username if user else "", "person": person,
               "role": user.role if user else "",
               "department": dept.name if dept else "", "harness": agent.harness, "rev": rev, "server": server,
               "profile": {"code": profile.code, "name": profile.name, "version": profile.version} if profile else
               {"code": "", "name": "без профиля", "version": 0},
               "rules": rules,
               "harness_config": render_hermes(agent.name, rules, server, tools, cache) if agent.harness == "hermes" else None,
               "soul": character, "synchronizer": offer, "desktop": window, "issued_at": now().isoformat(timespec="seconds")}
        return {"rev": rev, "rules": rules, "profile": doc["profile"], "doc": doc,
                "overrides": row.overrides if row else {}, "applied_rev": row.applied_rev if row else "",
                "applied_at": row.applied_at if row else None}
    finally:
        if own:
            s.close()


def synchronizer_offer() -> dict:
    """The synchronizer SYSAI gives out, in the signed document: the computer updates itself only to this wheel (its
    sha256) and only while «обновлять сами» is on (Настройки → Сеть агентов)."""
    from .. import settings_store
    from . import installer
    _, _, sha = installer.wheel()
    return {"version": installer.version(), "sha256": sha, "auto_update": bool(settings_store.get("agents_auto_update"))}


def rules_for(agent_id: int) -> dict:
    return document(agent_id)["rules"]


def signed_document(agent_id: int, server: str = "") -> dict:
    return sign(document(agent_id, server)["doc"])


# ----------------------------------------------------------------------------- the profile form
def rules_from_form(form) -> dict:
    get = form.get

    def lines(key):
        return _lines(str(get(key) or ""))
    mcp = {}
    for line in lines("mcp"):
        server, _, tools = line.partition(":")
        server = server.strip()
        if server:
            mcp[server] = [t.strip() for t in tools.split(",") if t.strip()] or ["*"]
    schedule = []
    for line in lines("schedule"):
        when, sep, task = line.partition("|")
        if sep and task.strip():
            schedule.append({"when": when.strip(), "task": task.strip()})
        elif line.strip():
            schedule.append({"when": "", "task": line.strip()})
    tiers = [t for t in TIERS if get("tier_" + t)]
    return normalize({
        "commands": {"run": bool(get("commands_run")), "deny": lines("commands_deny")},
        "folders": {"read": lines("folders_read"), "write": lines("folders_write"), "deny": lines("folders_deny")},
        "programs": lines("programs"),
        "mcp": mcp,
        "data": {"projects": get("data_projects"), "to_base": get("data_to_base"), "tags": get("data_tags"),
                 "questions": bool(get("data_questions")), "people": bool(get("data_people"))},
        "internet": {"mode": get("internet_mode"), "sites": lines("internet_sites")},
        "models": {"tiers": tiers, "main": get("model_main"), "workers": get("model_workers"), "auto_cost": get("auto_cost"),
                   "budget_day_usd": get("budget_day_usd"), "budget_project_usd": get("budget_project_usd")},
        "schedule": schedule,
        "chat": {"owner": bool(get("chat_owner")), "others": lines("chat_others")},
        "skills": {"departments": lines("skills_departments"), "propose": bool(get("skills_propose"))},
        "files": {"read": lines("files_read"), "write": lines("files_write")},
    })
