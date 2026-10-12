"""The AI employee does work itself, remembers people, keeps notes and answers "who does what"."""
import datetime as dt
import json

import pytest

from app import agent, analyze, assistant, pipeline, settings_store, userbot, visuals
from app.db import (Artifact, ConversationEscalation, ConversationMessage, Employee, Meeting, Note, PersonMemory,
                    SessionLocal, Task)
from test_ai_mode import FakeBackend, FakeModel, history, incoming, make_meeting, model, tgacc  # noqa: F401  (fixtures)


# ----------------------------------------------------------------------------- charts
def spec(kind="bar", **extra):
    base = {"type": kind, "title": "Задачи по людям", "unit": "шт.", "labels": ["Иван", "Анна", "Олег"],
            "series": [{"name": "в работе", "values": [2, 3, 1]}]}
    return {**base, **extra}


@pytest.mark.parametrize("bad, message", [
    ({"type": "radar"}, "Тип графика"),
    ({"labels": []}, "labels"),
    ({"labels": ["a"] * 31}, "labels"),
    ({"series": []}, "series"),
    ({"series": [{"name": "x", "values": [1, 2]}]}, "столько же"),
    ({"series": [{"name": "x", "values": [1, float("nan"), 3]}]}, "конечными"),
    ({"series": [{"name": "x", "values": [1, True, 3]}]}, "конечными"),
    ({"series": [{"name": "x", "values": [1, "2", 3]}]}, "конечными"),
    ({"title": ""}, "заголовок"),
    ({"type": "pie", "series": [{"name": "x", "values": [1, -2, 3]}]}, "неотрицательные"),
    ({"type": "pie", "series": [{"name": "a", "values": [1, 2, 3]}, {"name": "b", "values": [1, 2, 3]}]}, "один ряд"),
])
def test_a_bad_chart_is_refused(bad, message):
    with pytest.raises(visuals.ChartError, match=message):
        visuals.validate(spec(**bad))


@pytest.mark.parametrize("kind", ["bar", "hbar", "pie", "line"])
def test_every_chart_type_draws_a_complete_svg(kind):
    svg = visuals.svg(spec(kind))
    assert svg.startswith("<svg") and svg.endswith("</svg>") and svg.count("<svg") == 1
    assert "<title>Иван: 2 шт.</title>" in svg.replace(" (в работе)", "") or "Иван: 2 шт." in svg  # hover tooltips
    assert visuals.svg(spec(kind)) == svg  # deterministic


def test_text_in_a_chart_is_escaped():
    svg = visuals.svg(spec(labels=["<script>alert(1)</script>", "A&B", "x"], title="<b>Заголовок</b>"))
    assert "<script>" not in svg and "&lt;script&gt;" in svg and "A&amp;B" in svg and "<b>" not in svg


def test_many_or_long_labels_become_horizontal_bars_and_several_series_get_a_legend():
    many = visuals.svg(spec(labels=[f"Сотрудник номер {i}" for i in range(15)],
                            series=[{"name": "n", "values": list(range(15))}]))
    assert "text-anchor=\"end\"" in many
    two = visuals.svg(spec(series=[{"name": "в работе", "values": [1, 2, 3]}, {"name": "сделано", "values": [3, 2, 1]}]))
    assert "сделано" in two and "в работе" in two


def test_a_single_slice_pie_is_a_full_circle():
    assert "<circle" in visuals.svg(spec("pie", labels=["Все"], series=[{"name": "", "values": [5]}]))


# ----------------------------------------------------------------------------- tasks the assistant takes on
def meeting_with_chart_task(status="awaiting_approval", assistant_status="draft"):
    with SessionLocal() as s:
        meeting = Meeting(title="Планёрка", status=status, report={"title": "Планёрка", "summary": "Итог"}, options={},
                          transcript=[{"speaker": "Спикер 1", "start": 0.0, "end": 60.0,
                                       "text": "Иван берёт смету, а нам нужен график загрузки команды на 3 недели"}])
        ivan = Employee(name="Иван Петров", telegram_chat_id="@ivan", position="Менеджер")
        meeting.tasks = [Task(title="Подготовить смету", assignee=ivan, status="draft"),
                         Task(title="Построить график загрузки команды", executor="assistant", assignee_name="ассистент",
                              status=assistant_status, source_time="00:10", source_quote="нужен график загрузки")]
        s.add(meeting)
        s.commit()
        return meeting.id


def chart_answer(facts):
    people = facts["computed"]["tasks_per_person"]
    return {"kind": "chart", "title": "Загрузка команды", "message": "Построил график загрузки, он в файле.",
            "chart": {"type": "bar", "title": "Задачи по людям", "unit": "", "labels": list(people),
                      "series": [{"name": "задачи", "values": list(people.values())}]},
            "notes": ["У Ивана одна задача"], "sections": []}


def test_the_report_keeps_the_executor_and_the_name_of_an_assistant_task():
    report = analyze.normalize({"tasks": [
        {"title": "Построить график", "executor": "assistant", "employee_id": 1, "assignee_name": "", "quote": "q", "time": "00:01"},
        {"title": "Смета", "executor": "person", "employee_id": None, "assignee_name": "Вадиму", "quote": "q", "time": "00:02"},
        {"title": "Договор", "executor": "robot", "employee_id": None, "assignee_name": "", "quote": "q", "time": "00:03"}]},
        [SimpleEmployee(1, "Вадим Чудин")])
    assert [(t["executor"], t["employee_id"], t["assignee_name"]) for t in out_tasks(report)] == [
        ("assistant", None, "ассистент"), ("person", 1, "Вадиму"), ("person", None, "")]
    assert "executor" in analyze.SYSTEM and "ассистент" in analyze.SYSTEM


class SimpleEmployee:
    def __init__(self, i, name):
        self.id, self.name, self.aliases = i, name, ""

    def alias_list(self):
        return []


def out_tasks(report):
    return report["tasks"]


def test_a_saved_report_stores_the_executor():
    mid = make_meeting(status="analyzing", people=())
    report = {"title": "X", "tasks": [{"title": "График", "executor": "assistant", "employee_id": None,
                                       "assignee_name": "ассистент", "deadline": None, "priority": "medium",
                                       "quote": "q", "time": "00:01", "description": ""}]}
    pipeline._save_report(mid, report, pipeline.meeting_settings(mid))
    with SessionLocal() as s:
        assert s.query(Task).one().executor == "assistant"


def test_the_draft_for_the_reviewer_says_what_the_assistant_takes_on(tgacc, model):
    mid = meeting_with_chart_task(status="analyzing")
    pipeline.request_approval(mid)
    facts = next(c["facts"] for c in model.calls if "проверяющему (руководителю" in c["system"])
    assert len(facts["tasks_assistant_takes_on"]) == 1 and facts["tasks_without_assignee"] == []
    assert "tasks_assistant_takes_on" in next(c["system"] for c in model.calls if "проверяющему (руководителю" in c["system"])


def test_the_assistant_builds_the_chart_after_approval_and_sends_it_to_the_reviewer(tgacc, model):
    mid = meeting_with_chart_task()
    model.assistant.append(chart_answer)
    assert pipeline.submit_deliver(mid)
    assignee = next(c for c in model.calls if "одному сотруднику" in c["system"])
    assert [t["title"] for t in assignee["facts"]["tasks"]] == ["Подготовить смету"]  # the chart is not Ivan's task
    assert ("@the_boss", "[unassigned]") not in tgacc.sent and ("@boss", "[unassigned]") not in tgacc.sent
    assert ("@boss", "Построил график загрузки, он в файле.") in tgacc.sent
    assert [(r, n.endswith(".pdf")) for r, n, c in tgacc.files if r == "@boss"] == [("@boss", True)]
    assert tgacc.file_data[-1].startswith(b"%PDF")
    with SessionLocal() as s:
        task = s.query(Task).filter_by(executor="assistant").one()
        artifact = s.query(Artifact).one()
        assert task.status == "assistant_done" and artifact.svg.startswith("<svg") and bytes(artifact.data).startswith(b"%PDF")
        assert s.query(Note).filter_by(task_id=task.id, kind="done").count() == 1


def test_a_number_that_is_not_in_the_data_is_never_charted(tgacc, model):
    mid = meeting_with_chart_task()

    def invented(facts):
        answer = chart_answer(facts)
        answer["chart"]["series"][0]["values"] = [777]
        return answer
    model.assistant += [invented, invented]
    pipeline.submit_deliver(mid)
    with SessionLocal() as s:
        task = s.query(Task).filter_by(executor="assistant").one()
        assert task.status == "assistant_failed" and s.query(Artifact).count() == 0
        assert "числа, которых нет" in s.query(Note).filter_by(task_id=task.id).one().text
    assert ("@boss", "[assistant-failed]") in tgacc.sent  # the reviewer is told, never left wondering
    assert "числа, которых нет" in model.calls[-2]["user"] or "числа, которых нет" in model.calls[-1]["user"]


def test_the_assistant_says_so_when_there_is_no_data_for_the_chart(tgacc, model):
    mid = meeting_with_chart_task()
    model.assistant.append({"kind": "cannot", "message": "График не получится: цифр по загрузке на встрече не прозвучало.",
                           "reason": "нет цифр", "title": "", "chart": None, "notes": [], "sections": []})
    pipeline.submit_deliver(mid)
    assert ("@boss", "График не получится: цифр по загрузке на встрече не прозвучало.") in tgacc.sent
    with SessionLocal() as s:
        assert s.query(Task).filter_by(executor="assistant").one().status == "assistant_failed"
        assert s.query(Artifact).count() == 0


def test_a_text_report_task_makes_a_pdf_with_sections(tgacc, model):
    mid = meeting_with_chart_task()
    model.assistant.append({"kind": "report", "title": "Сводка", "message": "Свёл итоги в короткую сводку.", "chart": None,
                           "notes": [], "sections": [{"heading": "Главное", "text": "Смета за Иваном."}]})
    pipeline.submit_deliver(mid)
    assert ("@boss", "Свёл итоги в короткую сводку.") in tgacc.sent
    with SessionLocal() as s:
        assert s.query(Artifact).one().title == "Сводка"


def test_unfinished_assistant_tasks_resume_after_a_restart(tgacc, model):
    mid = meeting_with_chart_task(status="done", assistant_status="assistant_running")
    assistant.recover()
    with SessionLocal() as s:
        assert s.query(Task).filter_by(executor="assistant").one().status == "assistant_todo"
    model.assistant.append(chart_answer)
    assistant.run_pending()
    with SessionLocal() as s:
        assert s.query(Task).filter_by(executor="assistant").one().status == "assistant_done"


def test_nothing_runs_before_the_meeting_is_approved(tgacc, model):
    meeting_with_chart_task(status="awaiting_approval", assistant_status="assistant_todo")
    assistant.run_pending()
    assert model.calls == []


def test_the_panel_shows_the_chart_and_serves_the_pdf(client, tgacc, model):
    mid = meeting_with_chart_task()
    model.assistant.append(chart_answer)
    pipeline.submit_deliver(mid)
    page = client.get(f"/meetings/{mid}").text
    assert "Материалы ассистента" in page and "<svg" in page and "Берёт на себя ассистент" in page
    with SessionLocal() as s:
        artifact_id = s.query(Artifact).one().id
    response = client.get(f"/artifacts/{artifact_id}")
    assert response.status_code == 200 and response.headers["content-type"] == "application/pdf" and response.content.startswith(b"%PDF")
    assert client.get("/artifacts/999").status_code == 404
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as anonymous:
        assert anonymous.get(f"/artifacts/{artifact_id}", follow_redirects=False).headers["location"] == "/login"


# ----------------------------------------------------------------------------- the reviewer hands tasks over
def awaiting_meeting_with_open_task():
    with SessionLocal() as s:
        meeting = Meeting(title="Планёрка", status="awaiting_approval", report={"title": "Планёрка", "summary": "Итог"},
                          options={}, transcript=[{"speaker": "Спикер 1", "start": 0, "end": 10, "text": "нужен график"}])
        anna = Employee(name="Анна Орлова", telegram_chat_id="@anna")
        meeting.tasks = [Task(title="Подготовить график", assignee_name="кто-то", status="draft")]
        s.add_all([meeting, anna])
        s.commit()
        return meeting.id, anna.id, meeting.tasks[0].id


def test_the_reviewer_can_give_an_open_task_to_the_assistant_and_it_gets_done(tgacc, model):
    mid, _, task_id = awaiting_meeting_with_open_task()
    model.decisions.append({"reply": "Беру на себя, сделаю после отправки", "actions": [
        {"type": "assign_task", "task_id": task_id, "to": "assistant"}]})
    agent.handle_incoming(incoming(user_id=7, username="boss", text="это на ИИ", message_id=1, name="Шеф"))
    ctx = model.calls[0]["facts"]
    assert [t["id"] for t in ctx["unassigned_tasks"]] == [task_id] and ctx["roles"] == ["reviewer"]
    with SessionLocal() as s:
        task = s.get(Task, task_id)
        assert (task.executor, task.status) == ("assistant", "assistant_todo")
        assert s.query(Note).filter_by(task_id=task_id, kind="commitment").count() == 1
    assert tgacc.sent[-1] == ("7", "Беру на себя, сделаю после отправки")
    model.decisions.append({"reply": "", "actions": []})  # nobody writes on approval; the model is only used for texts
    model.assistant.append(lambda facts: {"kind": "report", "title": "График", "message": "Сделал.", "chart": None, "notes": [],
                                          "sections": [{"heading": "Итог", "text": "Готово"}]})
    assert pipeline.submit_deliver(mid)
    with SessionLocal() as s:
        assert s.get(Task, task_id).status == "assistant_done"
    assert ("@boss", "Сделал.") in tgacc.sent


def test_the_reviewer_can_give_an_open_task_to_a_person(tgacc, model):
    mid, anna_id, task_id = awaiting_meeting_with_open_task()
    model.decisions.append({"reply": "Отдал Анне", "actions": [{"type": "assign_task", "task_id": task_id, "to": anna_id}]})
    agent.handle_incoming(incoming(user_id=7, username="boss", text="отдай Анне", message_id=1, name="Шеф"))
    with SessionLocal() as s:
        task = s.get(Task, task_id)
        assert (task.executor, task.assignee_id, task.assignee_name) == ("person", anna_id, "Анна Орлова")
    assert not any(ref == "@anna" for ref, _ in tgacc.sent)  # she gets it with the rest after approval
    pipeline.submit_deliver(mid)
    assert ("@anna", "[assignee:Анна Орлова]") in tgacc.sent


def test_a_task_moved_after_the_meeting_was_sent_needs_a_message_for_the_new_person(tgacc, model):
    (smeta, _, _), _, anna_id = sent_tasks()
    bad = {"reply": "Отдал", "actions": [{"type": "assign_task", "task_id": smeta, "to": anna_id}]}
    good = {"reply": "Отдал Анне и написал ей", "actions": [{"type": "assign_task", "task_id": smeta, "to": anna_id,
                                                              "message": "Анна, смета теперь на тебе."}]}
    model.decisions += [bad, good]
    agent.handle_incoming(incoming(user_id=7, username="boss", text="смету лучше Анне", message_id=1, name="Шеф"))
    assert "нужен message сотруднику" in model.calls[-1]["user"]
    assert ("@anna", "Анна, смета теперь на тебе.") in tgacc.sent
    with SessionLocal() as s:
        assert s.get(Task, smeta).assignee_id == anna_id


def test_a_person_and_an_employee_cannot_hand_tasks_over(tgacc, model):
    mid, anna_id, task_id = awaiting_meeting_with_open_task()
    with SessionLocal() as s:
        s.add(Employee(name="Иван Петров", telegram_chat_id="@ivan"))
        s.commit()
    hijack = {"reply": "Готово", "actions": [{"type": "assign_task", "task_id": task_id, "to": "assistant"}]}
    model.decisions += [hijack, hijack]
    agent.handle_incoming(incoming(text="отдай задачу ассистенту"))
    assert tgacc.sent == []
    with SessionLocal() as s:
        assert s.get(Task, task_id).executor == "person"


# ----------------------------------------------------------------------------- progress, notes, memory
def sent_tasks():
    with SessionLocal() as s:
        meeting = Meeting(title="Планёрка", status="done", report={"title": "Планёрка", "summary": "Итог"}, options={})
        ivan, anna = Employee(name="Иван Петров", telegram_chat_id="@ivan"), Employee(name="Анна Орлова", telegram_chat_id="@anna")
        meeting.tasks = [Task(title="Подготовить смету", assignee=ivan, status="sent",
                              deadline=dt.date.today() - dt.timedelta(days=1)),
                         Task(title="Позвонить клиенту", assignee=anna, status="sent"),
                         Task(title="Сдать договор", assignee=anna, status="done")]
        s.add(meeting)
        s.commit()
        return [t.id for t in meeting.tasks], ivan.id, anna.id


def test_an_employee_reports_progress_and_the_note_is_kept(tgacc, model):
    (smeta, call, _), ivan_id, _ = sent_tasks()
    model.decisions.append({"reply": "Отлично, спасибо!", "actions": [
        {"type": "task_status", "task_id": smeta, "status": "done", "note": "смета ушла клиенту"}]})
    agent.handle_incoming(incoming(text="смету закончил, отправил клиенту"))
    with SessionLocal() as s:
        assert s.get(Task, smeta).status == "done"
        note = s.query(Note).filter_by(task_id=smeta).one()
        assert note.kind == "done" and "смета ушла клиенту" in note.text and note.employee_id == ivan_id and note.author == "Иван"
    assert tgacc.sent == [("42", "Отлично, спасибо!")]


def test_an_employee_cannot_report_for_someone_else_or_note_about_others(tgacc, model):
    (_, call, _), _, anna_id = sent_tasks()
    other = {"reply": "Принял", "actions": [{"type": "task_status", "task_id": call, "status": "done"}]}
    gossip = {"reply": "Записал", "actions": [{"type": "note", "text": "Анна всегда опаздывает", "employee_id": anna_id}]}
    model.decisions += [other, other, gossip, gossip]
    agent.handle_incoming(incoming(text="Анна позвонила клиенту", message_id=1))
    agent.handle_incoming(incoming(text="запиши про Анну", message_id=2))
    assert tgacc.sent == []
    with SessionLocal() as s:
        assert s.get(Task, call).status == "sent" and s.query(Note).count() == 0
        errors = [r.error for r in s.query(ConversationMessage).filter_by(role="user")]
    assert "не найдена среди доступных" in errors[0] and "только о себе" in errors[1]


def test_a_reviewer_keeps_notes_about_any_employee(tgacc, model):
    _, ivan_id, _ = sent_tasks()
    model.decisions.append({"reply": "Записал", "actions": [{"type": "note", "text": "Иван просил не давать задач по пятницам",
                                                              "employee_id": ivan_id, "kind": "fact"}]})
    agent.handle_incoming(incoming(user_id=7, username="boss", text="запиши про Ивана", message_id=1, name="Шеф"))
    with SessionLocal() as s:
        assert s.query(Note).one().employee_id == ivan_id
    model.decisions.append({"reply": "Да, помню", "actions": []})
    agent.handle_incoming(incoming(text="привет", message_id=2))
    assert any("пятницам" in n["text"] for n in model.calls[-1]["facts"]["notes"])  # Ivan sees what is kept about him


def test_the_memory_of_a_person_is_kept_and_used_in_the_next_talk(tgacc, model):
    sent_tasks()
    model.decisions.append({"reply": "Принято", "actions": [], "memory": "Иван любит короткие сообщения, работает по утрам"})
    agent.handle_incoming(incoming(text="пиши мне коротко, я работаю по утрам", message_id=1))
    with SessionLocal() as s:
        assert "по утрам" in s.get(PersonMemory, "42").summary
    model.decisions.append({"reply": "Хорошо", "actions": []})
    agent.handle_incoming(incoming(text="привет", message_id=2))
    assert "короткие сообщения" in model.calls[-1]["facts"]["memory"]


def test_a_conversation_that_is_going_on_is_not_greeted_again(tgacc, model):
    sent_tasks()
    model.decisions += [{"reply": "Привет, Иван!", "actions": []}, {"reply": "Ок, жду", "actions": []}]
    agent.handle_incoming(incoming(text="привет", message_id=1))
    assert model.calls[0]["facts"]["conversation"]["already_talking"] is False
    agent.handle_incoming(incoming(text="ещё вопрос по смете", message_id=2))
    talk = model.calls[1]["facts"]["conversation"]
    assert talk["already_talking"] is True and talk["messages_so_far"] == 2
    system = model.calls[1]["system"]
    assert "не здоровайся и не представляйся" in system and "already_talking" in system


def test_what_was_sent_to_a_person_is_part_of_the_conversation(tgacc, model):
    mid = meeting_with_chart_task(status="awaiting_approval")
    model.assistant.append(chart_answer)
    pipeline.submit_deliver(mid)
    with SessionLocal() as s:
        assert s.query(Employee).filter_by(name="Иван Петров").one().telegram_user_id == "42"
    assert ("assistant", "agent_done", "[assignee:Иван Петров]") in history(42)
    model.decisions.append({"reply": "Да, смета на мне", "actions": []})
    agent.handle_incoming(incoming(text="я про задачу со сметой"))
    ctx = model.calls[-1]["facts"]
    assert ctx["conversation"]["already_talking"] is True
    assert {"from": "ты", "text": "[assignee:Иван Петров]"} in ctx["history"]
    # the next message from the assistant to the same person does not start with a greeting
    with SessionLocal() as s:
        ivan = s.query(Employee).filter_by(name="Иван Петров").one()
        second = Meeting(title="Вторая встреча", status="awaiting_approval", report={"title": "Вторая", "summary": "Итог"}, options={})
        second.tasks = [Task(title="Новая задача", assignee=ivan, status="draft")]
        s.add(second)
        s.commit()
        second_id = second.id
    pipeline.submit_deliver(second_id)
    later = [c for c in model.calls if "одному сотруднику" in c["system"]][-1]
    assert later["facts"]["already_talking"] is True and "не здоровайся заново" in later["system"]


# ----------------------------------------------------------------------------- full access and the team report
def test_full_access_contacts_get_a_report_of_who_does_what(tgacc, model, monkeypatch):
    sent_tasks()
    settings_store.set_many({"full_access_chat_ids": ["@helper"]})
    model.decisions.append({"reply": "Коротко: у Ивана одна просроченная задача, у Анны одна в работе и одна сделана. Детали в файле.",
                            "actions": [{"type": "team_report", "scope": "all"}]})
    agent.handle_incoming(incoming(user_id=55, username="helper", text="кто что делает?", name="Помощник"))
    ctx = model.calls[0]["facts"]
    assert ctx["roles"] == ["full_access"] and {p["name"] for p in ctx["team"]} == {"Иван Петров", "Анна Орлова"}
    assert "meetings_awaiting_approval" not in ctx and "open_questions_from_employees" not in ctx  # not a reviewer
    assert tgacc.sent[-1][0] == "55" and "Детали в файле" in tgacc.sent[-1][1]
    assert tgacc.files[-1][0] == "55" and tgacc.files[-1][1].startswith("Кто_что_делает_") and tgacc.file_data[-1].startswith(b"%PDF")


def test_the_team_report_is_built_from_the_database(monkeypatch):
    sent_tasks()
    with SessionLocal() as s:
        ivan = s.query(Employee).filter_by(name="Иван Петров").one()
        s.add(Note(employee_id=ivan.id, kind="status", text="Просил ещё день на смету", source="conversation"))
        s.commit()
    captured = {}
    monkeypatch.setattr(assistant, "_pdf", lambda html: captured.setdefault("html", html) and b"%PDF-fake")
    filename, pdf = assistant.team_report("all")
    html = captured["html"]
    assert filename.startswith("Кто_что_делает_") and pdf == b"%PDF-fake"
    for expected in ("Иван Петров", "Анна Орлова", "Подготовить смету", "Позвонить клиенту", "Сдать договор",
                     "просрочено", "Просил ещё день на смету", "<svg"):
        assert expected in html
    captured.clear()
    with SessionLocal() as s:
        ivan_id = s.query(Employee).filter_by(name="Иван Петров").one().id
    filename, _ = assistant.team_report(ivan_id)
    assert "Иван Петров" in captured["html"] and "Анна Орлова" not in captured["html"]
    assert "Кто что делает: Иван Петров" in captured["html"]


def test_an_ordinary_employee_cannot_ask_for_the_team_report(tgacc, model):
    sent_tasks()
    hijack = {"reply": "Вот отчёт", "actions": [{"type": "team_report", "scope": "all"}]}
    model.decisions += [hijack, hijack]
    agent.handle_incoming(incoming(text="кто что делает в команде?"))
    assert tgacc.sent == [] and tgacc.files == []
    assert "team" not in model.calls[0]["facts"]  # the data is not even shown to an employee
    with SessionLocal() as s:
        assert "полным доступом" in s.query(ConversationMessage).filter_by(role="user").one().error


def test_full_access_contacts_are_a_setting_shown_only_in_ai_mode(client):
    assert "Полный доступ" not in client.get("/settings").text
    client.post("/settings", data={"present_ai_employee": "1", "ai_employee": "on", "present_full_access_chat_ids": "1",
                                   "full_access_chat_ids_extra": "t.me/helper_one, +375 29 111-22-33"})
    assert settings_store.get("full_access_chat_ids") == ["@helper_one", "+375291112233"]
    assert "Полный доступ" in client.get("/settings").text
    bad = client.post("/settings", data={"present_full_access_chat_ids": "1", "full_access_chat_ids_extra": "кто-то"})
    assert bad.status_code == 400


# ----------------------------------------------------------------------------- the account stays online
def test_the_watchdog_reconnects_a_dropped_account(tgacc):
    assert tgacc.connects == 1
    userbot.watchdog()
    assert tgacc.connects == 1  # alive: nothing to do
    tgacc.alive = False
    userbot.watchdog()
    assert tgacc.connects == 2 and userbot.ready()


def test_the_watchdog_connects_an_account_that_never_came_up(tgacc):
    userbot._state["connected"] = False
    userbot.watchdog()
    assert tgacc.connects == 2 and userbot.ready()


def test_the_watchdog_does_nothing_in_bot_mode_or_without_an_account(monkeypatch):
    backend = FakeBackend()
    monkeypatch.setattr(userbot, "backend", backend)
    monkeypatch.setattr(userbot, "_state", {"me": None, "error": "", "connected": False})
    userbot.watchdog()
    assert backend.connects == 0


def test_poking_the_loop_marks_activity():
    pipeline._ACTIVITY[0] = 0
    pipeline.poke()
    assert pipeline._ACTIVITY[0] > 0 and pipeline._WAKE.is_set()
    pipeline._WAKE.clear()
