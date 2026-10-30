"""Database models (SQLite by default, Postgres via DATABASE_URL)."""
import datetime as dt
import json
import logging
import socket
import time
from urllib.parse import urlparse

from sqlalchemy import (JSON, Boolean, Column, Date, DateTime, Float, ForeignKey, Index, Integer, LargeBinary, String, Text, UniqueConstraint,
                        create_engine, text)
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

from . import config

log = logging.getLogger("sysai.db")


def ipv4_hostaddr(hostname: str) -> str | None:
    """First A-record for the host, or None if IPv4 is unavailable.

    Render (and some other PaaS) resolve Neon to AAAA first. Outbound IPv6 is often
    missing, which surfaces as 'Network is unreachable' to port 5432. Pinning hostaddr
    to IPv4 keeps the same hostname/TLS while using a reachable path.
    """
    host = (hostname or "").strip().strip("[]")
    if not host or host in {"localhost", "127.0.0.1"}:
        return None
    try:
        infos = socket.getaddrinfo(host, 5432, socket.AF_INET, socket.SOCK_STREAM)
    except OSError:
        return None
    for family, _type, _proto, _canon, sockaddr in infos:
        if family == socket.AF_INET and sockaddr:
            return sockaddr[0]
    return None


def engine_options(url: str) -> dict:
    """Connection settings per backend.

    Postgres (Neon): an idle compute suspends and needs a few seconds to wake up, so connections are
    recycled, pre-pinged and given a generous connect timeout. Neon's pooled host (PgBouncer in transaction
    mode) cannot keep psycopg's automatically prepared statements, so server-side preparing is switched off.
    """
    if url.startswith("sqlite"):
        return {"connect_args": {"check_same_thread": False}, "pool_pre_ping": True}
    connect_args = {"connect_timeout": 30}
    if url.startswith("postgresql+psycopg://"):
        connect_args["prepare_threshold"] = None
        host = urlparse(url).hostname
        hostaddr = ipv4_hostaddr(host or "")
        if hostaddr:
            connect_args["hostaddr"] = hostaddr
    return {"pool_pre_ping": True, "pool_recycle": 300, "pool_size": 8, "max_overflow": 8,
            "connect_args": connect_args}


engine = create_engine(config.DATABASE_URL, **engine_options(config.DATABASE_URL))
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
Base = declarative_base()


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Setting(Base):
    __tablename__ = "settings"
    key = Column(String(100), primary_key=True)
    value = Column(Text, nullable=False)  # JSON


class Reminder(Base):
    """A reminder the AI employee writes to people at a given moment. Asked in a chat, usually by voice:
    "напомни всем, что сегодня совещание". One row per request, delivery is tracked per recipient."""
    __tablename__ = "reminders"
    id = Column(Integer, primary_key=True)
    created_at = Column(DateTime(timezone=True), default=now)
    chat_id = Column(String(40), default="")         # Telegram user id of the person who asked
    requested_by = Column(String(120), default="")
    due_at = Column(DateTime(timezone=True), nullable=False, index=True)
    topic = Column(String(300), nullable=False)
    details = Column(Text, default="")
    recipients = Column(JSON, default=list)          # [{"ref": "@ivan", "name": "Иван", "employee_id": 3}]
    delivered = Column(JSON, default=list)          # refs that already got the message
    status = Column(String(20), default="pending", index=True)  # pending | sending | sent | failed | cancelled | missed (not sent: noticed more than reminders.MAX_LATE late)
    attempts = Column(Integer, default=0)
    error = Column(Text, default="")
    claimed_at = Column(DateTime(timezone=True), nullable=True)
    sent_at = Column(DateTime(timezone=True), nullable=True)


class MeetingAlert(Base):
    """A reminder about a meeting set in the web panel ("Напоминания"): the meeting time, a switch and the people to remind.
    Weekly on the chosen days, or once on a date. The background loop turns a due alert into an ordinary Reminder."""
    __tablename__ = "meeting_alerts"
    id = Column(Integer, primary_key=True)
    created_at = Column(DateTime(timezone=True), default=now)
    enabled = Column(Boolean, nullable=False, default=True)
    title = Column(String(120), nullable=False, default="Совещание")
    details = Column(Text, default="")                    # place, what to prepare
    meet_time = Column(String(5), nullable=False, default="10:00")   # HH:MM, service time zone
    lead_minutes = Column(Integer, nullable=False, default=15)       # remind this long before the meeting
    weekdays = Column(JSON, default=list)                 # 0 = Monday ... 6 = Sunday; unused when once_date is set
    once_date = Column(Date, nullable=True)
    recipients = Column(JSON, default=list)               # employee ids
    last_fired_on = Column(Date, nullable=True)           # the meeting date it was last handled for
    last_result = Column(String(300), default="")
    skips = Column(JSON, default=list)                    # [{"date": "2026-10-13", "employee_id": 3, "by": 12}]: the director reminded them


class User(Base):
    """Panel accounts. Sign-up requests start as "pending" and cannot sign in until a superuser approves them."""
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    username = Column(String(64), nullable=False, unique=True)
    display_name = Column(String(120), default="")
    password_hash = Column(String(255), nullable=False)
    role = Column(String(20), default="user")        # user | superuser
    status = Column(String(20), default="pending")   # pending | approved | rejected | disabled
    created_at = Column(DateTime(timezone=True), default=now)
    decided_at = Column(DateTime(timezone=True), nullable=True)
    decided_by = Column(String(64), default="")
    last_login_at = Column(DateTime(timezone=True), nullable=True)
    consent_at = Column(DateTime(timezone=True), nullable=True)   # consent to personal data processing (Law 99-Z)
    consent_version = Column(String(20), default="")


class TelegramChat(Base):
    """Chats the bot knows: people who pressed /start, groups and channels it was added to."""
    __tablename__ = "telegram_chats"
    chat_id = Column(String(40), primary_key=True)
    kind = Column(String(20))  # private | group | supergroup | channel
    title = Column(String(255))
    username = Column(String(100))
    active = Column(Boolean, default=True)
    updated_at = Column(DateTime(timezone=True), default=now, onupdate=now)


class Employee(Base):
    __tablename__ = "employees"
    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False)
    aliases = Column(String(500), default="")  # comma separated: "Ваня, Иван Петрович"
    position = Column(String(200), default="")
    department = Column(String(200), default="")
    competencies = Column(Text, default="")  # comma/newline separated skills and areas of expertise
    responsibilities = Column(Text, default="")  # what this employee is accountable for
    context = Column(Text, default="")  # extra disambiguating notes for assignment decisions
    communication_style = Column(Text, default="")  # how the person prefers to be written to (memory card)
    channel = Column(String(20), default="telegram")  # only telegram in MVP
    telegram_chat_id = Column(String(40), default="")  # bot chat id; in AI-employee mode @username, +phone or id
    telegram_user_id = Column(String(40), default="")  # numeric Telegram id, learned from the first message
    bitrix_user_id = Column(String(40), default="")
    active = Column(Boolean, default=True)
    org_order = Column(Integer, nullable=False, default=0)
    map_x = Column(Float, nullable=True)
    map_y = Column(Float, nullable=True)
    department_memberships = relationship("EmployeeDepartment", back_populates="employee",
                                          cascade="all, delete-orphan",
                                          order_by="(EmployeeDepartment.sort_order, EmployeeDepartment.id)")
    voiceprint = relationship("EmployeeVoiceprint", back_populates="employee", uselist=False,
                              cascade="all, delete-orphan", single_parent=True)
    voice_samples = relationship("EmployeeVoiceSample", back_populates="employee",
                                 cascade="all, delete-orphan", order_by="EmployeeVoiceSample.created_at")

    def alias_list(self):
        return [a.strip() for a in (self.aliases or "").split(",") if a.strip()]


class Department(Base):
    __tablename__ = "departments"
    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False, unique=True)
    description = Column(Text, default="")
    regulations = Column(Text, default="")  # how the department works: rules, procedures, who signs what
    active = Column(Boolean, default=True)
    map_x = Column(Float, nullable=True)
    map_y = Column(Float, nullable=True)
    created_at = Column(DateTime(timezone=True), default=now)
    memberships = relationship("EmployeeDepartment", back_populates="department",
                               cascade="all, delete-orphan",
                               order_by="(EmployeeDepartment.sort_order, EmployeeDepartment.id)")


class EmployeeDepartment(Base):
    __tablename__ = "employee_departments"
    id = Column(Integer, primary_key=True)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False, index=True)
    department_id = Column(Integer, ForeignKey("departments.id"), nullable=False, index=True)
    role = Column(String(200), default="")
    responsibilities = Column(Text, default="")
    is_primary = Column(Boolean, default=False)
    sort_order = Column(Integer, nullable=False, default=0)
    map_x = Column(Float, nullable=True)
    map_y = Column(Float, nullable=True)
    employee = relationship("Employee", back_populates="department_memberships")
    department = relationship("Department", back_populates="memberships")
    __table_args__ = (UniqueConstraint("employee_id", "department_id", name="uq_employee_department"),)


class OrgRelation(Base):
    """An editable directed edge in the company organisation graph.

    Existing EmployeeDepartment rows remain the source of department membership and
    per-department leadership. This table stores cross-person reporting lines and
    explicit company-level links without duplicating employee or department records.
    """
    __tablename__ = "org_relations"
    id = Column(Integer, primary_key=True)
    source_type = Column(String(20), nullable=False, default="employee")  # company | employee | department
    source_id = Column(Integer, nullable=False, default=1)
    target_type = Column(String(20), nullable=False, default="employee")  # employee | department
    target_id = Column(Integer, nullable=False, index=True)
    relation_type = Column(String(40), nullable=False, default="reports_to")
    label = Column(String(200), nullable=False, default="")
    created_at = Column(DateTime(timezone=True), default=now)
    __table_args__ = (
        UniqueConstraint("source_type", "source_id", "target_type", "target_id", "relation_type",
                         name="uq_org_relation_edge"),
    )


class EmployeeVoiceprint(Base):
    """Encrypted provider voiceprint and consent metadata; raw enrollment audio is not stored."""
    __tablename__ = "employee_voiceprints"
    id = Column(Integer, primary_key=True)
    employee_id = Column(Integer, ForeignKey("employees.id", ondelete="CASCADE"), nullable=False, unique=True, index=True)
    encrypted_voiceprint = Column(LargeBinary, nullable=False)
    provider = Column(String(40), nullable=False, default="pyannoteAI")
    sample_duration = Column(Float, nullable=False, default=0.0)
    consent_at = Column(DateTime(timezone=True), nullable=False, default=now)
    consent_version = Column(String(40), nullable=False, default="voiceprint-consent-v1")
    created_at = Column(DateTime(timezone=True), nullable=False, default=now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=now, onupdate=now)
    employee = relationship("Employee", back_populates="voiceprint")


class EmployeeVoiceSample(Base):
    """One consented reference recording. Audio lives encrypted in the DB; embeddings are optional."""
    __tablename__ = "employee_voice_samples"
    id = Column(Integer, primary_key=True)
    employee_id = Column(Integer, ForeignKey("employees.id", ondelete="CASCADE"), nullable=False, index=True)
    sample_blob_path = Column(String(600), nullable=False, default="")
    encrypted_audio = Column(LargeBinary, nullable=True)
    audio_mime = Column(String(80), nullable=False, default="audio/wav")
    audio_bytes = Column(Integer, nullable=False, default=0)
    encrypted_embedding = Column(LargeBinary, nullable=False, default=bytes)
    embedding_status = Column(String(24), nullable=False, default="pending")  # pending | ready | failed
    embedding_dimensions = Column(Integer, nullable=False, default=0)
    model_version = Column(String(120), nullable=False, default="")
    sample_duration = Column(Float, nullable=False, default=0.0)
    waveform_data = Column(JSON, nullable=False, default=list)
    source_kind = Column(String(32), nullable=False, default="manual")  # manual | meeting_confirmed
    source_meeting_id = Column(Integer, ForeignKey("meetings.id", ondelete="SET NULL"), nullable=True, index=True)
    source_start = Column(Float, nullable=True)
    source_end = Column(Float, nullable=True)
    content_sha256 = Column(String(64), nullable=False, default="", index=True)
    source_audio_sha256 = Column(String(64), nullable=False, default="")
    quality_metrics = Column(JSON, nullable=False, default=dict)
    confirmed_by = Column(String(120), nullable=False, default="")
    access_owner = Column(String(128), nullable=False, default="")
    expires_at = Column(DateTime(timezone=True), nullable=True)
    consent_at = Column(DateTime(timezone=True), nullable=False, default=now)
    consent_version = Column(String(40), nullable=False, default="voiceprint-consent-v2")
    created_at = Column(DateTime(timezone=True), nullable=False, default=now)
    employee = relationship("Employee", back_populates="voice_samples")


class VoiceSampleCleanup(Base):
    """Legacy external-copy cleanup outbox; no audio/embedding or identity evidence."""
    __tablename__ = "voice_sample_cleanup"
    id = Column(Integer, primary_key=True)
    employee_id = Column(Integer, nullable=False)
    storage_path = Column(String(600), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now)


class Meeting(Base):
    __tablename__ = "meetings"
    id = Column(Integer, primary_key=True)
    title = Column(String(300), default="")
    source = Column(String(20), default="web")  # web | microphone | demo | api
    filename = Column(String(300), default="")
    audio_path = Column(String(500), default="")
    status = Column(String(30), default="queued")
    progress = Column(String(300), default="")
    error = Column(Text, default="")
    duration_sec = Column(Float, default=0)
    meeting_date = Column(Date, nullable=True)
    transcript = Column(JSON, nullable=True)  # [{speaker, start, end, text}]
    report = Column(JSON, nullable=True)
    options = Column(JSON, nullable=True)  # per-meeting overrides of settings (approval, deadlines, recipients…)
    created_at = Column(DateTime(timezone=True), default=now)
    approved_at = Column(DateTime(timezone=True), nullable=True)
    sent_at = Column(DateTime(timezone=True), nullable=True)
    tasks = relationship("Task", back_populates="meeting", cascade="all, delete-orphan", order_by="Task.id")
    deliveries = relationship("Delivery", cascade="all, delete-orphan", order_by="Delivery.id")
    checkpoints = relationship("ProcessingCheckpoint", cascade="all, delete-orphan")
    dialogue = relationship("ConversationMessage", cascade="all, delete-orphan", order_by="ConversationMessage.id")
    escalations = relationship("ConversationEscalation", cascade="all, delete-orphan", order_by="ConversationEscalation.id")
    report_versions = relationship("ReportVersion", cascade="all, delete-orphan", order_by="ReportVersion.version")


class ReportVersion(Base):
    __tablename__ = "report_versions"
    __table_args__ = (UniqueConstraint("meeting_id", "version"),)
    id = Column(Integer, primary_key=True)
    meeting_id = Column(Integer, ForeignKey("meetings.id"), nullable=False, index=True)
    version = Column(Integer, nullable=False)
    title = Column(String(300), nullable=False)
    filename = Column(String(300), nullable=False)
    pdf = Column(LargeBinary, nullable=False)
    snapshot = Column(JSON, nullable=False)
    origin = Column(String(30), nullable=False, default="approval")
    created_at = Column(DateTime(timezone=True), nullable=False, default=now)

    @property
    def size_bytes(self):
        return len(self.pdf)


class Recording(Base):
    __tablename__ = "recordings"

    id = Column(String(64), primary_key=True)
    owner = Column(String(128), nullable=False, index=True)
    mime_type = Column(String(80), nullable=False)
    title = Column(String(300), nullable=False, default="")
    device_label = Column(String(120), nullable=False, default="")
    meeting_date = Column(Date, nullable=True)
    options = Column(JSON, nullable=False, default=dict)
    status = Column(String(20), nullable=False, default="recording", index=True)
    next_sequence = Column(Integer, nullable=False, default=0)
    size_bytes = Column(Integer, nullable=False, default=0)
    path = Column(String(500), nullable=False, default="")
    meeting_id = Column(Integer, ForeignKey("meetings.id", ondelete="SET NULL"), nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=now, onupdate=now)


class RecordingChunk(Base):
    __tablename__ = "recording_chunks"
    __table_args__ = (UniqueConstraint("recording_id", "sequence"),)

    recording_id = Column(String(64), ForeignKey("recordings.id", ondelete="CASCADE"), primary_key=True)
    sequence = Column(Integer, primary_key=True)
    digest = Column(String(64), nullable=False)
    size_bytes = Column(Integer, nullable=False)


class Task(Base):
    __tablename__ = "tasks"
    id = Column(Integer, primary_key=True)
    meeting_id = Column(Integer, ForeignKey("meetings.id"))
    title = Column(String(500), nullable=False)
    description = Column(Text, default="")
    assignee_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    assignee_name = Column(String(200), default="")  # as heard in the meeting
    target_employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True, index=True)
    target_employee_name = Column(String(200), default="")  # person the assistant must contact, not the task owner
    deadline = Column(Date, nullable=True)
    deadline_source = Column(String(20), default="none")  # stated | default | asked | manual | none
    deadline_quote = Column(String(500), default="")
    priority = Column(String(10), default="medium")
    source_quote = Column(Text, default="")
    source_time = Column(String(20), default="")
    status = Column(String(30), default="draft")  # draft | sent | awaiting_deadline | no_recipient | in_progress | blocked | done | assistant_todo | assistant_done | assistant_failed
    executor = Column(String(12), default="person")  # person | assistant (the AI employee does it itself)
    action_type = Column(String(12), default="")  # message | ask | request | forward | produce | do (see task_routing)
    counterparts = Column(JSON, default=list)  # [{"role": "source|recipient|target", "employee_id": 3, "name": "Аня"}]
    routing_note = Column(String(500), default="")  # why the executor differs from what the model proposed
    bitrix_task_id = Column(String(40), default="")
    meeting = relationship("Meeting", back_populates="tasks")
    assignee = relationship("Employee", foreign_keys=[assignee_id])
    target_employee = relationship("Employee", foreign_keys=[target_employee_id])


class DeadlineRequest(Base):
    __tablename__ = "deadline_requests"
    id = Column(Integer, primary_key=True)
    task_id = Column(Integer, ForeignKey("tasks.id"))
    chat_id = Column(String(40))
    message_id = Column(Integer, nullable=True)
    asked_at = Column(DateTime(timezone=True), default=now)
    resolved = Column(Boolean, default=False)
    task = relationship("Task")


class ConversationMessage(Base):
    __tablename__ = "conversation_messages"
    __table_args__ = (UniqueConstraint("chat_id", "message_id"),)
    id = Column(Integer, primary_key=True)
    chat_id = Column(String(40), nullable=False, index=True)
    message_id = Column(Integer, nullable=True)
    role = Column(String(20), nullable=False)
    status = Column(String(20), nullable=False, default="pending", index=True)
    text = Column(Text, nullable=False)
    meeting_id = Column(Integer, ForeignKey("meetings.id"), nullable=True)
    reply_to = Column(Integer, nullable=True)
    result = Column(JSON, nullable=True)
    action_applied = Column(Boolean, nullable=False, default=False)
    error = Column(Text, default="")
    created_at = Column(DateTime(timezone=True), default=now)


class ConversationEscalation(Base):
    __tablename__ = "conversation_escalations"
    id = Column(Integer, primary_key=True)
    meeting_id = Column(Integer, ForeignKey("meetings.id"), nullable=False, index=True)
    task_id = Column(Integer, ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True)
    chat_id = Column(String(40), nullable=False)
    reason = Column(Text, nullable=False)
    status = Column(String(20), nullable=False, default="open")
    created_at = Column(DateTime(timezone=True), default=now)


class ProcessingCheckpoint(Base):
    __tablename__ = "processing_checkpoints"
    __table_args__ = (UniqueConstraint("meeting_id", "stage", "part", "fingerprint"),)
    id = Column(Integer, primary_key=True)
    meeting_id = Column(Integer, ForeignKey("meetings.id"), nullable=False, index=True)
    stage = Column(String(30), nullable=False)
    part = Column(String(100), nullable=False)
    fingerprint = Column(String(64), nullable=False)
    data = Column(JSON, nullable=False)
    created_at = Column(DateTime(timezone=True), default=now)


class Delivery(Base):
    __tablename__ = "deliveries"
    __table_args__ = (UniqueConstraint("meeting_id", "key"),)
    id = Column(Integer, primary_key=True)
    meeting_id = Column(Integer, ForeignKey("meetings.id"), nullable=False, index=True)
    key = Column(String(160), nullable=False)
    phase = Column(String(10), nullable=False)
    kind = Column(String(30), nullable=False)
    chat_id = Column(String(40), nullable=False)
    payload = Column(JSON, nullable=False)
    document = Column(LargeBinary, nullable=True)
    task_ids = Column(JSON, nullable=False, default=list)
    depends_on = Column(Integer, ForeignKey("deliveries.id", ondelete="SET NULL"), nullable=True)
    status = Column(String(20), nullable=False, default="pending", index=True)
    attempts = Column(Integer, nullable=False, default=0)
    cycle_attempts = Column(Integer, nullable=False, default=0)
    message_id = Column(Integer, nullable=True)
    error = Column(Text, default="")
    next_attempt_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=now)
    sent_at = Column(DateTime(timezone=True), nullable=True)


def _db_error_text(exc: Exception) -> str:
    return str(exc)


def is_permanent_db_error(exc: Exception) -> bool:
    """Errors that will not clear by waiting (quota, auth, TLS). Do not retry these at boot."""
    text = _db_error_text(exc).lower()
    markers = (
        "exceeded the quota",
        "upgrade your plan",
        "password authentication failed",
        "no password supplied",
        "ssl connection has been closed unexpectedly",
        "invalid authorization",
        "permission denied",
    )
    return any(marker in text for marker in markers)


def wait_for_db(attempts: int = 6, delay: float = 3.0):
    """Block until the database answers. A suspended Neon compute needs a few seconds to wake up,
    so a failed first connection retries with a growing pause instead of crashing the boot."""
    for attempt in range(1, attempts + 1):
        try:
            with engine.connect() as con:
                con.execute(text("SELECT 1"))
            return
        except OperationalError as exc:
            message = str(exc).splitlines()[0]
            if is_permanent_db_error(exc):
                log.error("Database refused the connection (not a wake-up delay): %s", message)
                raise
            if attempt == attempts:
                raise
            log.warning("Database is not ready (attempt %s of %s): %s", attempt, attempts, message)
            time.sleep(delay * attempt)


class Note(Base):
    """What people said and did, one fact per row: the AI employee's notebook and the source for "who does what"."""
    __tablename__ = "notes"
    id = Column(Integer, primary_key=True)
    employee_id = Column(Integer, ForeignKey("employees.id", ondelete="SET NULL"), nullable=True, index=True)
    task_id = Column(Integer, ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True, index=True)
    meeting_id = Column(Integer, ForeignKey("meetings.id", ondelete="SET NULL"), nullable=True)
    kind = Column(String(20), default="fact")  # fact | status | commitment | done | system | company
    text = Column(Text, nullable=False)
    source = Column(String(20), default="conversation")  # conversation | meeting | system | manual
    author = Column(String(80), default="")
    created_at = Column(DateTime(timezone=True), default=now, index=True)
    department_id = Column(Integer, ForeignKey("departments.id", ondelete="SET NULL"), nullable=True, index=True)
    confidence = Column(String(10), default="medium")  # high | medium | low
    quote = Column(Text, default="")  # the words the fact rests on (meeting transcript, message)
    archived = Column(Boolean, nullable=False, default=False)  # hidden from memory, kept for the audit trail
    updated_at = Column(DateTime(timezone=True), nullable=True)
    edited_by = Column(String(120), default="")


class PersonMemory(Base):
    """A rolling summary of everything the AI employee knows about one person it talks to (key: Telegram user id)."""
    __tablename__ = "person_memory"
    chat_id = Column(String(40), primary_key=True)
    employee_id = Column(Integer, ForeignKey("employees.id", ondelete="SET NULL"), nullable=True)
    summary = Column(Text, default="")
    updated_at = Column(DateTime(timezone=True), default=now, onupdate=now)


class Artifact(Base):
    """A document the AI employee produced itself (chart, report): kept for the panel and sent to the reviewer."""
    __tablename__ = "artifacts"
    id = Column(Integer, primary_key=True)
    meeting_id = Column(Integer, ForeignKey("meetings.id", ondelete="SET NULL"), nullable=True, index=True)
    task_id = Column(Integer, ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True)
    title = Column(String(300), default="")
    filename = Column(String(200), default="")
    mime = Column(String(60), default="application/pdf")
    data = Column(LargeBinary, nullable=True)
    svg = Column(Text, default="")
    created_at = Column(DateTime(timezone=True), default=now)


class LlmSpend(Base):
    """What one OpenRouter call cost (usage.cost in the answer). The daily budget is the sum for the UTC day."""
    __tablename__ = "llm_spend"
    id = Column(Integer, primary_key=True)
    day = Column(String(10), nullable=False, index=True)  # YYYY-MM-DD, UTC
    model = Column(String(120), default="")
    cost = Column(Float, nullable=False, default=0.0)
    created_at = Column(DateTime(timezone=True), default=now)



class NotionConnection(Base):
    """Single Notion workspace authorized by an administrator; tokens are encrypted at rest."""
    __tablename__ = "notion_connections"
    id = Column(Integer, primary_key=True, default=1)
    access_token_encrypted = Column(Text, nullable=False, default="")
    refresh_token_encrypted = Column(Text, nullable=False, default="")
    token_expires_at = Column(DateTime(timezone=True), nullable=True)
    workspace_id = Column(String(100), default="")
    workspace_name = Column(String(200), default="")
    owner_name = Column(String(200), default="")
    bot_id = Column(String(100), default="")
    webhook_verification_token_encrypted = Column(Text, nullable=False, default="")
    last_sync_at = Column(DateTime(timezone=True), nullable=True)
    connected_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(DateTime(timezone=True), default=now, onupdate=now)


class NotionImportedPage(Base):
    """One Notion page maps to at most one SYSAI meeting."""
    __tablename__ = "notion_imported_pages"
    page_id = Column(String(64), primary_key=True)
    meeting_id = Column(Integer, ForeignKey("meetings.id", ondelete="CASCADE"), nullable=False, index=True)
    title = Column(String(300), default="")
    page_url = Column(String(1000), default="")
    last_edited_time = Column(String(50), default="")
    status = Column(String(20), default="queued")
    imported_at = Column(DateTime(timezone=True), default=now)
    error = Column(Text, default="")


class TicnoteImport(Base):
    """One TicNote device recording maps to at most one SYSAI meeting (record_id is the TicNote recordId)."""
    __tablename__ = "ticnote_imports"
    record_id = Column(String(64), primary_key=True)
    meeting_id = Column(Integer, ForeignKey("meetings.id", ondelete="SET NULL"), nullable=True, index=True)
    title = Column(String(300), default="")
    status = Column(String(20), default="downloading")  # downloading | queued | error
    attempts = Column(Integer, nullable=False, default=0)
    error = Column(Text, default="")
    created_at = Column(DateTime(timezone=True), default=now)
    updated_at = Column(DateTime(timezone=True), default=now, onupdate=now)


class NotionWebhookEvent(Base):
    """Durable, deduplicated Notion webhook inbox and retry state."""
    __tablename__ = "notion_webhook_events"
    event_id = Column(String(200), primary_key=True)
    page_id = Column(String(64), nullable=False, index=True)
    event_type = Column(String(80), nullable=False, default="")
    status = Column(String(20), nullable=False, default="pending", index=True)
    attempts = Column(Integer, nullable=False, default=0)
    error = Column(Text, default="")
    received_at = Column(DateTime(timezone=True), default=now, index=True)
    processed_at = Column(DateTime(timezone=True), nullable=True)


class Case(Base):
    """A piece of work the AI employee carries through to the end: ask somebody, wait, get a document, pass it on."""
    __tablename__ = "cases"
    id = Column(Integer, primary_key=True)
    task_id = Column(Integer, ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True, unique=True)
    meeting_id = Column(Integer, ForeignKey("meetings.id", ondelete="SET NULL"), nullable=True, index=True)
    title = Column(String(500), nullable=False, default="")
    kind = Column(String(12), default="")  # ask | request | forward (task_routing action types)
    # awaiting_approval | suggested | running | waiting | attention | done | failed | cancelled
    status = Column(String(20), nullable=False, default="running", index=True)
    autonomy = Column(String(10), default="auto")  # auto | approve | suggest
    planner = Column(String(10), default="template")  # model | template
    note = Column(Text, default="")  # why the case stopped or what it needs
    attempts = Column(Integer, nullable=False, default=0)
    retry_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=now)
    updated_at = Column(DateTime(timezone=True), default=now, onupdate=now)
    closed_at = Column(DateTime(timezone=True), nullable=True)
    steps = relationship("CaseStep", order_by="CaseStep.position", cascade="all, delete-orphan")
    events = relationship("CaseEvent", order_by="CaseEvent.id", cascade="all, delete-orphan")
    task = relationship("Task")


class CaseStep(Base):
    __tablename__ = "case_steps"
    id = Column(Integer, primary_key=True)
    case_id = Column(Integer, ForeignKey("cases.id", ondelete="CASCADE"), nullable=False, index=True)
    position = Column(Integer, nullable=False)
    tool = Column(String(30), nullable=False)  # send_message | wait_reply | forward_document | report | close
    args = Column(JSON, nullable=False, default=dict)
    # pending | sending | waiting | awaiting_approval | done | uncertain | failed | skipped
    status = Column(String(20), nullable=False, default="pending")
    result = Column(JSON, nullable=True)
    error = Column(Text, default="")
    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)


class CaseWait(Base):
    """The case waits for one person: an answer or a document. Reminders and escalation run on this row."""
    __tablename__ = "case_waits"
    id = Column(Integer, primary_key=True)
    case_id = Column(Integer, ForeignKey("cases.id", ondelete="CASCADE"), nullable=False, index=True)
    step_id = Column(Integer, ForeignKey("case_steps.id", ondelete="CASCADE"), nullable=False, unique=True)
    employee_id = Column(Integer, ForeignKey("employees.id", ondelete="SET NULL"), nullable=True, index=True)
    chat_ref = Column(String(80), default="")
    expects = Column(String(10), nullable=False, default="answer")  # answer | document
    status = Column(String(12), nullable=False, default="open", index=True)  # open | resolved | cancelled
    asked_message_id = Column(Integer, nullable=True)
    started_at = Column(DateTime(timezone=True), default=now)
    next_reminder_at = Column(DateTime(timezone=True), nullable=True)
    reminders_sent = Column(Integer, nullable=False, default=0)
    escalate_at = Column(DateTime(timezone=True), nullable=True)
    escalated = Column(Boolean, nullable=False, default=False)
    resolved_at = Column(DateTime(timezone=True), nullable=True)
    answer = Column(Text, default="")
    document_id = Column(Integer, ForeignKey("documents.id", ondelete="SET NULL"), nullable=True)


class CaseEvent(Base):
    """The case's diary: what was done, to whom, what came back. Shown in the panel and kept as an audit trail."""
    __tablename__ = "case_events"
    id = Column(Integer, primary_key=True)
    case_id = Column(Integer, ForeignKey("cases.id", ondelete="CASCADE"), nullable=False, index=True)
    step_id = Column(Integer, nullable=True)
    kind = Column(String(30), nullable=False)
    text = Column(Text, nullable=False, default="")
    data = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), default=now)


class Document(Base):
    """A file a person sent to the AI employee in Telegram (PDF, DOCX, XLSX, photo...): the document library."""
    __tablename__ = "documents"
    __table_args__ = (UniqueConstraint("chat_id", "message_id"),)
    id = Column(Integer, primary_key=True)
    case_id = Column(Integer, ForeignKey("cases.id", ondelete="SET NULL"), nullable=True, index=True)
    employee_id = Column(Integer, ForeignKey("employees.id", ondelete="SET NULL"), nullable=True, index=True)
    chat_id = Column(String(40), nullable=False)
    message_id = Column(Integer, nullable=False)
    filename = Column(String(255), default="")
    mime = Column(String(120), default="")
    size = Column(Integer, nullable=False, default=0)
    sha256 = Column(String(64), nullable=False)
    kind = Column(String(10), default="other")  # pdf | docx | xlsx | image | other
    caption = Column(Text, default="")
    sensitive = Column(Boolean, nullable=False, default=False)
    data = Column(LargeBinary, nullable=True)
    text = Column(Text, default="")  # extracted text (PDF/DOCX/XLSX, scans through the vision model)
    text_status = Column(String(12), default="pending")  # pending | done | empty | failed
    summary = Column(Text, default="")
    created_at = Column(DateTime(timezone=True), default=now, index=True)
    employee = relationship("Employee")


class DocumentTransfer(Base):
    """One forwarding of a document: who to, which bytes (hash), who allowed it, what Telegram answered."""
    __tablename__ = "document_transfers"
    id = Column(Integer, primary_key=True)
    document_id = Column(Integer, ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True)
    case_id = Column(Integer, ForeignKey("cases.id", ondelete="SET NULL"), nullable=True, index=True)
    step_id = Column(Integer, ForeignKey("case_steps.id", ondelete="SET NULL"), nullable=True)
    to_employee_id = Column(Integer, ForeignKey("employees.id", ondelete="SET NULL"), nullable=True)
    to_ref = Column(String(80), default="")
    sha256 = Column(String(64), nullable=False)
    # awaiting_approval | approved | sending | sent | uncertain | blocked
    status = Column(String(20), nullable=False, default="approved")
    reason = Column(Text, default="")
    approved_by = Column(String(120), default="")
    message_id = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), default=now)
    sent_at = Column(DateTime(timezone=True), nullable=True)


class DocumentChunk(Base):
    """A piece of a document's text: what memory search finds and what questions about the document are answered from."""
    __tablename__ = "document_chunks"
    id = Column(Integer, primary_key=True)
    document_id = Column(Integer, ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True)
    position = Column(Integer, nullable=False)
    text = Column(Text, nullable=False)


class Skill(Base):
    """A know-how of the AI employee: a scenario or a template («как запросить исполнительную», «тон с подрядчиками»)."""
    __tablename__ = "skills"
    id = Column(Integer, primary_key=True)
    title = Column(String(200), nullable=False)
    kind = Column(String(12), default="scenario")  # scenario | template | tone
    when_to_use = Column(Text, default="")
    body = Column(Text, nullable=False, default="")
    active = Column(Boolean, nullable=False, default=True)
    uses = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), default=now)
    updated_at = Column(DateTime(timezone=True), default=now, onupdate=now)
    updated_by = Column(String(120), default="")


class MemoryVector(Base):
    """Embedding of one memory item (key like "note:12"); re-made when the item's text changes (hash)."""
    __tablename__ = "memory_vectors"
    key = Column(String(60), primary_key=True)
    hash = Column(String(40), nullable=False)
    model = Column(String(120), default="")
    vector = Column(JSON, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=now, onupdate=now)


def _add_column_if_missing(table: str, column: str, ddl: str):
    """Lightweight migration for databases created by earlier versions."""
    from sqlalchemy import inspect
    sqlite_ddl = ddl.replace(" BYTEA", " BLOB") if engine.dialect.name == "sqlite" else ddl
    for attempt in (1, 2):
        names = {c["name"] for c in inspect(engine).get_columns(table)}
        if column in names:
            return
        try:
            with engine.begin() as con:
                con.execute(text(f"ALTER TABLE {table} ADD COLUMN {sqlite_ddl}"))
            return
        except OperationalError as exc:
            # SQLite parses ALTER against the schema cached by the pooled connection. A connection that missed a
            # schema change made through another one reports a duplicate column that is not there: reconnect once.
            if "duplicate column" not in str(exc).lower() or attempt == 2:
                raise
            engine.dispose()


def init_db():
    if engine.dialect.name == "sqlite":
        # Pooled SQLite connections cache the schema; start from fresh ones after DDL done through another connection.
        engine.dispose()
    wait_for_db()
    log.info("Database ready: %s%s", engine.dialect.name, f" @ {engine.url.host}" if engine.url.host else "")
    Base.metadata.create_all(engine)
    _add_column_if_missing("meetings", "options", "options JSON")
    _add_column_if_missing("deliveries", "cycle_attempts", "cycle_attempts INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing("tasks", "executor", "executor VARCHAR(12) DEFAULT 'person'")
    _add_column_if_missing("tasks", "target_employee_id", "target_employee_id INTEGER REFERENCES employees(id)")
    _add_column_if_missing("tasks", "target_employee_name", "target_employee_name VARCHAR(200) DEFAULT ''")
    _add_column_if_missing("tasks", "action_type", "action_type VARCHAR(12) DEFAULT ''")
    _add_column_if_missing("tasks", "counterparts", "counterparts JSON")
    _add_column_if_missing("tasks", "routing_note", "routing_note VARCHAR(500) DEFAULT ''")
    _add_column_if_missing("employees", "telegram_user_id", "telegram_user_id VARCHAR(40) DEFAULT ''")
    _add_column_if_missing("recordings", "device_label", "device_label VARCHAR(120) NOT NULL DEFAULT ''")
    _add_column_if_missing("employees", "department", "department VARCHAR(200) DEFAULT ''")
    _add_column_if_missing("employees", "competencies", "competencies TEXT DEFAULT ''")
    _add_column_if_missing("employees", "responsibilities", "responsibilities TEXT DEFAULT ''")
    _add_column_if_missing("employees", "context", "context TEXT DEFAULT ''")
    _add_column_if_missing("users", "consent_at", "consent_at TIMESTAMP")
    _add_column_if_missing("employees", "communication_style", "communication_style TEXT DEFAULT ''")
    _add_column_if_missing("employees", "org_order", "org_order INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing("employee_departments", "sort_order", "sort_order INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing("employees", "map_x", "map_x FLOAT")
    _add_column_if_missing("employees", "map_y", "map_y FLOAT")
    _add_column_if_missing("departments", "map_x", "map_x FLOAT")
    _add_column_if_missing("departments", "map_y", "map_y FLOAT")
    _add_column_if_missing("employee_departments", "map_x", "map_x FLOAT")
    _add_column_if_missing("employee_departments", "map_y", "map_y FLOAT")
    _add_column_if_missing("departments", "regulations", "regulations TEXT DEFAULT ''")
    _add_column_if_missing("notes", "department_id", "department_id INTEGER REFERENCES departments(id) ON DELETE SET NULL")
    _add_column_if_missing("notes", "confidence", "confidence VARCHAR(10) DEFAULT 'medium'")
    _add_column_if_missing("notes", "quote", "quote TEXT DEFAULT ''")
    _add_column_if_missing("notes", "archived", "archived BOOLEAN NOT NULL DEFAULT FALSE")
    _add_column_if_missing("notes", "updated_at", "updated_at TIMESTAMP")
    _add_column_if_missing("notes", "edited_by", "edited_by VARCHAR(120) DEFAULT ''")
    _add_column_if_missing("documents", "text", "text TEXT DEFAULT ''")
    _add_column_if_missing("documents", "text_status", "text_status VARCHAR(12) DEFAULT 'pending'")
    _add_column_if_missing("documents", "summary", "summary TEXT DEFAULT ''")
    _add_column_if_missing("users", "consent_version", "consent_version VARCHAR(20) DEFAULT ''")
    _add_column_if_missing("employee_voice_samples", "source_kind", "source_kind VARCHAR(32) NOT NULL DEFAULT 'manual'")
    _add_column_if_missing("employee_voice_samples", "source_meeting_id", "source_meeting_id INTEGER REFERENCES meetings(id) ON DELETE SET NULL")
    _add_column_if_missing("employee_voice_samples", "source_start", "source_start FLOAT")
    _add_column_if_missing("employee_voice_samples", "source_end", "source_end FLOAT")
    _add_column_if_missing("employee_voice_samples", "encrypted_audio", "encrypted_audio BYTEA")
    _add_column_if_missing("employee_voice_samples", "audio_mime", "audio_mime VARCHAR(80) NOT NULL DEFAULT 'audio/wav'")
    _add_column_if_missing("employee_voice_samples", "audio_bytes", "audio_bytes INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing("employee_voice_samples", "embedding_status", "embedding_status VARCHAR(24) NOT NULL DEFAULT 'pending'")
    _add_column_if_missing("employee_voice_samples", "content_sha256", "content_sha256 VARCHAR(64) NOT NULL DEFAULT ''")
    _add_column_if_missing("employee_voice_samples", "source_audio_sha256", "source_audio_sha256 VARCHAR(64) NOT NULL DEFAULT ''")
    _add_column_if_missing("employee_voice_samples", "quality_metrics", "quality_metrics JSON NOT NULL DEFAULT '{}'")
    _add_column_if_missing("employee_voice_samples", "confirmed_by", "confirmed_by VARCHAR(120) NOT NULL DEFAULT ''")
    _add_column_if_missing("employee_voice_samples", "access_owner", "access_owner VARCHAR(128) NOT NULL DEFAULT ''")
    _add_column_if_missing("employee_voice_samples", "expires_at", "expires_at TIMESTAMP")
    _ensure_voice_sample_constraints()


def _ensure_voice_sample_constraints():
    """Add indexes even for an existing table. Unknown legacy rows remain ineligible."""
    with engine.begin() as con:
        con.execute(text("CREATE INDEX IF NOT EXISTS ix_employee_voice_samples_source_meeting_id "
                         "ON employee_voice_samples (source_meeting_id)"))
        con.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_voice_sample_content_sha256 "
                         "ON employee_voice_samples (content_sha256) WHERE content_sha256 <> ''"))
        # SQLite FK enforcement was historically disabled in this app. Restrict the fix
        # to biometric rows; do not rebuild unrelated legacy tables or change their data.
        if engine.dialect.name == "sqlite":
            con.execute(text("CREATE TRIGGER IF NOT EXISTS voice_samples_delete_meeting "
                             "AFTER DELETE ON meetings BEGIN DELETE FROM employee_voice_samples "
                             "WHERE source_meeting_id = OLD.id; END"))
            con.execute(text("CREATE TRIGGER IF NOT EXISTS voice_samples_delete_employee "
                             "AFTER DELETE ON employees BEGIN DELETE FROM employee_voice_samples "
                             "WHERE employee_id = OLD.id; END"))


def dumps(v) -> str:
    return json.dumps(v, ensure_ascii=False)
