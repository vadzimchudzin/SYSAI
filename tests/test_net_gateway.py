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
    assert [m["id"] for m in data["data"]] == ["sysai/cheap", "sysai/mid", "sysai/strong", "sysai/vision", "sysai/auto"]
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
    # the agent's own list of models is dropped: SYSAI gives OpenRouter its spare model of the tier instead (ОПО-5.5)
    assert body["models"] == ["anthropic/claude-sonnet-4.5", gateway.DEFAULT_FALLBACK["mid"]]
    assert "route" not in body and body["user"] == "opo-sysai" and body["temperature"] == 0.2
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
    access.set_agent_access(net_agent["id"], overrides={"internet": {"mode": "list", "sites": ["se.com"]}})
    _ask(client, net_agent, plugins=[{"id": "web"}])
    assert "plugins" not in up.bodies[-1]  # only a list of sites
    access.set_agent_access(net_agent["id"], overrides={"internet": {"mode": "all"}})
    _ask(client, net_agent, plugins=[{"id": "web"}])
    assert up.bodies[-1]["plugins"] == [{"id": "web"}]



# ----------------------------------------------------------------------------- sysai/auto: Jev Router (ОПО-5.8)
CATALOG = {"data": [
    {"id": "google/gemini-2.5-flash-lite", "pricing": {"prompt": "0.0000001", "completion": "0.0000004"}},
    {"id": "google/gemini-2.5-flash", "pricing": {"prompt": "0.0000003", "completion": "0.0000025"}},
    {"id": "google/gemini-2.5-pro", "pricing": {"prompt": "0.00000125", "completion": "0.00001"}},
    {"id": "deepseek/deepseek-v3.2", "pricing": {"prompt": "0.00000027", "completion": "0.0000004"}},
    {"id": "anthropic/claude-opus-4.1", "pricing": {"prompt": "0.000015", "completion": "0.000075"}},
    {"id": "openai/o3-pro", "pricing": {"prompt": "0.00002", "completion": "0.00008"}},
    {"id": "x-ai/grok-long", "pricing": {"prompt": "0.000001", "completion": "0.00002"}},
    {"id": "typesafe/jev-router", "pricing": {"prompt": "-1", "completion": "-1"}},
]}
SERVED = {"id": "gen-2", "model": "google/gemini-2.5-flash", "choices": [{"message": {"role": "assistant", "content": "ok"}}],
          "usage": {"prompt_tokens": 1000, "completion_tokens": 50, "cost": 0.001,
                    "prompt_tokens_details": {"cached_tokens": 600}}}


def _catalog(monkeypatch, data=None, status=200):
    asked = []

    def fake_get(url, headers):
        asked.append(url)
        return httpx.Response(status, json=CATALOG if data is None else data)
    monkeypatch.setattr(gateway, "_get", fake_get)
    return asked


def test_auto_lets_jev_pick_only_among_the_agents_models(client, net_agent, monkeypatch):
    asked = _catalog(monkeypatch)
    up = Upstream(data=SERVED)
    monkeypatch.setattr(gateway, "_post", up.post)
    r = _ask(client, net_agent, model="sysai/auto", reasoning={"max_tokens": 2000, "effort": "high"},
             sysai={"role": "workers", "anything": "else"})
    assert r.status_code == 200, r.text
    body = up.bodies[0]
    assert body["model"] == gateway.JEV_ROUTER and "sysai" not in body
    assert body["reasoning"] == {"effort": "high"}  # a budget of tokens the router refuses; the effort is a hint
    plugin = body["plugins"][-1]
    assert plugin["id"] == "jev-router" and plugin["cost_tier"] == "low"
    assert plugin["models"] == ["google/gemini-2.5-flash", "google/gemini-2.5-flash-lite", "google/gemini-2.5-pro"]
    # dearer than gemini-2.5-pro per prompt or per completion token: never; routers (-1) are not models
    assert plugin["excluded_models"] == ["anthropic/claude-opus-4.1", "openai/o3-pro", "x-ai/grok-long"]
    with SessionLocal() as s:
        spend = s.query(NetSpend).one()
        assert (spend.tier, spend.model, spend.route, spend.role) == ("auto", "google/gemini-2.5-flash", "jev:low", "workers")
        assert spend.cached_tokens == 600 and spend.ok and spend.error == "" and spend.ms >= 0
    access.set_agent_access(net_agent["id"], overrides={"models": {"auto_cost": "high"}})
    _ask(client, net_agent, model="sysai/auto")
    assert up.bodies[-1]["plugins"][-1]["cost_tier"] == "high" and "reasoning" not in up.bodies[-1]
    assert len(asked) == 1  # the catalog is kept for hours


def test_auto_falls_back_to_mid_and_lets_jev_rest(client, net_agent, monkeypatch):
    _catalog(monkeypatch)

    class NoJev(Upstream):
        def post(self, url, body, headers):
            self.bodies.append(body)
            if body["model"] == gateway.JEV_ROUTER:
                return httpx.Response(404, json={"error": {"message": "No endpoints found for typesafe/jev-router"}})
            return httpx.Response(200, json={**SERVED, "model": body["model"]})
    up = NoJev()
    monkeypatch.setattr(gateway, "_post", up.post)
    assert _ask(client, net_agent, model="sysai/auto").status_code == 200
    assert [b["model"] for b in up.bodies] == [gateway.JEV_ROUTER, gateway.DEFAULT_MODELS["mid"]]
    assert "plugins" not in up.bodies[1]
    assert _ask(client, net_agent, model="sysai/auto").status_code == 200  # Jev rests: straight to «mid»
    assert len(up.bodies) == 3 and up.bodies[2]["model"] == gateway.DEFAULT_MODELS["mid"]
    with SessionLocal() as s:
        routes = [r.route for r in s.query(NetSpend).order_by(NetSpend.id)]
    assert routes[0].startswith("jev→mid: HTTP 404") and routes[1].startswith("auto→mid: Jev недоступен")
    gateway._state["jev_rest_until"] = 0.0
    up.bodies.clear()
    access.set_agent_access(net_agent["id"], overrides={"models": {"tiers": ["cheap", "auto"]}})
    _ask(client, net_agent, model="sysai/auto")
    assert [b["model"] for b in up.bodies] == [gateway.JEV_ROUTER, gateway.DEFAULT_MODELS["cheap"]]  # no «mid»


def test_auto_without_the_catalog_does_not_ask_jev(client, net_agent, monkeypatch):
    asked = _catalog(monkeypatch, {"error": "down"}, status=500)
    up = Upstream()
    monkeypatch.setattr(gateway, "_post", up.post)
    assert _ask(client, net_agent, model="sysai/auto").status_code == 200
    assert up.bodies[0]["model"] == gateway.DEFAULT_MODELS["mid"] and "plugins" not in up.bodies[0]
    _ask(client, net_agent, model="sysai/auto")
    assert len(asked) == 1  # asked again in 10 minutes, not on every request
    with SessionLocal() as s:
        assert s.query(NetSpend).first().route == "auto→mid: нет цен моделей в каталоге OpenRouter"


def test_refused_and_failed_requests_are_counted_with_their_reason(client, net_agent, monkeypatch):
    monkeypatch.setattr(gateway, "_post", Upstream(500, {"error": {"message": "boom"}}).post)
    assert _ask(client, net_agent).status_code == 502
    access.set_agent_access(net_agent["id"], overrides={"models": {"tiers": ["cheap"]}})
    assert _ask(client, net_agent, model="sysai/strong").status_code == 403
    with SessionLocal() as s:
        rows = s.query(NetSpend).order_by(NetSpend.id).all()
        assert [(r.ok, r.tier, r.cost) for r in rows] == [(False, "mid", 0.0), (False, "strong", 0.0)]
        assert rows[0].error.startswith("upstream_error") and "boom" in rows[0].error
        assert rows[1].error.startswith("model_not_allowed") and rows[0].project == "24-017"
    assert gateway.summary(net_agent["id"])["errors"] == 2


def test_a_stream_through_jev_keeps_the_served_model_and_a_broken_end(client, net_agent, monkeypatch):
    _catalog(monkeypatch)
    lines = []
    for chunk in ({"model": "google/gemini-2.5-flash-lite", "choices": [{"delta": {"content": "x"}}]},
                  {"error": {"message": "provider died"}}):
        lines += ["data: " + json.dumps(chunk), ""]
    opened = []

    def fake_open(url, body, headers):
        opened.append(body["model"])
        if body["model"] == gateway.JEV_ROUTER and len(opened) == 1:
            return FakeClient(), FakeStream([], status=503)
        return FakeClient(), FakeStream(lines)
    monkeypatch.setattr(gateway, "_open", fake_open)
    r = _ask(client, net_agent, model="sysai/auto", stream=True)
    assert r.status_code == 200 and "provider died" in r.text
    assert opened == [gateway.JEV_ROUTER, gateway.DEFAULT_MODELS["mid"]]
    with SessionLocal() as s:
        spend = s.query(NetSpend).one()
        assert spend.model == "google/gemini-2.5-flash-lite" and spend.ok is False and "provider died" in spend.error
        assert spend.route.startswith("jev→mid: HTTP 503")


def test_a_stream_the_agent_leaves_is_closed_and_counted(client, net_agent, monkeypatch):
    import anyio
    from app.net import agents, routes
    lines = ["data: " + json.dumps({"model": "google/gemini-2.5-flash", "choices": [{"delta": {"content": "x"}}]}), "",
             "data: [DONE]", ""]
    upstream = FakeStream(lines)
    monkeypatch.setattr(gateway, "_open", lambda url, body, headers: (FakeClient(), upstream))
    ctx = agents.authenticate(net_agent["token"])
    chunks = gateway.stream(ctx, {"model": "sysai/mid", "messages": [{"role": "user", "content": "x"}], "stream": True})

    async def leave_after_the_first_piece():
        relay = routes._relay(chunks)
        assert (await relay.__anext__()).startswith(b"data:")
        await relay.aclose()
    anyio.run(leave_after_the_first_piece)
    assert upstream.closed
    with SessionLocal() as s:
        spend = s.query(NetSpend).one()
        assert spend.ok is False and spend.error.startswith("client_closed") and spend.model == "google/gemini-2.5-flash"


# ----------------------------------------------------------------------------- spare models (ОПО-5.5)
class Flaky(Upstream):
    """Fails the first `fails` requests with `fail_status`, then answers."""
    def __init__(self, fails=1, fail_status=503):
        super().__init__()
        self.fails, self.fail_status = fails, fail_status

    def post(self, url, body, headers):
        if len(self.bodies) < self.fails:
            self.bodies.append(body)
            self.headers.append(headers)
            return httpx.Response(self.fail_status, json={"error": {"message": "provider is down"}})
        return super().post(url, body, headers)


def test_a_failed_model_is_asked_once_more_on_the_spare_one(client, net_agent, monkeypatch):
    up = Flaky(fails=1, fail_status=503)
    monkeypatch.setattr(gateway, "_post", up.post)
    r = _ask(client, net_agent)
    assert r.status_code == 200, r.text
    first, second = up.bodies
    mid, spare = gateway.DEFAULT_MODELS["mid"], gateway.DEFAULT_FALLBACK["mid"]
    assert first["model"] == mid and first["models"] == [mid, spare]
    assert second["model"] == spare and "models" not in second
    with SessionLocal() as s:
        row = s.query(NetSpend).one()
        assert row.ok and row.model == spare and row.route.startswith("mid→запасная: HTTP 503")
    # the request's own fault (400) is not retried
    up = Flaky(fails=1, fail_status=400)
    monkeypatch.setattr(gateway, "_post", up.post)
    assert _ask(client, net_agent).status_code >= 400 and len(up.bodies) == 1


def test_openrouter_switching_to_the_spare_by_itself_is_seen(client, net_agent, monkeypatch):
    spare = gateway.DEFAULT_FALLBACK["mid"]
    up = Upstream(data={"id": "gen-2", "model": spare, "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                        "usage": {"prompt_tokens": 10, "completion_tokens": 2, "cost": 0.001}})
    monkeypatch.setattr(gateway, "_post", up.post)
    assert _ask(client, net_agent).status_code == 200
    with SessionLocal() as s:
        row = s.query(NetSpend).one()
        assert row.model == spare and row.route == "ответила запасная"


def test_spare_models_are_set_in_the_settings_and_can_be_turned_off(client, net_agent, monkeypatch):
    page = client.get("/agents/access").text
    assert 'name="fallback_mid"' in page and gateway.DEFAULT_FALLBACK["mid"] in page and "нет запасной" in page
    r = client.post("/agents/access/settings", data={"model_mid": "", "fallback_mid": "", "fallback_cheap": "openai/gpt-4o-mini",
                                                     "mcp_per_minute": "120"}, follow_redirects=False)
    assert "msg=" in r.headers["location"]
    assert settings_store.get("agents_fallback")["mid"] == "" and gateway.backup_for("cheap") == "openai/gpt-4o-mini"
    assert gateway.backup_for("mid") == ""
    up = Flaky(fails=5, fail_status=503)
    monkeypatch.setattr(gateway, "_post", up.post)
    r = _ask(client, net_agent)
    assert r.status_code >= 500 and len(up.bodies) == 1 and "models" not in up.bodies[0]  # no spare: one try
    # the spare model is never the model itself
    settings_store.set_many({"agents_models": {"cheap": "openai/gpt-4o-mini"}})
    assert gateway.backup_for("cheap") == ""


def test_claude_tiers_get_prompt_cache_marks_in_the_hermes_config(client, net_agent):
    before = access.document(net_agent["id"], "https://sysai.example")
    assert "providers" not in before["doc"]["harness_config"]  # the default models cache by themselves
    settings_store.set_many({"agents_models": {"strong": "anthropic/claude-sonnet-4.5"}})
    after = access.document(net_agent["id"], "https://sysai.example")
    provider = after["doc"]["harness_config"]["providers"]["sysai"]
    assert provider["api"] == "https://sysai.example/v1" and provider["api_key"] == "${SYSAI_AGENT_TOKEN}"
    assert provider["models"]["sysai/strong"] == {"prompt_caching": True} and "sysai/mid" not in provider["models"]
    assert after["rev"] != before["rev"]  # the computer re-applies the configuration
