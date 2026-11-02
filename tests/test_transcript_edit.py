"""Corrections of one replica: another speaker only for it, a split in two, a merge with a neighbour, and undo."""
import re

import pytest

from app import transcript_edit
from app.db import Employee, Meeting, SessionLocal


def seg(speaker, start, end, text, **extra):
    return dict(speaker=speaker, start=start, end=end, text=text, **extra)


TRANSCRIPT = [
    seg("Анна Петрова", 0.0, 10.0, "Доброе утро, начинаем планёрку по объекту."),
    seg("Спикер 2", 10.0, 20.0, "По Кринице договор подписан. Да, и акт тоже готов."),
    seg("Спикер 2", 20.0, 24.0, "Согласен."),
    seg("Спикер 3", 24.0, 30.0, "Тогда отправим акт сегодня."),
]


def add_meeting(transcript=TRANSCRIPT, status="awaiting_approval"):
    with SessionLocal() as s:
        meeting = Meeting(title="Планёрка", status=status, source="web", transcript=transcript, duration_sec=30)
        s.add(meeting)
        s.commit()
        return meeting.id


def add_employee(name):
    with SessionLocal() as s:
        employee = Employee(name=name, active=True)
        s.add(employee)
        s.commit()
        return employee.id


def saved(mid):
    with SessionLocal() as s:
        meeting = s.get(Meeting, mid)
        return meeting.transcript, meeting.transcript_auto, meeting.transcript_edits


def sig(item):
    return transcript_edit.signature(item)


# ------------------------------------------------------------------------------------------------------ the module
def test_one_replica_gets_another_speaker_and_the_others_stay():
    result, entry = transcript_edit.set_speaker(TRANSCRIPT, 2, sig(TRANSCRIPT[2]), ("Анна Петрова", None, False), by="sysai")
    assert [s["speaker"] for s in result] == ["Анна Петрова", "Спикер 2", "Анна Петрова", "Спикер 3"]
    assert result[2]["speaker_source"] == "manual" and result[2]["speaker_auto"] == {"speaker": "Спикер 2"}
    assert result[1] == TRANSCRIPT[1] and entry["op"] == "speaker" and entry["patches"] == [[2, 1, [TRANSCRIPT[2]]]]
    assert "Спикер 2 → Анна Петрова" in entry["summary"]


def test_a_stale_form_does_not_change_another_replica():
    with pytest.raises(transcript_edit.Stale):
        transcript_edit.set_speaker(TRANSCRIPT, 2, sig(TRANSCRIPT[1]), ("Анна Петрова", None, False))
    with pytest.raises(transcript_edit.EditError):
        transcript_edit.set_speaker(TRANSCRIPT, 9, "x", ("Анна Петрова", None, False))


def test_split_before_a_word_estimates_the_time_by_the_text():
    text = TRANSCRIPT[1]["text"]
    at = text.index("Да,")
    result, entry = transcript_edit.split(TRANSCRIPT, 1, sig(TRANSCRIPT[1]), at, ("Спикер 3", None, False), by="sysai")
    head, tail = result[1], result[2]
    assert head["text"] == "По Кринице договор подписан." and tail["text"] == "Да, и акт тоже готов."
    assert head["start"] == 10.0 and tail["end"] == 20.0 and 10.0 < head["end"] == tail["start"] < 20.0
    assert head["speaker"] == "Спикер 2" and tail["speaker"] == "Спикер 3" and tail["speaker_source"] == "manual"
    assert len(result) == len(TRANSCRIPT) + 1 and result[3] == TRANSCRIPT[2]
    assert entry["patches"] == [[1, 2, [TRANSCRIPT[1]]]]
    for wrong in (0, at + 1, len(text), "x"):
        with pytest.raises(transcript_edit.EditError):
            transcript_edit.split(TRANSCRIPT, 1, sig(TRANSCRIPT[1]), wrong)


def test_merge_keeps_the_speaker_of_the_replica_that_takes_the_text():
    joined, _ = transcript_edit.merge(TRANSCRIPT, 3, sig(TRANSCRIPT[3]), "prev", sig(TRANSCRIPT[2]))
    assert joined[2]["text"] == "Согласен. Тогда отправим акт сегодня." and joined[2]["speaker"] == "Спикер 2"
    assert joined[2]["start"] == 20.0 and joined[2]["end"] == 30.0 and len(joined) == 3
    taken, entry = transcript_edit.merge(TRANSCRIPT, 2, sig(TRANSCRIPT[2]), "next", sig(TRANSCRIPT[3]))
    assert taken == joined and entry["seq"] == 2
    with pytest.raises(transcript_edit.Stale):
        transcript_edit.merge(TRANSCRIPT, 2, sig(TRANSCRIPT[2]), "next", sig(TRANSCRIPT[1]))
    with pytest.raises(transcript_edit.EditError):
        transcript_edit.merge(TRANSCRIPT, 0, sig(TRANSCRIPT[0]), "prev", "")


class Row:
    transcript_auto = None
    transcript_edits = None

    def __init__(self, transcript):
        self.transcript = transcript


def test_corrections_are_undone_newest_first_back_to_the_automatic_transcript():
    row = Row(list(TRANSCRIPT))
    first, entry = transcript_edit.set_speaker(row.transcript, 2, sig(row.transcript[2]), ("Анна Петрова", None, False))
    transcript_edit.record(row, first, entry)
    second, entry = transcript_edit.split(row.transcript, 1, sig(row.transcript[1]), row.transcript[1]["text"].index("Да,"))
    transcript_edit.record(row, second, entry)
    assert row.transcript_auto == TRANSCRIPT and len(row.transcript) == 5
    assert transcript_edit.history(row)["count"] == 2 and transcript_edit.history(row)["undoable"]
    transcript_edit.undo(row)
    assert row.transcript == first
    transcript_edit.undo(row)
    assert row.transcript == TRANSCRIPT and row.transcript_edits is None
    with pytest.raises(transcript_edit.EditError):
        transcript_edit.undo(row)


def test_undo_never_applies_to_a_replaced_transcript():
    row = Row(list(TRANSCRIPT))
    changed, entry = transcript_edit.set_speaker(row.transcript, 0, sig(row.transcript[0]), ("Спикер 9", None, False))
    transcript_edit.record(row, changed, entry)
    row.transcript = list(TRANSCRIPT[:2])  # a new transcription
    assert not transcript_edit.history(row)["undoable"]
    with pytest.raises(transcript_edit.Stale):
        transcript_edit.undo(row)
    assert row.transcript == TRANSCRIPT[:2]


def test_the_log_is_bounded():
    row = Row(list(TRANSCRIPT))
    for number in range(transcript_edit.HISTORY_LIMIT + 5):
        changed, entry = transcript_edit.set_speaker(row.transcript, 0, sig(row.transcript[0]), (f"Имя {number}", None, False))
        transcript_edit.record(row, changed, entry)
    assert len(row.transcript_edits["entries"]) == transcript_edit.HISTORY_LIMIT
    assert row.transcript_auto == TRANSCRIPT


# --------------------------------------------------------------------------------------------------------- the page
def test_every_replica_has_an_editor_and_it_offers_people_and_a_new_voice(client):
    boris = add_employee("Борис Смирнов")
    mid = add_meeting()
    html = client.get(f"/meetings/{mid}").text
    assert len(re.findall(r'data-seg-edit="\d+"', html)) == len(TRANSCRIPT)
    assert "transcript-edit.js" in html and "Отменить последнюю правку" not in html
    fragment = client.get(f"/meetings/{mid}/segments/1?fragment=1")
    assert fragment.status_code == 200 and "<html" not in fragment.text
    assert 'value="label:Спикер 3"' in fragment.text and f'value="employee:{boris}"' in fragment.text
    assert "Новый говорящий — Спикер 4" in fragment.text and 'name="at"' in fragment.text
    assert "Присоединить к предыдущей" in fragment.text and "Присоединить следующую" in fragment.text
    full = client.get(f"/meetings/{mid}/segments/1")
    assert full.status_code == 200 and "Правка реплики" in full.text and "<html" in full.text
    assert client.get(f"/meetings/{mid}/segments/99").status_code == 404
    assert client.get(f"/meetings/{mid}/segments/x").status_code == 404


def test_changing_one_replica_through_the_page_and_undoing_it(client):
    boris = add_employee("Борис Смирнов")
    mid = add_meeting()
    response = client.post(f"/meetings/{mid}/segments/2/speaker", data={"sig": sig(TRANSCRIPT[2]), "who": f"employee:{boris}"},
                           follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == f"/meetings/{mid}?seg=speaker#seg-2"
    transcript, auto, edits = saved(mid)
    assert [s["speaker"] for s in transcript] == ["Анна Петрова", "Спикер 2", "Борис Смирнов", "Спикер 3"]
    assert transcript[2]["speaker_employee_id"] == boris and auto == TRANSCRIPT and len(edits["entries"]) == 1
    html = client.get(response.headers["location"]).text
    assert "говорящий изменён только у этой реплики" in html and "Отменить последнюю правку" in html
    assert "История правок · 1" in html and 'id="meeting-transcript" open' in html
    stale = client.post(f"/meetings/{mid}/segments/2/speaker", data={"sig": sig(TRANSCRIPT[2]), "who": "new"})
    assert stale.status_code == 409
    undone = client.post(f"/meetings/{mid}/transcript/undo", follow_redirects=False)
    assert undone.headers["location"] == f"/meetings/{mid}?seg=undo#seg-2"
    assert saved(mid)[0] == TRANSCRIPT
    assert client.post(f"/meetings/{mid}/transcript/undo").status_code == 400


def test_split_and_merge_through_the_page(client):
    mid = add_meeting()
    at = TRANSCRIPT[1]["text"].index("Да,")
    client.post(f"/meetings/{mid}/segments/1/split", data={"sig": sig(TRANSCRIPT[1]), "at": str(at), "who": "new"})
    transcript = saved(mid)[0]
    assert [s["speaker"] for s in transcript] == ["Анна Петрова", "Спикер 2", "Спикер 4", "Спикер 2", "Спикер 3"]
    response = client.post(f"/meetings/{mid}/segments/3/merge",
                           data={"sig": sig(transcript[3]), "with": "prev", "neighbour_sig": sig(transcript[2])},
                           follow_redirects=False)
    assert response.headers["location"] == f"/meetings/{mid}?seg=merge#seg-2"
    transcript = saved(mid)[0]
    assert transcript[2]["text"] == "Да, и акт тоже готов. Согласен." and transcript[2]["speaker"] == "Спикер 4"
    same = client.post(f"/meetings/{mid}/segments/0/split", data={"sig": sig(transcript[0]), "at": "2", "who": "same"})
    assert same.status_code == 400  # not the start of a word
    client.post(f"/meetings/{mid}/transcript/undo")
    client.post(f"/meetings/{mid}/transcript/undo")
    assert saved(mid)[0] == TRANSCRIPT


def test_who_is_this_is_logged_and_reset_brings_back_the_processing_result(client):
    boris = add_employee("Борис Смирнов")
    mid = add_meeting()
    client.post(f"/meetings/{mid}/segments/1/split", data={"sig": sig(TRANSCRIPT[1]), "at": str(TRANSCRIPT[1]["text"].index("Да,")),
                                                           "who": "same"})
    client.post(f"/meetings/{mid}/speakers", data={"speaker": "Спикер 2", "employee_id": str(boris)})
    transcript, auto, edits = saved(mid)
    assert [s["speaker"] for s in transcript].count("Борис Смирнов") == 3 and len(edits["entries"]) == 2
    client.post(f"/meetings/{mid}/transcript/undo")
    assert [s["speaker"] for s in saved(mid)[0]].count("Спикер 2") == 3
    client.post(f"/meetings/{mid}/speakers", data={"speaker": "Спикер 2", "employee_id": "guest"})
    response = client.post(f"/meetings/{mid}/speakers/reset", follow_redirects=False)
    assert response.status_code == 303
    assert saved(mid) == (TRANSCRIPT, None, None)


def test_corrections_are_refused_while_transcribing_and_need_a_signed_in_user(client):
    busy = add_meeting(status="transcribing")
    assert client.get(f"/meetings/{busy}/segments/0").status_code == 409
    assert client.post(f"/meetings/{busy}/segments/0/speaker", data={"sig": sig(TRANSCRIPT[0]), "who": "new"}).status_code == 409
    from fastapi.testclient import TestClient
    from app.main import app
    mid = add_meeting()
    with TestClient(app) as anonymous:
        for url in (f"/meetings/{mid}/segments/0/speaker", f"/meetings/{mid}/segments/0/split",
                    f"/meetings/{mid}/segments/0/merge", f"/meetings/{mid}/transcript/undo"):
            response = anonymous.post(url, data={"sig": sig(TRANSCRIPT[0]), "who": "new"}, follow_redirects=False)
            assert response.status_code == 303 and response.headers["location"] == "/login"
        assert anonymous.get(f"/meetings/{mid}/segments/0", follow_redirects=False).status_code == 303
    assert saved(mid)[0] == TRANSCRIPT
