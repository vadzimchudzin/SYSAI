"""Sprint 10: TicNote recordings survive a server restart and a revoked token."""
import pytest

from app import config, pipeline, settings_store, ticnote, ticnote_ingest, transcribe
from app.db import Meeting, SessionLocal, TicnoteImport


def test_wiped_disk_redownloads_ticnote_audio_and_goes_on(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "llm_enabled", lambda: True)
    settings_store.set_many({"ticnote_appkey": "tnovs_sk_x"})
    gone = tmp_path / "uploads" / "ticnote_r1.wav"          # was here before the restart
    mid = pipeline.create_meeting("p.wav", str(gone), source="ticnote", title="Планёрка")
    with SessionLocal() as s:
        s.add(TicnoteImport(record_id="r1", title="Планёрка", status="queued", attempts=1, meeting_id=mid))
        s.commit()
    asked = []
    monkeypatch.setattr(ticnote, "file_detail", lambda key, rid: asked.append(rid) or {"formatUrl": "https://cdn/x.wav"})
    monkeypatch.setattr(ticnote, "download_wav", lambda url, dest, timeout=180: dest.write_bytes(b"RIFF") or 4)
    heard = []

    def fake_transcribe(path, settings, progress=None, checkpoint=None):
        heard.append(path)
        raise RuntimeError("stop after transcription start")  # enough: we only check the audio came back
    monkeypatch.setattr(transcribe, "transcribe", fake_transcribe)

    pipeline.process(mid)
    assert asked == ["r1"] and heard == [str(gone)] and gone.is_file()
    with SessionLocal() as s:
        m = s.get(Meeting, mid)
        assert m.audio_path == str(gone)
        assert "Исходное аудио не найдено" not in (m.error or "")


def test_non_ticnote_meeting_without_audio_still_fails_clearly(monkeypatch):
    monkeypatch.setattr(config, "llm_enabled", lambda: True)
    mid = pipeline.create_meeting("a.mp3", "/nonexistent/a.mp3", source="web")
    pipeline.process(mid)
    with SessionLocal() as s:
        m = s.get(Meeting, mid)
        assert m.status == "error" and "Исходное аудио не найдено" in m.error


def test_revoked_token_logs_in_again_once(monkeypatch):
    ticnote.clear_token_cache()
    tokens, seen = iter(["old", "new"]), []

    def fake_request(method, url, token=None, body=None, timeout=30):
        if url.endswith("/api/p1/appkey/login"):
            return {"code": 200, "data": {"token": next(tokens)}}
        seen.append(token)
        if token == "old":
            raise ticnote.TicNoteError("TicNote HTTP 401: expired")
        return {"data": {"recordId": "r1", "formatUrl": "https://cdn/x.wav"}}
    monkeypatch.setattr(ticnote, "_request", fake_request)
    assert ticnote.file_detail("tnovs_sk_x", "r1")["recordId"] == "r1"
    assert seen == ["old", "new"]


def test_other_errors_are_not_retried(monkeypatch):
    ticnote.clear_token_cache()
    calls = []

    def fake_request(method, url, token=None, body=None, timeout=30):
        if url.endswith("/login"):
            return {"code": 200, "data": {"token": "t"}}
        calls.append(url)
        raise ticnote.TicNoteError("TicNote HTTP 500: down")
    monkeypatch.setattr(ticnote, "_request", fake_request)
    with pytest.raises(ticnote.TicNoteError, match="500"):
        ticnote.file_detail("tnovs_sk_x", "r1")
    assert len(calls) == 1
