"""Plan №16: the Skills page — every built-in skill with its switch, who may use it, its mode and how often it worked,
was cancelled or failed; the assistant obeys what is set there."""
import types

import pytest

from app import agent, agent_checks, agent_skills, cases, orders, settings_store, skill_stats
from app.db import Case, ChatOrder, Note, SessionLocal
from test_ai_mode import FakeBackend, FakeModel, tgacc  # noqa: F401  (fixtures)
from test_cases import CaseModel, cmodel, incoming, office  # noqa: F401  (fixtures)

BOSS = agent_skills.Person(employee=types.SimpleNamespace(id=1), reviewer=True, full=True, director=True)


def _row(page: str, name: str) -> str:
    """The row of one built-in skill on the Skills page (from its switch to the next row)."""
    start = page.index(f'name="on_{name}"')
    end = page.find("data-sk-row", start)
    return page[start:end if end != -1 else len(page)]


def test_the_page_lists_every_skill_with_its_switches_and_its_uses(client):
    for outcome in ("done", "done", "error", "approval"):
        skill_stats.record("note", outcome, "задача уже закрыта" if outcome == "error" else "")
    skill_stats.record("confirm_order", "done")  # a companion skill is counted with the one it belongs to
    skill_stats.record("order", "cancelled")
    page = client.get("/skills").text
    for name in agent_skills.SKILLS:
        assert (f'name="on_{name}"' in page) == (name not in agent_skills.PAIRED)
    note = _row(page, "note")  # 2 done, 1 waiting for the director, 1 failed: 4 uses, the last reason shown
    assert "<b>4</b> раза" in note and "ушло на одобрение: 1" in note and "1 ошибка · последняя: задача уже закрыта" in note
    assert "отменено:" not in note and "сегодня" in note
    order = _row(page, "order")  # the start of an order (confirm_order) is counted with the order itself
    assert "<b>2</b> раза" in order and "отменено: 1" in order and "вместе с шагом «Начать поручение»" in order
    assert "не применялся" in _row(page, "remind")
    assert 'name="mode_confirm_order"' in page  # an order still may wait for the director: its start has the mode
    assert 'name="mode_make_document"' in page and 'name="mode_confirm_document"' not in page
    assert skill_stats.stats()["note"] == {"done": 2, "approval": 1, "suggested": 0, "cancelled": 0, "error": 1,
                                           "last_error": "задача уже закрыта"}


def test_switches_saved_on_the_page_are_obeyed_by_the_checks(client):
    form = {"present_switches": "1", "who_team_report": "director", "who_note": "leaders", "who_order": "anyone",
            "mode_set_deadline": "approve",
            **{f"on_{n}": "on" for n in agent_skills.SKILLS if n not in ("remind", *agent_skills.PAIRED)}}
    assert client.post("/skills/agent", data=form, follow_redirects=False).status_code == 303
    assert settings_store.get("agent_skills_off") == ["remind"]
    assert settings_store.get("agent_skill_who") == {"note": "leaders", "team_report": "director"}  # unknown dropped
    assert agent_skills.modes()["set_deadline"] == "approve"
    assert "remind" not in {t["function"]["name"] for t in agent_skills.tools(BOSS)}  # the director too
    remind = agent_checks.scenario("remind-me")
    ctx, clean, _ = remind.build()
    action = {"type": "remind", "to": "me", "at": remind.actions[0]["at"], "topic": "банк"}
    assert agent_skills.check(action, ctx, clean) == ""  # the checking dialogs keep the clean company
    assert "выключен на странице «Навыки»" in agent_skills.check(action, ctx, BOSS)
    reviewer = agent_skills.Person(reviewer=True, full=True)
    report = {"type": "team_report", "scope": "all"}
    assert "только директору" in agent_skills.check(report, ctx, reviewer)
    assert agent_skills.check(report, ctx, BOSS) == ""
    worker = agent_skills.Person(employee=types.SimpleNamespace(id=2))
    assert "только руководителям" in agent_skills.check({"type": "note", "text": "x"}, ctx, worker)
    assert agent_skills.check({"type": "note", "text": "x"}, ctx, reviewer) == ""
    # a form without the switches (an older page) leaves them alone
    client.post("/skills/agent", data={"transport": "json"}, follow_redirects=False)
    assert settings_store.get("agent_skills_off") == ["remind"]
    client.post("/skills/agent", data={"present_switches": "1", **{f"on_{n}": "on" for n in agent_skills.SKILLS}},
                follow_redirects=False)
    assert settings_store.get("agent_skills_off") == [] and settings_store.get("agent_skill_who") == {}


def test_the_assistant_declines_a_skill_turned_off_on_the_page(office, tgacc, cmodel):
    settings_store.set_many({"agent_skills_off": ["note"]})
    seen = []

    def first(facts):
        seen.append(facts.get("skills_off"))
        return {"reply": "Записал.", "actions": [{"type": "note", "text": "Анна в отпуске с понедельника"}]}
    cmodel.decisions += [first, {"reply": "Такие заметки сейчас не веду — лучше скажите руководителю.", "actions": []}]
    agent.handle_incoming(incoming(43, "anna", text="Запиши: я в отпуске с понедельника", message_id=950))
    assert seen == [["Запомнить факт"]]
    with SessionLocal() as s:
        assert s.query(Note).filter(Note.text.like("%отпуске%")).count() == 0
    assert tgacc.sent[-1][1] == "Такие заметки сейчас не веду — лучше скажите руководителю."


def test_uses_approvals_and_cancellations_are_counted(office, tgacc, cmodel):
    settings_store.set_many({"agent_skill_modes": {"note": "approve"}})
    cmodel.decisions.append({"reply": "Передал руководителю.", "actions": [{"type": "note", "text": "Нужен пропуск"}]})
    agent.handle_incoming(incoming(44, "petya", text="Запиши, что мне нужен пропуск", message_id=951))
    assert skill_stats.stats()["note"]["approval"] == 1
    with SessionLocal() as s:
        case_id = s.query(Case).filter_by(kind="skill").one().id
    assert cases.cancel(case_id, "Вадим")
    assert skill_stats.stats()["note"]["cancelled"] == 1
    settings_store.set_many({"agent_skill_modes": {}})
    cmodel.decisions.append({"reply": "Записал.", "actions": [{"type": "note", "text": "Пропуск оформлен"}]})
    agent.handle_incoming(incoming(44, "petya", text="Пропуск оформили", message_id=952))
    assert skill_stats.stats()["note"]["done"] == 1
    with SessionLocal() as s:
        order = ChatOrder(chat_id="7", payload={"kind": "task", "to": office["anna"], "title": "Акт"})
        s.add(order)
        s.commit()
        order_id = order.id
    assert orders.decide(order_id, "7", False, "Вадим") == {"cancelled": True}
    assert skill_stats.stats()["order"]["cancelled"] == 1


def test_a_failed_skill_is_counted_with_its_reason(monkeypatch):
    def boom(*args, **kwargs):
        raise agent_skills.SkillError("задача уже закрыта")
    monkeypatch.setattr(agent_skills, "execute", boom)
    with pytest.raises(agent_skills.SkillError):
        agent_skills.run({"type": "note", "text": "x"}, "42", agent_skills.Person(), "Иван")
    assert skill_stats.stats()["note"]["error"] == 1
    assert skill_stats.stats()["note"]["last_error"] == "задача уже закрыта"
    skill_stats.record("note", "strange")  # unknown outcomes are not stored
    assert sum(skill_stats.stats()["note"][k] for k in skill_stats.OUTCOMES) == 1


def test_the_page_groups_the_skills_and_sums_up_the_month(client):
    grouped = [name for _, names in agent_skills.GROUPS for name in names]
    assert sorted(grouped) == sorted(n for n in agent_skills.SKILLS if n not in agent_skills.PAIRED)  # each once
    assert len(grouped) == len(set(grouped))
    for outcome in ("done", "done", "error"):
        skill_stats.record("remind", outcome, "нет такого сотрудника" if outcome == "error" else "")
    settings_store.set_many({"agent_skills_off": ["note"], "agent_skill_modes": {"set_deadline": "approve"},
                             "agent_skill_who": {"team_report": "director"}})
    page = client.get("/skills").text
    for title, _ in agent_skills.GROUPS:
        assert title in page
    assert "Напоминания и заметки</h3><span data-sk-count>3 из 4 включено" in page  # «Запомнить факт» is off
    assert f"{len(grouped) - 1}<small> из {len(grouped)}</small>" in page  # the tile: on of all
    assert "в 1 навыке" in page and "одобрение у 1 навыка" in page
    assert 'data-sk-filter="errors"' in page and 'data-sk-filter="unused"' in page
    assert "директор не указан в" in _row(page, "team_report")  # only the director, but nobody is the director
    assert 'value="director" selected' in _row(page, "team_report") and "задумано: полный доступ" in _row(page, "team_report")
    assert "кому поручать — в карточке" in _row(page, "order")
    remind = _row(page, "remind")
    assert "<b>3</b> раза" in remind and "1 ошибка · последняя: нет такого сотрудника" in remind


def test_a_company_skill_can_be_turned_off_in_its_form(client):
    from app.db import Skill
    client.post("/skills", data={"title": "Тон", "kind": "tone", "body": "На «вы».", "active": "on", "active_shown": "1"})
    with SessionLocal() as s:
        skill = s.query(Skill).filter_by(title="Тон").one()
    assert skill.active
    r = client.post("/skills", data={"id": str(skill.id), "title": "Тон", "kind": "tone", "body": "На «вы».",
                                     "active_shown": "1"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("#company-skills")
    with SessionLocal() as s:
        assert s.get(Skill, skill.id).active is False  # an unticked switch turns it off
    client.post("/skills", data={"id": str(skill.id), "title": "Тон", "kind": "tone", "body": "На «вы»."})
    with SessionLocal() as s:
        assert s.get(Skill, skill.id).active is True  # a form without the switch keeps the old default
    page = client.get(f"/skills?edit={skill.id}").text
    assert "Изменить навык «Тон»" in page and "is-editing" in page and 'id="skill-form" open' in page
