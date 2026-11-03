"""The model gateway (ОПО-5.x): agents reach the models only through SYSAI — OpenAI-compatible /v1/chat/completions
and /v1/models, streaming included, signed in by the agent's token. The agent asks for a tier (sysai/cheap, mid,
strong, vision); the real model behind a tier is chosen in the settings, so changing it in SYSAI changes it for every
agent. The OpenRouter key never leaves the server. Budgets per agent per day and per project; every request is
counted in the SYSAI spend (daily budget of the company) and in net_spend with the agent, the project and the skill."""
import datetime as dt
import json
import logging

import httpx
from sqlalchemy import func

from .. import config, llm, settings_store
from ..db import SessionLocal
from . import access
from .agents import AgentCtx
from .models import NetSpend

log = logging.getLogger("sysai.net.gateway")
TIERS = access.TIERS
DEFAULT_MODELS = {"cheap": "google/gemini-2.5-flash-lite", "mid": "google/gemini-2.5-flash", "strong": "google/gemini-2.5-pro",
                  "vision": ""}
DROP = ("models", "route", "user")  # no way around the tier: no own model lists or routing


class GatewayError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message

    def body(self) -> dict:
        return {"error": {"message": self.message, "type": self.code, "code": self.code}}


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


def prepare(ctx: AgentCtx, body, stream: bool = False, project: str = "") -> tuple[str, str, dict]:
    """(tier, model, the body for OpenRouter) or GatewayError with a clear reason the agent can tell its person."""
    if not isinstance(body, dict) or not isinstance(body.get("messages"), list):
        raise GatewayError(400, "invalid_request", "Нужен JSON с model и messages")
    tier = tier_of(body.get("model"))
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
    if project and project_limit > 0:
        spent = spent_project(project)
        if spent >= project_limit:
            raise GatewayError(429, "budget_exceeded", f"Бюджет проекта {project} исчерпан: ${spent:.2f} из ${project_limit:.2f}")
    try:
        llm.check_budget()
    except llm.LLMError as exc:
        raise GatewayError(429, "budget_exceeded", str(exc)) from exc
    model = model_for(tier)
    out = {k: v for k, v in body.items() if k not in DROP}
    if rules["internet"]["mode"] != "all":
        out.pop("plugins", None)  # web search of OpenRouter is the internet too
    out["model"] = model
    out["user"] = ctx.name  # OpenRouter keeps per-user limits and abuse checks apart
    out["usage"] = {"include": True}
    if stream:
        out["stream"] = True
    else:
        out.pop("stream", None)
        out.pop("stream_options", None)
    return tier, model, out


def _headers() -> dict:
    return {"Authorization": f"Bearer {config.openrouter_key()}", "X-Title": "SYSAI agents",
            "HTTP-Referer": config.PUBLIC_URL or "https://github.com/VadimChudin/SYSAI"}


def _post(url: str, body: dict, headers: dict) -> httpx.Response:
    return httpx.post(url, json=body, headers=headers, timeout=httpx.Timeout(600.0, connect=30.0))


def _open(url: str, body: dict, headers: dict):
    client = httpx.Client(timeout=httpx.Timeout(600.0, connect=30.0))
    try:
        return client, client.send(client.build_request("POST", url, json=body, headers=headers), stream=True)
    except Exception:
        client.close()
        raise


def _upstream_error(status: int, text: str) -> GatewayError:
    try:
        message = json.loads(text).get("error", {}).get("message") or text
    except (ValueError, AttributeError):
        message = text
    message = str(message)[:500]
    if status in (401, 403):
        return GatewayError(502, "upstream_auth", "OpenRouter не принял ключ SYSAI: " + message)
    if status == 402:
        return GatewayError(402, "upstream_credits", "На счёте OpenRouter нет средств: " + message)
    if status == 429:
        return GatewayError(429, "upstream_rate_limit", "Модель перегружена, повтори позже: " + message)
    if 400 <= status < 500:
        return GatewayError(status, "upstream_rejected", message)
    return GatewayError(502, "upstream_error", f"OpenRouter HTTP {status}: {message}")


def record(ctx: AgentCtx, tier: str, model: str, usage, project: str = "", skill: str = "") -> None:
    usage = usage if isinstance(usage, dict) else {}
    llm.record_cost(model, usage)  # the company's daily budget sees agents too
    cost = usage.get("cost")
    try:
        with SessionLocal() as s:
            s.add(NetSpend(day=_today(), agent_id=ctx.agent_id, user_id=ctx.user_id, tier=tier, model=str(model)[:120],
                           project=project, skill=skill, prompt_tokens=int(usage.get("prompt_tokens") or 0),
                           completion_tokens=int(usage.get("completion_tokens") or 0),
                           cost=float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else 0.0))
            s.commit()
    except Exception:  # noqa: BLE001  the answer is still good when the bookkeeping fails
        log.exception("could not record the spend of agent %s", ctx.name)


def complete(ctx: AgentCtx, body, project: str = "", skill: str = "") -> dict:
    project, skill = _clip(project), _clip(skill)
    tier, model, out = prepare(ctx, body, stream=False, project=project)
    try:
        r = _post(f"{config.OPENROUTER_BASE_URL}/chat/completions", out, _headers())
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
    record(ctx, tier, model, data.get("usage"), project, skill)
    return data


def stream(ctx: AgentCtx, body, project: str = "", skill: str = ""):
    """Server-sent events passed through as they come; the usage of the last chunk is recorded at the end."""
    project, skill = _clip(project), _clip(skill)
    tier, model, out = prepare(ctx, body, stream=True, project=project)
    try:
        client, resp = _open(f"{config.OPENROUTER_BASE_URL}/chat/completions", out, _headers())
    except httpx.HTTPError as exc:
        raise GatewayError(502, "upstream_error", f"OpenRouter недоступен: {exc}") from exc
    if resp.status_code != 200:
        try:
            text = resp.read().decode("utf-8", errors="replace")
        finally:
            resp.close()
            client.close()
        raise _upstream_error(resp.status_code, text)

    def relay():
        usage = None
        try:
            for line in resp.iter_lines():
                if line.startswith("data:"):
                    data = line[5:].strip()
                    if data and data != "[DONE]":
                        try:
                            chunk = json.loads(data)
                        except ValueError:
                            chunk = None
                        if isinstance(chunk, dict) and isinstance(chunk.get("usage"), dict):
                            usage = chunk["usage"]
                yield (line + "\n").encode("utf-8")
        finally:
            resp.close()
            client.close()
            record(ctx, tier, model, usage, project, skill)
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
    out = {"today": 0.0, "period": 0.0, "requests": len(rows), "by_tier": {}, "by_project": {}, "by_skill": {},
           "by_agent": {}, "tokens": 0}
    for r in rows:
        cost = r.cost or 0.0
        out["period"] += cost
        if r.day == today:
            out["today"] += cost
        out["tokens"] += (r.prompt_tokens or 0) + (r.completion_tokens or 0)
        for key, value in (("by_tier", r.tier), ("by_project", r.project), ("by_skill", r.skill), ("by_agent", r.agent_id)):
            if value:
                out[key][value] = round(out[key].get(value, 0.0) + cost, 4)
    out["today"], out["period"] = round(out["today"], 4), round(out["period"], 4)
    return out
