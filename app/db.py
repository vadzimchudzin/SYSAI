"""Database models (SQLite by default, Postgres via DATABASE_URL)."""
import datetime as dt
import json
import logging
import socket
import time
from urllib.parse import urlparse

from sqlalchemy import (JSON, Boolean, Column, Date, DateTime, Float, ForeignKey, Index, Integer, LargeBinary, String, Text, UniqueConstraint,
                        create_engine, event, text)
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import declarative_base, deferred, relationship, sessionmaker

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
    email = Column(String(200), default="")  # work address: the invited of the company calendar are matched by it
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
    # «Запоминать голос между встречами» (voice_memory.py): when and by whom the employee's consent was recorded
    voice_memory_consent_at = Column(DateTime(timezone=True), nullable=True)
    voice_memory_consent_by = Column(String(120), default="")
    # Rights of the AI employee's chat (agent_skills.py): to whom the person may give orders ("" = by the access level,
    # all | department | self) and which skills the assistant carries out for them (None = all the level allows).
    order_scope = Column(String(12), default="")
    skills_allowed = Column(JSON, nullable=True)
    # When the person does not answer the assistant (cases.silence_rule): whom to tell (an employee; None = the
    # department's choice, then the reviewers), after how many hours and after how many reminders (None = inherited).
    silence_contact_id = Column(Integer, nullable=True)
    silence_hours = Column(Integer, nullable=True)
    silence_reminders = Column(Integer, nullable=True)
    # How the assistant addresses the person: the name to use («Анна Сергеевна») and «ты» or «вы» ("" = by the persona).
    address_as = Column(String(120), default="")
    address_form = Column(String(3), default="")

    def alias_list(self):
        return [a.strip() for a in (self.aliases or "").split(",") if a.strip()]


class Department(Base):
    __tablename__ = "departments"
    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False, unique=True)
    description = Column(Text, default="")
    regulations = Column(Text, default="")  # how the department works: rules, procedures, who signs what
    active = Column(Boolean, default=True)
    # The silence rule of the department's people (see Employee): their own card wins
    silence_contact_id = Column(Integer, nullable=True)
    silence_hours = Column(Integer, nullable=True)
    silence_reminders = Column(Integer, nullable=True)
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
    """Templates of the removed pyannoteAI identification. Nothing writes them any more; init_db deletes what is left."""
    __tablename__ = "employee_voiceprints"
    id = Column(Integer, primary_key=True)
    employee_id = Column(Integer, ForeignKey("employees.id", ondelete="CASCADE"), nullable=False, unique=True, index=True)
    encrypted_voiceprint = Column(LargeBinary, nullable=False)
    provider = Column(String(40), nullable=False, default="legacy")
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


class EmployeeVoiceMemory(Base):
    """The voice print of one employee from one meeting where that voice was confirmed (voice_memory.py). No audio:
    the average print of the person's clear replicas, encrypted. Deleted with the consent, the meeting or its term."""
    __tablename__ = "employee_voice_memory"
    id = Column(Integer, primary_key=True)
    employee_id = Column(Integer, ForeignKey("employees.id", ondelete="CASCADE"), nullable=False, index=True)
    meeting_id = Column(Integer, ForeignKey("meetings.id", ondelete="CASCADE"), nullable=True, index=True)
    encrypted_embedding = Column(LargeBinary, nullable=False)
    model_version = Column(String(120), nullable=False, default="")
    seconds = Column(Float, nullable=False, default=0.0)
    replicas = Column(Integer, nullable=False, default=0)
    how = Column(String(16), nullable=False, default="manual")  # manual | context
    confirmed_by = Column(String(120), nullable=False, default="")
    consent_version = Column(String(40), nullable=False, default="voice-memory-v1")
    created_at = Column(DateTime(timezone=True), nullable=False, default=now)
    expires_at = Column(DateTime(timezone=True), nullable=True)


class VoiceSampleCleanup(Base):
    """Legacy external-copy cleanup outbox; no audio/embedding or identity evidence."""
    __tablename__ = "voice_sample_cleanup"
    id = Column(Integer, primary_key=True)
    employee_id = Column(Integer, nullable=False)
    storage_path = Column(String(600), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now)


class StorageCleanup(Base):
    """Objects to delete from the recordings storage (a deleted meeting, an expired or removed recording). An outbox: the
    deletion is committed with the change that needs it and retried until the storage confirms it (audio_archive.py)."""
    __tablename__ = "storage_cleanup"
    id = Column(Integer, primary_key=True)
    key = Column(String(600), nullable=False)
    reason = Column(String(20), nullable=False, default="")
    attempts = Column(Integer, nullable=False, default=0)
    error = Column(String(300), nullable=False, default="")
    next_at = Column(DateTime(timezone=True), nullable=False, default=now, index=True)
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
    # Manual corrections (transcript_edit.py): the automatic transcript as it was before the first one, the undo
    # history, and the corrected copy frozen as a reference («эталон») for measuring the labelling (diarization_eval.py).
    # Deferred: the lists of meetings never need them.
    transcript_auto = deferred(Column(JSON, nullable=True))
    transcript_edits = deferred(Column(JSON, nullable=True))
    diarization_reference = deferred(Column(JSON, nullable=True))
    participants = Column(JSON, nullable=True)  # who was there, see participants.py: {"people": [...], "source": ...}
    waveform = Column(JSON, nullable=True)  # loudness outline for the voice diagram, see waveform.py
    recording_status = Column(String(12), default="")  # "" | pending | stored | failed | deleted | expired, see audio_archive.py
    recording = Column(JSON, nullable=True)  # the kept copy: storage key, size, duration, until when it is kept, last error
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
    created_at = Column(DateTime(timezone=True), default=now, nullable=True)  # older tasks: unknown (the meeting's date)
    done_at = Column(DateTime(timezone=True), nullable=True)  # set when the status becomes done (see below)
    meeting = relationship("Meeting", back_populates="tasks")
    assignee = relationship("Employee", foreign_keys=[assignee_id])
    target_employee = relationship("Employee", foreign_keys=[target_employee_id])


TASK_DONE = ("done", "assistant_done")


@event.listens_for(Task.status, "set")
def _task_done_at(task, value, old, initiator):
    """When a task was done: for «выполнено в срок» of the tables (№27). Reopened, it is not done any more."""
    if value in TASK_DONE:
        if old not in TASK_DONE or task.done_at is None:
            task.done_at = now()
    elif old in TASK_DONE:
        task.done_at = None


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
    entity_id = Column(Integer, ForeignKey("entities.id", ondelete="SET NULL"), nullable=True, index=True)  # a client/object


class PersonMemory(Base):
    """A rolling summary of everything the AI employee knows about one person it talks to (key: Telegram user id)."""
    __tablename__ = "person_memory"
    chat_id = Column(String(40), primary_key=True)
    employee_id = Column(Integer, ForeignKey("employees.id", ondelete="SET NULL"), nullable=True)
    summary = Column(Text, default="")
    updated_at = Column(DateTime(timezone=True), default=now, onupdate=now)


class ChatOrder(Base):
    """An order given in the chat («поручи Виктору купить перфоратор до пятницы»), retold to the person and waiting for
    their «да». Confirmed, it becomes a case (orders.py); a correction replaces it with a new one."""
    __tablename__ = "chat_orders"
    id = Column(Integer, primary_key=True)
    chat_id = Column(String(40), nullable=False, index=True)
    requested_by = Column(String(120), default="")
    employee_id = Column(Integer, ForeignKey("employees.id", ondelete="SET NULL"), nullable=True)  # who gave it
    payload = Column(JSON, nullable=False, default=dict)  # the checked `order` action: kind, to, title, deadline...
    status = Column(String(12), nullable=False, default="pending", index=True)  # pending | started | cancelled | replaced
    case_id = Column(Integer, ForeignKey("cases.id", ondelete="SET NULL"), nullable=True)
    task_id = Column(Integer, ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), default=now)
    decided_at = Column(DateTime(timezone=True), nullable=True)


class DialogMemory(Base):
    """A short retelling of the older part of one conversation (dialog_memory.py); the fresh part goes to the model word
    for word. `upto_id` is the last conversation message the retelling covers."""
    __tablename__ = "dialog_memory"
    chat_id = Column(String(40), primary_key=True)
    summary = Column(Text, default="")
    upto_id = Column(Integer, default=0)
    messages = Column(Integer, default=0)
    error = Column(Text, default="")
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


class AgentCheckRun(Base):
    """A run of the checking dialogs (agent_checks.py): which models and ways of asking were compared, how it ended."""
    __tablename__ = "agent_check_runs"
    id = Column(Integer, primary_key=True)
    created_at = Column(DateTime(timezone=True), default=now)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    status = Column(String(12), nullable=False, default="running")  # running | done | stopped | failed | interrupted
    models = Column(JSON, nullable=False, default=list)
    transports = Column(JSON, nullable=False, default=list)
    total = Column(Integer, nullable=False, default=0)
    by = Column(String(120), default="")
    note = Column(Text, default="")


class AgentCheckResult(Base):
    """One checking dialog answered by one model in one way: passed or why not, what it cost."""
    __tablename__ = "agent_check_results"
    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("agent_check_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    model = Column(String(120), nullable=False)
    transport = Column(String(10), nullable=False)
    case_key = Column(String(60), nullable=False)  # a built-in scenario id or "case:<AgentCheckCase.id>"
    topic = Column(String(60), default="")
    ok = Column(Boolean, nullable=False, default=False)
    reasons = Column(JSON, nullable=False, default=list)
    reply = Column(Text, default="")
    actions = Column(JSON, nullable=False, default=list)
    via = Column(String(10), default="")  # the way that answered (a model without function calls answers in JSON)
    cost = Column(Float, nullable=False, default=0.0)
    calls = Column(Integer, nullable=False, default=0)
    tokens = Column(Integer, nullable=False, default=0)
    seconds = Column(Float, nullable=False, default=0.0)
    created_at = Column(DateTime(timezone=True), default=now)


class AgentCheckCase(Base):
    """A bad answer from a real conversation kept as a checking dialog, with how it should have been."""
    __tablename__ = "agent_check_cases"
    id = Column(Integer, primary_key=True)
    created_at = Column(DateTime(timezone=True), default=now)
    title = Column(String(200), default="")
    text = Column(Text, nullable=False, default="")
    ctx = Column(JSON, nullable=False, default=dict)      # what the model saw (rebuilt when the answer was marked)
    person = Column(JSON, nullable=False, default=dict)   # employee_id, employee_name, reviewer, full, director
    expected = Column(Text, nullable=False, default="")   # «как надо было», in the director's words
    must = Column(JSON, nullable=False, default=list)     # skills that had to be used
    must_not = Column(JSON, nullable=False, default=list)  # skills that must not be used
    bad_reply = Column(Text, default="")
    source_message_id = Column(Integer, nullable=True)
    by = Column(String(120), default="")
    active = Column(Boolean, nullable=False, default=True)


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
    escalated_to = Column(JSON, nullable=True)   # the chats told about the silence: their answer decides (silence_choice)
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
    # What doc_intake.py found in it: the type, the requisites with their quotes, the deadlines, the problems with the
    # sums, the route by the rules and what was done (see doc_intake.py for the shape)
    facts = Column(JSON, nullable=True)
    facts_status = Column(String(12), default="")  # "" not yet | working | done | failed | skipped | old (before №18)
    created_at = Column(DateTime(timezone=True), default=now, index=True)
    employee = relationship("Employee")


class DocRule(Base):
    """«Что кому»: an incoming document of this type (and with these words) goes to the person or to the head of the
    department; its deadlines become their tasks with reminders (doc_intake.py). The first active rule that fits wins."""
    __tablename__ = "doc_rules"
    id = Column(Integer, primary_key=True)
    kind = Column(String(20), default="")          # doc_intake.KINDS key; "" = any type
    keywords = Column(String(300), default="")     # any of these words in the file (comma-separated); "" = no condition
    to_employee_id = Column(Integer, ForeignKey("employees.id", ondelete="SET NULL"), nullable=True)
    to_department_id = Column(Integer, ForeignKey("departments.id", ondelete="SET NULL"), nullable=True)
    make_tasks = Column(Boolean, nullable=False, default=True)
    active = Column(Boolean, nullable=False, default=True)
    position = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), default=now)
    updated_by = Column(String(120), default="")


class Entity(Base):
    """A client, an object or another organization the company works with (entities.py, №19): one card for «Криница»,
    «ОАО «Криница»» and «Криницы». A merged duplicate points to the card it went into."""
    __tablename__ = "entities"
    id = Column(Integer, primary_key=True)
    kind = Column(String(12), nullable=False, default="org")  # object | client | contractor | org
    name = Column(String(300), nullable=False)
    aliases = Column(JSON, nullable=False, default=list)       # other spellings: «ОАО Криница», «Криница-Минск»
    unp = Column(String(20), default="")
    address = Column(String(300), default="")
    contacts = Column(Text, default="")
    description = Column(Text, default="")
    sources = Column(JSON, nullable=True)  # where a field came from: {"unp": {"type": "document", "id": 5, "quote": "…"}}
    source = Column(String(20), default="manual")              # how the card appeared: manual | meeting | document
    active = Column(Boolean, nullable=False, default=True)
    merged_into_id = Column(Integer, ForeignKey("entities.id", ondelete="SET NULL"), nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), default=now)
    updated_at = Column(DateTime(timezone=True), default=now, onupdate=now)
    updated_by = Column(String(120), default="")


class EntityLink(Base):
    """Where a client or an object comes up: a meeting, a task, a document, a message — with the words."""
    __tablename__ = "entity_links"
    __table_args__ = (UniqueConstraint("entity_id", "source_type", "source_id"),)
    id = Column(Integer, primary_key=True)
    entity_id = Column(Integer, ForeignKey("entities.id", ondelete="CASCADE"), nullable=False, index=True)
    source_type = Column(String(12), nullable=False)  # meeting | task | document | message
    source_id = Column(Integer, nullable=False)
    quote = Column(Text, default="")
    created_at = Column(DateTime(timezone=True), default=now)


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


class DocTemplate(Base):
    """A DOCX template of the company (a letter, an act, a memo) with fields {{…}}: the assistant fills it on request
    (doc_templates.py). fields: [{"name", "hint"}] in the order of the document; output: docx | pdf | both."""
    __tablename__ = "doc_templates"
    id = Column(Integer, primary_key=True)
    title = Column(String(200), nullable=False)
    description = Column(Text, default="")  # when to use it, for the assistant
    filename = Column(String(255), default="")
    data = Column(LargeBinary, nullable=False)
    fields = Column(JSON, nullable=False, default=list)
    output = Column(String(8), nullable=False, default="docx")
    active = Column(Boolean, nullable=False, default=True)
    uses = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), default=now)
    updated_at = Column(DateTime(timezone=True), default=now, onupdate=now)
    updated_by = Column(String(120), default="")


class DocDraft(Base):
    """A document filled from a template for a person who asked: the draft goes to them first; after their «ок» it is
    kept in the document library and sent to send_to, if they named somebody."""
    __tablename__ = "doc_drafts"
    id = Column(Integer, primary_key=True)
    template_id = Column(Integer, ForeignKey("doc_templates.id", ondelete="SET NULL"), nullable=True, index=True)
    chat_id = Column(String(40), nullable=False, index=True)
    requested_by = Column(String(120), default="")
    employee_id = Column(Integer, ForeignKey("employees.id", ondelete="SET NULL"), nullable=True)
    title = Column(String(300), default="")
    values = Column(JSON, nullable=False, default=dict)
    send_to_id = Column(Integer, ForeignKey("employees.id", ondelete="SET NULL"), nullable=True)
    # draft | replaced | cancelled | approved (kept, not sent yet) | sent | kept (approved, nobody to send to)
    status = Column(String(12), nullable=False, default="draft", index=True)
    docx = Column(LargeBinary, nullable=True)
    pdf = Column(LargeBinary, nullable=True)
    shown_at = Column(DateTime(timezone=True), nullable=True)   # the draft file went to the person who asked
    document_ids = Column(JSON, nullable=True)                  # the library entries after «ок»
    error = Column(Text, default="")
    created_at = Column(DateTime(timezone=True), default=now)
    decided_at = Column(DateTime(timezone=True), nullable=True)


class SkillRun(Base):
    """One use of a built-in skill of the AI employee (the Skills page counts them): done, sent for approval or as a
    suggestion, cancelled (the request declined, the order answered «нет») or failed."""
    __tablename__ = "skill_runs"
    id = Column(Integer, primary_key=True)
    name = Column(String(40), nullable=False, index=True)
    outcome = Column(String(12), nullable=False)  # done | approval | suggested | cancelled | error
    detail = Column(Text, default="")
    chat = Column(String(64), default="")
    created_at = Column(DateTime(timezone=True), default=now, index=True)


class MailMessage(Base):
    """One letter of the connected mailbox (mail.py, «Почтальон» №22–25): what came, its attachments in «Документы»,
    the type, the client or object and the case it belongs to (№23), who it went to and the tasks it gave (№24), the
    draft of the answer (№25). Our own answers sent through SMTP are kept here too (direction «out»)."""
    __tablename__ = "mail_messages"
    __table_args__ = (UniqueConstraint("mailbox", "folder", "uidvalidity", "uid"),)
    id = Column(Integer, primary_key=True)
    mailbox = Column(String(320), nullable=False, default="")    # the address of the box
    folder = Column(String(200), nullable=False, default="INBOX")
    uidvalidity = Column(String(40), nullable=False, default="")
    uid = Column(Integer, nullable=False, default=0)               # 0 for our own answers
    direction = Column(String(4), nullable=False, default="in")   # in | out
    message_id = Column(String(500), default="", index=True)       # the Message-ID header: the same letter is kept once
    in_reply_to = Column(String(500), default="")
    refs = Column(Text, default="")                                # the References header
    thread = Column(String(500), default="", index=True)           # the first Message-ID of the conversation
    from_addr = Column(String(320), default="", index=True)
    from_name = Column(String(300), default="")
    reply_to = Column(String(320), default="")
    to_addrs = Column(Text, default="")
    cc_addrs = Column(Text, default="")
    subject = Column(String(1000), default="")
    sent_at = Column(DateTime(timezone=True), nullable=True)       # the Date header
    text = Column(Text, default="")                                # the body as text (HTML turned into text)
    size = Column(Integer, nullable=False, default=0)
    signals = Column(JSON, nullable=True)                          # bulk mail, auto-reply… (headers that tell the kind)
    attachments = Column(JSON, nullable=True)                      # [{"filename", "mime", "size", "document_id", "skipped"}]
    kind = Column(String(20), default="")                          # the type (mail.kinds()); "" = not sorted yet
    entity_id = Column(Integer, ForeignKey("entities.id", ondelete="SET NULL"), nullable=True, index=True)
    case_id = Column(Integer, ForeignKey("cases.id", ondelete="SET NULL"), nullable=True, index=True)
    task_id = Column(Integer, ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True, index=True)
    labels = Column(JSON, nullable=True)                           # ["срочно", "ждёт ответа", "срок 05.12"]
    sort = Column(JSON, nullable=True)                             # what the sorting found and why (mail.py)
    sort_status = Column(String(12), nullable=False, default="")   # "" not yet | working | done | failed | skipped
    checked = Column(JSON, nullable=True)                          # the panel: {"ok", "kind", "entity_id", "was", "by", "at"}
    route = Column(JSON, nullable=True)                            # who it went to and the tasks it gave (№24)
    draft = Column(JSON, nullable=True)                            # the answer being prepared (№25)
    place = Column(JSON, nullable=True)                            # the folder of the box it was moved into (mail_folders.py)
    status = Column(String(12), nullable=False, default="new", index=True)  # new | sorted | routed | answered | done | ignored
    created_at = Column(DateTime(timezone=True), default=now, index=True)


class MailRule(Base):
    """«Кто чем занимается» (mail_route.py, №24): a letter of this type, about this client or object, from these senders
    (addresses or @domains) or with these words goes to the person or to the head of the department; its deadlines
    become their tasks. The first active rule that fits wins."""
    __tablename__ = "mail_rules"
    id = Column(Integer, primary_key=True)
    kind = Column(String(20), default="")          # mail.kinds() key; "" = any type
    entity_id = Column(Integer, ForeignKey("entities.id", ondelete="SET NULL"), nullable=True)
    senders = Column(String(500), default="")      # «ivanov@krinica.by, @monolit.by» (comma-separated); "" = anybody
    keywords = Column(String(300), default="")     # any of these words in the subject or the text; "" = no condition
    to_employee_id = Column(Integer, ForeignKey("employees.id", ondelete="SET NULL"), nullable=True)
    to_department_id = Column(Integer, ForeignKey("departments.id", ondelete="SET NULL"), nullable=True)
    make_tasks = Column(Boolean, nullable=False, default=True)
    active = Column(Boolean, nullable=False, default=True)
    position = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), default=now)
    updated_by = Column(String(120), default="")


class MailDraft(Base):
    """An answer to a letter (mail_reply.py, №25): the person who handles the letter reads it in Telegram, asks to
    change it or says «ок» — only then it goes out through SMTP. A newer draft for the same person replaces the older."""
    __tablename__ = "mail_drafts"
    id = Column(Integer, primary_key=True)
    mail_id = Column(Integer, ForeignKey("mail_messages.id", ondelete="CASCADE"), nullable=False, index=True)
    chat_id = Column(String(64), default="", index=True)       # the conversation that decides ("" = by the employee)
    employee_id = Column(Integer, ForeignKey("employees.id", ondelete="SET NULL"), nullable=True, index=True)
    requested_by = Column(String(120), default="")
    points = Column(Text, default="")                          # what the person asked to say (their words)
    to_addrs = Column(Text, default="")
    subject = Column(String(1000), default="")
    text = Column(Text, default="")                            # the answer as it will go (with the signature)
    warnings = Column(JSON, nullable=True)                     # numbers and dates found nowhere in the sources
    missing = Column(JSON, nullable=True)                      # what the model lacked for a full answer
    version = Column(Integer, nullable=False, default=1)
    # draft | replaced | cancelled | approved (sent right after the reply) | sending | sent | failed
    status = Column(String(12), nullable=False, default="draft", index=True)
    error = Column(Text, default="")
    shown_at = Column(DateTime(timezone=True), nullable=True)
    decided_at = Column(DateTime(timezone=True), nullable=True)
    sent_at = Column(DateTime(timezone=True), nullable=True)
    sent_id = Column(Integer, nullable=True)                   # the MailMessage of the answer that went
    created_at = Column(DateTime(timezone=True), default=now)


class CalendarEvent(Base):
    """A meeting of the company calendar (calendars.py, №26), each recurrence on its own: who was invited is who is
    expected at a recording made at that time. Read again every 15 minutes."""
    __tablename__ = "calendar_events"
    id = Column(Integer, primary_key=True)
    uid = Column(String(300), nullable=False, default="")
    title = Column(String(300), default="")
    location = Column(String(300), default="")
    starts_at = Column(DateTime(timezone=True), nullable=False, index=True)
    ends_at = Column(DateTime(timezone=True), nullable=True)
    organizer = Column(JSON, nullable=True)                  # {"name", "email", "status", "kind"}
    attendees = Column(JSON, nullable=False, default=list)   # [{"name", "email", "status": ACCEPTED | DECLINED…, "kind"}]
    fetched_at = Column(DateTime(timezone=True), default=now)


class BitrixLink(Base):
    """One task or client card of SYSAI and its twin in Bitrix24 (bitrix_sync.py, №28). `base` keeps the values both
    sides had at the last sync: a field that differs from it changed on that side, so neither side's change is lost
    to the other side's old value. kind: task (local_id: Task) | company | contact (local_id: Entity)."""
    __tablename__ = "bitrix_links"
    __table_args__ = (UniqueConstraint("kind", "remote_id"),)
    id = Column(Integer, primary_key=True)
    kind = Column(String(10), nullable=False)
    local_id = Column(Integer, nullable=False, index=True)
    remote_id = Column(String(40), nullable=False)
    base = Column(JSON, nullable=False, default=dict)   # task: title, deadline, status, responsible; card: name, unp, kind
    data = Column(JSON, nullable=True)                  # card: phones, e-mails, site, contact persons of the CRM
    state = Column(String(10), nullable=False, default="ok")  # ok | gone (deleted in Bitrix24)
    error = Column(String(300), default="")
    synced_at = Column(DateTime(timezone=True), default=now)
    created_at = Column(DateTime(timezone=True), default=now)


class DriveFile(Base):
    """A file of the company folder in Google Drive and what it is here (gdrive.py, №28): an incoming file that became
    a document, a document of SYSAI put there, or a template (kept the same both ways). sig: md5 (or version) of the
    Drive file at the last sync; local_sha: the template's bytes at the last sync."""
    __tablename__ = "drive_files"
    id = Column(Integer, primary_key=True)
    file_id = Column(String(120), nullable=True, unique=True)
    kind = Column(String(10), nullable=False)            # inbox | upload | template
    origin = Column(String(6), nullable=False, default="drive")  # drive | panel: where the file appeared first
    name = Column(String(300), default="")
    sig = Column(String(80), default="")
    local_sha = Column(String(64), default="")
    version = Column(Integer, nullable=False, default=0)
    document_id = Column(Integer, ForeignKey("documents.id", ondelete="SET NULL"), nullable=True, index=True)
    template_id = Column(Integer, ForeignKey("doc_templates.id", ondelete="SET NULL"), nullable=True, index=True)
    status = Column(String(10), nullable=False, default="ok")  # ok | failed | skipped | gone
    error = Column(String(300), default="")
    attempts = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), default=now)
    updated_at = Column(DateTime(timezone=True), default=now, onupdate=now)


class ServerRun(Base):
    """One start of the service process (server_watch.py, №29): its start, its last sign of life (a heartbeat every
    minute), its clean stop and its memory peak. A gap between one run's last sign of life and the next run's start is
    downtime; a run that ended without a clean stop was killed (most often for memory)."""
    __tablename__ = "server_runs"
    id = Column(Integer, primary_key=True)
    instance = Column(String(12), nullable=False, unique=True)
    platform = Column(String(10), default="")             # render | docker | server
    commit = Column(String(12), default="")
    started_at = Column(DateTime(timezone=True), nullable=False, default=now, index=True)
    beat_at = Column(DateTime(timezone=True), nullable=False, default=now)
    stopped_at = Column(DateTime(timezone=True), nullable=True)
    limit_mb = Column(Integer, nullable=True)             # the memory the process may use (container limit or RAM)
    peak_mb = Column(Integer, nullable=True)              # the peak of the process itself
    child_mb = Column(Integer, nullable=True)             # the peak of its biggest child process (ffmpeg)
    oom_kills = Column(Integer, nullable=False, default=0)  # processes the kernel killed for memory in the container
    busy = Column(String(200), default="")                # what it was doing at the last heartbeat


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
    from .net import models as _net_models  # noqa: F401  the net_* tables of the agent network (app/net)
    Base.metadata.create_all(engine)
    _add_column_if_missing("meetings", "options", "options JSON")
    _add_column_if_missing("meetings", "waveform", "waveform JSON")
    _add_column_if_missing("meetings", "recording_status", "recording_status VARCHAR(12) DEFAULT ''")
    _add_column_if_missing("meetings", "recording", "recording JSON")
    _add_column_if_missing("meetings", "transcript_auto", "transcript_auto JSON")
    _add_column_if_missing("meetings", "transcript_edits", "transcript_edits JSON")
    _add_column_if_missing("meetings", "diarization_reference", "diarization_reference JSON")
    _add_column_if_missing("meetings", "participants", "participants JSON")
    _add_column_if_missing("deliveries", "cycle_attempts", "cycle_attempts INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing("tasks", "executor", "executor VARCHAR(12) DEFAULT 'person'")
    _add_column_if_missing("tasks", "target_employee_id", "target_employee_id INTEGER REFERENCES employees(id)")
    _add_column_if_missing("tasks", "target_employee_name", "target_employee_name VARCHAR(200) DEFAULT ''")
    _add_column_if_missing("tasks", "action_type", "action_type VARCHAR(12) DEFAULT ''")
    _add_column_if_missing("tasks", "counterparts", "counterparts JSON")
    _add_column_if_missing("tasks", "routing_note", "routing_note VARCHAR(500) DEFAULT ''")
    _add_column_if_missing("employees", "telegram_user_id", "telegram_user_id VARCHAR(40) DEFAULT ''")
    _add_column_if_missing("recordings", "device_label", "device_label VARCHAR(120) NOT NULL DEFAULT ''")
    _add_column_if_missing("mail_messages", "place", "place JSON")
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
    _add_column_if_missing("documents", "facts", "facts JSON")
    _add_column_if_missing("notes", "entity_id", "entity_id INTEGER REFERENCES entities(id) ON DELETE SET NULL")
    # The files already in the library are not routed by the new rules: they are marked «old» once (new rows get "")
    _add_column_if_missing("documents", "facts_status", "facts_status VARCHAR(12) DEFAULT 'old'")
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
    _add_column_if_missing("employees", "voice_memory_consent_at", "voice_memory_consent_at TIMESTAMP")
    _add_column_if_missing("employees", "voice_memory_consent_by", "voice_memory_consent_by VARCHAR(120) DEFAULT ''")
    _add_column_if_missing("employees", "order_scope", "order_scope VARCHAR(12) DEFAULT ''")
    _add_column_if_missing("employees", "skills_allowed", "skills_allowed JSON")
    for table in ("employees", "departments"):
        _add_column_if_missing(table, "silence_contact_id", "silence_contact_id INTEGER")
        _add_column_if_missing(table, "silence_hours", "silence_hours INTEGER")
        _add_column_if_missing(table, "silence_reminders", "silence_reminders INTEGER")
    _add_column_if_missing("employees", "address_as", "address_as VARCHAR(120) DEFAULT ''")
    _add_column_if_missing("employees", "address_form", "address_form VARCHAR(3) DEFAULT ''")
    _add_column_if_missing("case_waits", "escalated_to", "escalated_to JSON")
    _add_column_if_missing("employees", "email", "email VARCHAR(200) DEFAULT ''")
    _add_column_if_missing("tasks", "created_at", "created_at TIMESTAMP")
    _add_column_if_missing("tasks", "done_at", "done_at TIMESTAMP")
    _ensure_voice_sample_constraints()
    _forget_pyannote()


def _forget_pyannote():
    """pyannoteAI is removed: its voice templates and key are of no use and are not kept (they are personal data)."""
    with engine.begin() as con:
        gone = con.execute(text("DELETE FROM employee_voiceprints")).rowcount or 0
        con.execute(text("DELETE FROM settings WHERE key = 'pyannoteai_api_key'"))
    if gone:
        log.info("Deleted %s voice templates of the removed pyannoteAI identification", gone)


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
