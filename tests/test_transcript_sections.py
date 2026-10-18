from app.transcript_sections import group_transcript


def segment(start, text):
    return {"start": start, "end": start + 2, "speaker": "Спикер 1", "text": text}


def test_group_transcript_uses_latest_topic_and_preserves_all_segments_in_order():
    segments = [segment(0, "Вступление"), segment(12, "Обсудили сроки"),
                segment(35, "Обсудили бюджет"), segment(50, "Уточнили бюджет")]
    topics = [{"title": "Сроки", "time": "00:10"}, {"title": "Бюджет", "time": "00:30"}]
    result = group_transcript(segments, topics)
    assert [section["title"] for section in result] == ["Начало обсуждения", "Сроки", "Бюджет"]
    assert [[item["text"] for item in section["segments"]] for section in result] == [
        ["Вступление"], ["Обсудили сроки"], ["Обсудили бюджет", "Уточнили бюджет"]]
    flattened = [item for section in result for item in section["segments"]]
    assert flattened == segments


def test_group_transcript_ignores_invalid_topics_and_falls_back_to_full_transcript():
    segments = [segment(0, "Первое"), segment(5, "Второе")]
    assert group_transcript(segments, [{"title": "Без времени"}, {"title": "Плохое время", "time": "99:99"}]) == [
        {"title": "Полная расшифровка", "time": "", "segments": segments}]


def test_group_transcript_ignores_empty_topic_buckets_and_supports_long_recordings():
    segments = [segment(10, "Перед темой"), segment(3605, "После часа")]
    topics = [{"title": "Empty topic", "time": "00:30"}, {"title": "После часа", "time": "1:00:02"}]
    result = group_transcript(segments, topics)
    assert [section["title"] for section in result] == ["Начало обсуждения", "После часа"]
    assert [item["text"] for section in result for item in section["segments"]] == ["Перед темой", "После часа"]
