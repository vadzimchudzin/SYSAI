from app import analyze, llm


def transcript_with_two_sessions():
    segments = []
    for index in range(20):
        start = index * 30 if index < 10 else 420 + (index - 10) * 30
        segments.append({
            "speaker": f"Спикер {1 + index % 3}",
            "start": float(start),
            "end": float(start + 10),
            "text": f"Реплика {index}",
        })
    return segments


def test_meeting_sections_require_a_supported_boundary(monkeypatch):
    monkeypatch.setattr(llm, "chat", lambda *args, **kwargs:
        '{"sections":['
        '{"start_index":0,"end_index":10,"title":"Маркетинг","reason":"длительная пауза","confidence":0.96},'
        '{"start_index":10,"end_index":20,"title":"Техническое совещание","reason":"длительная пауза","confidence":0.97}'
        ']}')
    sections = analyze.detect_meeting_sections(
        transcript_with_two_sessions(), {"report_model": "test/model"}
    )
    assert len(sections) == 2
    assert sections[0]["start"] == 0
    assert sections[0]["end"] == 280
    assert sections[1]["start"] == 420
    assert sections[1]["end"] == 700


def test_meeting_sections_reject_topic_split_without_pause_or_explicit_transition(monkeypatch):
    segments = [
        {"speaker": "Спикер 1", "start": float(i * 30), "end": float(i * 30 + 10), "text": f"Реплика {i}"}
        for i in range(20)
    ]
    monkeypatch.setattr(llm, "chat", lambda *args, **kwargs:
        '{"sections":['
        '{"start_index":0,"end_index":10,"title":"Первая тема","reason":"смена темы","confidence":0.99},'
        '{"start_index":10,"end_index":20,"title":"Вторая тема","reason":"смена темы","confidence":0.99}'
        ']}')
    assert analyze.detect_meeting_sections(segments, {"report_model": "test/model"}) == []


def test_meeting_sections_reject_incomplete_coverage(monkeypatch):
    monkeypatch.setattr(llm, "chat", lambda *args, **kwargs:
        '{"sections":['
        '{"start_index":0,"end_index":10,"title":"Первая","reason":"пауза","confidence":0.99},'
        '{"start_index":11,"end_index":20,"title":"Вторая","reason":"пауза","confidence":0.99}'
        ']}')
    assert analyze.detect_meeting_sections(transcript_with_two_sessions(), {"report_model": "test/model"}) == []
