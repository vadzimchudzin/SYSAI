"""The AI employee acts through skills: a registry of what it can do, who may ask for it, in which mode it runs
(at once, with the director's approval, only a suggestion) and two ways the model asks for it (JSON or function calls)."""
import datetime as dt
import json
import types

import pytest

from app import agent, agent_checks, agent_skills, cases, config, llm, settings_store, voice
from app.db import Case, Note, Reminder, SessionLocal, Task
from test_ai_mode import FakeBackend, FakeModel, incoming, make_meeting, tgacc  # noqa: F401  (fixtures)

ANYONE = {"task_status", "note", "remind", "cancel_reminder", "silence_choice"}


class SkillModel(FakeModel):
    """The conversation fake of test_ai_mode plus the case writer (approvals, «done», «declined»)."""

    def __init__(self):
        super().__init__()
        self.writes = []

    def __call__(self, model_name, messages, **kwargs):
        system = messages[0]["content"]
        if "Ты ведёшь дело" in system:
            facts = json.loads(messages[1]["content"].split("<data>")[1].split("</data>")[0])
            self.writes.append(facts)
            return json.dumps({"text": f"[{facts['purpose']}] {facts.get('case', {}).get('title', '')}"}, ensure_ascii=False)
        return super().__call__(model_name, messages, **kwargs)


@pytest.fixture
def smodel(monkeypatch):
    fake = SkillModel()
    monkeypatch.setattr(llm, "chat", fake)
    return fake


@pytest.fixture(autouse=True)
def tools_forgotten(monkeypatch):
    monkeypatch.setattr(agent, "_NO_TOOLS", set())


def ivan_task():
    with SessionLocal() as s:
        return s.query(Task).filter_by(title="Подготовить смету").one().id


def skill_cases():
    with SessionLocal() as s:
        return [(c.id, c.status, c.autonomy) for c in s.query(Case).filter_by(kind="skill").order_by(Case.id)]


# ----------------------------------------------------------------------------- the registry
def test_every_action_is_a_skill_with_a_form_and_a_rule():
    assert set(agent_skills.SKILLS) == {
        "set_deadline", "task_status", "note", "ask_reviewer", "relay_answer", "approve_meeting", "reject_meeting",
        "assign_task", "deliver_files", "team_report", "remind", "cancel_reminder", "approve_case", "cancel_case",
        "order", "confirm_order", "silence_choice"}
    for name, skill in agent_skills.SKILLS.items():
        assert skill.title and skill.hint and skill.when and skill.who in agent_skills.WHO_RU
        assert f'"type":"{name}"' in skill.template.replace(" ", "")
        assert set(skill.required) <= set(skill.params), name
        assert skill.template in agent.ACTIONS_HELP  # the JSON answer describes every skill
    # the decisions of the director are never sent to the director for approval
    assert not any(agent_skills.SKILLS[n].modes for n in ("approve_meeting", "reject_meeting", "approve_case",
                                                          "cancel_case", "relay_answer", "assign_task"))


def test_function_definitions_follow_the_rights():
    def names(person):
        return {t["function"]["name"] for t in agent_skills.tools(person)}

    nobody = agent_skills.Person()
    assert names(nobody) == ANYONE | {"update_memory"}
    worker = agent_skills.Person(employee=types.SimpleNamespace(id=3))
    assert names(worker) == ANYONE | {"set_deadline", "ask_reviewer", "update_memory", "order", "confirm_order"}
    boss = agent_skills.Person(employee=types.SimpleNamespace(id=1), reviewer=True, full=True)
    assert names(boss) == set(agent_skills.SKILLS) | {"update_memory"}
    for tool in agent_skills.tools(boss):
        params = tool["function"]["parameters"]
        assert params["type"] == "object" and set(params["required"]) <= set(params["properties"])
        json.dumps(tool)  # goes to OpenRouter as it is


def test_function_calls_become_checked_actions():
    actions, memory = agent_skills.from_calls([
        {"id": "1", "name": "remind", "arguments": json.dumps({"to": "people", "people": ["3", 4], "topic": "акт", "at": ""})},
        {"id": "2", "name": "deliver_files", "arguments": {"to": "2", "document_ids": ["501", "#502"], "type": "x"}},
        {"id": "3", "name": "update_memory", "arguments": '{"summary": "Иван ждёт акт"}'},
        {"id": "4", "name": "task_status", "arguments": "{broken"},
        {"id": "5", "name": "team_report", "arguments": ""},
    ])
    assert actions[0] == {"type": "remind", "to": [3, 4], "topic": "акт", "at": None}
    assert actions[1] == {"type": "deliver_files", "to": 2, "document_ids": [501, 502]}
    assert actions[2]["type"] == "task_status" and actions[2]["_error"]
    assert actions[3] == {"type": "team_report", "scope": "all"}
    assert memory == "Иван ждёт акт"
    ctx, person = agent_checks.build_ctx("employee")
    assert "JSON" in agent_skills.check(actions[2], ctx, person)  # a broken call is explained to the model
    assert agent_skills.normalize({"type": "assign_task", "task_id": "12", "to": " Assistant "}) == \
        {"type": "assign_task", "task_id": 12, "to": "assistant"}
    assert agent_skills.normalize({"type": "team_report", "scope": "#5"})["scope"] == 5


def test_rights_are_checked_by_the_person():
    ctx, worker = agent_checks.build_ctx("employee")
    assert "проверяющему" in agent_skills.check({"type": "approve_meeting", "meeting_id": 8}, ctx, worker)
    assert agent_skills.denied({"type": "team_report", "scope": "all"}, worker)
    assert not agent_skills.denied({"type": "remind", "to": "me", "topic": "x"}, worker)
    assert agent_skills.check({"type": "dance"}, ctx, worker).startswith("неизвестное действие")
    ctx, boss = agent_checks.build_ctx("director")
    assert agent_skills.check({"type": "team_report", "scope": "all"}, ctx, boss) == ""
    assert boss.leader and not worker.leader


# ----------------------------------------------------------------------------- the modes
def test_modes_and_the_hint_for_the_model():
    settings_store.set_many({"agent_skill_modes": {"remind": "approve", "note": "suggest", "approve_case": "approve",
                                                   "task_status": "nonsense"}})
    modes = agent_skills.modes()
    assert modes["remind"] == "approve" and modes["note"] == "suggest" and modes["task_status"] == "auto"
    assert "approve_case" not in modes  # a decision of the director has no mode
    worker = agent_skills.Person(employee=types.SimpleNamespace(id=3))
    boss = agent_skills.Person(reviewer=True)
    assert agent_skills.mode_for("remind", worker) == "approve" and agent_skills.mode_for("remind", boss) == "auto"
    assert agent_skills.approval_hint(worker) == {"skills_with_approval": {
        "remind": "с одобрением руководителя", "note": "только предложение руководителю"}}
    assert agent_skills.approval_hint(boss) == {}


def test_a_request_waits_for_the_director_then_runs_and_the_person_hears(tgacc, smodel):
    make_meeting(task_status="sent")
    task_id = ivan_task()
    settings_store.set_many({"agent_skill_modes": {"task_status": "approve"}})
    smodel.decisions.append({"reply": "Принял, отмечу после подтверждения руководителя",
                             "actions": [{"type": "task_status", "task_id": task_id, "status": "done"}]})
    agent.handle_incoming(incoming(text="смету сделал"))
    facts = smodel.calls[0]["facts"]
    assert facts["skills_with_approval"] == {"task_status": "с одобрением руководителя"}
    with SessionLocal() as s:
        assert s.get(Task, task_id).status == "sent"  # nothing happened yet
    [(case_id, status, autonomy)] = skill_cases()
    assert status == "awaiting_approval" and autonomy == "approve"
    assert any(ref == "@boss" and text.startswith("[approval]") for ref, text in tgacc.sent)
    assert ("42", "Принял, отмечу после подтверждения руководителя") in tgacc.sent
    plan = cases.describe_plan
    with SessionLocal() as s:
        assert "выполнена" in plan(s, s.get(Case, case_id))
    assert cases.approve(case_id, "Вадим")
    with SessionLocal() as s:
        assert s.get(Task, task_id).status == "done"
        assert s.query(Note).filter_by(task_id=task_id, kind="done").count() == 1
        assert s.get(Case, case_id).status == "done"
    assert tgacc.sent[-1][0] == "42" and tgacc.sent[-1][1].startswith("[skill_done]")


def test_a_suggestion_the_director_declines_changes_nothing(tgacc, smodel):
    make_meeting(task_status="sent")
    task_id = ivan_task()
    settings_store.set_many({"agent_skill_modes": {"task_status": "suggest"}})
    smodel.decisions.append({"reply": "Передал руководителю", "actions": [
        {"type": "task_status", "task_id": task_id, "status": "blocked", "note": "нет объёмов"}]})
    agent.handle_incoming(incoming(text="застрял со сметой"))
    [(case_id, status, autonomy)] = skill_cases()
    assert status == "suggested" and autonomy == "suggest"
    assert cases.cancel(case_id, "Вадим")
    with SessionLocal() as s:
        assert s.get(Task, task_id).status == "sent"
    assert tgacc.sent[-1][0] == "42" and tgacc.sent[-1][1].startswith("[skill_declined]")


def test_what_a_leader_asks_for_runs_at_once(tgacc, smodel):
    settings_store.set_many({"agent_skill_modes": {"remind": "approve"}})
    at = (dt.date.today() + dt.timedelta(days=1)).isoformat() + " 10:00"
    smodel.decisions.append({"reply": "Напомню завтра в 10", "actions": [
        {"type": "remind", "to": "me", "at": at, "topic": "позвонить в банк"}]})
    agent.handle_incoming(incoming(user_id=7, username="boss", name="Вадим", text="напомни мне завтра в 10 про банк"))
    assert skill_cases() == []
    with SessionLocal() as s:
        assert s.query(Reminder).count() == 1


# ----------------------------------------------------------------------------- function calls
class ToolModel:
    """The model of the conversation through function calls: first the calls, then the reply that sees them accepted."""

    def __init__(self, first, second="Отметил: в работе", refuse=False):
        self.first, self.second, self.refuse = first, second, refuse
        self.calls = []

    def __call__(self, model_name, messages, **kwargs):
        self.calls.append({"messages": messages, **kwargs})
        if kwargs.get("tools"):
            if self.refuse:
                raise llm.ToolsUnsupported("no tool use")
            if kwargs.get("tool_choice") == "none":
                return {"content": self.second, "tool_calls": []}
            return self.first
        return json.dumps({"reply": "Ответ в JSON", "actions": []}, ensure_ascii=False)


def test_function_calls_are_acted_on_and_the_reply_follows(tgacc, monkeypatch):
    make_meeting(task_status="sent")
    task_id = ivan_task()
    settings_store.set_many({"agent_transport": "tools"})
    fake = ToolModel({"content": "", "tool_calls": [
        {"id": "c1", "name": "task_status", "arguments": json.dumps({"task_id": str(task_id), "status": "in_progress"})},
        {"id": "c2", "name": "update_memory", "arguments": json.dumps({"summary": "Иван взялся за смету"})}]})
    monkeypatch.setattr(llm, "chat", fake)
    agent.handle_incoming(incoming(text="начал смету"))
    first, second = fake.calls
    assert {t["function"]["name"] for t in first["tools"]} >= {"task_status", "set_deadline", "update_memory"}
    assert "approve_meeting" not in {t["function"]["name"] for t in first["tools"]}  # not the employee's skill
    assert first["tool_choice"] == "auto" and second["tool_choice"] == "none"
    assert [m["role"] for m in second["messages"]][-3:] == ["assistant", "tool", "tool"]
    with SessionLocal() as s:
        assert s.get(Task, task_id).status == "in_progress"
    assert tgacc.sent[-1] == ("42", "Отметил: в работе")
    from app.db import ConversationMessage
    with SessionLocal() as s:
        row = s.query(ConversationMessage).filter_by(chat_id="42", role="user").one()
        assert row.result["transport"] == "tools" and row.result["outcomes"] == [{"done": True}]


def test_a_model_without_function_calls_answers_in_json(tgacc, monkeypatch):
    make_meeting(task_status="sent")
    settings_store.set_many({"agent_transport": "tools"})
    fake = ToolModel(None, refuse=True)
    monkeypatch.setattr(llm, "chat", fake)
    agent.handle_incoming(incoming(text="привет"))
    assert tgacc.sent[-1] == ("42", "Ответ в JSON")
    assert agent._NO_TOOLS == {voice.model()}
    agent.handle_incoming(incoming(text="ещё раз", message_id=8))
    assert [bool(c.get("tools")) for c in fake.calls] == [True, False, False]  # remembered: no second refusal


def test_the_json_way_stays_the_default(tgacc, smodel):
    make_meeting(task_status="sent")
    smodel.decisions.append({"reply": "Привет!", "actions": []})
    agent.handle_incoming(incoming(text="привет"))
    assert agent_skills.transport() == "json" and '"actions"' in smodel.calls[0]["system"]


def test_the_chat_client_sends_functions_and_reads_the_calls(monkeypatch):
    monkeypatch.setattr(config, "OPENROUTER_API_KEY", "test-key")
    sent = []

    class Answer:
        def __init__(self, status, data, text=""):
            self.status_code, self._data, self.text = status, data, text

        def json(self):
            return self._data

    answers = [Answer(200, {"choices": [{"finish_reason": "tool_calls", "message": {"content": None, "tool_calls": [
        {"id": "x1", "type": "function", "function": {"name": "remind", "arguments": '{"to":"me","topic":"банк"}'}},
        {"id": "x2", "type": "function", "function": {"name": "note", "arguments": {"text": "dict args"}}}]}}],
        "usage": {"cost": 0.004, "prompt_tokens": 900, "completion_tokens": 40}}),
        Answer(400, {}, '{"error":{"message":"No endpoints found that support tool use"}}')]

    def post(url, json=None, headers=None, timeout=None):
        sent.append(json)
        return answers.pop(0)
    monkeypatch.setattr(llm.httpx, "post", post)
    tools = agent_skills.tools(agent_skills.Person())
    with llm.meter() as box:
        out = llm.chat("m/a", [{"role": "user", "content": "hi"}], tools=tools, tool_choice="auto")
    assert sent[0]["tools"] == tools and sent[0]["tool_choice"] == "auto" and sent[0]["usage"] == {"include": True}
    assert out["content"] == "" and [c["name"] for c in out["tool_calls"]] == ["remind", "note"]
    assert json.loads(out["tool_calls"][1]["arguments"]) == {"text": "dict args"}
    assert box == {"calls": 1, "prompt_tokens": 900, "completion_tokens": 40, "cost": pytest.approx(0.004)}
    with pytest.raises(llm.ToolsUnsupported):
        llm.chat("m/b", [{"role": "user", "content": "hi"}], tools=tools)


# ----------------------------------------------------------------------------- the Skills page
def test_the_skills_page_sets_modes_and_the_way(client):
    page = client.get("/skills").text
    assert "Что умеет ИИ-сотрудник" in page and "Проверочные диалоги" in page and "Навыки компании" in page
    assert 'name="mode_remind"' in page and 'name="mode_approve_meeting"' not in page
    r = client.post("/skills/agent", data={"mode_remind": "approve", "mode_note": "suggest", "mode_approve_case": "approve",
                                           "mode_task_status": "auto", "transport": "tools"}, follow_redirects=False)
    assert r.status_code == 303
    assert settings_store.get("agent_skill_modes") == {"remind": "approve", "note": "suggest"}
    assert agent_skills.transport() == "tools"
    page = client.get("/skills?saved=1").text
    assert "Сохранено" in page and 'value="approve" selected' in page
    client.post("/skills/agent", data={"transport": "carrier-pigeon"})
    assert agent_skills.transport() == "tools"  # an unknown way keeps the current one
