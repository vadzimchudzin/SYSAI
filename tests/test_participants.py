"""Who was at a meeting (participants.py): taken from «Напоминания», corrected on the meeting page, and used for the
names of the voices, the hint of the whole-record labelling and «Присутствовали» of the protocol."""
import datetime as dt

from app import analyze, participants, pipeline, reminders, render, settings_store, ticnote_ingest, transcript_edit
from app.db import Employee, Meeting, MeetingAlert, SessionLocal

MONDAY = dt.date(2026, 3, 2)


def staff(*names):
    with SessionLocal() as s:
        rows = [Employee(name=name, position="инженер") for name in names]
        s.add_all(rows)
        s.commit()
        return [row.id for row in rows]


def regular(title, at, recipients, weekdays=(0,), once=None):
    with SessionLocal() as s:
        row = MeetingAlert(title=title, meet_time=at, weekdays=list(weekdays), once_date=once, recipients=list(recipients))
        s.add(row)
        s.commit()
        return row.id


def local(day, hhmm):
    hour, minute = (int(x) for x in hhmm.split(":"))
    return dt.datetime.combine(day, dt.time(hour, minute), tzinfo=reminders.zone())


def meeting(title="Запись TicNote", day=MONDAY, recorded=None, source="ticnote", transcript=None):
    options = {"recorded_at": recorded.astimezone(dt.timezone.utc).isoformat()} if recorded else {}
    with SessionLocal() as s:
        row = Meeting(title=title, filename="rec.wav", source=source, status="awaiting_approval", meeting_date=day,
                      options=options, transcript=transcript, duration_sec=120)
        s.add(row)
        s.commit()
        return row.id


def stored(mid):
    with SessionLocal() as s:
        return s.get(Meeting, mid).participants


def ensure(mid):
    with SessionLocal() as s:
        m = s.get(Meeting, mid)
        done = participants.ensure(s, m)
        s.commit()
        return done, participants.view(s, m)


def test_a_recording_takes_the_people_of_its_regular_meeting():
    anna, boris, vera = staff("Анна Петрова", "Борис Смирнов", "Вера Иванова")
    regular("Планёрка", "10:00", [anna, boris])
    regular("Разбор продаж", "15:00", [str(vera)])
    done, view = ensure(meeting(recorded=local(MONDAY, "10:07")))
    assert done and [p["name"] for p in view["people"]] == ["Анна Петрова", "Борис Смирнов"]
    assert view["origin"] == "Из напоминания «Планёрка»." and view["count"] == 2
    afternoon = meeting(recorded=local(MONDAY, "15:02"))
    assert ensure(afternoon)[1]["ids"] == [vera]
    assert ensure(afternoon)[0] is False  # a list once set is not replaced
    assert ensure(meeting(recorded=local(MONDAY, "21:30")))[0] is False  # far from both: not clear which
    assert ensure(meeting(title="Разбор продаж — запись"))[1]["ids"] == [vera]  # no time: the title decides
    assert ensure(meeting(day=MONDAY + dt.timedelta(days=1)))[0] is False  # nothing on Tuesday


def test_one_regular_meeting_on_the_day_is_the_one_and_alerts_without_people_do_not_count():
    anna, boris = staff("Анна Петрова", "Борис Смирнов")
    regular("Пустая", "09:00", [])
    regular("Совет", "18:00", [anna], weekdays=(), once=MONDAY)
    done, view = ensure(meeting(recorded=local(MONDAY, "11:00")))
    assert done and view["ids"] == [anna] and view["origin"] == "Из напоминания «Совет»."


def test_processing_takes_the_list_and_names_voices_only_from_it():
    anna, vera = staff("Анна Петрова", "Вера Иванова")
    regular("Планёрка", "10:00", [anna])
    mid = meeting(recorded=local(MONDAY, "10:01"))
    assert pipeline._participants(mid) == ([anna], 1, "Анна Петрова")
    assert pipeline._participants(meeting(day=MONDAY + dt.timedelta(days=2))) == (None, 0, "")
    segments = [{"speaker": "Спикер 1", "start": 0, "end": 5, "text": "Доброе утро. Это Вера, начинаем."},
                {"speaker": "Спикер 2", "start": 5, "end": 9, "text": "Да, слушаю."}]
    assert pipeline._name_speakers(segments, None)[0]["speaker"] == "Вера Иванова"
    assert pipeline._name_speakers(segments, [anna])[0]["speaker"] == "Спикер 1"  # Вера was not there


def test_the_protocol_says_who_was_there_and_the_analysis_knows_it():
    anna, boris = staff("Анна Петрова", "Борис Смирнов")
    mid = meeting()
    with SessionLocal() as s:
        m = s.get(Meeting, mid)
        m.report = {"title": "Планёрка", "participants": [{"speaker": "Спикер 1", "name": "Анна Петрова"}]}
        assert "Присутствовали" not in render.report_html(m, settings_store.all_settings())
        participants.set_people(s, m, [str(boris), anna, "x", 999], ["Иван, подрядчик", " иван,  подрядчик "], by="sysai")
        s.commit()
        html = render.report_html(m, settings_store.all_settings())
    assert "Присутствовали:</b> Борис Смирнов, Анна Петрова, Иван, подрядчик (гость)" in html and "Участников: 3" in html
    base = {"report_model": "m", "glossary": ""}
    assert (analyze._fingerprint([], [], base, MONDAY) !=
            analyze._fingerprint([], [], dict(base, attendees="Анна Петрова"), MONDAY))


def test_the_meeting_page_corrects_the_list(client):
    anna, boris, vera = staff("Анна Петрова", "Борис Смирнов", "Вера Иванова")
    mid = meeting()
    page = client.get(f"/meetings/{mid}").text
    assert "Отметить участников" in page and "Список не задан" in page and "Взять из напоминания" not in page
    response = client.post(f"/meetings/{mid}/participants", follow_redirects=False,
                           data={"ids": [str(anna), str(vera)], "guests": "Иван, подрядчик\n\nиван, подрядчик", "action": "save"})
    assert response.status_code == 303 and response.headers["location"] == f"/meetings/{mid}?people=saved#meeting-participants"
    data = stored(mid)
    assert data["people"] == [{"employee_id": anna}, {"employee_id": vera}, {"guest": "Иван, подрядчик"}]
    assert data["source"] == "manual" and data["by"]
    page = client.get(response.headers["location"]).text
    assert "Участники сохранены" in page and "Участники · 3" in page and "Исправлено вручную" in page
    assert page.count('name="ids"') == 3 and page.count("checked") >= 2
    assert client.post(f"/meetings/{mid}/participants", data={"action": "alert"}, follow_redirects=False).headers["location"].endswith("people=noalert#meeting-participants")
    assert stored(mid)["source"] == "manual"
    regular("Планёрка", "10:00", [boris])
    assert "Взять из напоминания «Планёрка»" in client.get(f"/meetings/{mid}").text
    client.post(f"/meetings/{mid}/participants", data={"action": "alert"})
    assert stored(mid)["people"] == [{"employee_id": boris}] and stored(mid)["source"] == "alert"
    client.post(f"/meetings/{mid}/participants", data={"action": "clear"})
    assert stored(mid) is None and "Список не задан" in client.get(f"/meetings/{mid}").text
    assert client.post(f"/meetings/{mid}/participants", data={"action": "drop"}).status_code == 400
    assert client.post("/meetings/9999/participants", data={"action": "clear"}).status_code == 404


def test_a_person_chosen_for_a_voice_was_at_the_meeting(client):
    anna, boris, vera = staff("Анна Петрова", "Борис Смирнов", "Вера Иванова")
    transcript = [{"speaker": "Спикер 1", "start": 0, "end": 10, "text": "начинаем планёрку по объекту"},
                  {"speaker": "Спикер 2", "start": 10, "end": 20, "text": "по объекту всё идёт по плану"},
                  {"speaker": "Спикер 3", "start": 20, "end": 30, "text": "у подрядчика есть вопросы по срокам"}]
    mid = meeting(transcript=transcript)
    client.post(f"/meetings/{mid}/speakers", data={"speaker": "Спикер 1", "employee_id": str(anna)})
    assert stored(mid) is None  # nobody chose who was there: one name is not the whole list
    with SessionLocal() as s:
        participants.set_people(s, s.get(Meeting, mid), [anna], [])
        s.commit()
    page = client.get(f"/meetings/{mid}").text
    assert '<optgroup label="Участники встречи">' in page and '<optgroup label="Другие сотрудники">' in page
    client.post(f"/meetings/{mid}/speakers", data={"speaker": "Спикер 2", "employee_id": str(boris)})
    client.post(f"/meetings/{mid}/speakers", data={"speaker": "Спикер 3", "employee_id": "guest", "guest_name": ""})
    assert participants.employee_ids(type("M", (), {"participants": stored(mid)})()) == [anna, boris]
    with SessionLocal() as s:
        segment = s.get(Meeting, mid).transcript[2]
    client.post(f"/meetings/{mid}/segments/2/speaker",
                data={"sig": transcript_edit.signature(segment), "who": "guest", "guest_name": "Пётр (Альфа)"})
    with SessionLocal() as s:
        segment = s.get(Meeting, mid).transcript[2]
    editor = client.get(f"/meetings/{mid}/segments/2?fragment=1").text
    assert '<optgroup label="Участники встречи">' in editor and f'value="employee:{boris}"' in editor
    client.post(f"/meetings/{mid}/segments/2/speaker", data={"sig": transcript_edit.signature(segment), "who": f"employee:{vera}"})
    data = stored(mid)
    assert data["people"] == [{"employee_id": anna}, {"employee_id": boris}, {"employee_id": vera}, {"guest": "Пётр (Альфа)"}]


def test_a_ticnote_recording_keeps_when_it_was_recorded():
    moment = dt.datetime(2026, 3, 2, 7, 5, tzinfo=dt.timezone.utc)
    assert ticnote_ingest._recorded({"createTimestamp": int(moment.timestamp() * 1000)}) == {"recorded_at": moment.isoformat()}
    assert ticnote_ingest._recorded({}) == {}


def test_processing_labels_the_whole_record_with_the_number_of_participants(monkeypatch, tmp_path):
    from app import config
    from test_recording_storage import make_wav
    anna, boris = staff("Анна Петрова", "Борис Смирнов")
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "test-key")
    settings_store.set_many({"approver_chat_ids": [], "diarization_method": "voice"})
    parts = [{"speaker": "Спикер 1", "start": 0, "end": 6, "text": "начинаем планёрку"},
             {"speaker": "Спикер 2", "start": 6, "end": 12, "text": "акт подписан"},
             {"speaker": "Спикер 3", "start": 12, "end": 18, "text": "отлично, тогда дальше"}]
    monkeypatch.setattr(pipeline.transcribe, "transcribe", lambda *a, **kw: (20.0, [dict(s) for s in parts]))
    seen = {}

    def relabel(audio_path, segments, total, settings, people=0, progress=None, name=None, meeting_id=None, allowed=None):
        seen.update(people=people, method=settings.get("diarization_method"), audio=audio_path, meeting_id=meeting_id,
                    allowed=allowed)
        joined = [dict(s, speaker="Спикер 1" if s["speaker"] == "Спикер 3" else s["speaker"]) for s in segments]
        return joined, {"method": "voice", "wanted": "voice", "error": "", "voices": 2, "seconds": 1.2}

    def analyse(segments, employees, settings, *args, **kwargs):
        seen.update(speakers=[s["speaker"] for s in segments], attendees=settings.get("attendees"))
        return {"title": "Отчёт", "summary": "Итог", "tasks": []}
    monkeypatch.setattr(pipeline.diarize, "relabel", relabel)
    monkeypatch.setattr(pipeline.analyze, "analyze", analyse)
    path = make_wav(tmp_path / "upload.wav")
    mid = pipeline.create_meeting("upload.wav", str(path), title="Планёрка")
    with SessionLocal() as s:
        participants.set_people(s, s.get(Meeting, mid), [anna, boris], [])
        s.commit()
    pipeline.process(mid)
    assert seen["people"] == 2 and seen["method"] == "voice" and seen["audio"] == str(path)
    assert seen["meeting_id"] and seen["allowed"] is not None  # remembered voices: only the participants
    assert seen["speakers"] == ["Спикер 1", "Спикер 2", "Спикер 1"] and seen["attendees"] == "Анна Петрова, Борис Смирнов"
    with SessionLocal() as s:
        m = s.get(Meeting, mid)
        assert m.status == "awaiting_approval" and m.options["diarization"]["method"] == "voice"
        assert [x["speaker"] for x in m.transcript] == ["Спикер 1", "Спикер 2", "Спикер 1"]
