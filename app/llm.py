"""OpenRouter client (OpenAI-compatible chat completions)."""
import base64
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


def transcribe_audio(audio_bytes: bytes, model: str, audio_format: str = "mp3", language: str = "ru",
                    timeout: float = 60) -> dict:
    """Call OpenRouter's dedicated speech-to-text endpoint and preserve provider timestamps when available."""
    key = config.openrouter_key()
    if not key:
        raise LLMError("Ключ OpenRouter не задан")
    if not isinstance(audio_bytes, bytes) or not audio_bytes:
        raise LLMError("Аудиофрагмент пуст")
    audio_format = str(audio_format or "").lower().lstrip(".")
    if audio_format not in {"wav", "mp3", "flac", "m4a", "ogg", "webm", "aac"}:
        raise LLMError(f"Неподдерживаемый формат аудио: {audio_format}")
    check_budget()
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
               "X-Title": "SYSAI",
               "HTTP-Referer": config.PUBLIC_URL or "https://github.com/VadimChudin/SYSAI"}
    base = {"model": model, "input_audio": {"data": base64.b64encode(audio_bytes).decode("ascii"),
                                             "format": audio_format}, "language": language}
    variants = [
        {**base, "response_format": "verbose_json", "timestamp_granularities": ["segment"]},
        {**base, "response_format": "verbose_json"},
        {**base, "response_format": "json"},
    ]
    last_error = "неизвестная ошибка"
    for index, body in enumerate(variants):
        retryable_error = None
        for attempt in range(2):
            try:
                response = httpx.post(f"{config.OPENROUTER_BASE_URL}/audio/transcriptions", json=body,
                                      headers=headers, timeout=timeout)
                if response.status_code == 400 and index < len(variants) - 1:
                    last_error = f"HTTP 400: {response.text[:300]}"
                    break  # try a less provider-specific response format
                if response.status_code in (408, 429, 500, 502, 503, 504):
                    retryable_error = f"HTTP {response.status_code}: {response.text[:300]}"
                    if attempt == 0:
                        time.sleep(2)
                        continue
                    raise LLMError(f"OpenRouter STT недоступен: {retryable_error}")
                if response.status_code != 200:
                    raise LLMError(f"OpenRouter STT HTTP {response.status_code}: {response.text[:500]}")
                try:
                    data = response.json()
                except ValueError:
                    raise LLMError("OpenRouter STT вернул некорректный JSON") from None
                if not isinstance(data, dict) or data.get("error"):
                    raise LLMError(f"OpenRouter STT: {str(data.get('error') if isinstance(data, dict) else data)[:400]}")
                if not isinstance(data.get("text"), str):
                    raise LLMError("OpenRouter STT вернул ответ без текста")
                record_cost(model, data.get("usage"))
                return data
            except httpx.TimeoutException as exc:
                last_error = str(exc) or "timeout"
                if attempt == 0:
                    continue
                raise IncompleteResponse("STT не успел обработать фрагмент; нужно разделить аудио на меньшие части") from exc
            except httpx.HTTPError as exc:
                last_error = str(exc)
                if attempt == 0:
                    continue
                raise LLMError(f"OpenRouter STT недоступен: {last_error}") from exc
        else:
            if retryable_error:
                raise LLMError(f"OpenRouter STT недоступен: {retryable_error}")
    raise LLMError(f"OpenRouter STT не принял формат ответа: {last_error}")


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
