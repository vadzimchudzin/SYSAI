"""The reference («эталон») and the accuracy of the automatic speaker labelling measured by it."""
import pytest

from app import diarization_eval, transcript_edit
from app.db import Meeting, SessionLocal


def seg(speaker, start, end, text="обсуждаем план работ на неделю", **extra):
    return dict(speaker=speaker, start=start, end=end, text=text, **extra)


REFERENCE = [seg("Анна Петрова", 0, 30), seg("Борис Смирнов", 30, 60), seg("Анна Петрова", 60, 90),
             seg("Борис Смирнов", 90, 91.5, "да"), seg("Виктор Иванов", 91.5, 120)]


def test_a_perfect_labelling_is_right_whatever_the_labels_are_called():
    auto = [dict(s, speaker={"Анна Петрова": "Спикер 1", "Борис Смирнов": "Спикер 2", "Виктор Иванов": "Спикер 3"}[s["speaker"]])
            for s in REFERENCE]
    result = diarization_eval.evaluate(REFERENCE, auto)
    assert result["accuracy"] == 1.0 and result["extra"] == 0 and result["merges"] == []
    assert result["short"] == {"total": 1, "right": 1, "accuracy": 1.0}
    assert result["names"]["precision"] is None and result["names"]["recall"] == 0.0
    assert result["ref_speakers"] == result["hyp_speakers"] == 3


def test_one_person_split_in_two_voices_is_an_extra_voice():
    auto = [seg("Спикер 1", 0, 30), seg("Спикер 2", 30, 60), seg("Спикер 4", 60, 90), seg("Спикер 2", 90, 91.5, "да"),
            seg("Спикер 3", 91.5, 120)]
    result = diarization_eval.evaluate(REFERENCE, auto)
    assert result["extra"] == 1 and result["merges"] == []
    assert result["split"] == [{"person": "Анна Петрова", "labels": [["Спикер 1", 30.0], ["Спикер 4", 30.0]]}]
    assert result["accuracy"] == pytest.approx(0.75)


def test_two_people_in_one_voice_is_a_merge_and_short_replicas_follow_the_labels():
    auto = [seg("Спикер 1", 0, 30), seg("Спикер 2", 30, 60), seg("Спикер 1", 60, 91.5), seg("Спикер 2", 91.5, 120)]
    result = diarization_eval.evaluate(REFERENCE, auto)
    assert [m["label"] for m in result["merges"]] == ["Спикер 2"]
    assert result["merges"][0]["people"] == [["Борис Смирнов", 30.0], ["Виктор Иванов", 28.5]]
    assert result["short"] == {"total": 1, "right": 0, "accuracy": 0.0}  # «да» went to the previous voice
    assert result["accuracy"] == pytest.approx((30 + 30 + 30) / 120, abs=0.01)


def test_names_count_only_where_the_labelling_put_a_name():
    auto = [seg("Анна Петрова", 0, 30), seg("Анна Петрова", 30, 60), seg("Спикер 1", 60, 90),
            seg("Спикер 2", 90, 91.5, "да"), seg("Виктор Иванов", 91.5, 120)]
    names = diarization_eval.evaluate(REFERENCE, auto)["names"]
    assert names["precision"] == pytest.approx((30 + 28.5) / (60 + 28.5), abs=0.001)
    assert names["named_seconds"] == 88.5 and names["recall"] == pytest.approx(58.5 / 120, abs=0.001)


def test_guests_in_the_reference_are_not_names_to_find():
    reference = REFERENCE + [seg("Пётр (Альфа)", 120, 150, speaker_guest=True)]
    auto = REFERENCE + [seg("Спикер 7", 120, 150)]
    assert diarization_eval.evaluate(reference, auto)["names"]["recall"] == 1.0


def test_totals_weigh_meetings_by_their_speech():
    short = diarization_eval.evaluate([seg("А", 0, 10)], [seg("Спикер 1", 0, 5), seg("Спикер 2", 5, 10)])
    long = diarization_eval.evaluate(REFERENCE, REFERENCE)
    total = diarization_eval.combine([short, long])
    assert total["references"] == 2 and total["extra"] == 1
    assert total["accuracy"] == pytest.approx((5 + 120) / 130, abs=0.001)
    assert diarization_eval.combine([]) is None


def add_meeting(transcript):
    with SessionLocal() as s:
        meeting = Meeting(title="Планёрка", status="awaiting_approval", source="web", transcript=transcript, duration_sec=120)
        s.add(meeting)
        s.commit()
        return meeting.id


def reference_of(mid):
    with SessionLocal() as s:
        return s.get(Meeting, mid).diarization_reference


def test_the_reference_freezes_the_corrections_and_the_automatic_labelling(client):
    auto = [seg("Спикер 1", 0, 30), seg("Спикер 2", 30, 60), seg("Спикер 1", 60, 91.5), seg("Спикер 2", 91.5, 120)]
    mid = add_meeting(auto)
    html = client.get(f"/meetings/{mid}").text
    assert "Эталон для замера точности" in html and "Сохранить как эталон" in html
    client.post(f"/meetings/{mid}/segments/2/split",
                data={"sig": transcript_edit.signature(auto[2]), "at": str(len("обсуждаем план работ на ")), "who": "label:Спикер 2"})
    response = client.post(f"/meetings/{mid}/reference", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == f"/meetings/{mid}?ref=saved#diarization-reference"
    saved = reference_of(mid)
    assert saved["auto"] == auto and len(saved["transcript"]) == 5 and saved["by"]
    page = client.get(response.headers["location"]).text
    assert "Эталон сохранён" in page and "Обновить эталон" in page and "Говорящий верный" in page
    overview = client.get("/diarization").text
    assert "Планёрка" in overview and "В среднем по 1 эталону" in overview
    client.post(f"/meetings/{mid}/transcript/undo")
    assert "Расшифровка изменилась после сохранения эталона" in client.get(f"/meetings/{mid}").text
    client.post(f"/meetings/{mid}/reference/delete")
    assert reference_of(mid) is None and "Эталонов пока нет" in client.get("/diarization").text


def test_a_reference_needs_timed_replicas_and_a_superuser(client):
    empty = add_meeting([dict(speaker="Спикер 1", text="без времени")])
    assert client.post(f"/meetings/{empty}/reference").status_code == 400
    from fastapi.testclient import TestClient
    from app.main import app
    mid = add_meeting(REFERENCE)
    with TestClient(app) as anonymous:
        assert anonymous.post(f"/meetings/{mid}/reference", follow_redirects=False).status_code == 303
        assert anonymous.get("/diarization", follow_redirects=False).status_code == 303
    assert reference_of(mid) is None
