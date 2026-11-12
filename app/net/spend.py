"""The spend report of the agents (ОПО-5.6): who spent how much and on what — by agents, projects, skills, tiers, real
models, who asked (the main model, workers, compression, vision) and days; refused and failed requests by reason;
how much came from the prompt cache. A superuser sees every agent, a person — only his own agents."""
import csv
import datetime as dt
import io

from sqlalchemy import case, func

from ..db import SessionLocal, User
from . import access, trace
from .models import NetAgent, NetSpend

PERIODS = (1, 7, 30, 90)
GROUPS = (("agent", "Агенты"), ("project", "Проекты"), ("skill", "Навыки"), ("tier", "Уровни"), ("model", "Модели"),
          ("role", "Кто спросил"))
COLUMN = {"agent": NetSpend.agent_id, "project": NetSpend.project, "skill": NetSpend.skill, "tier": NetSpend.tier,
          "model": NetSpend.model, "role": NetSpend.role}
EMPTY_RU = {"project": "без проекта", "skill": "без навыка", "model": "—", "tier": "—"}
ERROR_RU = {"budget_exceeded": "бюджет исчерпан", "model_not_allowed": "уровень не разрешён", "no_key": "нет ключа OpenRouter",
            "upstream_error": "сбой OpenRouter или модели", "upstream_rate_limit": "модель перегружена",
            "upstream_auth": "OpenRouter не принял ключ", "upstream_credits": "нет средств на OpenRouter",
            "upstream_rejected": "модель отклонила запрос", "stream_broken": "поток оборвался",
            "client_closed": "агент не дослушал", "invalid_request": "неверный запрос",
            "unauthorized": "токен агента не принят"}


# the size of a request (its prompt tokens): the whole conversation goes again in each, so the size is the cost
SIZES = ((50_000, "до 50 тыс."), (100_000, "50–100 тыс."), (200_000, "100–200 тыс."), (trace.PRICE_STEP, "200–272 тыс."),
         (None, "от 272 тыс. — цена вдвое"))


def _today() -> dt.date:
    return dt.datetime.now(dt.timezone.utc).date()


def period(days) -> int:
    try:
        days = int(days)
    except (TypeError, ValueError):
        return 7
    return days if days in PERIODS else 7


def _measures():
    return (func.coalesce(func.sum(NetSpend.cost), 0.0), func.count(NetSpend.id),
            func.coalesce(func.sum(case((NetSpend.ok.is_(False), 1), else_=0)), 0),
            func.coalesce(func.sum(NetSpend.prompt_tokens), 0), func.coalesce(func.sum(NetSpend.completion_tokens), 0),
            func.coalesce(func.sum(NetSpend.cached_tokens), 0))


def _row(key, cost, requests, errors, prompt, completion, cached) -> dict:
    prompt, completion, cached = int(prompt or 0), int(completion or 0), int(cached or 0)
    return {"key": key, "cost": round(float(cost or 0.0), 6), "requests": int(requests or 0), "errors": int(errors or 0),
            "prompt": prompt, "completion": completion, "tokens": prompt + completion, "cached": cached,
            "cache_pct": round(100.0 * cached / prompt) if prompt else 0}


def report(agent_ids=None, days: int = 7) -> dict:
    """Everything of the page for the agents given (None — every agent) over the last `days` days (UTC)."""
    days = period(days)
    today = _today()
    since = (today - dt.timedelta(days=days - 1)).isoformat()
    conds = [NetSpend.day >= since]
    if agent_ids is not None:
        conds.append(NetSpend.agent_id.in_(list(agent_ids) or [0]))
    with SessionLocal() as s:
        total = _row("", *s.query(*_measures()).filter(*conds).one())
        groups = {}
        for key, _ in GROUPS:
            col = COLUMN[key]
            rows = [_row(k if k is not None else "", *m) for k, *m in s.query(col, *_measures()).filter(*conds).group_by(col)]
            groups[key] = sorted(rows, key=lambda r: (-r["cost"], -r["requests"], str(r["key"])))
        by_day = {k: _row(k, *m) for k, *m in s.query(NetSpend.day, *_measures()).filter(*conds).group_by(NetSpend.day)}
        failures = {}
        for error, count in s.query(NetSpend.error, func.count(NetSpend.id)).filter(*conds, NetSpend.ok.is_(False)) \
                .group_by(NetSpend.error):
            code = (error or "").split(":", 1)[0].strip() or "error"
            item = failures.setdefault(code, {"code": code, "ru": ERROR_RU.get(code, code), "count": 0, "example": ""})
            item["count"] += int(count or 0)
            item["example"] = item["example"] or (error or "").split(":", 1)[-1].strip()[:200]
        bucket = case(*[(NetSpend.prompt_tokens < top, i) for i, (top, _) in enumerate(SIZES) if top], else_=len(SIZES) - 1)
        by_size = {int(k): _row(int(k), *m) for k, *m in s.query(bucket, *_measures())
                   .filter(*conds, NetSpend.prompt_tokens > 0).group_by(bucket)}
        parts = s.query(func.count(NetSpend.id), func.coalesce(func.sum(NetSpend.tok_tools), 0),
                        func.coalesce(func.sum(NetSpend.tok_system), 0), func.coalesce(func.sum(NetSpend.tok_history), 0),
                        func.coalesce(func.sum(NetSpend.tok_last_result), 0), func.coalesce(func.max(NetSpend.prompt_tokens), 0)) \
            .filter(*conds, NetSpend.n_msgs > 0, NetSpend.prompt_tokens > 0).one()
        ids = {r["key"] for r in groups["agent"] if r["key"]} | set(agent_ids or [])
        agents = {a.id: a for a in s.query(NetAgent).filter(NetAgent.id.in_(list(ids) or [0]))}
        users = {u.id: u.username for u in s.query(User).filter(User.id.in_([a.user_id for a in agents.values()] or [0]))}
        spent_today = {k: float(v or 0.0) for k, v in s.query(NetSpend.agent_id, func.coalesce(func.sum(NetSpend.cost), 0.0))
                       .filter(NetSpend.day == today.isoformat(), NetSpend.agent_id.in_(list(ids) or [0]))
                       .group_by(NetSpend.agent_id)}
    for r in groups["agent"]:
        a = agents.get(r["key"])
        r["label"] = a.name if a else "удалённый агент"
        r["owner"] = users.get(a.user_id, "") if a else ""
        r["href"] = f"/agents/{a.id}" if a else ""
        if a:
            limit = access.rules_for(a.id)["models"]["budget_day_usd"]
            r["today"], r["limit"] = round(spent_today.get(a.id, 0.0), 4), limit
            r["budget_pct"] = min(100, round(100 * spent_today.get(a.id, 0.0) / limit)) if limit else 0
    for key in ("project", "skill", "tier", "model", "role"):
        for r in groups[key]:
            if key == "role":
                r["label"] = trace.ROLE_RU.get(r["key"] or "", r["key"] or "")
            elif key == "tier" and r["key"]:
                r["label"] = f"sysai/{r['key']} — {access.TIER_RU.get(r['key'], r['key'])}"
            else:
                r["label"] = r["key"] or EMPTY_RU.get(key, "—")
    top = max((r["cost"] for rows in groups.values() for r in rows), default=0.0)
    for rows in groups.values():
        for r in rows:
            r["share"] = round(100 * r["cost"] / total["cost"]) if total["cost"] else 0
            r["bar"] = round(100 * r["cost"] / top) if top else 0
    sizes = []
    for i, (_, label) in enumerate(SIZES):
        r = by_size.get(i) or _row(i, 0, 0, 0, 0, 0, 0)
        r.update(label=label, share=round(100 * r["cost"] / total["cost"]) if total["cost"] else 0,
                 avg=round(r["prompt"] / r["requests"]) if r["requests"] else 0, over=i == len(SIZES) - 1)
        sizes.append(r)
    measured, tools, system, history, last, biggest = (int(v or 0) for v in parts)
    whole = tools + system + history
    anatomy = None
    if measured and whole:
        def pct(n):
            return round(100 * n / whole, 1)
        anatomy = {"requests": measured, "tools": round(tools / measured), "system": round(system / measured),
                   "history": round(history / measured), "last": round(last / measured), "biggest": biggest,
                   "tools_pct": pct(tools), "system_pct": pct(system), "history_pct": pct(history), "last_pct": pct(last)}
    daily = []
    for i in range(days):
        day = (today - dt.timedelta(days=days - 1 - i)).isoformat()
        daily.append(by_day.get(day) or _row(day, 0, 0, 0, 0, 0, 0))
    peak = max((d["cost"] for d in daily), default=0.0)
    for d in daily:
        d["bar"] = round(100 * d["cost"] / peak) if peak else 0
    return {"days": days, "since": since, "until": today.isoformat(), "total": total, "groups": groups, "daily": daily,
            "failures": sorted(failures.values(), key=lambda f: -f["count"]), "sizes": sizes, "anatomy": anatomy,
            "avg": round(total["cost"] / total["requests"], 6) if total["requests"] else 0.0}


def csv_rows(agent_ids=None, days: int = 7) -> str:
    """Every request of the period summed by day, agent, project, skill, tier, model and who asked — for Excel."""
    days = period(days)
    since = (_today() - dt.timedelta(days=days - 1)).isoformat()
    conds = [NetSpend.day >= since]
    if agent_ids is not None:
        conds.append(NetSpend.agent_id.in_(list(agent_ids) or [0]))
    keys = (NetSpend.day, NetSpend.agent_id, NetSpend.project, NetSpend.skill, NetSpend.tier, NetSpend.model, NetSpend.role)
    with SessionLocal() as s:
        names = {a.id: a.name for a in s.query(NetAgent)}
        rows = s.query(*keys, *_measures()).filter(*conds).group_by(*keys).order_by(NetSpend.day, NetSpend.agent_id).all()
    out = io.StringIO()
    out.write("\ufeff")  # Excel opens UTF-8 with the mark
    w = csv.writer(out, delimiter=";")
    w.writerow(["день", "агент", "проект", "навык", "уровень", "модель", "кто спросил", "запросов", "ошибок",
                "токенов на входе", "из кэша", "токенов ответа", "стоимость, $"])
    for day, agent_id, project, skill, tier, model, role, cost, requests, errors, prompt, completion, cached in rows:
        w.writerow([day, names.get(agent_id, agent_id or ""), project or "", skill or "", tier or "", model or "",
                    trace.ROLE_RU.get(role or "", role or ""), requests, errors, prompt, cached, completion,
                    f"{float(cost or 0.0):.6f}".replace(".", ",")])
    return out.getvalue()
