"""The model gateway (docs/opo/TASKS.md, stage 5): tiers, budgets, spend, streaming."""
import json

import httpx

from app import llm, settings_store
from app.db import LlmSpend, SessionLocal
from app.net import access, gateway
from app.net.models import NetSpend


class Upstream:
    def __init__(self, status=200, data=None):
        self.status, self.data, self.bodies, self.headers = status, data, [], []

    def post(self, url, body, headers):
        self.bodies.append(body)
        self.headers.append(headers)
        data = self.data if self.data is not None else {
            "id": "gen-1", "model": body["model"], "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20, "cost": 0.0123}}
        return httpx.Response(self.status, json=data)


def _ask(client, agent, model="sysai/mid", **extra):
    return client.post("/v1/chat/completions", headers={**agent["headers"], "X-SYSAI-Project": "24-017",
                                                         "X-SYSAI-Skill": "plc-io-from-excel"},
                       json={"model": model, "messages": [{"role": "user", "content": "привет"}], **extra})


def test_models_are_the_allowed_tiers(client, net_agent):
    data = client.get("/v1/models", headers=net_agent["headers"]).json()
    assert [m["id"] for m in data["data"]] == ["sysai/cheap", "sysai/mid", "sysai/strong", "sysai/vision"]
    access.set_agent_access(net_agent["id"], overrides={"models": {"tiers": ["cheap"]}})
    assert [m["id"] for m in client.get("/v1/models", headers=net_agent["headers"]).json()["data"]] == ["sysai/cheap"]


def test_a_request_goes_to_the_tier_model_and_is_counted(client, net_agent, monkeypatch):
    up = Upstream()
    monkeypatch.setattr(gateway, "_post", up.post)
    settings_store.set_many({"agents_models": {"mid": "anthropic/claude-sonnet-4.5"}})
    r = _ask(client, net_agent, models=["openai/o3-pro"], route="fallback", user="someone", temperature=0.2)
    assert r.status_code == 200, r.text
    assert r.json()["choices"][0]["message"]["content"] == "ok"
    body = up.bodies[0]
    assert body["model"] == "anthropic/claude-sonnet-4.5" and body["usage"] == {"include": True}
    assert "models" not in body and "route" not in body and body["user"] == "opo-sysai" and body["temperature"] == 0.2
    assert up.headers[0]["Authorization"] == "Bearer test-openrouter-key"
    with SessionLocal() as s:
        spend = s.query(NetSpend).one()
        assert (spend.agent_id, spend.tier, spend.model, spend.project, spend.skill) == \
            (net_agent["id"], "mid", "anthropic/claude-sonnet-4.5", "24-017", "plc-io-from-excel")
        assert spend.prompt_tokens == 100 and abs(spend.cost - 0.0123) < 1e-9
        assert s.query(LlmSpend).one().cost == 0.0123  # the company's daily budget sees the agent too
    page = client.get(f"/agents/{net_agent['id']}").text
    assert "24-017" in page and "$0.01" in page


def test_only_tiers_and_only_allowed_ones(client, net_agent, monkeypatch):
    up = Upstream()
    monkeypatch.setattr(gateway, "_post", up.post)
    r = _ask(client, net_agent, model="openai/gpt-4o")
    assert r.status_code == 400 and r.json()["error"]["code"] == "model_not_allowed"
    access.set_agent_access(net_agent["id"], overrides={"models": {"tiers": ["cheap"]}})
    r = _ask(client, net_agent, model="sysai/strong")
    assert r.status_code == 403 and "sysai/strong" in r.json()["error"]["message"]
    assert _ask(client, net_agent, model="cheap").status_code == 200
    assert up.bodies[-1]["model"] == gateway.DEFAULT_MODELS["cheap"]
    assert client.post("/v1/chat/completions", headers=net_agent["headers"], json={"model": "sysai/cheap"}).status_code == 400


def test_budgets_stop_the_agent_with_a_clear_reason(client, net_agent, monkeypatch):
    up = Upstream()
    monkeypatch.setattr(gateway, "_post", up.post)
    access.set_agent_access(net_agent["id"], overrides={"models": {"budget_day_usd": 0.02, "budget_project_usd": 0.0}})
    assert _ask(client, net_agent).status_code == 200
    assert _ask(client, net_agent).status_code == 200  # 0.0246 spent now
    r = _ask(client, net_agent)
    assert r.status_code == 429 and r.json()["error"]["code"] == "budget_exceeded" and "Дневной бюджет" in r.json()["error"]["message"]
    access.set_agent_access(net_agent["id"], overrides={"models": {"budget_day_usd": 0, "budget_project_usd": 0.01}})
    r = _ask(client, net_agent)
    assert r.status_code == 429 and "24-017" in r.json()["error"]["message"]
    access.set_agent_access(net_agent["id"], overrides={"models": {"budget_day_usd": 0, "budget_project_usd": 0}})
    settings_store.set_many({"daily_budget_usd": 0.01})
    r = _ask(client, net_agent)
    assert r.status_code == 429 and "лимит" in r.json()["error"]["message"].lower()
    assert len(up.bodies) == 2
    assert llm.spent_today() > 0


def test_no_key_and_upstream_failures(client, net_agent, monkeypatch):
    monkeypatch.setattr(gateway, "_post", Upstream(500, {"error": {"message": "boom"}}).post)
    r = _ask(client, net_agent)
    assert r.status_code == 502 and "boom" in r.json()["error"]["message"]
    monkeypatch.setattr(gateway, "_post", Upstream(401, {"error": {"message": "bad key"}}).post)
    assert _ask(client, net_agent).json()["error"]["code"] == "upstream_auth"
    settings_store.set_many({"openrouter_api_key": ""})
    r = _ask(client, net_agent)
    assert r.status_code == 503 and r.json()["error"]["code"] == "no_key"


class FakeStream:
    def __init__(self, lines, status=200):
        self.lines, self.status_code, self.closed = lines, status, False

    def iter_lines(self):
        yield from self.lines

    def read(self):
        return b'{"error": {"message": "overloaded"}}'

    def close(self):
        self.closed = True


class FakeClient:
    def close(self):
        pass


def test_streaming_passes_events_through_and_counts_the_usage(client, net_agent, monkeypatch):
    chunks = [{"choices": [{"delta": {"content": "При"}}]}, {"choices": [{"delta": {"content": "вет"}}]},
              {"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 7, "completion_tokens": 2, "cost": 0.002}}]
    lines = [": OPENROUTER PROCESSING", ""]
    for chunk in chunks:
        lines += ["data: " + json.dumps(chunk, ensure_ascii=False), ""]
    lines += ["data: [DONE]", ""]
    seen = {}
    stream = FakeStream(lines)

    def fake_open(url, body, headers):
        seen["body"] = body
        return FakeClient(), stream
    monkeypatch.setattr(gateway, "_open", fake_open)
    r = _ask(client, net_agent, stream=True, stream_options={"include_usage": True})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    events = [line for line in r.text.split("\n\n") if line.startswith("data:")]
    assert len(events) == 4 and events[-1] == "data: [DONE]"
    assert "".join(json.loads(e[5:])["choices"][0]["delta"].get("content", "") for e in events[:-1]) == "Привет"
    assert seen["body"]["stream"] is True and seen["body"]["usage"] == {"include": True}
    assert stream.closed
    with SessionLocal() as s:
        spend = s.query(NetSpend).one()
        assert spend.completion_tokens == 2 and abs(spend.cost - 0.002) < 1e-9
    monkeypatch.setattr(gateway, "_open", lambda url, body, headers: (FakeClient(), FakeStream([], status=429)))
    r = _ask(client, net_agent, stream=True)
    assert r.status_code == 429 and "overloaded" in r.json()["error"]["message"]


def test_web_plugins_need_the_whole_internet(client, net_agent, monkeypatch):
    up = Upstream()
    monkeypatch.setattr(gateway, "_post", up.post)
    _ask(client, net_agent, plugins=[{"id": "web"}])
    assert "plugins" not in up.bodies[-1]  # the engineer's profile has only a list of sites
    access.set_agent_access(net_agent["id"], overrides={"internet": {"mode": "all"}})
    _ask(client, net_agent, plugins=[{"id": "web"}])
    assert up.bodies[-1]["plugins"] == [{"id": "web"}]
