"""Addresses of the agent network. Pages for people (/agents, /agents/access, /hub) and the API of agents
(/agents/api/*, /mcp, /v1/*). Every address answers 404 while the setting «agents_on» is off."""
import json
from urllib.parse import quote

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse

from .. import config, settings_store
from ..db import Department, Employee, SessionLocal, User
from . import access, agents, enabled, gateway, hub, journal, mcp
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
    sep = "&" if "?" in url else "?"
    if msg:
        url += f"{sep}msg={quote(msg)}"
    elif error:
        url += f"{sep}error={quote(error)}"
    return RedirectResponse(url, 303)


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


def _int(value):
    try:
        return int(value) if str(value or "").strip() else None
    except (TypeError, ValueError):
        return None


def _api_error(status: int, message: str, code: str = "error", headers=None) -> JSONResponse:
    return JSONResponse({"error": {"message": message, "code": code}}, status, headers=headers)


def _agent_ctx(request: Request):
    """(AgentCtx, None) or (None, the JSON answer to give)."""
    if not enabled():
        return None, _api_error(404, "Not Found", "not_found")
    ctx = agents.authenticate(agents.bearer(request.headers), _ip(request))
    if ctx is None:
        return None, _api_error(401, "Нужен действующий токен агента SYSAI", "unauthorized", {"WWW-Authenticate": "Bearer"})
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
                 spend=gateway.summary(agent.id, days=7), events=journal.feed(agent.id, 40), profiles=access.profiles(),
                 overrides=json.dumps(doc["overrides"] or {}, ensure_ascii=False, indent=2),
                 sections=access.SECTION_RU, tier_ru=access.TIER_RU, scope_ru=access.SCOPE_RU, level_ru=access.LEVEL_RU,
                 internet_ru=access.INTERNET_RU, harnesses=agents.HARNESSES, ttl=int(agents.CODE_TTL.total_seconds() // 60),
                 **_choices())


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
            agents.enable(agent_id, by=me["username"])
            return _back(f"/agents/{agent_id}", msg="Агент включён: получите новый код подключения")
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
@router.get("/agents/access", response_class=HTMLResponse)
def access_page(request: Request, msg: str = "", error: str = "", edit: str = ""):
    _person(request, admin=True)
    models = settings_store.get("agents_models") or {}
    return _page(request, "net/access.html", profiles=access.profiles(), msg=msg, error=error, edit=edit,
                 sections=access.SECTION_RU, tiers=access.TIERS, tier_ru=access.TIER_RU, scopes=access.SCOPES,
                 scope_ru=access.SCOPE_RU, levels=access.LEVELS, level_ru=access.LEVEL_RU, internet=access.INTERNET,
                 internet_ru=access.INTERNET_RU, always_deny=access.ALWAYS_DENY,
                 models={t: (models.get(t) if isinstance(models, dict) else "") or gateway.DEFAULT_MODELS.get(t, "") for t in access.TIERS},
                 vision_default=settings_store.get("vision_model"), mcp_per_minute=mcp.rate_limit(),
                 hub_hosts="\n".join(settings_store.get("agents_hub_hosts") or hub.DEFAULT_HOSTS),
                 events=journal.feed(None, 20, "profile:"))


@router.post("/agents/access/settings")
async def access_settings(request: Request):
    me = _person(request, admin=True)
    form = await request.form()
    models = {}
    for tier in access.TIERS:
        value = str(form.get("model_" + tier) or "").strip()
        if value and (len(value) > 200 or not all(ch.isalnum() or ch in "._:/-~" for ch in value)):
            return _back("/agents/access#models", error=f"Некорректный идентификатор модели: {value}")
        models[tier] = value
    hosts = [h.strip().lower() for h in str(form.get("hub_hosts") or "").replace(",", "\n").splitlines() if h.strip()][:100]
    per_minute = _int(form.get("mcp_per_minute")) or 120
    before = {"agents_models": settings_store.get("agents_models"), "agents_hub_hosts": settings_store.get("agents_hub_hosts"),
              "agents_mcp_per_minute": settings_store.get("agents_mcp_per_minute")}
    after = {"agents_models": models, "agents_hub_hosts": hosts, "agents_mcp_per_minute": max(1, min(10000, per_minute))}
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
@router.get("/hub", response_class=HTMLResponse)
def hub_page(request: Request, q: str = "", department: str = "", msg: str = "", error: str = ""):
    me = _person(request)
    return _page(request, "net/hub.html", skills=hub.listing(q, _int(department)), q=q, department=_int(department),
                 queue=hub.review_queue(), msg=msg, error=error, boss=me["role"] == "superuser", **_choices())


@router.post("/hub/upload")
async def hub_upload(request: Request, file: UploadFile = File(...), changelog: str = Form(""), review: str = Form(""),
                     department_id: str = Form("")):
    me = _person(request)
    data = await file.read(hub.MAX_ZIP + 1)
    try:
        files = hub.read_zip(data)
        name, version = hub.submit(files, user=me, changelog=changelog, review=bool(review), department_id=_int(department_id))
    except hub.HubError as exc:
        return _back("/hub", error=str(exc))
    return _back(f"/hub/{name}?v={version}", msg=f"Загружена версия {version}")


@router.get("/hub/new", response_class=HTMLResponse)
def hub_new_page(request: Request, error: str = ""):
    _person(request)
    return _page(request, "net/skill_edit.html", skill=None, text=hub.template(), error=error, base="", **_choices())


@router.post("/hub/new")
def hub_new(request: Request, skill_md: str = Form(...), changelog: str = Form(""), review: str = Form(""),
            department_id: str = Form("")):
    me = _person(request)
    try:
        name, version = hub.submit({"SKILL.md": skill_md}, user=me, changelog=changelog, review=bool(review),
                                   department_id=_int(department_id))
    except hub.HubError as exc:
        return _back("/hub/new", error=str(exc))
    return _back(f"/hub/{name}?v={version}", msg=f"Создана версия {version}")


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
        raise HTTPException(404, "Нет такого навыка")
    chosen = next((x for x in card["versions"] if x["version"] == v), None) or \
        next((x for x in card["versions"] if x["current"]), None) or (card["versions"][0] if card["versions"] else None)
    changes = []
    if a and b:
        try:
            changes = hub.diff(name, a, b)
        except hub.HubError as exc:
            error = str(exc)
    with SessionLocal() as s:
        skill = s.query(NetSkill).filter(NetSkill.name == name).first()
        approver = hub.can_approve(me, skill)
    if edit and chosen:
        return _page(request, "net/skill_edit.html", skill=card, text=chosen["skill_md"], error=error, base=chosen["version"],
                     **_choices())
    return _page(request, "net/skill.html", k=card, chosen=chosen, changes=changes, a=a, b=b, msg=msg, error=error,
                 approver=approver, me=me, **_choices())


@router.post("/hub/{name}/edit")
def hub_edit(request: Request, name: str, skill_md: str = Form(...), changelog: str = Form(""), review: str = Form(""),
             base: str = Form("")):
    me = _person(request)
    try:
        files = hub.files_of(name, base) if base else {}
        files["SKILL.md"] = skill_md.encode("utf-8")
        new_name, version = hub.submit(files, user=me, changelog=changelog, review=bool(review))
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
        hub.configure(name, me, _int(form.get("department_id")), _int(form.get("owner_user_id")), audience)
    except hub.HubError as exc:
        return _back(f"/hub/{name}", error=str(exc))
    return _back(f"/hub/{name}", msg="Настройки навыка сохранены")


@router.post("/hub/{name}/{action}")
def hub_skill_action(request: Request, name: str, action: str):
    me = _person(request)
    try:
        if action == "retire":
            hub.retire(name, me)
            return _back(f"/hub/{name}", msg="Навык снят: агенты уберут его при следующей синхронизации")
        if action == "restore":
            hub.retire(name, me, back=True)
            return _back(f"/hub/{name}", msg="Навык возвращён")
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


# ============================================================================= API of agents
@router.get("/agents/api/public-key")
def api_public_key():
    if not enabled():
        return _api_error(404, "Not Found", "not_found")
    return access.public_key()


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
        agent, token = agents.exchange(str(data.get("code") or ""), str(data.get("host") or ""), str(data.get("harness") or ""),
                                       data.get("versions"), _ip(request))
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
    ctx, problem = _agent_ctx(request)
    if problem:
        return problem
    try:
        data = await request.json()
    except ValueError:
        data = {}
    return agents.heartbeat(ctx, data)


@router.get("/agents/api/hub/manifest")
def api_manifest(request: Request):
    ctx, problem = _agent_ctx(request)
    if problem:
        return problem
    return hub.manifest(ctx)


@router.get("/agents/api/hub/skills/{name}/{version}.zip")
def api_skill_zip(request: Request, name: str, version: str):
    ctx, problem = _agent_ctx(request)
    if problem:
        return problem
    found = hub.agent_blob(ctx, name, version)
    if found is None:
        return _api_error(404, "Нет такого навыка среди доступных агенту", "not_found")
    blob, sha = found
    return Response(blob, media_type="application/zip", headers={"X-SHA256": sha,
                                                                 "Content-Disposition": f'attachment; filename="{name}-{version}.zip"'})


@router.post("/agents/api/hub/proposals")
async def api_propose(request: Request):
    ctx, problem = _agent_ctx(request)
    if problem:
        return problem
    try:
        if (request.headers.get("content-type") or "").startswith("multipart/"):
            form = await request.form()
            upload = form.get("file")
            if upload is None or not hasattr(upload, "read"):
                return _api_error(400, "Нужен файл навыка (zip) в поле file", "invalid_request")
            files = hub.read_zip(await upload.read(hub.MAX_ZIP + 1))
            changelog, why = str(form.get("changelog") or ""), str(form.get("why") or "")
        else:
            data = await request.json()
            if not isinstance(data, dict) or not str(data.get("skill_md") or "").strip():
                return _api_error(400, "Нужен JSON с skill_md, files, changelog, why", "invalid_request")
            files = {**{str(p): str(v) for p, v in (data.get("files") or {}).items()}, "SKILL.md": str(data["skill_md"])}
            changelog, why = str(data.get("changelog") or ""), str(data.get("why") or "")
        name, version = hub.propose(ctx, files, changelog, why)
    except hub.HubError as exc:
        return _api_error(400, str(exc), "rejected")
    except (ValueError, AttributeError):
        return _api_error(400, "Нужен JSON или multipart с zip", "invalid_request")
    return {"skill": name, "version": version, "status": "review"}


# ============================================================================= MCP
@router.post("/mcp")
async def mcp_post(request: Request):
    ctx, problem = _agent_ctx(request)
    if problem:
        return problem
    raw = await request.body()
    if len(raw) > mcp.MAX_BODY:
        return _api_error(413, "Слишком большой запрос", "too_large")
    try:
        payload = json.loads(raw or b"null")
    except ValueError:
        return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}, 400)
    answer = mcp.handle(ctx, payload)
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
    ctx, problem = _agent_ctx(request)
    if problem:
        return problem
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse(gateway.GatewayError(400, "invalid_request", "Нужен JSON").body(), 400)
    project = request.headers.get("x-sysai-project") or ""
    skill = request.headers.get("x-sysai-skill") or ""
    try:
        if isinstance(body, dict) and body.get("stream"):
            return StreamingResponse(gateway.stream(ctx, body, project, skill), media_type="text/event-stream",
                                     headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
        return JSONResponse(gateway.complete(ctx, body, project, skill))
    except gateway.GatewayError as exc:
        return JSONResponse(exc.body(), exc.status)
