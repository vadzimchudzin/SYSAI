"""Additive pilot tables. Deliberately separate from db.Base: no startup migration."""
from sqlalchemy import JSON, Column, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import declarative_base
from ..db import now

Base = declarative_base()


class Run(Base):
    __tablename__ = "coordinator_runs"
    id = Column(String(32), primary_key=True)
    task_id = Column(Integer, unique=True, nullable=False, index=True)
    owner_id = Column(Integer, nullable=False)
    state = Column(String(24), nullable=False, default="queued", index=True)
    plan = Column(JSON, nullable=False, default=dict)
    summary = Column(Text, nullable=False, default="")
    reason = Column(String(200), nullable=False, default="")
    created_at = Column(DateTime(timezone=True), nullable=False, default=now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=now)


class Action(Base):
    __tablename__ = "coordinator_actions"
    __table_args__ = (UniqueConstraint("run_id", "sequence"),)
    id = Column(Integer, primary_key=True)
    run_id = Column(String(32), nullable=False, index=True)
    sequence = Column(Integer, nullable=False)
    employee_id = Column(Integer, nullable=False)
    recipient = Column(String(40), nullable=False)
    body = Column(Text, nullable=False)
    status = Column(String(24), nullable=False, default="prepared", index=True)
    message_id = Column(Integer, nullable=True)
    artifact_id = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now)
    attempted_at = Column(DateTime(timezone=True), nullable=True)


class Event(Base):
    __tablename__ = "coordinator_events"
    id = Column(Integer, primary_key=True)
    run_id = Column(String(32), nullable=False, index=True)
    kind = Column(String(32), nullable=False)
    data = Column(JSON, nullable=False, default=dict)
    # Telegram message identity or manual operation id; NULL for ordinary audit events.
    external_key = Column(String(100), nullable=True, unique=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now)


class Lease(Base):
    __tablename__ = "coordinator_leases"
    name = Column(String(40), primary_key=True)
    token = Column(String(32), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
