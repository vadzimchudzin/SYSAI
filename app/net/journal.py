"""The journal of the network (ОПО-0.4): every change of agents, access and skills and every call of an agent."""
import json

from ..db import SessionLocal
from .models import NetEvent

MAX_DETAILS = 4000


def _fit(details):
    if details is None:
        return None
    text = json.dumps(details, ensure_ascii=False, default=str)
    if len(text) <= MAX_DETAILS:
        return json.loads(text)
    return {"truncated": text[:MAX_DETAILS]}


def log(action: str, target: str = "", details=None, actor: str = "", actor_kind: str = "user", agent_id=None,
        session=None):
    """The id of the event when it is written here; with a session of the caller — None (it is written on its commit)."""
    row = NetEvent(action=action[:60], target=str(target or "")[:160], details=_fit(details), actor=str(actor or "")[:64],
                   actor_kind=actor_kind, agent_id=agent_id)
    if session is not None:
        session.add(row)
        return None
    with SessionLocal() as s:
        s.add(row)
        s.commit()
        return row.id


def changes(before: dict, after: dict) -> dict:
    """«было → стало» of the keys that differ."""
    out = {}
    for key in sorted(set(before) | set(after)):
        if before.get(key) != after.get(key):
            out[key] = {"было": before.get(key), "стало": after.get(key)}
    return out


def feed(agent_id=None, limit: int = 30, prefix: str = "") -> list:
    with SessionLocal() as s:
        q = s.query(NetEvent)
        if agent_id is not None:
            q = q.filter(NetEvent.agent_id == agent_id)
        if prefix:
            q = q.filter(NetEvent.target.like(prefix + "%"))
        rows = q.order_by(NetEvent.at.desc(), NetEvent.id.desc()).limit(limit).all()
        return [{"at": r.at, "actor": r.actor, "actor_kind": r.actor_kind, "action": r.action, "target": r.target,
                 "details": r.details, "summary": summary(r.details)} for r in rows]


def summary(details, length: int = 240) -> str:
    if not details:
        return ""
    text = json.dumps(details, ensure_ascii=False, default=str)
    return text if len(text) <= length else text[:length - 1] + "…"
