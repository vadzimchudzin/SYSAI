"""The MCP server «sysai» (ОПО-4.x): Streamable HTTP, JSON-RPC 2.0 over POST /mcp, sign-in by the agent's token.

A light implementation without the SDK: initialize, notifications (202), ping, tools/list, tools/call. Every tool
checks the agent's access (the same signed rules the computer applies) and the rights of its person; answers are
short (summaries, pages), every call goes to the journal, calls are limited per minute."""
import collections
import json
import threading
import time

from .. import settings_store, telegram, userbot
from ..db import Department, Employee, EmployeeDepartment, SessionLocal
from . import access, hub, journal
from .agents import AgentCtx

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER_INFO = {"name": "sysai", "title": "SYSAI", "version": "0.1.0"}
MAX_BODY = 1024 * 1024
NOTIFY_PER_HOUR = 20
INSTRUCTIONS = ("SYSAI — сервер компании. Здесь: кто ты и что тебе можно (whoami), отделы и люди, общий хаб навыков "
                "SYSAI_Hub (hub_search, hub_get, hub_propose), сообщение своему человеку в Telegram (notify_me) и журнал "
                "(log_event). Подтверждений не бывает: что не разрешено в доступах, то запрещено.")

_calls = collections.defaultdict(collections.deque)
_notes = collections.defaultdict(collections.deque)
_lock = threading.Lock()


class ToolError(Exception):
    pass


def _rate_ok(store, key, limit: int, window: float) -> bool:
    stamp = time.time()
    with _lock:
        q = store[key]
        while q and q[0] < stamp - window:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(stamp)
        return True


def rate_limit() -> int:
    try:
        return max(1, int(settings_store.get("agents_mcp_per_minute") or 120))
    except (TypeError, ValueError):
        return 120


# ----------------------------------------------------------------------------- tools
def _whoami(ctx: AgentCtx, rules: dict, args: dict) -> dict:
    doc = access.document(ctx.agent_id)
    return {"agent": ctx.name, "user": ctx.username, "role": ctx.role, "department": doc["doc"]["department"],
            "profile": doc["profile"]["name"], "access_rev": doc["rev"],
            "tools": [t for t in TOOLS if access.tool_allowed(rules, "sysai", t)],
            "models": [f"sysai/{t}" for t in rules["models"]["tiers"]],
            "budget_day_usd": rules["models"]["budget_day_usd"], "rules": rules}


def _departments(ctx, rules, args):
    with SessionLocal() as s:
        rows = s.query(Department).filter(Department.active.is_(True)).order_by(Department.name).all()
        return {"departments": [{"id": d.id, "name": d.name, "description": (d.description or "")[:300]} for d in rows]}


def _people(ctx, rules, args):
    if not rules["data"]["people"]:
        raise ToolError("Доступы агента не разрешают справочник людей")
    query = str(args.get("query") or "").strip().lower()
    dept = str(args.get("department") or "").strip().lower()
    limit = max(1, min(50, int(args.get("limit") or 20)))
    with SessionLocal() as s:
        names = {d.id: d.name for d in s.query(Department).all()}
        member = collections.defaultdict(list)
        for m in s.query(EmployeeDepartment).all():
            if m.department_id in names:
                member[m.employee_id].append(names[m.department_id])
        out = []
        for e in s.query(Employee).filter(Employee.active.is_(True)).order_by(Employee.name).all():
            depts = member.get(e.id) or ([e.department] if e.department else [])
            hay = f"{e.name} {e.aliases} {e.position} {' '.join(depts)} {e.competencies if ctx.superuser else ''}".lower()
            if query and query not in hay:
                continue
            if dept and not any(dept in d.lower() for d in depts):
                continue
            item = {"name": e.name, "position": e.position, "departments": depts}
            if ctx.superuser:  # the person sees the whole card in SYSAI, so may his agent
                item.update(competencies=(e.competencies or "")[:300], responsibilities=(e.responsibilities or "")[:300])
            out.append(item)
            if len(out) >= limit:
                break
        return {"people": out, "shown": len(out)}


def _hub_search(ctx, rules, args):
    query = str(args.get("query") or "").strip().lower()
    dept = str(args.get("department") or "").strip().lower()
    limit = max(1, min(20, int(args.get("limit") or 10)))
    found = []
    for k in hub.visible_skills(ctx):
        if query and query not in f"{k['name']} {k['title']} {k['description']}".lower():
            continue
        if dept and dept not in (k["department"] or "").lower():
            continue
        found.append({"name": k["name"], "title": k["title"], "description": k["description"][:300], "version": k["version"],
                      "department": k["department"]})
    return {"skills": found[:limit], "total": len(found)}


def _hub_get(ctx, rules, args):
    name = str(args.get("name") or "").strip()
    item = next((k for k in hub.visible_skills(ctx) if k["name"] == name), None)
    if item is None:
        raise ToolError("Нет такого навыка среди доступных агенту")
    files = hub.files_of(name, item["version"])
    text = (files.get("SKILL.md") or b"").decode("utf-8", errors="replace")
    return {"name": name, "version": item["version"], "sha256": item["sha256"], "skill_md": text[:20000],
            "truncated": len(text) > 20000, "files": [{"path": p, "size": len(v)} for p, v in sorted(files.items())],
            "download": f"/agents/api/hub/skills/{name}/{item['version']}.zip"}


def _hub_propose(ctx, rules, args):
    skill_md = str(args.get("skill_md") or "")
    if not skill_md.strip():
        raise ToolError("Нужен текст SKILL.md")
    files = {"SKILL.md": skill_md}
    extra = args.get("files") or {}
    if not isinstance(extra, dict):
        raise ToolError("files: объект {путь: текст}")
    files.update({str(p): str(v) for p, v in extra.items() if str(p) != "SKILL.md"})
    try:
        name, version = hub.propose(ctx, files, str(args.get("changelog") or ""), str(args.get("why") or ""))
    except hub.HubError as exc:
        raise ToolError(str(exc)) from exc
    card = hub.card(name)
    checks = next((v["checks"] for v in card["versions"] if v["version"] == version), {})
    return {"skill": name, "version": version, "status": "review",
            "message": "Предложение отправлено владельцу навыка в SYSAI_Hub", "checks": checks}


def _notify_me(ctx, rules, args):
    text = str(args.get("text") or "").strip()
    if not text:
        raise ToolError("Нужен текст сообщения")
    if not ctx.employee_id:
        raise ToolError("У агента не указан сотрудник: некому писать (страница «Агенты»)")
    with SessionLocal() as s:
        person = s.get(Employee, ctx.employee_id)
        chat = (person.telegram_chat_id or "").strip() if person else ""
    if not chat:
        raise ToolError("У сотрудника не указан Telegram")
    if not _rate_ok(_notes, ctx.agent_id, NOTIFY_PER_HOUR, 3600):
        raise ToolError(f"Не больше {NOTIFY_PER_HOUR} сообщений в час")
    message = f"🤖 {ctx.name}: {text[:3500]}"
    transport = userbot if userbot.ai_mode() else telegram
    try:
        transport.send_message(chat, message)
    except Exception as exc:  # noqa: BLE001  the agent gets the reason instead of a crash
        raise ToolError(f"Сообщение не отправлено: {exc}") from exc
    return {"sent": True}


def _log_event(ctx, rules, args):
    action = str(args.get("action") or "").strip()[:50]
    if not action:
        raise ToolError("Нужно действие (action)")
    details = args.get("details") if isinstance(args.get("details"), dict) else {"text": str(args.get("details") or "")[:2000]}
    journal.log("agent:" + action, str(args.get("target") or "")[:160], details, actor=ctx.name, actor_kind="agent",
                agent_id=ctx.agent_id)
    if action == "skill_used" and details.get("skill"):
        ok = details.get("ok")
        hub.record_use(ctx, details["skill"], str(details.get("version") or ""), int(details.get("tokens") or 0),
                       ok if isinstance(ok, bool) else None)
    return {"logged": True}


def _schema(props: dict, required=()):
    return {"type": "object", "properties": props, "required": list(required), "additionalProperties": False}


_STR = {"type": "string"}
TOOLS = {
    "whoami": ("Кто я: агент, человек, отдел, профиль доступа и все его правила, модели и бюджет.", _schema({}), _whoami, True),
    "departments": ("Отделы компании.", _schema({}), _departments, True),
    "people": ("Люди компании: имя, должность, отделы. Поиск по имени, должности, отделу.",
               _schema({"query": _STR, "department": _STR, "limit": {"type": "integer", "minimum": 1, "maximum": 50}}),
               _people, True),
    "hub_search": ("Поиск навыков SYSAI_Hub, доступных агенту (навыки всех отделов).",
                   _schema({"query": _STR, "department": _STR, "limit": {"type": "integer", "minimum": 1, "maximum": 20}}),
                   _hub_search, True),
    "hub_get": ("Навык SYSAI_Hub: текст SKILL.md, файлы, ссылка на архив.", _schema({"name": _STR}, ["name"]), _hub_get, True),
    "hub_propose": ("Предложить новый навык или улучшение в SYSAI_Hub: SKILL.md, файлы, что изменилось и зачем. "
                    "Владелец навыка утверждает — и навык получают все агенты.",
                    _schema({"skill_md": _STR, "files": {"type": "object", "additionalProperties": _STR},
                             "changelog": _STR, "why": _STR}, ["skill_md", "why"]), _hub_propose, False),
    "notify_me": ("Сообщение своему человеку в Telegram (сообщение, не подтверждение).", _schema({"text": _STR}, ["text"]),
                  _notify_me, False),
    "log_event": ("Запись в журнал SYSAI: что сделано и над чем. action=skill_used с details.skill — учёт навыка.",
                  _schema({"action": _STR, "target": _STR, "details": {"type": "object"}}, ["action"]), _log_event, False),
}


def tools_list(rules: dict) -> list:
    out = []
    for name, (description, schema, _fn, read_only) in TOOLS.items():
        if access.tool_allowed(rules, "sysai", name):
            out.append({"name": name, "description": description, "inputSchema": schema,
                        "annotations": {"readOnlyHint": read_only}})
    return out


def call_tool(ctx: AgentCtx, name: str, args: dict) -> dict:
    rules = access.rules_for(ctx.agent_id)
    if name not in TOOLS:
        return {"content": [{"type": "text", "text": f"Нет инструмента {name}"}], "isError": True}
    if not access.tool_allowed(rules, "sysai", name):
        journal.log("mcp.denied", name, None, actor=ctx.name, actor_kind="agent", agent_id=ctx.agent_id)
        return {"content": [{"type": "text", "text": f"Доступы агента не разрешают инструмент {name}"}], "isError": True}
    started = time.time()
    try:
        result = TOOLS[name][2](ctx, rules, args if isinstance(args, dict) else {})
        error = None
    except ToolError as exc:
        result, error = None, str(exc)
    except (TypeError, ValueError) as exc:
        result, error = None, f"Неверные аргументы: {exc}"
    if name != "log_event":
        journal.log("mcp." + name, "", {"args": {k: (str(v)[:200]) for k, v in (args or {}).items()} if isinstance(args, dict) else {},
                                        "ok": error is None, "ms": int((time.time() - started) * 1000),
                                        **({"error": error} if error else {})},
                    actor=ctx.name, actor_kind="agent", agent_id=ctx.agent_id)
    if error is not None:
        return {"content": [{"type": "text", "text": error}], "isError": True}
    return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, default=str)}],
            "structuredContent": result, "isError": False}


# ----------------------------------------------------------------------------- JSON-RPC
def _error(rid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def handle_one(ctx: AgentCtx, msg) -> dict | None:
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method", None), str):
        return _error(msg.get("id") if isinstance(msg, dict) else None, -32600, "Invalid Request")
    rid, method, params = msg.get("id"), msg["method"], msg.get("params") or {}
    if "id" not in msg:  # a notification: nothing to answer
        return None
    if not isinstance(params, dict):
        return _error(rid, -32602, "params must be an object")
    if method == "initialize":
        asked = str(params.get("protocolVersion") or "")
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
            "capabilities": {"tools": {"listChanged": False}}, "serverInfo": SERVER_INFO, "instructions": INSTRUCTIONS}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": tools_list(access.rules_for(ctx.agent_id))}}
    if method == "tools/call":
        name = params.get("name")
        if not isinstance(name, str):
            return _error(rid, -32602, "tools/call needs a tool name")
        if not _rate_ok(_calls, ctx.agent_id, rate_limit(), 60):
            return _error(rid, -32000, f"Слишком часто: не больше {rate_limit()} вызовов в минуту")
        return {"jsonrpc": "2.0", "id": rid, "result": call_tool(ctx, name, params.get("arguments") or {})}
    if method in ("resources/list", "prompts/list"):
        return {"jsonrpc": "2.0", "id": rid, "result": {method.split("/")[0]: []}}
    return _error(rid, -32601, f"Method not found: {method}")


def handle(ctx: AgentCtx, payload):
    """One message or a batch. None means «only notifications»: the answer is 202 without a body."""
    if isinstance(payload, list):
        if not payload:
            return _error(None, -32600, "Invalid Request")
        out = [r for r in (handle_one(ctx, m) for m in payload[:50]) if r is not None]
        return out or None
    return handle_one(ctx, payload)
