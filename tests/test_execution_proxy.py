"""OpenAI-compatible proxy safety with a local HTTP mock, never a provider call."""
import json

import httpx
import pytest

from test_coordinator import pilot
from test_execution_mcp import TOKEN
from app import config, db, llm
from app.coordinator import proxy, service as c


@pytest.fixture
def proxy_client(pilot, client, monkeypatch):
    monkeypatch.setenv("SYSAI_MCP_TOKEN", TOKEN)
    monkeypatch.setenv("SYSAI_MCP_USER_ID", "1")
    monkeypatch.setenv("SYSAI_MCP_SCOPES", "read,plan,send,model")
    monkeypatch.setattr(config, "openrouter_key", lambda: "offline-provider-placeholder")
    calls=[]
    def provider(request):
        calls.append(request)
        return httpx.Response(200,json={"id":"offline","object":"chat.completion","created":0,"model":"approved",
            "choices":[{"index":0,"message":{"role":"assistant","content":"Evidence needed"},"finish_reason":"stop"}],
            "usage":{"cost":0.01},"internal_debug":"must not be forwarded"})
    real=httpx.AsyncClient
    monkeypatch.setattr(proxy.httpx,"AsyncClient",lambda **kwargs:real(**kwargs,transport=httpx.MockTransport(provider)))
    def send(data=None, **kwargs):
        return client.post("/execution/v1/chat/completions",json=data if data is not None else {
            "model":"sysai-agent","messages":[{"role":"user","content":"One bounded question"}]},
            headers={"Authorization":"Bearer "+TOKEN},**kwargs)
    return send,calls


def test_proxy_uses_main_credentials_fixed_upstream_and_spend_accounting(proxy_client):
    send,calls=proxy_client
    response=send()
    assert response.status_code==200 and response.json()["choices"]
    assert "internal_debug" not in response.json()
    assert str(calls[0].url)=="https://openrouter.ai/api/v1/chat/completions"
    assert json.loads(calls[0].content)["model"]=="google/gemini-2.5-flash"
    assert llm.spent_today()==0.01


@pytest.mark.parametrize("data,status", [
    ({"url":"http://attacker"},400),
    ({"model":"unapproved","messages":[{"role":"user","content":"x"}]},400),
    ({"model":"sysai-agent","messages":[],"stream":False},400),
    ({"model":"sysai-agent","messages":[{"role":"root","content":"x"}]},400),
    ({"model":"sysai-agent","messages":[{"role":"user","content":"x","url":"bad"}]},400),
    ({"model":"sysai-agent","messages":[{"role":"user","content":[{"type":"image_url","image_url":"bad"}]}]},400),
    ({"model":"sysai-agent","messages":[{"role":"user","content":"x"}],"stream":True},400),
    ({"model":"sysai-agent","messages":[{"role":"user","content":"x"}],"max_tokens":2049},400),
    ({"model":"sysai-agent","messages":[{"role":"user","content":"x"}],"temperature":2},400),
    ({"model":"sysai-agent","messages":[{"role":"user","content":"x"*65000}]},413),
    ({"model":"sysai-agent","messages":[{"role":"user","content":"x"}],"tools":[{}]*21},400),
])
def test_proxy_rejects_unbounded_or_untrusted_fields(proxy_client,data,status):
    send,calls=proxy_client
    assert send(data).status_code==status
    assert not calls


def test_proxy_supports_bounded_tool_call_messages(proxy_client):
    send,calls=proxy_client
    response=send({"model":"sysai-agent","messages":[{"role":"assistant","content":None,"tool_calls":[{
        "id":"call1","type":"function","function":{"name":"list_runs","arguments":"{}"}}]},
        {"role":"tool","tool_call_id":"call1","content":"{}"},
        {"role":"user","content":[{"type":"text","text":"Next step?"}]}],"max_completion_tokens":900,
        "tools":[{"type":"function","function":{"name":"list_runs","parameters":{"type":"object"}}}]})
    assert response.status_code==200
    body=json.loads(calls[0].content)
    assert body["max_tokens"]==900 and "max_completion_tokens" not in body


def test_proxy_auth_scope_role_and_missing_key(proxy_client,client,monkeypatch):
    send,calls=proxy_client
    assert client.post("/execution/v1/chat/completions",json={}).status_code==401
    monkeypatch.setenv("SYSAI_MCP_SCOPES","read")
    assert send().status_code==403
    monkeypatch.setenv("SYSAI_MCP_SCOPES","model")
    monkeypatch.setattr(config,"openrouter_key",lambda:"")
    assert send().status_code==503
    assert not calls


def test_proxy_durable_budget_survives_requests(proxy_client):
    send,calls=proxy_client
    c.configure({"daily_actions":1})
    for _ in range(3):assert send().status_code==200
    assert send().status_code==429 and len(calls)==3


def test_proxy_disabled_lease_and_spend_guards(proxy_client,monkeypatch):
    send,calls=proxy_client
    c.configure({"enabled":False});assert send().status_code==403
    c.configure({"enabled":True})
    token=c.acquire();assert send().status_code==429;c.release(token)
    def exceeded():raise llm.LLMError("sensitive key")
    monkeypatch.setattr(llm,"check_budget",exceeded)
    assert send().status_code==429 and not calls


@pytest.mark.parametrize("mode",["provider_error","invalid_json","large_response","invalid_shape","timeout"])
def test_proxy_provider_failure_is_sanitized(proxy_client,monkeypatch,mode):
    send,calls=proxy_client
    # Recover the real class from the configured factory's global module before replacing it again.
    real=httpx._client.AsyncClient
    def handler(request):
        if mode=="provider_error":return httpx.Response(500,text="secret provider details")
        if mode=="invalid_json":return httpx.Response(200,text="not json")
        if mode=="large_response":return httpx.Response(200,content=b"x"*262145)
        if mode=="invalid_shape":return httpx.Response(200,json={"secret":"details"})
        raise httpx.ReadTimeout("secret provider details")
    monkeypatch.setattr(proxy.httpx,"AsyncClient",lambda **kwargs:real(**kwargs,transport=httpx.MockTransport(handler)))
    response=send()
    assert response.status_code==502 and "secret" not in response.text


def test_proxy_request_size_and_json_errors(proxy_client,client):
    send,calls=proxy_client
    headers={"Authorization":"Bearer "+TOKEN}
    assert client.post("/execution/v1/chat/completions",headers=headers,content="x"*131073).status_code==413
    assert client.post("/execution/v1/chat/completions",headers=headers,content="x").status_code==400
    assert not calls
