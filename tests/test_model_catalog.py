"""Model picker: OpenRouter catalog shaping, routing of STT-only ids and saving of catalog ids."""
import httpx

from app import config, settings_store, transcribe

CHAT = {"data": [
    {"id": "google/gemini-2.5-flash", "name": "Google: Gemini 2.5 Flash",
     "architecture": {"input_modalities": ["text", "audio"], "output_modalities": ["text"]},
     "pricing": {"prompt": "0.0000003", "completion": "0.0000025"}},
    {"id": "google/gemini-2.5-flash:batch", "name": "Google: Gemini 2.5 Flash (batch)",
     "architecture": {"input_modalities": ["text", "audio"], "output_modalities": ["text"]},
     "pricing": {"prompt": "0.00000015", "completion": "0.00000125"}},
    {"id": "google/gemini-2.5-flash-image", "name": "Google: Nano Banana",
     "architecture": {"input_modalities": ["text", "image", "audio"], "output_modalities": ["image"]},
     "pricing": {"prompt": "0.0000003", "completion": "0.0000025"}},
    {"id": "~google/gemini-flash-latest", "name": "Google: Gemini Flash Latest",
     "architecture": {"input_modalities": ["text", "audio"], "output_modalities": ["text"]},
     "pricing": {"prompt": "0.0000003", "completion": "0.0000025"}},
    {"id": "openrouter/free", "name": "Free Models Router",
     "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
     "pricing": {"prompt": "0", "completion": "0"}},
]}
STT = {"data": [
    {"id": "elevenlabs/scribe-v2", "name": "ElevenLabs: Scribe v2",
     "pricing": {"prompt": "0.0000611111111111", "completion": "0"},
     "description": "Speech-to-text with word-level timestamps and optional speaker diarization."},
    {"id": "google/gemini-3.5-transcribe", "name": "Google: Gemini 3.5 Transcribe",
     "pricing": {"prompt": "0.000002", "completion": "0.000012"}},
    {"id": "microsoft/mai-transcribe-2", "name": "Microsoft AI: MAI-Transcribe 2",
     "pricing": {"prompt": "0.1", "completion": "0"}},
]}


def fake_catalog(monkeypatch, chat=CHAT, stt=STT):
    def fake_get(url, headers=None, timeout=None):
        payload = stt if "output_modalities=transcription" in url else chat
        request = httpx.Request("GET", url)
        if isinstance(payload, Exception):
            raise payload
        return httpx.Response(200, json=payload, request=request)
    monkeypatch.setattr(httpx, "get", fake_get)


def test_gemini_transcribe_takes_the_stt_path_not_the_chat_path():
    assert not transcribe.chat_model("google/gemini-3.5-transcribe")
    assert transcribe.stt_only_model("google/gemini-3.5-transcribe")
    assert transcribe.stt_only_model("mistralai/voxtral-small-24b-2507-stt")
    assert transcribe.stt_only_model("qwen/qwen3-asr-1.7b")
    assert transcribe.chat_model("google/gemini-2.5-flash") and transcribe.chat_model("~google/gemini-flash-latest")
    assert not transcribe.stt_only_model("google/gemini-2.5-flash-lite")


def test_catalog_groups_stt_and_one_pass_gemini_with_prices(client, monkeypatch):
    fake_catalog(monkeypatch)
    data = client.get("/settings/models").json()
    stt = {m["id"]: m for m in data["transcription_models"]}
    assert stt["google/gemini-3.5-transcribe"]["kind"] == "stt"
    assert stt["elevenlabs/scribe-v2"]["usd_per_hour"] == 0.22
    assert stt["google/gemini-3.5-transcribe"]["usd_per_hour"] is None  # token-priced
    assert stt["microsoft/mai-transcribe-2"]["usd_per_hour"] is None    # not a per-second price
    assert stt["google/gemini-2.5-flash"]["kind"] == "chat"
    assert "google/gemini-2.5-flash:batch" not in stt           # batch queue is too slow for meetings
    assert "google/gemini-2.5-flash-image" not in stt           # no text output
    assert data["stt_count"] == 3 and data["warnings"] == []
    models = {m["id"]: m for m in data["models"]}
    assert models["google/gemini-2.5-flash:batch"]["batch"] is True
    assert models["openrouter/free"]["free"] is True
    assert models["~google/gemini-flash-latest"]["provider"] == "google"


def test_one_failing_catalog_keeps_the_other_lists(client, monkeypatch):
    fake_catalog(monkeypatch, stt=httpx.ConnectError("stt down"))
    data = client.get("/settings/models").json()
    assert data["count"] == len(CHAT["data"]) and data["stt_count"] == 0
    assert data["warnings"] and {m["kind"] for m in data["transcription_models"]} == {"chat"}
    fake_catalog(monkeypatch, chat=httpx.ConnectError("down"), stt=httpx.ConnectError("down"))
    assert client.get("/settings/models").status_code == 502


def test_latest_alias_ids_from_the_catalog_can_be_saved(client):
    before = settings_store.get("report_model")
    try:
        response = client.post("/settings", data={"report_model": "~google/gemini-flash-latest"}, follow_redirects=False)
        assert response.status_code in (200, 303)
        assert settings_store.get("report_model") == "~google/gemini-flash-latest"
        assert client.post("/settings", data={"report_model": "bad model"}).status_code == 400
    finally:
        settings_store.set_many({"report_model": before})


def test_the_settings_page_keeps_the_saved_model_in_the_select(client):
    html = client.get("/settings").text
    current = settings_store.get("transcribe_model")
    assert f'data-current="{current}"' in html and 'class="model-filter"' in html
    assert 'id="transcribe-model-hint"' in html
