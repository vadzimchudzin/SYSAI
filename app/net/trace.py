"""The trace of an agent (ОПО-5.7): its steps in one feed — requests to the models through the gateway (net_spend:
tier, model, who asked, tokens, cost, time, how it ended) and its calls of MCP and other actions (net_events).
Steps go together into runs: one task of the board (X-SYSAI-Task) or, without it, steps less than 15 minutes apart.

The debug mode of an agent (24 hours, then it turns itself off) also keeps the texts: the end of every request and
the answer of the model, the arguments and the result of every MCP call (net_debug, kept 7 days)."""
import datetime as dt
import hashlib
import json
import re
import threading
import time

from ..db import SessionLocal
from .models import NetAgent, NetDebug, NetEvent, NetSpend

DEBUG_HOURS = 24
DEBUG_KEEP_DAYS = 7
GAP = dt.timedelta(minutes=15)
TEXT = 24_000          # characters of one text kept in the debug mode
ROLES = ("workers", "compression", "vision")
ROLE_RU = {"": "главная", "workers": "исполнитель", "compression": "сжатие", "vision": "зрение"}
EVENT_RU = {"agent.connect": "подключился", "access.applied": "применил доступы", "hub.take": "взял из хаба",
            "hub.drop": "отдал хабу", "hub.propose": "предложил в хаб", "files.put": "записал файл",
            "files.mkdir": "создал папку", "files.delete": "удалил в корзину", "files.restore": "вернул из корзины",
            "files.purge": "стёр насовсем"}
_purged = {"at": 0.0}
_lock = threading.Lock()


def aware(value):
    """SQLite gives the moments back without the zone: they are UTC."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


# ----------------------------------------------------------------------------- the debug mode
def debug_until(agent_id: int, session=None):
    """The moment the debug mode of the agent ends, or None when it is off."""
    own = session is None
    s = session or SessionLocal()
    try:
        agent = s.get(NetAgent, agent_id)
        until = aware(agent.debug_until) if agent is not None else None
    finally:
        if own:
            s.close()
    return until if until and until > _now() else None


def debug_on(agent_id: int, session=None) -> bool:
    return debug_until(agent_id, session) is not None


def set_debug(agent_id: int, on: bool, by: str) -> None:
    from . import journal
    with SessionLocal() as s:
        agent = s.get(NetAgent, agent_id)
        if agent is None:
            return
        agent.debug_until = _now() + dt.timedelta(hours=DEBUG_HOURS) if on else None
        agent.debug_by = by[:64] if on else ""
        journal.log("agent.debug", agent.name, {"режим отладки": f"включён на {DEBUG_HOURS} ч" if on else "выключен"},
                    actor=by, agent_id=agent.id, session=s)
        s.commit()


def clip(value, limit: int = TEXT) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + f" … (ещё {len(text) - limit} знаков)"


def save(agent_id: int, kind: str, ref_id, request=None, response=None, session=None) -> None:
    """Texts of one step; only while the debug mode is on (the caller checks)."""
    if ref_id is None:
        return
    row = NetDebug(agent_id=agent_id, kind=kind, ref_id=int(ref_id), request=request, response=response)
    if session is not None:  # the caller commits it and purges after (SQLite has one writer at a time)
        session.add(row)
        return
    with SessionLocal() as s:
        s.add(row)
        s.commit()
    purge()


def purge(force: bool = False) -> int:
    """Texts older than 7 days go away; checked at most once an hour."""
    stamp = time.time()
    with _lock:
        if not force and stamp - _purged["at"] < 3600:
            return 0
        _purged["at"] = stamp
    cutoff = _now() - dt.timedelta(days=DEBUG_KEEP_DAYS)
    with SessionLocal() as s:
        gone = s.query(NetDebug).filter(NetDebug.at < cutoff).delete(synchronize_session=False)
        s.commit()
    return gone


def text_of(content) -> str:
    """The text of a chat message: a string or parts (images and files are named, not kept)."""
    if isinstance(content, str):
        return content
    out = []
    for part in content or [] if isinstance(content, list) else []:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text":
            out.append(str(part.get("text") or ""))
        else:
            out.append(f"[{part.get('type') or 'вложение'}]")
    return "\n".join(out)


def request_excerpt(body: dict) -> dict:
    """The end of a model request: the last messages (what the model saw last), the start of the system prompt, the
    names of the tools and the settings. The whole conversation goes again in every request; its end is the news."""
    messages = [m for m in body.get("messages") or [] if isinstance(m, dict)]
    tail, size = [], 0
    for m in reversed(messages):
        item = {"role": str(m.get("role") or "")}
        if m.get("role") != "system":
            item["content"] = clip(text_of(m.get("content")), 8000)
        calls = [c for c in m.get("tool_calls") or [] if isinstance(c, dict)]
        if calls:
            item["tool_calls"] = [{"name": str((c.get("function") or {}).get("name") or ""),
                                   "arguments": clip(str((c.get("function") or {}).get("arguments") or ""), 4000)}
                                  for c in calls[:20]]
        for key in ("name", "tool_call_id"):
            if m.get(key):
                item[key] = str(m[key])[:80]
        tail.append(item)
        size += len(json.dumps(item, ensure_ascii=False))
        if len(tail) >= 8 or size >= TEXT or m.get("role") == "user":
            break
    system = next((m for m in messages if m.get("role") == "system"), None)
    system_text = text_of(system.get("content")) if system else ""
    tools = [str((t.get("function") or {}).get("name") or t.get("name") or "") for t in body.get("tools") or []
             if isinstance(t, dict)]
    return {"messages": len(messages), "tail": list(reversed(tail)), "system_chars": len(system_text),
            "system_start": clip(system_text, 4000), "tools": tools[:300],
            "settings": {k: body[k] for k in ("temperature", "max_tokens", "reasoning_effort", "reasoning", "tool_choice")
                         if k in body}}


# ----------------------------------------------------------------------------- what a request is made of
PROJECT_LINE = re.compile(r"Папка проекта:[ \t]*([^\r\n]+)")  # the window's requests of a project (lib/projects.js)
PRICE_STEP = 272_000   # prompt tokens from which the big models cost twice as much (OpenRouter, 11.2026)
IMAGE_PARTS = ("image_url", "input_image", "image")


def _chars(content) -> tuple[int, int]:
    """(characters of the text, pictures) of the content of a message."""
    if isinstance(content, str):
        return len(content), 0
    size = images = 0
    for part in content if isinstance(content, list) else []:
        if not isinstance(part, dict):
            continue
        if part.get("type") in IMAGE_PARTS:
            images += 1
        elif isinstance(part.get("text"), str):
            size += len(part["text"])
    return size, images


def anatomy(body) -> dict:
    """The parts of a model request by size, in characters: the definitions of the tools, the system prompt, the
    conversation (with the results of the tools) and the results the last step brought; pictures; which request of
    the conversation it is; the conversation's mark; the project the window named in its first message. Every request
    carries the whole conversation again, so this is where its tokens come from."""
    body = body if isinstance(body, dict) else {}
    messages = [m for m in body.get("messages") or [] if isinstance(m, dict)] if isinstance(body.get("messages"), list) else []
    tools = [t for t in body.get("tools") or [] if isinstance(t, dict)] if isinstance(body.get("tools"), list) else []
    names = [str((t.get("function") or {}).get("name") or t.get("name") or "") for t in tools]
    out = {"n_msgs": len(messages), "n_tools": len(tools), "system_chars": 0, "history_chars": 0, "last_result_chars": 0,
           "tools_chars": len(json.dumps(tools, ensure_ascii=False)) if tools else 0, "last_result_tool": "", "n_images": 0,
           "turn": 1, "conv": "", "project": "", "tool_mode": "bridge" if "tool_search" in names else "eager" if tools else ""}
    called, first_user, system_start, tail = {}, None, "", []
    for m in messages:
        role = m.get("role")
        size, images = _chars(m.get("content"))
        out["n_images"] += images
        if role == "system":
            out["system_chars"] += size
            system_start = system_start or text_of(m.get("content"))[:200]
            continue
        for call in m.get("tool_calls") or [] if isinstance(m.get("tool_calls"), list) else []:
            fn = call.get("function") if isinstance(call, dict) and isinstance(call.get("function"), dict) else {}
            size += len(str(fn.get("name") or "")) + len(str(fn.get("arguments") or ""))
            if isinstance(call, dict) and call.get("id"):
                called[str(call["id"])] = str(fn.get("name") or "")
        out["history_chars"] += size
        if role == "assistant":
            out["turn"] += 1
        if role == "user" and first_user is None:
            first_user = text_of(m.get("content"))
        if role == "tool":
            tail.append((size, str(m.get("name") or called.get(str(m.get("tool_call_id") or ""), ""))))
        else:
            tail = []
    if tail:  # the conversation ends with the results of tools: what the last step added
        out["last_result_chars"] = sum(size for size, _ in tail)
        out["last_result_tool"] = max(tail)[1][:64]
    if first_user is not None:
        out["conv"] = hashlib.sha1((system_start + "\n" + first_user[:2000]).encode("utf-8", "replace")).hexdigest()[:10]
        found = PROJECT_LINE.search(first_user[:4000])
        if found:
            out["project"] = re.split(r"[\\/]+", found.group(1).strip().rstrip("\\/"))[-1].strip()[:64]
    return out


def anatomy_columns(a, prompt_tokens: int = 0) -> dict:
    """The columns of net_spend for the anatomy of a request. Tokens of the parts are estimates: the prompt tokens the
    provider counted, shared by the characters of the parts (pictures are not in the shares)."""
    if not isinstance(a, dict):
        return {}
    total = a["tools_chars"] + a["system_chars"] + a["history_chars"]

    def share(chars):
        return int(round(prompt_tokens * chars / total)) if total and prompt_tokens > 0 else 0
    return {"n_msgs": a["n_msgs"], "n_tools": a["n_tools"], "tok_tools": share(a["tools_chars"]),
            "tok_system": share(a["system_chars"]), "tok_history": share(a["history_chars"]),
            "tok_last_result": share(a["last_result_chars"]), "last_result_tool": a["last_result_tool"],
            "n_images": a["n_images"], "turn": a["turn"], "conv": a["conv"]}


def answer_of(data: dict) -> dict:
    """The answer of a model (not streamed)."""
    choice = ((data.get("choices") or [{}])[0] if isinstance(data.get("choices"), list) else {}) or {}
    message = choice.get("message") if isinstance(choice, dict) else None
    message = message if isinstance(message, dict) else {}
    return {"content": clip(text_of(message.get("content"))),
            "tool_calls": [{"name": str((c.get("function") or {}).get("name") or ""),
                            "arguments": clip(str((c.get("function") or {}).get("arguments") or ""), 8000)}
                           for c in message.get("tool_calls") or [] if isinstance(c, dict)][:20],
            "finish_reason": str(choice.get("finish_reason") or "") if isinstance(choice, dict) else ""}


class StreamedAnswer:
    """The answer of a streamed request, put together from the chunks."""

    def __init__(self):
        self.parts, self.size, self.calls, self.finish = [], 0, {}, ""

    def add(self, chunk: dict) -> None:
        for choice in chunk.get("choices") or [] if isinstance(chunk.get("choices"), list) else []:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
            text = delta.get("content")
            if isinstance(text, str) and self.size < TEXT:
                self.parts.append(text)
                self.size += len(text)
            for call in delta.get("tool_calls") or [] if isinstance(delta.get("tool_calls"), list) else []:
                if not isinstance(call, dict):
                    continue
                fn = call.get("function") if isinstance(call.get("function"), dict) else {}
                item = self.calls.setdefault(call.get("index", len(self.calls)), {"name": "", "arguments": ""})
                item["name"] += str(fn.get("name") or "")
                if len(item["arguments"]) < 8000:
                    item["arguments"] += str(fn.get("arguments") or "")
            if choice.get("finish_reason"):
                self.finish = str(choice["finish_reason"])

    def value(self) -> dict:
        return {"content": clip("".join(self.parts)), "tool_calls": list(self.calls.values())[:20],
                "finish_reason": self.finish}


# ----------------------------------------------------------------------------- the feed
def _model_step(r: NetSpend, texts: dict) -> dict:
    tokens = (r.prompt_tokens or 0) + (r.completion_tokens or 0)
    title = f"sysai/{r.tier}" if r.tier else "модель"
    return {"at": aware(r.at), "kind": "model", "ok": r.ok is not False, "title": title, "model": r.model or "",
            "role": ROLE_RU.get(r.role or "", r.role or ""), "route": r.route or "", "tokens": tokens,
            "prompt": r.prompt_tokens or 0, "completion": r.completion_tokens or 0, "cached": r.cached_tokens or 0,
            "cost": r.cost or 0.0, "ms": r.ms or 0, "error": r.error or "", "task": r.task or "",
            "project": r.project or "", "skill": r.skill or "", "detail": "", "text": texts.get(("model", r.id)),
            "anatomy": _anatomy_step(r)}


def _anatomy_step(r: NetSpend):
    """What the request was made of, for the «Подробности» of a step; None for the rows written before it was kept."""
    if not r.n_msgs:
        return None
    return {"turn": r.turn or 0, "conv": r.conv or "", "tools": r.tok_tools or 0, "n_tools": r.n_tools or 0,
            "system": r.tok_system or 0, "history": r.tok_history or 0, "last": r.tok_last_result or 0,
            "last_tool": r.last_result_tool or "", "images": r.n_images or 0}


def _event_step(e: NetEvent, texts: dict) -> dict:
    d = e.details if isinstance(e.details, dict) else {}
    action = e.action or ""
    if action == "mcp.denied":
        kind, title, ok, error = "mcp", e.target or "MCP", False, "нет в доступах агента"
    elif action.startswith("mcp."):
        kind, title, ok, error = "mcp", action[4:], d.get("ok", True) is not False, str(d.get("error") or "")
    else:
        title = EVENT_RU.get(action) or ("журнал: " + action[6:] if action.startswith("agent:") else action)
        kind, ok, error = "event", True, ""
    args = d.get("args") if isinstance(d.get("args"), dict) else {}
    detail = ", ".join(f"{k}={v}" for k, v in args.items()) if args else (e.target or "")
    if kind == "event" and not args:
        rest = {k: v for k, v in d.items() if k not in ("task",)}
        detail = (e.target or "") + (" " + json.dumps(rest, ensure_ascii=False, default=str) if rest else "")
    return {"at": aware(e.at), "kind": kind, "ok": ok, "title": title, "model": "", "role": "", "route": "", "tokens": 0,
            "prompt": 0, "completion": 0, "cached": 0, "cost": 0.0, "ms": int(d.get("ms") or 0), "error": error, "anatomy": None,
            "task": str(d.get("task") or ""), "project": "", "skill": "", "detail": clip(detail.strip(), 300),
            "text": texts.get(("mcp", e.id))}


def runs(agent_id: int, days: int = 3, limit: int = 3000) -> list:
    """The runs of the last days, the newest first; in a run its steps in order and what it took."""
    since = _now() - dt.timedelta(days=max(1, min(days, DEBUG_KEEP_DAYS)))
    with SessionLocal() as s:
        spends = (s.query(NetSpend).filter(NetSpend.agent_id == agent_id, NetSpend.at >= since)
                  .order_by(NetSpend.at.desc(), NetSpend.id.desc()).limit(limit).all())
        events = (s.query(NetEvent).filter(NetEvent.agent_id == agent_id, NetEvent.actor_kind == "agent",
                                           NetEvent.at >= since)
                  .order_by(NetEvent.at.desc(), NetEvent.id.desc()).limit(limit).all())
        texts = {(kind, ref): did for did, kind, ref in s.query(NetDebug.id, NetDebug.kind, NetDebug.ref_id)
                 .filter(NetDebug.agent_id == agent_id, NetDebug.at >= since)}
    steps = [_model_step(r, texts) for r in spends] + [_event_step(e, texts) for e in events]
    steps.sort(key=lambda x: x["at"])
    out = []
    for step in steps:
        run = out[-1] if out else None
        if run is None or step["at"] - run["end"] > GAP or (step["task"] and run["task"] and step["task"] != run["task"]):
            run = {"start": step["at"], "end": step["at"], "task": "", "steps": [], "models": 0, "mcp": 0, "errors": 0,
                   "tokens": 0, "cached": 0, "prompt": 0, "cost": 0.0, "model_ms": 0, "by_role": {}, "first_error": "",
                   "peak": 0, "over": 0}
            out.append(run)
        run["end"] = step["at"]
        run["task"] = run["task"] or step["task"]
        run["steps"].append(step)
        if step["kind"] == "model":
            run["models"] += 1
            run["tokens"] += step["tokens"]
            run["prompt"] += step["prompt"]
            run["cached"] += step["cached"]
            run["cost"] += step["cost"]
            run["model_ms"] += step["ms"]
            run["peak"] = max(run["peak"], step["prompt"])
            run["over"] += step["prompt"] >= PRICE_STEP
            role = step["role"] or "главная"
            run["by_role"][role] = run["by_role"].get(role, 0) + step["tokens"]
        elif step["kind"] == "mcp":
            run["mcp"] += 1
        if not step["ok"]:
            run["errors"] += 1
            run["first_error"] = run["first_error"] or f"{step['title']}: {step['error']}"
    for run in out:
        run["minutes"] = max(1, round((run["end"] - run["start"]).total_seconds() / 60))
        run["cache_pct"] = round(100 * run["cached"] / run["prompt"]) if run["prompt"] else 0
        run["cost"] = round(run["cost"], 4)
    out.reverse()
    return out


def text(agent_id: int, text_id: int):
    with SessionLocal() as s:
        row = s.get(NetDebug, text_id)
        if row is None or row.agent_id != agent_id:
            return None
        return {"id": row.id, "at": aware(row.at), "kind": row.kind, "request": row.request, "response": row.response}


# ----------------------------------------------------------------------------- for the pages
def tokens(n) -> str:
    n = int(n or 0)
    if n < 10_000:
        return f"{n:,}".replace(",", " ")
    if n < 1_000_000:
        return f"{n / 1000:.1f}".replace(".", ",").replace(",0", "") + " тыс."
    return f"{n / 1_000_000:.2f}".replace(".", ",") + " млн"


def duration(ms) -> str:
    ms = int(ms or 0)
    if ms < 1000:
        return f"{ms} мс"
    if ms < 60_000:
        return f"{ms / 1000:.1f}".replace(".", ",") + " с"
    return f"{ms // 60_000} мин {ms % 60_000 // 1000} с"
