"""Orders from the chat (plan №12) and who may give them (№13): the assistant retells the order and asks «Делаю?»,
starts it only after «да», follows it up by the deadline and reports to the person who gave it; the employee card says
to whom a person may give orders and which skills are on for them."""
import datetime as dt
import types
from zoneinfo import ZoneInfo

import pytest

from app import agent, agent_checks, agent_skills, cases, config, deadlines, orders, settings_store
from app.db import (Case, CaseEvent, CaseWait, ChatOrder, Department, Employee, EmployeeDepartment, Note, SessionLocal,
                    Task, now)
from test_ai_mode import FakeBackend, FakeModel, tgacc  # noqa: F401  (fixtures)
from test_cases import CaseModel, cmodel, doc, incoming, office  # noqa: F401  (fixtures)

BOSS = (7, "boss")


def day(n: int) -> dt.date:
    return deadlines.today() + dt.timedelta(days=n)


def say(user, text, message_id):
    agent.handle_incoming(incoming(user[0], user[1], text=text, message_id=message_id))


def order_case():
    with SessionLocal() as s:
        case = s.query(Case).filter(Case.planner == "order").order_by(Case.id.desc()).first()
        return (case.id, case.status, case.kind, [(x.tool, x.status) for x in case.steps], case.task_id) if case else None


def confirm(facts, ok=True, reply="Передал, сообщу, когда будет готово."):
    waiting = facts["order_to_confirm"]
    return {"reply": reply, "actions": [{"type": "confirm_order", "order_id": waiting["id"], "ok": ok}]}


def at_local(date: dt.date, hour: int):
    return dt.datetime.combine(date, dt.time(hour, 0), ZoneInfo(config.TIMEZONE)).astimezone(dt.timezone.utc)


# ----------------------------------------------------------------------------- the rights
def test_whom_a_person_may_give_orders_to():
    worker = agent_skills.Person(employee=types.SimpleNamespace(id=2))
    assert worker.order_scope == "self"
    assert agent_skills.Person(full=True).order_scope == "all"
    assert agent_skills.Person(employee=types.SimpleNamespace(id=2), scope="department").order_scope == "department"
    assert agent_skills.Person(full=True, scope="self").order_scope == "self"   # the card narrows the level
    assert agent_skills.Person(director=True, scope="self").order_scope == "all"  # never for the director

    ctx, boss = agent_checks.build_ctx("director")
    task = {"type": "order", "kind": "task", "to": 3, "title": "Купить перфоратор", "deadline": day(3).isoformat()}
    assert agent_skills.check(task, ctx, boss) == ""
    assert "срок" in agent_skills.check({**task, "deadline": None}, ctx, boss)
    assert "прошёл" in agent_skills.check({**task, "deadline": day(-1).isoformat()}, ctx, boss)
    assert "Telegram" in agent_skills.check({**task, "to": 7}, ctx, boss)  # Дмитрий has no contact
    assert "forward_to" in agent_skills.check({**task, "forward_to": 2}, ctx, boss)
    request = {"type": "order", "kind": "request", "to": 1, "title": "Акт сверки", "forward_to": 1}
    assert "не пересылают" in agent_skills.check(request, ctx, boss)
    assert agent_skills.check({**request, "forward_to": 2}, ctx, boss) == ""
    assert "kind" in agent_skills.check({**task, "kind": "dance"}, ctx, boss)

    ctx, head = agent_checks.build_ctx("head")  # Дмитрий: his department is Сергей Волков and himself
    assert {p["id"] for p in ctx["team"]} == {5, 7} and ctx["order_scope"] == "department" and ctx["refer_to"]
    assert all("open_tasks" in p for p in ctx["team"])
    assert agent_skills.check({**task, "to": 5}, ctx, head) == ""
    assert not agent_skills.denied({**task, "to": 5}, head)
    assert agent_skills.denied({**task, "to": 1}, head) and agent_skills.check({**task, "to": 1}, ctx, head)
    assert agent_skills.check({"type": "remind", "to": [5], "topic": "допуск", "at": None}, ctx, head) == ""
    assert agent_skills.denied({"type": "remind", "to": [1], "topic": "x"}, head)
    assert agent_skills.denied({"type": "remind", "to": "all", "topic": "x"}, head)

    ctx, worker = agent_checks.build_ctx("employee")
    assert "team" not in ctx and ctx["order_scope"] == "self"
    mine = {**task, "to": "me"}
    assert agent_skills.check(mine, ctx, worker) == "" and not agent_skills.denied(mine, worker)
    assert agent_skills.personal({**task, "to": 2}, worker)["to"] == "me"  # his own ID is «себе»
    assert "только себе" in agent_skills.check({**task, "to": 1}, ctx, worker) and agent_skills.denied({**task, "to": 1}, worker)
    assert "задачу" in agent_skills.check({**mine, "kind": "ask"}, ctx, worker)
    assert agent_skills.check({"type": "remind", "to": [1], "topic": "x"}, ctx, worker).startswith("remind: сотрудник")


def test_skills_turned_off_in_the_card_are_neither_offered_nor_done():
    ctx, person = agent_checks.build_ctx("full_noremind")
    names = {t["function"]["name"] for t in agent_skills.tools(person)}
    assert "remind" not in names and "cancel_reminder" not in names and "team_report" in names
    assert ctx["skills_off"] == ["Напомнить", "Отменить напоминание"]
    remind = {"type": "remind", "to": [1], "topic": "счёт", "at": None}
    assert "выключ" in agent_skills.check(remind, ctx, person) and agent_skills.denied(remind, person)
    no_orders = agent_skills.Person(full=True, skills=frozenset({"note"}))
    names = {t["function"]["name"] for t in agent_skills.tools(no_orders)}
    assert "order" not in names and "confirm_order" not in names  # the pair goes with its skill
    assert agent_skills.allowed_skill("confirm_order", agent_skills.Person(full=True, skills=frozenset({"order"})))


def test_the_card_rights_reach_the_person(office):
    with SessionLocal() as s:
        montage = Department(name="Монтаж")
        s.add(montage)
        s.flush()
        s.add_all([EmployeeDepartment(employee_id=office["ivan"], department_id=montage.id, is_primary=True),
                   EmployeeDepartment(employee_id=office["petya"], department_id=montage.id)])
        ivan = s.get(Employee, office["ivan"])
        ivan.order_scope, ivan.skills_allowed = "department", ["note", "order", "remind"]
        s.commit()
        person = agent_skills.person_of(s, s.get(Employee, office["ivan"]))
    assert person.order_scope == "department" and person.team == {office["ivan"], office["petya"]}
    assert person.skills == {"note", "order", "remind"} and not agent_skills.allowed_skill("set_deadline", person)
    with SessionLocal() as s:  # the head of a department sends people beyond his rights to the reviewers
        assert agent._refer_to(s, s.get(Employee, office["petya"])) == [{"name": "Иван Петров", "role": "руководитель: Монтаж"}]


def test_the_card_saves_whom_the_person_may_give_orders_to(client):
    with SessionLocal() as s:
        e = Employee(name="Пётр", telegram_chat_id="@petya_w")
        s.add(e)
        s.commit()
        employee_id = e.id
    html = client.get(f"/employees?edit={employee_id}").text
    assert 'name="order_scope"' in html and 'name="skill_allowed"' in html and "Поручение из чата" in html
    names = [n for n in agent_skills.SKILLS if n not in agent_skills.PAIRED]
    base = {"id": employee_id, "name": "Пётр", "telegram_manual": "@petya_w", "active": "on", "skills_form": "1"}
    r = client.post("/employees", data={**base, "order_scope": "department",
                                        "skill_allowed": [n for n in names if n != "remind"]}, follow_redirects=False)
    assert r.status_code == 303
    with SessionLocal() as s:
        e = s.get(Employee, employee_id)
        assert e.order_scope == "department" and "remind" not in e.skills_allowed and "order" in e.skills_allowed
    assert "выключено: Напомнить" in client.get(f"/employees?edit={employee_id}").text
    client.post("/employees", data={**base, "order_scope": "nonsense", "skill_allowed": names})
    with SessionLocal() as s:
        e = s.get(Employee, employee_id)
        assert e.order_scope == "" and e.skills_allowed is None  # everything on = by the level
    client.post("/employees", data={"id": employee_id, "name": "Пётр", "telegram_manual": "@petya_w", "active": "on"})
    with SessionLocal() as s:
        assert s.get(Employee, employee_id).skills_allowed is None  # a form without the section changes nothing


# ----------------------------------------------------------------------------- the director gives an order
def test_an_order_starts_after_yes_and_the_director_hears_the_result(office, tgacc, cmodel):
    deadline = day(3)
    ivan = office["ivan"]
    cmodel.decisions.append({"reply": f"Понял: Иван Петров — проверить смету до {deadline:%d.%m}. Делаю?",
                             "actions": [{"type": "order", "kind": "task", "to": ivan, "title": "Проверить смету",
                                          "details": "по кровле", "deadline": deadline.isoformat()}]})
    say(BOSS, f"Поручи Ивану проверить смету по кровле до {deadline:%d.%m}", 1)
    assert tgacc.sent[-1] == ("7", f"Понял: Иван Петров — проверить смету до {deadline:%d.%m}. Делаю?")
    assert order_case() is None and not any(r == "@ivan" for r, _ in tgacc.sent)  # nothing before «да»
    with SessionLocal() as s:
        order = s.query(ChatOrder).one()
        assert order.status == "pending" and order.payload["to"] == ivan and order.chat_id == "7"

    seen = {}
    cmodel.decisions.append(lambda facts: seen.update(facts) or confirm(facts))
    say(BOSS, "да", 2)
    assert seen["order_to_confirm"]["to"] == "Иван Петров" and seen["order_to_confirm"]["deadline"] == deadline.isoformat()
    case_id, status, kind, steps, task_id = order_case()
    assert kind == "order" and status == "waiting"
    assert [t for t, _ in steps] == ["send_message", "wait_reply", "report", "close"]
    assert tgacc.sent[-2][0] == "@ivan" and tgacc.sent[-2][1].startswith("[order]")
    assert cmodel.writes[-1]["purpose"] == "order" and cmodel.writes[-1]["requested_by"] == "boss"
    assert cmodel.writes[-1]["deadline"]
    with SessionLocal() as s:
        task = s.get(Task, task_id)
        assert (task.assignee_id, task.status, task.deadline, task.executor) == (ivan, "sent", deadline, "person")
        assert task.description == "по кровле" and "boss" in task.source_quote
        assert s.query(ChatOrder).one().status == "started"
        wait = s.query(CaseWait).one()
        assert wait.expects == "done" and wait.employee_id == ivan
        assert s.query(Note).filter_by(employee_id=ivan, kind="commitment").count() == 1

    # Иван sees the order among what the assistant waits from him and says it is done
    cmodel.decisions.append(lambda facts: {
        "reply": "Отлично, передам.",
        "actions": [{"type": "task_status", "task_id": facts["assistant_waiting_for_you"][0]["task_id"], "status": "done",
                     "note": "проверил, ошибок нет"}]})
    say((42, "ivan"), "Смету проверил, ошибок нет", 3)
    case_id, status, kind, steps, _ = order_case()
    assert status == "done" and all(st == "done" for _, st in steps)
    assert ("7", next(t for r, t in tgacc.sent if r == "7" and t.startswith("[order_report]"))) in tgacc.sent
    report = next(w for w in cmodel.writes if w["purpose"] == "order_report")
    assert report["answers"] == [{"from": "Иван Петров", "text": "проверил, ошибок нет"}]
    with SessionLocal() as s:
        assert s.get(Task, task_id).status == "done"
        kinds = [e.kind for e in s.query(CaseEvent).filter_by(case_id=case_id).order_by(CaseEvent.id)]
    assert "told_requester" in kinds and kinds[-1] == "done"


def test_no_and_a_correction(office, tgacc, cmodel):
    ivan = office["ivan"]
    order = {"type": "order", "kind": "task", "to": ivan, "title": "Купить перфоратор", "deadline": day(4).isoformat()}
    cmodel.decisions.append({"reply": "Понял: Иван — купить перфоратор. Делаю?", "actions": [order]})
    say(BOSS, "Поручи Ивану купить перфоратор", 1)
    cmodel.decisions.append({"reply": "Понял: Иван — купить перфоратор до послезавтра. Делаю?",
                             "actions": [{**order, "deadline": day(2).isoformat()}]})
    say(BOSS, "нет, пусть до послезавтра", 2)
    with SessionLocal() as s:
        assert [o.status for o in s.query(ChatOrder).order_by(ChatOrder.id)] == ["replaced", "pending"]
    cmodel.decisions.append(lambda facts: confirm(facts, ok=False, reply="Хорошо, отменил."))
    say(BOSS, "а, не надо, отбой", 3)
    with SessionLocal() as s:
        assert [o.status for o in s.query(ChatOrder).order_by(ChatOrder.id)] == ["replaced", "cancelled"]
    assert order_case() is None and not any(r == "@ivan" for r, _ in tgacc.sent)
    with pytest.raises(orders.OrderError):  # an old order cannot be started any more
        orders.decide(2, "7", True)


def test_an_order_without_a_question_gets_one(office, tgacc, cmodel):
    order = {"type": "order", "kind": "task", "to": office["ivan"], "title": "Купить перфоратор",
             "deadline": day(4).isoformat()}
    cmodel.decisions += [{"reply": "Поручу Ивану купить перфоратор.", "actions": [order]},
                         {"reply": "Поручу Ивану купить перфоратор.", "actions": [order]}]
    say(BOSS, "Поручи Ивану купить перфоратор до пятницы", 1)
    assert tgacc.sent[-1] == ("7", "Поручу Ивану купить перфоратор. Делаю?")
    decision_calls = [c for c in cmodel.calls if "Тебе пишет человек" in c["system"]]
    assert "Делаю?" in decision_calls[-1]["user"]  # the model was told to ask first


def test_ask_a_colleague_and_bring_the_answer(office, tgacc, cmodel):
    cmodel.decisions.append({"reply": "Понял: узнать у Анны, когда придёт счёт. Делаю?",
                             "actions": [{"type": "order", "kind": "ask", "to": office["anna"],
                                          "title": "Когда придёт счёт от «Монолита»"}]})
    say(BOSS, "Узнай у Анны, когда придёт счёт от «Монолита»", 1)
    cmodel.decisions.append(confirm)
    say(BOSS, "да", 2)
    case_id, status, kind, steps, task_id = order_case()
    assert kind == "ask" and status == "waiting"
    assert any(r == "@anna" and t.startswith("[ask]") for r, t in tgacc.sent)
    with SessionLocal() as s:
        task = s.get(Task, task_id)
        assert task.executor == "assistant" and task.action_type == "ask" and task.meeting_id is None
    say((43, "anna"), "Счёт придёт в четверг", 3)
    case_id, status, kind, steps, _ = order_case()
    assert status == "done"
    assert any(r == "7" and t.startswith("[order_report]") for r, t in tgacc.sent)
    report = next(w for w in cmodel.writes if w["purpose"] == "order_report")
    assert report["answers"] == [{"from": "Анна Смирнова", "text": "Счёт придёт в четверг"}]


def test_request_a_document_and_pass_it_on(office, tgacc, cmodel):
    cmodel.decisions.append({"reply": "Понял: запросить у Анны акт сверки и передать Петру. Делаю?",
                             "actions": [{"type": "order", "kind": "request", "to": office["anna"],
                                          "title": "Акт сверки с «Монолитом»", "forward_to": office["petya"]}]})
    say(BOSS, "Запроси у Анны акт сверки с «Монолитом» и передай Петру", 1)
    cmodel.decisions.append(confirm)
    say(BOSS, "делай", 2)
    case_id, status, kind, steps, _ = order_case()
    assert kind == "forward" and status == "waiting"
    assert any(r == "@anna" and t.startswith("[request]") for r, t in tgacc.sent)
    agent.handle_incoming(incoming(43, "anna", document=doc(), message_id=3))
    case_id, status, kind, steps, _ = order_case()
    assert status == "done" and tgacc.files[-1][0] == "@petya"
    assert any(r == "7" and t.startswith("[order_report]") for r, t in tgacc.sent)


def test_an_order_to_oneself_is_a_task_with_a_deadline(office, tgacc, cmodel):
    cmodel.decisions.append({"reply": f"Понял: тебе — позвонить в банк до {day(2):%d.%m}. Делаю?",
                             "actions": [{"type": "order", "kind": "task", "to": office["ivan"], "title": "Позвонить в банк",
                                          "deadline": day(2).isoformat()}]})
    say((42, "ivan"), "Поставь мне задачу позвонить в банк до послезавтра", 1)
    with SessionLocal() as s:
        assert s.query(ChatOrder).one().payload == {"kind": "task", "to": office["ivan"], "self": True,
                                                    "title": "Позвонить в банк", "deadline": day(2).isoformat()}
    cmodel.decisions.append(confirm)
    say((42, "ivan"), "да", 2)
    assert order_case() is None
    with SessionLocal() as s:
        task = s.query(Task).filter_by(title="Позвонить в банк").one()
        assert (task.assignee_id, task.deadline, task.status) == (office["ivan"], day(2), "sent")
        assert s.query(Note).filter_by(task_id=task.id, kind="commitment").count() == 1


def test_an_employee_cannot_order_a_colleague_even_if_the_model_tries(office, tgacc, cmodel):
    wrong = {"reply": "Понял: Анне — прислать акт. Делаю?",
             "actions": [{"type": "order", "kind": "task", "to": office["anna"], "title": "Прислать акт",
                          "deadline": day(2).isoformat()}]}
    seen = []
    cmodel.decisions += [lambda facts: seen.append(facts) or wrong, lambda facts: seen.append(facts) or wrong,
                         {"reply": "Это лучше через руководителя.", "actions": []}]
    say((42, "ivan"), "Поручи Анне прислать акт", 1)
    assert seen[0]["order_scope"] == "self" and "team" not in seen[0]
    with SessionLocal() as s:
        assert s.query(ChatOrder).count() == 0


# ----------------------------------------------------------------------------- following it up
def test_blocked_and_silence_reach_the_person_who_gave_the_order(office, tgacc, cmodel):
    deadline = day(3)
    cmodel.decisions.append({"reply": "Понял: Иван — проверить смету. Делаю?",
                             "actions": [{"type": "order", "kind": "task", "to": office["ivan"], "title": "Проверить смету",
                                          "deadline": deadline.isoformat()}]})
    say(BOSS, "Поручи Ивану проверить смету", 1)
    cmodel.decisions.append(confirm)
    say(BOSS, "да", 2)
    case_id, status, kind, steps, task_id = order_case()
    with SessionLocal() as s:
        wait = s.query(CaseWait).one()
        assert abs((cases._aware(wait.next_reminder_at) - at_local(deadline, 10)).total_seconds()) < 1
        assert abs((cases._aware(wait.escalate_at) - at_local(deadline, 18)).total_seconds()) < 1

    sent = len(tgacc.sent)
    cases.run_due(at_local(deadline, 8))
    assert len(tgacc.sent) == sent  # nothing before the morning of the deadline
    cases.run_due(at_local(deadline, 11))
    assert tgacc.sent[-1][0] == "@ivan" and tgacc.sent[-1][1].startswith("[remind]")
    cases.run_due(at_local(deadline, 19))
    assert tgacc.sent[-1][0] == "7" and tgacc.sent[-1][1].startswith("[order_silent]")
    silent = next(w for w in cmodel.writes if w["purpose"] == "order_silent")
    assert silent["deadline"] and silent["reminders"] == 1
    count = len(tgacc.sent)
    cases.run_due(at_local(deadline, 23))
    assert len(tgacc.sent) == count  # told once

    cmodel.decisions.append(lambda facts: {"reply": "Понял, передам.", "actions": [
        {"type": "task_status", "task_id": facts["assistant_waiting_for_you"][0]["task_id"], "status": "blocked",
         "note": "нет доступа к папке со сметой"}]})
    say((42, "ivan"), "Не могу, нет доступа к папке со сметой", 3)
    assert tgacc.sent[-2][0] == "7" and tgacc.sent[-2][1].startswith("[order_blocked]")
    assert next(w for w in cmodel.writes if w["purpose"] == "order_blocked")["note"] == "нет доступа к папке со сметой"
    assert order_case()[1] == "waiting"  # still open: a «мешает» is not the end

    with SessionLocal() as s:  # the director closes it by hand on the Progress page: the task is done too
        cases.close_manually(case_id, by="Вадим", note="Сделано, сказал по телефону")
        assert s.get(Task, task_id).status == "done"


def test_the_progress_page_marks_an_order_done(client, office, tgacc, cmodel):
    cmodel.decisions.append({"reply": "Понял: Иван — проверить смету. Делаю?",
                             "actions": [{"type": "order", "kind": "task", "to": office["ivan"], "title": "Проверить смету",
                                          "deadline": day(3).isoformat()}]})
    say(BOSS, "Поручи Ивану проверить смету", 1)
    cmodel.decisions.append(confirm)
    say(BOSS, "да", 2)
    case_id, *_ = order_case()
    html = client.get("/progress?scope=chat").text
    assert "Проверить смету" in html and "Отметить выполненным" in html
    assert cases.record_answer(case_id, "Сделал, сообщил по телефону", by="Вадим")
    assert order_case()[1] == "done"
