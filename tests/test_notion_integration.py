import hashlib
import hmac
import json

import pytest

from app import config, notion, pipeline, settings_store
from app.db import Meeting, NotionConnection, NotionImportedPage, NotionWebhookEvent, SessionLocal


def _body(payload, token):
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    signature = "sha256=" + hmac.new(token.encode(), raw, hashlib.sha256).hexdigest()
    return raw, signature


def test_oauth_url_requires_config_and_uses_state_and_callback(monkeypatch):
    monkeypatch.setattr(config, "NOTION_CLIENT_ID", "")
    monkeypatch.setattr(config, "NOTION_CLIENT_SECRET", "")
    with pytest.raises(notion.NotionError):
        notion.authorization_url("state")
    monkeypatch.setattr(config, "NOTION_CLIENT_ID", "client-id")
    monkeypatch.setattr(config, "NOTION_CLIENT_SECRET", "client-secret")
    url = notion.authorization_url("abc-state", "https://example.test/settings/notion/callback")
    assert "client_id=client-id" in url and "state=abc-state" in url
    assert "redirect_uri=https%3A%2F%2Fexample.test%2Fsettings%2Fnotion%2Fcallback" in url


def test_webhook_challenge_encrypts_token_and_event_is_idempotent():
    token = "verification-secret-for-tests-12345"
    challenge, _ = _body({"verification_token": token}, token)
    assert notion.receive_webhook(challenge, "")["verification_token_received"]
    with SessionLocal() as s:
        row = s.get(NotionConnection, 1)
        assert row and row.webhook_verification_token_encrypted != token
    page_id = "a1b2c3d4e5f6478899aabbccddeeff00"
    event = {"id": "event-001", "type": "page.created", "entity": {"id": page_id, "type": "page"},
             "data": {"id": page_id}}
    raw, sig = _body(event, token)
    assert notion.receive_webhook(raw, sig)["queued"]
    assert notion.receive_webhook(raw, sig)["queued"]
    with SessionLocal() as s:
        assert s.query(NotionWebhookEvent).count() == 1


def test_webhook_rejects_invalid_signature():
    token = "verification-secret-for-tests-12345"
    challenge, _ = _body({"verification_token": token}, token)
    notion.receive_webhook(challenge, "")
    event = {"id": "event-bad", "type": "page.created",
             "entity": {"id": "a1b2c3d4e5f6478899aabbccddeeff00", "type": "page"}}
    raw, _ = _body(event, token)
    with pytest.raises(notion.WebhookError) as error:
        notion.receive_webhook(raw, "sha256=bad")
    assert error.value.status_code == 401


def test_import_page_creates_one_meeting_and_calls_pipeline(monkeypatch):
    root = "11111111111141118111111111111111"
    page_id = "a1b2c3d4e5f6478899aabbccddeeff00"
    settings_store.set_many({"notion_parent_id": root})
    monkeypatch.setattr(notion, "connection_status", lambda: {"connected": True})
    submitted = []
    monkeypatch.setattr(pipeline, "submit", submitted.append)

    def fake_api(method, path, params=None, json_body=None, retry_auth=True):
        if path == "/pages/" + page_id:
            return {"id": page_id, "url": "https://www.notion.so/" + page_id,
                    "parent": {"type": "page_id", "page_id": root},
                    "created_time": "2026-10-22T09:00:00.000Z",
                    "last_edited_time": "2026-10-22T09:00:00.000Z",
                    "properties": {"title": {"type": "title", "title": [{"plain_text": "Планёрка"}]}}}
        if path == "/blocks/" + page_id + "/children":
            return {"results": [
                {"type": "paragraph", "has_children": False,
                 "paragraph": {"rich_text": [{"plain_text": "00:00 Иван: проверить оплату до пятницы"}]}},
                {"type": "paragraph", "has_children": False,
                 "paragraph": {"rich_text": [{"plain_text": "00:06 Мария: подготовить отчёт"}]}},
            ], "has_more": False}
        raise AssertionError(path)

    monkeypatch.setattr(notion, "api_request", fake_api)
    first, second = notion.import_page(page_id), notion.import_page(page_id)
    assert first["status"] == "queued" and second["status"] == "duplicate"
    assert first["meeting_id"] == second["meeting_id"]
    assert submitted == [first["meeting_id"]]
    with SessionLocal() as s:
        meeting = s.get(Meeting, first["meeting_id"])
        assert meeting.source == "notion" and meeting.title == "Планёрка"
        assert len(meeting.transcript) == 2 and meeting.transcript[0]["speaker"] == "Иван"
        assert s.query(NotionImportedPage).count() == 1


def test_settings_render_connect_and_validate_parent(client, monkeypatch):
    monkeypatch.setattr(config, "NOTION_CLIENT_ID", "client-id")
    monkeypatch.setattr(config, "NOTION_CLIENT_SECRET", "client-secret")
    monkeypatch.setattr(config, "PUBLIC_URL", "https://sysai.example")
    response = client.get("/settings")
    assert response.status_code == 200 and "Notion" in response.text
    bad = client.post("/settings", data={"notion_parent_id": "not-a-page-id"}, follow_redirects=False)
    assert bad.status_code == 400
    good = client.post("/settings", data={
        "notion_parent_id": "https://www.notion.so/Workspace-11111111111141118111111111111111"
    }, follow_redirects=False)
    assert good.status_code == 303
    assert settings_store.get("notion_parent_id") == "11111111111141118111111111111111"
