"""«Отметка голоса» on the voice diagram: a part of the recording marked, listened to and confirmed as one person's
voice goes into that person's voice profile (an encrypted sample), the replicas in it are signed with the person, and
the profile names the voice at the next meetings — in the transcription and in the voice service's way."""
import array
import datetime as dt
import itertools
import json
import math
import re
import wave
from urllib.parse import unquote

import pytest

from app import audio, config, diarize, llm, settings_store, speaker_id, transcribe, transcript_edit, voice_diagram
from app import voice_relabel
from app.db import Employee, EmployeeVoiceSample, Meeting, SessionLocal, now

TEXT = "обсуждаем план работ на эту неделю подробно"


def vec(*weights):
    raw = list(weights) + [0.0] * (192 - len(weights))
    norm = math.sqrt(sum(x * x for x in raw))
    return [x / norm for x in raw]


ANNA, BORIS, STRANGER = vec(1, 0, 0.1), vec(0, 1, 0.1), vec(0, 0, 0, 1)


TONES = itertools.count(230, 17)  # every meeting is a recording of its own: its cut samples differ from the others


def make_wav(path, seconds=60, tone=None):
    tone = tone or next(TONES)
    values = array.array('h', (int(6000 * math.sin(2 * math.pi * tone * i / 16000)) for i in range(seconds * 16000)))
    with wave.open(str(path), 'wb') as out:
        out.setnchannels(1); out.setsampwidth(2); out.setframerate(16000); out.writeframes(values.tobytes())
    return path


def seg(speaker, start, end, text=TEXT, **extra):
    return dict(speaker=speaker, start=start, end=end, text=text, **extra)


ROWS = [seg("Спикер 1", 0, 8), seg("Спикер 2", 8, 20), seg("Спикер 1", 20, 28), seg("Спикер 2", 28, 40)]


@pytest.fixture
def voices(monkeypatch):
    """The voice service is connected; a sample cut from a meeting sounds like the person it is saved for."""
    monkeypatch.setattr(config, "voice_service_enabled", lambda: True)
    prints = {}

    def embed(path, employee_id):
        return dict(embedding=prints.get(employee_id, ANNA), model_version="real-model-v1", waveform=[],
                    sample_blob_path=None, duration_seconds=audio_seconds(path))
    monkeypatch.setattr(speaker_id, "embed_voice_sample", embed)
    return prints


def audio_seconds(path):
    with wave.open(str(path)) as w:
        return w.getnframes() / 16000


def people():
    with SessionLocal() as s:
        anna, boris = Employee(name="Анна Петрова"), Employee(name="Борис Смирнов")
        s.add_all([anna, boris])
        s.commit()
        return anna.id, boris.id


def meeting(tmp_path, rows=None, stored=True, title="Планёрка"):
    path = make_wav(tmp_path / f"{title}.wav")
    with SessionLocal() as s:
        m = Meeting(title=title, source="upload", status="done", audio_path=str(path), duration_sec=60,
                    transcript=list(rows or ROWS))
        if stored:  # the copy in the storage of recordings: the diagram can play it
            m.recording_status = "stored"
            m.recording = {"key": "recordings/x.mp3", "size": 1000,
                           "expires_at": (now() + dt.timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")}
        s.add(m)
        s.commit()
        return m.id


def version(mid):
    with SessionLocal() as s:
        return transcript_edit.fingerprint(s.get(Meeting, mid).transcript)


def mark(client, mid, employee_id, start=8, end=20, **extra):
    data = dict(start=start, end=end, employee_id=employee_id, listened="yes", consent="yes", apply="mark",
                version=version(mid))
    data.update(extra)
    return client.post(f"/meetings/{mid}/voice-marks", data=data, follow_redirects=False)


def message(response):
    location = response.headers["location"]
    assert location.endswith("#voice-diagram")
    return unquote(re.search(r"voice_mark=([^#&]*)", location).group(1))


def samples():
    with SessionLocal() as s:
        rows = s.query(EmployeeVoiceSample).order_by(EmployeeVoiceSample.id).all()
        s.expunge_all()
        return rows


def transcript(mid):
    with SessionLocal() as s:
        return s.get(Meeting, mid).transcript


def test_a_mark_goes_into_the_voice_profile_and_signs_its_replicas(client, voices, tmp_path):
    anna, _ = people()
    mid = meeting(tmp_path)
    response = mark(client, mid, anna)
    assert response.status_code == 303
    text = message(response)
    assert "Образец 12 с сохранён в профиль голоса: Анна Петрова" in text
    assert "будет узнаваться на следующих совещаниях" in text and "надёжнее с 3" in text and "Подписано реплик: 1" in text
    [row] = samples()
    assert (row.employee_id, row.source_meeting_id, row.source_kind) == (anna, mid, "meeting_marked")
    assert (row.source_start, row.source_end, row.sample_duration) == (8, 20, 12)
    assert row.embedding_status == "ready" and speaker_id.sample_has_usable_embedding(row)
    with wave.open(__import__("io").BytesIO(speaker_id.decrypt_sample_audio(row))) as w:
        assert w.getnframes() == 12 * 16000
    after = transcript(mid)
    assert after[1]["speaker"] == "Анна Петрова" and after[1]["speaker_source"] == "manual"
    assert after[1]["speaker_employee_id"] == anna and after[1]["speaker_auto"] == {"speaker": "Спикер 2"}
    assert [r["speaker"] for r in after[2:]] == ["Спикер 1", "Спикер 2"]  # only the replica in the mark
    page = client.get(f"/meetings/{mid}").text
    assert "00:08–00:20: отметка голоса → Анна Петрова (1)" in page  # in the history of the transcript edits
    assert client.post(f"/meetings/{mid}/transcript/undo", follow_redirects=False).status_code == 303
    assert transcript(mid) == ROWS and len(samples()) == 1  # the replica is undone, the sample stays in the profile


def test_a_mark_signs_every_replica_of_its_speaker_or_nothing(client, voices, tmp_path):
    anna, boris = people()
    first = meeting(tmp_path, title="Первая")
    assert "Подписано реплик: 2" in message(mark(client, first, anna, apply="label"))
    assert [r["speaker"] for r in transcript(first)] == ["Спикер 1", "Анна Петрова", "Спикер 1", "Анна Петрова"]
    second = meeting(tmp_path, title="Вторая")
    text = message(mark(client, second, boris, start=0, end=8, apply="none"))
    assert "сохранён в профиль голоса: Борис Смирнов" in text and "Подписано" not in text
    assert transcript(second) == ROWS
    # the transcript changed after the page was opened: the sample is saved, the replicas are not signed
    third = meeting(tmp_path, title="Третья")
    text = message(mark(client, third, boris, start=28, end=40, version="old"))
    assert "реплики не подписаны, обновите страницу" in text and transcript(third) == ROWS
    assert len(samples()) == 3


@pytest.mark.parametrize("change, expected", [
    (dict(listened=""), "Прослушайте отметку"),
    (dict(consent=""), "согласие сотрудника"),
    (dict(employee_id=""), "Выберите сотрудника"),
    (dict(employee_id="99"), "Выберите сотрудника"),
    (dict(start=8, end=12), "от 5 до 30 секунд"),
    (dict(start=0, end=31), "от 5 до 30 секунд"),
    (dict(start=-1, end=8), "от 5 до 30 секунд"),
    (dict(start="nan", end=8), "от 5 до 30 секунд"),
    (dict(start=55, end=70), "выходит за конец записи"),
])
def test_a_mark_needs_listening_consent_a_person_and_5_to_30_seconds(client, voices, tmp_path, change, expected):
    anna, _ = people()
    mid = meeting(tmp_path)
    who = change.pop("employee_id", anna)
    assert expected in message(mark(client, mid, who, **change))
    assert samples() == [] and transcript(mid) == ROWS


def test_a_part_already_in_a_profile_is_not_saved_twice(client, voices, tmp_path):
    anna, boris = people()
    mid = meeting(tmp_path)
    mark(client, mid, anna)
    assert "уже сохранена образцом голоса (Анна Петрова)" in message(mark(client, mid, boris, start=15, end=25))
    assert len(samples()) == 1


def test_without_the_voice_service_the_sample_waits(client, voices, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "voice_service_enabled", lambda: False)
    anna, _ = people()
    text = message(mark(client, meeting(tmp_path), anna))
    assert "Сервис голосов не подключён" in text and "Повторить извлечение признаков" in text
    assert samples()[0].embedding_status == "pending"


def test_an_old_voice_service_is_named_in_the_answer(client, voices, tmp_path, monkeypatch):
    def outdated(path, employee_id):
        speaker_id._fresh("speechbrain-ecapa-voxceleb-onnx-a9cb932")
    monkeypatch.setattr(speaker_id, "embed_voice_sample", outdated)
    anna, _ = people()
    text = message(mark(client, meeting(tmp_path), anna))
    assert "Признаки голоса извлечь не удалось: Сервис голосов на Vercel — старая версия" in text
    assert samples()[0].embedding_status == "failed"


def test_the_diagram_offers_marking_to_whom_may_listen(client, voices, tmp_path):
    anna, _ = people()
    mid = meeting(tmp_path)
    mark(client, mid, anna, start=0, end=8, apply="none")
    page = client.get(f"/meetings/{mid}").text
    assert 'data-vd-mark hidden' in page and "Сохранить в профиль голоса" in page and "протяните мышью по волне" in page
    data = json.loads(re.search(r'id="voice-diagram-data">(.*?)</script>', page, re.S).group(1))
    assert data["mark"]["samples"] == [[0.0, 8.0, "Анна Петрова", anna, "ready"]]
    assert data["mark"]["profiles"][str(anna)] == 1 and data["mark"]["need"] == 3
    assert data["mark"]["version"] == version(mid) and data["speakers"][0]["employee"] is None
    # nothing to listen to: nothing to mark
    quiet = meeting(tmp_path, stored=False, title="Без записи")
    assert "data-vd-mark" not in client.get(f"/meetings/{quiet}").text
    assert 'id="voice-diagram-data"' in client.get(f"/meetings/{quiet}").text


def test_marks_are_for_the_superuser_from_this_panel(client, voices, tmp_path):
    anna, _ = people()
    mid = meeting(tmp_path)
    assert mark(client, mid, anna, **{}).status_code == 303
    response = client.post(f"/meetings/{mid}/voice-marks", data=dict(start=20, end=28, employee_id=anna, listened="yes",
                           consent="yes"), headers={"origin": "https://evil.example"}, follow_redirects=False)
    assert response.status_code == 403 and len(samples()) == 1


def test_the_rules_of_a_mark():
    rows = [seg("A", 0, 10), seg("B", 9, 12), seg("A", 12, 30), seg("C", 31, 31)]
    assert voice_diagram.inside(rows, 5, 25) == [1, 2]           # half of 0–10 is not inside, 12–30 is (13 of 18 s)
    assert voice_diagram.inside(rows, 30, 35) == [3]              # a replica without a length: its start
    assert voice_diagram.speech_in(rows, 5, 25) == {"A": 18.0, "B": 3.0}
    assert voice_diagram.mark_label(rows, 13, 30) == "A" and voice_diagram.mark_label(rows, 5, 14) is None


# --------------------------------------------------------------------------------------------- the next meeting
def test_one_sample_names_the_voice_at_the_next_meeting(client, voices, tmp_path, monkeypatch):
    anna, boris = people()
    voices[boris] = BORIS
    mark(client, meeting(tmp_path, title="Первая"), anna)
    mark(client, meeting(tmp_path, title="Вторая"), boris, start=0, end=8)

    def fake_stt(data, model, fmt, language, timeout=60, prompt="", **extras):
        return {"text": TEXT, "segments": [{"start": 0.0, "end": 9.0, "text": TEXT, "speaker": "A"},
                                           {"start": 9.0, "end": 18.0, "text": TEXT, "speaker": "B"},
                                           {"start": 18.0, "end": 27.0, "text": TEXT, "speaker": "C"}]}
    asked = []

    def fake_identify(path, segments, min_seconds=None):  # the voices of the new recording: Анна, Борис, a stranger
        asked.append(segments)
        first = {s["speaker"]: s["start"] for s in reversed(segments)}
        order = sorted(first, key=first.get)
        return {"model_version": "real-model-v1",
                "speakers": {label: {"embedding": voice} for label, voice in zip(order, [ANNA, BORIS, STRANGER])}}
    monkeypatch.setattr(transcribe.config, "llm_enabled", lambda: True)
    monkeypatch.setattr(llm, "transcribe_audio", fake_stt)
    monkeypatch.setattr(llm, "chat", lambda *a, **k: json.dumps({"replacements": []}))
    monkeypatch.setattr(audio, "duration", lambda p: 27.0)
    monkeypatch.setattr(audio, "extract", lambda src, dst, start, sec, cleanup=None: open(dst, "wb").write(b"x"))
    monkeypatch.setattr(speaker_id, "embed_speakers", fake_identify)
    record = tmp_path / "new.mp3"
    record.write_bytes(b"audio")
    _, segments = transcribe.transcribe(str(record), dict(settings_store.all_settings(), transcribe_model="openai/gpt-4o-transcribe"))
    assert asked, "the voice service is asked for the voices of the new recording"
    assert [(s["speaker"], s.get("speaker_source")) for s in segments] == [
        ("Анна Петрова", "voiceprint"), ("Борис Смирнов", "voiceprint"), (segments[2]["speaker"], None)]
    assert segments[0]["speaker_employee_id"] == anna and segments[0]["speaker_similarity"] == 1.0
    # the participants are known: only their profiles are compared
    _, segments = transcribe.transcribe(str(record), dict(settings_store.all_settings(), transcribe_model="openai/gpt-4o-transcribe",
                                                          _participants=[boris]))
    assert [s.get("speaker_source") for s in segments] == [None, "voiceprint", None]


def test_a_voice_like_two_profiles_or_a_revoked_one_is_not_named(client, voices, tmp_path, monkeypatch):
    anna, boris = people()
    voices[boris] = vec(1, 0.05, 0.1)  # Борис sounds almost like Анна: no clear lead
    mark(client, meeting(tmp_path, title="Первая"), anna)
    mark(client, meeting(tmp_path, title="Вторая"), boris, start=0, end=8)
    monkeypatch.setattr(speaker_id, "embed_speakers", lambda path, segments, min_seconds=None: {
        "model_version": "real-model-v1", "speakers": {"Спикер 1": {"embedding": ANNA}}})
    rows = [seg("Спикер 1", 0, 9)]
    profiles = speaker_id.enrolled_embeddings()
    assert speaker_id.identify_embedded_segments("x.mp3", rows, profiles) == rows
    with SessionLocal() as s:  # Борис withdrew: Анна is clear again — and a profile revoked meanwhile is not used
        speaker_id.revoke_employee_voice_data(s, boris)
        s.commit()
    assert speaker_id.identify_embedded_segments("x.mp3", rows, profiles)[0]["speaker"] == "Анна Петрова"
    with SessionLocal() as s:
        speaker_id.revoke_employee_voice_data(s, anna)
        s.commit()
    assert speaker_id.identify_embedded_segments("x.mp3", rows, profiles) == rows


def test_two_marks_of_one_meeting_are_one_recording_with_their_average_voice():
    one = dict(source_audio_sha256="a", source_meeting_id=1, embedding=vec(1, 0), employee_id=1)
    two = dict(source_audio_sha256="a", source_meeting_id=1, embedding=vec(0, 1), employee_id=1)
    other = dict(source_audio_sha256="b", source_meeting_id=2, embedding=vec(1, 1), employee_id=1)
    [first, second] = speaker_id._recordings([one, two, other])
    assert first["samples"] == 2 and first["embedding"][:2] == pytest.approx([2 ** -0.5, 2 ** -0.5])
    assert second is other


def test_the_voice_way_names_the_voices_by_the_profiles(client, voices, tmp_path, monkeypatch):
    anna, _ = people()
    mark(client, meeting(tmp_path), anna, apply="none")
    monkeypatch.setattr(diarize, "unavailable", lambda name: "")
    voice = {"Спикер 1": ANNA, "Спикер 2": STRANGER}

    def groups(path, segments, total, progress=None):
        spoken = {}
        for s in segments:
            spoken[s["speaker"]] = spoken.get(s["speaker"], 0.0) + s["end"] - s["start"]
        return [{"window": 0, "label": label, "embedding": voice[label], "seconds": seconds, "model": "real-model-v1"}
                for label, seconds in spoken.items()]
    monkeypatch.setattr(diarize, "voice_groups", groups)
    record = tmp_path / "new.mp3"
    record.write_bytes(b"audio")
    rows = [seg("Спикер 1", 0, 30), seg("Спикер 2", 30, 60), seg("Спикер 1", 60, 90)]
    result, info = diarize.relabel(str(record), rows, 90, {"diarization_method": "voice", "diarization_voice_threshold": 0.5})
    assert info["method"] == "voice" and info["profiled"] == 1 and "remembered" not in info
    assert result[0]["speaker"] == result[2]["speaker"] == "Анна Петрова"
    assert result[0]["speaker_source"] == "voiceprint" and "speaker_memory" not in result[0]
    assert result[1]["speaker"] == "Спикер 2"
    # «Переразметить голоса» of an older meeting finds her too
    proposal = voice_relabel.propose(rows, str(record), "voice", {"diarization_voice_threshold": 0.5}, 90, [])
    assert proposal["summary"]["profiled"] == 1 and proposal["summary"]["remembered"] == 0
    # the participants are known and she was not there: her voice is not given to anybody
    result, info = diarize.relabel(str(record), rows, 90, {"diarization_method": "voice", "diarization_voice_threshold": 0.5},
                                   allowed=set())
    assert "profiled" not in info and result[0]["speaker"] == "Спикер 1"
