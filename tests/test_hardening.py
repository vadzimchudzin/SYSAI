"""Hardening: OpenRouter spend and the daily limit, quiet hours for messages to people, the review card."""
import datetime as dt

import pytest

from app import agent, assistant, config, deadlines, delivery, llm, pipeline, settings_store, userbot
from app.db import ConversationMessage, Delivery, Employee, LlmSpend, Meeting, SessionLocal, Task
from test_ai_mode import FakeBackend, FakeModel, incoming, make_meeting, model, tgacc  # noqa: F401  (fixtures)


def utc(hour, minute=0, day=12):
    return dt.datetime(2026, 10, day, hour, minute, tzinfo=dt.timezone.utc)


@pytest.fixture
def openrouter(monkeypatch):
    """OpenRouter answers with the cost the service reports in `usage.cost`."""
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "test-key")
    state = {"cost": 0.02, "calls": 0}

    class Answer:
        status_code = 200
        text = ""

        def json(self):
            return {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}], "usage": {"cost": state["cost"]}}

    def post(url, json=None, headers=None, timeout=None):
        state["calls"] += 1
        return Answer()
    monkeypatch.setattr(llm.httpx, "post", post)
    return state


# ----------------------------------------------------------------------------- spend and the daily limit
def test_every_answer_records_what_it_cost(openrouter):
    assert llm.chat("model/a", [{"role": "user", "content": "hi"}]) == "ok"
    assert llm.spent_today() == pytest.approx(0.02)
    with SessionLocal() as s:
        row = s.query(LlmSpend).one()
        assert row.model == "model/a" and row.cost == pytest.approx(0.02)


def test_the_daily_limit_stops_calls_for_the_rest_of_the_day(openrouter):
    settings_store.set_many({"daily_budget_usd": 0.01})
    assert llm.chat("m", [{"role": "user", "content": "1"}]) == "ok"  # 0 spent so far, the first call goes
    with pytest.raises(llm.LLMError, match=r"лимит расходов на ИИ исчерпан: \$0.02 из \$0.01"):
        llm.chat("m", [{"role": "user", "content": "2"}])
    assert openrouter["calls"] == 1  # the second request never left the service


def test_zero_means_no_limit(openrouter):
    settings_store.set_many({"daily_budget_usd": 0})
    for _ in range(3):
        llm.chat("m", [{"role": "user", "content": "x"}])
    assert openrouter["calls"] == 3


def test_yesterdays_spend_does_not_count_today(openrouter):
    with SessionLocal() as s:
        s.add(LlmSpend(day="2000-01-01", model="old", cost=100.0))
        s.commit()
    assert llm.spent_today() == 0.0
    assert llm.chat("m", [{"role": "user", "content": "x"}]) == "ok"


def test_an_answer_without_a_cost_is_not_recorded(openrouter, monkeypatch):
    openrouter["cost"] = None
    llm.chat("m", [{"role": "user", "content": "x"}])
    with SessionLocal() as s:
        assert s.query(LlmSpend).count() == 0


# ----------------------------------------------------------------------------- quiet hours
@pytest.mark.parametrize("now, opens", [
    (utc(20, 0, 12), utc(6, 0, 13)),   # 23:00 Minsk: quiet until 09:00 tomorrow
    (utc(15, 0, 12), None),            # 18:00 Minsk: the quiet hours begin at 21:00
    (utc(2, 0, 12), utc(6, 0, 12)),    # 05:00 Minsk: quiet until 09:00 today
    (utc(6, 0, 12), None),             # 09:00 Minsk: the window is open
    (utc(7, 0, 12), None),             # 10:00 Minsk
])
def test_the_quiet_window_is_21_to_9_in_the_service_time_zone(now, opens):
    assert userbot.quiet_until(now) == opens


def test_quiet_hours_are_off_in_tests_unless_asked_for():
    assert userbot.quiet_until() is None


def test_equal_hours_mean_no_quiet_time():
    settings_store.set_many({"quiet_from_hour": 9, "quiet_to_hour": 9})
    assert userbot.quiet_until(utc(2, 0, 12)) is None


def test_a_window_inside_one_day_works_too():
    settings_store.set_many({"quiet_from_hour": 1, "quiet_to_hour": 6})
    assert userbot.quiet_until(utc(0, 0, 12)) == utc(3, 0, 12)  # 03:00 Minsk: until 06:00 Minsk
    assert userbot.quiet_until(utc(4, 0, 12)) is None  # 07:00 Minsk


def test_a_message_to_a_person_waits_for_the_morning_and_is_not_an_attempt(tgacc, monkeypatch):
    mid = make_meeting(status="done")
    morning = utc(6, 0, 13)
    monkeypatch.setattr(userbot, "quiet_until", lambda now_utc=None: morning)
    with SessionLocal() as s:
        row_id = delivery.enqueue(s, mid, "final", "summary", "@team", {"text": "Привет"}).id
        s.commit()
    delivery.send(row_id)
    assert tgacc.sent == []
    with SessionLocal() as s:
        row = s.get(Delivery, row_id)
        assert row.status == "pending" and row.attempts == 0
        when = row.next_attempt_at if row.next_attempt_at.tzinfo else row.next_attempt_at.replace(tzinfo=dt.timezone.utc)
        assert when == morning  # SQLite hands back naive times, Postgres aware ones
        assert "Тихие часы" in row.error and "09:00 13.10" in row.error
    monkeypatch.setattr(userbot, "quiet_until", lambda now_utc=None: None)
    with SessionLocal() as s:
        s.get(Delivery, row_id).next_attempt_at = None  # the morning has come
        s.commit()
    delivery.send(row_id)
    assert tgacc.sent == [("@team", "Привет")]


def test_a_direct_send_in_quiet_hours_says_when_it_can_go(tgacc, monkeypatch):
    monkeypatch.setattr(userbot, "quiet_until", lambda now_utc=None: utc(6, 0, 13))
    with pytest.raises(userbot.QuietHours) as caught:
        userbot.send_message("@ivan", "привет")
    assert caught.value.retryable and caught.value.retry_after >= 60
    assert tgacc.sent == []


def test_replies_wait_until_the_quiet_hours_end(tgacc, model, monkeypatch):
    make_meeting(task_status="sent")
    monkeypatch.setattr(userbot, "quiet_until", lambda now_utc=None: utc(6, 0, 13))
    model.decisions.append({"reply": "Привет", "actions": []})
    agent.handle_incoming(incoming(text="привет"))
    assert tgacc.sent == [] and model.calls == []
    with SessionLocal() as s:
        row = s.query(ConversationMessage).filter_by(chat_id="42", role="user").one()
        assert row.status == "agent_pending"
        row.created_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=5)
        s.commit()
    monkeypatch.setattr(userbot, "quiet_until", lambda now_utc=None: None)
    agent.run_pending()
    assert tgacc.sent == [("42", "Привет")]


def test_the_assistant_starts_its_work_after_the_quiet_hours(tgacc, model, monkeypatch):
    with SessionLocal() as s:
        meeting = Meeting(title="Планёрка", status="done", report={"title": "Планёрка", "summary": "Итог"}, options={},
                          transcript=[{"speaker": "Спикер 1", "start": 0, "end": 10, "text": "нужен график"}])
        meeting.tasks = [Task(title="График", executor="assistant", status="assistant_todo", assignee_name="ассистент",
                              source_time="00:01", source_quote="график")]
        s.add(meeting)
        s.commit()
        task_id = meeting.tasks[0].id
    monkeypatch.setattr(userbot, "quiet_until", lambda now_utc=None: utc(6, 0, 13))
    assistant.run_task(task_id)
    assert model.calls == []
    with SessionLocal() as s:
        assert s.get(Task, task_id).status == "assistant_todo"


# ----------------------------------------------------------------------------- the review card
def test_the_review_card_lists_what_leaves_after_approval(tgacc):
    with SessionLocal() as s:
        ivan = Employee(name="Иван Петров", telegram_chat_id="@ivan")
        anna = Employee(name="Анна Орлова", telegram_chat_id="")
        meeting = Meeting(title="Планёрка", status="awaiting_approval", report={"title": "Планёрка", "summary": "Итог"}, options={})
        meeting.tasks = [
            Task(title="Смета", assignee=ivan, deadline=dt.date(2026, 10, 16)),
            Task(title="Смета 2", assignee=ivan),
            Task(title="Звонок", assignee=anna),
            Task(title="График", executor="assistant", status="assistant_todo", assignee_name="ассистент"),
            Task(title="Договор", assignee_name="кто-то"),
        ]
        s.add_all([meeting])
        s.commit()
        mid = meeting.id
    with SessionLocal() as s:
        loaded = pipeline.load(s, mid)
        plan = pipeline.review_plan(loaded, settings_store.for_meeting(loaded.options))
    people = {p["name"]: p for p in plan["people"]}
    assert people["Иван Петров"]["reachable"] and len(people["Иван Петров"]["tasks"]) == 2
    assert people["Иван Петров"]["tasks"][0]["deadline"] == deadlines.fmt(dt.date(2026, 10, 16))
    assert people["Иван Петров"]["tasks"][1]["deadline"] == "без срока"
    assert people["Анна Орлова"]["reachable"] is False
    assert [t["title"] for t in plan["assistant"]] == ["График"]
    assert [(t["title"], t["said"]) for t in plan["unassigned"]] == [("Договор", "кто-то")]
    assert plan["reviewers"] == ["@boss"] and plan["send_tasks"] is True


def test_the_meeting_page_shows_the_card_and_the_quiet_hours(client, tgacc, monkeypatch):
    mid = make_meeting()
    page = client.get(f"/meetings/{mid}").text
    assert "Что уйдёт после подтверждения" in page and "Иван Петров" in page and "Анна Орлова" in page
    assert "Сейчас тихие часы" not in page
    monkeypatch.setattr(userbot, "quiet_until", lambda now_utc=None: utc(6, 0, 13))
    assert "Сейчас тихие часы: сообщения людям уйдут после 09:00 13.10" in client.get(f"/meetings/{mid}").text


def test_the_card_is_not_shown_in_bot_mode(client):
    mid = make_meeting()
    assert "Что уйдёт после подтверждения" not in client.get(f"/meetings/{mid}").text


# ----------------------------------------------------------------------------- settings
def test_the_budget_and_quiet_hours_are_saved_and_checked(client, tgacc):
    page = client.get("/settings").text
    assert "Расходы и тихие часы" in page and "Тихие часы: не писать с" in page
    ok = client.post("/settings", data={"daily_budget_usd": "2,5", "quiet_from_hour": "22", "quiet_to_hour": "8"},
                     follow_redirects=False)
    assert ok.status_code == 303
    assert settings_store.get("daily_budget_usd") == 2.5
    assert settings_store.get("quiet_from_hour") == 22 and settings_store.get("quiet_to_hour") == 8
    assert client.post("/settings", data={"daily_budget_usd": "много"}).status_code == 400
    assert client.post("/settings", data={"quiet_from_hour": "25"}).status_code == 400
    assert client.post("/settings", data={"quiet_to_hour": "ночью"}).status_code == 400
    assert settings_store.get("quiet_from_hour") == 22  # a refused form changes nothing
