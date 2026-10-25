"""Sprint 11.4: access levels in the employee card, and «обещал — сделал» checked by code."""
from app import access, agent, settings_store
from app.db import Employee, SessionLocal
from test_ai_mode import FakeBackend, tgacc  # noqa: F401
from test_cases import CaseModel, cmodel, incoming  # noqa: F401


# ----------------------------------------------------------------------------- access levels
def emp(**kw):
    with SessionLocal() as s:
        e = Employee(**kw)
        s.add(e)
        s.commit()
        return e


def test_set_level_moves_the_person_between_lists():
    settings_store.set_many({"approver_chat_ids": ["@boss_rev"], "full_access_chat_ids": [], "observer_chat_ids": []})
    anna = emp(name="Анна", telegram_chat_id="@anna_w", telegram_user_id="43")
    assert access.level_of(anna) == "employee"
    access.set_level(anna, "full")
    assert settings_store.get("full_access_chat_ids") == ["@anna_w"] and access.level_of(anna) == "full"
    access.set_level(anna, "reviewer")
    assert settings_store.get("full_access_chat_ids") == [] and settings_store.get("approver_chat_ids") == ["@boss_rev", "@anna_w"]
    access.set_level(anna, "observer")
    assert access.level_of(anna) == "observer" and settings_store.get("approver_chat_ids") == ["@boss_rev"]
    access.set_level(anna, "employee")
    assert access.level_of(anna) == "employee" and settings_store.get("observer_chat_ids") == []


def test_level_found_by_numeric_id_and_removed_cleanly():
    settings_store.set_many({"full_access_chat_ids": ["43"]})
    anna = emp(name="Анна", telegram_chat_id="@anna_w", telegram_user_id="43")
    assert access.level_of(anna) == "full"
    access.set_level(anna, "employee")
    assert settings_store.get("full_access_chat_ids") == []


def test_rights_need_telegram():
    nobody = emp(name="Без телеграма")
    try:
        access.set_level(nobody, "full")
        assert False, "must refuse"
    except access.AccessError as exc:
        assert "Telegram" in str(exc)


def test_card_saves_the_level_and_shows_it(client):
    settings_store.set_many({"approver_chat_ids": [], "full_access_chat_ids": []})
    e = emp(name="Пётр", telegram_chat_id="@petya_w", position="Юрист")
    html = client.get(f"/employees?edit={e.id}").text
    assert 'name="access_level"' in html and "Наблюдатель" in html
    r = client.post("/employees", data={"id": e.id, "name": "Пётр", "telegram_manual": "@petya_w", "active": "on",
                                        "access_level": "full"}, follow_redirects=False)
    assert r.status_code == 303 and settings_store.get("full_access_chat_ids") == ["@petya_w"]
    card = client.get("/employees").text.split("<b>Пётр</b>")[1][:400]
    assert "Полный доступ" in card and "pill red" in card
    client.post("/employees", data={"id": e.id, "name": "Пётр", "telegram_manual": "@petya_w", "active": "on",
                                    "access_level": "observer"})
    card = client.get("/employees").text.split("<b>Пётр</b>")[1][:400]
    assert "Наблюдатель" in card and "pill blue" in card and settings_store.get("full_access_chat_ids") == []
    bad = client.post("/employees", data={"id": e.id, "name": "Пётр", "telegram_manual": "", "active": "on",
                                          "access_level": "full"})
    assert bad.status_code == 400


def test_observer_sees_everything_but_is_never_told_about_cases(tgacc, cmodel):
    from app import cases
    tgacc.users.update({"@obs": 70})
    settings_store.set_many({"observer_chat_ids": ["@obs"], "approver_chat_ids": ["@omg"], "director_chat_ids": []})
    agent.invalidate_roster()
    seen = {}
    cmodel.decisions.append(lambda facts: seen.update(facts) or {"reply": "Вот что по команде", "actions": []})
    agent.handle_incoming(incoming(70, "obs", text="как дела у команды?", message_id=1))
    assert "team" in seen  # full view
    assert "@obs" not in cases._leaders()


# ----------------------------------------------------------------------------- «обещал — сделал»
def test_unbacked_promise_is_detected():
    bad = agent.unbacked_promises("Спасибо, что прислал! Я передам Вадиму.", [], {})
    assert bad == ["Я передам Вадиму."]
    assert agent.unbacked_promises("Перешлю ему.", [{"type": "deliver_files"}], {}) == []
    assert agent.unbacked_promises("Напомню завтра в 9.", [{"type": "remind"}], {}) == []
    assert agent.unbacked_promises("Напомню завтра в 9.", [], {}) == ["Напомню завтра в 9."]


def test_offers_conditions_and_running_cases_are_not_promises():
    assert agent.unbacked_promises("Если пришлёшь файлы — упакую и передам.", [], {}) == []
    assert agent.unbacked_promises("Могу переслать, если нужно.", [], {}) == []
    assert agent.unbacked_promises("Я не перешлю это без разрешения.", [], {}) == []
    assert agent.unbacked_promises("Спасибо, передам Петру.", [], {"this_message_for_case": {"case_id": 1}}) == []


def test_model_is_made_to_act_or_stop_promising(tgacc, cmodel):
    tgacc.users.update({"@omg": 60})
    settings_store.set_many({"approver_chat_ids": ["@omg"]})
    agent.invalidate_roster()
    cmodel.decisions += [{"reply": "Получил, перешлю Вадиму.", "actions": []},
                         {"reply": "Получил. Кому переслать и какие файлы?", "actions": []}]
    agent.handle_incoming(incoming(60, "omg", text="вот файлы", message_id=5))
    assert tgacc.sent[-1] == ("60", "Получил. Кому переслать и какие файлы?")
    second = [c for c in cmodel.calls if "Тебе пишет человек" in c["system"]][-1]
    assert "обещаешь сделать" in second["user"]


def test_promise_that_survives_the_rewrite_is_not_sent(tgacc, cmodel):
    tgacc.users.update({"@omg": 60})
    settings_store.set_many({"approver_chat_ids": ["@omg"]})
    agent.invalidate_roster()
    cmodel.decisions += [{"reply": "Спасибо! Перешлю Вадиму.", "actions": []},
                         {"reply": "Спасибо! Перешлю Вадиму.", "actions": []}]
    agent.handle_incoming(incoming(60, "omg", text="вот файлы", message_id=6))
    assert tgacc.sent[-1] == ("60", "Спасибо!")
