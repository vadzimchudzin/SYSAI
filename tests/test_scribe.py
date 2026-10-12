"""ElevenLabs Scribe recognition: paragraphs and speakers from the real API answer, errors that are explained, and the answer
kept as a checkpoint so a failed analysis does not pay for recognition twice."""
import copy
import json
import pathlib

import httpx
import pytest

from app import config, llm, settings_store, transcribe

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "scribe_dialogue.json"


def real_answer():
    """The answer of the real API (scribe_v2, diarize=true, word timestamps) for a two-voice Russian dialogue."""
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def word(text, start, end, sid="speaker_0"):
    return {"text": text, "start": start, "end": end, "type": "word", "speaker_id": sid}


def space(sid="speaker_0"):
    return {"text": " ", "start": None, "end": None, "type": "spacing", "speaker_id": sid}


# ----------------------------------------------------------------------------- reading the real answer
def test_the_real_answer_becomes_one_paragraph_per_turn():
    segments = transcribe.scribe_segments(real_answer())
    assert [s["speaker"] for s in segments] == ["Спикер 1", "Спикер 2", "Спикер 1", "Спикер 2"]
    assert segments[0]["text"].startswith("Иван, давай смету на завтра.")
    assert segments[1]["text"].startswith("Да, возьму.")
    assert segments[3]["text"].endswith("на проверку.")
    assert [s["start"] for s in segments] == sorted(s["start"] for s in segments)


def test_no_word_is_lost_and_the_text_matches_the_service_text():
    data = real_answer()
    segments = transcribe.scribe_segments(data)
    assert " ".join(s["text"] for s in segments).split() == data["text"].split()


def test_the_first_speaker_to_talk_is_speaker_one_whatever_the_service_called_them():
    data = real_answer()
    for token in data["words"]:
        token["speaker_id"] = {"speaker_0": "speaker_7", "speaker_1": "speaker_2"}.get(token.get("speaker_id"), token.get("speaker_id"))
    assert [s["speaker"] for s in transcribe.scribe_segments(data)] == ["Спикер 1", "Спикер 2", "Спикер 1", "Спикер 2"]


def test_a_long_pause_starts_a_new_paragraph_for_the_same_speaker():
    data = {"words": [word("Раз", 0.0, 0.4), space(), word("два", 0.5, 0.9), space(), word("три", 4.0, 4.4)]}
    segments = transcribe.scribe_segments(data)
    assert [s["text"] for s in segments] == ["Раз два", "три"]
    assert segments[1]["start"] == 4.0


def test_a_very_long_run_is_split_so_one_paragraph_stays_readable():
    words = []
    t = 0.0
    for i in range(transcribe.SCRIBE_MAX_WORDS + 5):
        words += [word(f"w{i}", t, t + 0.3), space()]
        t += 0.35
    segments = transcribe.scribe_segments({"words": words})
    assert len(segments) == 2 and len(segments[0]["text"].split()) == transcribe.SCRIBE_MAX_WORDS


def test_audio_events_and_empty_tokens_are_ignored():
    data = {"words": [{"text": "(smile)", "type": "audio_event", "speaker_id": "speaker_0"},
                      word("Привет", 0.0, 0.5), space(), {"text": "", "type": "word", "start": 0.5, "end": 0.5, "speaker_id": "speaker_0"}]}
    assert [s["text"] for s in transcribe.scribe_segments(data)] == ["Привет"]


def test_a_word_without_time_is_an_error_not_a_silent_loss():
    with pytest.raises(llm.LLMError, match="без времени"):
        transcribe.scribe_segments({"words": [{"text": "Привет", "type": "word", "speaker_id": "speaker_0"}]})


def test_an_answer_without_words_is_an_error():
    with pytest.raises(llm.LLMError, match="без слов"):
        transcribe.scribe_segments({"text": "есть текст, нет слов"})


# ----------------------------------------------------------------------------- the request and its errors
class Response:
    def __init__(self, status, payload=None, text=""):
        self.status_code = status
        self._payload = payload
        self.text = text or (json.dumps(payload) if payload is not None else "")

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setattr(config, "ELEVENLABS_API_KEY", "test-key")
    monkeypatch.setattr(transcribe.time, "sleep", lambda seconds: None)


@pytest.fixture
def sent(monkeypatch):
    """Records each request to the service and answers with the queued responses."""
    calls, queue = [], []

    def post(url, headers=None, data=None, files=None, timeout=None):
        calls.append({"url": url, "headers": headers, "data": data, "files": files, "timeout": timeout})
        return queue.pop(0)
    monkeypatch.setattr(transcribe.httpx, "post", post)
    return {"calls": calls, "queue": queue}


def test_the_request_asks_for_speakers_and_word_times_in_russian(key, sent):
    sent["queue"].append(Response(200, real_answer()))
    answer = transcribe._scribe_post(b"mp3-bytes", "rus")
    call = sent["calls"][0]
    assert call["url"] == "https://api.elevenlabs.io/v1/speech-to-text"
    assert call["headers"] == {"xi-api-key": "test-key"}
    assert call["data"] == {"model_id": "scribe_v2", "diarize": "true", "timestamps_granularity": "word", "language_code": "rus"}
    assert call["files"]["file"][0] == "meeting.mp3" and call["timeout"] == transcribe.SCRIBE_TIMEOUT
    assert answer["words"]


def test_without_a_key_the_error_says_where_to_put_it(monkeypatch):
    monkeypatch.setattr(config, "ELEVENLABS_API_KEY", "")
    with pytest.raises(llm.LLMError, match="Нужен ключ ElevenLabs"):
        transcribe._scribe_post(b"x", "rus")


@pytest.mark.parametrize("status, message", [(401, "не принял ключ"), (403, "не принял ключ"), (402, "закончился баланс")])
def test_account_problems_are_explained_and_not_retried(key, sent, status, message):
    sent["queue"].append(Response(status, text="{}"))
    with pytest.raises(llm.LLMError, match=message):
        transcribe._scribe_post(b"x", "rus")
    assert len(sent["calls"]) == 1


def test_a_quota_message_is_recognised_as_a_balance_problem(key, sent):
    sent["queue"].append(Response(400, text='{"detail": "quota exceeded"}'))
    with pytest.raises(llm.LLMError, match="закончился баланс"):
        transcribe._scribe_post(b"x", "rus")


def test_a_rate_limit_is_retried_then_succeeds(key, sent):
    sent["queue"] += [Response(429, text="slow down"), Response(200, real_answer())]
    assert transcribe._scribe_post(b"x", "rus")["words"]
    assert len(sent["calls"]) == 2


def test_a_service_that_stays_down_is_reported(key, sent):
    sent["queue"] += [Response(503, text="busy")] * 3
    with pytest.raises(llm.LLMError, match="недоступен"):
        transcribe._scribe_post(b"x", "rus")
    assert len(sent["calls"]) == 3


def test_a_network_failure_is_retried(key, monkeypatch):
    attempts = []

    def post(*args, **kwargs):
        attempts.append(1)
        if len(attempts) < 2:
            raise httpx.ConnectTimeout("timed out")
        return Response(200, real_answer())
    monkeypatch.setattr(transcribe.httpx, "post", post)
    assert transcribe._scribe_post(b"x", "rus")["words"] and len(attempts) == 2


def test_an_answer_that_is_not_json_is_an_error(key, sent):
    sent["queue"].append(Response(200, payload=None, text="<html>"))
    with pytest.raises(llm.LLMError, match="не JSON"):
        transcribe._scribe_post(b"x", "rus")


# ----------------------------------------------------------------------------- the whole recording
class MemoryCheckpoint:
    def __init__(self):
        self.store = {}
        self.puts = 0

    def get(self, stage, index, key):
        return self.store.get((stage, str(index), key))

    def put(self, stage, index, key, value):
        self.puts += 1
        self.store[(stage, str(index), key)] = value


@pytest.fixture
def recording(tmp_path, monkeypatch):
    path = tmp_path / "meeting.mp3"
    path.write_bytes(b"fake recording " * 50)
    monkeypatch.setattr(transcribe.audio, "duration", lambda p: 13.44)

    def extract(source, destination, start, seconds):
        pathlib.Path(destination).write_bytes(b"prepared-mp3")
    monkeypatch.setattr(transcribe.audio, "extract", extract)
    return path


def test_a_recording_is_recognised_once_and_the_answer_is_kept(key, sent, recording):
    sent["queue"].append(Response(200, real_answer()))
    checkpoint = MemoryCheckpoint()
    total, segments = transcribe.transcribe(str(recording), {"language": "ru"}, checkpoint=checkpoint)
    assert total == 13.44 and len(segments) == 4
    assert segments[-1]["end"] == 13.4 and segments[0]["end"] == segments[1]["start"]  # end is the rounded duration
    assert sent["calls"][0]["data"]["language_code"] == "rus"
    # the second run (for example after the analysis failed) reuses the answer and pays nothing
    again_total, again = transcribe.transcribe(str(recording), {"language": "ru"}, checkpoint=checkpoint)
    assert len(sent["calls"]) == 1 and again == segments


def test_a_changed_recording_is_recognised_again(key, sent, recording, tmp_path):
    sent["queue"] += [Response(200, real_answer()), Response(200, real_answer())]
    checkpoint = MemoryCheckpoint()
    transcribe.transcribe(str(recording), {"language": "ru"}, checkpoint=checkpoint)
    other = tmp_path / "other.mp3"
    other.write_bytes(b"another recording")
    transcribe.transcribe(str(other), {"language": "ru"}, checkpoint=checkpoint)
    assert len(sent["calls"]) == 2


def test_a_recording_without_speech_is_reported_before_any_analysis(key, sent, recording):
    sent["queue"].append(Response(200, {"text": "", "words": [], "audio_duration_secs": 13.4}))
    with pytest.raises(llm.LLMError, match="не распознана"):
        transcribe.transcribe(str(recording), {"language": "ru"})


def test_the_provider_is_chosen_in_the_settings(monkeypatch, recording):
    seen = []
    monkeypatch.setattr(transcribe, "transcribe_scribe", lambda *a, **k: seen.append("scribe") or (1.0, []))
    monkeypatch.setattr(transcribe, "transcribe_gemini", lambda *a, **k: seen.append("gemini") or (1.0, []))
    transcribe.transcribe(str(recording), {})
    transcribe.transcribe(str(recording), {"transcribe_provider": "elevenlabs"})
    transcribe.transcribe(str(recording), {"transcribe_provider": "gemini"})
    assert seen == ["scribe", "scribe", "gemini"]


def test_the_default_provider_is_scribe():
    assert settings_store.DEFAULTS["transcribe_provider"] == "elevenlabs"
    assert "elevenlabs_api_key" in settings_store.SECRET_KEYS


# ----------------------------------------------------------------------------- the settings page
def test_the_settings_offer_the_provider_and_hide_the_key(client):
    settings_store.set_many({"elevenlabs_api_key": "sk-secret-elevenlabs-key"})
    page = client.get("/settings").text
    assert "Распознавание речи" in page and "ElevenLabs Scribe" in page and "Gemini (через OpenRouter)" in page
    assert "sk-secret-elevenlabs-key" not in page and "Сохранён в приложении" in page


def test_saving_the_provider_validates_it(client):
    ok = client.post("/settings", data={"transcribe_provider": "gemini"}, follow_redirects=False)
    assert ok.status_code == 303 and settings_store.get("transcribe_provider") == "gemini"
    bad = client.post("/settings", data={"transcribe_provider": "whisper"})
    assert bad.status_code == 400 and settings_store.get("transcribe_provider") == "gemini"


def test_the_elevenlabs_key_is_saved_and_can_be_cleared(client):
    client.post("/settings", data={"action_elevenlabs_api_key": "replace", "elevenlabs_api_key": "sk-new"})
    assert settings_store.get("elevenlabs_api_key") == "sk-new"
    client.post("/settings", data={"action_elevenlabs_api_key": "clear"})
    assert settings_store.get("elevenlabs_api_key") == ""
