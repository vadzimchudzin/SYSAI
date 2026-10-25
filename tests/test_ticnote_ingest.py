"""Sprint 8: TicNote mode picks up new device recordings and queues them in our pipeline (audio only)."""
import pytest

from app import pipeline, settings_store, ticnote, ticnote_ingest
from app.db import Meeting, SessionLocal, TicnoteImport

T0 = 1_800_000_000_000  # ms: the moment the mode was switched on


def node(rid, add_ms, ftype="recording_file", status=2, name="Планёрка"):
    return {"id": rid, "name": name, "fileType": ftype, "status": status,
            "createTimestamp": add_ms - 60_000, "addTimestamp": add_ms}


@pytest.fixture
def tn(monkeypatch, tmp_path):
    from app import config
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    settings_store.set_many({"ticnote_enabled": True, "ticnote_appkey": "tnovs_sk_x", "ticnote_enabled_at": T0})
    state = {"nodes": [], "detail": {"formatUrl": "https://cdn.example/a.wav", "transcodeStatus": "suc"},
             "downloads": [], "submitted": [], "fail": None}
    monkeypatch.setattr(ticnote, "ready_recordings", lambda key: state["nodes"])
    monkeypatch.setattr(ticnote, "file_detail", lambda key, rid: dict(state["detail"]))

    def fake_download(url, dest, timeout=180):
        if state["fail"]:
            raise ticnote.TicNoteError(state["fail"])
        state["downloads"].append(url)
        open(dest, "wb").write(b"RIFFdata")
        return 8
    monkeypatch.setattr(ticnote, "download_wav", fake_download)
    monkeypatch.setattr(pipeline, "submit", lambda mid: state["submitted"].append(mid))
    ticnote_ingest._last_scan[0] = 0
    return state


def meetings():
    with SessionLocal() as s:
        return s.query(Meeting).order_by(Meeting.id).all()


def test_new_recording_becomes_meeting_once(tn):
    tn["nodes"] = [node("r1", T0 + 5_000, name="Совещание по срокам")]
    queued = ticnote_ingest.scan(force=True)
    assert len(queued) == 1 and tn["submitted"] == queued
    m = meetings()[0]
    assert m.source == "ticnote" and m.title == "Совещание по срокам" and m.audio_path.endswith("ticnote_r1.wav")
    assert tn["downloads"] == ["https://cdn.example/a.wav"]
    # next pass: nothing new, no duplicate meeting
    assert ticnote_ingest.scan(force=True) == []
    assert len(meetings()) == 1


def test_history_before_switch_on_and_other_types_are_ignored(tn):
    tn["nodes"] = [node("old", T0 - 1), node("live", T0 + 1, ftype="listen_translation"),
                   node("upl", T0 + 1, ftype="upload_recording"), node("failed", T0 + 1, status=3)]
    assert ticnote_ingest.scan(force=True) == []
    assert meetings() == []


def test_offline_recording_uploaded_later_is_taken(tn):
    n = node("late", T0 + 10_000)
    n["createTimestamp"] = T0 - 3_600_000  # recorded an hour before, synced after the switch
    tn["nodes"] = [n]
    assert len(ticnote_ingest.scan(force=True)) == 1


def test_wav_not_ready_is_retried_later_without_spending_attempts(tn):
    tn["nodes"] = [node("r1", T0 + 1)]
    tn["detail"] = {"formatUrl": "", "transcodeStatus": "ing"}
    assert ticnote_ingest.scan(force=True) == []
    with SessionLocal() as s:
        assert s.get(TicnoteImport, "r1") is None
    tn["detail"] = {"formatUrl": "https://cdn.example/a.wav", "transcodeStatus": "suc"}
    assert len(ticnote_ingest.scan(force=True)) == 1


def test_download_error_retries_then_gives_up(tn):
    tn["nodes"] = [node("r1", T0 + 1)]
    tn["fail"] = "сеть"
    for _ in range(ticnote_ingest.MAX_ATTEMPTS + 2):
        ticnote_ingest.scan(force=True)
    with SessionLocal() as s:
        row = s.get(TicnoteImport, "r1")
        assert row.status == "error" and row.attempts == ticnote_ingest.MAX_ATTEMPTS and "сеть" in row.error
    assert meetings() == []


def test_mode_off_or_no_key_does_nothing(tn, monkeypatch):
    tn["nodes"] = [node("r1", T0 + 1)]
    settings_store.set_many({"ticnote_enabled": False})
    assert ticnote_ingest.scan(force=True) == []
    settings_store.set_many({"ticnote_enabled": True, "ticnote_appkey": ""})
    from app import config
    monkeypatch.setattr(config, "TICNOTE_APPKEY", "")
    assert ticnote_ingest.scan(force=True) == []
    assert meetings() == []


def test_poll_is_throttled(tn):
    tn["nodes"] = [node("r1", T0 + 1)]
    ticnote_ingest.scan(force=True)
    tn["nodes"].append(node("r2", T0 + 2))
    assert ticnote_ingest.scan() == []  # within POLL_SECONDS
    assert len(meetings()) == 1


def test_switching_mode_on_in_settings_sets_start_point(client):
    settings_store.set_many({"ticnote_enabled": False, "ticnote_enabled_at": 0})
    client.post("/settings", data={"present_ticnote_enabled": "1", "ticnote_enabled": "on"})
    first = settings_store.get("ticnote_enabled_at")
    assert first > 0
    client.post("/settings", data={"present_ticnote_enabled": "1", "ticnote_enabled": "on"})
    assert settings_store.get("ticnote_enabled_at") == first  # saving again keeps the start point
