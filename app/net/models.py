"""Tables of the agent network (net_*). Existing SYSAI tables are not changed: links to them are plain foreign keys
that cascade or are cleared when the account, the person or the department goes away."""
from sqlalchemy import JSON, Boolean, Column, DateTime, Float, ForeignKey, Index, Integer, LargeBinary, String, Text, UniqueConstraint
from sqlalchemy.orm import deferred

from ..db import Base, now


class NetAgent(Base):
    """One agent on one computer: opo-ivanov works for the account ivanov, from his PC, in his Hermes profile."""
    __tablename__ = "net_agents"
    id = Column(Integer, primary_key=True)
    name = Column(String(64), nullable=False, unique=True)            # also the name of the harness profile
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    employee_id = Column(Integer, ForeignKey("employees.id", ondelete="SET NULL"), nullable=True)  # Telegram of notify_me
    department_id = Column(Integer, ForeignKey("departments.id", ondelete="SET NULL"), nullable=True)
    harness = Column(String(20), default="hermes")                     # hermes | openclaw | other
    host = Column(String(120), default="")                             # the computer, as the installer reported it
    status = Column(String(12), default="pending")                     # pending | active | disabled
    versions = Column(JSON, nullable=True)                             # {"harness": "…", "sync": "…", "plc-kit": "…"}
    hub_state = Column(JSON, nullable=True)                            # {"skills": {name: version}, "conflicts": [names]}
    last_seen_at = Column(DateTime(timezone=True), nullable=True)
    last_error = Column(Text, default="")
    created_at = Column(DateTime(timezone=True), default=now)
    created_by = Column(String(64), default="")


class NetAgentToken(Base):
    """Only hashes are kept: a one-time connect code (10 minutes) and the token of the agent it is exchanged for."""
    __tablename__ = "net_agent_tokens"
    id = Column(Integer, primary_key=True)
    agent_id = Column(Integer, ForeignKey("net_agents.id", ondelete="CASCADE"), nullable=False, index=True)
    kind = Column(String(10), nullable=False, default="agent")          # connect | agent
    token_hash = Column(String(64), nullable=False, unique=True)
    hint = Column(String(8), default="")                               # the last characters, to tell tokens apart
    created_at = Column(DateTime(timezone=True), default=now)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    last_used_at = Column(DateTime(timezone=True), nullable=True)
    used_from = Column(String(64), default="")
    revoked_at = Column(DateTime(timezone=True), nullable=True)


class NetAccessProfile(Base):
    """What an agent may do (sections of TASKS.md 2.2). Built-in templates and the company's own."""
    __tablename__ = "net_access_profiles"
    id = Column(Integer, primary_key=True)
    code = Column(String(40), nullable=False, unique=True)
    name = Column(String(120), nullable=False)
    description = Column(Text, default="")
    rules = Column(JSON, nullable=False)
    version = Column(Integer, nullable=False, default=1)
    builtin = Column(Boolean, default=False)
    updated_at = Column(DateTime(timezone=True), default=now)
    updated_by = Column(String(64), default="")


class NetAgentAccess(Base):
    """The profile of one agent, its personal exceptions and what the computer has applied."""
    __tablename__ = "net_agent_access"
    id = Column(Integer, primary_key=True)
    agent_id = Column(Integer, ForeignKey("net_agents.id", ondelete="CASCADE"), nullable=False, unique=True)
    profile_id = Column(Integer, ForeignKey("net_access_profiles.id", ondelete="SET NULL"), nullable=True)
    overrides = Column(JSON, nullable=True)
    applied_rev = Column(String(16), default="")
    applied_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(DateTime(timezone=True), default=now)
    updated_by = Column(String(64), default="")


class NetSkill(Base):
    """A skill of SYSAI_Hub: one hub for every agent of the network."""
    __tablename__ = "net_skills"
    id = Column(Integer, primary_key=True)
    name = Column(String(64), nullable=False, unique=True)             # the folder name, like the SKILL.md name
    title = Column(String(200), default="")
    description = Column(Text, default="")
    department_id = Column(Integer, ForeignKey("departments.id", ondelete="SET NULL"), nullable=True)  # owner department
    owner_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    audience = Column(JSON, nullable=True)                             # {"all": true} | {"departments": [ids]}
    status = Column(String(12), default="draft")                       # draft | review | published | retired
    current_version = Column(String(20), default="")                   # the published version agents get
    created_at = Column(DateTime(timezone=True), default=now)
    updated_at = Column(DateTime(timezone=True), default=now)


class NetSkillVersion(Base):
    """A version is never changed after publishing: a fix is a new version, a rollback picks an older one."""
    __tablename__ = "net_skill_versions"
    __table_args__ = (UniqueConstraint("skill_id", "version", name="uq_net_skill_version"),)
    id = Column(Integer, primary_key=True)
    skill_id = Column(Integer, ForeignKey("net_skills.id", ondelete="CASCADE"), nullable=False, index=True)
    version = Column(String(20), nullable=False)
    blob = deferred(Column(LargeBinary, nullable=False))               # the skill folder as a zip (at most 2 MB)
    sha256 = Column(String(64), nullable=False)
    size = Column(Integer, default=0)
    files = Column(JSON, nullable=True)                                # [{"path", "size"}]
    skill_md = Column(Text, default="")
    changelog = Column(Text, default="")
    why = Column(Text, default="")                                     # the agent's explanation of a proposal
    checks = Column(JSON, nullable=True)                               # {"errors": [...], "warnings": [...]}
    status = Column(String(12), default="draft")                       # draft | review | published | rejected
    author_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    author_agent_id = Column(Integer, ForeignKey("net_agents.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), default=now)
    decided_by = Column(String(64), default="")
    decided_at = Column(DateTime(timezone=True), nullable=True)
    decision_note = Column(Text, default="")


class NetSkillUsage(Base):
    __tablename__ = "net_skill_usage"
    id = Column(Integer, primary_key=True)
    skill = Column(String(64), nullable=False, index=True)
    version = Column(String(20), default="")
    agent_id = Column(Integer, ForeignKey("net_agents.id", ondelete="CASCADE"), nullable=True, index=True)
    at = Column(DateTime(timezone=True), default=now)
    tokens = Column(Integer, default=0)
    ok = Column(Boolean, nullable=True)
    edits_after = Column(Integer, nullable=True)


class NetEvent(Base):
    """The journal: who (a person or an agent), what, over what, when, «было → стало»."""
    __tablename__ = "net_events"
    __table_args__ = (Index("ix_net_events_agent_at", "agent_id", "at"),)
    id = Column(Integer, primary_key=True)
    at = Column(DateTime(timezone=True), default=now, index=True)
    actor_kind = Column(String(8), default="user")                     # user | agent | system
    actor = Column(String(64), default="")
    agent_id = Column(Integer, ForeignKey("net_agents.id", ondelete="CASCADE"), nullable=True)
    action = Column(String(60), nullable=False)
    target = Column(String(160), default="")
    details = Column(JSON, nullable=True)


class NetSpend(Base):
    """What one request of an agent through the gateway cost, with the project and the skill it was for."""
    __tablename__ = "net_spend"
    __table_args__ = (Index("ix_net_spend_agent_day", "agent_id", "day"),)
    id = Column(Integer, primary_key=True)
    day = Column(String(10), nullable=False, index=True)               # YYYY-MM-DD, UTC
    at = Column(DateTime(timezone=True), default=now)
    agent_id = Column(Integer, ForeignKey("net_agents.id", ondelete="CASCADE"), nullable=True)
    user_id = Column(Integer, nullable=True)
    tier = Column(String(12), default="")
    model = Column(String(120), default="")
    project = Column(String(64), default="", index=True)
    skill = Column(String(64), default="")
    prompt_tokens = Column(Integer, default=0)
    completion_tokens = Column(Integer, default=0)
    cost = Column(Float, default=0.0)
