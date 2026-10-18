"""Regression tests for merging transcript chunks with restarted speaker numbering."""


from app import transcribe


def test_unique_overlap_utterance_stabilises_speaker_and_is_not_duplicated():
    segments = [
        {"speaker": "Спикер 1", "start": 59.0, "text": "Продолжим закупку оборудования"},
        {"speaker": "Спикер 1", "start": 57.0, "text": "Итак, начнем"},
    ]
    new = [
        {"speaker": "Спикер 2", "start": 60.0, "text": "Продолжим закупку оборудования"},
        {"speaker": "Спикер 2", "start": 62.0, "text": "Срок до пятницы"},
    ]

    transcribe._merge(segments, new, 60.0)

    assert len(segments) == 3
    assert [s["text"] for s in segments].count("Продолжим закупку оборудования") == 1
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
