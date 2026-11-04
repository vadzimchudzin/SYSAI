"""Голоса запоминаются между встречами: a voice confirmed at a meeting («Кто это?», a corrected replica, a sure
introduction) of an employee who agreed is remembered — encrypted, without audio — and recognised at the next meetings
labelled by the voice service. Without the consent nothing is kept; withdrawing it, «Забыть голос» and deleting the
meeting remove the prints."""
import datetime as dt
import math

import pytest

from app import config, diarize, settings_store, speaker_id, voice_memory
from app.db import Employee, EmployeeVoiceMemory, Meeting, SessionLocal, now

TEXT = "обсуждаем план работ на эту неделю подробно"


def vec(*weights):
    raw = list(weights) + [0.0] * (192 - len(weights))
    norm = math.sqrt(sum(x * x for x in raw))
    return [x / norm for x in raw]


VOICE_OF = {}


def seg(speaker, start, end, text=TEXT, **extra):
    return dict(speaker=speaker, start=start, end=end, text=text, **extra)


@pytest.fixture
def memory_on(monkeypatch):
    settings_store.set_many({"voice_memory": True})
    monkeypatch.setattr(config, "voice_service_enabled", lambda: True)
    monkeypatch.setattr(diarize, "unavailable", lambda name: "")
    VOICE_OF.clear()
    VOICE_OF.update({"Спикер 1": vec(0.97, 0.05, 0.2), "Спикер 2": vec(0, 0, 1)})
    seen = []

    def groups(path, segments, total, progress=None):
        seen.append([s["speaker"] for s in segments])
        spoken = {}
        for s in segments:
            spoken[s["speaker"]] = spoken.get(s["speaker"], 0.0) + s["end"] - s["start"]
        return [{"window": 0, "label": label, "embedding": VOICE_OF.get(label) or voice_of(label), "seconds": seconds,
                 "model": "test-model"} for label, seconds in spoken.items()]
    monkeypatch.setattr(diarize, "voice_groups", groups)
    return seen


def voice_of(label):
    """employee-1 sounds like Анна, employee-2 like Борис."""
    return {"employee-1": vec(1, 0, 0.1), "employee-2": vec(0, 1, 0.1)}.get(label, vec(0, 0, 0, 1))


def employees(consent=False):
    with SessionLocal() as s:
        anna, boris = Employee(name="Анна Петрова"), Employee(name="Борис Смирнов")
        if consent:
            anna.voice_memory_consent_at = boris.voice_memory_consent_at = now()
        s.add_all([anna, boris])
        s.commit()
        assert (anna.id, boris.id) == (1, 2)
        return anna.id, boris.id


def confirmed_meeting(tmp_path, title="Планёрка"):
    """Анна and Борис corrected by hand: 24 s of clear speech each."""
    record = tmp_path / f"{title}.mp3"
    record.write_bytes(b"audio")
    rows = [seg("Анна Петрова", 0, 12, speaker_source="manual", speaker_employee_id=1),
            seg("Борис Смирнов", 12, 24, speaker_source="manual", speaker_employee_id=2),
            seg("Анна Петрова", 24, 36, speaker_source="manual", speaker_employee_id=1),
            seg("Борис Смирнов", 36, 48, speaker_source="manual", speaker_employee_id=2),
            seg("Спикер 3", 48, 50, text="да")]
    with SessionLocal() as s:
        m = Meeting(title=title, filename="rec.mp3", source="upload", status="done", transcript=rows,
                    duration_sec=50, audio_path=str(record))
        s.add(m)
        s.commit()
        return m.id


def prints_of(employee_id):
    with SessionLocal() as s:
        rows = s.query(EmployeeVoiceMemory).filter_by(employee_id=employee_id).order_by(EmployeeVoiceMemory.id).all()
        s.expunge_all()
        return rows


def consent_of(employee_id):
    with SessionLocal() as s:
        return s.get(Employee, employee_id).voice_memory_consent_at


def test_a_confirmed_voice_is_remembered_only_with_consent(client, memory_on, tmp_path):
    anna, boris = employees()
    mid = confirmed_meeting(tmp_path)
    assert voice_memory.remember(mid, by="sysai") == {} and memory_on == []  # nobody agreed: the service is not asked
    page = client.get(f"/employees?edit={anna}&tab=voice").text
    assert "Голос между встречами" in page and "нет согласия" in page and "Сотрудник согласен — запоминать голос" in page

    response = client.post(f"/employees/{anna}/voice-memory", data={"consent": "yes"}, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"].startswith(f"/employees?edit={anna}&tab=voice")
    assert consent_of(anna) is not None and consent_of(boris) is None
    rows = prints_of(anna)  # the latest confirmed meeting is remembered at once
    assert [(r.meeting_id, r.how, r.seconds, r.replicas, r.model_version) for r in rows] == [(mid, "manual", 24.0, 2, "test-model")]
    assert memory_on == [["employee-1", "employee-1"]]  # only her clear replicas, without the short «да»
    assert b"0.99" not in rows[0].encrypted_embedding and rows[0].expires_at is not None
    assert speaker_id.decrypt_voiceprint(rows[0].encrypted_embedding).startswith("[0.99")
    assert prints_of(boris) == []

    page = client.get(f"/employees?edit={anna}&tab=voice&voice_msg=x").text
    assert "Согласие сотрудника отмечено" in page and "1 из 2 встреч" in page and "подтверждён вручную" in page
    assert "Забыть голос" in page and "Отозвать согласие" in page


def test_a_voice_from_two_meetings_is_recognised_at_the_next(client, memory_on, tmp_path):
    employees(consent=True)
    first, second = confirmed_meeting(tmp_path, "Первая"), confirmed_meeting(tmp_path, "Вторая")
    assert voice_memory.remember(first) == {1: 24.0, 2: 24.0} and voice_memory.remember(second) == {1: 24.0, 2: 24.0}
    record = tmp_path / "new.mp3"
    record.write_bytes(b"audio")
    rows = [seg("Спикер 1", 0, 30), seg("Спикер 2", 30, 60), seg("Спикер 1", 60, 90)]
    result, info = diarize.relabel(str(record), rows, 90, {"diarization_method": "voice", "diarization_voice_threshold": 0.5})
    assert info["method"] == "voice" and info["remembered"] == 1
    assert result[0]["speaker"] == result[2]["speaker"] == "Анна Петрова"
    assert result[0]["speaker_source"] == "voiceprint" and result[0]["speaker_memory"] is True
    assert result[0]["speaker_employee_id"] == 1 and result[0]["speaker_similarity"] > 0.9
    assert result[1]["speaker"] == "Спикер 2" and "speaker_source" not in result[1]  # nobody like it: stays unknown

    with SessionLocal() as s:
        m = Meeting(title="Третья", filename="rec.mp3", source="upload", status="done", transcript=result, duration_sec=90)
        s.add(m)
        s.commit()
        mid = m.id
    page = client.get(f"/meetings/{mid}").text
    assert "голос совпал" in page and "Голос узнан по прошлым совещаниям" in page

    settings_store.set_many({"voice_memory": False})  # switched off: nobody is recognised
    result, info = diarize.relabel(str(record), rows, 90, {"diarization_method": "voice", "diarization_voice_threshold": 0.5})
    assert "remembered" not in info and result[0]["speaker"] == "Спикер 1"


def test_one_meeting_a_short_or_an_unclear_voice_is_not_enough():
    anna = vec(1, 0, 0.1)
    found = {"centres": {"Спикер 1": vec(0.97, 0.05, 0.2)}, "seconds": {"Спикер 1": 30}, "model": "m"}
    memory = {1: {"name": "Анна Петрова", "prints": [(10, anna, "m")]}}
    assert voice_memory.recognise(found, memory=memory, limit=0.6) == {}  # one meeting only
    memory[1]["prints"].append((11, anna, "m"))
    assert voice_memory.recognise(found, memory=memory, limit=0.6)["Спикер 1"]["employee_id"] == 1
    assert voice_memory.recognise(found, meeting_id=11, memory=memory, limit=0.6) == {}  # its own print does not count
    assert voice_memory.recognise(found, memory=memory, limit=0.995) == {}
    assert voice_memory.recognise(dict(found, model="other"), memory=memory, limit=0.6) == {}  # another voice model
    assert voice_memory.recognise(dict(found, seconds={"Спикер 1": 3}), memory=memory, limit=0.6) == {}
    assert voice_memory.recognise(found, memory=memory, limit=0.6, allowed={2}) == {}  # not at this meeting
    memory[2] = {"name": "Двойник", "prints": [(10, vec(1, 0.02, 0.12), "m"), (12, vec(1, 0.02, 0.12), "m")]}
    assert voice_memory.recognise(found, memory=memory, limit=0.6) == {}  # two people alike: nobody is chosen

    rows = [seg("Спикер 1", 0, 10, speaker_confidence=0.5), seg("Спикер 1", 10, 20, speaker_source="manual")]
    named = voice_memory.rename(rows, {"Спикер 1": {"employee_id": 1, "name": "Анна Петрова", "similarity": 0.9}})
    assert named[0]["speaker"] == "Анна Петрова" and "speaker_confidence" not in named[0]
    assert named[1] == rows[1]  # a manual correction is never changed


def test_a_sure_introduction_confirms_a_voice_too():
    with SessionLocal() as s:
        s.add_all([Employee(name="Анна Петрова"), Employee(name="Борис Смирнов", active=False)])
        s.commit()
        people = s.query(Employee).all()
        rows = [seg("Анна Петрова", 0, 10, speaker_confidence=0.85), seg("Анна Петрова", 10, 20, speaker_confidence=0.6),
                seg("Борис Смирнов", 20, 30, speaker_source="manual", speaker_employee_id=2, speaker_guest=True),
                seg("Анна Петрова", 30, 40, speaker_source="voiceprint", speaker_employee_id=1)]
        assert voice_memory.confirmed(rows, people) == {1: {"how": "context", "rows": [0]}}


def test_remembering_after_who_is_it(client, memory_on, tmp_path):
    employees(consent=True)
    record = tmp_path / "rec.mp3"
    record.write_bytes(b"audio")
    with SessionLocal() as s:
        m = Meeting(title="Планёрка", filename="rec.mp3", source="upload", status="done", duration_sec=40,
                    audio_path=str(record), transcript=[seg("Спикер 1", 0, 12), seg("Спикер 2", 12, 20), seg("Спикер 1", 20, 32)])
        s.add(m)
        s.commit()
        mid = m.id
    response = client.post(f"/meetings/{mid}/speakers", data={"speaker": "Спикер 1", "employee_id": "1"},
                           follow_redirects=False)
    assert response.status_code == 303
    assert [(r.meeting_id, r.how, r.seconds, r.confirmed_by) for r in prints_of(1)] == [(mid, "manual", 24.0, "sysai")]


def test_withdrawing_forgetting_and_deleting_remove_the_prints(client, memory_on, tmp_path):
    anna, boris = employees(consent=True)
    first, second = confirmed_meeting(tmp_path, "Первая"), confirmed_meeting(tmp_path, "Вторая")
    voice_memory.remember(first)
    voice_memory.remember(second)
    assert len(prints_of(anna)) == len(prints_of(boris)) == 2
    deleted = client.post(f"/meetings/{first}/delete", follow_redirects=False)
    assert deleted.status_code == 303, deleted.text[:300]
    assert [r.meeting_id for r in prints_of(anna)] == [second]

    response = client.post(f"/employees/{anna}/voice-memory/forget", follow_redirects=False)
    assert response.status_code == 303 and prints_of(anna) == [] and consent_of(anna) is not None
    voice_memory.remember(second)
    assert len(prints_of(anna)) == 1
    client.post(f"/employees/{anna}/voice-memory", data={"consent": "no"})
    assert consent_of(anna) is None and prints_of(anna) == []
    assert voice_memory.remember(second) == {2: 24.0}  # without the consent she is not remembered again

    with SessionLocal() as s:
        speaker_id.revoke_employee_voice_data(s, boris)
        s.commit()
    assert prints_of(boris) == []
    voice_memory.remember(second)
    with SessionLocal() as s:
        s.query(EmployeeVoiceMemory).update({"expires_at": now() - dt.timedelta(days=1)})
        s.commit()
    assert voice_memory.prints() == {}  # expired prints are not used…
    with SessionLocal() as s:
        speaker_id.purge_expired_samples(s)
        s.commit()
    assert prints_of(boris) == []  # …and are deleted


def test_memory_is_switched_on_in_the_settings(client):
    page = client.get("/settings").text
    assert "Запоминать голоса между встречами" in page and 'name="voice_memory_threshold" value="0,62"' in page
    assert client.post("/settings", data={"present_voice_memory": "1", "voice_memory": "on",
                                          "voice_memory_threshold": "0,7"}).status_code == 200
    assert settings_store.get("voice_memory") is True and settings_store.get("voice_memory_threshold") == 0.7
    assert voice_memory.threshold() == 0.7
    assert client.post("/settings", data={"voice_memory_threshold": "1,2"}).status_code == 400
    client.post("/settings", data={"present_voice_memory": "1"})
    assert settings_store.get("voice_memory") is False and voice_memory.enabled() is False


def test_consent_without_the_switch_waits(client, monkeypatch):
    monkeypatch.setattr(config, "voice_service_enabled", lambda: False)
    anna, _ = employees()
    response = client.post(f"/employees/{anna}/voice-memory", data={"consent": "yes"})
    assert "Голос начнёт запоминаться, когда в настройках включат" in response.text
    assert "Выключено в настройках" in response.text and consent_of(anna) is not None
