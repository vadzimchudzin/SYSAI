"""Deterministic transcript grouping using the report's topic start times."""
import re


def _seconds(value):
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"(?:(\d+):)?(\d{1,2}):(\d{2})", value.strip())
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    minutes, seconds = int(minutes), int(seconds)
    if seconds > 59 or minutes > 59:
        return None
    return (int(hours or 0) * 3600) + minutes * 60 + seconds


def group_transcript(segments, topics):
    """Group every transcript segment under the latest topic timestamp without changing its order.

    Invalid or missing topic timestamps are ignored. If there are no usable topics, return the
    complete transcript as one section; no segment is dropped because of model output.
    """
    segments = list(segments or [])
    if not segments:
        return []
    timed_topics = []
    for index, topic in enumerate(topics or []):
        if not isinstance(topic, dict):
            continue
        start = _seconds(topic.get("time", ""))
        title = topic.get("title")
        if start is None or not isinstance(title, str) or not title.strip():
            continue
        timed_topics.append((start, index, title.strip(), topic.get("time", "").strip()))
    timed_topics.sort(key=lambda item: (item[0], item[1]))
    if not timed_topics:
        return [{"title": "Полная расшифровка", "time": "", "segments": segments}]

    prelude = []
    buckets = [[] for _ in timed_topics]
    for segment in segments:
        try:
            start = float(segment.get("start", 0) or 0)
        except (AttributeError, TypeError, ValueError):
            start = 0.0
        active = -1
        for index, topic in enumerate(timed_topics):
            if topic[0] <= start:
                active = index
            else:
                break
        (prelude if active < 0 else buckets[active]).append(segment)

    grouped = []
    if prelude:
        grouped.append({"title": "Начало обсуждения", "time": "", "segments": prelude})
    for topic, bucket in zip(timed_topics, buckets):
        if bucket:
            grouped.append({"title": topic[2], "time": topic[3], "segments": bucket})
    return grouped or [{"title": "Полная расшифровка", "time": "", "segments": segments}]
