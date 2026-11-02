"""«Сравнить способы»: every way of labelling the voices runs on the recordings of the references and is measured
against them; the best way and threshold can be applied from the page."""
import math

import pytest

from app import diarization_eval, diarization_runs, diarize, settings_store
from app.db import Meeting, SessionLocal


def vec(*weights):
    raw = list(weights) + [0.0] * (192 - len(weights))
    norm = math.sqrt(sum(x * x for x in raw))
    return [x / norm for x in raw]


def seg(speaker, start, end, text="обсуждаем план работ на эту неделю подробно", **extra):
    return dict(speaker=speaker, start=start, end=end, text=text, **extra)


TRUTH = [seg("Анна Петрова", 0, 30, speaker_source="manual"), seg("Борис Смирнов", 30, 60, speaker_source="manual"),
         seg("Анна Петрова", 61, 90, speaker_source="manual"), seg("Борис Смирнов", 90, 120, speaker_source="manual")]
AUTO = [seg("Спикер 1", 0, 30), seg("Спикер 2", 30, 60), seg("Спикер 3", 61, 90), seg("Спикер 2", 90, 120)]  # Анна split


def reference(audio_path=""):
    with SessionLocal() as s:
        meeting = Meeting(title="Планёрка", filename="rec.wav", source="ticnote", status="awaiting_approval",
                          transcript=TRUTH, transcript_auto=AUTO, duration_sec=120, audio_path=audio_path)
        s.add(meeting)
        s.flush()
        diarization_eval.save(meeting, by="sysai")
        s.commit()
        return meeting.id


def runs_of(mid):
    with SessionLocal() as s:
        return s.get(Meeting, mid).diarization_reference["runs"]


def fake_ways(monkeypatch, calls):
    monkeypatch.setattr(diarize, "unavailable", lambda name: "")

    def groups(path, segments, total, progress=None):
        calls.append(("voice", [s["speaker"] for s in segments]))
        return [{"window": 0, "label": "Спикер 1", "embedding": vec(1, 0, 0), "seconds": 30},
                {"window": 0, "label": "Спикер 2", "embedding": vec(0, 1, 0), "seconds": 30},
                {"window": 1, "label": "Спикер 3", "embedding": vec(0.75, 0, 0.66), "seconds": 29},  # ≈ 0.75 like Анна
                {"window": 1, "label": "Спикер 2", "embedding": vec(0, 1, 0.1), "seconds": 30}]

    def gemini(path, segments, total, settings, people=0, progress=None, details=None):
        calls.append(("gemini", people))
        return ["Спикер 1", "Спикер 2", "Спикер 1", "Спикер 2"]
    monkeypatch.setattr(diarize, "voice_groups", groups)
    monkeypatch.setattr(diarize, "gemini_labels", gemini)


def test_compare_runs_every_way_on_every_reference_and_shows_the_best(client, monkeypatch, tmp_path):
    record = tmp_path / "meeting.mp3"
    record.write_bytes(b"audio")
    mid = reference(str(record))
    calls = []
    fake_ways(monkeypatch, calls)
    response = client.post("/diarization/compare", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/diarization?msg=started#compare"
    assert calls == [("voice", ["Спикер 1", "Спикер 2", "Спикер 3", "Спикер 2"]), ("gemini", 0)]
    runs = runs_of(mid)
    assert runs["chunks"]["metrics"]["accuracy"] == pytest.approx(90 / 119, abs=0.001) and runs["gemini"]["metrics"]["accuracy"] == 1.0
    sweep = runs["voice"]["thresholds"]
    assert set(sweep) == {f"{t:.2f}" for t in diarize.THRESHOLDS}
    assert sweep["0.50"]["accuracy"] == 1.0 and sweep["0.80"]["accuracy"] < 1.0 and sweep["0.80"]["extra"] == 1
    assert "speakers" not in runs["chunks"]["metrics"] and "mapping" not in sweep["0.50"]
    status = diarization_runs.status()
    assert status["running"] is False and status["done"] == status["total"] == 1 and status["errors"] == []

    page = client.get("/diarization?msg=started").text
    assert "Сравнение способов" in page and "Сравнение запущено" in page and "Пороги сходства" in page
    assert page.count('class="pill green">лучший') >= 2 and "Gemini слушает всю запись" in page
    overview = diarization_runs.overview([diarization_eval_reference(mid)], "chunks", 0.5)
    assert overview["best"] in ("voice", "gemini") and overview["best_threshold"] in {"0.30", "0.35", "0.40", "0.45", "0.50"}
    assert [w["name"] for w in overview["ways"]] == ["chunks", "voice", "gemini"]
    assert overview["ways"][1]["threshold"] == "0.50" and overview["ways"][0]["current"]
    meeting_page = client.get(f"/meetings/{mid}").text
    assert "Способы на этой записи" in meeting_page and "Gemini слушает всю запись — <b>100%</b>" in meeting_page

    response = client.post("/diarization/method", data={"method": "voice", "threshold": "0,55"}, follow_redirects=False)
    assert response.headers["location"] == "/diarization?msg=applied#compare"
    assert settings_store.get("diarization_method") == "voice" and settings_store.get("diarization_voice_threshold") == 0.55
    assert client.post("/diarization/method", data={"method": "pyannote"}).status_code == 400
    assert client.post("/diarization/method", data={"method": "voice", "threshold": "1,5"}).status_code == 400
    assert "Способ разметки сохранён" in client.get("/diarization?msg=applied").text


def diarization_eval_reference(mid):
    with SessionLocal() as s:
        return s.get(Meeting, mid).diarization_reference


def test_without_a_recording_only_the_part_labels_are_measured(client, monkeypatch):
    mid = reference("")
    calls = []
    fake_ways(monkeypatch, calls)
    diarization_runs.start()
    runs = runs_of(mid)
    assert calls == [] and runs["chunks"]["metrics"]["accuracy"] == pytest.approx(90 / 119, abs=0.001)
    assert runs["voice"]["error"].startswith("Записи этого совещания нет") and runs["gemini"]["error"] == runs["voice"]["error"]
    page = client.get("/diarization").text
    assert "Записи этого совещания нет" in page


def test_a_way_that_cannot_run_says_why_and_the_others_go_on(client, monkeypatch, tmp_path):
    record = tmp_path / "meeting.mp3"
    record.write_bytes(b"audio")
    mid = reference(str(record))
    monkeypatch.setattr(diarize, "unavailable", lambda name: "Сервис голосов не подключён." if name == "voice" else "")
    monkeypatch.setattr(diarize, "gemini_labels", lambda *a, **k: (_ for _ in ()).throw(diarize.DiarizationError("Модель не вернула говорящих по репликам.")))
    diarization_runs.start()
    runs = runs_of(mid)
    assert runs["voice"] == {"error": "Сервис голосов не подключён.", "at": runs["voice"]["at"]}
    assert runs["gemini"]["error"] == "Модель не вернула говорящих по репликам." and "metrics" in runs["chunks"]


def test_nothing_to_compare_without_references_and_one_run_at_a_time(client, monkeypatch):
    response = client.post("/diarization/compare", follow_redirects=False)
    assert response.headers["location"] == "/diarization?msg=empty#compare"
    reference("")
    monkeypatch.setitem(diarization_runs._state, "running", True)
    assert client.post("/diarization/compare", follow_redirects=False).headers["location"] == "/diarization?msg=busy#compare"
    assert diarization_runs.start() is False
    page = client.get("/diarization").text
    assert 'http-equiv="refresh"' in page and "Сравнение идёт" in page


def test_saving_the_reference_again_keeps_the_runs_only_for_the_same_answer_key(monkeypatch):
    mid = reference("")
    with SessionLocal() as s:
        meeting = s.get(Meeting, mid)
        meeting.diarization_reference = dict(meeting.diarization_reference, runs={"chunks": {"metrics": {}, "at": "x"}})
        s.commit()
    with SessionLocal() as s:
        meeting = s.get(Meeting, mid)
        assert diarization_eval.save(meeting)["runs"] == {"chunks": {"metrics": {}, "at": "x"}}
        meeting.transcript = TRUTH[:3]
        assert diarization_eval.save(meeting)["runs"] == {}


def test_the_way_is_chosen_in_the_settings(client):
    page = client.get("/settings").text
    assert "Разметка голосов" in page and "Свой сервис голосов" in page and "Сервис голосов не подключён" in page
    assert "pyannote" not in page.lower()
    response = client.post("/settings", data={"diarization_method": "gemini", "diarization_voice_threshold": "0,62"},
                           follow_redirects=False)
    assert response.status_code == 303
    assert settings_store.get("diarization_method") == "gemini" and settings_store.get("diarization_voice_threshold") == 0.62
    assert client.post("/settings", data={"diarization_method": "pyannote"}).status_code == 400
    assert client.post("/settings", data={"diarization_voice_threshold": "1,5"}).status_code == 400


def test_the_meeting_page_says_how_the_voices_were_labelled(client):
    with SessionLocal() as s:
        meeting = Meeting(title="Планёрка", filename="rec.wav", source="web", status="awaiting_approval", transcript=AUTO,
                          duration_sec=120, options={"diarization": {"method": "chunks", "wanted": "voice",
                                                                     "error": "Сервис голосов не ответил по 3 из 4 минут"}})
        s.add(meeting)
        s.commit()
        mid = meeting.id
    page = client.get(f"/meetings/{mid}").text
    assert "Разметка «Свой сервис голосов» не сработала: Сервис голосов не ответил по 3 из 4 минут. Голоса размечены по кускам записи." in page
    with SessionLocal() as s:
        s.get(Meeting, mid).options = {"diarization": {"method": "gemini", "wanted": "gemini", "error": "", "voices": 4}}
        s.commit()
    assert "Голоса размечены по всей записи: Gemini слушает всю запись · 4 голоса." in client.get(f"/meetings/{mid}").text
