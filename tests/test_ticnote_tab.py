"""Sprint 9: TicNote tab on the new meeting page, status that lights up while a recording is taken."""
from app import pipeline, settings_store, ticnote_ingest
from app.db import SessionLocal, TicnoteImport


def test_new_meeting_page_has_ticnote_tab(client):
    html = client.get("/app/new").text
    assert 'value="ticnote"' in html and 'id="ticnote-pane"' in html
    html = client.get("/app/new?source=ticnote").text
    assert 'data-pane="ticnote"' in html and 'class="pane on" data-pane="ticnote"' in html
    assert 'class="pane " data-pane="file"' in html  # file pane hidden, its input not required


def test_status_modes(client):
    settings_store.set_many({"ticnote_enabled": False})
    assert client.get("/ticnote/status").json()["mode"] == "off"
    settings_store.set_many({"ticnote_enabled": True, "ticnote_appkey": ""})
    assert client.get("/ticnote/status").json()["mode"] == "nokey"
    settings_store.set_many({"ticnote_appkey": "tnovs_sk_x"})
    assert client.get("/ticnote/status").json()["mode"] == "waiting"


def test_status_busy_while_recording_is_taken_then_links_meeting(client):
    settings_store.set_many({"ticnote_enabled": True, "ticnote_appkey": "tnovs_sk_x"})
    with SessionLocal() as s:
        s.add(TicnoteImport(record_id="r1", title="Планёрка", status="downloading", attempts=1))
        s.commit()
    data = client.get("/ticnote/status").json()
    assert data["mode"] == "busy" and data["recent"][0]["title"] == "Планёрка"
    mid = pipeline.create_meeting("p.wav", "", source="ticnote", title="Планёрка")
    pipeline._set(mid, status="awaiting_approval")
    with SessionLocal() as s:
        row = s.get(TicnoteImport, "r1")
        row.status, row.meeting_id = "queued", mid
        s.commit()
    data = client.get("/ticnote/status").json()
    assert data["mode"] == "waiting"
    assert data["recent"][0]["meeting_id"] == mid and data["recent"][0]["label"] == "Ждёт проверки"


def test_scan_now_requires_mode_and_runs_scan(client, monkeypatch):
    settings_store.set_many({"ticnote_enabled": False})
    assert client.post("/ticnote/scan").status_code == 409
    settings_store.set_many({"ticnote_enabled": True, "ticnote_appkey": "tnovs_sk_x"})
    calls = []
    monkeypatch.setattr(ticnote_ingest, "scan", lambda force=False: calls.append(force) or [])
    assert client.post("/ticnote/scan").json() == {"ok": True}
    assert calls == [True]


def test_status_shows_poll_error(client):
    settings_store.set_many({"ticnote_enabled": True, "ticnote_appkey": "tnovs_sk_x"})
    ticnote_ingest.STATE.update(last_scan_at=1.0, last_error="TicNote HTTP 401: bad")
    try:
        assert client.get("/ticnote/status").json()["last_error"].startswith("TicNote HTTP 401")
    finally:
        ticnote_ingest.STATE.update(last_scan_at=0.0, last_error="")


def test_dashboard_has_ticnote_quick_access(client):
    settings_store.set_many({"ticnote_enabled": False})
    html = client.get("/app").text
    assert 'id="tn-dash"' in html and 'href="/app/new?source=ticnote"' in html and 'data-mode="off"' in html
    settings_store.set_many({"ticnote_enabled": True, "ticnote_appkey": "tnovs_sk_x"})
    assert 'data-mode="waiting"' in client.get("/app").text
