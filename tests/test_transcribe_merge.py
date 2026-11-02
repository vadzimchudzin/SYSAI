"""Regression tests for merging transcript chunks with restarted speaker numbering."""


from app import transcribe


def test_unique_overlap_utterance_stabilises_speaker_and_is_not_duplicated():
    segments = [
        {"speaker": "Спикер 1", "start": 59.0, "text": "Продолжим закупку оборудования."},
        {"speaker": "Спикер 1", "start": 57.0, "text": "Итак, начнем"},
    ]
    new = [
        {"speaker": "Спикер 2", "start": 60.0, "text": "Продолжим закупку оборудования"},
        {"speaker": "Спикер 2", "start": 62.0, "text": "Срок до пятницы"},
    ]

    transcribe._merge(segments, new, 60.0)

    assert len(segments) == 3
    assert sum(s["text"].rstrip(".") == "Продолжим закупку оборудования" for s in segments) == 1
    assert next(s for s in segments if s["text"] == "Срок до пятницы")["speaker"] == "Спикер 1"


def test_ambiguous_overlap_does_not_force_a_speaker_mapping():
    segments = [
        {"speaker": "Спикер 1", "start": 59.0, "text": "Да, согласен"},
        {"speaker": "Спикер 3", "start": 61.0, "text": "Да, согласен"},
    ]
    new = [{"speaker": "Спикер 2", "start": 60.0, "text": "Да, согласен"}]

    transcribe._merge(segments, new, 60.0)

    assert len(segments) == 3
    assert next(s for s in segments if s["start"] == 60.0)["speaker"] == "Спикер 2"


def test_partial_utterance_in_overlap_is_kept_once():
    # The old chunk ends mid-sentence and the new chunk starts mid-sentence: the same words, never equal segments.
    segments = [
        {"speaker": "Спикер 1", "start": 48.0, "text": "Коротко по закупке."},
        {"speaker": "Спикер 2", "start": 55.0, "text": "Вентиляторы придут в среду, накладные проверю и напишу"},
    ]
    new = [
        {"speaker": "Спикер 1", "start": 60.0, "text": "в среду, накладные проверю и напишу в Битрикс до вечера."},
        {"speaker": "Спикер 2", "start": 66.0, "text": "Хорошо, спасибо."},
    ]

    transcribe._merge(segments, new, 60.0)

    text = " ".join(s["text"] for s in segments)
    assert text.count("накладные проверю") == 1
    assert "в Битрикс до вечера." in text
    # The shared words prove that the new chunk's «Спикер 1» is the old «Спикер 2».
    assert next(s for s in segments if "Битрикс" in s["text"])["speaker"] == "Спикер 2"
    # Its «Спикер 2» is unproven and must not be glued to the old «Спикер 2» (another voice): a new label.
    assert next(s for s in segments if s["text"] == "Хорошо, спасибо.")["speaker"] == "Спикер 3"
    assert [s["start"] for s in segments] == sorted(s["start"] for s in segments)


def test_overlap_without_a_unique_shared_run_keeps_every_word():
    segments = [{"speaker": "Спикер 1", "start": 58.0, "text": "Да, да, конечно"}]
    new = [{"speaker": "Спикер 1", "start": 61.0, "text": "Начнём с бюджета на квартал"}]

    transcribe._merge(segments, new, 60.0)

    assert [s["text"] for s in segments] == ["Да, да, конечно", "Начнём с бюджета на квартал"]
