"""Sprint 7: TicNote mode switch, AppKey storage and key check in settings."""
from app import config, settings_store, ticnote


def test_settings_page_shows_ticnote_without_echoing_key(client):
    settings_store.set_many({"ticnote_appkey": "tnovs_sk_secret-value"})
    html = client.get("/settings").text
    assert 'id="ticnote"' in html
    assert 'name="ticnote_enabled"' in html
    assert 'name="ticnote_appkey"' in html
    assert "tnovs_sk_secret-value" not in html
    assert 'formaction="/settings/test-ticnote"' in html


def test_save_toggle_and_key(client):
    r = client.post("/settings", data={"present_ticnote_enabled": "1", "ticnote_enabled": "on",
                                       "action_ticnote_appkey": "replace", "ticnote_appkey": " tnovs_sk_abc "},
                    follow_redirects=False)
    assert r.status_code == 303
    s = settings_store.all_settings()
    assert s["ticnote_enabled"] is True
    assert s["ticnote_appkey"] == "tnovs_sk_abc"
    assert config.ticnote_appkey() == "tnovs_sk_abc"

    client.post("/settings", data={"present_ticnote_enabled": "1"}, follow_redirects=False)
    assert settings_store.get("ticnote_enabled") is False
    assert settings_store.get("ticnote_appkey") == "tnovs_sk_abc"  # key kept unless cleared


def test_check_key_reports_result(client, monkeypatch):
    settings_store.set_many({"ticnote_appkey": "tnovs_sk_abc"})
    monkeypatch.setattr(ticnote, "check_key", lambda key: "ключ принят, готовых записей: 3")
    r = client.post("/settings/test-ticnote", follow_redirects=False)
    assert r.status_code == 303
    assert "TicNote" in r.headers["location"]

    def bad(key):
        raise ticnote.TicNoteError("TicNote HTTP 401: bad")
    monkeypatch.setattr(ticnote, "check_key", bad)
    page = client.post("/settings/test-ticnote").text
    assert "401" in page


def test_check_key_without_key(client):
    page = client.post("/settings/test-ticnote").text
    assert "AppKey" in page
