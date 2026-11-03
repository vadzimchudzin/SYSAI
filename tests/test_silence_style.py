"""Plan №14 (whom to tell when a person does not answer) and №15 (how the assistant writes: the address from the card,
one question at a time, the hourly limit for one person, the days off)."""
import datetime as dt
import json
from zoneinfo import ZoneInfo

import pytest

from app import agent, agent_checks, agent_skills, assistant, cases, config, llm, progress, settings_store, userbot, voice
from app.db import Case, CaseEvent, CaseWait, Department, Employee, EmployeeDepartment, SessionLocal, now
from test_ai_mode import FakeBackend, FakeModel, tgacc  # noqa: F401  (fixtures)
from test_cases import CaseModel, cmodel, incoming, make_task, office  # noqa: F401  (fixtures)


def local(day: int, hour: int, minute: int = 0):
    """A moment of October 2026 in the service time zone (12 October is a Monday)."""
    return dt.datetime(2026, 10, day, hour, minute, tzinfo=ZoneInfo(config.TIMEZONE)).astimezone(dt.timezone.utc)


def card(employee_id: int, **fields):
    with SessionLocal() as s:
        e = s.get(Employee, employee_id)
        for key, value in fields.items():
            setattr(e, key, value)
        s.commit()
    agent.invalidate_roster()


def ask_anna(office) -> int:
    task_id = make_task(office, kind="ask", counterparts=(("recipient", "anna"),), target="anna",
                        title="Узнать у Анны сроки сверки")
    assistant.run_task(task_id)
    with SessionLocal() as s:
        return s.query(Case).filter_by(task_id=task_id).one().id


def wait_of(case_id: int):
    with SessionLocal() as s:
        w = s.query(CaseWait).filter_by(case_id=case_id).order_by(CaseWait.id.desc()).first()
        return {"status": w.status, "escalated": w.escalated, "to": w.escalated_to, "reminders": w.reminders_sent,
                "next": w.next_reminder_at, "escalate_at": w.escalate_at}


def choose(choice: str, reply: str):
    def decision(facts):
        silent = facts["silence_to_decide"]
        return {"reply": reply, "actions": [{"type": "silence_choice", "case_id": silent[0]["case_id"], "choice": choice}]}
    return decision


# ----------------------------------------------------------------------------- №14: whom to tell, when
def test_whom_to_tell_comes_from_the_card_then_the_department_then_the_settings(office):
    settings_store.set_many({"case_escalate_hours": 24, "case_max_reminders": 2})
    with SessionLocal() as s:
        dept = Department(name="Бухгалтерия", silence_contact_id=office["petya"], silence_hours=8, silence_reminders=1)
        s.add(dept)
        s.flush()
        s.add(EmployeeDepartment(employee_id=office["anna"], department_id=dept.id))
        s.commit()

    def rule(employee="anna"):
        with SessionLocal() as s:
            r = cases.silence_rule(s, s.get(Employee, office[employee]))
            return r["contact"].name if r["contact"] else None, r["hours"], r["reminders"], r["source"]

    assert rule() == ("Пётр Иванов", 8, 1, "подразделение «Бухгалтерия»")
    card(office["anna"], silence_contact_id=office["ivan"], silence_hours=3)
    assert rule() == ("Иван Петров", 3, 1, "карточка сотрудника")  # her own card wins, the rest from the department
    card(office["anna"], silence_contact_id=office["anna"], silence_hours=None)
    assert rule()[0] == "Пётр Иванов"  # never the silent person herself
    card(office["petya"], telegram_chat_id="")
    assert rule() == (None, 8, 1, "настройки")  # nobody reachable named: the reviewers, as before
    assert rule("ivan") == (None, 24, 2, "настройки")


def test_only_the_named_person_hears_about_the_silence_and_decides_in_words(office, tgacc, cmodel):
    settings_store.set_many({"case_reminder_hours": 4})
    card(office["anna"], silence_contact_id=office["petya"], silence_hours=6, silence_reminders=1)
    case_id = ask_anna(office)
    cases.run_due(now() + dt.timedelta(hours=5))
    assert tgacc.sent[-1][0] == "@anna" and tgacc.sent[-1][1].startswith("[remind]")
    cases.run_due(now() + dt.timedelta(hours=7))
    assert tgacc.sent[-1][0] == "@anna"  # the last reminder gets its hours before the silence is reported
    cases.run_due(now() + dt.timedelta(hours=10))
    assert tgacc.sent[-1][0] == "@petya" and tgacc.sent[-1][1].startswith("[escalate]")
    assert not any(ref == "@boss" for ref, _ in tgacc.sent)  # the reviewers are not disturbed
    assert wait_of(case_id)["escalated"] and wait_of(case_id)["to"] == ["@petya"]
    assert [x["case_id"] for x in cases.silence_view({"user_id": 44, "username": "petya"})] == [case_id]
    assert cases.silence_view({"user_id": 7, "username": "boss"}) == []
    with SessionLocal() as s:
        view = progress.case_card(s, s.get(Case, case_id))
        assert view["waiting"]["told"] == "Пётр Иванов" and view["can"]["remind"] and view["can"]["take_over"]

    # «Напомни ещё»: one more reminder now, and the same person hears again if the silence goes on
    cmodel.decisions.append(choose("remind", "Хорошо, напомнил Анне ещё раз."))
    agent.handle_incoming(incoming(44, "petya", text="Напомни ей ещё раз", message_id=801))
    assert [t for ref, t in tgacc.sent if ref == "@anna"][-1].startswith("[remind]")
    state = wait_of(case_id)
    assert not state["escalated"] and state["reminders"] == 2 and state["status"] == "open"
    cases.run_due(now() + dt.timedelta(hours=13))
    assert tgacc.sent[-1][0] == "@petya" and tgacc.sent[-1][1].startswith("[escalate]")

    # «Займусь сам»: the case is his, nobody is reminded any more
    cmodel.decisions.append(choose("self", "Понял, больше не напоминаю."))
    agent.handle_incoming(incoming(44, "petya", text="Займусь сам", message_id=802))
    with SessionLocal() as s:
        case = s.get(Case, case_id)
        assert case.status == "handed_over" and case.task.assignee_id == office["petya"]
        assert s.query(CaseWait).filter_by(case_id=case_id, status="open").count() == 0
    count = len(tgacc.sent)
    cases.run_due(now() + dt.timedelta(hours=60))
    assert len(tgacc.sent) == count


def test_no_reminders_means_the_contact_hears_at_once_and_can_cancel(office, tgacc, cmodel):
    card(office["anna"], silence_contact_id=office["ivan"], silence_hours=2, silence_reminders=0)
    case_id = ask_anna(office)
    asked = len(tgacc.sent)
    cases.run_due(now() + dt.timedelta(hours=1))
    assert len(tgacc.sent) == asked
    cases.run_due(now() + dt.timedelta(hours=3))
    assert [ref for ref, _ in tgacc.sent[asked:]] == ["@ivan"]  # no reminder to Анна first
    person = agent_skills.Person(employee=type("E", (), {"id": office["ivan"]})())
    ctx = {"silence_to_decide": cases.silence_view({"user_id": 42, "username": "ivan"})}
    assert agent_skills.check({"type": "silence_choice", "case_id": case_id, "choice": "cancel"}, ctx, person) == ""
    assert "не найдено" in agent_skills.check({"type": "silence_choice", "case_id": case_id + 1, "choice": "cancel"},
                                              ctx, person)
    assert "choice" in agent_skills.check({"type": "silence_choice", "case_id": case_id, "choice": "wait"}, ctx, person)
    cmodel.decisions.append(choose("cancel", "Отменил."))
    agent.handle_incoming(incoming(42, "ivan", text="Отмени, уже не нужно", message_id=803))
    with SessionLocal() as s:
        assert s.get(Case, case_id).status == "cancelled"


def test_the_progress_buttons_remind_again_and_take_over(office, tgacc, cmodel, client):
    card(office["anna"], silence_contact_id=office["petya"], silence_hours=2, silence_reminders=0)
    case_id = ask_anna(office)
    cases.run_due(now() + dt.timedelta(hours=3))
    assert client.post(f"/progress/cases/{case_id}/remind", follow_redirects=False).status_code == 303
    assert tgacc.sent[-1][0] == "@anna" and tgacc.sent[-1][1].startswith("[remind]")
    assert not wait_of(case_id)["escalated"]
    with SessionLocal() as s:
        assert "попросил SYSAI" in s.query(CaseEvent).filter_by(case_id=case_id, kind="reminder").all()[-1].text
    assert client.post(f"/progress/cases/{case_id}/take_over", follow_redirects=False).status_code == 303
    with SessionLocal() as s:
        assert s.get(Case, case_id).status == "handed_over"
    assert client.post(f"/progress/cases/{case_id}/remind", follow_redirects=False).status_code == 409


def test_silence_is_counted_in_working_time():
    settings_store.set_many({"quiet_from_hour": 21, "quiet_to_hour": 9, "quiet_days": [5, 6],
                             "silence_working_hours": True})
    assert userbot.working_after(local(16, 19), 4) == local(19, 11)    # Friday 19:00: 2 h on Friday, 2 h on Monday
    assert userbot.working_after(local(15, 10), 24) == local(19, 10)   # Thursday 11 h + Friday 12 h + Monday 1 h
    assert userbot.working_after(local(17, 12), 1) == local(19, 10)    # asked on Saturday: counted from Monday
    assert userbot.working_after(local(14, 10), 2) == local(14, 12)    # an ordinary Wednesday
    settings_store.set_many({"silence_working_hours": False})
    assert userbot.working_after(local(16, 19), 4) == local(16, 23)


def test_a_case_waits_in_working_time():
    settings_store.set_many({"quiet_from_hour": 21, "quiet_to_hour": 9, "quiet_days": [5, 6],
                             "silence_working_hours": True, "case_reminder_hours": 1})
    first, escalate = cases._wait_times(local(16, 19), "answer", None, {"hours": 4, "reminders": 1})
    assert first == local(16, 20) and escalate == local(19, 11)  # asked on Friday at 19:00: reported on Monday
    first, escalate = cases._wait_times(local(16, 19), "answer", None, {"hours": 4, "reminders": 0})
    assert first == escalate == local(19, 11)  # no reminders: the contact hears when the time is up


# ----------------------------------------------------------------------------- №15: the quiet days and the limit
def test_days_off_hold_first_messages_but_not_answers():
    settings_store.set_many({"quiet_from_hour": 21, "quiet_to_hour": 9, "quiet_days": [5, 6]})
    monday = local(19, 9)
    assert userbot.quiet_until(local(17, 12)) == monday                       # Saturday noon
    assert userbot.quiet_until(local(16, 22)) == monday                       # Friday night runs into the weekend
    assert userbot.quiet_until(local(16, 15)) is None                         # Friday afternoon
    assert userbot.quiet_until(local(17, 12), answering=True) is None         # an answer goes out on Saturday
    assert userbot.quiet_until(local(16, 22), answering=True) == local(17, 9)  # but not at night
    settings_store.set_many({"quiet_days": []})
    assert userbot.quiet_until(local(17, 12)) is None


def test_the_hourly_limit_for_one_person(office, tgacc):
    settings_store.set_many({"person_hourly_limit": 2})
    userbot.send_message("@anna", "1")
    userbot.send_message("@anna", "2")
    with pytest.raises(userbot.Busy) as busy:
        userbot.send_message("@anna", "3")
    assert isinstance(busy.value, userbot.QuietHours) and 60 <= busy.value.retry_after <= 3600
    userbot.send_message("@anna", "ответ", answering=True)              # an answer is never held by the limit
    userbot.send_message("@anna", "в 15:00 позвонить", urgent=True)    # a reminder asked for at a given time
    userbot.send_message("@petya", "1")                                 # another person
    assert [t for _, t in tgacc.sent] == ["1", "2", "ответ", "в 15:00 позвонить", "1"]


def test_a_reminder_over_the_limit_waits_instead_of_retrying_every_minute(office, tgacc, cmodel):
    settings_store.set_many({"person_hourly_limit": 1, "case_reminder_hours": 4})
    case_id = ask_anna(office)  # the question itself is the one message of this hour
    moment = now() + dt.timedelta(hours=5)
    count = len(tgacc.sent)
    cases.run_due(moment)
    assert len(tgacc.sent) == count
    state = wait_of(case_id)
    nxt = state["next"] if state["next"].tzinfo else state["next"].replace(tzinfo=dt.timezone.utc)
    assert state["reminders"] == 0 and nxt > moment


# ----------------------------------------------------------------------------- №15: the address and the style
def test_the_card_says_how_to_address_the_person(office, tgacc, cmodel):
    card(office["anna"], address_as="Анна Сергеевна", address_form="вы")
    assert voice.address_of(None) is None
    seen = []

    def decision(facts):
        seen.append(facts["employee"].get("address"))
        return {"reply": "Анна Сергеевна, сейчас ничего не горит.", "actions": []}
    cmodel.decisions.append(decision)
    agent.handle_incoming(incoming(43, "anna", text="Что у меня горит?", message_id=900))
    assert seen == [{"name": "Анна Сергеевна", "form": "вы"}]
    ask_anna(office)
    assert cmodel.writes[-1]["person"]["address"] == {"name": "Анна Сергеевна", "form": "вы"}
    card(office["anna"], address_as="", address_form="ты")
    with SessionLocal() as s:
        assert voice.address_of(s.get(Employee, office["anna"])) == {"name": None, "form": "ты"}


def test_one_question_at_a_time_and_plain_words(monkeypatch):
    assert voice.questions("Какой срок? И кому?") == 2
    assert voice.questions("Кому поручить и к какому сроку?") == 1
    assert voice.questions("Сделаю до пятницы.") == 0
    calls = []
    answers = iter([{"reply": "Кому поручить? К какому сроку?", "actions": []}, {"reply": "Кому поручить?", "actions": []}])

    def fake(model_name, messages, **kwargs):
        calls.append(json.dumps(messages, ensure_ascii=False))
        return json.dumps(next(answers), ensure_ascii=False)
    monkeypatch.setattr(llm, "chat", fake)
    ctx, person, text = agent_checks.scenario("style-one-question").build()
    decision = agent.decide(ctx, text, person, model="m/x", transport="json", approvers=agent_checks.APPROVERS)
    assert decision["reply"] == "Кому поручить?" and len(calls) == 2
    assert "одним вопросом за раз" in calls[1] and "несколько вопросов сразу" in calls[1]


def test_the_checks_name_style_mistakes():
    one = agent_checks.scenario("style-one-question")
    ctx, _, _ = one.build()
    reasons = one.evaluate({"reply": "Кому поручить? К какому сроку? Что именно проверить?", "actions": []}, ctx)
    assert any("несколько вопросов" in r for r in reasons) and any("слишком много вопросов" in r for r in reasons)
    assert one.evaluate({"reply": "Что именно с поставками: сроки или поставщик?", "actions": []}, ctx) == []
    vy = agent_checks.scenario("style-vy")
    ctx, _, _ = vy.build()
    assert ctx["employee"]["address"] == {"name": "Анна Сергеевна", "form": "вы"}
    assert any("на «ты»" in r for r in vy.evaluate({"reply": "У тебя горит оплата бетона.", "actions": []}, ctx))
    assert vy.evaluate({"reply": "Анна Сергеевна, у вас горит оплата счёта за бетон — срок завтра.",
                        "actions": []}, ctx) == []
    assert any("канцелярит" in r for r in vy.evaluate(
        {"reply": "Доводим до вашего сведения, что срок оплаты завтра.", "actions": []}, ctx))
    ty = agent_checks.scenario("style-ty")
    ctx, _, _ = ty.build()
    assert any("на «вы»" in r for r in ty.evaluate({"reply": "Вам нужно сдать КС-2 до четверга.", "actions": []}, ctx))
    plain = agent_checks.scenario("style-plain")
    ctx, _, _ = plain.build()
    assert any("слишком длинно" in r for r in plain.evaluate({"reply": "Сначала смета. " * 40, "actions": []}, ctx))
    groups = {x.group for x in agent_checks.scenarios()}
    assert {"Стиль общения", "Молчание"} <= groups


# ----------------------------------------------------------------------------- the panel
def test_the_settings_keep_the_days_off_the_limit_and_the_working_time(tgacc, client):
    form = {"present_quiet_days": "1", "quiet_days": ["5", "6", "0"], "person_hourly_limit": "3",
            "present_silence_working_hours": "1"}
    assert client.post("/settings", data=form, follow_redirects=False).status_code in (200, 303)
    assert settings_store.get("quiet_days") == [0, 5, 6] and settings_store.get("person_hourly_limit") == 3
    assert settings_store.get("silence_working_hours") is False
    every = {"present_quiet_days": "1", "quiet_days": [str(d) for d in range(7)]}
    assert client.post("/settings", data=every, follow_redirects=False).status_code == 400
    assert client.post("/settings", data={"person_hourly_limit": "99"}, follow_redirects=False).status_code == 400
    page = client.get("/settings").text
    assert "Выходные: первым не писать" in page and "Считать часы молчания только в рабочее время" in page


def test_the_employee_card_keeps_the_address_and_the_silence_rule(office, client):
    page = client.get(f"/employees?edit={office['anna']}&tab=contacts").text
    assert "Как обращаться" in page and "Если молчит — сообщать" in page
    base = {"id": str(office["anna"]), "name": "Анна Смирнова", "telegram_manual": "@anna_smirnova", "active": "on",
            "talk_form": "1", "address_as": "Анна Сергеевна", "address_form": "вы",
            "silence_contact_id": str(office["petya"]), "silence_hours": "6", "silence_reminders": "0"}
    assert client.post("/employees", data=base, follow_redirects=False).status_code == 303
    with SessionLocal() as s:
        anna = s.get(Employee, office["anna"])
        assert (anna.address_as, anna.address_form, anna.silence_contact_id, anna.silence_hours,
                anna.silence_reminders) == ("Анна Сергеевна", "вы", office["petya"], 6, 0)
    assert client.post("/employees", data={**base, "silence_contact_id": str(office["anna"])},
                       follow_redirects=False).status_code == 400
    card(office["ivan"], telegram_chat_id="")
    assert client.post("/employees", data={**base, "silence_contact_id": str(office["ivan"])},
                       follow_redirects=False).status_code == 400
    assert client.post("/employees", data={**base, "silence_contact_id": "", "silence_hours": "", "address_form": "оно"},
                       follow_redirects=False).status_code == 303
    with SessionLocal() as s:
        anna = s.get(Employee, office["anna"])
        assert anna.silence_contact_id is None and anna.silence_hours is None and anna.address_form == ""


def test_the_department_keeps_its_silence_rule(office, client):
    data = {"name": "Бухгалтерия", "silence_form": "1", "silence_contact_id": str(office["petya"]),
            "silence_hours": "8", "silence_reminders": "1"}
    assert client.post("/departments", data=data, follow_redirects=False).status_code == 303
    with SessionLocal() as s:
        dept = s.query(Department).filter_by(name="Бухгалтерия").one()
        assert (dept.silence_contact_id, dept.silence_hours, dept.silence_reminders) == (office["petya"], 8, 1)
        dept_id = dept.id
    page = client.get(f"/departments?view=list&edit={dept_id}").text
    assert "Если сотрудник отдела не отвечает ассистенту" in page
    assert client.post("/departments", data={**data, "id": str(dept_id), "silence_hours": "abc"},
                       follow_redirects=False).status_code == 400
