"""Dedicated OpenRouter transcription endpoint adapter tests."""
from types import SimpleNamespace

from app import config, llm


def test_transcribe_audio_retries_without_unsupported_timestamp_granularity(monkeypatch):
    calls = []
    costs = []
    responses = [
        SimpleNamespace(status_code=400, text="timestamp_granularities not supported", json=lambda: {}),
        SimpleNamespace(status_code=200, text="", json=lambda: {
            "text": "Проверка связи.", "segments": [{"start": 0.0, "text": "Проверка связи."}],
            "usage": {"cost": 0.01},
        }),
    ]
    monkeypatch.setattr(config, "openrouter_key", lambda: "test-key")
    monkeypatch.setattr(config, "OPENROUTER_BASE_URL", "https://openrouter.example/api/v1")
    monkeypatch.setattr(llm, "check_budget", lambda: None)
    monkeypatch.setattr(llm, "record_cost", lambda model, usage: costs.append((model, usage)))
    def fake_post(url, json, headers, timeout):
        calls.append((url, json, headers, timeout))
        return responses.pop(0)
    monkeypatch.setattr(llm.httpx, "post", fake_post)

    result = llm.transcribe_audio(b"fake audio", "openai/gpt-4o-transcribe", "mp3", "ru")

    assert result["text"] == "Проверка связи."
    assert len(calls) == 2
    assert calls[0][1]["timestamp_granularities"] == ["segment"]
    assert "timestamp_granularities" not in calls[1][1]
    assert "diarize" not in calls[0][1] and "diarize" not in calls[1][1]
    assert calls[1][1]["language"] == "ru"
    assert costs == [("openai/gpt-4o-transcribe", {"cost": 0.01})]
