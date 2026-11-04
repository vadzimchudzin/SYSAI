"""The memory of a whole conversation: the fresh part word for word, a retelling of the older part, old messages found by
code for a new message, and «вспомнить» by the model itself (JSON and function calls)."""
import datetime as dt
import itertools
import json

import pytest

from app import agent, agent_checks, dialog_memory, llm, settings_store
from app.db import ConversationMessage, DialogMemory, Employee, SessionLocal, now
from test_ai_mode import FakeBackend, FakeModel, _facts, incoming, make_meeting, model, tgacc  # noqa: F401  (fixtures)

_NUMBERS = itertools.count(10_000)
FILLER = ("Как там смета на кровлю?", "Смета в работе, к пятнице будет.", "Напомни Анне про счёт за бетон.",
          "Хорошо, напомню Анне завтра в 10:00.", "Пришли отчёт по команде.", "Отправил отчёт по команде.")


def talk(count: int, special: dict | None = None, chat: str = "42", days_ago: int = 30) -> list:
    """`count` earlier messages of the chat (the person and the assistant in turn); `special` puts texts at positions."""
    special = special or {}
    ids = []
    with SessionLocal() as s:
        for i in range(count):
            text = special.get(i, FILLER[i % len(FILLER)])
            row = ConversationMessage(chat_id=chat, message_id=next(_NUMBERS), role="user" if i % 2 == 0 else "assistant",
                                      status="agent_done", text=text, result={},
                                      created_at=now() - dt.timedelta(days=days_ago, minutes=count - i))
            s.add(row)
            s.flush()
            ids.append(row.id)
        s.commit()
    return ids


def last_decision_facts(model):
    return [c for c in model.calls if "Тебе пишет человек" in c["system"]][-1]["facts"]


# ----------------------------------------------------------------------------- the fresh part and the older part
def test_fresh_messages_go_word_for_word_and_older_ones_are_counted(tgacc, model):
    make_meeting(task_status="sent")
    long_text = "Подробно по кровле на складе. " + "Слой за слоем, примыкания и воронки. " * 70 + "Смету сдам к 18 мая."
    talk(60, special={54: long_text})
    model.decisions.append({"reply": "Да, помню: к 18 мая", "actions": []})
    agent.handle_incoming(incoming(text="К какому числу я обещал смету?", message_id=1))
    facts = last_decision_facts(model)
    assert len(long_text) > 2500 and {"from": "человек", "text": long_text} in facts["history"]  # not cut at 900 any more
    assert len(facts["history"]) == dialog_memory.MAX_FRESH
    assert facts["conversation"]["messages_so_far"] == 60
    assert facts["conversation"]["older_not_shown"] == 60 - dialog_memory.MAX_FRESH
    assert "dialog_memory" not in facts  # nothing retold yet (no model key): the fresh part is all there is
    assert tgacc.sent[-1] == ("42", "Да, помню: к 18 мая")


def test_a_short_conversation_has_nothing_older():
    with SessionLocal() as s:
        assert dialog_memory.window(s, "42", 10**9) == {"fresh": [], "total": 0, "summary": ""}
    talk(5)
    with SessionLocal() as s:
        seen = dialog_memory.window(s, "42", 10**9)
    assert len(seen["fresh"]) == 5 and seen["total"] == 5


def test_the_fresh_part_keeps_within_its_size():
    talk(30, special={i: "х" * 3000 for i in range(30)})
    with SessionLocal() as s:
        seen = dialog_memory.window(s, "42", 10**9)
    assert 1 <= len(seen["fresh"]) < 30 and seen["total"] == 30
    assert sum(len(dialog_memory.clip(r.text)) for r in seen["fresh"]) <= dialog_memory.FRESH_CHARS


class RetellingModel(FakeModel):
    """The conversation model plus the retelling of the older part."""

    def __init__(self, summary="Договорились: бюджет на кровлю 12 тысяч, водосток считаем отдельно.", fail=False):
        super().__init__()
        self.summary, self.fail, self.retold = summary, fail, []

    def __call__(self, model_name, messages, **kwargs):
        if messages[0]["content"] == dialog_memory.SUMMARY_PROMPT:
            self.retold.append(_facts(messages))
            if self.fail:
                raise llm.LLMError("модель недоступна")
            return json.dumps({"summary": self.summary}, ensure_ascii=False)
        return super().__call__(model_name, messages, **kwargs)


@pytest.fixture
def retelling(monkeypatch):
    fake = RetellingModel()
    monkeypatch.setattr(llm, "chat", fake)
    monkeypatch.setattr(dialog_memory.config, "llm_enabled", lambda: True)
    return fake


def test_the_older_part_is_retold_after_a_reply_and_shown_next_time(tgacc, retelling):
    make_meeting(task_status="sent")
    ids = talk(50, special={0: "Бюджет на кровлю — до 12 тысяч, так и договорились."})
    retelling.decisions.append({"reply": "Принял", "actions": []})
    agent.handle_incoming(incoming(text="Начинаю смету", message_id=1))
    # 50 earlier + the message + the reply: all but the fresh 24 are retold, in one call
    [told] = retelling.retold
    assert told["memory"] == "" and len(told["messages"]) == 52 - dialog_memory.FRESH
    assert told["messages"][0] == {"when": told["messages"][0]["when"], "from": "человек",
                                   "text": "Бюджет на кровлю — до 12 тысяч, так и договорились."}
    with SessionLocal() as s:
        stored = s.get(DialogMemory, "42")
        assert stored.summary == retelling.summary and stored.messages == 28 and stored.upto_id == ids[27]
        assert stored.error == ""
    retelling.decisions.append({"reply": "Да, до 12 тысяч", "actions": []})
    agent.handle_incoming(incoming(text="Как договаривались по бюджету?", message_id=2))
    facts = last_decision_facts(retelling)
    assert facts["dialog_memory"] == retelling.summary
    assert len(facts["history"]) == dialog_memory.FRESH and facts["conversation"]["older_not_shown"] == 28
    assert len(retelling.retold) == 1  # 26 messages after the retelling now: not yet another batch


def test_the_retelling_goes_on_from_where_it_stopped(retelling):
    talk(45)
    assert dialog_memory.refresh("42") is True
    talk(20, chat="42", days_ago=1)
    assert dialog_memory.refresh("42") is True
    first, second = retelling.retold
    assert len(first["messages"]) == 21 and second["memory"] == retelling.summary and len(second["messages"]) == 20
    with SessionLocal() as s:
        assert s.get(DialogMemory, "42").messages == 41
    assert dialog_memory.refresh("42") is False  # 24 fresh messages left


def test_a_failed_retelling_never_stops_the_reply(tgacc, retelling):
    retelling.fail = True
    make_meeting(task_status="sent")
    talk(50)
    retelling.decisions.append({"reply": "Принял", "actions": []})
    agent.handle_incoming(incoming(text="Начинаю смету", message_id=1))
    assert tgacc.sent[-1] == ("42", "Принял")
    with SessionLocal() as s:
        stored = s.get(DialogMemory, "42")
        assert stored.summary == "" and "недоступна" in stored.error
        assert s.query(ConversationMessage).filter_by(role="user", message_id=1).one().status == "agent_done"


def test_no_retelling_without_a_model(monkeypatch):
    talk(80)
    monkeypatch.setattr(llm, "chat", lambda *a, **k: pytest.fail("no model without a key"))
    assert dialog_memory.refresh("42") is False


def test_a_forgotten_retelling_is_not_written_again(client, retelling):
    with SessionLocal() as s:
        person = Employee(name="Иван Петров", telegram_chat_id="@ivan", telegram_user_id="42")
        s.add(person)
        s.commit()
        person_id = person.id
    talk(45)
    dialog_memory.refresh("42")
    page = client.get(f"/memory/people/{person_id}").text
    assert "Пересказ давней переписки" in page and retelling.summary in page
    client.post(f"/memory/people/{person_id}/forget-summary")
    with SessionLocal() as s:
        stored = s.get(DialogMemory, "42")
        assert stored.summary == "" and stored.upto_id > 0
        seen = dialog_memory.window(s, "42", 10**9)
    assert seen["summary"] == "" and len(seen["fresh"]) == dialog_memory.MAX_FRESH  # the latest, word for word
    assert "Пересказ давней переписки" not in client.get(f"/memory/people/{person_id}").text


# ----------------------------------------------------------------------------- old messages found by code
def test_old_messages_about_the_same_thing_come_with_the_new_one(tgacc, model):
    make_meeting(task_status="sent")
    talk(70, special={4: "Акт скрытых работ по Ленина 12 подписали, №47. Оригинал куда?",
                      5: "Оригинал акта №47 отдай Ольге, она подошьёт в папку объекта."})
    model.decisions += [{"reply": "Ты отдавал его Ольге", "actions": []}, {"reply": "👍", "actions": []}]
    agent.handle_incoming(incoming(text="А где сейчас оригинал того акта?", message_id=1))
    [episode] = last_decision_facts(model)["from_earlier"]
    assert [m["from"] for m in episode["messages"]] == ["человек", "ты"]
    assert "Ольге" in episode["messages"][1]["text"] and episode["when"]
    agent.handle_incoming(incoming(text="ок, спасибо", message_id=2))
    assert "from_earlier" not in last_decision_facts(model)


def test_words_are_matched_whatever_their_ending():
    assert dialog_memory.keys("Где оригинал того акта?") == ["оригин", "акт"]
    assert dialog_memory.keys("Как договаривались, помнишь?") == []
    rows = [type("R", (), {"id": i, "role": role, "text": text, "created_at": now()})()
            for i, (role, text) in enumerate([("user", "Бюджет сметы утвердили?"), ("assistant", "Да, бюджет — 12 тысяч."),
                                              ("user", "Спасибо"), ("assistant", "Пожалуйста")])]
    found = dialog_memory.search("какой был бюджетом", rows)
    assert len(found) == 1 and [m["text"] for m in found[0]["messages"]] == ["Бюджет сметы утвердили?", "Да, бюджет — 12 тысяч."]
    assert dialog_memory.search("скидка Монолита", rows) == []
    long = "Начало. " * 300 + "Номер договора 4471, подписан." + " Конец." * 300
    [hit] = dialog_memory.search("договор 4471", [type("R", (), {"id": 1, "role": "user", "text": long, "created_at": now()})()])
    assert "4471" in hit["messages"][0]["text"] and len(hit["messages"][0]["text"]) <= dialog_memory.RECALL_TEXT + 2


# ----------------------------------------------------------------------------- the model asks to remember
def test_the_model_asks_to_remember_and_answers_with_what_was_found(tgacc, model):
    make_meeting(task_status="sent")
    talk(70, special={2: "С «Монолитом» договорились: скидка 7% при оплате до 25-го числа.",
                      3: "Записал: у «Монолита» скидка 7% при оплате до 25-го."})

    def after_recall(facts):
        texts = [m["text"] for e in facts["recalled"]["found"] for m in e["messages"]]
        assert facts["recalled"]["query"] == "Монолит скидка" and any("7%" in t for t in texts)
        return {"reply": "Скидка 7%, если оплатить до 25-го", "actions": []}
    model.decisions += [{"reply": "", "actions": [{"type": "recall", "query": "Монолит скидка"}]}, after_recall]
    agent.handle_incoming(incoming(text="Напомни, на что мы там вышли с поставщиком?", message_id=1))
    assert tgacc.sent[-1] == ("42", "Скидка 7%, если оплатить до 25-го")
    conversation = [c for c in model.calls if "Тебе пишет человек" in c["system"]]
    assert len(conversation) == 2 and "Ты попросил вспомнить" in conversation[1]["user"]
    assert "recall" in conversation[0]["system"]
    with SessionLocal() as s:
        decision = s.query(ConversationMessage).filter_by(role="user", message_id=1).one().result["decision"]
    assert decision["actions"] == [] and decision["recalled"] == "Монолит скидка"


def test_remembering_happens_once_per_message(monkeypatch):
    ctx, person = agent_checks.build_ctx("employee")
    calls, looked = [], []

    def stubborn(model_name, messages, **kwargs):
        calls.append(messages)
        return json.dumps({"reply": "Сейчас посмотрю ещё", "actions": [{"type": "recall", "query": "акт"}]},
                          ensure_ascii=False)
    monkeypatch.setattr(llm, "chat", stubborn)
    decision = agent.decide(ctx, "Где тот акт?", person, model="m/x", transport="json",
                            recall=lambda query: looked.append(query) or [])
    assert len(calls) == 2 and looked == ["акт"]
    assert decision["actions"] == [] and ctx["recalled"] == {"query": "акт", "found": []}


def test_without_a_history_to_search_nothing_is_found(monkeypatch):
    ctx, person = agent_checks.build_ctx("employee")
    answers = iter([{"reply": "", "actions": [{"type": "recall", "query": "такси"}]},
                    {"reply": "Не помню такого, напомни, пожалуйста?", "actions": []}])
    monkeypatch.setattr(llm, "chat", lambda *a, **k: json.dumps(next(answers), ensure_ascii=False))
    decision = agent.decide(ctx, "Как договорились по такси?", person, model="m/x", transport="json")
    assert decision["reply"].startswith("Не помню") and ctx["recalled"]["found"] == []


class RecallTools:
    """Function calls: first «вспомнить», then the reply with what was found."""

    def __init__(self, then=None):
        self.calls = []
        self.then = then or {"content": "Скидка 7% при оплате до 25-го", "tool_calls": []}

    def __call__(self, model_name, messages, **kwargs):
        self.calls.append({"messages": [dict(m) for m in messages], **kwargs})
        if len(self.calls) == 1:
            return {"content": "", "tool_calls": [
                {"id": "r1", "name": "recall_conversation", "arguments": json.dumps({"query": "Монолит скидка"})},
                {"id": "m1", "name": "update_memory", "arguments": json.dumps({"summary": "Следит за скидками"})}]}
        return self.then if kwargs.get("tool_choice") == "auto" else {"content": "Готово", "tool_calls": []}


def test_the_model_remembers_through_a_function_call(tgacc, monkeypatch):
    make_meeting(task_status="sent")
    talk(70, special={2: "С «Монолитом» договорились: скидка 7% при оплате до 25-го числа."})
    settings_store.set_many({"agent_transport": "tools"})
    fake = RecallTools()
    monkeypatch.setattr(llm, "chat", fake)
    agent.handle_incoming(incoming(text="Что там нам обещали по оплате?", message_id=1))
    first, second = fake.calls
    assert "recall_conversation" in {t["function"]["name"] for t in first["tools"]}
    assert "recall_conversation" not in {t["function"]["name"] for t in second["tools"]}
    assert second["tool_choice"] == "auto" and [m["role"] for m in second["messages"]][-3:] == ["assistant", "tool", "tool"]
    found = json.loads(second["messages"][-2]["content"])
    assert found["query"] == "Монолит скидка" and "7%" in json.dumps(found, ensure_ascii=False)
    assert tgacc.sent[-1] == ("42", "Скидка 7% при оплате до 25-го")
    with SessionLocal() as s:
        row = s.query(ConversationMessage).filter_by(role="user", message_id=1).one()
        assert row.result["decision"]["recalled"] == "Монолит скидка" and row.result["decision"]["actions"] == []
    from app.db import PersonMemory
    with SessionLocal() as s:
        assert s.get(PersonMemory, "42").summary == "Следит за скидками"


def test_after_remembering_the_calls_still_get_their_reply(tgacc, monkeypatch):
    make_meeting(task_status="sent")
    talk(70)
    settings_store.set_many({"agent_transport": "tools"})
    fake = RecallTools(then={"content": "", "tool_calls": [
        {"id": "n1", "name": "note", "arguments": json.dumps({"text": "Скидка у Монолита 7%"})}]})
    monkeypatch.setattr(llm, "chat", fake)
    agent.handle_incoming(incoming(text="Что там нам обещали по оплате?", message_id=1))
    assert [c["tool_choice"] for c in fake.calls] == ["auto", "auto", "none"]
    assert tgacc.sent[-1] == ("42", "Готово")


# ----------------------------------------------------------------------------- the checking dialogs
def test_the_checking_dialogs_have_a_context_topic(monkeypatch):
    found = [x for x in agent_checks.scenarios() if x.group == "Контекст"]
    assert len(found) >= 5
    recall = agent_checks.scenario("ctx-recall")
    ctx, person, text = recall.build()
    assert ctx["conversation"]["older_not_shown"] == len(recall.archive) and "7%" not in json.dumps(ctx, ensure_ascii=False)
    [episode] = recall.recall("Монолит скидка")
    assert "7%" in episode["messages"][0]["text"]
    answers = iter([{"reply": "", "actions": [{"type": "recall", "query": "скидка Монолит"}]},
                    lambda facts: {"reply": "Монолит дал скидку 7% при оплате до 25-го числа.", "actions": []}
                    if facts.get("recalled", {}).get("found") else {"reply": "Не помню", "actions": []}])

    def fake(model_name, messages, **kwargs):
        item = next(answers)
        return json.dumps(item(_facts(messages)) if callable(item) else item, ensure_ascii=False)
    monkeypatch.setattr(llm, "chat", fake)
    decision = agent.decide(ctx, text, person, model="m/x", transport="json", recall=recall.recall)
    assert recall.evaluate(decision, ctx) == []
    assert recall.evaluate({"reply": "Не помню такого.", "actions": []}, ctx)  # an answer without the facts fails
    long_one = agent_checks.scenario("ctx-long-message")
    ctx, _, _ = long_one.build()
    assert len(ctx["history"][0]["text"]) > 900  # the deadline is at the end of a long message
    items = agent_checks._cases()
    assert all(len(item) == 5 for item in items)
