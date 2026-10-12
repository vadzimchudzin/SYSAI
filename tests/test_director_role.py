"""The AI employee answers to the director: only the director sees everybody and gives orders; everybody else is declined gently."""
import pytest
from fastapi.testclient import TestClient

from app import agent, settings_store
from app.db import ConversationMessage, Reminder, SessionLocal
from test_ai_mode import incoming, make_meeting, tgacc  # noqa: F401  (fixture)
from test_voice_reminders import director as boss_says, everyone, model, remind, reminder_rows  # noqa: F401  (fixture)

CEO = {"user_id": 9, "username": "ceo", "name": "Борис Директор"}
SOFT = "Такие просьбы я по запросам коллег не выполняю, лучше к руководителю. Зато могу напомнить вам самому."


def set_director(tgacc, refs=("@ceo",)):
    tgacc.users["@ceo"] = 9
    settings_store.set_many({"director_chat_ids": list(refs)})
    agent.invalidate_roster()


def ask(text, message_id=1, **who):
    agent.handle_incoming(incoming(text=text, message_id=message_id, **(who or CEO)))


def contexts(model):
    return [c["facts"] for c in model.calls if "roles" in c["facts"]]


# ----------------------------------------------------------------------------- who is who
def test_the_director_is_served_even_when_nobody_listed_him_as_anything_else(tgacc, model):
    set_director(tgacc)
    settings_store.set_many({"approver_chat_ids": [], "full_access_chat_ids": []})
    agent.invalidate_roster()
    assert agent.wants(incoming(**CEO))
    assert not agent.wants(incoming(user_id=55, username="stranger"))


def test_the_director_sees_the_whole_team_and_carries_both_roles(tgacc, model):
    make_meeting()
    set_director(tgacc)
    model.decisions.append({"reply": "Слушаю", "actions": []})
    ask("кто чем занят?")
    ctx = contexts(model)[-1]
    assert ctx["roles"] == ["reviewer", "director"] and ctx["director_mode"] is True
    assert {p["name"] for p in ctx["team"]} == {"Иван Петров", "Анна Орлова"}


def test_a_reviewer_who_is_not_the_director_keeps_the_review_but_loses_the_team(tgacc, model):
    make_meeting()
    set_director(tgacc)                                   # the account fixture lists @boss (id 7) as a reviewer
    model.decisions.append({"reply": "Вижу черновик", "actions": []})
    ask("что на проверке?", user_id=7, username="boss", name="Заместитель")
    ctx = contexts(model)[-1]
    assert ctx["roles"] == ["reviewer"] and "team" not in ctx
    assert ctx["meetings_awaiting_approval"] and ctx["director_mode"] is True


def test_a_full_access_contact_loses_the_team_once_there_is_a_director(tgacc, model):
    make_meeting()
    settings_store.set_many({"full_access_chat_ids": ["@helper"]})
    set_director(tgacc)
    model.decisions.append({"reply": "Привет", "actions": []})
    ask("привет", user_id=12, username="helper", name="Помощник")
    ctx = contexts(model)[-1]
    assert "team" not in ctx and ctx["roles"] == [] and ctx["director_mode"] is True      # nothing of the team is shown any more


def test_without_a_director_the_old_rights_stay(tgacc, model):
    make_meeting()
    model.decisions.append({"reply": "Вот команда", "actions": []})
    ask("кто чем занят?", user_id=7, username="boss", name="Проверяющий")
    ctx = contexts(model)[-1]
    assert ctx["director_mode"] is False and {p["name"] for p in ctx["team"]} == {"Иван Петров", "Анна Орлова"}


# ----------------------------------------------------------------------------- orders
def test_the_director_orders_a_reminder_for_everybody(tgacc, model):
    make_meeting()
    set_director(tgacc)
    model.decisions.append(lambda facts: remind(everyone(facts), reply="Напомню всем"))
    ask("напомни всем о совещании")
    assert ("9", "Напомню всем") in tgacc.sent and sorted(reminder_rows()[0][3]) == ["@anna", "@ivan"]


def test_the_director_asks_for_the_team_report(tgacc, model):
    make_meeting()
    set_director(tgacc)
    model.decisions.append({"reply": "Вот отчёт", "actions": [{"type": "team_report", "scope": "all"}]})
    ask("пришли отчёт кто что делает")
    assert len(tgacc.files) == 1 and tgacc.files[0][0] == "9"


# ----------------------------------------------------------------------------- the gentle no
def test_a_colleague_who_orders_a_reminder_is_declined_gently_and_nothing_is_set(tgacc, model):
    make_meeting()
    set_director(tgacc)
    model.decisions += [lambda facts: remind(["x"], reply="Напомню всем"), {"reply": SOFT, "actions": []}]
    model.decisions[0] = {"reply": "Напомню всем", "actions": [{"type": "remind", "to": "all", "topic": "Совещание"}]}
    ask("напомни всем про совещание", user_id=42, username="ivan", name="Иван")
    assert tgacc.sent[-1] == ("42", SOFT)
    assert reminder_rows() == []
    second = model.calls[-1]["user"]
    assert "нельзя выполнить" in second and "по запросам коллег ты не выполняешь" in second and "Без слов «нет доступа»" in second


def test_the_team_report_is_refused_gently_for_an_employee(tgacc, model):
    make_meeting()
    set_director(tgacc)
    model.decisions += [{"reply": "Сейчас пришлю", "actions": [{"type": "team_report", "scope": "all"}]},
                        {"reply": SOFT, "actions": []}]
    ask("пришли отчёт по всем", user_id=42, username="ivan", name="Иван")
    assert tgacc.files == [] and tgacc.sent[-1] == ("42", SOFT)


def test_the_gentle_no_also_works_without_a_director_for_an_ordinary_employee(tgacc, model):
    make_meeting()
    model.decisions += [{"reply": "Напомню", "actions": [{"type": "remind", "to": "all", "topic": "Совещание"}]},
                        {"reply": SOFT, "actions": []}]
    ask("напомни всем", user_id=42, username="ivan", name="Иван")
    assert tgacc.sent[-1] == ("42", SOFT) and reminder_rows() == []
    assert "по запросам коллег ты не выполняешь" in model.calls[-1]["user"]


def test_a_mistake_in_an_allowed_action_gets_the_plain_correction_not_the_gentle_no(tgacc, model):
    make_meeting()
    set_director(tgacc)
    model.decisions += [{"reply": "Напомню", "actions": [{"type": "remind", "to": "all"}]},       # no topic: a mistake
                        {"reply": "Уточните тему", "actions": []}]
    ask("напомни всем")
    feedback = model.calls[-1]["user"]
    assert "remind: нужен topic" in feedback and "по запросам коллег" not in feedback


def test_an_employee_can_still_ask_to_be_reminded_himself(tgacc, model):
    make_meeting()
    set_director(tgacc)
    model.decisions.append(remind("me", reply="Напомню вам"))
    ask("напомни мне через час", user_id=42, username="ivan", name="Иван")
    assert tgacc.sent[-1] == ("42", "Напомню вам") and reminder_rows()[0][3] == ["42"]


def test_the_prompt_tells_the_model_how_to_say_no(tgacc, model):
    make_meeting()
    set_director(tgacc)
    model.decisions.append({"reply": "ок", "actions": []})
    ask("привет", user_id=42, username="ivan", name="Иван")
    system = model.calls[-1]["system"]
    assert "Ты подчиняешься директору" in system and "Откажи мягко" in system
    assert "«нет доступа»" in system        # named only to forbid it
    assert '"meeting":true или false' in system


def test_a_colleague_cannot_assign_tasks_or_write_notes_about_others_even_as_a_reviewer(tgacc, model):
    make_meeting()
    set_director(tgacc)
    with SessionLocal() as s:
        from app.db import Task
        task_id = s.query(Task).first().id
    model.decisions += [{"reply": "Поручил", "actions": [{"type": "assign_task", "task_id": task_id, "to": "assistant"}]},
                        {"reply": SOFT, "actions": []}]
    ask("поручи это ассистенту", user_id=7, username="boss", name="Заместитель")
    assert tgacc.sent[-1] == ("7", SOFT)


# ----------------------------------------------------------------------------- the settings page
def test_the_director_is_saved_and_shown_in_the_settings(tgacc):
    from app.main import app
    with TestClient(app) as c:
        c.post("/login", data={"username": "sysai", "password": "pw"})
        answer = c.post("/settings", data={"present_director_chat_ids": "1", "director_chat_ids_extra": "the_ceo, +375 29 123-45-67"},
                        follow_redirects=False)
        assert answer.status_code == 303
        assert settings_store.get("director_chat_ids") == ["@the_ceo", "+375291234567"]
        page = c.get("/settings").text
        assert "Директор" in page and "@the_ceo" in page


def test_a_bad_address_for_the_director_is_refused(tgacc):
    from app.main import app
    with TestClient(app) as c:
        c.post("/login", data={"username": "sysai", "password": "pw"})
        answer = c.post("/settings", data={"present_director_chat_ids": "1", "director_chat_ids_extra": "иван"}, follow_redirects=False)
        assert answer.status_code == 400 and settings_store.get("director_chat_ids") == []
