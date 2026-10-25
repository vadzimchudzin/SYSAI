"""TicNote client: login, list ready recordings, WAV URL — no transcript/summary used."""
import io
import json
from urllib.error import HTTPError

import pytest

from app import ticnote


class FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.status = status

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_resolve_base_url_overseas_and_unknown():
    assert ticnote.resolve_base_url("tnovs_sk_abc") == "https://ainote-api.mobvoi.com"
    with pytest.raises(ticnote.TicNoteError):
        ticnote.resolve_base_url("sk-other")


def test_login_caches_token(monkeypatch):
    ticnote.clear_token_cache()
    calls = []

    def fake_urlopen(req, timeout=30):
        calls.append(req.full_url)
        return FakeResp({"code": 200, "data": {"token": "jwt-1", "tokenType": "Bearer"}})

    monkeypatch.setattr(ticnote.urllib.request, "urlopen", fake_urlopen)
    assert ticnote.login("tnovs_sk_testkey") == "jwt-1"
    assert ticnote.login("tnovs_sk_testkey") == "jwt-1"
    assert len(calls) == 1


def test_ready_recordings_skips_failed_and_listen(monkeypatch):
    ticnote.clear_token_cache()
    tree = {"fileTree": [
        {"id": "1", "name": "ok", "fileType": "recording_file", "status": 2},
        {"id": "2", "name": "fail", "fileType": "recording_file", "status": 3},
        {"id": "3", "name": "listen", "fileType": "listen_translation", "status": 2},
        {"id": "4", "name": "welcome", "fileType": "upload_recording", "status": 2},
    ]}
    chats = [{"name": "Recordings", "project_id": "root-1"}]

    def fake_request(method, url, **kw):
        if url.endswith("/login"):
            return {"code": 200, "data": {"token": "t"}}
        if "file-index/chats" in url:
            return {"data": chats}
        if "file-tree" in url:
            assert "rootId=root-1" in url
            return tree
        raise AssertionError(url)

    monkeypatch.setattr(ticnote, "_request", fake_request)
    recs = ticnote.ready_recordings("tnovs_sk_x")
    assert [r["id"] for r in recs] == ["1", "4"]


def test_audio_url_prefers_wav_not_avo():
    url = ticnote.audio_url({
        "fileUrl": "https://cdn.example/raw.avo",
        "formatUrl": "https://cdn.example/clip.wav",
    })
    assert url.endswith(".wav")
    with pytest.raises(ticnote.TicNoteError):
        ticnote.audio_url({"fileUrl": "https://cdn.example/raw.avo"})


def test_download_wav(monkeypatch, tmp_path):
    monkeypatch.setattr(ticnote.urllib.request, "urlopen", lambda req, timeout=180: FakeResp(b"RIFF....WAV"))
    dest = tmp_path / "a.wav"
    assert ticnote.download_wav("https://cdn.example/a.wav", dest) == 11
    assert dest.read_bytes()[:4] == b"RIFF"
    buf = io.BytesIO()
    ticnote.download_wav("https://cdn.example/a.wav", buf)
    assert buf.getvalue() == b"RIFF....WAV"


def test_login_http_error(monkeypatch):
    ticnote.clear_token_cache()

    def boom(req, timeout=30):
        raise HTTPError(req.full_url, 401, "no", hdrs=None, fp=io.BytesIO(b"bad key"))

    monkeypatch.setattr(ticnote.urllib.request, "urlopen", boom)
    with pytest.raises(ticnote.TicNoteError, match="401"):
        ticnote.login("tnovs_sk_bad")
