"""Checking dialogs: about fifty situations of a test company answered by several models; the code checks every answer,
the page compares quality with the price a month, the director turns bad real answers into new checks."""
import json

import pytest

from app import agent, agent_checks, agent_skills, config, llm, settings_store
from app.db import AgentCheckCase, AgentCheckResult, AgentCheckRun, ConversationMessage, SessionLocal
from test_ai_mode import FakeBackend, FakeModel, incoming, make_meeting, model, tgacc  # noqa: F401  (fixtures)

M1, M2 = "good/model-a", "plain/no-tools"
REAL_CHAT = llm.chat


@pytest.fixture(autouse=True)
def tools_forgotten(monkeypatch):
    monkeypatch.setattr(agent, "_NO_TOOLS", set())


@pytest.fixture
def openrouter(monkeypatch):
    """OpenRouter as the checks see it: every model says «Принял, спасибо.» and does nothing; M2 refuses functions;
    the judge agrees. A JSON answer costs $0.001, a function-call answer $0.002."""
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "test-key")
    state = {"bodies": [], "judge": {"ok": True, "why": "по сути так"}}

    class Answer:
        def __init__(self, status, data, text=""):
            self.status_code, self._data, self.text = status, data, text

        def json(self):
            return self._data

    def answer(content, cost, calls=None):
        message = {"content": content, "tool_calls": calls} if calls is not None else {"content": content}
        return Answer(200, {"choices": [{"message": message, "finish_reason": "stop"}],
                            "usage": {"cost": cost, "prompt_tokens": 1000, "completion_tokens": 30}})

    def post(url, json=None, headers=None, timeout=None):
        body = json
        if not url.endswith("/chat/completions"):
            return Answer(404, {}, "only chat here")  # the memory search goes on without its embeddings
        state["bodies"].append(body)
        system = body["messages"][0]["content"]
        if system.startswith("Ты проверяешь ответ"):
            import json as js
            return answer(js.dumps(state["judge"], ensure_ascii=False), 0.0005)
        if body.get("tools"):
            if body["model"] == M2:
                return Answer(400, {}, '{"error":{"message":"No endpoints found that support tool use"}}')
            return answer("Принял, спасибо.", 0.002, calls=[])
        import json as js
        return answer(js.dumps({"reply": "Принял, спасибо.", "actions": []}, ensure_ascii=False), 0.001)
    monkeypatch.setattr(llm.httpx, "post", post)
    return state


def ideal(spec: dict) -> dict:
    """The action a perfect model would take for an expected spec (callables become a fitting text)."""
    action = {k: (getattr(v, "example", "до 12 тысяч, прораб не дал объёмы") if callable(v) else v)
              for k, v in spec.items()}
    for name in agent_skills.SKILLS[spec["type"]].required:
        action.setdefault(name, "Уточнить у руководителя по задаче")
    return action


# ----------------------------------------------------------------------------- the situations
def test_there_are_enough_distinct_situations_in_every_topic():
    found = agent_checks.scenarios()
    assert len(found) >= 50 and len({x.id for x in found}) == len(found)
    topics = {}
    for x in found:
        topics.setdefault(x.group, []).append(x)
        assert x.role in agent_checks.ROLE_RU and x.text.strip() and x.title.strip()
        assert all(spec["type"] in agent_skills.SKILLS for spec in x.actions)
        assert set(x.allowed) <= set(agent_skills.SKILLS)
    assert len(topics) >= 8 and all(len(v) >= 4 for v in topics.values())
    ctx, person, text = found[0].build()
    json.dumps(ctx, ensure_ascii=False, default=str)
    assert agent._user_message(ctx, text, "")  # the model gets them exactly like a real message


@pytest.mark.parametrize("scenario", [x for x in agent_checks.scenarios() if x.actions], ids=lambda x: x.id)
def test_the_expected_actions_pass_the_real_checks(scenario, monkeypatch):
    """A perfect model passes: the test company is consistent with the checks the real conversation uses."""
    ctx, person, text = scenario.build()
    reply = "Какого именно? Уточни, пожалуйста." if scenario.question else "Готово."
    decision = {"reply": reply, "actions": [ideal(spec) for spec in scenario.actions]}
    monkeypatch.setattr(llm, "chat", lambda *a, **k: json.dumps(decision, ensure_ascii=False))
    answered = agent.decide(ctx, text, person, model="m/x", transport="json", approvers=agent_checks.APPROVERS)
    reasons = scenario.evaluate(answered, ctx)
    assert not [r for r in reasons if r.split(":")[0] in ("не сделал", "сделал не так", "лишнее действие", "нельзя было")]


def test_mistakes_are_named():
    remind = agent_checks.scenario("remind-me")
    ctx, person, text = remind.build()
    at = remind.actions[0]["at"]
    good = {"reply": "Хорошо, напомню завтра в 9:00.", "actions": [{"type": "remind", "to": "me", "at": at, "topic": "банк"}]}
    assert remind.evaluate(good, ctx) == []
    lazy = {"reply": "Хорошо, напомню!", "actions": []}
    reasons = remind.evaluate(lazy, ctx)
    assert any(r.startswith("не сделал: remind") for r in reasons) and any("обещание без действия" in r for r in reasons)
    wrong = {"reply": "Хорошо.", "actions": [{"type": "remind", "to": "me", "at": at.replace("09:00", "10:00"), "topic": "банк"}]}
    assert any(r.startswith("сделал не так") for r in remind.evaluate(wrong, ctx))
    ack = agent_checks.scenario("ack")
    ctx, _, _ = ack.build()
    assert ack.evaluate({"reply": "👍", "actions": []}, ctx) == []
    assert any(r.startswith("лишнее действие: note") for r in ack.evaluate({"reply": "Ок", "actions": [{"type": "note"}]}, ctx))
    assert any("слишком длинно" in r for r in ack.evaluate({"reply": "Пожалуйста! " * 40, "actions": []}, ctx))
    which = agent_checks.scenario("approve-which")
    ctx, _, _ = which.build()
    assert any("переспросить" in r for r in which.evaluate({"reply": "Отправляю.", "actions": []}, ctx))
    peek = agent_checks.scenario("rights-report")
    ctx, _, _ = peek.build()
    assert any("стеной" in r for r in peek.evaluate({"reply": "У вас нет доступа к отчёту.", "actions": []}, ctx))
    talk = agent_checks.scenario("talking")
    ctx, _, _ = talk.build()
    assert any("поздоровался" in r for r in talk.evaluate({"reply": "Привет! Лучше профнастил.", "actions": []}, ctx))


# ----------------------------------------------------------------------------- a run
def test_a_run_compares_models_ways_and_prices(openrouter, client):
    run_id = agent_checks.start([M1, M2, M1, "not a model"], ["json", "tools", "pigeon"], "sysai")
    total = len(agent_checks.scenarios())
    with SessionLocal() as s:
        run = s.get(AgentCheckRun, run_id)
        assert run.status == "done" and run.models == [M1, M2] and run.transports == ["json", "tools"]
        assert run.total == 4 * total and s.query(AgentCheckResult).filter_by(run_id=run_id).count() == 4 * total
        ack = s.query(AgentCheckResult).filter_by(run_id=run_id, case_key="ack").all()
        assert len(ack) == 4 and all(r.ok for r in ack) and all(r.cost > 0 and r.calls >= 1 for r in ack)
    data = agent_checks.summary(run_id)
    pairs = {(p["model"], p["transport"]): p for p in data["pairs"]}
    assert set(pairs) == {(M1, "json"), (M1, "tools"), (M2, "json"), (M2, "tools")}
    assert all(p["count"] == total for p in pairs.values())
    assert pairs[(M2, "tools")]["fell_back"] == total and pairs[(M1, "tools")]["fell_back"] == 0
    assert pairs[(M1, "json")]["per_100"] == pytest.approx(0.1, rel=0.2)  # ~$0.001 a message
    assert pairs[(M1, "tools")]["per_100"] > pairs[(M1, "json")]["per_100"]
    assert data["month"] == agent_checks.ASSUMED_MONTH and not data["counted"]
    assert pairs[(M1, "json")]["per_month"] == pytest.approx(pairs[(M1, "json")]["per_100"] * 10)
    assert [p for p in data["pairs"] if p.get("recommended")][0]["transport"] == "json"
    assert any(p["best"] for p in data["pairs"])
    assert {g["group"] for g in data["groups"]} == {x.group for x in agent_checks.scenarios()}
    failing = {c["key"]: c for c in data["failing"]}
    assert "remind-me" in failing and len(failing["remind-me"]["fails"]) == 4 and "ack" not in failing
    page = client.get("/skills/checks").text
    for text in ("Сравнение", M1, M2, "рекомендуем", "Где ошибаются", "По темам", f"{total} без функций",
                 "Какие ситуации проверяются", "Свои случаи", "Выбрать"):
        assert text in page, text
    # a budget below the price: nothing is recommended, every pair is over it
    settings_store.set_many({"agent_budget_month_usd": 0.5})
    data = agent_checks.summary(run_id)
    assert all(p["over_budget"] for p in data["pairs"]) and not any(p.get("recommended") for p in data["pairs"])
    assert "выше бюджета" in client.get(f"/skills/checks?run={run_id}").text


def test_the_daily_limit_stops_a_run(openrouter):
    settings_store.set_many({"daily_budget_usd": 0.0025})
    run_id = agent_checks.start([M1], ["json"])
    with SessionLocal() as s:
        run = s.get(AgentCheckRun, run_id)
        assert run.status == "stopped" and "лимит" in run.note
        assert s.query(AgentCheckResult).filter_by(run_id=run_id).count() < len(agent_checks.scenarios())


def test_a_run_needs_the_key_models_and_no_other_run(monkeypatch):
    with pytest.raises(agent_checks.CheckError, match="ключ OpenRouter"):
        agent_checks.start([M1], ["json"])
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "test-key")
    with pytest.raises(agent_checks.CheckError, match="хотя бы одну"):
        agent_checks.start(["no slash here"], ["json"])
    with pytest.raises(agent_checks.CheckError, match="хотя бы одну"):
        agent_checks.start([M1], [])
    monkeypatch.setitem(agent_checks._ACTIVE, "run_id", 999)
    with pytest.raises(agent_checks.CheckError, match="уже идёт"):
        agent_checks.start([M1], ["json"])
    monkeypatch.setitem(agent_checks._ACTIVE, "run_id", None)
    assert agent_checks.stop() is False


def test_the_page_without_a_key_explains_and_refuses(client):
    page = client.get("/skills/checks").text
    assert "Нужен ключ OpenRouter" in page and "Прогнать" in page and "Последние ответы" in page
    r = client.post("/skills/checks", data={"do": "run", "models": M1, "transport": "json"})
    assert "Нужен ключ OpenRouter" in r.text and r.url.path == "/skills/checks"


# ----------------------------------------------------------------------------- choosing
def test_pick_sets_the_model_and_the_way_and_budget_is_validated(client):
    r = client.post("/skills/checks", data={"do": "pick", "model": "google/gemini-2.5-flash", "transport": "tools"})
    assert "Модель и способ выбраны" in r.text
    assert settings_store.get("agent_model") == "google/gemini-2.5-flash" and agent_skills.transport() == "tools"
    r = client.post("/skills/checks", data={"do": "pick", "model": "rm -rf /", "transport": "tools"})
    assert "Неизвестная модель" in r.text and settings_store.get("agent_model") == "google/gemini-2.5-flash"
    client.post("/skills/checks", data={"do": "budget", "budget": "25,5"})
    assert agent_checks.budget() == 25.5
    r = client.post("/skills/checks", data={"do": "budget", "budget": "много"})
    assert "Бюджет — число" in r.text and agent_checks.budget() == 25.5
    assert client.post("/skills/checks", data={"do": "dance"}).status_code == 404


# ----------------------------------------------------------------------------- the director's own cases
def answered(tgacc, model, text="напомни мне про банк", reply="Хорошо!"):
    make_meeting(task_status="sent")
    model.decisions.append({"reply": reply, "actions": []})
    agent.handle_incoming(incoming(text=text))
    with SessionLocal() as s:
        return s.query(ConversationMessage).filter_by(chat_id="42", role="user").one().id


def test_a_bad_real_answer_becomes_a_check(tgacc, model, client):
    message_id = answered(tgacc, model)
    page = client.get("/skills/checks").text
    assert "напомни мне про банк" in page and "Плохой ответ — в проверочные" in page
    r = client.post("/skills/checks", data={"do": "mark", "message_id": str(message_id), "must": "remind",
                                           "must_not": "", "expected": "Поставить напоминание и спросить, когда"})
    assert "добавлен в проверочные" in r.text and "в проверочных" in r.text
    with SessionLocal() as s:
        case = s.query(AgentCheckCase).one()
        assert case.must == ["remind"] and case.must_not == [] and case.bad_reply == "Хорошо!"
        assert case.person["employee_name"] == "Иван Петров" and case.ctx["employee"]["name"] == "Иван Петров"
        case_id = case.id
    r = client.post("/skills/checks", data={"do": "mark", "message_id": str(message_id), "expected": " "})
    assert "как надо было" in r.text
    client.post("/skills/checks", data={"do": "toggle", "case_id": str(case_id)})
    with SessionLocal() as s:
        assert s.get(AgentCheckCase, case_id).active is False
    client.post("/skills/checks", data={"do": "delete", "case_id": str(case_id)})
    with SessionLocal() as s:
        assert s.query(AgentCheckCase).count() == 0


def test_the_judge_reads_how_it_should_have_been(tgacc, model, monkeypatch):
    message_id = answered(tgacc, model)
    case_id = agent_checks.add_case(message_id, "Поставить напоминание", must=["remind"], must_not=["team_report", "x"])
    with SessionLocal() as s:
        case = s.get(AgentCheckCase, case_id)
        s.expunge(case)
    assert case.must_not == ["team_report"]
    seen = []

    def judge_says(verdict):
        def chat(model_name, messages, **kwargs):
            seen.append(json.loads(messages[1]["content"].split("<data>")[1].split("</data>")[0]))
            if isinstance(verdict, Exception):
                raise verdict
            return json.dumps(verdict, ensure_ascii=False)
        monkeypatch.setattr(llm, "chat", chat)

    good = {"reply": "Когда напомнить?", "actions": [{"type": "remind", "to": "me", "topic": "банк"}]}
    judge_says({"ok": True, "why": "так"})
    assert agent_checks.judge(case, good) == []
    assert seen[-1]["expected"] == "Поставить напоминание" and seen[-1]["message"] == "напомни мне про банк"
    judge_says({"ok": False, "why": "не спросил время"})
    assert agent_checks.judge(case, {"reply": "Ок", "actions": [{"type": "team_report"}]}) == [
        "не сделал: remind", "нельзя было: team_report", "по мнению судьи: не спросил время"]
    judge_says(llm.LLMError("timeout"))
    assert agent_checks.judge(case, good)[0].startswith("судья недоступен")
    ctx, person, text = agent_checks._case_scenario(case)
    assert text == "напомни мне про банк" and person.employee.name == "Иван Петров" and not person.leader


def test_own_cases_join_the_run(openrouter, tgacc, model, monkeypatch):
    message_id = answered(tgacc, model)
    agent_checks.add_case(message_id, "Поставить напоминание", must=["remind"])
    monkeypatch.setattr(llm, "chat", REAL_CHAT)  # the run talks to the fake OpenRouter, not to the conversation fake
    run_id = agent_checks.start([M1], ["json"])
    with SessionLocal() as s:
        mine = s.query(AgentCheckResult).filter(AgentCheckResult.run_id == run_id,
                                                AgentCheckResult.case_key.like("case:%")).one()
        assert mine.topic == "Свои случаи" and not mine.ok and "не сделал: remind" in mine.reasons
    titles = [c["title"] for c in agent_checks.summary(run_id)["failing"]]
    assert "напомни мне про банк" in titles


# ----------------------------------------------------------------------------- the conversations, out
def test_the_export_numbers_the_chats_and_the_month_counts_messages(tgacc, model, client):
    answered(tgacc, model)
    r = client.get("/skills/checks/export")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    lines = [json.loads(x) for x in r.text.splitlines()]
    assert [x["role"] for x in lines] == ["user", "assistant"] and {x["chat"] for x in lines} == {"chat-1"}
    assert lines[0]["text"] == "напомни мне про банк" and lines[0]["name"] and "42" not in r.text.replace('"', " ").split()
    assert agent_checks.monthly_messages() == (agent_checks.ASSUMED_MONTH, False)
    with SessionLocal() as s:
        s.add_all(ConversationMessage(chat_id="77", message_id=1000 + i, role="user", status="agent_done", text="ок",
                                      result={}) for i in range(30))
        s.commit()
    assert agent_checks.monthly_messages() == (31, True)
