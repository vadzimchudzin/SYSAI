"""Approval-to-OpenClaw handoff regression tests; no legacy/provider sends."""
import pytest

from test_coordinator import pilot, enroll
from app import assistant, db, settings_store
from app.coordinator import service as c
from app.coordinator.models import Base, Run


@pytest.fixture
def principal(monkeypatch):
    monkeypatch.setenv("SYSAI_MCP_USER_ID", "1")


def test_approval_automatically_hands_assistant_task_to_openclaw(pilot, principal, monkeypatch):
    def legacy_forbidden(*args):
        raise AssertionError("Legacy planner must not execute opted-in tasks")
    monkeypatch.setattr(assistant, "_ask", legacy_forbidden)
    assistant.schedule(pilot["meeting"])
    with db.SessionLocal() as s:
        task=s.get(db.Task,pilot["task"])
        assert task.status=="coordinator_owned"
        run=s.query(Run).filter_by(task_id=task.id).one()
        assert run.state=="queued" and run.owner_id==1
    assert not pilot["sent"]
    assistant.run_task(pilot["task"])
    with db.SessionLocal() as s:assert s.query(Run).count()==1


def test_disabled_preserves_legacy_planning(pilot,monkeypatch):
    c.configure({"enabled":False})
    seen=[]
    def legacy(facts,*args):
        seen.append(facts)
        raise assistant.AssistantError("offline legacy path reached")
    monkeypatch.setattr(assistant,"_ask",legacy)
    monkeypatch.setattr(assistant,"_tell_reviewers",lambda *args:None)
    monkeypatch.setattr(assistant,"_notify_reviewers_text",lambda *args:None)
    assistant.schedule(pilot["meeting"])
    assert len(seen)==1
    with db.SessionLocal() as s:
        assert s.query(Run).count()==0
        assert s.get(db.Task,pilot["task"]).status=="assistant_failed"


def test_owned_task_never_falls_back_after_pilot_disabled(pilot):
    run=enroll(pilot);c.configure({"enabled":False})
    assistant.run_task(pilot["task"])
    with db.SessionLocal() as s:
        assert s.get(db.Task,pilot["task"]).status=="coordinator_owned"
        assert s.get(Run,run).state=="queued"
    assert not pilot["sent"]


@pytest.mark.parametrize("failure",["principal_missing","principal_invalid","role","approval","schema"])
def test_enabled_handoff_failure_never_silently_uses_legacy(pilot,principal,monkeypatch,failure):
    if failure=="principal_missing":monkeypatch.delenv("SYSAI_MCP_USER_ID")
    if failure=="principal_invalid":monkeypatch.setenv("SYSAI_MCP_USER_ID","invalid")
    if failure=="schema":Base.metadata.drop_all(db.engine)
    with db.SessionLocal() as s:
        s.get(db.Task,pilot["task"]).status="assistant_todo"
        if failure=="role":s.get(db.User,1).role="user"
        if failure=="approval":s.get(db.Meeting,pilot["meeting"]).approved_at=None
        s.commit()
    def forbidden(*args):raise AssertionError("Legacy planner must not execute")
    monkeypatch.setattr(assistant,"_ask",forbidden)
    assistant.run_task(pilot["task"])
    with db.SessionLocal() as s:assert s.get(db.Task,pilot["task"]).status=="assistant_failed"
    assert not pilot["sent"]


def test_handoff_cas_loser_does_not_overwrite_winning_run(pilot,principal,monkeypatch):
    actual=c.enroll
    def another_wins(task_id,owner_id):
        actual(task_id,owner_id)
        raise c.Hold("Task already claimed by overlapping approval")
    monkeypatch.setattr(c,"enroll",another_wins)
    assistant.run_task(pilot["task"])
    with db.SessionLocal() as s:
        assert s.query(Run).count()==1
        assert s.get(db.Task,pilot["task"]).status=="coordinator_owned"
    assert not pilot["sent"]


def test_helper_ignores_person_or_missing_tasks_and_does_not_bulk_enroll(pilot,principal):
    with db.SessionLocal() as s:
        person=db.Task(meeting_id=pilot["meeting"],title="Human task",executor="person",status="draft")
        s.add(person);s.commit();pid=person.id
    assert c.maybe_enroll_task(pid) is False
    assert c.maybe_enroll_task(999999) is False
    with db.SessionLocal() as s:assert s.query(Run).count()==0


def test_dryrun_handoff_enrolls_but_does_not_send(pilot,principal):
    c.configure({"dry_run":True})
    assistant.schedule(pilot["meeting"])
    with db.SessionLocal() as s:assert s.query(Run).one().state=="queued"
    assert not pilot["sent"]


def test_authenticated_ui_runtime_readiness_never_exposes_token(pilot,client,monkeypatch):
    secret="offline-sensitive-gateway-placeholder-123456789"
    monkeypatch.setenv("SYSAI_OPENCLAW_URL","https://sysai-openclaw-agent.onrender.com")
    monkeypatch.setenv("SYSAI_OPENCLAW_TOKEN",secret)
    monkeypatch.setenv("SYSAI_OPENCLAW_ENABLED","1")
    response=client.get("/coordinator")
    assert response.status_code==200 and "отдельный OpenClaw" in response.text
    assert "https://sysai-openclaw-agent.onrender.com" in response.text and secret not in response.text
    status=client.get("/api/coordinator").json()
    assert status["runtime"]["enabled"] and status["runtime"]["gateway_token_configured"]
    assert secret not in str(status)
    monkeypatch.setenv("SYSAI_OPENCLAW_URL","https://user:secret@host/path")
    assert c.runtime_readiness()["origin"]==""
