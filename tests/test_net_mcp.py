"""The MCP server «sysai» (docs/opo/TASKS.md, stage 4): Streamable HTTP JSON-RPC, tools checked by access."""
from app.db import Employee, SessionLocal
from app.net import access, agents, mcp
from app.net.models import NetAgent, NetEvent, NetSkillUsage


def _rpc(client, agent, method, params=None, rid=1):
    body = {"jsonrpc": "2.0", "method": method, **({"id": rid} if rid is not None else {})}
    if params is not None:
        body["params"] = params
    return client.post("/mcp", json=body, headers={**agent["headers"], "Accept": "application/json, text/event-stream"})


def _call(client, agent, name, args=None):
    r = _rpc(client, agent, "tools/call", {"name": name, "arguments": args or {}})
    assert r.status_code == 200, r.text
    return r.json()["result"]


def test_the_protocol(client, net_agent):
    init = _rpc(client, net_agent, "initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                                   "clientInfo": {"name": "hermes", "version": "1"}}).json()
    assert init["result"]["protocolVersion"] == "2025-03-26" and init["result"]["serverInfo"]["name"] == "sysai"
    assert "tools" in init["result"]["capabilities"]
    newer = _rpc(client, net_agent, "initialize", {"protocolVersion": "2099-01-01"}).json()
    assert newer["result"]["protocolVersion"] == mcp.PROTOCOL_VERSIONS[0]
    assert _rpc(client, net_agent, "notifications/initialized", rid=None).status_code == 202
    assert _rpc(client, net_agent, "ping").json()["result"] == {}
    assert _rpc(client, net_agent, "no/such").json()["error"]["code"] == -32601
    bad = client.post("/mcp", content=b"{oops", headers={**net_agent["headers"], "Content-Type": "application/json"})
    assert bad.status_code == 400 and bad.json()["error"]["code"] == -32700
    batch = client.post("/mcp", json=[{"jsonrpc": "2.0", "id": 1, "method": "ping"},
                                      {"jsonrpc": "2.0", "method": "notifications/initialized"}], headers=net_agent["headers"])
    assert [m["id"] for m in batch.json()] == [1]
    assert client.get("/mcp", headers=net_agent["headers"]).status_code == 405
    no_token = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert no_token.status_code == 401 and no_token.headers["www-authenticate"] == "Bearer"


def test_tools_follow_the_access_of_the_agent(client, net_agent):
    names = {t["name"] for t in _rpc(client, net_agent, "tools/list").json()["result"]["tools"]}
    assert names == set(access.SYSAI_TOOLS)
    who = _call(client, net_agent, "whoami")["structuredContent"]
    assert who["agent"] == "opo-sysai" and who["profile"] == "Инженер ОПО" and "sysai/strong" in who["models"]
    access.set_agent_access(net_agent["id"], overrides={"mcp": {"sysai": ["whoami"]}})
    assert {t["name"] for t in _rpc(client, net_agent, "tools/list").json()["result"]["tools"]} == {"whoami"}
    denied = _call(client, net_agent, "people")
    assert denied["isError"] and "не разрешают" in denied["content"][0]["text"]
    with SessionLocal() as s:
        assert s.query(NetEvent).filter(NetEvent.action == "mcp.denied").count() == 1


def test_people_and_departments(client, net_agent):
    with SessionLocal() as s:
        s.add(Employee(name="Пётр Сидоров", position="Инженер ТО", department="ТО", competencies="AutoCAD, схемы"))
        s.add(Employee(name="Анна Котова", position="Инженер ОПО", department="ОПО"))
        s.commit()
    found = _call(client, net_agent, "people", {"query": "autocad"})["structuredContent"]["people"]
    assert [p["name"] for p in found] == ["Пётр Сидоров"] and "competencies" in found[0]  # the person is a superuser
    depts = _call(client, net_agent, "departments")["structuredContent"]["departments"]
    assert [d["name"] for d in depts] == ["ОПО"]
    access.set_agent_access(net_agent["id"], overrides={"data": {"people": False}})
    assert _call(client, net_agent, "people")["isError"]


def test_notify_me_writes_to_the_person_in_telegram(client, net_agent, tg):
    assert _call(client, net_agent, "notify_me", {"text": "Готово"})["isError"]  # no person yet
    with SessionLocal() as s:
        person = Employee(name="Админ", telegram_chat_id="555")
        s.add(person)
        s.commit()
        s.get(NetAgent, net_agent["id"]).employee_id = person.id
        s.commit()
    ctx = agents.authenticate(net_agent["token"])
    assert ctx.employee_id
    result = _call(client, net_agent, "notify_me", {"text": "Теги проекта 24-017 готовы к проверке"})
    assert not result["isError"]
    sent = [d for m, d, _ in tg if m == "sendMessage"]
    assert sent and sent[-1]["chat_id"] == "555" and "opo-sysai" in sent[-1]["text"] and "24-017" in sent[-1]["text"]


def test_hub_tools_and_skill_usage(client, net_agent):
    skill = ("---\nname: st-style\ndescription: Стиль кода ST компании\n---\n# Стиль ST\n## Когда применять\n## Как делать\n"
             "## Ошибки\n## Проверка\n")
    proposal = _call(client, net_agent, "hub_propose", {"skill_md": skill, "why": "Повторяющиеся правки в ревью"})
    assert not proposal["isError"] and proposal["structuredContent"]["status"] == "review"
    assert _call(client, net_agent, "hub_search", {"query": "стиль"})["structuredContent"]["skills"] == []
    client.post("/hub/st-style/1.0.0/publish")
    found = _call(client, net_agent, "hub_search", {"query": "стиль"})["structuredContent"]["skills"]
    assert [k["name"] for k in found] == ["st-style"]
    got = _call(client, net_agent, "hub_get", {"name": "st-style"})["structuredContent"]
    assert got["version"] == "1.0.0" and "# Стиль ST" in got["skill_md"] and got["download"].endswith("1.0.0.zip")
    assert _call(client, net_agent, "hub_get", {"name": "nope"})["isError"]
    logged = _call(client, net_agent, "log_event", {"action": "skill_used", "target": "24-017",
                                                    "details": {"skill": "st-style", "version": "1.0.0", "tokens": 1200, "ok": True}})
    assert not logged["isError"]
    with SessionLocal() as s:
        use = s.query(NetSkillUsage).one()
        assert use.skill == "st-style" and use.tokens == 1200 and use.ok is True
    assert "применений" in client.get("/hub/st-style").text


def test_calls_are_limited_per_minute(client, net_agent):
    from app import settings_store
    settings_store.set_many({"agents_mcp_per_minute": 3})
    for _ in range(3):
        assert "result" in _rpc(client, net_agent, "tools/call", {"name": "whoami", "arguments": {}}).json()
    limited = _rpc(client, net_agent, "tools/call", {"name": "whoami", "arguments": {}}).json()
    assert limited["error"]["code"] == -32000
    big = client.post("/mcp", content=b"x" * (mcp.MAX_BODY + 1), headers={**net_agent["headers"], "Content-Type": "application/json"})
    assert big.status_code == 413
