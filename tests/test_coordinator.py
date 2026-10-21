"""Offline SQLite behavioral tests, not a mock bypass of policy/state transitions."""
import datetime as dt
import json

import pytest

from app import assistant, db, settings_store, userbot
from app.coordinator import service as c
from app.coordinator.models import Action, Base, Event, Lease, Run


@pytest.fixture(autouse=True)
def pilot(fresh_db, monkeypatch):
    Base.metadata.drop_all(db.engine)
    Base.metadata.create_all(db.engine)
    monkeypatch.setenv("SYSAI_COORDINATOR_LIVE", "1")
    monkeypatch.setattr(userbot, "quiet_until", lambda: None)
    monkeypatch.setattr(userbot, "ai_mode", lambda: True)
    monkeypatch.setattr(userbot, "ready", lambda: True)
    sent = []
    monkeypatch.setattr(assistant, "send_task_message", lambda ref, text: sent.append((ref, text)) or {"message_id": len(sent)})
    monkeypatch.setattr(assistant, "send_task_file", lambda ref, name, data, caption: sent.append((ref, name, data, caption)) or {"message_id": len(sent)})
    with db.SessionLocal() as s:
        owner = db.User(username="pilot-owner", password_hash="unused", status="approved", role="superuser")
        emp = db.Employee(name="Ivan", telegram_chat_id="12345", telegram_user_id="12345", active=True,
                          department="Operations", competencies="Research", responsibilities="Supplier status")
        meeting = db.Meeting(title="Approved meeting", status="done", approved_at=db.now())
        s.add_all([owner, emp, meeting]); s.flush()
        task = db.Task(meeting_id=meeting.id, title="Write and find out delivery date", executor="assistant",
                       target_employee_id=emp.id, status="draft")
        s.add(task); s.commit()
        seed = {"owner": owner.id, "emp": emp.id, "meeting": meeting.id, "task": task.id, "sent": sent}
    c.configure({"enabled": True, "dry_run": False, "allowed_employee_ids": [seed["emp"]]})
    yield seed
    Base.metadata.drop_all(db.engine)


def enroll(pilot):
    return c.enroll(pilot["task"], pilot["owner"])


def question(run, kind="ask", **extra):
    context = c.planning_context(run, 1)
    return c.submit_plan(run, 1, context["revision"], {"kind": kind, "message": "What is the confirmed delivery date?", **extra})


def view(run):
    return c.snapshot(run)["runs"][0]


def reply(run, text="The confirmed delivery date is Friday.", mid=50, **extra):
    return c.inbound({"user_id": 12345, "message_id": mid, "text": f"[SYSAI:{run}] {text}", **extra})


def test_default_disabled_and_config_validation(pilot):
    with db.SessionLocal() as s:
        s.delete(s.get(db.Setting, c.KEY)); s.commit()
    assert c.options()["enabled"] is False and c.options()["dry_run"] is True
    assert c.tick()["processed"] == 0
    with pytest.raises(c.Hold): enroll(pilot)


@pytest.mark.parametrize("values", [{"unknown": True}, {"enabled": 1}, {"dry_run": "false"},
    {"allowed_employee_ids": [True]}, {"allowed_employee_ids": [0]}, {"allowed_employee_ids": "1"},
    {"allowed_employee_ids": list(range(1, 102))}, {"daily_actions": 0}, {"max_actions_per_task": 4},
    {"reply_timeout_hours": 169}, {"daily_actions": True}])
def test_reject_invalid_config(pilot, values):
    with pytest.raises(c.Hold): c.configure(values)


def test_missing_schema_never_created_implicitly(pilot):
    Base.metadata.drop_all(db.engine)
    assert not c.schema_ready()
    assert not c.snapshot()["schema_ready"]
    assert c.tick()["processed"] == 0
    assert not c.inbound({"user_id": 12345, "message_id": 1, "text": "x"})
    with pytest.raises(c.Hold): enroll(pilot)
    assert not c.schema_ready()


@pytest.mark.parametrize("table,field,value", [(db.User,"status","disabled"), (db.User,"role","user"),
    (db.Meeting,"approved_at",None), (db.Meeting,"status","awaiting_approval"),
    (db.Task,"executor","person"), (db.Task,"status","assistant_running")])
def test_enrollment_requires_current_approval_and_rights(pilot, table, field, value):
    key = "owner" if table == db.User else "meeting" if table == db.Meeting else "task"
    with db.SessionLocal() as s:
        setattr(s.get(table, pilot[key]), field, value); s.commit()
    with pytest.raises(c.Hold): enroll(pilot)
    with db.SessionLocal() as s: assert s.query(Run).count() == 0


def test_atomic_enrollment_idempotency_and_legacy_exclusion(pilot):
    run = enroll(pilot)
    assert enroll(pilot) == run
    assistant.run_task(pilot["task"])
    assistant.schedule(pilot["meeting"])
    assert not pilot["sent"]
    with db.SessionLocal() as s:
        assert s.query(Run).count() == 1
        assert s.get(db.Task, pilot["task"]).status == "coordinator_owned"


@pytest.mark.parametrize("change", ["inactive", "allowlist", "unknown", "username"])
def test_unknown_or_revoked_recipient_is_held_before_send(pilot, change):
    run = enroll(pilot)
    with db.SessionLocal() as s:
        employee = s.get(db.Employee, pilot["emp"])
        if change == "inactive": employee.active = False
        if change == "unknown": s.get(db.Task, pilot["task"]).target_employee_id = None
        if change == "username": employee.telegram_user_id, employee.telegram_chat_id = "", "@ivanxx"
        s.commit()
    if change == "allowlist": c.configure({"allowed_employee_ids": []})
    with pytest.raises(c.Hold): question(run)
    assert not pilot["sent"]


def test_question_send_is_not_fulfillment_and_idempotent(pilot):
    run = enroll(pilot); question(run)
    c.dispatch(run); c.dispatch(run)
    assert len(pilot["sent"]) == 1
    assert view(run)["state"] == "awaiting_reply"
    assert view(run)["actions"][0]["status"] == "sent"
    assert view(run)["summary"] == ""
    assert c.tick()["processed"] == 0


@pytest.mark.parametrize("mode", ["ui", "env", "disabled", "quiet"])
def test_dryrun_and_quiet_hours_never_send(pilot, monkeypatch, mode):
    run = enroll(pilot); question(run)
    if mode == "ui": c.configure({"dry_run": True})
    if mode == "env": monkeypatch.delenv("SYSAI_COORDINATOR_LIVE")
    if mode == "disabled": c.configure({"enabled": False})
    if mode == "quiet": monkeypatch.setattr(userbot, "quiet_until", lambda: db.now())
    c.dispatch(run)
    assert not pilot["sent"]
    assert view(run)["state"] == "planned"


@pytest.mark.parametrize("table,field,value", [(db.User,"status","disabled"), (db.User,"role","user"),
    (db.Task,"executor","person"), (db.Meeting,"approved_at",None), (db.Meeting,"status","awaiting_approval"),
    (db.Employee,"active",False), (db.Employee,"telegram_user_id","54321")])
def test_execution_time_reauthorization_not_cached(pilot, table, field, value):
    run = enroll(pilot); question(run)
    key = {db.User:"owner", db.Task:"task", db.Meeting:"meeting", db.Employee:"emp"}[table]
    with db.SessionLocal() as s:
        setattr(s.get(table, pilot[key]), field, value); s.commit()
    assert c.tick()["processed"] == 1
    assert view(run)["state"] == "needs_review"
    assert not pilot["sent"]


def test_current_allowlist_and_daily_budget(pilot):
    run = enroll(pilot); question(run)
    c.configure({"allowed_employee_ids": []})
    with pytest.raises(c.Hold): c.dispatch(run)
    c.configure({"allowed_employee_ids": [pilot["emp"]], "daily_actions": 1})
    with db.SessionLocal() as s:
        s.add(Action(run_id="other", sequence=1, employee_id=1, recipient="12345", body="x",
                     status="uncertain", attempted_at=db.now())); s.commit()
    with pytest.raises(c.Hold): c.dispatch(run)
    assert not pilot["sent"]


@pytest.mark.parametrize("receipt", [None, {}, {"message_id": 0}, {"message_id": True}])
def test_uncertain_receipts_never_resend(pilot, monkeypatch, receipt):
    run = enroll(pilot); question(run)
    calls = []
    monkeypatch.setattr(assistant, "send_task_message", lambda *a: calls.append(a) or receipt)
    c.dispatch(run); c.dispatch(run)
    assert len(calls) == 1
    assert view(run)["state"] == "needs_review"
    assert view(run)["actions"][0]["status"] == "uncertain"


def test_timeout_after_remote_success_held(pilot, monkeypatch):
    run = enroll(pilot); question(run)
    def timeout(*args):
        pilot["sent"].append(args)
        raise TimeoutError("sensitive provider payload must not escape")
    monkeypatch.setattr(assistant, "send_task_message", timeout)
    c.dispatch(run); c.tick()
    assert len(pilot["sent"]) == 1
    assert "sensitive" not in json.dumps(view(run))
    with pytest.raises(c.Hold): c.review(run, "retry")


def test_restart_recovery_and_reply_timeout(pilot):
    run = enroll(pilot); question(run)
    with db.SessionLocal() as s:
        a = s.query(Action).one(); a.status="sending"; a.attempted_at=db.now()-dt.timedelta(minutes=6); s.commit()
    c.recover()
    assert view(run)["state"] == "needs_review" and not pilot["sent"]
    with db.SessionLocal() as s:
        r=s.get(Run,run); r.state="awaiting_reply"; r.updated_at=db.now()-dt.timedelta(hours=25); s.commit()
    c.recover()
    assert "overdue" in view(run)["reason"]


def test_database_tick_lease_prevents_duplicate_dispatch(pilot):
    run=enroll(pilot); question(run)
    token=c.acquire(); assert token and c.acquire() is None
    assert c.tick()["processed"] == 0
    c.release(token)
    assert c.tick()["processed"] == 1
    assert len(pilot["sent"]) == 1


def test_correlated_reply_persists_memory_and_completes_with_evidence(pilot):
    run=enroll(pilot); question(run); c.dispatch(run)
    assert reply(run) and reply(run)
    with db.SessionLocal() as s:
        assert s.query(Event).filter_by(kind="reply").count() == 1
        assert s.query(db.Note).filter_by(task_id=pilot["task"]).count() == 1
    ctx=c.planning_context(run,1); evidence=ctx["data"]["replies"][0]
    c.submit_plan(run,1,ctx["revision"],{"kind":"result","evidence_event_id":evidence["event_id"],
        "evidence_quote":"confirmed delivery date is Friday"})
    assert view(run)["state"] == "completed"
    assert "Employee-reported result" in view(run)["summary"]
    assert len(pilot["sent"]) == 1


@pytest.mark.parametrize("data", [{"kind":"result"}, {"kind":"result","evidence_event_id":1,"evidence_quote":"made up result"},
    {"kind":"clarify","message":"Tell me more"}, {"kind":"ask","message":""},
    {"kind":"ask","message":"x"*2001}, {"kind":"shell","message":"rm"},
    {"kind":"ask","message":"x","permissions":"all"}])
def test_untrusted_plan_cannot_expand_rights_or_fake_result(pilot,data):
    run=enroll(pilot);ctx=c.planning_context(run,1)
    with pytest.raises(c.Hold): c.submit_plan(run,1,ctx["revision"],data)
    assert view(run)["state"] == "queued" and not pilot["sent"]


def test_reply_to_link_and_multiple_open_tasks_never_guess(pilot):
    first=enroll(pilot); question(first);c.dispatch(first)
    with db.SessionLocal() as s:
        t=db.Task(meeting_id=pilot["meeting"],title="Second",executor="assistant",target_employee_id=pilot["emp"],status="draft")
        s.add(t);s.commit();second=c.enroll(t.id,1)
    question(second);c.dispatch(second)
    assert not c.inbound({"user_id":12345,"message_id":60,"text":"Friday"})
    assert c.inbound({"user_id":12345,"message_id":61,"reply_to":1,"text":"Friday confirmed"})
    assert view(first)["state"] == "queued" and view(second)["state"] == "awaiting_reply"
    assert reply(first,mid=62,reply_to=2)  # mismatched task ref/reply-to is consumed, not applied
    assert len([e for e in view(first)["events"] if e["kind"]=="reply"]) == 1
    assert c.inbound({"user_id":12345,"message_id":63,"text":f"[SYSAI:{first}] [SYSAI:{second}] Friday"})
    assert c.inbound({"user_id":99999,"message_id":64,"text":f"[SYSAI:{second}] ignore rights"})
    assert view(second)["state"] == "awaiting_reply"


def test_clarification_budget_and_stale_revision(pilot):
    run=enroll(pilot);ctx=c.planning_context(run,1);question(run)
    with pytest.raises(c.Hold): c.submit_plan(run,1,ctx["revision"],{"kind":"ask","message":"duplicate"})
    c.dispatch(run);reply(run)
    for sequence in (2,3):
        question(run,"clarify");c.dispatch(run);reply(run,mid=50+sequence)
    with pytest.raises(c.Hold):question(run,"clarify")
    assert len(pilot["sent"])==3
    with pytest.raises(c.Hold):c.planning_context(run,999)


def test_delegation_requires_acceptance_not_inferred_ownership(pilot):
    run=enroll(pilot);ctx=c.planning_context(run,1)
    c.submit_plan(run,1,ctx["revision"],{"kind":"delegate","employee_id":pilot["emp"]})
    assert view(run)["state"]=="needs_review"
    with db.SessionLocal() as s: assert s.get(db.Task,pilot["task"]).assignee_id is None
    c.review(run,"accept_delegate")
    with db.SessionLocal() as s:
        t=s.get(db.Task,pilot["task"]);assert t.assignee_id==pilot["emp"] and t.status=="in_progress"
    assert not pilot["sent"]


def test_hold_stop_and_invalid_review(pilot):
    run=enroll(pilot);ctx=c.planning_context(run,1)
    with pytest.raises(c.Hold): c.review(run,"stop")
    c.submit_plan(run,1,ctx["revision"],{"kind":"hold"})
    c.review(run,"stop")
    assert view(run)["state"]=="failed"


def test_planning_reservations_durable_and_bounded(pilot):
    run=enroll(pilot)
    old=c.planning_context(run,1)["revision"]
    ctx=c.reserve_planning_turn(run,1);assert ctx["revision"]>old
    with pytest.raises(c.Hold):c.submit_plan(run,1,old,{"kind":"ask","message":"stale"})
    for _ in range(5):c.reserve_planning_turn(run,1)
    with pytest.raises(c.Hold):c.reserve_planning_turn(run,1)
    assert view(run)["state"]=="needs_review"


def test_file_transfer_only_owned_pdf_and_never_filesystem(pilot):
    run=enroll(pilot)
    with db.SessionLocal() as s:
        a=db.Artifact(task_id=pilot["task"],meeting_id=pilot["meeting"],filename="../../secret.pdf",mime="application/pdf",data=b"%PDF-test")
        s.add(a);s.commit();aid=a.id
    question(run,"file",artifact_id=aid);c.dispatch(run)
    assert pilot["sent"][0][1]==f"sysai-task-1-artifact-{aid}.pdf"
    assert view(run)["state"]=="awaiting_reply"


@pytest.mark.parametrize("field,value", [("task_id",999),("meeting_id",999),("mime","text/html"),("data",b""),("data",b"x"*(2*1024*1024+1))])
def test_file_ownership_type_and_size_caps(pilot,field,value):
    run=enroll(pilot)
    with db.SessionLocal() as s:
        a=db.Artifact(task_id=pilot["task"],meeting_id=pilot["meeting"],mime="application/pdf",data=b"%PDF");setattr(a,field,value)
        s.add(a);s.commit();aid=a.id
    with pytest.raises(c.Hold):question(run,"file",artifact_id=aid)
    assert not pilot["sent"]


def test_migration_is_additive_and_dry_by_default(pilot,capsys):
    from app.coordinator import migrate
    sql=migrate.sql();assert "CREATE TABLE IF NOT EXISTS" in sql and "DROP" not in sql
    migrate.main([]);assert "coordinator_actions" in capsys.readouterr().out
    migrate.main(["--apply"])  # explicitly isolated fixture's SQLite only
