"""Short replies and a change of voice inside a phrase (Кто говорит №5): the transcript is cut by word times, a short
«да» is labelled by its own voice, and cross-talk is marked instead of glued."""
import json
import math
import pathlib

import pytest

from app import diarize, llm, speaker_id, transcribe
from app.db import Meeting, SessionLocal


def vec(*weights):
    raw = list(weights) + [0.0] * (192 - len(weights))
    norm = math.sqrt(sum(x * x for x in raw))
    return [x / norm for x in raw]


ANNA, BORIS = vec(1, 0, 0), vec(0, 1, 0)


def seg(speaker, start, end, text="обсуждаем план работ на эту неделю подробно", **extra):
    return dict(speaker=speaker, start=start, end=end, text=text, **extra)


def group(window, label, embedding, seconds=20.0, **extra):
    return dict(window=window, label=label, embedding=embedding, seconds=seconds, **extra)


# ------------------------------------------------------------------------------------------- transcription
def test_the_end_and_the_cross_talk_mark_of_a_replica_are_kept():
    data = {"segments": [{"speaker": "Спикер 1", "start": 0.5, "end": 3.2, "text": "Акт подписан."},
                         {"speaker": "Спикер 2", "start": 3.0, "end": 3.6, "text": "Да.", "overlap": True},
                         {"speaker": "Спикер 1", "start": 4.0, "end": 2.0, "text": "Отправлю завтра.", "overlap": "yes"}]}
    result, _ = transcribe._validate(data, offset=60, length=10)
    assert result == [{"speaker": "Спикер 1", "start": 60.5, "end": 63.2, "text": "Акт подписан."},
                      {"speaker": "Спикер 2", "start": 63.0, "end": 63.6, "text": "Да.", "overlap": True},
                      {"speaker": "Спикер 1", "start": 64.0, "text": "Отправлю завтра."}]  # an end before the start is dropped


def test_ends_keep_pauses_and_marked_cross_talk_only():
    segments = transcribe._ends([
        {"speaker": "Спикер 1", "start": 0.0, "end": 2.0, "text": "Начинаем планёрку."},        # a pause after it
        {"speaker": "Спикер 2", "start": 3.0, "end": 9.0, "text": "Акт подписан вчера."},     # runs into the next
        {"speaker": "Спикер 1", "start": 5.0, "end": 12.0, "text": "Хорошо, спасибо.", "overlap": True},
        {"speaker": "Спикер 2", "start": 6.0, "text": "А оригинал отправили по почте?"},       # no end of its own
        {"speaker": "Спикер 1", "start": 8.0, "end": 8.01, "text": "Да, отправили утром курьером."},  # impossible
        {"speaker": "Спикер 2", "start": 11.0, "end": 30.0, "text": "Отлично."}], total=20.0)
    assert [s["end"] for s in segments] == [2.0, 8.0, 9.0, 8.0, 11.0, 20.0]
    # 2: cross-talk marked on the next replica, so it may overlap it by at most MAX_OVERLAP_SECONDS (5 + 3 = 8)
    # 3: its own mark: 12 is capped at 6 + 3 = 9


def test_word_times_cut_the_text_at_pauses_and_the_model_names_each_piece(monkeypatch, tmp_path):
    path = tmp_path / "part.mp3"
    path.write_bytes(b"audio")
    text = "Акт подписан вчера? Да. Тогда отправляем оригинал."
    words = [("Акт", 0.0, 0.4), ("подписан", 0.45, 1.0), ("вчера", 1.05, 1.5), ("Да", 1.9, 2.1),
             ("Тогда", 2.9, 3.2), ("отправляем", 3.25, 3.9), ("оригинал", 3.95, 4.6)]
    result = {"text": text, "words": [{"word": w, "start": a, "end": b} for w, a, b in words]}
    asked = []

    def chat(model, messages, **kwargs):
        asked.append(json.loads(messages[0]["content"][0]["text"].split("Сегменты: ", 1)[1]))
        return json.dumps({"speakers": [{"index": 0, "speaker": "Спикер 1"}, {"index": 1, "speaker": "Спикер 2"},
                                        {"index": 2, "speaker": "Спикер 1"}]})
    monkeypatch.setattr(llm, "chat", chat)
    out = transcribe._by_words(path, result, {}, 10.0, "")["segments"]
    assert [item["text"] for item in asked[0]] == ["Акт подписан вчера?", "Да.", "Тогда отправляем оригинал."]
    assert out == [{"start": 0.0, "end": 1.5, "text": "Акт подписан вчера?", "speaker": "Спикер 1"},
                   {"start": 1.9, "end": 2.1, "text": "Да.", "speaker": "Спикер 2"},
                   {"start": 2.9, "end": 4.6, "text": "Тогда отправляем оригинал.", "speaker": "Спикер 1"}]


def test_speakers_of_the_words_are_taken_as_they_are_and_one_voice_stays_one_replica(monkeypatch):
    monkeypatch.setattr(llm, "chat", lambda *a, **k: pytest.fail("no model is needed"))
    text = "Начинаем. Да, слушаю. Отлично, тогда по порядку."
    words = [("Начинаем", 0.0, 0.6, "A"), ("Да", 0.9, 1.0, "B"), ("слушаю", 1.1, 1.5, "B"),
             ("Отлично", 1.7, 2.1, "A"), ("тогда", 2.3, 2.6, "A"), ("по", 3.4, 3.5, "A"), ("порядку", 3.5, 4.0, "A")]
    result = {"text": text, "words": [{"text": w, "start": a, "end": b, "speaker_id": who, "type": "word"}
                                      for w, a, b, who in words] + [{"text": " ", "start": 4.0, "end": 4.1, "type": "spacing"}]}
    out = transcribe._by_words(pathlib.Path("unused.mp3"), result, {}, 10.0, "")["segments"]
    assert [(item["speaker"], item["text"]) for item in out] == [
        ("Спикер 1", "Начинаем."), ("Спикер 2", "Да, слушаю."), ("Спикер 1", "Отлично, тогда по порядку.")]


def test_without_word_times_the_phrases_of_the_provider_are_used():
    assert transcribe._by_words(pathlib.Path("x.mp3"), {"text": "Привет.", "segments": []}, {}, 5.0, "") is None
    broken = {"text": "Привет всем.", "words": [{"word": "Привет", "start": "?", "end": 1}]}
    assert transcribe._by_words(pathlib.Path("x.mp3"), broken, {}, 5.0, "") is None


def test_the_projection_keeps_the_end_and_the_cross_talk_of_the_model():
    text = "Добрый день, коллеги. Да."
    pieces = transcribe._project(text, [{"speaker": "Спикер 1", "start": 0, "end": 1.8, "text": "Добрый день коллеги"},
                                        {"speaker": "Спикер 2", "start": 1.6, "end": 2.0, "text": "да", "overlap": True}], 5.0)
    assert pieces == [{"speaker": "Спикер 1", "start": 0.0, "end": 1.8, "text": "Добрый день, коллеги."},
                      {"speaker": "Спикер 2", "start": 1.6, "end": 2.0, "overlap": True, "text": "Да."}]


def test_the_prompts_ask_for_short_replies_ends_and_cross_talk():
    prompt = transcribe._prompt({"language": "ru"}, 60.0, "")
    assert "отдельный segment" in prompt and '"overlap": true' in prompt and '"end": 2.4' in prompt
    assert "Даже одно слово" not in prompt  # that rule belongs to very short recordings only


# ---------------------------------------------------------------------------------- whole-record labelling
def test_a_replica_is_cut_where_the_second_voice_begins():
    assert diarize.cut_at("Подписан вчера, да, согласен с этим", "да, согласен") == 16
    assert diarize.cut_at("Да, согласен", "да") is None  # never at the first word
    assert diarize.cut_at("Подписан вчера", "нет такого") is None
    segments = [seg("Спикер 1", 0.0, 6.0, "Акт подписан вчера вечером да согласен"), seg("Спикер 2", 6.0, 9.0, "Хорошо.")]
    cut = diarize.cut_at(segments[0]["text"], "да согласен")
    out = diarize.apply(segments, ["Спикер 1", "Спикер 2"], {"cuts": {0: [(cut, "Спикер 2")]}, "overlap": {1}})
    assert [(s["speaker"], s["text"]) for s in out] == [
        ("Спикер 1", "Акт подписан вчера вечером"), ("Спикер 2", "да согласен"), ("Спикер 2", "Хорошо.")]
    assert out[0]["start"] == 0.0 and out[0]["end"] == out[1]["start"] and out[1]["end"] == 6.0
    assert 3.5 < out[1]["start"] < 5.0  # 4 of 13 syllables are «да согласен»
    assert out[2]["overlap"] is True and "overlap" not in out[0]


def test_the_audio_model_cuts_replicas_and_marks_cross_talk(monkeypatch, tmp_path):
    record = tmp_path / "meeting.mp3"
    record.write_bytes(b"audio")
    def cut(source, folder, start, seconds, name):
        target = pathlib.Path(folder) / name
        target.write_bytes(b"part")
        return target
    monkeypatch.setattr(diarize, "_cut", cut)
    answer = {"speakers": [
        {"i": 0, "speaker": "Спикер 1", "split": [{"from": "да, верно", "speaker": "Спикер 2"}]},
        {"i": 1, "speaker": "Спикер 2", "overlap": True},
        {"i": 2, "speaker": "Спикер 1", "split": [{"from": "нет такого текста", "speaker": "Спикер 3"}]}]}
    monkeypatch.setattr(llm, "chat", lambda *a, **k: json.dumps(answer, ensure_ascii=False))
    segments = [seg("Спикер 1", 0, 5, "Сроки сдвигаются на неделю, да, верно"), seg("Спикер 1", 5, 8, "Тогда переносим."),
                seg("Спикер 2", 8, 12, "Договорились.")]
    details = {}
    labels = diarize.gemini_labels(record, segments, 12, {}, details=details)
    assert labels == ["Спикер 1", "Спикер 2", "Спикер 1"]
    assert details["cuts"] == {0: [(len("Сроки сдвигаются на неделю, "), "Спикер 2")]} and details["overlap"] == {1}
    out = diarize.apply(segments, labels, details)
    assert [(s["speaker"], s["text"], s.get("overlap", False)) for s in out] == [
        ("Спикер 1", "Сроки сдвигаются на неделю,", False), ("Спикер 2", "да, верно", False),
        ("Спикер 2", "Тогда переносим.", True), ("Спикер 1", "Договорились.", False)]


def test_a_short_reply_goes_to_the_voice_it_really_has():
    segments = [seg("Спикер 1", 0, 20), seg("Спикер 2", 20, 40), seg("Спикер 1", 40, 41, "да"),
                seg("Спикер 1", 41, 42, "угу"), seg("Спикер 1", 42, 59)]
    groups = [group(0, "Спикер 1", ANNA), group(0, "Спикер 2", BORIS),
              group(0, "#2", vec(0.1, 1, 0), seconds=0.8, short=2),      # «да» sounds like Борис, glued to Анна
              group(0, "#3", vec(1, 0.8, 0), seconds=0.7, short=3)]      # «угу» is unclear: it stays
    assert diarize.voice_labels(segments, groups, 0.5, total=60) == ["Спикер 1", "Спикер 2", "Спикер 2", "Спикер 1", "Спикер 1"]
    assert diarize.cluster([g for g in groups if "short" not in g], 0.5) == [0, 1]  # short prints never cluster


def test_the_voice_service_is_asked_for_short_clips(monkeypatch, tmp_path):
    clip = tmp_path / "minute.mp3"
    clip.write_bytes(b"mp3")
    sent = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"speakers": {}}

    def post(url, headers, data, files, timeout):
        sent.update(data)
        return Response()
    monkeypatch.setattr(speaker_id, "_voice_service_request_url", lambda path: "https://voice.example" + path)
    monkeypatch.setattr(speaker_id, "_voice_service_headers", lambda: {})
    monkeypatch.setattr(speaker_id.httpx, "post", post)
    speaker_id.embed_speakers(clip, [{"speaker": "#3", "start": 0, "end": 0.6}], min_seconds=0.5)
    assert sent["min_seconds"] == "0.50"
    speaker_id.embed_speakers(clip, [{"speaker": "Спикер 1", "start": 0, "end": 3}])
    assert "min_seconds" not in sent or sent.get("min_seconds") == "0.50"


def test_the_transcript_marks_cross_talk(client):
    with SessionLocal() as s:
        m = Meeting(title="Планёрка", filename="a.mp3", source="upload", status="awaiting_approval", duration_sec=10,
                    transcript=[seg("Спикер 1", 0, 5, "Сроки сдвигаются."), seg("Спикер 2", 4, 6, "Нет, не так.", overlap=True)])
        s.add(m)
        s.commit()
        mid = m.id
    page = client.get(f"/meetings/{mid}").text
    assert page.count(">перебивание</span>") == 1
