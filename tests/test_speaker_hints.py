"""№6: a name from the conversation carries its confidence; below the threshold «Кто это?» offers 2–3 candidates
with the reasons, the moments to listen to and one-click assignment."""
import json

import pytest

from app import llm, settings_store, speakers
from app.db import Employee, Meeting, SessionLocal

MEETING = [
    {"speaker": "Спикер 1", "start": 0.0, "end": 4.0, "text": "Добрый день, это Ольга, начинаем планёрку."},
    {"speaker": "Спикер 1", "start": 4.5, "end": 8.0, "text": "Игорь, что с монтажом на объекте?"},
    {"speaker": "Спикер 2", "start": 8.5, "end": 12.0, "text": "Начинаем со вторника, бригада готова."},
    {"speaker": "Спикер 1", "start": 12.5, "end": 15.0, "text": "Хорошо, спасибо."},
]


@pytest.fixture
def staff():
    with SessionLocal() as s:
        for name, alias, position in (("Ольга Кузнецова", "Оля", "Руководитель проекта"), ("Игорь Соколов", "", "Прораб"),
                                      ("Анна Смирнова", "Аня", "Бухгалтер")):
            s.add(Employee(name=name, aliases=alias, position=position, telegram_chat_id=""))
        s.commit()
        return {e.name: e for e in s.query(Employee).all()}


def _meeting(transcript, **extra) -> int:
    with SessionLocal() as s:
        meeting = Meeting(title="Планёрка", filename="rec.wav", source="web", status="awaiting_approval",
                          transcript=transcript, duration_sec=20, **extra)
        s.add(meeting)
        s.commit()
        return meeting.id


def test_the_name_shows_its_confidence_and_the_unsure_voice_is_asked_about_with_candidates(client, staff):
    from app import pipeline
    mid = _meeting([])
    named = pipeline._name_speakers([dict(seg) for seg in MEETING], None, mid)
    assert [seg["speaker"] for seg in named] == ["Ольга Кузнецова", "Ольга Кузнецова", "Спикер 2", "Ольга Кузнецова"]
    assert named[0]["speaker_confidence"] == pytest.approx(0.85) and "speaker_confidence" not in named[2]
    assert "speaker_employee_id" not in named[0]  # a name from the conversation is not a confirmed voice
    with SessionLocal() as s:
        meeting = s.get(Meeting, mid)
        hints = meeting.options["speaker_hints"]
        assert hints["threshold"] == speakers.DEFAULT_THRESHOLD
        assert hints["named"]["Ольга Кузнецова"]["label"] == "Спикер 1"
        top = hints["ask"]["Спикер 2"][0]
        assert top["name"] == "Игорь Соколов" and top["confidence"] == pytest.approx(0.6)
        assert top["reasons"][0]["seq"] == 2 and top["reasons"][0]["at"] == 4.5
        meeting.transcript = named
        s.commit()
    page = client.get(f"/meetings/{mid}").text
    assert "имя из разговора · 85%" in page and "Уверенность 85%: представился: «Добрый день, это Ольга, начинаем планёрку.»" in page
    assert "Вопрос: кто это?" in page and "Это Игорь Соколов" in page and ">60%<" in page and "Прораб" in page
    assert "назвали по имени, ответил этот голос: «Игорь, что с монтажом на объекте?»" in page
    response = client.post(f"/meetings/{mid}/speakers", data={"speaker": "Спикер 2", "employee_id": str(staff["Игорь Соколов"].id)},
                           follow_redirects=False)
    assert response.status_code == 303
    with SessionLocal() as s:
        assert {seg["speaker"] for seg in s.get(Meeting, mid).transcript} == {"Ольга Кузнецова", "Игорь Соколов"}
    page = client.get(f"/meetings/{mid}").text
    assert "Вопрос: кто это?" not in page and "указан вручную" in page


def test_candidates_follow_the_corrections_and_the_participants(client, staff):
    anna = staff["Анна Смирнова"]
    transcript = [dict(seg) for seg in MEETING] + [
        {"speaker": "Спикер 3", "start": 15.5, "end": 19.0, "text": "Акт сверки отправлю сегодня."}]
    mid = _meeting(transcript)
    page = client.get(f"/meetings/{mid}").text  # processed before the names had candidates: the page finds them itself
    assert "Это Игорь Соколов" in page and ">60%<" in page and "разговор подсказывает, кто это" in page
    stored = {"ask": {"Спикер 3": [{"employee_id": anna.id, "name": anna.name, "confidence": 0.25, "reasons": [
        {"kind": "role", "text": "роль и тема: говорит про акт сверки", "at": 15.5, "start": 15.5, "end": 19.0}]}],
        "Спикер 2": [{"employee_id": staff["Игорь Соколов"].id, "name": "Игорь Соколов", "confidence": 0.45, "reasons": []}]}}
    with SessionLocal() as s:
        s.get(Meeting, mid).options = {"speaker_hints": stored}
        s.commit()
    page = client.get(f"/meetings/{mid}").text  # what the processing found, the model's evidence included, comes first
    assert "Это Анна Смирнова" in page and "роль и тема: говорит про акт сверки" in page and ">25%<" in page
    assert ">45%<" in page and ">60%<" not in page
    with SessionLocal() as s:
        meeting = s.get(Meeting, mid)
        meeting.transcript = [dict(seg, speaker="Анна Смирнова") if seg["speaker"] == "Спикер 3" else seg for seg in transcript]
        meeting.participants = {"people": [{"employee_id": staff["Ольга Кузнецова"].id}, {"employee_id": anna.id}]}
        s.commit()
    page = client.get(f"/meetings/{mid}").text
    assert "Вопрос: кто это?" in page and "Это Анна Смирнова" not in page  # she is already another voice
    assert "Это Игорь Соколов" not in page  # not a participant of this meeting


def test_the_model_reads_names_from_the_whole_long_meeting(staff, monkeypatch):
    segments = [{"speaker": f"Спикер {1 + i % 2}", "start": 3.0 * i, "end": 3.0 * i + 2.5, "text": "Обсуждаем график поставок."}
                for i in range(500)]
    segments[450] = dict(segments[450], text="Анна, что с актом сверки?")
    segments[451] = dict(segments[451], text="Отправила в пятницу.")
    assert speakers._model_lines(segments, list(staff.values())) == [449, 450, 451, 452]
    seen = []
    monkeypatch.setattr(llm, "chat", lambda model, messages, **k: seen.append(messages[1]["content"]) or '{"speakers": []}')
    found = speakers.analyse(segments, list(staff.values()))
    assert "[450] Спикер 1: Анна, что с актом сверки?" in seen[0] and "[10]" not in seen[0]
    assert found["ask"]["Спикер 2"][0]["name"] == "Анна Смирнова"
    assert found["ask"]["Спикер 2"][0]["reasons"][0]["start"] == pytest.approx(1353.0)


def test_a_role_hint_offers_a_candidate_but_never_a_name(staff, monkeypatch):
    segments = [{"speaker": "Спикер 1", "start": 0, "end": 3, "text": "Коллеги, коротко по объекту."},
                {"speaker": "Спикер 2", "start": 3.5, "end": 8, "text": "Монтаж начинаем со вторника, бригада готова."}]
    igor = staff["Игорь Соколов"]
    monkeypatch.setattr(llm, "chat", lambda *a, **k: json.dumps({"speakers": [
        {"label": "Спикер 2", "employee_id": igor.id, "kind": "role", "quote": "Монтаж начинаем со вторника",
         "why": "говорит о монтаже — это прораб"},
        {"label": "Спикер 1", "employee_id": igor.id, "kind": "name", "quote": "Коллеги, коротко по объекту."}]},
        ensure_ascii=False))
    found = speakers.analyse(segments, list(staff.values()), threshold=0.5)
    assert found["mapping"] == {}
    [offer] = found["ask"]["Спикер 2"]
    assert offer["name"] == "Игорь Соколов" and offer["confidence"] == pytest.approx(0.25)
    assert offer["reasons"][0]["text"] == "роль и тема: говорит о монтаже — это прораб"
    assert "Спикер 1" not in found["ask"]  # a «name» whose quote has no name proves nothing


def test_the_threshold_is_set_in_percent(client):
    page = client.get("/settings").text
    assert "Уверенность для имени из разговора, %" in page and 'name="speaker_name_threshold" value="75"' in page
    assert client.post("/settings", data={"speaker_name_threshold": "60"}, follow_redirects=False).status_code == 303
    assert settings_store.get("speaker_name_threshold") == 0.6 and speakers.threshold_of() == 0.6
    assert client.post("/settings", data={"speaker_name_threshold": "40"}).status_code == 400
    assert client.post("/settings", data={"speaker_name_threshold": "много"}).status_code == 400
    assert speakers.threshold_of({"speaker_name_threshold": 80}) == 0.8 and speakers.threshold_of({"speaker_name_threshold": 0.2}) == 0.5
    assert speakers.threshold_of({"speaker_name_threshold": "x"}) == speakers.DEFAULT_THRESHOLD


def test_this_is_anna_who_did_it_is_not_anna_introducing_herself(staff):
    people = list(staff.values())
    about = [{"speaker": "Спикер 1", "start": 0.0, "end": 4.0, "text": "Смета готова? Это Анна делала, она знает."},
             {"speaker": "Спикер 2", "start": 4.5, "end": 8.0, "text": "Алло, это Игорь?"}]
    _, mapping = speakers.name_speakers([dict(seg) for seg in about], people, use_model=False)
    assert mapping == {}
    herself = [{"speaker": "Спикер 1", "start": 0.0, "end": 4.0, "text": "Это Анна Смирнова, бухгалтерия, добрый день."}]
    _, mapping = speakers.name_speakers([dict(seg) for seg in herself], people, use_model=False)
    assert mapping == {"Спикер 1": "Анна Смирнова"}
