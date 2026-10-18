from app import transcribe


def test_transcription_prompt_prioritizes_verbatim_speech_and_protects_numbers_and_negation():
    prompt = transcribe._prompt({"language": "ru", "glossary": "CRM, SYSAI"}, 30, "")
    assert "Сохраняй отрицания" in prompt
    assert "повторы" in prompt
    assert "не угадывай" in prompt
    assert "не является источником слов для расшифровки" in prompt
    assert "CRM, SYSAI" in prompt


def test_short_audio_prompt_does_not_invent_speakers_or_complete_words():
    prompt = transcribe._prompt({"language": "ru"}, 5, "")
    assert "очень короткая запись" in prompt
    assert "не создавай дополнительных говорящих" in prompt
    assert "Не дополняй оборванные слова по смыслу" in prompt


def test_previous_context_is_explicitly_only_for_speaker_continuity():
    context = transcribe._context([
        {"speaker": "Спикер 1", "text": "Анна проверит оплату."},
        {"speaker": "Спикер 2", "text": "Срок — пятница."},
    ])
    assert "только для согласованности меток говорящих" in context
    assert "не копируй" in context


def test_validation_preserves_repetitions_and_speaker_text_verbatim():
    data = {"segments": [{"speaker": "Спикер 1", "start": 0.2,
                          "text": "Ну, ну, я не уверен — не уверен."}], "truncated": False}
    result, truncated = transcribe._validate(data, offset=60, length=10)
    assert not truncated
    assert result == [{"speaker": "Спикер 1", "start": 60.2,
                       "text": "Ну, ну, я не уверен — не уверен."}]
