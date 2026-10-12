"""OpenRouter client (OpenAI-compatible chat completions)."""
import datetime as dt
import json
import logging
import re
import time

import httpx
from sqlalchemy import func

from . import config, db, settings_store

log = logging.getLogger("sysai.llm")


class LLMError(RuntimeError):
    pass


class IncompleteResponse(LLMError):
    pass


def _today() -> str:
    return dt.datetime.now(dt.timezone.utc).date().isoformat()


def spent_today() -> float:
    """Money spent on OpenRouter today (UTC), from the costs the service itself recorded."""
    with db.SessionLocal() as s:
        total = s.query(func.coalesce(func.sum(db.LlmSpend.cost), 0.0)).filter(db.LlmSpend.day == _today()).scalar()
    return float(total or 0.0)


def budget_usd() -> float:
    """Daily limit in dollars; 0 means no limit."""
    try:
        return max(0.0, float(settings_store.get("daily_budget_usd") or 0))
    except (TypeError, ValueError):
        return 0.0


def check_budget() -> None:
    limit = budget_usd()
    if limit <= 0:
        return
    spent = spent_today()
    if spent >= limit:
        raise LLMError(f"Дневной лимит расходов на ИИ исчерпан: ${spent:.2f} из ${limit:.2f}. "
                       "Новые запросы пойдут завтра (по UTC) или после увеличения лимита в настройках")


def record_cost(model: str, usage) -> None:
    cost = usage.get("cost") if isinstance(usage, dict) else None
    if not isinstance(cost, (int, float)) or isinstance(cost, bool) or not cost > 0:
        return
    try:
        with db.SessionLocal() as s:
            s.add(db.LlmSpend(day=_today(), model=str(model)[:120], cost=float(cost)))
            s.commit()
    except Exception:  # noqa: BLE001  the answer is still good when the bookkeeping fails
        log.exception("could not record the cost of %s", model)


def chat(model: str, messages: list, json_mode: bool = False, temperature: float = 0.1,
         max_tokens: int | None = None, timeout: float = 600) -> str:
    key = config.openrouter_key()
    if not key:
        raise LLMError("Ключ OpenRouter не задан (Настройки → Подключения)")
    check_budget()
    body = {"model": model, "messages": messages, "temperature": temperature}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    if max_tokens:
        body["max_tokens"] = max_tokens
    headers = {"Authorization": f"Bearer {key}", "X-Title": "SYSAI",
               "HTTP-Referer": config.PUBLIC_URL or "https://github.com/VadimChudin/SYSAI"}
    last = None
    for attempt in range(3):
        try:
            r = httpx.post(f"{config.OPENROUTER_BASE_URL}/chat/completions", json=body, headers=headers,
                           timeout=timeout)
            if r.status_code in (408, 429, 500, 502, 503, 504):
                last = f"HTTP {r.status_code}: {r.text[:300]}"
                time.sleep(5 * (attempt + 1))
                continue
            if r.status_code != 200:
                raise LLMError(f"OpenRouter HTTP {r.status_code}: {r.text[:500]}")
            data = r.json()
            if "error" in data:
                raise LLMError(f"OpenRouter: {data['error']}")
            record_cost(model, data.get("usage"))
            try:
                choice = data["choices"][0]
                reason = choice.get("finish_reason")
                content = choice["message"]["content"]
            except (KeyError, IndexError, TypeError):
                raise LLMError("OpenRouter вернул ответ без результата модели") from None
            if reason == "length":
                raise IncompleteResponse("Ответ модели обрезан по лимиту — требуется меньший фрагмент")
            if reason != "stop":
                raise LLMError(f"Модель не завершила ответ: {reason}")
            if not isinstance(content, str) or not content.strip():
                raise LLMError("Модель вернула пустой ответ")
            return content
        except httpx.HTTPError as e:
            last = str(e)
            time.sleep(5 * (attempt + 1))
    raise LLMError(f"OpenRouter недоступен: {last}")


def parse_json(text: str):
    """Tolerant JSON extraction: strips ```json fences and leading/trailing prose."""
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.S)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    for open_c, close_c in (("{", "}"), ("[", "]")):
        a, b = t.find(open_c), t.rfind(close_c)
        if a != -1 and b > a:
            try:
                return json.loads(t[a:b + 1])
            except json.JSONDecodeError:
                continue
    raise LLMError("Модель вернула не JSON: " + text[:300])


def check_key() -> str:
    """Validates the OpenRouter key; returns a short human description."""
    key = config.openrouter_key()
    if not key:
        raise LLMError("Ключ не задан")
    r = httpx.get(f"{config.OPENROUTER_BASE_URL}/key", headers={"Authorization": f"Bearer {key}"}, timeout=20)
    if r.status_code != 200:
        raise LLMError(f"OpenRouter отклонил ключ (HTTP {r.status_code})")
    d = r.json().get("data", {})
    usage, limit = d.get("usage"), d.get("limit")
    return "ключ принят" + (f", израсходовано ${usage:.2f}" if isinstance(usage, (int, float)) else "") + \
        (f" из ${limit:.2f}" if isinstance(limit, (int, float)) else "")
