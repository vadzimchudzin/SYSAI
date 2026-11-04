"""«Переразметить голоса»: an old meeting with a kept recording is labelled again; only the speakers change, the
manual corrections stay, and nothing is saved before «было → стало» is checked."""
import datetime as dt
import math

from app import diarize, voice_relabel
from app.db import Employee, Meeting, SessionLocal

TEXT = "обсуждаем план работ на эту неделю подробно"


def vec(*weights):
    raw = list(weights) + [0.0] * (192 - len(weights))
    norm = math.sqrt(sum(x * x for x in raw))
    return [x / norm for x in raw]


VOICES = {"Спикер 1": vec(1, 0, 0), "Спикер 2": vec(0, 1, 0), "Спикер 3": vec(0.75, 0, 0.66),  # ≈ 0.75 like Спикер 1
          "Борис Смирнов": vec(0, 1, 0.1)}


def seg(speaker, start, end, text=TEXT, **extra):
    return dict(speaker=speaker, start=start, end=end, text=text, **extra)


def transcript():
    return [seg("Спикер 1", 0, 30), seg("Спикер 2", 30, 60), seg("Спикер 3", 61, 90),
            seg("Борис Смирнов", 90, 120, speaker_source="manual", speaker_employee_id=2, speaker_assigned_by="sysai",
                speaker_auto={"speaker": "Спикер 2"})]


AUTO = [seg("Спикер 1", 0, 30), seg("Спикер 2", 30, 60), seg("Спикер 3", 61, 90), seg("Спикер 2", 90, 120)]


def fake_voice(monkeypatch, calls=None):
    monkeypatch.setattr(diarize, "unavailable", lambda name: "")

    def groups(path, segments, total, progress=None):
        if calls is not None:
            calls.append([s["speaker"] for s in segments])
        if progress:
            progress("Слушаю голоса")
        spoken = {}
        for s in segments:
            key = (int(s["start"] // diarize.VOICE_WINDOW), s["speaker"])
            spoken[key] = spoken.get(key, 0.0) + s["end"] - s["start"]
        return [{"window": w, "label": label, "embedding": VOICES[label], "seconds": seconds, "model": "test"}
                for (w, label), seconds in spoken.items()]
    monkeypatch.setattr(diarize, "voice_groups", groups)


def meeting(tmp_path, audio=True, **extra):
    record = tmp_path / "meeting.mp3"
    if audio:
        record.write_bytes(b"audio")
    with SessionLocal() as s:
        s.add_all([Employee(name="Анна Петрова"), Employee(name="Борис Смирнов")])
        m = Meeting(title="Планёрка", filename="rec.mp3", source="upload", status="awaiting_approval",
                    transcript=transcript(), transcript_auto=AUTO, duration_sec=120,
                    audio_path=str(record) if audio else "", options={"diarization_method": "voice"}, **extra)
        s.add(m)
        s.commit()
        return m.id


def load(mid):
    with SessionLocal() as s:
        m = s.get(Meeting, mid)
        _ = m.transcript, m.transcript_auto, m.options
        s.expunge(m)
        return m


def test_relabelling_shows_before_and_after_and_saves_only_the_speakers(client, monkeypatch, tmp_path):
    calls = []
    fake_voice(monkeypatch, calls)
    mid = meeting(tmp_path)
    page = client.get(f"/meetings/{mid}").text
    assert "Переразметить голоса" in page and "«было → стало»" in page

    response = client.post(f"/meetings/{mid}/speakers/relabel", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == f"/meetings/{mid}?relabel=started#voice-relabel"
    assert calls == [["Спикер 1", "Спикер 2", "Спикер 3", "Борис Смирнов"]]
    m = load(mid)
    assert m.transcript == transcript()  # nothing is saved before the decision
    data = m.options["relabel"]
    assert data["state"] == "ready" and data["method"] == "voice"
    assert data["summary"]["changed"] == 1 and data["summary"]["manual"] == 1
    assert data["summary"]["before"] == 4 and data["summary"]["after"] == 3
    assert data["pairs"] == [{"before": "Спикер 3", "after": "Спикер 1", "count": 1}]
    assert client.get(f"/meetings/{mid}/speakers/relabel/status").json() == {"state": "ready", "progress": ""}

    page = client.get(f"/meetings/{mid}?relabel=started").text
    assert "Переразметка голосов запущена" in page and "Сохранить новую разметку" in page
    assert "говорящий сменится у <b>1</b> из 4 реплик" in page and "голосов было 4, станет 3" in page
    assert "Ручные исправления (1 реплика) не тронуты" in page and "Спикер 3 → <b>Спикер 1</b>" in page

    response = client.post(f"/meetings/{mid}/speakers/relabel/save", follow_redirects=False)
    assert response.headers["location"] == f"/meetings/{mid}?relabel=saved&changed=1#voice-diagram"
    m = load(mid)
    assert [x["speaker"] for x in m.transcript] == ["Спикер 1", "Спикер 2", "Спикер 1", "Борис Смирнов"]
    assert m.transcript[3]["speaker_source"] == "manual" and m.transcript[3]["speaker_employee_id"] == 2
    assert m.transcript[3]["speaker_auto"] == {"speaker": "Спикер 2"}
    assert [x["text"] for x in m.transcript] == [TEXT] * 4 and [x["start"] for x in m.transcript] == [0, 30, 61, 90]
    assert [x["speaker"] for x in m.transcript_auto] == ["Спикер 1", "Спикер 2", "Спикер 1", "Спикер 2"]
    assert m.options["relabel"]["state"] == "saved"
    page = client.get(f"/meetings/{mid}?relabel=saved&changed=1").text
    assert "Новая разметка голосов сохранена (1 реплик" in page and "Вернуть прежнюю разметку" in page

    response = client.post(f"/meetings/{mid}/speakers/relabel/revert", follow_redirects=False)
    assert response.headers["location"] == f"/meetings/{mid}?relabel=reverted&changed=1#voice-diagram"
    m = load(mid)
    assert m.transcript == transcript() and [x["speaker"] for x in m.transcript_auto] == [x["speaker"] for x in AUTO]
    assert "relabel" not in m.options
    assert client.post(f"/meetings/{mid}/speakers/relabel/revert").status_code == 409


def test_a_labelling_made_before_an_edit_cannot_be_saved(client, monkeypatch, tmp_path):
    fake_voice(monkeypatch)
    mid = meeting(tmp_path)
    client.post(f"/meetings/{mid}/speakers/relabel")
    with SessionLocal() as s:
        m = s.get(Meeting, mid)
        changed = transcript()
        changed[0]["text"] = "другой текст реплики после правки"
        m.transcript = changed
        s.commit()
    page = client.get(f"/meetings/{mid}").text
    assert "эту разметку уже не сохранить" in page and "Переразметить заново" in page
    assert client.post(f"/meetings/{mid}/speakers/relabel/save").status_code == 409
    assert load(mid).transcript[2]["speaker"] == "Спикер 3"
    response = client.post(f"/meetings/{mid}/speakers/relabel/close", follow_redirects=False)
    assert response.status_code == 303 and "relabel" not in load(mid).options


def test_the_same_labelling_has_nothing_to_save_and_cancel_hides_it(client, monkeypatch, tmp_path):
    fake_voice(monkeypatch)
    VOICES_SAME = dict(VOICES, **{"Спикер 3": vec(0, 0, 1)})  # a voice of its own: nothing changes
    monkeypatch.setitem(globals(), "VOICES", VOICES_SAME)
    mid = meeting(tmp_path)
    client.post(f"/meetings/{mid}/speakers/relabel")
    page = client.get(f"/meetings/{mid}").text
    assert "разметил голоса так же, как сейчас" in page and "Сохранить новую разметку" not in page
    client.post(f"/meetings/{mid}/speakers/relabel/close")
    assert "relabel" not in load(mid).options


def test_without_a_recording_or_a_way_there_is_no_relabelling(client, monkeypatch, tmp_path):
    monkeypatch.setattr(diarize, "unavailable", lambda name: "")
    mid = meeting(tmp_path, audio=False)
    assert "Переразметить голоса" not in client.get(f"/meetings/{mid}").text
    response = client.post(f"/meetings/{mid}/speakers/relabel")
    assert response.status_code == 409 and "Записи этого совещания нет" in response.text

    other = meeting(tmp_path)
    monkeypatch.setattr(diarize, "unavailable", lambda name: "нет")
    assert voice_relabel.way({"diarization_method": "voice"}) == ""
    assert "Нужен свой сервис голосов" in voice_relabel.unavailable(load(other))
    monkeypatch.setattr(diarize, "unavailable", lambda name: "" if name == "gemini" else "нет")
    assert voice_relabel.way({"diarization_method": "voice"}) == "gemini"
    assert voice_relabel.way({"diarization_method": "chunks"}) == "gemini"


def test_a_failed_or_lost_run_says_so_and_can_be_closed(client, monkeypatch, tmp_path):
    monkeypatch.setattr(diarize, "unavailable", lambda name: "")

    def broken(*args, **kwargs):
        raise diarize.DiarizationError("Сервис голосов не ответил.")
    monkeypatch.setattr(diarize, "voice_groups", broken)
    mid = meeting(tmp_path)
    client.post(f"/meetings/{mid}/speakers/relabel")
    m = load(mid)
    assert m.options["relabel"]["state"] == "error" and m.transcript == transcript()
    page = client.get(f"/meetings/{mid}").text
    assert "Переразметка не удалась: Сервис голосов не ответил." in page and "Попробовать ещё раз" in page

    with SessionLocal() as s:  # the server restarted in the middle of a run
        m = s.get(Meeting, mid)
        old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2)).isoformat(timespec="seconds")
        m.options = dict(m.options, relabel={"state": "running", "at": old, "method": "voice"})
        s.commit()
    assert client.get(f"/meetings/{mid}/speakers/relabel/status").json()["state"] == "error"
    assert "сервер перезапускался" in client.get(f"/meetings/{mid}").text
    with SessionLocal() as s:
        m = s.get(Meeting, mid)
        now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        m.options = dict(m.options, relabel={"state": "running", "at": now, "method": "voice", "progress": "Слушаю"})
        s.commit()
    page = client.get(f"/meetings/{mid}").text
    assert "data-vd-relabel-running" in page and "Слушаю" in page
    assert client.post(f"/meetings/{mid}/speakers/relabel").status_code == 409  # one run at a time
    assert client.post(f"/meetings/{mid}/speakers/relabel/close").status_code == 409


def test_an_anonymous_manual_voice_is_not_named_by_the_conversation(monkeypatch, tmp_path):
    fake_voice(monkeypatch)
    rows = [seg("Спикер 1", 0, 30, text="Меня зовут Анна Петрова, я веду встречу"), seg("Спикер 2", 30, 60),
            seg("Спикер 3", 61, 90), seg("Спикер 2", 90, 120, speaker_source="manual", speaker_auto={"speaker": "Спикер 2"})]
    with SessionLocal() as s:
        s.add_all([Employee(name="Анна Петрова"), Employee(name="Борис Смирнов")])
        s.commit()
        employees = s.query(Employee).all()
        s.expunge_all()
    proposal = voice_relabel.propose(rows, str(tmp_path / "x.mp3"), "voice", {"diarization_voice_threshold": 0.5},
                                     120, employees)
    after = voice_relabel.patched(rows, proposal["patches"])
    assert after[0]["speaker"] == after[2]["speaker"] == "Анна Петрова"  # one person again, named by the introduction
    assert after[3]["speaker"] == "Спикер 2" and after[3]["speaker_source"] == "manual"  # kept unnamed by a person
    assert proposal["summary"]["manual"] == 1 and proposal["summary"]["named"] == 1
