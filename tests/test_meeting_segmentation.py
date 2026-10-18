from app import analyze, llm, pipeline
from app.db import Meeting, SessionLocal


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


def test_segment_creation_is_review_gated_and_keeps_source_audio(monkeypatch):
    segments = transcript_with_two_sessions()
    with SessionLocal() as session:
        parent = Meeting(
            title="Исходная запись",
            filename="long-meeting.mp3",
            audio_path="/tmp/long-meeting.mp3",
            source="web",
            status="analyzing",
            duration_sec=700,
            transcript=segments,
            options={"report_chat_ids": ["-100123"], "approval_required": False,
                     "send_tasks_to_assignees": True},
        )
        session.add(parent)
        session.commit()
        parent_id = parent.id

    processed = []
    monkeypatch.setattr(pipeline, "_process", lambda meeting_id: processed.append(meeting_id))
    sections = [
        {"start_index": 0, "end_index": 10, "start": 0.0, "end": 280.0,
         "title": "Маркетинг", "reason": "длительная пауза", "confidence": 0.96},
        {"start_index": 10, "end_index": 20, "start": 420.0, "end": 700.0,
         "title": "Техническое совещание", "reason": "длительная пауза", "confidence": 0.97},
    ]
    pipeline._create_segment_meetings(parent_id, segments, sections)

    with SessionLocal() as session:
        parent = session.get(Meeting, parent_id)
        children = session.query(Meeting).filter_by(parent_meeting_id=parent_id).order_by(Meeting.id).all()
        assert parent.status == "segmented"
        assert parent.audio_path == "/tmp/long-meeting.mp3"
        assert parent.options["segmentation_checked"] is True
        assert len(parent.report["segments"]) == 2
        assert len(children) == 2
        for child in children:
            assert child.audio_path == ""
            assert child.options["approval_required"] is True
            assert child.options["report_chat_ids"] == []
            assert child.options["send_tasks_to_assignees"] is False
        assert children[0].transcript == segments[:10]
        assert children[1].transcript == segments[10:]
    assert processed == [children[0].id, children[1].id]
