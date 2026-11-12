"""The trace of an agent (docs/opo/TASKS.md, ОПО-5.7): requests to the models and MCP calls in one feed, put
together into runs by the task or by time; the debug mode keeps the texts for a while and turns itself off."""
import datetime as dt

import httpx

from app import accounts
from app.db import SessionLocal
from app.net import access, gateway, trace
from app.net.models import NetAgent, NetDebug, NetEvent, NetSpend


def _ok(url, body, headers):
    return httpx.Response(200, json={"model": body["model"], "usage": {"prompt_tokens": 50, "completion_tokens": 5, "cost": 0.0004},
                                     "choices": [{"message": {"role": "assistant", "content": "готово"}, "finish_reason": "stop"}]})


def _ask(client, agent, task="", **extra):
    headers = {**agent["headers"], **({"X-SYSAI-Task": task} if task else {})}
    return client.post("/v1/chat/completions", headers=headers,
                       json={"model": "sysai/mid", "messages": [{"role": "system", "content": "Ты агент ОПО"},
                                                               {"role": "user", "content": "привет, проверь IO"}], **extra})


def _mcp(client, agent, name, args=None, task=""):
    headers = {**agent["headers"], **({"X-SYSAI-Task": task} if task else {})}
    r = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                                  "params": {"name": name, "arguments": args or {}}})
    assert r.status_code == 200, r.text
    return r.json()["result"]


def test_steps_go_together_into_runs(client, net_agent, monkeypatch):
    monkeypatch.setattr(gateway, "_post", _ok)
    with SessionLocal() as s:  # an hour ago: a run of its own
        s.add(NetSpend(day="2000-01-01", agent_id=net_agent["id"], tier="cheap", model="m", prompt_tokens=10,
                       at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)))
        s.commit()
    assert _ask(client, net_agent, task="T-17").status_code == 200
    assert not _mcp(client, net_agent, "whoami", task="T-17")["isError"]
    access.set_agent_access(net_agent["id"], overrides={"data": {"people": False}})
    assert _mcp(client, net_agent, "people", {"query": "x"}, task="T-17")["isError"]  # no directory for this agent
    assert _ask(client, net_agent, model="sysai/nope").status_code == 400
    runs = trace.runs(net_agent["id"])
    assert len(runs) == 2
    run, old = runs
    assert run["task"] == "T-17"
    assert [s["title"] for s in run["steps"]] == ["подключился", "sysai/mid", "whoami", "people", "модель"]
    assert (run["models"], run["mcp"], run["errors"], run["tokens"]) == (2, 2, 2, 55)
    assert run["first_error"].startswith("people:") and abs(run["cost"] - 0.0004) < 1e-9
    assert old["models"] == 1 and old["task"] == ""
    with SessionLocal() as s:
        assert s.query(NetSpend).filter(NetSpend.task == "T-17").count() == 1
        assert s.query(NetEvent).filter(NetEvent.action == "mcp.whoami").one().details["task"] == "T-17"
    page = client.get(f"/agents/{net_agent['id']}").text
    assert "Трасса" in page and "T-17" in page and f"/agents/{net_agent['id']}/trace" in page
    page = client.get(f"/agents/{net_agent['id']}/trace").text
    assert "T-17" in page and "whoami" in page and gateway.DEFAULT_MODELS["mid"] in page and "model_not_allowed" in page
    assert "без задачи" in page
    only = client.get(f"/agents/{net_agent['id']}/trace?errors=1").text
    assert "T-17" in only and "без задачи" not in only


def test_the_debug_mode_keeps_the_texts_and_turns_itself_off(client, net_agent, monkeypatch):
    monkeypatch.setattr(gateway, "_post", _ok)
    aid = net_agent["id"]
    _ask(client, net_agent)
    with SessionLocal() as s:
        assert s.query(NetDebug).count() == 0  # without the debug mode only the numbers
    r = client.post(f"/agents/{aid}/debug", data={"on": "1"}, follow_redirects=False)
    assert r.status_code == 303 and f"/agents/{aid}/trace" in r.headers["location"]
    assert trace.debug_on(aid)
    _ask(client, net_agent, tools=[{"type": "function", "function": {"name": "terminal"}}])
    _mcp(client, net_agent, "whoami", {"verbose": "да"})
    with SessionLocal() as s:
        texts = {d.kind: d for d in s.query(NetDebug).all()}
        assert set(texts) == {"model", "mcp"}
        model = texts["model"]
        assert model.request["tail"][-1]["content"] == "привет, проверь IO" and model.request["tools"] == ["terminal"]
        assert model.request["system_chars"] == len("Ты агент ОПО") and model.response["content"] == "готово"
        assert texts["mcp"].request["tool"] == "whoami" and "opo-sysai" in texts["mcp"].response["result"]
        ids = {k: v.id for k, v in texts.items()}
        assert s.query(NetEvent).filter(NetEvent.action == "agent.debug").count() == 1
    page = client.get(f"/agents/{aid}/trace").text
    assert "включён до" in page and f"/agents/{aid}/trace/{ids['model']}" in page
    text = client.get(f"/agents/{aid}/trace/{ids['model']}").text
    assert "привет, проверь IO" in text and "готово" in text and "terminal" in text
    assert "opo-sysai" in client.get(f"/agents/{aid}/trace/{ids['mcp']}").text
    assert client.get(f"/agents/{aid}/trace/999999").status_code == 404
    client.post(f"/agents/{aid}/debug", data={"on": "0"})
    assert not trace.debug_on(aid)
    _ask(client, net_agent)
    with SessionLocal() as s:
        assert s.query(NetDebug).count() == 2
        agent = s.get(NetAgent, aid)
        agent.debug_until = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1)  # 24 hours are over
        s.add(NetDebug(agent_id=aid, kind="model", ref_id=1, at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=8)))
        s.commit()
    assert not trace.debug_on(aid)
    assert trace.purge(force=True) == 1  # texts older than 7 days go away


def test_the_trace_is_only_for_the_owner_and_superusers(client, net_agent):
    accounts.create_user("petrov", "correct-horse", "user", "test")
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as other:
        other.post("/login", data={"username": "petrov", "password": "correct-horse"})
        assert other.get(f"/agents/{net_agent['id']}/trace").status_code == 404
        assert other.post(f"/agents/{net_agent['id']}/debug", data={"on": "1"}).status_code == 404
    assert not trace.debug_on(net_agent["id"])


def test_excerpts_and_numbers_for_the_pages():
    body = {"messages": [{"role": "system", "content": "s" * 10},
                         {"role": "user", "content": [{"type": "text", "text": "смотри"}, {"type": "image_url"}]},
                         {"role": "assistant", "content": None,
                          "tool_calls": [{"function": {"name": "terminal", "arguments": "{\"cmd\": \"dir\"}"}}]},
                         {"role": "tool", "tool_call_id": "c1", "content": "ok"}],
            "temperature": 0.1}
    out = trace.request_excerpt(body)
    assert [m["role"] for m in out["tail"]] == ["user", "assistant", "tool"]
    assert out["tail"][0]["content"] == "смотри\n[image_url]" and out["tail"][1]["tool_calls"][0]["name"] == "terminal"
    assert out["messages"] == 4 and out["settings"] == {"temperature": 0.1}
    answer = trace.StreamedAnswer()
    for chunk in ({"choices": [{"delta": {"content": "При"}}]},
                  {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"name": "files_read", "arguments": "{\"pa"}}]}}]},
                  {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "th\": 1}"}}]}, "finish_reason": "tool_calls"}]}):
        answer.add(chunk)
    assert answer.value() == {"content": "При", "tool_calls": [{"name": "files_read", "arguments": "{\"path\": 1}"}],
                              "finish_reason": "tool_calls"}
    assert trace.tokens(950) == "950" and trace.tokens(12_400) == "12,4 тыс." and trace.tokens(2_500_000) == "2,50 млн"
    assert trace.duration(340) == "340 мс" and trace.duration(4200) == "4,2 с" and trace.duration(125_000) == "2 мин 5 с"
