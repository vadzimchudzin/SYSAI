"""Voice diagram: the loudness outline kept from the recording, who-spoke-when data and manual speaker corrections."""
import array
import json
import math
import pathlib
import re
import wave

import pytest

from app import config, db, pipeline, settings_store, voice_diagram, waveform
from app.db import Employee, Meeting, SessionLocal


def make_wav(path, seconds=20, loud=((4, 8), (12, 16)), rate=16000):
    values = array.array("h")
    for i in range(seconds * rate):
        t = i / rate
        amplitude = 9000 if any(a <= t < b for a, b in loud) else 40
        values.append(int(amplitude * math.sin(2 * math.pi * 220 * t)))
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(values.tobytes())
    return path


def at(outline, second):
    return outline["peaks"][int(second / outline["duration"] * len(outline["peaks"]))]


def seg(speaker, start, end, text="Обсуждаем план работ на неделю", **extra):
    return dict(speaker=speaker, start=start, end=end, text=text, **extra)


def add_meeting(**fields):
    fields.setdefault("status", "awaiting_approval")
    fields.setdefault("source", "web")
    with SessionLocal() as s:
        meeting = Meeting(**fields)
        s.add(meeting)
        s.commit()
        return meeting.id


def add_employee(name, active=True):
    with SessionLocal() as s:
        employee = Employee(name=name, active=active)
        s.add(employee)
        s.commit()
        return employee.id


def transcript_of(mid):
    with SessionLocal() as s:
        return s.get(Meeting, mid).transcript


# ---------------------------------------------------------------------------------------------------- waveform.py
def test_outline_follows_the_loudness_of_a_real_recording(tmp_path):
    outline = waveform.compute(make_wav(tmp_path / "talk.wav"), duration_hint=20)
    assert outline["v"] == waveform.VERSION and abs(outline["duration"] - 20) < 0.05
    assert len(outline["peaks"]) == waveform.bins_for(20) == 600
    assert all(isinstance(v, int) and 0 <= v <= 100 for v in outline["peaks"])
    assert min(at(outline, s) for s in (4.5, 6, 7.5, 12.5, 14, 15.5)) >= 90
    assert max(at(outline, s) for s in (1, 3, 10, 18)) <= 5
    assert "synthetic" not in outline


def test_outline_of_an_undecodable_file_is_none(tmp_path):
    broken = tmp_path / "broken.wav"
    broken.write_bytes(b"not audio at all")
    assert waveform.compute(broken) is None
    assert waveform.compute(tmp_path / "missing.wav") is None


def test_outline_size_is_bounded():
    assert waveform.bins_for(1) == waveform.MIN_BINS
    assert waveform.bins_for(1000) == 2000
    assert waveform.bins_for(4 * 3600) == waveform.MAX_BINS
    assert waveform.bins_for(float("nan")) == waveform.bins_for(-5) == waveform.MIN_BINS
    assert waveform.outline([], 10) is None
    assert waveform.outline([0] * 50, 10)["peaks"] == [0] * waveform.MIN_BINS


def test_stored_outline_is_validated_before_drawing():
    good = {"v": 1, "duration": 12.5, "peaks": [0, 50, 100]}
    assert waveform.clean(good) == {"duration": 12.5, "peaks": [0, 50, 100], "synthetic": False}
    for bad in (None, [], {"v": 2, "duration": 1, "peaks": [1]}, {"v": 1, "duration": 0, "peaks": [1]},
                {"v": 1, "duration": float("inf"), "peaks": [1]}, {"v": 1, "duration": 1, "peaks": []},
                {"v": 1, "duration": 1, "peaks": [101]}, {"v": 1, "duration": 1, "peaks": [True]},
                {"v": 1, "duration": 1, "peaks": ["5"]}, {"v": 1, "duration": 1, "peaks": [1] * (waveform.MAX_STORED_BINS + 1)}):
        assert waveform.clean(bad) is None, bad


def test_demo_outline_is_synthetic_deterministic_and_follows_the_transcript():
    segments = [seg("Спикер 1", 0, 20), seg("Спикер 2", 40, 60)]
    first, second = waveform.synthetic(segments, 80), waveform.synthetic(segments, 80)
    assert first == second and first["synthetic"] is True
    speech = [at(first, s) for s in range(1, 19)] + [at(first, s) for s in range(41, 59)]
    quiet = [at(first, s) for s in range(22, 38)] + [at(first, s) for s in range(62, 79)]
    assert sum(speech) / len(speech) > 40 > 5 > max(quiet)
    assert waveform.synthetic(segments, 0) is None and waveform.synthetic(segments, "x") is None


# ----------------------------------------------------------------------------------------------- voice_diagram.py
def test_diagram_speakers_shares_turns_and_cross_talk():
    transcript = [
        seg("Анна Петрова", 0, 10, speaker_source="voiceprint", speaker_employee_id=7),
        seg("Анна Петрова", 10.8, 20, speaker_source="voiceprint", speaker_employee_id=7),  # same turn: short pause
        seg("Спикер 2", 19, 30),                       # one second of cross-talk with Анна
        seg("Борис", 30, 40),
        seg("Анна Петрова", 43, 50, speaker_source="voiceprint", speaker_employee_id=7),
        seg("Анна Петрова", 52, 60, speaker_source="voiceprint", speaker_employee_id=7),  # a new turn: 2 s pause
        seg("Гость", 60, 62, speaker_source="manual", speaker_guest=True),
        seg("Спикер 2", 61.9, 70),                     # 0.1 s overlap: rounding, not cross-talk
        "not a segment", {"speaker": "X", "start": None}, {"speaker": "Y", "start": float("nan"), "end": 3},
    ]
    diagram = voice_diagram.build(transcript, duration=75, employees=[type("E", (), {"id": 3, "name": "Борис"})()])
    people = {p["name"]: p for p in diagram["speakers"]}
    assert [p["name"] for p in diagram["speakers"]] == ["Анна Петрова", "Спикер 2", "Борис", "Гость"]
    assert [p["n"] for p in diagram["speakers"]] == [1, 2, 3, 4]
    assert {name: p["status"] for name, p in people.items()} == {
        "Анна Петрова": "voice", "Спикер 2": "unknown", "Борис": "context", "Гость": "guest"}
    assert people["Анна Петрова"]["employee_id"] == 7 and people["Борис"]["employee_id"] == 3
    assert people["Гость"]["employee_id"] is None
    assert sum(p["share"] for p in diagram["speakers"]) == 100
    assert people["Анна Петрова"]["seconds"] == pytest.approx(10 + 9.2 + 7 + 8)
    assert diagram["duration"] == 75 and diagram["speech_seconds"] == pytest.approx(10 + (40 - 10.8) + 7 + (70 - 52))
    assert [(t[0], t[1], t[2]) for t in diagram["turns"]] == [
        (0, 0, 20), (1, 19, 30), (2, 30, 40), (0, 43, 50), (0, 52, 60), (3, 60, 62), (1, 61.9, 70)]
    assert diagram["turns"][0][3:] == [0, 1]
    assert diagram["overlaps"] == [[19, 20]]
    assert [p["name"] for p in diagram["unknown"]] == ["Спикер 2"]
    assert diagram["peaks"] is None and diagram["voice_matched"] == 1
    payload = voice_diagram.client_payload(diagram)
    assert "samples" not in json.dumps(payload) and payload["speakers"][0]["color"] == voice_diagram.PALETTE[0]


def test_diagram_without_timed_speech_is_none():
    assert voice_diagram.build(None) is None
    assert voice_diagram.build([]) is None
    assert voice_diagram.build([{"speaker": "A", "text": "без времени"}]) is None
    assert voice_diagram.build([seg("A", 0, 0)], duration=0) is None


def test_samples_are_the_longest_replicas_in_time_order():
    transcript = [seg("Спикер 1", 0, 2, "коротко"), seg("Спикер 1", 10, 40, "x" * 400), seg("Спикер 1", 50, 60, "средне"),
                  seg("Спикер 1", 70, 75, "   "), seg("Спикер 1", 80, 100, "длинно")]
    samples = voice_diagram.build(transcript)["speakers"][0]["samples"]
    assert [s["start"] for s in samples] == [10, 50, 80]
    assert len(samples[0]["text"]) == voice_diagram.QUOTE_CHARS and samples[0]["text"].endswith("…")


def test_assignment_keeps_the_automatic_labelling_and_can_be_undone():
    original = [seg("Спикер 2", 0, 5, speaker_confidence=0.4), seg("Анна", 5, 9, speaker_source="voiceprint",
                speaker_employee_id=4, speaker_similarity=0.8), seg("Спикер 2", 9, 12)]
    renamed, changed = voice_diagram.assign(original, "Спикер 2", "Борис Смирнов", employee_id=9, by="director")
    assert changed == 2 and renamed[1] is original[1]
    assert renamed[0] == {**seg("Борис Смирнов", 0, 5), "speaker_source": "manual", "speaker_employee_id": 9,
                          "speaker_assigned_by": "director", "speaker_auto": {"speaker": "Спикер 2", "speaker_confidence": 0.4}}
    again, changed = voice_diagram.assign(renamed, "Борис Смирнов", "Гость", guest=True)
    assert changed == 2 and again[0]["speaker_auto"] == {"speaker": "Спикер 2", "speaker_confidence": 0.4}
    assert again[0]["speaker_guest"] is True and "speaker_employee_id" not in again[0]
    voiceprint, _ = voice_diagram.assign(original, "Анна", "Анна Петрова", employee_id=5)
    assert "speaker_similarity" not in voiceprint[1] and voiceprint[1]["speaker_source"] == "manual"
    for edited in (again, voiceprint):
        restored, count = voice_diagram.reset(edited)
        assert restored == original and count > 0
    assert voice_diagram.reset(original) == (original, 0)


def test_guest_names_do_not_merge_different_people():
    transcript = [seg("Спикер 1", 0, 1), seg("Гость", 1, 2), seg("Гость 2", 2, 3)]
    assert voice_diagram.guest_name(transcript, "", "Спикер 1") == "Гость 3"
    assert voice_diagram.guest_name(transcript, "", "Гость") == "Гость"
    assert voice_diagram.guest_name(transcript, "  Пётр   из Альфы ", "Спикер 1") == "Пётр из Альфы"
    assert len(voice_diagram.guest_name(transcript, "я" * 300, "Спикер 1")) == 80


# ------------------------------------------------------------------------------------------------------ pipeline
def test_demo_processing_stores_a_synthetic_outline():
    settings_store.set_many({"approver_chat_ids": []})
    mid = pipeline.create_meeting("demo", source="demo")
    pipeline.process(mid)
    with SessionLocal() as s:
        m = s.get(Meeting, mid)
        assert m.status == "awaiting_approval"
        assert m.waveform["synthetic"] is True and len(m.waveform["peaks"]) == waveform.bins_for(m.duration_sec)


@pytest.fixture
def real_processing(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "test-key")
    settings_store.set_many({"approver_chat_ids": []})
    segments = [seg("Спикер 1", 3.5, 8.2), seg("Спикер 2", 11.8, 16.3)]
    monkeypatch.setattr(pipeline.transcribe, "transcribe", lambda *a, **kw: (20.0, [dict(s) for s in segments]))
    monkeypatch.setattr(pipeline.analyze, "analyze", lambda *a, **kw: {"title": "Отчёт", "summary": "Итог", "tasks": []})
    path = make_wav(tmp_path / "upload.wav")
    return pipeline.create_meeting("upload.wav", str(path)), path


def test_processing_keeps_the_outline_and_still_deletes_the_original(real_processing):
    mid, path = real_processing
    pipeline.process(mid)
    with SessionLocal() as s:
        m = s.get(Meeting, mid)
        assert m.status == "awaiting_approval" and m.audio_path == ""
        assert not path.exists()
        assert "synthetic" not in m.waveform and at(m.waveform, 6) >= 90 and at(m.waveform, 10) <= 5


def test_outline_is_decoded_next_to_the_analysis_in_production(real_processing, monkeypatch):
    mid, path = real_processing
    monkeypatch.setattr(config, "TESTING", False)
    threads = []
    original_start = pipeline.threading.Thread.start

    def start(thread):
        threads.append(thread.name)
        original_start(thread)

    monkeypatch.setattr(pipeline.threading.Thread, "start", start)
    pipeline.process(mid)
    assert f"waveform-{mid}" in threads
    with SessionLocal() as s:
        m = s.get(Meeting, mid)
        assert m.waveform and m.waveform["peaks"] and not path.exists()


def test_a_failing_outline_never_stops_the_meeting(real_processing, monkeypatch):
    mid, path = real_processing
    monkeypatch.setattr(pipeline.waveform, "compute", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("ffmpeg crashed")))
    pipeline.process(mid)
    with SessionLocal() as s:
        m = s.get(Meeting, mid)
        assert m.status == "awaiting_approval" and m.waveform is None and m.report["title"] == "Отчёт"
        assert not path.exists()


def test_the_outline_is_computed_once(real_processing, monkeypatch):
    mid, path = real_processing
    with SessionLocal() as s:
        s.get(Meeting, mid).waveform = {"v": 1, "duration": 20, "peaks": [7] * 600}
        s.commit()
    monkeypatch.setattr(pipeline.waveform, "compute", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("decoded twice")))
    pipeline.process(mid)
    with SessionLocal() as s:
        assert s.get(Meeting, mid).waveform["peaks"] == [7] * 600


# ----------------------------------------------------------------------------------------------------------- page
def payload_of(html):
    found = re.search(r'<script type="application/json" id="voice-diagram-data">(.*?)</script>', html, re.S)
    return json.loads(found.group(1)) if found else None


TRANSCRIPT = [
    seg("Анна Петрова", 0, 30, "Коллеги, начинаем планёрку.", speaker_source="voiceprint", speaker_employee_id=1),
    seg("Спикер 2", 31, 50, "По складу отгрузку переносим на пятницу."),
    seg("Анна Петрова", 51, 70, "Тогда предупредим клиента сегодня.", speaker_source="voiceprint", speaker_employee_id=1),
    seg("Спикер 2", 71, 90, "Я напишу им до обеда."),
]


def test_meeting_page_draws_the_voice_diagram(client):
    anna = add_employee("Анна Петрова")
    add_employee("Борис Смирнов")
    outline = {"v": 1, "duration": 90.0, "peaks": [50] * 600}
    mid = add_meeting(title="Планёрка", transcript=TRANSCRIPT, waveform=outline, duration_sec=90,
                      report={"title": "Планёрка", "summary": "", "tasks": []})
    html = client.get(f"/meetings/{mid}").text
    assert 'id="voice-diagram"' in html and "Кто и когда говорил" in html
    assert re.search(r'/static/voice-diagram\.js\?v=[0-9a-f]{10}"', html)
    assert re.search(r'/static/voice-diagram\.css\?v=[0-9a-f]{10}"', html)
    data = payload_of(html)
    assert [p["name"] for p in data["speakers"]] == ["Анна Петрова", "Спикер 2"]
    assert data["peaks"] == [50] * 600 and data["peaksDuration"] == 90 and len(data["turns"]) == 4
    assert 'id="seg-0"' in html and 'data-vd-seq="3"' in html
    assert "голос совпал" in html and "не распознан" in html
    assert "Вопрос: кто это?" in html and "По складу отгрузку переносим на пятницу." in html
    assert f'<option value="{anna}">Анна Петрова</option>' in html and 'value="guest"' in html
    assert "Волна громкости строится для новых записей" not in html


def test_old_meetings_draw_speech_from_the_transcript_alone(client):
    mid = add_meeting(title="Старое", transcript=TRANSCRIPT, duration_sec=90, status="done",
                      report={"title": "Старое", "summary": "", "tasks": []})
    html = client.get(f"/meetings/{mid}").text
    assert payload_of(html)["peaks"] is None
    assert "Волна громкости строится для новых записей" in html
    assert 'class="vd-static"' in html  # drawn even without JavaScript


def test_no_diagram_before_the_transcript(client):
    mid = add_meeting(title="В очереди", status="queued")
    html = client.get(f"/meetings/{mid}").text
    assert 'id="voice-diagram"' not in html and "voice-diagram.js" not in html


def test_corrections_are_not_offered_while_transcribing(client):
    mid = add_meeting(title="Идёт расшифровка", status="transcribing", transcript=TRANSCRIPT)
    html = client.get(f"/meetings/{mid}").text
    assert 'id="voice-diagram"' in html
    assert "Вопрос: кто это?" not in html and "Исправить говорящего" not in html


def test_speaker_names_cannot_break_out_of_the_page(client):
    evil = '</script><script>alert(1)</script>'
    mid = add_meeting(title="XSS", transcript=[seg(evil, 0, 5, "<b>текст</b>")], duration_sec=5)
    html = client.get(f"/meetings/{mid}").text
    assert evil not in html and "<b>текст</b>" not in html
    assert payload_of(html)["speakers"][0]["name"] == evil


# ------------------------------------------------------------------------------------------------- corrections
def test_assigning_a_speaker_renames_every_replica(client):
    boris = add_employee("Борис Смирнов")
    mid = add_meeting(title="Планёрка", transcript=TRANSCRIPT, duration_sec=90)
    response = client.post(f"/meetings/{mid}/speakers", data={"speaker": "Спикер 2", "employee_id": str(boris)},
                           follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == f"/meetings/{mid}?speakers=assigned&changed=2#voice-diagram"
    saved = transcript_of(mid)
    assert [s["speaker"] for s in saved] == ["Анна Петрова", "Борис Смирнов", "Анна Петрова", "Борис Смирнов"]
    assert saved[1]["speaker_source"] == "manual" and saved[1]["speaker_employee_id"] == boris
    assert saved[1]["speaker_assigned_by"] and saved[1]["speaker_auto"] == {"speaker": "Спикер 2"}
    assert saved[0] == TRANSCRIPT[0]
    html = client.get(response.headers["location"]).text
    assert "говорящий исправлен во всех его репликах (2)" in html
    assert "указан вручную" in html and "Вопрос: кто это?" not in html
    assert "Вернуть автоматическую разметку" in html


def test_a_guest_gets_a_free_name_or_the_typed_one(client):
    transcript = TRANSCRIPT + [seg("Спикер 3", 91, 95), seg("Спикер 4", 96, 99)]
    mid = add_meeting(title="Гости", transcript=transcript, duration_sec=99)
    client.post(f"/meetings/{mid}/speakers", data={"speaker": "Спикер 2", "employee_id": "guest", "guest_name": ""})
    client.post(f"/meetings/{mid}/speakers", data={"speaker": "Спикер 3", "employee_id": "guest", "guest_name": ""})
    client.post(f"/meetings/{mid}/speakers", data={"speaker": "Спикер 4", "employee_id": "guest", "guest_name": "Пётр (Альфа)"})
    saved = transcript_of(mid)
    assert [s["speaker"] for s in saved[1::2][:2]] == ["Гость", "Гость"]
    assert saved[4]["speaker"] == "Гость 2" and saved[5]["speaker"] == "Пётр (Альфа)"
    assert all(s.get("speaker_guest") for s in saved[4:]) and "speaker_employee_id" not in saved[5]


def test_wrong_speaker_corrections_are_refused(client):
    retired = add_employee("Уволен", active=False)
    mid = add_meeting(title="Планёрка", transcript=TRANSCRIPT, duration_sec=90)
    url = f"/meetings/{mid}/speakers"
    assert client.post(url, data={"speaker": "Спикер 9", "employee_id": "guest"}).status_code == 400
    assert client.post(url, data={"speaker": "Спикер 2", "employee_id": ""}).status_code == 400
    assert client.post(url, data={"speaker": "Спикер 2", "employee_id": "999"}).status_code == 400
    assert client.post(url, data={"speaker": "Спикер 2", "employee_id": "²"}).status_code == 400
    assert client.post(url, data={"speaker": "Спикер 2", "employee_id": str(retired)}).status_code == 400
    assert client.post("/meetings/999999/speakers", data={"speaker": "Спикер 2", "employee_id": "guest"}).status_code == 404
    busy = add_meeting(title="Идёт", status="transcribing", transcript=TRANSCRIPT)
    assert client.post(f"/meetings/{busy}/speakers", data={"speaker": "Спикер 2", "employee_id": "guest"}).status_code == 409
    empty = add_meeting(title="Пусто", status="error")
    assert client.post(f"/meetings/{empty}/speakers/reset").status_code == 409
    assert transcript_of(mid) == TRANSCRIPT


def test_reset_brings_back_the_automatic_labels(client):
    boris = add_employee("Борис Смирнов")
    mid = add_meeting(title="Планёрка", transcript=TRANSCRIPT, duration_sec=90)
    client.post(f"/meetings/{mid}/speakers", data={"speaker": "Спикер 2", "employee_id": str(boris)})
    client.post(f"/meetings/{mid}/speakers", data={"speaker": "Анна Петрова", "employee_id": "guest"})
    response = client.post(f"/meetings/{mid}/speakers/reset", follow_redirects=False)
    assert response.headers["location"] == f"/meetings/{mid}?speakers=reset&changed=4#voice-diagram"
    assert transcript_of(mid) == TRANSCRIPT


def test_corrections_need_a_signed_in_user():
    from fastapi.testclient import TestClient
    from app.main import app
    mid = add_meeting(title="Планёрка", transcript=TRANSCRIPT, duration_sec=90)
    with TestClient(app) as anonymous:
        for url in (f"/meetings/{mid}/speakers", f"/meetings/{mid}/speakers/reset"):
            response = anonymous.post(url, data={"speaker": "Спикер 2", "employee_id": "guest"}, follow_redirects=False)
            assert response.status_code == 303 and response.headers["location"] == "/login"
    assert transcript_of(mid) == TRANSCRIPT


def test_the_new_column_is_added_to_an_existing_database():
    from sqlalchemy import inspect, text
    with db.engine.begin() as con:
        con.execute(text("ALTER TABLE meetings DROP COLUMN waveform"))
    db.engine.dispose()
    assert "waveform" not in {c["name"] for c in inspect(db.engine).get_columns("meetings")}
    db.init_db()
    assert "waveform" in {c["name"] for c in inspect(db.engine).get_columns("meetings")}
