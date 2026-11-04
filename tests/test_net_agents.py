"""Agents in SYSAI (docs/opo/TASKS.md, stage 1): connecting with a one-time code, tokens, switching off."""
import datetime as dt
import re

from app import accounts, settings_store
from app.db import SessionLocal, User
from app.net import agents
from app.net.models import NetAgent, NetAgentAccess, NetAgentToken, NetEvent


def test_switched_off_network_answers_404_and_hides_the_menu(client):
    assert client.get("/agents").status_code == 404
    assert client.get("/hub").status_code == 404
    assert client.get("/agents/access").status_code == 404
    assert client.post("/agents/api/connect", json={"code": "AAAA-BBBB"}).status_code == 404
    assert client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}).status_code == 404
    assert client.get("/v1/models").status_code == 404
    assert client.post("/v1/chat/completions", json={}).status_code == 404
    assert "SYSAI_Hub" not in client.get("/app").text


def test_menu_shows_agents_and_hub_when_on(client, net_on):
    html = client.get("/app").text
    assert 'href="/agents"' in html and 'href="/hub"' in html


def test_connect_with_a_one_time_code(client, net_on):
    from app.db import Department
    with SessionLocal() as s:
        s.add(Department(name="ОПО"))
        s.commit()
        dept = s.query(Department).first().id
        admin = s.query(User).filter(User.role == "superuser").first().id
    r = client.post("/agents/create", data={"user_id": admin, "department_id": dept, "harness": "hermes"},
                    follow_redirects=False)
    assert r.status_code == 303
    agent_url = r.headers["location"].split("?")[0]
    with SessionLocal() as s:
        agent = s.query(NetAgent).one()
        assert agent.name == "opo-sysai" and agent.status == "pending"
        profile = s.query(NetAgentAccess).one().profile_id
        assert profile is not None
    page = client.post(agent_url + "/connect")
    assert page.status_code == 200 and page.headers["cache-control"] == "no-store"
    code = re.search(r'class="net-code">([A-Z2-9]{4}-[A-Z2-9]{4})<', page.text).group(1)
    with SessionLocal() as s:  # only the hash is kept
        assert all(code not in t.token_hash for t in s.query(NetAgentToken))
    answer = client.post("/agents/api/connect", json={"code": code.lower().replace("-", " "), "host": "PC-1",
                                                       "harness": "hermes", "versions": {"hermes": "0.9.1"}})
    assert answer.status_code == 200, answer.text
    data = answer.json()
    assert data["agent"] == "opo-sysai" and data["token"].startswith("sya_") and data["gateway"].endswith("/v1")
    again = client.post("/agents/api/connect", json={"code": code})
    assert again.status_code == 400 and "использован" in again.json()["error"]["message"]
    config = client.get("/agents/api/config", headers={"Authorization": "Bearer " + data["token"]})
    assert config.status_code == 200 and config.json()["doc"]["agent"] == "opo-sysai"
    with SessionLocal() as s:
        agent = s.query(NetAgent).one()
        assert agent.status == "active" and agent.host == "PC-1" and agent.versions == {"hermes": "0.9.1"}
        assert s.query(NetEvent).filter(NetEvent.action == "agent.connect").count() == 1
    listing = client.get("/agents").text
    assert "opo-sysai" in listing and "работает" in listing
    card = client.get(agent_url).text
    assert "PC-1" in card and "approvals" in card and "Инженер ОПО" in card


def test_expired_code_and_unknown_tokens(client, net_agent):
    code = agents.issue_code(net_agent["id"])
    with SessionLocal() as s:
        row = s.query(NetAgentToken).filter(NetAgentToken.kind == "connect", NetAgentToken.revoked_at.is_(None)).one()
        row.expires_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1)
        s.commit()
    r = client.post("/agents/api/connect", json={"code": code})
    assert r.status_code == 400 and "просрочен" in r.json()["error"]["message"]
    assert client.get("/agents/api/config", headers={"Authorization": "Bearer sya_" + "x" * 43}).status_code == 401
    assert client.get("/agents/api/config").status_code == 401
    assert client.get("/agents/api/config", headers=net_agent["headers"]).status_code == 200


def test_a_new_code_replaces_the_old_computer(client, net_agent):
    code = agents.issue_code(net_agent["id"])
    _, token = agents.exchange(code, host="PC-2")
    assert client.get("/agents/api/config", headers=net_agent["headers"]).status_code == 401
    assert client.get("/agents/api/config", headers={"Authorization": f"Bearer {token}"}).status_code == 200


def test_switching_off_stops_mcp_and_the_gateway_at_once(client, net_agent):
    ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    assert client.post("/mcp", json=ping, headers=net_agent["headers"]).status_code == 200
    assert client.post(f"/agents/{net_agent['id']}/disable", follow_redirects=False).status_code == 303
    assert client.post("/mcp", json=ping, headers=net_agent["headers"]).status_code == 401
    assert client.get("/v1/models", headers=net_agent["headers"]).status_code == 401
    with SessionLocal() as s:
        assert s.get(NetAgent, net_agent["id"]).status == "disabled"
    assert client.post(f"/agents/{net_agent['id']}/connect", follow_redirects=False).status_code == 303  # no code while off
    client.post(f"/agents/{net_agent['id']}/enable")
    code = agents.issue_code(net_agent["id"])
    _, token = agents.exchange(code)
    assert client.get("/v1/models", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    assert client.post("/mcp", json=ping, headers=net_agent["headers"]).status_code == 401  # the old token stays dead


def test_the_agent_loses_access_with_its_person(client, net_agent):
    assert client.get("/agents/api/config", headers=net_agent["headers"]).status_code == 200
    with SessionLocal() as s:
        s.get(User, net_agent["user_id"]).status = "disabled"
        s.commit()
    assert agents.authenticate(net_agent["token"]) is None


def test_heartbeat_reports_what_the_computer_applied(client, net_agent):
    config = client.get("/agents/api/config", headers=net_agent["headers"]).json()
    rev = config["doc"]["rev"]
    r = client.post("/agents/api/heartbeat", headers=net_agent["headers"],
                    json={"access_rev": rev, "versions": {"hermes": "1.0", "sync": "0.1"},
                          "hub": {"skills": {"plc-io": "1.0.0"}, "conflicts": ["plc-io"]}, "error": ""})
    assert r.status_code == 200 and r.json()["access_rev"] == rev
    with SessionLocal() as s:
        assert s.query(NetAgentAccess).one().applied_rev == rev
        agent = s.get(NetAgent, net_agent["id"])
        assert agent.versions["sync"] == "0.1" and agent.hub_state["conflicts"] == ["plc-io"]
    assert "применены" in client.get("/agents").text


def test_a_plain_user_sees_only_his_own_agent_and_gets_the_viewer_profile(client, net_on):
    accounts.create_user("ivanov", "correct-horse", "user", "test")
    from fastapi.testclient import TestClient
    from app.main import app
    with SessionLocal() as s:
        admin = s.query(User).filter(User.role == "superuser").first().id
    agents.create(admin, "opo-admin")
    with TestClient(app) as other:
        other.post("/login", data={"username": "ivanov", "password": "correct-horse"})
        assert "Мой агент" in other.get("/agents").text
        r = other.post("/agents/create", data={"name": "opo-ivanov", "profile": "opo-lead", "user_id": admin},
                       follow_redirects=False)
        assert r.status_code == 303
        page = other.get("/agents").text
        assert "opo-ivanov" in page and "opo-admin" not in page
        with SessionLocal() as s:
            mine = s.query(NetAgent).filter(NetAgent.name == "opo-ivanov").one()
            assert mine.user_id != admin
            admin_agent = s.query(NetAgent).filter(NetAgent.name == "opo-admin").one().id
        assert other.get(f"/agents/{admin_agent}").status_code == 404
        assert other.get("/agents/access").status_code == 403
        from app.net import access
        assert access.document(mine.id)["profile"]["code"] == "viewer"
        assert other.post(f"/agents/{mine.id}/access", data={"profile_id": "1"}).status_code == 404


def test_agent_names_are_checked(client, net_on):
    with SessionLocal() as s:
        admin = s.query(User).filter(User.role == "superuser").first().id
    r = client.post("/agents/create", data={"user_id": admin, "name": "Опо Иванов!"}, follow_redirects=False)
    assert r.status_code == 303 and "error=" in r.headers["location"]
    assert agents.suggest_name("ОПО", "ivan.petrov") == "opo-ivan-petrov"
    settings_store.set_many({"agents_on": False})
    assert client.get("/agents").status_code == 404
