"""Voice detector, cutting in pauses, the checks against the audio (verify.py, meaning.py) and the engine comparison."""
import json
import pathlib
import subprocess

import pytest

from app import audio, checkpoints, config, llm, meaning, settings_store, speech_fix, stt_compare, transcribe, vad, verify
from app.db import Employee, Entity, EntityLink, Meeting, SessionLocal

FRAME = vad.FRAME


# --------------------------------------------------------------------------------------------- voice detector
def test_speech_stretches_follow_the_hysteresis_of_the_detector():
    probs = [0.0] * 10 + [0.9] * 20 + [0.4] * 3 + [0.9] * 10 + [0.1] * 20 + [0.9] * 3 + [0.0] * 30
    found = vad.regions(probs)
    # a dip to 0.4 (above the lower threshold) does not end the speech; a 3-frame burst (96 ms) is not speech
    assert len(found) == 1
    start, end, score = found[0]
    assert start == pytest.approx(10 * FRAME - vad.PAD, abs=0.01)
    assert end == pytest.approx(43 * FRAME + vad.PAD, abs=0.01)
    assert 0.8 < score <= 0.9


def test_pauses_and_seconds_of_speech():
    speech = [[1.0, 3.0, 0.9], [3.2, 5.0, 0.8], [8.0, 9.0, 0.9]]
    assert vad.pauses(speech, 10.0) == [[0.0, 1.0], [5.0, 8.0], [9.0, 10.0]]  # 0.2 s between is no pause
    assert vad.speech_in(speech, 2.0, 8.5) == pytest.approx(1.0 + 1.8 + 0.5)
    assert vad.total_speech(speech) == pytest.approx(4.8)


def test_the_detector_runs_on_audio_without_a_voice(tmp_path):
    pytest.importorskip("onnxruntime")
    source = tmp_path / "tone.wav"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000",
                    "-t", "40", str(source)], check=True, capture_output=True)
    assert vad.available()
    assert vad.detect(str(source)) == []


def test_speech_is_found_once_and_kept_with_the_meeting(monkeypatch):
    calls = []
    monkeypatch.setattr(vad, "available", lambda: True)
    monkeypatch.setattr(vad, "detect", lambda path, progress=None: calls.append(path) or [[1.0, 2.0, 0.9]])
    with SessionLocal() as s:
        meeting = Meeting(title="M", filename="m.mp3", status="queued")
        s.add(meeting)
        s.commit()
        mid = meeting.id
    store = checkpoints.Store(mid)
    assert vad.speech("a.mp3", store, "digest") == [[1.0, 2.0, 0.9]]
    assert vad.speech("a.mp3", store, "digest") == [[1.0, 2.0, 0.9]]
    assert calls == ["a.mp3"]


def test_a_failing_detector_leaves_recognition_as_before(monkeypatch):
    monkeypatch.setattr(vad, "available", lambda: True)
    monkeypatch.setattr(vad, "detect", lambda path, progress=None: (_ for _ in ()).throw(vad.VADError("broken")))
    assert vad.speech("a.mp3") is None


# ---------------------------------------------------------------------------------------------- cutting in pauses
def test_parts_are_cut_in_pauses_without_overlap(monkeypatch):
    monkeypatch.setattr(audio, "duration", lambda path: 200.0)
    speech = [[0, 50, 0.9], [51.5, 118.9, 0.9], [119.3, 175, 0.9], [176, 200, 0.9]]
    total, plan = audio.plan_chunks("x.mp3", None, overlap_seconds=4, max_chunk_minutes=1, speech=speech)
    assert total == 200.0
    assert [round(start, 2) for start, _ in plan] == [0, 51, 119.1, 175.5]
    for (start, seconds), (following, _) in zip(plan, plan[1:]):
        assert start + seconds == pytest.approx(following)  # the parts meet in a pause: nothing to stitch
    assert plan[-1][0] + plan[-1][1] == pytest.approx(200)


def test_without_a_pause_the_cut_stays_with_its_overlap(monkeypatch):
    monkeypatch.setattr(audio, "duration", lambda path: 200.0)
    _, plan = audio.plan_chunks("x.mp3", None, overlap_seconds=4, max_chunk_minutes=1, speech=[[0, 200, 0.9]])
    assert plan[0] == (0.0, 64)
    _, old = audio.plan_chunks("x.mp3", None, overlap_seconds=4, max_chunk_minutes=1)
    assert old == [(0.0, 64), (60.0, 64), (120.0, 64), (180.0, 20.0)]


# ---------------------------------------------------------------------------------------------- STT request
class Response:
    def __init__(self, status, data=None, text=""):
        self.status_code, self._data, self.text = status, data, text or json.dumps(data or {})

    def json(self):
        return self._data


@pytest.fixture
def stt_calls(monkeypatch):
    monkeypatch.setattr(llm.config, "openrouter_key", lambda: "key")
    monkeypatch.setattr(llm, "_STT_FORMAT", {})
    monkeypatch.setattr(llm, "_STT_UNSUPPORTED", {})
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    calls, answers = [], []

    def post(url, json=None, headers=None, timeout=None):
        calls.append(json)
        return answers.pop(0)
    monkeypatch.setattr(llm.httpx, "post", post)
    return calls, answers


def test_names_and_speakers_are_asked_for_and_dropped_when_the_engine_cannot(stt_calls):
    calls, answers = stt_calls
    answers += [Response(400, text='{"error": {"message": "keyterms are not supported by this model"}}'),
                Response(200, {"text": "Да."}), Response(200, {"text": "Нет."})]
    assert llm.transcribe_audio(b"a", "x/stt", keyterms=["ВентСнаб"], diarize=True)["text"] == "Да."
    assert calls[0]["keyterms"] == ["ВентСнаб"] and calls[0]["diarize"] is True
    assert calls[0]["response_format"] == "verbose_json"
    assert "keyterms" not in calls[1] and calls[1]["diarize"] is True  # the same format, without the names
    assert calls[1]["response_format"] == "verbose_json"
    llm.transcribe_audio(b"a", "x/stt", keyterms=["ВентСнаб"], diarize=True)
    assert "keyterms" not in calls[2]  # remembered for the model


def test_a_plain_json_engine_gets_names_but_never_speakers(stt_calls):
    calls, answers = stt_calls
    answers.append(Response(200, {"text": "Да."}))
    llm.transcribe_audio(b"a", "openai/gpt-4o-transcribe", keyterms=["Вадим"], diarize=True)
    assert calls[0]["response_format"] == "json" and calls[0]["keyterms"] == ["Вадим"] and "diarize" not in calls[0]


def test_an_unexplained_rejection_drops_the_extras_before_the_format(stt_calls):
    calls, answers = stt_calls
    answers += [Response(400, text="bad request"), Response(200, {"text": "Ок."})]
    llm.transcribe_audio(b"a", "y/stt", keyterms=["Вадим"])
    assert "keyterms" in calls[0] and "keyterms" not in calls[1]
    assert calls[1]["response_format"] == "verbose_json"


# ---------------------------------------------------------------------------------------------- word traces
def test_unsure_words_and_heard_stretches_travel_with_the_replicas(tmp_path):
    result = {"text": "Подпиши акт сверхи сегодня.", "words": [
        {"word": "Подпиши", "start": 0.0, "end": 0.5, "confidence": 0.98, "speaker": 0},
        {"word": "акт", "start": 0.6, "end": 0.8, "confidence": 0.95, "speaker": 0},
        {"word": "сверхи", "start": 0.9, "end": 1.4, "confidence": 0.21, "speaker": 0},
        {"word": "сегодня.", "start": 2.5, "end": 3.0, "confidence": 0.9, "speaker": 0}]}
    out = transcribe._by_words(tmp_path / "x.mp3", result, {}, 10, "")
    segment = out["segments"][0]
    assert segment["text"] == "Подпиши акт сверхи сегодня."
    assert segment["doubts"] == [[0.9, 1.4, "сверхи", 0.21]]
    assert segment["heard"] == [[0.0, 1.4], [2.5, 3.0]]
    shifted, _ = transcribe._validate(out, 100.0, 10)
    assert shifted[0]["doubts"] == [[100.9, 101.4, "сверхи", 0.21]]
    assert shifted[0]["heard"] == [[100.0, 101.4], [102.5, 103.0]]


# ---------------------------------------------------------------------------------------------- missed speech
def test_voice_with_no_words_over_it_is_a_gap():
    segments = [{"speaker": "Спикер 1", "start": 0.0, "end": 9.0, "text": "раз два", "heard": [[0.0, 2.0], [7.5, 9.0]]},
                {"speaker": "Спикер 2", "start": 20.0, "text": "три четыре пять"}]
    spans = verify.heard_spans(segments)
    assert spans[:2] == [[0.0, 2.0], [7.5, 9.0]]
    assert spans[2][0] == 20.0 and spans[2][1] == pytest.approx(20.0 + 0.45 * 3 + 0.3)
    found = verify.gaps([[0.0, 9.0, 0.9], [12.0, 12.6, 0.9], [20.0, 21.0, 0.9]], spans)
    assert found == [[2.0, 7.5, 0.9]]  # 0.6 s of voice is too short to count


@pytest.fixture
def fake_audio(monkeypatch):
    monkeypatch.setattr(audio, "extract", lambda src, dst, start, sec, cleanup=None: pathlib.Path(dst).write_bytes(b"x"))
    monkeypatch.setattr(config, "llm_enabled", lambda: True)


def test_missed_speech_is_heard_again_by_the_second_engine(fake_audio, monkeypatch):
    heard = []

    def stt(data, model, fmt="mp3", language="ru", timeout=60, prompt="", **extras):
        heard.append(model)
        # the clip starts 0.3 s before the gap at 10.0: words in seconds of the clip
        return {"text": "а договор подписан", "words": [{"word": "а", "start": 0.0, "end": 0.2},
                                                         {"word": "договор", "start": 0.4, "end": 1.0},
                                                         {"word": "подписан", "start": 1.1, "end": 1.8}]}
    monkeypatch.setattr(llm, "transcribe_audio", stt)
    segments = [{"speaker": "Спикер 1", "start": 0.0, "end": 10.0, "text": "начнём", "heard": [[0.0, 10.0]]},
                {"speaker": "Спикер 2", "start": 13.0, "end": 15.0, "text": "хорошо", "heard": [[13.0, 15.0]]}]
    info = {}
    out = verify.recover("rec.mp3", segments, [[0.0, 15.0, 0.95]], {"verify_model": "google/gemini-3.5-transcribe",
                                                                      "transcribe_model": "elevenlabs/scribe-v2"},
                         15.0, info=info)
    assert heard == ["google/gemini-3.5-transcribe"]
    recovered = [s for s in out if s.get("recovered")]
    # «а» lies in the 0.3 s before the gap (the previous replica's time): only the words inside the gap are taken
    assert [s["text"] for s in recovered] == ["договор подписан"]
    assert recovered[0]["speaker"] == "Спикер 1" and recovered[0]["start"] == pytest.approx(10.1, abs=0.05)
    assert info["gaps"] == 1 and info["recovered"] == 1 and info["recovered_words"] == 2


def test_nothing_heard_in_clear_voice_is_marked_unclear_and_repeats_are_not_added(fake_audio, monkeypatch):
    answers = [{"text": "", "words": []}, {"text": "хорошо договорились мы", "words": [
        {"word": "хорошо", "start": 0.3, "end": 0.8}, {"word": "договорились", "start": 0.9, "end": 1.6},
        {"word": "мы", "start": 1.7, "end": 1.9}]}]
    monkeypatch.setattr(llm, "transcribe_audio", lambda *a, **k: answers.pop(0))
    segments = [{"speaker": "Спикер 1", "start": 0.0, "end": 2.0, "text": "раз", "heard": [[0.0, 2.0]]},
                {"speaker": "Спикер 2", "start": 12.0, "end": 14.0, "text": "хорошо договорились мы",
                 "heard": [[12.0, 14.0]]}]
    info = {}
    out = verify.recover("rec.mp3", segments, [[2.0, 5.0, 0.9], [20.0, 22.0, 0.9]],
                         {"transcribe_model": "elevenlabs/scribe-v2"}, 30.0, info=info)
    unclear = [s for s in out if s.get("unclear")]
    assert [s["text"] for s in unclear] == [verify.UNCLEAR] and unclear[0]["start"] == 2.0
    assert not [s for s in out if s.get("recovered")]  # «хорошо договорились мы» is already said at 12 s
    assert info["unclear"] == 1 and info["recovered"] == 0


def test_phrases_invented_over_silence_are_dropped_only_there():
    segments = [{"speaker": "Спикер 1", "start": 0.0, "end": 3.0, "text": "Продолжение следует..."},
                {"speaker": "Спикер 1", "start": 10.0, "end": 12.0, "text": "Субтитры сделал DimaTorzok"},
                {"speaker": "Спикер 2", "start": 20.0, "end": 22.0, "text": "тихий ответ"}]
    info = {}
    kept = verify.drop_phantoms(segments, [[10.0, 12.0, 0.9]], info)
    assert [s["text"] for s in kept] == ["Субтитры сделал DimaTorzok", "тихий ответ"]
    assert info["phantoms"] == 1


# ---------------------------------------------------------------------------------------------- doubtful words
def doubtful():
    return [{"speaker": "Спикер 1", "start": 10.0, "end": 13.0, "text": "Подпиши акт сверхи сегодня.",
             "doubts": [[11.0, 11.5, "сверхи", 0.2]]}]


def judge_with(monkeypatch, answer, calls):
    def chat(model, messages, **kwargs):
        calls.append((model, messages[0]["content"][0]["text"]))
        return json.dumps(answer, ensure_ascii=False)
    monkeypatch.setattr(llm, "chat", chat)


SETTINGS = {"transcribe_model": "elevenlabs/scribe-v2", "verify_model": "google/gemini-3.5-transcribe",
            "verify_judge_model": "google/gemini-2.5-pro", "glossary": ""}


def test_a_doubtful_word_changes_when_the_judge_agrees_with_the_second_engine(fake_audio, monkeypatch):
    monkeypatch.setattr(llm, "transcribe_audio", lambda *a, **k: {"text": "подпиши акт сверки сегодня"})
    calls = []
    judge_with(monkeypatch, {"word": "сверки", "sure": True}, calls)
    info = {}
    out = verify.resolve("rec.mp3", doubtful(), SETTINGS, 60.0, info=info)
    assert out[0]["text"] == "Подпиши акт сверки сегодня."
    assert out[0]["checked"] == [{"from": "сверхи", "to": "сверки"}]
    assert calls[0][0] == "google/gemini-2.5-pro" and "⟦сверхи⟧" in calls[0][1]
    assert info["doubts"] == 1 and info["checked"] == 1 and info["fixed"] == 1


@pytest.mark.parametrize("answer", [{"word": "сверки", "sure": False}, {"word": "сверху", "sure": True}])
def test_a_doubtful_word_stays_unless_two_hearings_agree(fake_audio, monkeypatch, answer):
    monkeypatch.setattr(llm, "transcribe_audio", lambda *a, **k: {"text": "подпиши акт сверки сегодня"})
    judge_with(monkeypatch, answer, [])
    out = verify.resolve("rec.mp3", doubtful(), SETTINGS, 60.0)
    assert out[0]["text"] == "Подпиши акт сверхи сегодня." and not out[0].get("checked")


def test_the_same_word_from_the_second_engine_needs_no_judge(fake_audio, monkeypatch):
    monkeypatch.setattr(llm, "transcribe_audio", lambda *a, **k: {"text": "подпиши акт сверхи сегодня"})
    calls = []
    judge_with(monkeypatch, {"word": "сверки", "sure": True}, calls)
    out = verify.resolve("rec.mp3", doubtful(), SETTINGS, 60.0)
    assert out[0]["text"] == "Подпиши акт сверхи сегодня." and calls == []


def test_a_name_of_the_vocabulary_is_accepted_from_the_judge(fake_audio, monkeypatch):
    settings_store.set_many({"glossary": "ВентСнаб"})
    monkeypatch.setattr(llm, "transcribe_audio", lambda *a, **k: {"text": "договор для венснаба готов"})
    judge_with(monkeypatch, {"word": "ВентСнаба", "sure": True}, [])
    segments = [{"speaker": "Спикер 1", "start": 0.0, "end": 3.0, "text": "Договор для винсунаба готов.",
                 "doubts": [[1.0, 1.6, "винсунаба", 0.3]]}]
    out = verify.resolve("rec.mp3", segments, dict(SETTINGS, glossary="ВентСнаб"), 60.0)
    assert out[0]["text"] == "Договор для ВентСнаба готов."


def test_the_meaning_check_proposes_and_the_ear_decides(fake_audio, monkeypatch):
    segments = [{"speaker": "Спикер 1", "start": 0.0, "end": 4.0, "text": "Отправь акт сверхи клиенту."},
                {"speaker": "Спикер 2", "start": 4.0, "end": 6.0, "text": "Хорошо."}]

    def chat(model, messages, **kwargs):
        content = messages[0]["content"]
        if isinstance(content, str):  # the meaning check: a text model
            return json.dumps({"suspects": [{"line": 0, "word": "сверхи", "fix": "сверки", "why": "акт сверки"},
                                            {"line": 1, "word": "нет такого", "fix": "x"},
                                            {"line": 7, "word": "Хорошо", "fix": "Плохо"}]}, ensure_ascii=False)
        return json.dumps({"word": "сверки", "sure": True}, ensure_ascii=False)
    monkeypatch.setattr(llm, "chat", chat)
    monkeypatch.setattr(llm, "transcribe_audio", lambda *a, **k: {"text": "отправь акт сверки клиенту"})
    info = {}
    proposals = meaning.review(segments, SETTINGS, info=info)
    assert [(p["index"], p["word"], p["fix"]) for p in proposals] == [(0, "сверхи", "сверки")]
    out = verify.resolve("rec.mp3", segments, SETTINGS, 60.0, proposals, info=info)
    assert out[0]["text"] == "Отправь акт сверки клиенту." and info["meaning"] == 1 and info["fixed"] == 1


# ---------------------------------------------------------------------------------------------- the whole run
def test_recognition_cuts_in_pauses_recovers_missed_speech_and_reports(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "llm_enabled", lambda: True)
    monkeypatch.setattr(audio, "duration", lambda p: 30.0)
    monkeypatch.setattr(audio, "extract", lambda src, dst, start, sec, cleanup=None: pathlib.Path(dst).write_bytes(
        json.dumps([start, sec]).encode()))
    monkeypatch.setattr(vad, "speech", lambda *a, **k: [[1.0, 6.0, 0.9], [8.0, 12.0, 0.95]])
    monkeypatch.setattr(llm, "chat", lambda *a, **k: json.dumps({"suspects": [], "replacements": []}))
    seen = []

    def stt(data, model, fmt="mp3", language="ru", timeout=60, prompt="", **extras):
        start, seconds = json.loads(data)
        seen.append((model, round(start, 1), sorted(extras)))
        if model == "elevenlabs/scribe-v2":  # the main engine drops the second phrase
            return {"text": "Начинаем планёрку.", "words": [
                {"word": "Начинаем", "start": 1.0, "end": 1.6, "speaker": 0, "confidence": 0.99},
                {"word": "планёрку.", "start": 1.7, "end": 2.4, "speaker": 0, "confidence": 0.97},
            ]}
        return {"text": "Отчёт готов", "words": [{"word": "Отчёт", "start": 0.4, "end": 0.9},
                                                 {"word": "готов", "start": 1.0, "end": 1.5}]}
    monkeypatch.setattr(llm, "transcribe_audio", stt)
    source = tmp_path / "m.mp3"
    source.write_bytes(b"audio")
    settings = dict(settings_store.all_settings(), transcribe_model="elevenlabs/scribe-v2")
    info = {}
    _, segments = transcribe.transcribe(str(source), settings, info=info)
    assert seen[0] == ("elevenlabs/scribe-v2", 0.0, ["diarize", "keyterms"])
    texts = [(s["text"], s.get("recovered", False)) for s in segments]
    assert ("Начинаем планёрку.", False) in texts and any(t[1] for t in texts)
    assert all("heard" not in s and "doubts" not in s for s in segments)
    assert info["engine"] == "elevenlabs/scribe-v2" and info["detector"] and info["recovered"] >= 1
    assert info["gaps"] >= 1 and info["keyterms"] >= 1


# ---------------------------------------------------------------------------------------------- names and terms
def test_keyterms_put_the_participants_first_and_keep_the_limits():
    with SessionLocal() as s:
        boss = Employee(name="Вадим Чудин", aliases="Вадик", telegram_chat_id="@boss")
        other = Employee(name="Ольга Кузнецова", telegram_chat_id="@olga")
        s.add_all([other, boss])
        client = Entity(name="Криница", aliases=["ОАО Криница"])
        rare = Entity(name="Галерея Борисов")
        s.add_all([rare, client])
        s.flush()
        s.add_all([EntityLink(entity_id=client.id, source_type="meeting", source_id=1),
                   EntityLink(entity_id=client.id, source_type="meeting", source_id=2)])
        s.commit()
        boss_id = boss.id
    terms = speech_fix.keyterms({"_participants": [boss_id], "glossary": "SYSAI; имена сотрудников: Вадим Чудин, "
                                 + "очень длинное название которое не поместится в один термин движка"})
    assert terms[:3] == ["Вадим Чудин", "Вадик", "SYSAI"]
    assert terms.index("Криница") < terms.index("Галерея Борисов") < terms.index("Ольга Кузнецова")
    assert all(len(term) <= speech_fix.KEYTERM_CHARS for term in terms)
    assert len(speech_fix.keyterms({}, limit=2)) == 2


# ---------------------------------------------------------------------------------------------- pages
def test_settings_show_and_save_the_checks(client):
    page = client.get("/settings").text
    assert 'name="verify_model"' in page and 'name="verify_judge_model"' in page
    assert "present_speech_detector" in page and "/speech-compare" in page
    response = client.post("/settings", data={"verify_model": "microsoft/mai-transcribe-2",
                                              "present_speech_doubts": "1", "present_stt_keyterms": "1",
                                              "stt_keyterms": "on"}, follow_redirects=False)
    assert response.status_code in (302, 303)
    saved = settings_store.all_settings()
    assert saved["verify_model"] == "microsoft/mai-transcribe-2"
    assert saved["speech_doubts"] is False and saved["stt_keyterms"] is True
    assert client.post("/settings", data={"verify_judge_model": "bad model!"}).status_code == 400


def test_the_meeting_page_tells_what_the_checks_found(client):
    with SessionLocal() as s:
        meeting = Meeting(title="Планёрка", filename="m.mp3", status="awaiting_approval", duration_sec=60,
                          transcript=[{"speaker": "Спикер 1", "start": 0.0, "end": 3.0, "text": "Начнём."},
                                      {"speaker": "Спикер 1", "start": 3.0, "end": 5.0, "text": "Отчёт готов",
                                       "recovered": True},
                                      {"speaker": "Спикер 2", "start": 5.0, "end": 8.0, "text": "Акт сверки",
                                       "checked": [{"from": "сверхи", "to": "сверки"}]}],
                          options={"recognition": {"engine": "elevenlabs/scribe-v2", "detector": True, "gaps": 3,
                                                   "recovered": 1, "unclear": 1, "doubts": 4, "checked": 4,
                                                   "fixed": 1, "phantoms": 2}})
        s.add(meeting)
        s.commit()
        mid = meeting.id
    page = client.get(f"/meetings/{mid}").text
    assert "Распознавание: elevenlabs/scribe-v2" in page
    assert "детектор речи нашёл 3 места с голосом без слов, дослышано 1 фраза" in page
    assert "исправлено 1" in page and "убрано выдуманных фраз: 2" in page
    assert "дослышано</span>" in page and "«сверхи» → «сверки»" in page


def test_engines_are_compared_on_a_recording_and_one_is_made_the_main(client, monkeypatch, tmp_path):
    source = tmp_path / "rec.wav"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=300:sample_rate=16000",
                    "-t", "240", str(source)], check=True, capture_output=True)
    with SessionLocal() as s:
        meeting = Meeting(title="Планёрка", filename="rec.wav", status="awaiting_approval", duration_sec=240,
                          audio_path=str(source), source="upload")
        s.add(meeting)
        s.commit()
        mid = meeting.id
    monkeypatch.setattr(vad, "speech", lambda *a, **k: [[0.0, 240.0, 0.9]])
    texts = {"a/good": ("подпиши акт сверки сегодня до вечера", True), "b/fine": ("подпиши акт сверки сегодня", True),
             "c/lossy": ("подпиши", True)}

    def stt(data, model, fmt="mp3", language="ru", timeout=60, prompt="", **extras):
        if model == "d/broken":
            raise llm.LLMError("OpenRouter STT HTTP 404: no such model")
        text, timed = texts[model]
        words = [{"word": w, "start": 0.5 * k, "end": 0.5 * k + 0.4} for k, w in enumerate(text.split())]
        return {"text": text, "words": words if timed else [], "usage": {"cost": 0.001}}
    monkeypatch.setattr(llm, "transcribe_audio", stt)
    response = client.post("/speech-compare/run", data={"meeting_id": str(mid),
                                                         "engines": "a/good\nb/fine, c/lossy\nd/broken"},
                           follow_redirects=False)
    assert response.headers["location"] == "/speech-compare?msg=started"
    result = stt_compare.last()
    assert len(result["windows"]) == stt_compare.WINDOWS and not result["reference"]
    rows = {row["model"]: row for row in result["engines"]}
    assert rows["d/broken"]["errors"] and rows["c/lossy"]["missed"] > rows["a/good"]["missed"]
    assert result["best"] in ("a/good", "b/fine")
    page = client.get("/speech-compare").text
    assert "Сравнение движков распознавания" in page and "c/lossy" in page and "Сделать основным" in page
    client.post("/speech-compare/apply", data={"model": "b/fine", "role": "main"})
    client.post("/speech-compare/apply", data={"model": "a/good", "role": "verify"})
    saved = settings_store.all_settings()
    assert saved["transcribe_model"] == "b/fine" and saved["verify_model"] == "a/good"


def test_with_a_reference_the_engines_are_measured_against_it(monkeypatch, tmp_path):
    source = tmp_path / "rec.wav"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=300:sample_rate=16000",
                    "-t", "120", str(source)], check=True, capture_output=True)
    truth = [{"speaker": "Анна", "start": float(k * 10), "end": float(k * 10 + 9), "text": "подпиши акт сверки"}
             for k in range(12)]
    with SessionLocal() as s:
        meeting = Meeting(title="Эталон", filename="rec.wav", status="awaiting_approval", duration_sec=120,
                          audio_path=str(source), source="upload",
                          diarization_reference={"transcript": truth, "auto": truth, "at": "x"})
        s.add(meeting)
        s.commit()
        mid = meeting.id
    monkeypatch.setattr(vad, "speech", lambda *a, **k: [])

    def stt(data, model, fmt="mp3", language="ru", timeout=60, prompt="", **extras):
        return {"text": "подпиши акт сверки" if model == "a/exact" else "подпиши акт сверху"}
    monkeypatch.setattr(llm, "transcribe_audio", stt)
    monkeypatch.setattr(stt_compare, "reference_text", lambda reference, start, end: "подпиши акт сверки")
    result = stt_compare.run(mid, ["b/near", "a/exact"])
    assert result["reference"] and result["best"] == "a/exact"
    rows = {row["model"]: row for row in result["engines"]}
    assert rows["a/exact"]["wer"] == 0 and rows["b/near"]["wer"] == pytest.approx(1 / 3, abs=1e-3)
