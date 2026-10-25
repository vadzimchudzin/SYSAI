"""Sprint 11.5: «Ну что там?» is answered from the cases this person takes part in, not guessed."""
from app import agent, assistant, cases, settings_store
from app.db import Employee, SessionLocal
from test_ai_mode import FakeBackend, tgacc  # noqa: F401
from test_cases import CaseModel, cmodel, doc, incoming, make_task  # noqa: F401
from test_delivery_cases import JPG, order, send_photos, team  # noqa: F401


def facts_of_next_answer(cmodel, reply="Сейчас расскажу"):
    seen = {}
    cmodel.decisions.append(lambda facts: seen.update(facts) or {"reply": reply, "actions": []})
    return seen


def test_vadim_asking_what_is_up_sees_his_cases(team, tgacc, cmodel):
    # 1. the assistant asked Вадим what he needs, he answered
    task_id = make_task({"vadim": team["vadim"]}, kind="ask", counterparts=(("recipient", "vadim"),), target="vadim",
                        title="Узнать у Вадима, что ему нужно")
    assistant.run_task(task_id)
    agent.handle_incoming(incoming(50, "vadim", text="нужны скриншоты, запаковать в зип", message_id=901))
    # 2. OMG sent the photos and ordered the delivery
    send_photos()
    order(cmodel, team)
    # 3. Вадим: «Ну что там?»
    seen = facts_of_next_answer(cmodel)
    agent.handle_incoming(incoming(50, "vadim", text="Ну что там?", message_id=902))
    mine = seen["my_cases"]
    titles = [c["title"] for c in mine]
    assert "Скриншоты для Вадима" in titles and "Узнать у Вадима, что ему нужно" in titles
    delivery = next(c for c in mine if c["title"] == "Скриншоты для Вадима")
    assert delivery["your_role"] == "тебе передают файлы" and delivery["status"] == "завершено"
    assert any("Отправил Вадим" in e["text"] for e in delivery["last_events"])
    assert all(e["at"] for e in delivery["last_events"])  # every event has a local time
    assert "my_cases" in agent.ACTIONS_HELP


def test_the_one_who_ordered_sees_it_too(team, tgacc, cmodel):
    send_photos()
    order(cmodel, team)
    seen = facts_of_next_answer(cmodel)
    agent.handle_incoming(incoming(60, "omg", text="ну что, отправил?", message_id=950))
    roles = {c["title"]: c["your_role"] for c in seen["my_cases"]}
    assert roles["Скриншоты для Вадима"] == "ты поручил это ассистенту"
    recent = seen["recent_cases"][0]
    assert recent["requested_by"] and recent["last_events"]


def test_open_cases_come_first_and_show_who_we_wait_for(team, tgacc, cmodel):
    task_id = make_task({"vadim": team["vadim"]}, kind="ask", counterparts=(("recipient", "vadim"),), target="vadim",
                        title="Спросить у Вадима сроки")
    assistant.run_task(task_id)
    view = cases.person_view(team["vadim"])
    assert view[0]["open"] and view[0]["waiting_for"][0]["person"] == "Вадим"
    assert view[0]["waiting_for"][0]["expects"] == "ответ"


def test_others_do_not_see_cases_they_are_not_in(team, tgacc, cmodel):
    with SessionLocal() as s:
        other = Employee(name="Аня", telegram_chat_id="@anya")
        s.add(other)
        s.commit()
        other_id = other.id
    send_photos()
    order(cmodel, team)
    assert cases.person_view(other_id, "anya-chat") == []
    assert cases.person_view(None, "") == []
