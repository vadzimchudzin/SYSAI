"""Addresses of the agent network. Pages for people (/agents, /agents/access, /hub, /files) and the API of agents
(/agents/api/*, /mcp, /v1/*). Every address answers 404 while the setting «agents_on» is off."""
import asyncio
import functools
import json
import weakref
from urllib.parse import quote

import anyio
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse

from .. import config, settings_store, storage
from ..db import Department, Employee, SessionLocal, User
from . import access, agents, desktop, enabled, files, gateway, hub, hub_bundles, installer, journal, mcp, soul, spend, trace
from .models import NetAgent, NetAgentToken, NetSkill

router = APIRouter()
MAX_OWN_AGENTS = 3


def _m():
    from .. import main  # the helpers of the panel; imported late, main imports this module
    return main


def _person(request: Request, admin: bool = False) -> dict:
    if not enabled():
        raise HTTPException(404, "Not Found")
    m = _m()
    (m.guard_admin if admin else m.guard)(request)
    return m.current_user(request)


def _page(request: Request, name: str, **ctx):
    return _m().page(request, name, **ctx)


def _back(url: str, msg: str = "", error: str = "") -> RedirectResponse:
    """Back to a page with a message; the message goes before the #anchor, otherwise the browser keeps it in the anchor."""
    url, mark, anchor = url.partition("#")
    sep = "&" if "?" in url else "?"
    if msg:
        url += f"{sep}msg={quote(msg)}"
    elif error:
        url += f"{sep}error={quote(error)}"
    return RedirectResponse(url + mark + anchor, 303)


def _server(request: Request) -> str:
    return (config.PUBLIC_URL or str(request.base_url)).rstrip("/")


def _ip(request: Request) -> str:
    return request.client.host if request.client else ""


def when(value) -> str:
    """A moment for the pages, in the company's time zone."""
    import datetime as dt
    import zoneinfo
    if not value:
        return "—"
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(zoneinfo.ZoneInfo(config.TIMEZONE)).strftime("%d.%m.%Y %H:%M")


def clock(value) -> str:
    """The time of a step of the trace, to the second, in the company's time zone."""
    import zoneinfo
    if not value:
        return ""
    return trace.aware(value).astimezone(zoneinfo.ZoneInfo(config.TIMEZONE)).strftime("%H:%M:%S")


def _int(value):
    try:
        return int(value) if str(value or "").strip() else None
    except (TypeError, ValueError):
        return None


def _api_error(status: int, message: str, code: str = "error", headers=None) -> JSONResponse:
    return JSONResponse({"error": {"message": message, "code": code}}, status, headers=headers)


GATEWAY_THREADS = 64  # requests to the models and MCP calls waiting at once; the pages have threads of their own
_limiters = weakref.WeakKeyDictionary()


def _limiter() -> anyio.CapacityLimiter:
    loop = asyncio.get_running_loop()
    if loop not in _limiters:
        _limiters[loop] = anyio.CapacityLimiter(GATEWAY_THREADS)
    return _limiters[loop]


async def _in_thread(func, *args):
    """A model answers for minutes, a tool waits for the base or Telegram: the wait goes to a thread of the agents, so
    neither the server nor the pages of SYSAI stand still meanwhile."""
    return await anyio.to_thread.run_sync(func, *args, limiter=_limiter())


async def _relay(chunks):
    """A stream of a model: each wait for its next piece in a thread of the agents."""
    end = object()
    try:
        while True:
            piece = await anyio.to_thread.run_sync(next, chunks, end, limiter=_limiter())
            if piece is end:
                return
            yield piece
    finally:
        chunks.close()  # the agent went away: the stream to OpenRouter is closed and the request counted


def _agent_ctx(request: Request):
    """(AgentCtx, None) or (None, the JSON answer to give)."""
    if not enabled():
        return None, _api_error(404, "Not Found", "not_found")
    ctx = agents.authenticate(agents.bearer(request.headers), _ip(request))
    if ctx is None:
        return None, _api_error(401, "Нужен действующий токен агента SYSAI", "unauthorized", {"WWW-Authenticate": "Bearer"})
    task = "".join(ch for ch in str(request.headers.get("x-sysai-task") or "") if ch.isprintable())[:64].strip()
    if task:  # the task of the board the agent works on: its steps go into one run of the trace (ОПО-5.7)
        ctx.extra["task"] = task
    return ctx, None


def _choices() -> dict:
    with SessionLocal() as s:
        return {"users": [{"id": u.id, "username": u.username, "name": u.display_name or u.username}
                          for u in s.query(User).filter(User.status == "approved").order_by(User.username)],
                "departments": [{"id": d.id, "name": d.name} for d in s.query(Department).filter(Department.active.is_(True))
                                .order_by(Department.name)],
                "employees": [{"id": e.id, "name": e.name, "telegram": bool(e.telegram_chat_id)}
                              for e in s.query(Employee).filter(Employee.active.is_(True)).order_by(Employee.name)]}


# ============================================================================= pages: agents
@router.get("/agents", response_class=HTMLResponse)
def agents_page(request: Request, msg: str = "", error: str = ""):
    me = _person(request)
    boss = me["role"] == "superuser"
    spend = gateway.summary(days=1)["by_agent"]
    return _page(request, "net/agents.html", agents=agents.listing(me), boss=boss, msg=msg, error=error,
                 spend=spend, profiles=access.profiles() if boss else [], harnesses=agents.HARNESSES,
                 can_create=boss or len(agents.listing(me)) < MAX_OWN_AGENTS, **_choices())


# ============================================================================= pages: spend
def _spend_scope(me: dict, agent: str):
    """The agents of the report a person may choose from and the ids to sum (None — every agent, for a superuser)."""
    mine = agents.listing(me)
    chosen = _int(agent)
    if chosen is not None:
        if chosen not in {a["id"] for a in mine}:
            raise HTTPException(404, "Нет такого агента")
        return mine, chosen, [chosen]
    return mine, None, (None if me["role"] == "superuser" else [a["id"] for a in mine])


@router.get("/agents/spend", response_class=HTMLResponse)
def spend_page(request: Request, days: str = "7", agent: str = ""):
    me = _person(request)
    mine, chosen, ids = _spend_scope(me, agent)
    return _page(request, "net/spend.html", r=spend.report(ids, spend.period(days)), agents=mine, chosen=chosen,
                 boss=me["role"] == "superuser", periods=spend.PERIODS, groups=spend.GROUPS, fmt_tokens=trace.tokens)


@router.get("/agents/spend.csv")
def spend_csv(request: Request, days: str = "7", agent: str = ""):
    me = _person(request)
    _, chosen, ids = _spend_scope(me, agent)
    days = spend.period(days)
    name = f"sysai-spend-{days}d" + (f"-{chosen}" if chosen else "") + ".csv"
    return Response(spend.csv_rows(ids, days), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})


@router.post("/agents/create")
def agents_create(request: Request, user_id: str = Form(""), name: str = Form(""), department_id: str = Form(""),
                  employee_id: str = Form(""), harness: str = Form("hermes"), profile: str = Form("")):
    me = _person(request)
    boss = me["role"] == "superuser"
    if not boss and len(agents.listing(me)) >= MAX_OWN_AGENTS:
        return _back("/agents", error=f"Не больше {MAX_OWN_AGENTS} агентов на человека")
    owner = _int(user_id) if boss else me["id"]
    try:
        # a plain user's own agent starts as «Наблюдатель»: access is given in the settings, by a superuser
        agent = agents.create(owner or me["id"], name, _int(department_id), _int(employee_id) if boss else None, harness,
                              profile if boss else "viewer", by=me["username"])
    except agents.AgentError as exc:
        return _back("/agents", error=str(exc))
    return _back(f"/agents/{agent.id}", msg="Агент создан. Получите код и подключите компьютер")


def _own_agent(me: dict, agent_id: int) -> NetAgent:
    with SessionLocal() as s:
        agent = s.get(NetAgent, agent_id)
    if agent is None or (me["role"] != "superuser" and agent.user_id != me["id"]):
        raise HTTPException(404, "Нет такого агента")
    return agent


def _agent_view(request: Request, me: dict, agent_id: int, msg: str = "", error: str = "", code: str = ""):
    agent = _own_agent(me, agent_id)
    boss = me["role"] == "superuser"
    item = next(a for a in agents.listing(None) if a["id"] == agent.id)
    doc = access.document(agent.id, _server(request))
    with SessionLocal() as s:
        tokens = [{"kind": t.kind, "hint": t.hint, "created_at": t.created_at, "expires_at": t.expires_at,
                   "last_used_at": t.last_used_at, "used_from": t.used_from, "revoked_at": t.revoked_at}
                  for t in s.query(NetAgentToken).filter(NetAgentToken.agent_id == agent.id)
                  .order_by(NetAgentToken.created_at.desc()).limit(10)]
    harness_yaml = access.to_yaml(doc["doc"]["harness_config"]) if doc["doc"]["harness_config"] else ""
    return _page(request, "net/agent.html", a=item, agent=agent, doc=doc, rules=doc["rules"], boss=boss, msg=msg,
                 error=error, code=code, server=_server(request), tokens=tokens, harness_yaml=harness_yaml,
                 hermes_note=access.hermes_note(item.get("versions")),
                 spend=gateway.summary(agent.id, days=7), events=journal.feed(agent.id, 40), profiles=access.profiles(),
                 runs=trace.runs(agent.id, days=2)[:3], debug_until=trace.debug_until(agent.id), fmt_tokens=trace.tokens,
                 fmt_ms=trace.duration, clock=clock, debug_hours=trace.DEBUG_HOURS,
                 overrides=json.dumps(doc["overrides"] or {}, ensure_ascii=False, indent=2),
                 sections=access.SECTION_RU, tier_ru=access.TIER_RU, scope_ru=access.SCOPE_RU, level_ru=access.LEVEL_RU,
                 internet_ru=access.INTERNET_RU, harnesses=agents.HARNESSES, ttl=int(agents.CODE_TTL.total_seconds() // 60),
                 sync_version=installer.version(), **_choices())


@router.get("/agents/{agent_id:int}", response_class=HTMLResponse)
def agent_page(request: Request, agent_id: int, msg: str = "", error: str = ""):
    me = _person(request)
    return _agent_view(request, me, agent_id, msg, error)


@router.post("/agents/{agent_id:int}/connect", response_class=HTMLResponse)
def agent_connect(request: Request, agent_id: int):
    me = _person(request)
    _own_agent(me, agent_id)
    try:
        code = agents.issue_code(agent_id, by=me["username"])
    except agents.AgentError as exc:
        return _back(f"/agents/{agent_id}", error=str(exc))
    resp = _agent_view(request, me, agent_id, code=code)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.get("/agents/{agent_id:int}/trace", response_class=HTMLResponse)
def agent_trace(request: Request, agent_id: int, days: int = 1, errors: int = 0, msg: str = "", error: str = ""):
    me = _person(request)
    agent = _own_agent(me, agent_id)
    days = max(1, min(trace.DEBUG_KEEP_DAYS, days))
    runs = trace.runs(agent.id, days=days)
    if errors:
        runs = [r for r in runs if r["errors"]]
    return _page(request, "net/trace.html", agent=agent, runs=runs, days=days, only_errors=bool(errors), msg=msg,
                 error=error, debug_until=trace.debug_until(agent.id), debug_by=agent.debug_by or "",
                 debug_hours=trace.DEBUG_HOURS, keep_days=trace.DEBUG_KEEP_DAYS, fmt_tokens=trace.tokens,
                 fmt_ms=trace.duration, clock=clock)


@router.get("/agents/{agent_id:int}/trace/{text_id:int}", response_class=HTMLResponse)
def agent_trace_text(request: Request, agent_id: int, text_id: int):
    me = _person(request)
    agent = _own_agent(me, agent_id)
    item = trace.text(agent.id, text_id)
    if item is None:
        raise HTTPException(404, "Нет такого текста: режим отладки хранит их 7 дней")
    return _page(request, "net/trace_text.html", agent=agent, item=item,
                 request_text=json.dumps(item["request"], ensure_ascii=False, indent=2, default=str),
                 response_text=json.dumps(item["response"], ensure_ascii=False, indent=2, default=str))


@router.post("/agents/{agent_id:int}/{action}")
async def agent_action(request: Request, agent_id: int, action: str):
    me = _person(request)
    agent = _own_agent(me, agent_id)
    boss = me["role"] == "superuser"
    form = await request.form()
    try:
        if action == "disable":
            agents.revoke(agent_id, by=me["username"])
            return _back(f"/agents/{agent_id}", msg="Агент отключён: токен отозван, MCP и шлюз ему отказывают")
        if action == "enable":
            if not agents.enable(agent_id, by=me["username"], boss=boss):
                return _back(f"/agents/{agent_id}", msg="Агент уже включён")
            return _back(f"/agents/{agent_id}", msg="Агент включён: получите новый код подключения")
        if action == "debug":
            on = str(form.get("on") or "") == "1"
            trace.set_debug(agent_id, on, by=me["username"])
            return _back(f"/agents/{agent_id}/trace", msg=f"Режим отладки включён на {trace.DEBUG_HOURS} ч: SYSAI сохраняет "
                                                         "тексты запросов, ответов и вызовов MCP" if on else
                         "Режим отладки выключен: тексты больше не сохраняются")
        if action == "delete" and boss:
            agents.delete(agent_id, by=me["username"])
            return _back("/agents", msg=f"Агент {agent.name} удалён")
        if action == "access" and boss:
            text = str(form.get("overrides") or "").strip()
            try:
                overrides = json.loads(text) if text else {}
            except ValueError:
                return _back(f"/agents/{agent_id}#access", error="Личные исключения: это не JSON")
            access.set_agent_access(agent_id, _int(form.get("profile_id")), overrides, by=me["username"])
            with SessionLocal() as s:
                row = s.get(NetAgent, agent_id)
                before = {"сотрудник": row.employee_id, "отдел": row.department_id}
                row.employee_id = _int(form.get("employee_id"))
                row.department_id = _int(form.get("department_id"))
                changes = journal.changes(before, {"сотрудник": row.employee_id, "отдел": row.department_id})
                if changes:
                    journal.log("agent.settings", row.name, changes, actor=me["username"], agent_id=row.id, session=s)
                s.commit()
            return _back(f"/agents/{agent_id}#access", msg="Доступы сохранены: агент применит их при следующей синхронизации")
    except (agents.AgentError, access.AccessError) as exc:
        return _back(f"/agents/{agent_id}", error=str(exc))
    raise HTTPException(404, "Нет такого действия")


# ============================================================================= pages: access profiles
@router.post("/agents/access/jev-probe")
def access_jev_probe(request: Request):
    """«Проверить Jev»: one tiny request through sysai/auto; what came back is shown on the page."""
    _person(request, admin=True)
    res = gateway.probe_jev()
    if res["ok"]:
        return _back("/agents/access#models", msg=f"Jev работает: ответила {res['served'] or 'модель'} за {res['ms']} мс")
    return _back("/agents/access#models", error="Jev не ответил: " + ((f"HTTP {res['status']} " if res["status"] else "") + res["message"])[:200])


@router.get("/agents/access", response_class=HTMLResponse)
def access_page(request: Request, msg: str = "", error: str = "", edit: str = ""):
    _person(request, admin=True)
    models = settings_store.get("agents_models") or {}
    spare = settings_store.get("agents_fallback")
    return _page(request, "net/access.html", profiles=access.profiles(), msg=msg, error=error, edit=edit,
                 jev=gateway.jev_status(), edits=access.EDITS, edits_ru=access.EDITS_RU,
                 sections=access.SECTION_RU, tiers=access.TIERS, tier_ru=access.TIER_RU, model_tiers=access.MODEL_TIERS,
                 cost_tiers=access.COST_TIERS, cost_ru=access.COST_RU, scopes=access.SCOPES,
                 scope_ru=access.SCOPE_RU, levels=access.LEVELS, level_ru=access.LEVEL_RU, internet=access.INTERNET,
                 internet_ru=access.INTERNET_RU, always_deny=access.ALWAYS_DENY, sysai_tools=access.SYSAI_TOOLS,
                 files_common=access.FILES_COMMON, files_own=access.FILES_OWN,
                 models={t: (models.get(t) if isinstance(models, dict) else "") or gateway.DEFAULT_MODELS.get(t, "")
                         for t in access.MODEL_TIERS},
                 fallbacks={t: str(spare.get(t) or "") if isinstance(spare, dict) and t in spare else
                            gateway.DEFAULT_FALLBACK.get(t, "") for t in access.MODEL_TIERS},
                 vision_default=settings_store.get("vision_model"), mcp_per_minute=mcp.rate_limit(),
                 hub_hosts="\n".join(settings_store.get("agents_hub_hosts") or hub.DEFAULT_HOSTS),
                 soul=soul.texts(), soul_departments=_soul_departments(), soul_max=soul.MAX_TEXT,
                 events=journal.feed(None, 20, "profile:"))


def _soul_departments() -> list:
    """Departments for the additions of the character: the active ones and any that already has an addition."""
    names = [d["name"] for d in _choices()["departments"]]
    for name in soul.texts()["departments"]:
        if not any(n.lower() == name.lower() for n in names):
            names.append(name)
    return names


@router.post("/agents/access/soul")
async def access_soul(request: Request):
    me = _person(request, admin=True)
    form = await request.form()
    additions = {}
    for key, name in form.items():
        if key.startswith("dept_name_") and str(name).strip():
            additions[str(name).strip()] = str(form.get("dept_" + key[10:]) or "")
    soul.save(str(form.get("common") or ""), additions, by=me["username"])
    return _back("/agents/access#soul", msg="Характер сохранён: агенты получат новый SOUL.md при следующей синхронизации")


@router.post("/agents/access/settings")
async def access_settings(request: Request):
    me = _person(request, admin=True)
    form = await request.form()
    models, spare = {}, {}
    for tier in access.MODEL_TIERS:  # sysai/auto has no model of its own: Jev picks among these
        for prefix, table in (("model_", models), ("fallback_", spare)):
            value = str(form.get(prefix + tier) or "").strip()
            if value and (len(value) > 200 or not all(ch.isalnum() or ch in "._:/-~" for ch in value)):
                return _back("/agents/access#models", error=f"Некорректный идентификатор модели: {value}")
            table[tier] = value
    hosts = [h.strip().lower() for h in str(form.get("hub_hosts") or "").replace(",", "\n").splitlines() if h.strip()][:100]
    per_minute = _int(form.get("mcp_per_minute")) or 120
    keys = ("agents_models", "agents_fallback", "agents_hub_hosts", "agents_mcp_per_minute")
    before = {k: settings_store.get(k) for k in keys}
    after = {"agents_models": models, "agents_fallback": spare, "agents_hub_hosts": hosts,
             "agents_mcp_per_minute": max(1, min(10000, per_minute))}
    settings_store.set_many(after)
    journal.log("net.settings", "settings", journal.changes(before, after), actor=me["username"])
    return _back("/agents/access#models", msg="Сохранено: новые модели уровней действуют для всех агентов сразу")


@router.post("/agents/access/new")
async def access_new(request: Request):
    me = _person(request, admin=True)
    form = await request.form()
    base = next((p for p in access.profiles() if p["code"] == form.get("copy")), None)
    try:
        pid = access.save_profile(None, str(form.get("name") or ""), base["description"] if base else "",
                                  base["rules"] if base else access.EMPTY, by=me["username"], code=str(form.get("code") or ""))
    except access.AccessError as exc:
        return _back("/agents/access", error=str(exc))
    return _back(f"/agents/access?edit={pid}#p{pid}", msg="Профиль создан")


@router.post("/agents/access/{profile_id:int}")
async def access_save(request: Request, profile_id: int):
    me = _person(request, admin=True)
    form = await request.form()
    try:
        if form.get("delete"):
            access.delete_profile(profile_id, by=me["username"])
            return _back("/agents/access", msg="Профиль удалён")
        access.save_profile(profile_id, str(form.get("name") or ""), str(form.get("description") or ""),
                            access.rules_from_form(form), by=me["username"])
    except access.AccessError as exc:
        return _back(f"/agents/access?edit={profile_id}#p{profile_id}", error=str(exc))
    return _back(f"/agents/access#p{profile_id}", msg="Профиль сохранён: агенты применят его при следующей синхронизации")


# ============================================================================= pages: SYSAI_Hub
HUB_CHIPS = (("all", "Все"), ("skill", "Навыки"), ("mcp", "MCP-серверы"), ("program", "Программы"), ("review", "На проверке"),
             ("on_demand", "По запросу"), ("draft", "Черновики"), ("retired", "Сняты"))
HUB_EMPTY = {"skill": "Навыков пока нет: загрузите папку с SKILL.md или напишите навык в редакторе.",
             "mcp": "Своих MCP-серверов пока нет: загрузите папку с TOOL.md (kind: mcp).",
             "program": "Программ пока нет: загрузите папку с TOOL.md (kind: program) — скрипт или утилиту для ПК."}


def _hub_page(request: Request, me: dict, panel: str = "", selected: str = "", **ctx):
    items = hub.listing()
    queue = hub.review_queue()
    groups = [{"key": kind, "name": hub.KIND_GROUP[kind], "empty": HUB_EMPTY[kind],
               "items": ([hub.builtin()] if kind == "mcp" else []) + [i for i in items if i["kind"] == kind]}
              for kind in hub.KINDS]
    summary = {"skills": sum(1 for i in items if i["kind"] == "skill"), "mcp": 1 + sum(1 for i in items if i["kind"] == "mcp"),
               "programs": sum(1 for i in items if i["kind"] == "program"), "review": len(queue),
               "on_demand": sum(1 for i in items if i["delivery"] == "on_demand")}
    ctx.setdefault("k", None)
    for key in ("msg", "error", "q", "a", "b"):
        ctx.setdefault(key, "")
    ctx.setdefault("changes", [])
    return _page(request, "net/hub.html", groups=groups, queue=queue, summary=summary, chips=HUB_CHIPS, panel=panel,
                 panel_open=panel in ("item", "upload"), selected=selected, platform_ru=hub.PLATFORM_RU,
                 boss=me["role"] == "superuser", me=me, bundles=hub_bundles.listing(), **ctx, **_choices())


@router.get("/hub", response_class=HTMLResponse)
def hub_page(request: Request, q: str = "", upload: str = "", msg: str = "", error: str = ""):
    me = _person(request)
    return _hub_page(request, me, panel="upload" if upload else "", q=q, msg=msg, error=error)


@router.post("/hub/upload")
async def hub_upload(request: Request, file: UploadFile = File(...), changelog: str = Form(""), review: str = Form(""),
                     department_id: str = Form("")):
    me = _person(request)
    data = await file.read(hub.MAX_TOOL_ZIP + 1)
    try:
        files_ = hub.read_zip(data)
        name, version = hub.submit(files_, user=me, changelog=changelog, review=bool(review), department_id=_int(department_id))
    except hub.HubError as exc:
        return _back("/hub?upload=1", error=str(exc))
    return _back(f"/hub/{name}?v={version}", msg=f"Загружена версия {version}")


def _new_kind(kind: str) -> str:
    return kind if kind in ("mcp", "program") else "skill"


@router.get("/hub/new", response_class=HTMLResponse)
def hub_new_page(request: Request, kind: str = "", error: str = ""):
    _person(request)
    kind = _new_kind(kind)
    text = hub.template() if kind == "skill" else hub.tool_template("my-tool" if kind == "mcp" else "my-program", kind)
    return _page(request, "net/skill_edit.html", skill=None, text=text, error=error, base="", kind=kind,
                 doc=hub.SKILL_DOC if kind == "skill" else hub.TOOL_DOC, files=[], **_choices())


@router.post("/hub/new")
def hub_new(request: Request, skill_md: str = Form(...), changelog: str = Form(""), review: str = Form(""),
            department_id: str = Form(""), kind: str = Form("")):
    me = _person(request)
    kind = _new_kind(kind)
    meta, _ = hub.parse_skill_md(skill_md)
    if str(meta.get("kind") or "").strip().lower() in ("mcp", "program"):
        kind = str(meta["kind"]).strip().lower()
    try:
        name, version = hub.submit({hub.SKILL_DOC if kind == "skill" else hub.TOOL_DOC: skill_md}, user=me, changelog=changelog,
                                   review=bool(review), department_id=_int(department_id))
    except hub.HubError as exc:
        return _back("/hub/new" + ("" if kind == "skill" else f"?kind={kind}"), error=str(exc))
    return _back(f"/hub/{name}?v={version}", msg=f"Создана версия {version}")


@router.get("/hub/bundled/{name}.zip")
def hub_bundled_download(request: Request, name: str):
    _person(request)
    try:
        item = hub_bundles.package(name)
    except (OSError, hub.HubError) as exc:
        raise HTTPException(404, "Пакет недоступен") from exc
    if item is None:
        raise HTTPException(404, "Нет такого пакета")
    return Response(item["blob"], media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{name}-{item["version"]}.zip"',
                             "X-SHA256": item["sha256"], "Cache-Control": "no-store"})


def _bundle_install(name: str, me: dict) -> str:
    done = hub_bundles.install(name, me)
    if done["published"]:
        return f"{name} {done['version']} опубликована: агенты получат её при следующей синхронизации"
    return f"{name} {done['version']} загружена и ждёт публикации ({done['note']})"


@router.post("/hub/bundled/install-all")
def hub_bundled_install_all(request: Request):
    """Every package that brings something new, in one step."""
    me = _person(request)
    lines, errors = [], []
    for item in hub_bundles.listing():
        if item["state"] in ("update", "new", "pending"):
            try:
                lines.append(_bundle_install(item["name"], me))
            except (OSError, hub.HubError) as exc:
                errors.append(f"{item['name']}: {exc}")
    if errors:
        return _back("/hub", error="; ".join(errors + lines))
    return _back("/hub", msg="; ".join(lines) or "Все пакеты уже установлены")


@router.post("/hub/bundled/{name}/install")
def hub_bundled_install(request: Request, name: str):
    me = _person(request)
    try:
        return _back(f"/hub/{name}", msg=_bundle_install(name, me))
    except (OSError, hub.HubError) as exc:
        return _back("/hub", error=str(exc))


@router.get("/hub/{name}/{version}.zip")
def hub_download(request: Request, name: str, version: str):
    _person(request)
    try:
        blob, sha = hub.blob_of(name, version)
    except hub.HubError as exc:
        raise HTTPException(404, str(exc)) from exc
    return Response(blob, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{name}-{version}.zip"',
                                                                 "X-SHA256": sha})


@router.get("/hub/{name}", response_class=HTMLResponse)
def hub_skill(request: Request, name: str, v: str = "", a: str = "", b: str = "", msg: str = "", error: str = "", edit: str = ""):
    me = _person(request)
    card = hub.card(name)
    if card is None:
        raise HTTPException(404, "В хабе этого нет")
    chosen = next((x for x in card["versions"] if x["version"] == v), None) or \
        next((x for x in card["versions"] if x["current"]), None) or (card["versions"][0] if card["versions"] else None)
    changes = []
    if a and b and not card.get("builtin"):
        try:
            changes = hub.diff(name, a, b)
        except hub.HubError as exc:
            error = str(exc)
    approver = False
    if not card.get("builtin"):
        with SessionLocal() as s:
            skill = s.query(NetSkill).filter(NetSkill.name == name).first()
            approver = hub.can_approve(me, skill)
    if edit and chosen:
        others = [f for f in chosen["files"] if f["path"] != card["doc"]]
        return _page(request, "net/skill_edit.html", skill=card, text=chosen["skill_md"], error=error, base=chosen["version"],
                     kind=card["kind"], doc=card["doc"], files=others, **_choices())
    key = f"{name}@{chosen['version']}" if chosen and chosen["status"] == "review" and v else name
    return _hub_page(request, me, panel="item", selected=key, k=card, chosen=chosen, changes=changes, a=a, b=b, msg=msg,
                     error=error, approver=approver)


@router.post("/hub/{name}/edit")
def hub_edit(request: Request, name: str, skill_md: str = Form(...), changelog: str = Form(""), review: str = Form(""),
             base: str = Form("")):
    me = _person(request)
    try:
        files_ = hub.files_of(name, base) if base else {}
        card = hub.card(name)
        doc = hub.TOOL_DOC if hub.TOOL_DOC in files_ or (card and card["kind"] != "skill") else hub.SKILL_DOC
        files_[doc] = skill_md.encode("utf-8")
        new_name, version = hub.submit(files_, user=me, changelog=changelog, review=bool(review))
    except hub.HubError as exc:
        return _back(f"/hub/{name}?edit=1&v={base}", error=str(exc))
    return _back(f"/hub/{new_name}?v={version}", msg=f"Создана версия {version}")


@router.post("/hub/{name}/settings")
async def hub_settings(request: Request, name: str):
    me = _person(request)
    form = await request.form()
    audience = {"all": True} if form.get("audience") != "departments" else \
        {"departments": [i for i in (_int(x) for x in form.getlist("audience_departments")) if i]}
    try:
        hub.configure(name, me, _int(form.get("department_id")), _int(form.get("owner_user_id")), audience,
                      delivery=form.get("delivery") or None)
    except hub.HubError as exc:
        return _back(f"/hub/{name}", error=str(exc))
    return _back(f"/hub/{name}", msg="Настройки сохранены")


@router.post("/hub/{name}/{action}")
def hub_skill_action(request: Request, name: str, action: str):
    me = _person(request)
    try:
        if action == "retire":
            hub.retire(name, me)
            return _back(f"/hub/{name}", msg="Снято с хаба: агенты уберут это при следующей синхронизации")
        if action == "restore":
            hub.retire(name, me, back=True)
            return _back(f"/hub/{name}", msg="Возвращено в хаб")
    except hub.HubError as exc:
        return _back(f"/hub/{name}", error=str(exc))
    raise HTTPException(404, "Нет такого действия")


@router.post("/hub/{name}/{version}/{action}")
def hub_version_action(request: Request, name: str, version: str, action: str, note: str = Form("")):
    me = _person(request)
    try:
        if action == "review":
            hub.to_review(name, version, me)
            done = "Версия отправлена на проверку"
        elif action == "publish":
            hub.publish(name, version, me, note)
            done = f"Версия {version} опубликована: агенты получат её при следующей синхронизации"
        elif action == "reject":
            hub.reject(name, version, me, note)
            done = "Версия отклонена"
        elif action == "rollback":
            hub.rollback(name, version, me)
            done = f"Действующая версия — {version}"
        else:
            raise HTTPException(404, "Нет такого действия")
    except hub.HubError as exc:
        return _back(f"/hub/{name}?v={version}", error=str(exc))
    return _back(f"/hub/{name}?v={version}", msg=done)


# ============================================================================= pages: the shared storage
FILE_CHIPS = (("all", "Все"), ("folder", "Папки"), ("doc", "Документы"), ("sheet", "Таблицы"), ("drawing", "Чертежи"),
              ("image", "Изображения"), ("archive", "Архивы"), ("code", "Код"), ("file", "Другое"))


def _files_url(path: str = "", f: str = "", **extra) -> str:
    params = [(k, v) for k, v in (("path", path), ("f", f), *extra.items()) if v]
    return "/files" + ("?" + "&".join(f"{k}={quote(str(v), safe='')}" for k, v in params) if params else "")


def _parent(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else ""


@router.get("/files", response_class=HTMLResponse)
def files_page(request: Request, path: str = "", f: str = "", q: str = "", trash: str = "", p: str = "", msg: str = "",
               error: str = ""):
    me = _person(request)
    rights = files.for_person(me)
    try:
        found = files.listing(rights, path, q=q, trash=bool(trash))
    except files.FilesError as exc:
        if path or q or trash:
            return _back("/files", error=str(exc))
        raise HTTPException(403, str(exc)) from exc
    item = None
    if f:
        try:
            item = files.info(rights, f)
        except files.FilesError as exc:
            error = error or str(exc)
    folder = None
    if not item and "/" in found["path"] and not q and not trash:
        try:
            folder = files.info(rights, found["path"])
        except files.FilesError:
            folder = None
    groups = []
    if not found["path"] and not q and not trash:
        groups.append({"key": "sections", "name": "Разделы", "items": found["items"]})
    elif trash:
        groups.append({"key": "trash", "name": "В корзине", "items": found["items"]})
    else:
        groups.append({"key": "folders", "name": "Папки", "items": [i for i in found["items"] if i["is_folder"]]})
        groups.append({"key": "files", "name": "Файлы", "items": [i for i in found["items"] if not i["is_folder"]]})
    return _page(request, "net/files.html", found=found, groups=groups, item=item, folder=folder, summary=files.summary(rights),
                 chips=FILE_CHIPS, msg=msg, error=error, boss=me["role"] == "superuser", rights=rights,
                 panel_open=bool(item) or bool(p), selected=item["path"] if item else "", files_url=_files_url, parent=_parent,
                 max_mb=files.MAX_UPLOAD // files.MB, s3=storage.configured())


async def _spool(stream, limit: int) -> tuple[str, int]:
    """An upload into a temporary file of the server, refusing what is over the limit; (path, size)."""
    import os
    import tempfile
    handle, tmp = tempfile.mkstemp(prefix="sysai-file-", dir=str(config.DATA_DIR))
    size = 0
    try:
        with os.fdopen(handle, "wb") as out:
            async for block in stream:
                size += len(block)
                if size > limit:
                    raise files.FilesError(f"Файл больше {limit // files.MB} МБ")
                out.write(block)
    except BaseException:
        os.unlink(tmp)
        raise
    return tmp, size


async def _chunks(upload: UploadFile):
    while True:
        block = await upload.read(256 * 1024)
        if not block:
            return
        yield block


@router.post("/files/upload")
async def files_upload(request: Request):
    import os
    me = _person(request)
    rights = files.for_person(me)
    form = await request.form()
    folder = str(form.get("folder") or "")
    note = str(form.get("note") or "")
    uploads = [u for u in form.getlist("files") if hasattr(u, "read") and getattr(u, "filename", "")]
    if not uploads:
        return _back(_files_url(folder), error="Выберите файлы")
    as_name = str(form.get("as_name") or "").strip() if len(uploads) == 1 else ""  # a new version of a chosen file
    done, same, last = 0, 0, ""
    for upload in uploads[:200]:
        try:
            tmp, _ = await _spool(_chunks(upload), files.MAX_UPLOAD)
            try:
                result = files.put(rights, f"{folder}/{as_name or upload.filename}", source=tmp, note=note,
                                   mime=upload.content_type or "")
            finally:
                os.unlink(tmp)
        except files.FilesError as exc:
            return _back(_files_url(folder), error=f"{upload.filename}: {exc}" + (f" (до этого загружено: {done})" if done else ""))
        last = result["path"]
        done += 1 if result["changed"] else 0
        same += 0 if result["changed"] else 1
    text = f"Загружено: {done}" + (f", без изменений: {same}" if same else "")
    return _back(_files_url(folder, last if len(uploads) == 1 else ""), msg=text)


@router.post("/files/mkdir")
def files_mkdir(request: Request, folder: str = Form(""), name: str = Form(...), note: str = Form("")):
    me = _person(request)
    try:
        made = files.mkdir(files.for_person(me), f"{folder}/{name}", note)
    except files.FilesError as exc:
        return _back(_files_url(folder), error=str(exc))
    return _back(_files_url(made["path"]), msg=f"Папка «{made['name']}» создана")


@router.post("/files/{action}")
def files_action(request: Request, action: str, path: str = Form(...)):
    me = _person(request)
    rights = files.for_person(me)
    try:
        if action == "delete":
            gone = files.remove(rights, path)
            return _back(_files_url(_parent(gone["path"])), msg="В корзине: вернуть можно из «Корзины»")
        if action == "restore":
            files.restore(rights, path)
            return _back(_files_url(trash="1"), msg="Возвращено")
        if action == "purge":
            files.purge(rights, path)
            return _back(_files_url(trash="1"), msg="Удалено навсегда")
    except files.FilesError as exc:
        return _back(_files_url(_parent(path), trash="1" if action != "delete" else ""), error=str(exc))
    raise HTTPException(404, "Нет такого действия")


def _file_response(found: dict, inline: bool = False):
    where, _, key = found["stored"].partition(":")
    if where == "s3":
        return RedirectResponse(storage.download_url(key, 600, filename="" if inline else found["name"]), 302)
    response = FileResponse(files._disk(key), media_type=found["mime"], filename=found["name"],
                            content_disposition_type="inline" if inline else "attachment")
    response.headers["X-Content-Type-Options"] = "nosniff"  # a stored file is shown as what it is, never as a page
    return response


@router.get("/files/raw")
def files_raw(request: Request, path: str, v: int = 0, inline: str = ""):
    me = _person(request)
    try:
        found = files.blob(files.for_person(me), path, v or None)
    except files.FilesError as exc:
        raise HTTPException(404, str(exc)) from exc
    safe = found["mime"].startswith(("image/", "application/pdf", "text/plain")) and "svg" not in found["mime"]
    return _file_response(found, inline=bool(inline) and safe)


# ============================================================================= API of agents
@router.get("/agents/download/install.ps1")
def download_agent_installer(request: Request):
    """A Windows PowerShell installer tied to this server; the user enters a one-use code on the PC."""
    _person(request)
    data = installer.script(_server(request)).encode("utf-8-sig")
    return Response(data, media_type="application/octet-stream",
                    headers={"Content-Disposition": 'attachment; filename="SYSAI-install.ps1"',
                             "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@router.get("/agents/api/public-key")
def api_public_key():
    if not enabled():
        return _api_error(404, "Not Found", "not_found")
    return access.public_key()


@router.get("/agents/api/install.ps1")
def api_install_script(request: Request):
    """The installer for the employee's PC: «$env:SYSAI_CODE='…'; irm <server>/agents/api/install.ps1 | iex»."""
    if not enabled():
        return _api_error(404, "Not Found", "not_found")
    return Response(installer.script(_server(request)), media_type="text/plain; charset=utf-8",
                    headers={"Cache-Control": "no-store"})


@router.get("/agents/api/sync/{filename}")
def api_sync_wheel(filename: str):
    """The synchronizer as a wheel (uv tool install takes it by this address)."""
    if not enabled():
        return _api_error(404, "Not Found", "not_found")
    name, blob, sha = installer.wheel()
    if filename != name:
        return _api_error(404, f"Нет такой сборки синхронизатора: сейчас {name}", "not_found")
    return Response(blob, media_type="application/zip", headers={"X-SHA256": sha, "Cache-Control": "no-store",
                                                                 "Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/agents/api/desktop/{filename}")
def api_desktop_app(filename: str):
    """The window «Агент SYSAI» (the app without Electron: the synchronizer downloads Electron itself and checks
    both by the sha256 in the signed document)."""
    if not enabled():
        return _api_error(404, "Not Found", "not_found")
    name, blob, sha = desktop.build()
    if filename != name:
        return _api_error(404, f"Нет такой сборки окна агента: сейчас {name}", "not_found")
    return Response(blob, media_type="application/zip", headers={"X-SHA256": sha, "Cache-Control": "no-store",
                                                                 "Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/agents/api/desktop/electron/{filename}")
def api_desktop_electron(filename: str):
    """Electron for the window, from SYSAI's checked copy (computers that reach SYSAI but not the internet); while
    SYSAI is still downloading it — 503, the computer takes it from the release addresses in the document."""
    if not enabled():
        return _api_error(404, "Not Found", "not_found")
    if filename != desktop.ELECTRON["file"]:
        return _api_error(404, f"Нет такой сборки Electron: сейчас {desktop.ELECTRON['file']}", "not_found")
    path = desktop.electron_path()
    if path is None:
        desktop.prefetch()
        return _api_error(503, "SYSAI ещё скачивает Electron для окна агента", "not_ready", {"Retry-After": "600"})
    return FileResponse(path, media_type="application/zip", filename=filename,
                        headers={"X-SHA256": desktop.ELECTRON["sha256"], "Cache-Control": "no-store"})


@router.post("/agents/api/connect")
async def api_connect(request: Request):
    if not enabled():
        return _api_error(404, "Not Found", "not_found")
    try:
        data = await request.json()
    except ValueError:
        return _api_error(400, "Нужен JSON: {\"code\": \"ABCD-EFGH\", \"host\": \"…\"}", "invalid_request")
    if not isinstance(data, dict):
        return _api_error(400, "Нужен JSON-объект", "invalid_request")
    try:
        agent, token = await _in_thread(agents.exchange, str(data.get("code") or ""), str(data.get("host") or ""),
                                        str(data.get("harness") or ""), data.get("versions"), _ip(request))
    except agents.AgentError as exc:
        return _api_error(400, str(exc), "bad_code")
    server = _server(request)
    return {"agent": agent.name, "token": token, "server": server, "config": f"{server}/agents/api/config",
            "mcp": f"{server}/mcp", "gateway": f"{server}/v1", "public_key": access.public_key()}


@router.get("/agents/api/config")
def api_config(request: Request):
    ctx, problem = _agent_ctx(request)
    if problem:
        return problem
    return access.signed_document(ctx.agent_id, _server(request))


@router.post("/agents/api/heartbeat")
async def api_heartbeat(request: Request):
    ctx, problem = await _in_thread(_agent_ctx, request)  # the base is not asked inside the event loop
    if problem:
        return problem
    try:
        data = await request.json()
    except ValueError:
        data = {}
    return await _in_thread(agents.heartbeat, ctx, data)


@router.get("/agents/api/hub/manifest")
def api_manifest(request: Request):
    ctx, problem = _agent_ctx(request)
    if problem:
        return problem
    return hub.manifest(ctx)


@router.get("/agents/api/hub/{part}/{name}/{version}.zip")
def api_skill_zip(request: Request, part: str, name: str, version: str):
    """The archive of a skill (skills/…) or a tool (tools/…) the agent may have."""
    ctx, problem = _agent_ctx(request)
    if problem:
        return problem
    if part not in ("skills", "tools"):
        return _api_error(404, "Not Found", "not_found")
    found = hub.agent_blob(ctx, name, version)
    if found is None:
        return _api_error(404, "Этого нет среди доступного агенту в хабе", "not_found")
    blob, sha = found
    return Response(blob, media_type="application/zip", headers={"X-SHA256": sha,
                                                                 "Content-Disposition": f'attachment; filename="{name}-{version}.zip"'})


@router.post("/agents/api/hub/proposals")
async def api_propose(request: Request):
    ctx, problem = await _in_thread(_agent_ctx, request)
    if problem:
        return problem
    try:
        if (request.headers.get("content-type") or "").startswith("multipart/"):
            form = await request.form()
            upload = form.get("file")
            if upload is None or not hasattr(upload, "read"):
                return _api_error(400, "Нужен архив навыка или инструмента (zip) в поле file", "invalid_request")
            files = hub.read_zip(await upload.read(hub.MAX_TOOL_ZIP + 1))
            changelog, why = str(form.get("changelog") or ""), str(form.get("why") or "")
        else:
            data = await request.json()
            if not isinstance(data, dict):
                raise ValueError("not an object")
            doc, text = ("TOOL.md", data.get("tool_md")) if str(data.get("tool_md") or "").strip() else \
                ("SKILL.md", data.get("skill_md"))
            if not str(text or "").strip():
                return _api_error(400, "Нужен JSON с skill_md (навык) или tool_md (инструмент), files, changelog, why",
                                  "invalid_request")
            files = {**{str(p): str(v) for p, v in (data.get("files") or {}).items()}, doc: str(text)}
            changelog, why = str(data.get("changelog") or ""), str(data.get("why") or "")
        name, version = await _in_thread(hub.propose, ctx, files, changelog, why)
    except hub.HubError as exc:
        return _api_error(400, str(exc), "rejected")
    except (ValueError, AttributeError):
        return _api_error(400, "Нужен JSON или multipart с zip", "invalid_request")
    card = await _in_thread(hub.card, name)
    return {"skill": name, "kind": (card or {}).get("kind", "skill"), "version": version, "status": "review"}


# ----------------------------------------------------------------------------- the storage, for the synchronizer
def _agent_files(request: Request):
    ctx, problem = _agent_ctx(request)
    if problem:
        return None, problem
    rights = files.for_agent(ctx)
    if rights.nothing:
        return None, _api_error(403, "Хранилище агенту не открыто: разделы открывают в «Доступах» (раздел «Хранилище»)",
                                "forbidden")
    return rights, None


@router.get("/agents/api/files")
def api_files(request: Request, path: str = "", q: str = "", trash: str = ""):
    rights, problem = _agent_files(request)
    if problem:
        return problem
    try:
        found = files.listing(rights, path, q=q, trash=bool(trash))
    except files.FilesError as exc:
        return _api_error(404, str(exc), "not_found")
    keep = ("path", "name", "kind", "is_folder", "size", "version", "sha256", "mime", "note", "updated_at", "updated_by",
            "by_agent", "can_write", "files")
    return {"path": found["path"], "can_write": found["can_write"],
            "items": [{k: i[k] for k in keep if k in i} for i in found["items"]]}


@router.get("/agents/api/files/raw")
def api_file_raw(request: Request, path: str, v: int = 0):
    rights, problem = _agent_files(request)
    if problem:
        return problem
    try:
        found = files.blob(rights, path, v or None)
    except files.FilesError as exc:
        return _api_error(404, str(exc), "not_found")
    response = _file_response(found)
    response.headers["X-SHA256"] = found["sha256"]
    response.headers["X-Version"] = str(found["version"])
    return response


@router.put("/agents/api/files/raw")
async def api_file_put(request: Request, path: str, note: str = ""):
    import os
    rights, problem = await _in_thread(_agent_files, request)
    if problem:
        return problem
    declared = _int(request.headers.get("content-length"))
    if declared and declared > files.MAX_UPLOAD:
        return _api_error(413, f"Файл больше {files.MAX_UPLOAD // files.MB} МБ", "too_large")
    try:
        tmp, _ = await _spool(request.stream(), files.MAX_UPLOAD)
        try:
            result = await _in_thread(functools.partial(files.put, rights, path, source=tmp, note=note,
                                                        mime=request.headers.get("content-type", "").split(";")[0]))
        finally:
            os.unlink(tmp)
    except files.FilesError as exc:
        return _api_error(400, str(exc), "rejected")
    return {"path": result["path"], "version": result["version"], "size": result["size"], "sha256": result["sha256"],
            "changed": result["changed"]}


# ============================================================================= MCP
@router.post("/mcp")
async def mcp_post(request: Request):
    ctx, problem = await _in_thread(_agent_ctx, request)
    if problem:
        return problem
    raw = await request.body()
    if len(raw) > mcp.MAX_BODY:
        return _api_error(413, "Слишком большой запрос", "too_large")
    try:
        payload = json.loads(raw or b"null")
    except ValueError:
        return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}, 400)
    answer = await _in_thread(mcp.handle, ctx, payload)
    if answer is None:
        return Response(status_code=202)
    return JSONResponse(answer)


@router.get("/mcp")
def mcp_get():
    if not enabled():
        return _api_error(404, "Not Found", "not_found")
    return Response(status_code=405, headers={"Allow": "POST"})


@router.delete("/mcp")
def mcp_delete():
    if not enabled():
        return _api_error(404, "Not Found", "not_found")
    return Response(status_code=405, headers={"Allow": "POST"})


# ============================================================================= the gateway
@router.get("/v1/models")
def v1_models(request: Request):
    ctx, problem = _agent_ctx(request)
    if problem:
        return problem
    return gateway.models_list(ctx)


@router.post("/v1/chat/completions")
async def v1_chat(request: Request):
    ctx, problem = await _in_thread(_agent_ctx, request)
    if problem:
        if problem.status_code == 401:  # a dead token of a known agent: seen in its trace and in «Отказы и сбои»
            who = await _in_thread(agents.refused, agents.bearer(request.headers))
            if who:
                await _in_thread(gateway.record_refused_token, *who)
        return problem
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse(gateway.GatewayError(400, "invalid_request", "Нужен JSON").body(), 400)
    project = request.headers.get("x-sysai-project") or ""
    skill = request.headers.get("x-sysai-skill") or ""
    task = ctx.extra.get("task") or ""
    try:
        if isinstance(body, dict) and body.get("stream"):
            chunks = await _in_thread(gateway.stream, ctx, body, project, skill, task)
            return StreamingResponse(_relay(chunks), media_type="text/event-stream",
                                     headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
        return JSONResponse(await _in_thread(gateway.complete, ctx, body, project, skill, task))
    except gateway.GatewayError as exc:
        return JSONResponse(exc.body(), exc.status)
