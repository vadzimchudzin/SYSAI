"""Agents in SYSAI (ОПО-1.x): the registry, connecting a computer with a one-time code, the token of the agent (kept
only as a hash), sign-in by that token and switching an agent off. The agent works for its person and never gets more
rights than the person has: a disabled account disables its agents at once."""
import datetime as dt
import hashlib
import re
import secrets
from dataclasses import dataclass, field

from ..db import Department, Employee, SessionLocal, User, now
from . import journal
from .models import NetAccessProfile, NetAgent, NetAgentAccess, NetAgentToken

CODE_TTL = dt.timedelta(minutes=10)
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I: the code is typed by hand
TOKEN_PREFIX = "sya_"
NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
HARNESSES = {"hermes": "Hermes Agent", "openclaw": "OpenClaw", "other": "другая обвязка"}
STATUS_RU = {"pending": ("ждёт подключения", "amber"), "active": ("работает", "green"), "disabled": ("отключён", "red")}
SEEN_WRITE_EVERY = dt.timedelta(seconds=60)


class AgentError(Exception):
    pass


def token_hash(raw: str) -> str:
    return hashlib.sha256((raw or "").strip().encode()).hexdigest()


def normalize_code(code: str) -> str:
    raw = re.sub(r"[^A-Za-z0-9]", "", code or "").upper()
    return f"{raw[:4]}-{raw[4:]}" if len(raw) == 8 else raw


def _aware(value):
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value


def slug(text: str) -> str:
    table = str.maketrans({"а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z",
                           "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
                           "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh",
                           "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya"})
    out = re.sub(r"[^a-z0-9]+", "-", (text or "").lower().translate(table)).strip("-")
    return re.sub(r"-{2,}", "-", out)[:64].strip("-")


def suggest_name(prefix: str, username: str) -> str:
    return "-".join(p for p in (slug(prefix) or "agent", slug(username)) if p)[:64]


@dataclass
class AgentCtx:
    """The signed-in agent and its person, as seen by MCP and the gateway."""
    agent_id: int
    name: str
    user_id: int
    username: str
    role: str
    department_id: int | None
    employee_id: int | None
    token_id: int
    extra: dict = field(default_factory=dict)

    @property
    def superuser(self) -> bool:
        return self.role == "superuser"


def create(user_id: int, name: str = "", department_id=None, employee_id=None, harness: str = "hermes",
           profile_code: str = "", by: str = "") -> NetAgent:
    from . import access
    harness = harness if harness in HARNESSES else "other"
    with SessionLocal() as s:
        user = s.get(User, user_id)
        if user is None or user.status != "approved":
            raise AgentError("Агента можно создать только для действующего аккаунта SYSAI")
        dept = s.get(Department, department_id) if department_id else None
        if department_id and dept is None:
            raise AgentError("Нет такого отдела")
        if employee_id and s.get(Employee, employee_id) is None:
            raise AgentError("Нет такого сотрудника")
        name = (name or "").strip().lower() or suggest_name(slug(dept.name)[:12] if dept else "agent", user.username)
        if not NAME_RE.fullmatch(name) or len(name) > 64:
            raise AgentError("Имя агента: латиница в нижнем регистре, цифры и дефисы, например opo-ivanov")
        if s.query(NetAgent).filter(NetAgent.name == name).first():
            raise AgentError(f"Агент {name} уже есть")
        agent = NetAgent(name=name, user_id=user.id, department_id=dept.id if dept else None, employee_id=employee_id or None,
                         harness=harness, status="pending", created_by=by)
        s.add(agent)
        s.flush()
        access.ensure_builtin(s)
        code = profile_code or access.default_profile_code(name)
        profile = s.query(NetAccessProfile).filter(NetAccessProfile.code == code).first()
        s.add(NetAgentAccess(agent_id=agent.id, profile_id=profile.id if profile else None, overrides={}, updated_by=by))
        journal.log("agent.create", name, {"человек": user.username, "отдел": dept.name if dept else "",
                                           "обвязка": harness, "профиль": profile.name if profile else ""},
                    actor=by, agent_id=agent.id, session=s)
        s.commit()
        return agent


def issue_code(agent_id: int, by: str = "") -> str:
    """A new one-time code (10 minutes). Earlier unused codes of this agent stop working."""
    code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
    code = f"{code[:4]}-{code[4:]}"
    with SessionLocal() as s:
        agent = s.get(NetAgent, agent_id)
        if agent is None:
            raise AgentError("Нет такого агента")
        if agent.status == "disabled":
            raise AgentError("Агент отключён: сначала включите его")
        stamp = now()
        for row in s.query(NetAgentToken).filter(NetAgentToken.agent_id == agent_id, NetAgentToken.kind == "connect",
                                                 NetAgentToken.revoked_at.is_(None)):
            row.revoked_at = stamp
        s.add(NetAgentToken(agent_id=agent_id, kind="connect", token_hash=token_hash(code), hint=code[-4:],
                            expires_at=stamp + CODE_TTL))
        journal.log("agent.code", agent.name, {"действует до": (stamp + CODE_TTL).isoformat(timespec="minutes")},
                    actor=by, agent_id=agent.id, session=s)
        s.commit()
    return code


def exchange(code: str, host: str = "", harness: str = "", versions=None, ip: str = "") -> tuple[NetAgent, str]:
    """The installer trades the code for the agent's token. The code works once; the new computer replaces the old
    one (the previous token stops working)."""
    normalized = normalize_code(code)
    if not normalized:
        raise AgentError("Нет кода подключения")
    with SessionLocal() as s:
        row = s.query(NetAgentToken).filter(NetAgentToken.token_hash == token_hash(normalized),
                                            NetAgentToken.kind == "connect").first()
        stamp = now()
        if row is None or row.revoked_at is not None:
            raise AgentError("Код не подходит или уже использован: получите новый на странице «Агенты»")
        if _aware(row.expires_at) is not None and _aware(row.expires_at) < stamp:
            raise AgentError("Код просрочен (действует 10 минут): получите новый на странице «Агенты»")
        agent = s.get(NetAgent, row.agent_id)
        user = s.get(User, agent.user_id) if agent else None
        if agent is None or agent.status == "disabled" or user is None or user.status != "approved":
            raise AgentError("Агент отключён или его аккаунт не действует")
        row.revoked_at = stamp
        row.last_used_at = stamp
        row.used_from = (ip or "")[:64]
        for old in s.query(NetAgentToken).filter(NetAgentToken.agent_id == agent.id, NetAgentToken.kind == "agent",
                                                 NetAgentToken.revoked_at.is_(None)):
            old.revoked_at = stamp
        raw = TOKEN_PREFIX + secrets.token_urlsafe(32)
        s.add(NetAgentToken(agent_id=agent.id, kind="agent", token_hash=token_hash(raw), hint=raw[-4:], used_from=(ip or "")[:64]))
        agent.status = "active"
        agent.host = (host or agent.host or "")[:120]
        if harness in HARNESSES:
            agent.harness = harness
        if isinstance(versions, dict):
            agent.versions = {str(k)[:40]: str(v)[:40] for k, v in list(versions.items())[:20]}
        agent.last_seen_at = stamp
        agent.last_error = ""
        journal.log("agent.connect", agent.name, {"компьютер": agent.host, "адрес": ip}, actor=agent.name,
                    actor_kind="agent", agent_id=agent.id, session=s)
        s.commit()
        return agent, raw


def bearer(headers) -> str:
    value = headers.get("authorization") or ""
    if value[:7].lower() == "bearer ":
        return value[7:].strip()
    return (headers.get("x-sysai-token") or "").strip()


def authenticate(raw: str, ip: str = "") -> AgentCtx | None:
    if not raw or not raw.startswith(TOKEN_PREFIX) or len(raw) > 200:
        return None
    with SessionLocal() as s:
        row = s.query(NetAgentToken).filter(NetAgentToken.token_hash == token_hash(raw), NetAgentToken.kind == "agent").first()
        if row is None or row.revoked_at is not None:
            return None
        stamp = now()
        if _aware(row.expires_at) is not None and _aware(row.expires_at) < stamp:
            return None
        agent = s.get(NetAgent, row.agent_id)
        if agent is None or agent.status != "active":
            return None
        user = s.get(User, agent.user_id)
        if user is None or user.status != "approved":  # the person lost access: so did the agent
            return None
        last = _aware(row.last_used_at)
        if last is None or stamp - last > SEEN_WRITE_EVERY:
            row.last_used_at = stamp
            row.used_from = (ip or row.used_from or "")[:64]
            agent.last_seen_at = stamp
            s.commit()
        return AgentCtx(agent_id=agent.id, name=agent.name, user_id=user.id, username=user.username, role=user.role,
                        department_id=agent.department_id, employee_id=agent.employee_id, token_id=row.id)


def revoke(agent_id: int, by: str = "") -> None:
    """Switch the agent off: every token stops working at once (MCP and the gateway refuse the next call)."""
    with SessionLocal() as s:
        agent = s.get(NetAgent, agent_id)
        if agent is None:
            raise AgentError("Нет такого агента")
        stamp = now()
        count = 0
        for row in s.query(NetAgentToken).filter(NetAgentToken.agent_id == agent_id, NetAgentToken.revoked_at.is_(None)):
            row.revoked_at = stamp
            count += 1
        before = agent.status
        agent.status = "disabled"
        journal.log("agent.disable", agent.name, {"статус": {"было": before, "стало": "disabled"}, "токенов отозвано": count},
                    actor=by, agent_id=agent.id, session=s)
        s.commit()


def enable(agent_id: int, by: str = "") -> None:
    """Back on, but only through a new connect code: old tokens stay revoked."""
    with SessionLocal() as s:
        agent = s.get(NetAgent, agent_id)
        if agent is None:
            raise AgentError("Нет такого агента")
        agent.status = "pending"
        journal.log("agent.enable", agent.name, {"статус": {"было": "disabled", "стало": "pending"}}, actor=by,
                    agent_id=agent.id, session=s)
        s.commit()


def delete(agent_id: int, by: str = "") -> None:
    with SessionLocal() as s:
        agent = s.get(NetAgent, agent_id)
        if agent is None:
            return
        name = agent.name
        s.query(NetAgentToken).filter(NetAgentToken.agent_id == agent_id).delete()
        s.query(NetAgentAccess).filter(NetAgentAccess.agent_id == agent_id).delete()
        s.delete(agent)
        journal.log("agent.delete", name, None, actor=by, session=s)
        s.commit()


def heartbeat(ctx: AgentCtx, data: dict) -> dict:
    """The pulse of the synchronizer: what it applied, versions, skills on the computer, the last error."""
    from . import access, hub
    data = data if isinstance(data, dict) else {}
    with SessionLocal() as s:
        agent = s.get(NetAgent, ctx.agent_id)
        stamp = now()
        agent.last_seen_at = stamp
        if isinstance(data.get("versions"), dict):
            agent.versions = {str(k)[:40]: str(v)[:40] for k, v in list(data["versions"].items())[:20]}
        if data.get("host"):
            agent.host = str(data["host"])[:120]
        if "error" in data:
            agent.last_error = str(data.get("error") or "")[:2000]
        hub_state = data.get("hub")
        if isinstance(hub_state, dict):
            skills = hub_state.get("skills") if isinstance(hub_state.get("skills"), dict) else {}
            conflicts = hub_state.get("conflicts") if isinstance(hub_state.get("conflicts"), list) else []
            agent.hub_state = {"skills": {str(k)[:64]: str(v)[:20] for k, v in list(skills.items())[:500]},
                               "conflicts": [str(c)[:64] for c in conflicts[:100]]}
        applied = str(data.get("access_rev") or "")[:16]
        row = s.query(NetAgentAccess).filter(NetAgentAccess.agent_id == ctx.agent_id).first()
        if row is not None and applied and applied != row.applied_rev:
            journal.log("access.applied", agent.name, {"было": row.applied_rev, "стало": applied}, actor=agent.name,
                        actor_kind="agent", agent_id=agent.id, session=s)
            row.applied_rev = applied
            row.applied_at = stamp
        s.commit()
    return {"ok": True, "access_rev": access.document(ctx.agent_id)["rev"], "hub_rev": hub.manifest(ctx)["rev"],
            "server_time": now().isoformat(timespec="seconds")}


def listing(user: dict | None = None) -> list:
    """Agents with their person, department and access state; a plain user sees only his own."""
    from . import access
    with SessionLocal() as s:
        q = s.query(NetAgent)
        if user is not None and user.get("role") != "superuser":
            q = q.filter(NetAgent.user_id == user["id"])
        agents = q.order_by(NetAgent.name).all()
        users = {u.id: u for u in s.query(User).filter(User.id.in_([a.user_id for a in agents] or [0]))}
        depts = {d.id: d.name for d in s.query(Department).all()}
        out = []
        for a in agents:
            doc = access.document(a.id, session=s)
            row = s.query(NetAgentAccess).filter(NetAgentAccess.agent_id == a.id).first()
            u = users.get(a.user_id)
            out.append({"id": a.id, "name": a.name, "status": a.status, "status_ru": STATUS_RU.get(a.status, (a.status, "gray")),
                        "user": u.username if u else "—", "user_name": (u.display_name or u.username) if u else "—",
                        "department": depts.get(a.department_id, ""), "harness": HARNESSES.get(a.harness, a.harness),
                        "host": a.host, "versions": a.versions or {}, "last_seen_at": a.last_seen_at,
                        "last_error": a.last_error, "profile": doc["profile"]["name"], "rev": doc["rev"],
                        "applied_rev": row.applied_rev if row else "", "applied_at": row.applied_at if row else None})
        return out
