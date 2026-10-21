"""Exercise the actual maintained MCP protocol in-process; no network/provider calls."""
import asyncio
import json
import os
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from test_coordinator import pilot, enroll, question, reply, view
from app import db
from app.coordinator import service as c, mcp_server as m

from examples.sysai_mcp_client import MCPGateway

TOKEN = "offline-test-credential-not-a-secret-12345"


@pytest.fixture
def credentials(monkeypatch):
    monkeypatch.setenv("SYSAI_MCP_TOKEN", TOKEN)
    monkeypatch.setenv("SYSAI_MCP_USER_ID", "1")
    monkeypatch.setenv("SYSAI_MCP_SCOPES", "read,plan,send")
    monkeypatch.setenv("SYSAI_AGENT_ALLOW_LOCALHOST", "1")


def test_protocol_initialize_tools_and_complete_vertical_slice(pilot, credentials):
    from app.main import app
    async def scenario():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver",
                                        headers={"Authorization":"Bearer "+TOKEN}) as http:
                async with MCPGateway("http://testserver/execution/mcp", TOKEN, http) as remote:
                    tools = await remote.session.list_tools()
                    names={t.name for t in tools.tools}
                    assert names == {"list_approved_tasks","list_runs","enroll_task","get_task_context", "prepare_task_action",
                                     "dispatch_task_action","get_correlated_replies","claim_planning_turn","recover_execution"}
                    assert (await remote.call("list_approved_tasks"))["tasks"][0]["id"]==pilot["task"]
                    run=(await remote.call("enroll_task",{"task_id":pilot["task"]}))["run_id"]
                    context=await remote.call("claim_planning_turn",{"run_id":run})
                    await remote.call("prepare_task_action",{"run_id":run,"revision":context["revision"],
                        "plan":{"kind":"ask","message":"Which delivery date is confirmed?"}})
                    result=await remote.call("dispatch_task_action",{"run_id":run})
                    assert result["state"]=="awaiting_reply"
                    await remote.call("dispatch_task_action",{"run_id":run})
                    assert len(pilot["sent"])==1
                    assert reply(run)
                    replies=await remote.call("get_correlated_replies",{"run_id":run})
                    assert len(replies["replies"])==1
                    context=await remote.call("get_task_context",{"run_id":run})
                    await remote.call("prepare_task_action",{"run_id":run,"revision":context["revision"],"plan":{
                        "kind":"result","evidence_event_id":replies["replies"][0]["event_id"],
                        "evidence_quote":"confirmed delivery date is Friday"}})
                    assert (await remote.call("list_runs"))["runs"][0]["state"]=="completed"
                    await remote.call("recover_execution")
    asyncio.run(scenario())


def test_mcp_auth_transport_body_cap_and_unsupported_get(pilot, credentials):
    from app.main import app
    with TestClient(app) as client:
        assert client.post("/execution/mcp",json={}).status_code==401
        assert client.post("/execution/mcp",headers={"Authorization":"Bearer bad"},json={}).status_code==401
        headers={"Authorization":"Bearer "+TOKEN,"Accept":"application/json, text/event-stream"}
        assert client.post("/execution/mcp",headers=headers,content="x"*32769).status_code==413
        assert client.get("/execution/mcp",headers=headers).status_code in (405,400)


@pytest.mark.parametrize("scope",["plan","send"])
def test_read_only_token_cannot_execute(pilot,credentials,monkeypatch,scope):
    monkeypatch.setenv("SYSAI_MCP_SCOPES","read")
    with pytest.raises(c.Hold):m.authorize(scope)


@pytest.mark.parametrize("change",["invalid_id","disabled","role"])
def test_tool_current_principal_checks(pilot,credentials,monkeypatch,change):
    if change=="invalid_id":monkeypatch.setenv("SYSAI_MCP_USER_ID","not-an-id")
    else:
        with db.SessionLocal() as s:
            owner=s.get(db.User,1)
            if change=="disabled":owner.status="disabled"
            else:owner.role="user"
            s.commit()
    with pytest.raises(c.Hold):m.list_runs()


def test_sdk_schema_rejects_unknown_plan_tool_arguments(pilot,credentials):
    from app.main import app
    async def scenario():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://testserver",
                headers={"Authorization":"Bearer "+TOKEN}) as http:
                async with MCPGateway("http://testserver/execution/mcp",TOKEN,http) as remote:
                    result=await remote.session.call_tool("prepare_task_action",{"run_id":"x","revision":0,
                        "plan":{"kind":"ask","message":"x","shell":"bad"}})
                    assert result.isError
                    result=await remote.session.call_tool("enroll_task",{"task_id":-1})
                    assert result.isError
                    assert not pilot["sent"]
    asyncio.run(scenario())


def test_missing_schema_read_tools_fail_closed(pilot,credentials):
    from app.coordinator.models import Base
    Base.metadata.drop_all(db.engine)
    assert m.list_runs()["schema_ready"] is False
    assert m.recover_execution()=={"ok":True}


def test_mcp_lease_collision_rejects_dispatch(pilot,credentials):
    run=enroll(pilot);question(run)
    token=c.acquire()
    with pytest.raises(c.Hold):m.dispatch_task_action(run)
    with pytest.raises(c.Hold):m.recover_execution()
    c.release(token)
    assert not pilot["sent"]


def csrf(client):
    import re
    response=client.get("/coordinator")
    assert response.status_code==200
    return re.search('data-csrf="([^"]+)"',response.text).group(1)


def test_ui_optin_csrf_and_diagnostics(pilot,client):
    token=csrf(client)
    assert "Пилот" in client.get("/coordinator").text
    assert client.get("/api/coordinator").status_code==200
    assert client.post("/api/coordinator/config",json={"enabled":False}).status_code==403
    headers={"x-csrf-token":token}
    assert client.post("/api/coordinator/config",headers=headers,json={"dry_run":True}).status_code==200
    response=client.post("/api/coordinator/enroll",headers=headers,json={"task_id":pilot["task"]})
    assert response.status_code==200
    run=response.json()["run_id"]
    assert client.get(f"/api/coordinator/tasks/{run}").json()["runs"][0]["state"]=="queued"
    assert client.post("/api/coordinator/tick",headers=headers,json={}).status_code==200
    assert client.post("/api/coordinator/no-such-tool",headers=headers,json={}).status_code==404
    assert client.post("/api/coordinator/enroll",headers=headers,json={"task_id":"1"}).status_code==400
    assert client.post("/api/coordinator/config",headers=headers,json=[]).status_code==400
    assert client.post("/api/coordinator/config",headers=headers,content="not-json").status_code==400
    assert client.get("/api/coordinator/tick").status_code==405
    with db.SessionLocal() as s:
        r=s.get(__import__('app.coordinator.models',fromlist=['Run']).Run,run)
        c.hold(s,r,"review");s.commit()
    assert client.post("/api/coordinator/review",headers=headers,json={"run_id":run,"decision":"stop"}).status_code==200


def test_ui_unauthenticated_and_nonadmin_denied(pilot):
    from app.main import app
    with TestClient(app) as client:
        assert client.get("/api/coordinator").status_code==403
        assert client.get("/coordinator").status_code==403


def test_optional_future_structured_api_token_is_read_plan_scoped(pilot,client,monkeypatch):
    monkeypatch.setenv("SYSAI_COORDINATOR_API_TOKEN",TOKEN)
    monkeypatch.setenv("SYSAI_COORDINATOR_API_USER_ID","1")
    headers={"Authorization":"Bearer "+TOKEN}
    assert client.get("/api/coordinator",headers=headers).status_code==200
    assert client.post("/api/coordinator/config",headers=headers,json={"enabled":True}).status_code==403
    assert client.post("/api/coordinator/review",headers=headers,json={}).status_code==403
    assert client.get("/api/coordinator",headers={"Authorization":"Bearer wrong"}).status_code==401
    monkeypatch.setenv("SYSAI_COORDINATOR_API_USER_ID","invalid")
    assert client.get("/api/coordinator",headers=headers).status_code==401
    monkeypatch.setenv("SYSAI_COORDINATOR_API_USER_ID","999")
    assert client.get("/api/coordinator",headers=headers).status_code==403
