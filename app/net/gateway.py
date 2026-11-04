"""The model gateway (ОПО-5.x): agents reach the models only through SYSAI — OpenAI-compatible /v1/chat/completions
and /v1/models, streaming included, signed in by the agent's token. The agent asks for a tier (sysai/cheap, mid,
strong, vision); the real model behind a tier is chosen in the settings, so changing it in SYSAI changes it for every
agent. The OpenRouter key never leaves the server. Budgets per agent per day and per project; every request is
counted in the SYSAI spend (daily budget of the company) and in net_spend with the agent, the project and the skill.

sysai/auto (ОПО-5.8): Jev Router of OpenRouter picks the model and the reasoning effort of each request — only among
the models of the agent's other tiers, and never one dearer than the dearest of them (every dearer model of the
OpenRouter catalog is in the router's hard exclusions). Jev unavailable — the request goes to «mid».

Every request, refused or failed ones too, is in net_spend with its time, its end and who asked (the main model,
workers, compression, vision): the model part of the trace (ОПО-5.7); in the debug mode also its texts."""
import dataclasses
import datetime as dt
import json
import logging
import threading
import time

import httpx
from sqlalchemy import func

from .. import config, llm, settings_store
from ..db import SessionLocal
from . import access, trace
from .agents import AgentCtx
from .models import NetSpend

log = logging.getLogger("sysai.net.gateway")
TIERS = access.TIERS
DEFAULT_MODELS = {"cheap": "google/gemini-2.5-flash-lite", "mid": "google/gemini-2.5-flash", "strong": "google/gemini-2.5-pro",
                  "vision": ""}
DROP = ("models", "route", "user", "sysai")  # no way around the tier: no own model lists or routing
JEV_ROUTER = "typesafe/jev-router"
JEV_EXCLUDE_MAX = (1024, 65_536)    # patterns and characters the router takes in excluded_models
JEV_REST = 600                       # seconds without Jev after it said «unavailable» or «no access»
FALL_BACK_ON = (400, 404, 500, 502, 503, 504)
CATALOG_TTL, CATALOG_RETRY = 12 * 3600, 600
_state = {"prices": {}, "prices_at": 0.0, "prices_failed": 0.0, "jev_rest_until": 0.0, "jev_why": ""}
_lock = threading.Lock()


class GatewayError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message

    def body(self) -> dict:
        return {"error": {"message": self.message, "type": self.code, "code": self.code}}


@dataclasses.dataclass
class Plan:
    """One request on its way: what the agent asked for, where it goes and what to write into the trace."""
    tier: str
    model: str
    out: dict
    project: str = ""
    skill: str = ""
    task: str = ""
    role: str = ""
    route: str = ""
    fallback: str = ""        # sysai/auto: the tier when Jev cannot route
    includes: tuple = ()      # sysai/auto: the models of the agent's tiers
    stream: bool = False
    debug: dict | None = None  # the debug mode: the end of the request

    @property
    def jev(self) -> bool:
        return self.model == JEV_ROUTER

    def fall_back(self, why: str) -> None:
        self.model = model_for(self.fallback)
        self.out = {**self.out, "model": self.model}  # a new body: the one sent to Jev stays as it was
        plugins = [p for p in self.out.get("plugins") or [] if not (isinstance(p, dict) and p.get("id") == "jev-router")]
        if plugins:
            self.out["plugins"] = plugins
        else:
            self.out.pop("plugins", None)
        self.route = f"jev→{self.fallback}: {why}"[:120]


def tier_of(model) -> str:
    name = str(model or "").strip().lower()
    if name.startswith("sysai/"):
        name = name[6:]
    return name if name in TIERS else ""


def model_for(tier: str) -> str:
    models = settings_store.get("agents_models") or {}
    name = str((models.get(tier) if isinstance(models, dict) else "") or DEFAULT_MODELS.get(tier) or "").strip()
    if tier == "vision" and not name:
        name = str(settings_store.get("vision_model") or "google/gemini-2.5-flash")
    return name


def _today() -> str:
    return dt.datetime.now(dt.timezone.utc).date().isoformat()


def spent_agent(agent_id: int, day: str | None = None) -> float:
    with SessionLocal() as s:
        total = s.query(func.coalesce(func.sum(NetSpend.cost), 0.0)).filter(NetSpend.agent_id == agent_id,
                                                                          NetSpend.day == (day or _today())).scalar()
    return float(total or 0.0)


def spent_project(project: str) -> float:
    with SessionLocal() as s:
        total = s.query(func.coalesce(func.sum(NetSpend.cost), 0.0)).filter(NetSpend.project == project).scalar()
    return float(total or 0.0)


def _clip(value, length: int = 64) -> str:
    return "".join(ch for ch in str(value or "") if ch.isprintable())[:length].strip()


# ----------------------------------------------------------------------------- Jev (sysai/auto)
def prices() -> dict:
    """{model: (price of a prompt token, of a completion token)} from the OpenRouter catalog, kept 12 hours."""
    stamp = time.time()
    with _lock:
        if (_state["prices"] and stamp - _state["prices_at"] < CATALOG_TTL) or stamp - _state["prices_failed"] < CATALOG_RETRY:
            return _state["prices"]
    try:
        r = _get(f"{config.OPENROUTER_BASE_URL}/models", _headers())
        rows = r.json().get("data") if r.status_code == 200 else None
        found = {}
        for m in rows if isinstance(rows, list) else []:
            try:
                p = m.get("pricing") or {}
                prompt, completion = float(p.get("prompt") or 0), float(p.get("completion") or 0)
            except (AttributeError, TypeError, ValueError):
                continue
            if isinstance(m.get("id"), str) and prompt >= 0 and completion >= 0:  # routers are priced -1
                found[m["id"]] = (prompt, completion)
        if not found:
            raise ValueError(f"no models (HTTP {r.status_code})")
        with _lock:
            _state.update(prices=found, prices_at=stamp)
    except Exception as exc:  # noqa: BLE001  without prices sysai/auto goes to «mid»
        log.warning("the OpenRouter catalog is not available: %s", exc)
        with _lock:
            _state["prices_failed"] = stamp
    return _state["prices"]


def jev_plugin(rules: dict) -> tuple[dict | None, str, list]:
    """(the jev-router plugin of the request, why not, the models of the agent's tiers). `models` of the router is only
    a preference (it may look wider when none fits), so every model of the catalog dearer than the dearest of the
    agent's — per prompt or per completion token — goes into `excluded_models`, which the router never breaks."""
    includes = sorted({model_for(t) for t in rules["models"]["tiers"] if t in access.MODEL_TIERS} - {""})
    table = prices()
    known = [table[m] for m in includes if m in table]
    if not table or not known:
        return None, "нет цен моделей в каталоге OpenRouter", includes
    top_prompt, top_completion = max(p for p, _ in known), max(c for _, c in known)
    dearer = sorted(m for m, (p, c) in table.items() if p > top_prompt or c > top_completion)
    if len(dearer) > JEV_EXCLUDE_MAX[0] or sum(len(m) for m in dearer) > JEV_EXCLUDE_MAX[1]:
        return None, "слишком много дорогих моделей, чтобы их исключить", includes
    return ({"id": "jev-router", "cost_tier": rules["models"].get("auto_cost") or "low", "models": includes,
             "excluded_models": dearer}, "", includes)


def _jev_resting() -> str:
    with _lock:
        if time.time() < _state["jev_rest_until"]:
            return "Jev недоступен (" + _state["jev_why"] + ")"
    return ""


def _jev_rest(why: str) -> None:
    with _lock:
        _state.update(jev_rest_until=time.time() + JEV_REST, jev_why=why[:60])


def _fallback_tier(rules: dict) -> str:
    tiers = rules["models"]["tiers"]
    return next((t for t in ("mid", "cheap", "strong") if t in tiers), "cheap")


def _route_auto(plan: Plan, rules: dict) -> None:
    plan.fallback = _fallback_tier(rules)
    why, plugin = _jev_resting(), None
    if not why:
        plugin, why, includes = jev_plugin(rules)
        plan.includes = tuple(includes)
    if plugin is None:
        plan.model = model_for(plan.fallback)
        plan.route = f"auto→{plan.fallback}: {why}"[:120]
        return
    plan.model = JEV_ROUTER
    others = [p for p in plan.out.get("plugins") or [] if isinstance(p, dict) and p.get("id") != "jev-router"]
    plan.out["plugins"] = others + [plugin]
    reasoning = plan.out.get("reasoning")
    if isinstance(reasoning, dict):  # the router takes an effort as a hint; a budget of tokens it refuses
        reasoning = {k: v for k, v in reasoning.items() if k not in ("max_tokens", "mode")}
        if reasoning:
            plan.out["reasoning"] = reasoning
        else:
            plan.out.pop("reasoning")
    plan.route = "jev:" + plugin["cost_tier"]


# ----------------------------------------------------------------------------- one request
def draft(body, project: str = "", skill: str = "", task: str = "", stream: bool = False) -> Plan:
    """What the trace knows of a request before any check: the tier asked, who asks (the «sysai» tag in the body that
    the settings of Hermes add for workers, compression and vision) and the task."""
    body = body if isinstance(body, dict) else {}
    tag = body.get("sysai") if isinstance(body.get("sysai"), dict) else {}
    role = str(tag.get("role") or "")
    tier = tier_of(body.get("model"))
    return Plan(tier=tier, model="" if tier else _clip(body.get("model"), 120), out={}, project=_clip(project),
                skill=_clip(skill), task=_clip(task or tag.get("task")), role=role if role in trace.ROLES else "",
                stream=stream)


def prepare(ctx: AgentCtx, body, plan: Plan) -> Plan:
    """The checks and the body for OpenRouter, or GatewayError with a clear reason the agent can tell its person."""
    if not isinstance(body, dict) or not isinstance(body.get("messages"), list):
        raise GatewayError(400, "invalid_request", "Нужен JSON с model и messages")
    tier = plan.tier
    if not tier:
        raise GatewayError(400, "model_not_allowed",
                           "model: один из уровней " + ", ".join(f"sysai/{t}" for t in TIERS) + " — модели выбирает SYSAI")
    rules = access.rules_for(ctx.agent_id)
    if tier not in rules["models"]["tiers"]:
        raise GatewayError(403, "model_not_allowed", f"Доступы агента не разрешают уровень sysai/{tier}")
    if not config.openrouter_key():
        raise GatewayError(503, "no_key", "В SYSAI не задан ключ OpenRouter")
    limit = rules["models"]["budget_day_usd"]
    if limit > 0:
        spent = spent_agent(ctx.agent_id)
        if spent >= limit:
            raise GatewayError(429, "budget_exceeded", f"Дневной бюджет агента исчерпан: ${spent:.2f} из ${limit:.2f}. "
                                                       "Скажи своему человеку: бюджет меняют в SYSAI → Агенты → Доступы")
    project_limit = rules["models"]["budget_project_usd"]
    if plan.project and project_limit > 0:
        spent = spent_project(plan.project)
        if spent >= project_limit:
            raise GatewayError(429, "budget_exceeded", f"Бюджет проекта {plan.project} исчерпан: ${spent:.2f} из ${project_limit:.2f}")
    try:
        llm.check_budget()
    except llm.LLMError as exc:
        raise GatewayError(429, "budget_exceeded", str(exc)) from exc
    out = {k: v for k, v in body.items() if k not in DROP}
    if rules["internet"]["mode"] != "all":
        out.pop("plugins", None)  # web search of OpenRouter is the internet too
    out["user"] = ctx.name  # OpenRouter keeps per-user limits and abuse checks apart
    out["usage"] = {"include": True}
    if plan.stream:
        out["stream"] = True
    else:
        out.pop("stream", None)
        out.pop("stream_options", None)
    plan.out = out
    if tier == "auto":
        _route_auto(plan, rules)
    else:
        plan.model = model_for(tier)
    out["model"] = plan.model
    if trace.debug_on(ctx.agent_id):
        plan.debug = trace.request_excerpt(body)
    return plan


def _headers() -> dict:
    return {"Authorization": f"Bearer {config.openrouter_key()}", "X-Title": "SYSAI agents",
            "HTTP-Referer": config.PUBLIC_URL or "https://github.com/VadimChudin/SYSAI"}


def _get(url: str, headers: dict) -> httpx.Response:
    return httpx.get(url, headers=headers, timeout=httpx.Timeout(30.0, connect=10.0))


def _post(url: str, body: dict, headers: dict) -> httpx.Response:
    return httpx.post(url, json=body, headers=headers, timeout=httpx.Timeout(600.0, connect=30.0))


def _open(url: str, body: dict, headers: dict):
    client = httpx.Client(timeout=httpx.Timeout(600.0, connect=30.0))
    try:
        return client, client.send(client.build_request("POST", url, json=body, headers=headers), stream=True)
    except Exception:
        client.close()
        raise


def _message(text: str) -> str:
    try:
        message = json.loads(text).get("error", {}).get("message") or text
    except (ValueError, AttributeError):
        message = text
    return str(message)[:500]


def _upstream_error(status: int, text: str) -> GatewayError:
    message = _message(text)
    if status in (401, 403):
        return GatewayError(502, "upstream_auth", "OpenRouter не принял ключ SYSAI: " + message)
    if status == 402:
        return GatewayError(402, "upstream_credits", "На счёте OpenRouter нет средств: " + message)
    if status == 429:
        return GatewayError(429, "upstream_rate_limit", "Модель перегружена, повтори позже: " + message)
    if 400 <= status < 500:
        return GatewayError(status, "upstream_rejected", message)
    return GatewayError(502, "upstream_error", f"OpenRouter HTTP {status}: {message}")


def _jev_failed(plan: Plan, status: int, text: str) -> bool:
    """Jev did not route: the request goes again to the fallback tier. True when it should."""
    if not plan.jev or status not in FALL_BACK_ON:
        return False
    why = f"HTTP {status} {_message(text)}"
    if status in (404, 503):  # no access to the router or it is down: do not ask it for a while
        _jev_rest(why)
    log.warning("Jev did not route a request (%s): falling back to sysai/%s", why[:200], plan.fallback)
    plan.fall_back(why)
    return True


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _cached(usage: dict) -> int:
    details = usage.get("prompt_tokens_details")
    value = details.get("cached_tokens") if isinstance(details, dict) else 0
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def record(ctx: AgentCtx, plan: Plan, usage=None, served: str = "", ms: int = 0, error: GatewayError | None = None,
           answer: dict | None = None) -> None:
    usage = usage if isinstance(usage, dict) else {}
    model = str(served or plan.model or "")[:120]
    llm.record_cost(model, usage)  # the company's daily budget sees agents too (a broken stream may cost as well)
    cost = usage.get("cost")
    route = plan.route
    if plan.jev and served and plan.includes and not any(str(served).startswith(m) for m in plan.includes):
        route = (route + " · не из уровней агента")[:120]
    try:
        with SessionLocal() as s:
            row = NetSpend(day=_today(), agent_id=ctx.agent_id, user_id=ctx.user_id, tier=plan.tier, model=model,
                           project=plan.project, skill=plan.skill, prompt_tokens=int(usage.get("prompt_tokens") or 0),
                           completion_tokens=int(usage.get("completion_tokens") or 0), cached_tokens=_cached(usage),
                           cost=float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else 0.0,
                           ms=ms, ok=error is None, error=f"{error.code}: {error.message}"[:300] if error else "",
                           role=plan.role, task=plan.task, route=route)
            s.add(row)
            s.flush()
            if plan.debug is not None:
                trace.save(ctx.agent_id, "model", row.id, request=plan.debug,
                           response=answer if error is None else {"error": f"{error.code}: {error.message}"}, session=s)
            s.commit()
        if plan.debug is not None:
            trace.purge()
    except Exception:  # noqa: BLE001  the answer is still good when the bookkeeping fails
        log.exception("could not record the request of agent %s", ctx.name)


def complete(ctx: AgentCtx, body, project: str = "", skill: str = "", task: str = "") -> dict:
    plan = draft(body, project, skill, task)
    started = time.monotonic()
    try:
        prepare(ctx, body, plan)
        url = f"{config.OPENROUTER_BASE_URL}/chat/completions"
        try:
            r = _post(url, plan.out, _headers())
            if r.status_code != 200 and _jev_failed(plan, r.status_code, r.text):
                r = _post(url, plan.out, _headers())
        except httpx.HTTPError as exc:
            raise GatewayError(502, "upstream_error", f"OpenRouter недоступен: {exc}") from exc
        if r.status_code != 200:
            raise _upstream_error(r.status_code, r.text)
        try:
            data = r.json()
        except ValueError as exc:
            raise GatewayError(502, "upstream_error", "OpenRouter ответил не JSON") from exc
        if not isinstance(data, dict) or data.get("error"):
            raise _upstream_error(502, json.dumps(data, ensure_ascii=False)[:1000])
    except GatewayError as exc:
        record(ctx, plan, ms=_ms(started), error=exc)
        raise
    record(ctx, plan, data.get("usage"), served=str(data.get("model") or ""), ms=_ms(started),
           answer=trace.answer_of(data) if plan.debug is not None else None)
    return data


def _open_stream(plan: Plan):
    url = f"{config.OPENROUTER_BASE_URL}/chat/completions"
    try:
        client, resp = _open(url, plan.out, _headers())
    except httpx.HTTPError as exc:
        raise GatewayError(502, "upstream_error", f"OpenRouter недоступен: {exc}") from exc
    if resp.status_code == 200:
        return client, resp
    try:
        text = resp.read().decode("utf-8", errors="replace")
    finally:
        resp.close()
        client.close()
    if _jev_failed(plan, resp.status_code, text):
        return _open_stream(plan)  # once: after falling back the request is not Jev's any more
    raise _upstream_error(resp.status_code, text)


def stream(ctx: AgentCtx, body, project: str = "", skill: str = "", task: str = ""):
    """Server-sent events passed through as they come; the usage of the last chunk is recorded at the end."""
    plan = draft(body, project, skill, task, stream=True)
    started = time.monotonic()
    try:
        prepare(ctx, body, plan)
        client, resp = _open_stream(plan)
    except GatewayError as exc:
        record(ctx, plan, ms=_ms(started), error=exc)
        raise

    def relay():
        usage, served, error = None, "", None
        answer = trace.StreamedAnswer() if plan.debug is not None else None
        try:
            for line in resp.iter_lines():
                if line.startswith("data:"):
                    data = line[5:].strip()
                    if data and data != "[DONE]":
                        try:
                            chunk = json.loads(data)
                        except ValueError:
                            chunk = None
                        if isinstance(chunk, dict):
                            if isinstance(chunk.get("usage"), dict):
                                usage = chunk["usage"]
                            if not served and isinstance(chunk.get("model"), str):
                                served = chunk["model"]
                            if isinstance(chunk.get("error"), dict):
                                error = GatewayError(502, "upstream_error", str(chunk["error"].get("message") or chunk["error"]))
                            if answer is not None:
                                answer.add(chunk)
                yield (line + "\n").encode("utf-8")
        except GeneratorExit:
            error = GatewayError(499, "client_closed", "Агент не дослушал ответ")
            raise
        except Exception as exc:
            error = GatewayError(502, "stream_broken", f"Поток оборвался: {exc}")
            raise
        finally:
            resp.close()
            client.close()
            record(ctx, plan, usage, served=served, ms=_ms(started), error=error,
                   answer=answer.value() if answer is not None else None)
    return relay()


def models_list(ctx: AgentCtx) -> dict:
    rules = access.rules_for(ctx.agent_id)
    return {"object": "list", "data": [{"id": f"sysai/{t}", "object": "model", "created": 0, "owned_by": "sysai",
                                        "name": f"SYSAI · {access.TIER_RU[t]}"} for t in rules["models"]["tiers"]]}


def summary(agent_id: int | None = None, days: int = 7) -> dict:
    """Spend for the page: today, the last days, by tier, project and skill."""
    since = (dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=days - 1)).isoformat()
    with SessionLocal() as s:
        q = s.query(NetSpend).filter(NetSpend.day >= since)
        if agent_id is not None:
            q = q.filter(NetSpend.agent_id == agent_id)
        rows = q.all()
    today = _today()
    out = {"today": 0.0, "period": 0.0, "requests": len(rows), "errors": 0, "by_tier": {}, "by_project": {},
           "by_skill": {}, "by_agent": {}, "tokens": 0, "cached": 0}
    for r in rows:
        cost = r.cost or 0.0
        out["period"] += cost
        if r.day == today:
            out["today"] += cost
        out["tokens"] += (r.prompt_tokens or 0) + (r.completion_tokens or 0)
        out["cached"] += r.cached_tokens or 0
        out["errors"] += r.ok is False
        for key, value in (("by_tier", r.tier), ("by_project", r.project), ("by_skill", r.skill), ("by_agent", r.agent_id)):
            if value:
                out[key][value] = round(out[key].get(value, 0.0) + cost, 4)
    out["today"], out["period"] = round(out["today"], 4), round(out["period"], 4)
    return out
