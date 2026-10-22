"""Sprint 1: the router is applied to every analysed task before it is saved and delivered."""
import datetime as dt
from types import SimpleNamespace

from app import analyze, task_routing


def person(i, name, chat="", position="", responsibilities="", aliases=""):
    return SimpleNamespace(id=i, name=name, aliases=aliases, position=position, responsibilities=responsibilities,
                           telegram_chat_id=chat, alias_list=lambda: [a.strip() for a in aliases.split(",") if a.strip()])


VADIM = person(1, "Вадим Чудин", "111", "Директор")
ANNA = person(2, "Анна Смирнова", "222", "Бухгалтер", aliases="Аня")
PETR = person(3, "Пётр Иванов", "333", "Юрист", aliases="Петя")
TEAM = [VADIM, ANNA, PETR]


def task(title, quote, **extra):
    base = {"title": title, "description": "", "quote": quote, "time": "00:10", "employee_id": None,
            "assignee_name": "", "executor": "person", "target_employee_id": None, "target_employee_name": None,
            "deadline": None, "deadline_quote": "", "priority": "medium"}
    base.update(extra)
    return base


def normalize(*tasks, participants=None):
    return analyze.normalize({"title": "Планёрка", "summary": "", "participants": participants or [], "topics": [],
                              "decisions": [], "tasks": list(tasks)}, TEAM, dt.date(2026, 10, 22))


def test_model_mistake_write_to_vadim_becomes_assistant_task_with_vadim_as_recipient():
    out = normalize(task("Написать Вадиму и узнать статус договора", "Нужно написать Вадиму и узнать статус договора",
                         employee_id=1, assignee_name="Вадим"))
    t = out["tasks"][0]
    assert t["executor"] == "assistant" and t["employee_id"] is None
    assert t["target_employee_id"] == 1
    assert t["action_type"] in ("ask", "message")
    assert any(c["employee_id"] == 1 for c in t["counterparts"])
    assert t["routing_note"]


def test_take_act_from_anna_and_send_to_petya_keeps_both_counterparts():
    out = normalize(task("Взять у Ани акт и отправить Пете", "Надо взять у Ани акт и отправить Пете",
                         employee_id=2, assignee_name="Аня"))
    t = out["tasks"][0]
    assert t["executor"] == "assistant"
    roles = {c["role"]: c["employee_id"] for c in t["counterparts"]}
    assert roles.get("source") == 2 and roles.get("recipient") == 3
    assert t["target_employee_id"] == 2  # first contact is the one who has the document
    assert "Анна" in task_routing.describe(t["action_type"], t["counterparts"]) or "Аня" in task_routing.describe(
        t["action_type"], t["counterparts"])


def test_direct_address_stays_person_task():
    out = normalize(task("Написать Петру про договор", "Вадим, напиши Петру про договор", employee_id=1,
                         assignee_name="Вадим"))
    t = out["tasks"][0]
    assert t["executor"] == "person" and t["employee_id"] == 1


def test_substantive_work_stays_with_named_person():
    out = normalize(task("Подготовить смету", "Пусть Петя подготовит смету к пятнице", employee_id=3,
                         assignee_name="Петя"))
    t = out["tasks"][0]
    assert t["executor"] == "person" and t["employee_id"] == 3 and not t["routing_note"]


def test_speaker_label_is_replaced_by_identified_participant():
    out = normalize(task("Подготовить отчёт", "Я подготовлю отчёт", assignee_name="Спикер 2"),
                    participants=[{"name": "Анна Смирнова", "speaker": "Спикер 2", "employee_id": 2}])
    t = out["tasks"][0]
    assert t["assignee_name"] != "Спикер 2"
    assert t["employee_id"] == 2


def test_unidentified_speaker_label_never_becomes_assignee_name():
    out = normalize(task("Подготовить отчёт", "Я подготовлю отчёт", assignee_name="Спикер 5"))
    t = out["tasks"][0]
    assert "Спикер" not in (t["assignee_name"] or "")
    assert t["employee_id"] is None
    assert out["requires_review"] is True


def test_unknown_person_is_reported_without_blocking():
    out = normalize(task("Подготовить смету", "Ольга подготовит смету", assignee_name="Ольга"))
    assert out["tasks"][0]["assignee_name"] == "Ольга"
    assert any("Ольга" in n and "справочнике" in n for n in out["assignment_notes"])


def test_analysis_prompt_receives_open_tasks_from_previous_meetings(monkeypatch):
    captured = {}

    def fake_request(model, system, text, *args, **kwargs):
        captured["system"] = system
        return {"title": "x", "summary": "", "participants": [], "topics": [], "decisions": [], "tasks": [],
                "open_questions": [], "risks": [], "notes": [], "next_meeting": None}

    monkeypatch.setattr(analyze.config, "llm_enabled", lambda: True)
    monkeypatch.setattr(analyze, "_request_verified_report", fake_request)
    segments = [{"start": 0, "end": 5, "speaker": "Спикер 1", "text": "Коротко обсудили договор."}]
    settings = {"report_model": "m", "open_tasks": "- Согласовать договор аренды — Вадим Чудин"}
    analyze.analyze(segments, TEAM, settings, dt.date(2026, 10, 22))
    assert "Согласовать договор аренды" in captured["system"] and "повтор:" in captured["system"]
    analyze.analyze(segments, TEAM, {"report_model": "m"}, dt.date(2026, 10, 22))
    assert "Открытые задачи прошлых совещаний" not in captured["system"]
