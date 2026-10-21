"""Narrow OpenAI-compatible model proxy: main retains its encrypted OpenRouter key.

Bounded text/tool calls; buffered OpenAI-compatible SSE when requested. No arbitrary upstream URL, model, file, image,
SQL or shell capability. Dollar spend uses existing SYSAI accounting; request count
is additionally reserved durably before the provider request, including failures.
"""
import hmac
import json
import math
import os
import time
import uuid

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from .. import config, db, llm, settings_store
from . import mcp_server, service
from .models import Event

router = APIRouter()


def validate(data):
    allowed = {"model", "messages", "stream", "stream_options", "max_tokens", "max_completion_tokens", "temperature", "tools", "tool_choice", "response_format"}
    if not isinstance(data, dict) or set(data) - allowed:
        raise HTTPException(400, "Unsupported completion fields")
    if type(data.get("stream", False)) is not bool:
        raise HTTPException(400, "stream must be boolean")
    stream_options = data.get("stream_options", {})
    if (not isinstance(stream_options, dict) or set(stream_options) - {"include_usage"} or
            type(stream_options.get("include_usage", False)) is not bool):
        raise HTTPException(400, "Unsupported stream options")
    selected = settings_store.get("agent_model")
    allowed_models = {selected, *(x.strip() for x in os.getenv("SYSAI_MCP_MODEL_ALLOWLIST", "").split(",") if x.strip())}
    model = data.get("model")
    if model == "sysai-agent":
        model = selected
    if not isinstance(model, str) or not model or model not in allowed_models:
        raise HTTPException(400, "Model is not allowlisted")
    messages = data.get("messages")
    if not isinstance(messages, list) or not 1 <= len(messages) <= 60:
        raise HTTPException(400, "Supply 1–60 bounded text messages")
    for message in messages:
        if not isinstance(message, dict) or set(message) - {"role", "content", "name", "tool_calls", "tool_call_id"}:
            raise HTTPException(400, "Unsupported message fields")
        if message.get("role") not in ("system", "developer", "user", "assistant", "tool"):
            raise HTTPException(400, "Unsupported role")
        content = message.get("content")
        if content is not None and not isinstance(content, str):
            if not isinstance(content, list) or any(not isinstance(p, dict) or set(p) != {"type", "text"} or
                    p["type"] != "text" or not isinstance(p["text"], str) for p in content):
                raise HTTPException(400, "Text-only content required")
    if len(json.dumps(messages, ensure_ascii=False)) > 64000:
        raise HTTPException(413, "Message context too large")
    tools = data.get("tools", [])
    if not isinstance(tools, list) or len(tools) > 20 or len(json.dumps(tools)) > 40000:
        raise HTTPException(400, "Tool schema limit exceeded")
    maximum = data.get("max_tokens", data.get("max_completion_tokens", 900))
    if type(maximum) is not int or not 1 <= maximum <= 2048:
        raise HTTPException(400, "Output token bound is 1–2048")
    temperature = data.get("temperature", 0.1)
    if type(temperature) not in (int, float) or not math.isfinite(temperature) or not 0 <= temperature <= 1:
        raise HTTPException(400, "Invalid temperature")
    body = {**data, "model": model, "stream": False, "max_tokens": maximum}
    body.pop("max_completion_tokens", None)
    body.pop("stream_options", None)  # upstream remains bounded NON-streaming
    return body


def buffered_sse(result, include_usage=False):
    """Convert one already-bounded completion into standard OpenAI SSE deltas.

    Upstream is never an unbounded stream. Tool calls retain index/id/name/arguments
    so OpenClaw's native openai-completions parser can reconstruct them exactly.
    """
    base = {"id": result.get("id", "sysai"), "object": "chat.completion.chunk",
            "created": result.get("created", int(time.time())), "model": result.get("model", "sysai-agent")}
    def chunk(delta, finish=None, index=0):
        return "data: " + json.dumps({**base, "choices": [{"index": index, "delta": delta,
                                                        "finish_reason": finish}]}, ensure_ascii=False) + "\n\n"
    for choice in result["choices"]:
        message = choice.get("message") or {}
        index = choice.get("index", 0)
        yield chunk({"role": "assistant"}, index=index)
        if message.get("content"):
            yield chunk({"content": message["content"]}, index=index)
        for number, tool in enumerate(message.get("tool_calls") or []):
            yield chunk({"tool_calls": [{"index": number, "id": tool["id"],
                        "type": "function", "function": tool["function"]}]}, index=index)
        yield chunk({}, choice.get("finish_reason") or "stop", index)
    if include_usage and "usage" in result:
        yield "data: " + json.dumps({**base, "choices": [], "usage": result["usage"]}) + "\n\n"
    yield "data: [DONE]\n\n"


def reserve():
    if not service.options()["enabled"] or not service.schema_ready():
        raise HTTPException(403, "Main execution pilot is disabled or migration missing")
    token = service.acquire()
    if not token:
        raise HTTPException(429, "Another bounded execution/model operation is running")
    try:
        llm.check_budget()
        midnight = db.now().replace(hour=0, minute=0, second=0, microsecond=0)
        with db.SessionLocal() as s:
            used = s.query(Event).filter(Event.kind == "model_reserved", Event.created_at >= midnight).count()
            if used >= service.options()["daily_actions"] * 3:
                raise HTTPException(429, "Daily durable model request budget exhausted")
            s.add(Event(run_id="model-proxy", kind="model_reserved", data={}, external_key="model:"+uuid.uuid4().hex))
            s.commit()
        return token
    except Exception:
        service.release(token)
        raise


@router.post("/execution/v1/chat/completions")
async def completions(request: Request):
    expected = os.getenv("SYSAI_MCP_TOKEN", "")
    supplied = request.headers.get("authorization", "")
    if len(expected) < 32 or not hmac.compare_digest(supplied, "Bearer " + expected):
        raise HTTPException(401, "Authentication required")
    try:
        mcp_server.authorize("model")
    except service.Hold:
        raise HTTPException(403, "Current service principal/model scope denied") from None
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 131072:
            raise HTTPException(413, "Request too large")
    try:
        requested = json.loads(raw)
        body = validate(requested)
        wants_stream = requested.get("stream", False)
        include_usage = (requested.get("stream_options") or {}).get("include_usage", False)
    except ValueError:
        raise HTTPException(400, "Invalid JSON") from None
    key = config.openrouter_key()
    if not key:
        raise HTTPException(503, "Main model provider is not configured")
    try:
        token = reserve()
    except llm.LLMError:
        raise HTTPException(429, "Main model spend budget exhausted") from None
    try:
        # Fixed upstream: ignores any model-supplied/provider URL.
        async with httpx.AsyncClient(timeout=20) as client:
            async with client.stream("POST", "https://openrouter.ai/api/v1/chat/completions",
                    headers={"Authorization": "Bearer "+key, "X-Title": "SYSAI scoped execution proxy"}, json=body) as response:
                if response.status_code != 200:
                    raise HTTPException(502, "Model provider rejected request")
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > 262144:
                        raise HTTPException(502, "Model provider response too large")
        result = json.loads(data)
        if not isinstance(result, dict) or not isinstance(result.get("choices"), list) or not 1 <= len(result["choices"]) <= 2:
            raise HTTPException(502, "Invalid provider completion")
        for choice in result["choices"]:
            message = choice.get("message") if isinstance(choice, dict) else None
            if not isinstance(message, dict) or (message.get("content") is not None and not isinstance(message["content"], str)):
                raise HTTPException(502, "Invalid provider message")
            tools = message.get("tool_calls") or []
            if not isinstance(tools, list) or len(tools) > 20:
                raise HTTPException(502, "Invalid provider tool calls")
            for tool in tools:
                if (not isinstance(tool, dict) or not isinstance(tool.get("id"), str) or
                        not isinstance(tool.get("function"), dict) or
                        not all(isinstance(tool["function"].get(k), str) for k in ("name", "arguments"))):
                    raise HTTPException(502, "Invalid provider tool call")
        llm.record_cost(body["model"], result.get("usage"))
        clean = {key: result[key] for key in ("id", "object", "created", "model", "choices", "usage") if key in result}
        if wants_stream:
            return StreamingResponse(buffered_sse(clean, include_usage), media_type="text/event-stream",
                                     headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
        return clean
    except (httpx.HTTPError, ValueError, TypeError):
        raise HTTPException(502, "Bounded model request failed") from None
    finally:
        service.release(token)
