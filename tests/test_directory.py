"""The employee directory is used for real: names and nicknames in any case point at the right person."""
from types import SimpleNamespace

import pytest

from app import analyze, pipeline, voice
from app.db import Employee, SessionLocal


def person(i, name, aliases=""):
    return SimpleNamespace(id=i, name=name, aliases=aliases,
                           alias_list=lambda: [a.strip() for a in aliases.split(",") if a.strip()])


TEAM = [person(1, "Вадим Чудин", "Вадя, Чудин"), person(2, "Иван Петров", "Ваня"), person(3, "Анна Орлова")]


@pytest.mark.parametrize("spoken, expected", [
    ("Вадим", 1), ("Вадиму", 1), ("у Вадима", 1), ("Вадимом", 1), ("Вадя", 1), ("Чудину", 1), ("Вадим Чудин", 1),
    ("Ваня", 2), ("Ване", 2), ("Ивану", 2), ("Иван Петров", 2), ("Ивану Петрову", 2),
    ("Анне", 3), ("Анна", 3), ("Орловой", 3), ("Анна (бухгалтер)", 3),
])
def test_a_spoken_name_in_any_case_finds_the_employee(spoken, expected):
    assert analyze.match_employee(spoken, TEAM).id == expected


@pytest.mark.parametrize("spoken", ["", "Сергей", "Вадим Иванов", "Иванов", "Вадислав", "команда", None])
def test_unknown_names_are_not_guessed(spoken):
    assert analyze.match_employee(spoken, TEAM) is None


def test_two_people_with_the_same_first_name_stay_ambiguous():
    twins = [person(1, "Вадим Чудин"), person(2, "Вадим Орлов")]
    assert analyze.match_employee("Вадиму", twins) is None
    assert analyze.match_employee("Вадиму Орлову", twins).id == 2


def test_a_surname_does_not_match_a_longer_surname():
    assert analyze.match_employee("Иван", [person(1, "Сергей Иванов")]) is None


def test_names_ending_in_a_soft_sign_or_y_are_matched():
    staff = [person(1, "Андрей Орлов"), person(2, "Дмитрий Козлов"), person(3, "Ольга Нова")]
    assert [analyze.match_employee(w, staff).id for w in ("Андрею", "Андреем", "Дмитрия", "Дмитрием", "Ольге")] == [1, 1, 2, 2, 3]


def test_the_report_gets_the_employee_even_when_the_model_left_the_id_empty():
    report = {"title": "Планёрка", "summary": "Итог",
              "participants": [{"speaker": "Спикер 1", "name": "Вадим", "employee_id": None, "role": ""}],
              "tasks": [{"title": "Подготовить смету", "employee_id": None, "assignee_name": "Вадиму", "time": "01:00",
                         "quote": "Вадиму подготовить смету"},
                        {"title": "Позвонить клиенту", "employee_id": None, "assignee_name": "кто-нибудь", "time": "02:00",
                         "quote": "кто-нибудь позвонит"},
                        {"title": "Договор", "employee_id": 2, "assignee_name": "Ване", "time": "03:00", "quote": "Ване договор"}]}
    out = analyze.normalize(report, TEAM)
    assert [t["employee_id"] for t in out["tasks"]] == [1, None, 2]
    assert [t["assignee_name"] for t in out["tasks"]] == ["Вадиму", "кто-нибудь", "Ване"]  # as spoken
    assert out["participants"][0]["employee_id"] == 1


def test_a_wrong_id_from_the_model_is_replaced_by_the_spoken_name():
    out = analyze.normalize({"tasks": [{"title": "Смета", "employee_id": 99, "assignee_name": "Анне"}]}, TEAM)
    assert out["tasks"][0]["employee_id"] == 3


def test_the_analyst_is_told_that_the_id_is_required_for_a_named_person():
    assert "employee_id" in analyze.SYSTEM and "в любом падеже" in analyze.SYSTEM and "обязателен" in analyze.SYSTEM


def test_employee_assignment_profile_is_included_in_analysis_directory():
    specialist = SimpleNamespace(
        id=7, name="Татьяна Смирнова", aliases="", position="Специалист по закупкам",
        department="Технический отдел", competencies="промышленные насосы, закупка оборудования",
        responsibilities="подбор поставщиков и закупка запчастей", context="согласует закупки с главным инженером",
        alias_list=lambda: [],
    )

    import json
    profile = json.loads(analyze.employees_json([specialist]))[0]
    assert profile["department"] == "Технический отдел"
    assert "промышленные насосы" in profile["competencies"]
    assert "закупка запчастей" in profile["responsibilities"]
    assert "главным инженером" in profile["context"]
    assert "подразделение, компетенции и обязанности" in analyze.SYSTEM


def test_directory_names_help_the_recogniser_and_the_analyst():
    with SessionLocal() as s:
        s.add_all([Employee(name="Вадим Чудин", aliases="Вадя, Чудин"), Employee(name="Иван Петров", aliases="Вадя"),
                   Employee(name="Бывший Сотрудник", active=False)])
        s.commit()
    assert pipeline._directory_terms() == ["Вадим Чудин", "Вадя", "Чудин", "Иван Петров"]


# ----------------------------------------------------------------------------- the first message is not an introduction
def test_the_persona_does_not_announce_itself_but_answers_honestly_when_asked():
    system = voice.system_prompt("Задача.")
    assert "Не представляйся" in system and "что ты ИИ, ассистент или бот" in system
    assert "не утверждай, что ты человек" in system and "ИИ-ассистент" in system  # only as an honest answer to a direct question
    assert "Ты ИИ-ассистент руководителя" not in system
