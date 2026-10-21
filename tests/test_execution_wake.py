"""Real OpenClaw wake contract, with offline transport and durable DB claims."""
import datetime as dt
from types import SimpleNamespace

import httpx
import pytest

from test_coordinator import pilot, enroll, question, reply, view
from app import db
from app.coordinator import service as c, wake
from app.coordinator.models import Run, Lease


@pytest.fixture
def gateway(monkeypatch):
    monkeypatch.setenv("SYSAI_OPENCLAW_ENABLED","1")
    monkeypatch.setenv("SYSAI_OPENCLAW_URL","https://sysai-openclaw-agent.onrender.com")
    monkeypatch.setenv("SYSAI_OPENCLAW_TOKEN","offline-openclaw-placeholder-token-123456")
    calls=[]
    monkeypatch.setattr(wake.httpx,"post",lambda url,**kwargs:calls.append((url,kwargs)) or httpx.Response(200,json={"output":[]}))
    return calls


def expire(run):
    with db.SessionLocal() as s:
        s.get(Lease,"wake:"+run).expires_at=db.now()-dt.timedelta(seconds=1);s.commit()


def test_disabled_wake_no_network(pilot,gateway,monkeypatch):
    enroll(pilot)
    monkeypatch.setenv("SYSAI_OPENCLAW_ENABLED","0")
    assert wake.wake_one()["woken"]==0 and not gateway
    wake.run_pending()


@pytest.mark.parametrize("origin",["http://unsafe", "https://host/path", "https://u:p@host", "https://host?secret=x", ""])
def test_wake_origin_strict_and_credentials_not_logged(pilot,gateway,monkeypatch,origin):
    enroll(pilot);monkeypatch.setenv("SYSAI_OPENCLAW_URL",origin)
    result=wake.wake_one()
    assert result["woken"]==0 and not gateway and "secret=x" not in str(result)


def test_gateway_token_required(pilot,gateway,monkeypatch):
    enroll(pilot);monkeypatch.setenv("SYSAI_OPENCLAW_TOKEN","")
    assert wake.wake_one()["woken"]==0 and not gateway


def test_actual_responses_wake_contract_and_per_run_backoff(pilot,gateway):
    run=enroll(pilot)
    assert wake.wake_one()["woken"]==1
    url,request=gateway[0]
    assert url=="https://sysai-openclaw-agent.onrender.com/v1/responses"
    assert request["json"]["model"]=="openclaw:main"
    assert request["json"]["max_output_tokens"]==2048 and request["timeout"]==25
    assert request["json"]["user"]=="sysai-task-"+run
    assert "claim_planning_turn" in request["json"]["input"] and run in request["json"]["input"]
    assert wake.wake_one()["woken"]==0 and len(gateway)==1
    assert view(run)["state"]=="queued"  # gateway response is NOT task fulfillment
    assert view(run)["wake"]["attempts"]==1


@pytest.mark.parametrize("failure",["timeout","503"])
def test_wake_failure_visible_durable_and_sanitized(pilot,gateway,monkeypatch,failure):
    run=enroll(pilot)
    def failed(*args,**kwargs):
        if failure=="timeout":raise httpx.ReadTimeout("secret token payload")
        return httpx.Response(503,text="secret token payload")
    monkeypatch.setattr(wake.httpx,"post",failed)
    result=wake.wake_one()
    assert result["woken"]==1 and "secret" not in result["error"]
    assert view(run)["wake"]["status"]=="backoff"
    assert "OpenClaw" in view(run)["reason"] and not pilot["sent"]
    assert wake.wake_one()["woken"]==0


def test_durable_claim_excludes_concurrent_waker_and_bounds_attempts(pilot,gateway):
    run=enroll(pilot)
    assert wake.claim()==run and wake.claim() is None
    for _ in range(2):expire(run);assert wake.wake_one()["woken"]==1
    expire(run);assert wake.wake_one()["woken"]==0
    assert view(run)["state"]=="needs_review" and len(gateway)==2


def test_correlated_reply_wakes_same_task_session(pilot,gateway):
    run=enroll(pilot);question(run);c.dispatch(run)
    assert wake.wake_one()["woken"]==0  # awaiting_reply is durable, no blocked waiting loop
    reply(run)
    assert wake.wake_one()["run_id"]==run
    assert gateway[0][1]["json"]["user"]=="sysai-task-"+run


def test_dryrun_prepared_does_not_self_ping(pilot,gateway):
    run=enroll(pilot);question(run);c.configure({"dry_run":True})
    assert wake.wake_one()["woken"]==0 and not gateway


def test_revoked_role_held_before_wake(pilot,gateway):
    run=enroll(pilot)
    with db.SessionLocal() as s:s.get(db.User,1).status="disabled";s.commit()
    assert wake.wake_one()["woken"]==0 and not gateway
    assert view(run)["state"]=="needs_review"


def test_scheduler_submits_nonblocking_single_future(pilot,gateway,monkeypatch):
    calls=[]
    future=SimpleNamespace(done=lambda:False)
    pool=SimpleNamespace(submit=lambda fn:calls.append(fn) or future)
    monkeypatch.setattr(wake,"POOL",pool);monkeypatch.setattr(wake,"_FUTURE",None)
    wake.run_pending();wake.run_pending()
    assert calls==[wake.wake_one]
