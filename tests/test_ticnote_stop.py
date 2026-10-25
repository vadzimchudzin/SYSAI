"""Sprint 9 (part 2): stop a stuck meeting / TicNote recording and start it again."""
import datetime as dt
import threading

from app import config, pipeline, settings_store, ticnote_ingest, transcribe
from app.db import Meeting, SessionLocal, TicnoteImport


def status_of(mid):
    with SessionLocal() as s:
        m = s.get(Meeting, mid)
        return m.status, m.error, m.report


def test_stop_aborts_running_transcription_and_retry_restarts(monkeypatch, tmp_path):
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"RIFF")
    monkeypatch.setattr(config, "llm_enabled", lambda: True)
    gate, reached, after = threading.Event(), threading.Event(), []

    def slow_transcribe(path, settings, progress=None, checkpoint=None):
        progress("Расшифровка 1 из 2")
        reached.set()
        gate.wait(5)
        progress("Расшифровка 2 из 2")  # the stopped run must end here
        after.append("continued")
        return 1.0, [{"speaker": "A", "start": 0, "end": 1, "text": "привет"}]
    monkeypatch.setattr(transcribe, "transcribe", slow_transcribe)
    mid = pipeline.create_meeting("a.wav", str(audio), source="ticnote")
    worker = threading.Thread(target=pipeline.process, args=(mid,))
    worker.start()
    assert reached.wait(5)
    assert status_of(mid)[0] == "transcribing"

    assert pipeline.cancel_processing(mid) is True
    gate.set()
    worker.join(5)
    status, error, report = status_of(mid)
    assert status == "error" and "вручную" in error and report is None
    assert after == []                      # the old run did not go on
    assert mid not in pipeline._PROCESSING  # slot is free for a new run
    assert pipeline.cancel_processing(mid) is False  # nothing to stop any more

    started = []
    monkeypatch.setattr(pipeline, "submit", lambda m: started.append(m))
    assert pipeline.retry_processing(mid) is True
    assert started == [mid] and status_of(mid)[0] == "queued"


def test_stop_button_and_route(client):
    mid = pipeline.create_meeting("a.wav", "", source="ticnote", title="Планёрка")
    pipeline._set(mid, status="transcribing", progress="Расшифровка 1 из 3")
    html = client.get(f"/meetings/{mid}").text
    assert f'action="/meetings/{mid}/cancel-processing"' in html
    r = client.post(f"/meetings/{mid}/cancel-processing", follow_redirects=False)
    assert r.status_code == 303 and status_of(mid)[0] == "error"
    assert client.post(f"/meetings/{mid}/cancel-processing").status_code == 409


def _row(**kw):
    with SessionLocal() as s:
        s.add(TicnoteImport(**kw))
        s.commit()


def _get(rid):
    with SessionLocal() as s:
        return s.get(TicnoteImport, rid)


def test_ticnote_stop_and_retry_download(client):
    settings_store.set_many({"ticnote_enabled": True, "ticnote_appkey": "tnovs_sk_x"})
    _row(record_id="r1", title="Планёрка", status="downloading", attempts=1)
    item = client.get("/ticnote/status").json()["recent"][0]
    assert item["can_stop"] and not item["can_retry"]

    assert client.post("/ticnote/imports/r1/stop").json() == {"ok": True}
    row = _get("r1")
    assert row.status == "error" and row.attempts == ticnote_ingest.MAX_ATTEMPTS  # no automatic retry
    item = client.get("/ticnote/status").json()["recent"][0]
    assert item["can_retry"] and not item["can_stop"]

    assert client.post("/ticnote/imports/r1/retry").json() == {"ok": True}
    assert _get("r1").attempts == 0
    assert ticnote_ingest._claim("r1", "Планёрка") is True  # next pass takes it again
    assert client.post("/ticnote/imports/nope/stop").status_code == 409


def test_ticnote_stop_meeting_in_pipeline(client):
    mid = pipeline.create_meeting("a.wav", "", source="ticnote", title="Планёрка")
    pipeline._set(mid, status="analyzing")
    _row(record_id="r2", title="Планёрка", status="queued", attempts=1, meeting_id=mid)
    assert client.get("/ticnote/status").json()["recent"][0]["can_stop"]
    assert client.post("/ticnote/imports/r2/stop").status_code == 200
    assert status_of(mid)[0] == "error"


def test_download_cut_off_by_restart_is_not_stuck_forever(client):
    settings_store.set_many({"ticnote_enabled": True, "ticnote_appkey": "tnovs_sk_x"})
    old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=ticnote_ingest.STALE_MINUTES + 5)
    _row(record_id="r3", title="Планёрка", status="downloading", attempts=1, created_at=old, updated_at=old)
    data = client.get("/ticnote/status").json()
    assert data["mode"] == "waiting" and data["recent"][0]["state"] == "error"
    assert ticnote_ingest._claim("r3", "Планёрка") is True
