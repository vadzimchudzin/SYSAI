"""Tests for converting STT endpoint segments into stable SYSAI transcript segments."""
from app import transcribe


def test_stt_segments_keep_words_and_normalize_provider_speaker_ids(tmp_path, monkeypatch):
    path = tmp_path / "chunk.mp3"
    path.write_bytes(b"audio")
    monkeypatch.setattr(transcribe.llm, "transcribe_audio", lambda *args, **kwargs: {
        "text": "Обсудим поставку. Срок до пятницы.",
        "segments": [
            {"start": 0.0, "end": 1.2, "text": "Обсудим поставку.", "speaker": "speaker_0"},
            {"start": 1.3, "end": 2.4, "text": "Срок до пятницы.", "speaker": "speaker_1"},
        ],
    })

    result = transcribe._transcribe_stt_chunk(path, {"transcribe_model": "openai/gpt-4o-transcribe"}, 5, "")

    assert result["segments"] == [
        {"start": 0.0, "text": "Обсудим поставку.", "speaker": "Спикер 1"},
        {"start": 1.3, "text": "Срок до пятницы.", "speaker": "Спикер 2"},
    ]


def test_stt_segments_without_speakers_use_the_separate_diarization_model(tmp_path, monkeypatch):
    path = tmp_path / "chunk.mp3"
    path.write_bytes(b"audio")
    monkeypatch.setattr(transcribe.llm, "transcribe_audio", lambda *args, **kwargs: {
        "text": "Уточнить бюджет.", "segments": [{"start": 1.0, "end": 2.0, "text": "Уточнить бюджет."}],
    })
    calls = []
    def fake_chat(model, messages, **kwargs):
        calls.append(model)
        return '{"speakers":[{"index":0,"speaker":"Спикер 2"}]}'
    monkeypatch.setattr(transcribe.llm, "chat", fake_chat)

    result = transcribe._transcribe_stt_chunk(
        path, {"transcribe_model": "openai/gpt-4o-transcribe", "diarization_model": "google/gemini-2.5-flash"},
        5, "Известный говорящий: Спикер 2"
    )

    assert calls == ["google/gemini-2.5-flash"]
    assert result["segments"][0]["speaker"] == "Спикер 2"
    assert result["segments"][0]["text"] == "Уточнить бюджет."
