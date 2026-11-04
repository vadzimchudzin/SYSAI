"""№20: answers about the company with the source — the marks of the model become «Источник: …» with the exact names
and days, a question without marks still gets the record it repeats, nothing for orders; the questions of the director
are asked over the real memory and judged."""
import datetime as dt
import json

import pytest

from app import agent, agent_checks, agent_skills, config, llm, memory, settings_store
from app.db import AgentCheckCase, AgentCheckResult, Employee, Meeting, Note, SessionLocal
from test_agent_checks import M1, REAL_CHAT, openrouter  # noqa: F401  (fixtures)


def facts() -> dict:
    return {"known_facts": [
        {"type": "Компания", "about": "Компания", "text": "Рабочий день с 9:00 до 18:00.", "source": "профиль компании"},
        {"type": "Заметка", "about": "Ленина 12", "text": "Ленина 12: сдачу кровли перенесли на 25 ноября",
         "cite": "совещание «Планёрка по объектам» от 10.11.2025", "url": "/meetings/7"},
        {"type": "Документ", "about": "счёт_15.pdf", "text": "документ «счёт_15.pdf»: всего к оплате 12 480,00 BYN",
         "cite": "документ «счёт_15.pdf»", "url": "/documents/3/view"}]}


def test_the_marks_become_one_source_line_with_the_exact_name_and_day():
    ctx = facts()
    reply, used = memory.cite("Сдачу кровли перенесли на 25 ноября [m2].", ctx, "Когда сдаём кровлю?")
    assert reply == "Сдачу кровли перенесли на 25 ноября.\n\nИсточник: совещание «Планёрка по объектам» от 10.11.2025"
    assert used == ["m2"] and "url" not in ctx["known_facts"][1] and ctx["_links"]["m2"] == "/meetings/7"
    reply, used = memory.cite("Кровля — 25 ноября [м2], к оплате 12 480 [m3, 1].", facts(), "Что по Ленина?")
    assert used == ["m2", "m3", "m1"] and "[" not in reply
    assert reply.endswith("Источники: совещание «Планёрка по объектам» от 10.11.2025; документ «счёт_15.pdf»; "
                          "профиль компании")


def test_a_source_named_in_words_is_not_repeated_and_square_metres_stay():
    reply, used = memory.cite("По планёрке 10.11: сдача 25 ноября [m2].", facts(), "Когда сдача?")
    assert used == ["m2"] and "Источник" not in reply
    only_one = {"known_facts": [facts()["known_facts"][0]]}
    reply, _ = memory.cite("Площадь кровли 50 [м2].", only_one, "Какая площадь?")
    assert reply == "Площадь кровли 50 [м2]."


def test_without_marks_the_record_the_answer_repeats_is_named_but_not_for_orders():
    reply, used = memory.cite("К оплате 12 480,00 BYN.", facts(), "Сколько к оплате по счёту?")
    assert used == ["m3"] and reply.endswith("Источник: документ «счёт_15.pdf»")
    assert memory.cite("Понял: Анне — оплатить 12 480 до пятницы. Делаю?", facts(), "Сколько к оплате?",
                       acting=True)[1] == []
    assert memory.cite("Хорошо, 12 480.", facts(), "Запиши: к оплате 12 480")[1] == []  # not a question
    assert memory.cite("Не знаю, в памяти этого нет.", facts(), "Какой у нас расчётный счёт?")[1] == []


def test_the_panel_link_only_for_those_who_use_the_panel(monkeypatch):
    monkeypatch.setattr(config, "PUBLIC_URL", "https://sysai.example")
    reply, _ = memory.cite("Кровля — 25 ноября [m2].", facts(), "Когда кровля?", panel=True)
    assert reply.endswith("от 10.11.2025 — https://sysai.example/meetings/7")
    reply, _ = memory.cite("Кровля — 25 ноября [m2].", facts(), "Когда кровля?", panel=False)
    assert "https://" not in reply


def test_the_conversation_gets_refs_and_meeting_sources_but_never_the_pages():
    with SessionLocal() as s:
        m = Meeting(title="Планёрка по объектам", status="done", meeting_date=dt.date(2025, 11, 10), options={})
        s.add(m)
        s.flush()
        s.add(Note(meeting_id=m.id, kind="fact", source="meeting", confidence="high",
                   text="Сдачу кровли на объекте Ленина 12 перенесли на 25 ноября"))
        row_text = "Когда сдача кровли на Ленина 12?"
        from app.db import ConversationMessage
        row = ConversationMessage(chat_id="7", message_id=1, role="user", status="agent_pending", text=row_text, result={})
        s.add(row)
        s.commit()
        ctx = agent._context(s, row, {"name": "Вадим"}, None, reviewer=True, full=True, director=True)
        mid = m.id
    found = next(f for f in ctx["known_facts"] if "кровли" in f["text"])
    assert found["cite"] == "совещание «Планёрка по объектам» от 10.11.2025" and found["ref"].startswith("m")
    assert "url" not in found and ctx["_links"][found["ref"]] == f"/meetings/{mid}"
    shown = json.loads(agent._user_message(ctx, row_text, "").split("<data>")[1].split("</data>")[0])
    assert "_links" not in shown and "ref" in shown["known_facts"][0]


def test_decide_puts_the_source_under_the_answer(monkeypatch):
    ctx, person, _ = agent_checks.scenario("src-meeting").build()
    day = agent_checks._day(12).day
    answer = {"reply": f"Кровлю сдаём {day} числа [m2].", "actions": []}
    monkeypatch.setattr(llm, "chat", lambda *a, **k: json.dumps(answer, ensure_ascii=False))
    decision = agent.decide(ctx, "Когда сдаём кровлю на Ленина 12?", person, model="m/x", transport="json")
    assert decision["sources"] == ["m2"] and "Источник: совещание «Планёрка по объектам» от " in decision["reply"]
    assert agent_checks.scenario("src-meeting").evaluate(decision, ctx) == []
    lazy = {"reply": "Что-то в ноябре, точно не помню [m7].", "actions": []}
    monkeypatch.setattr(llm, "chat", lambda *a, **k: json.dumps(lazy, ensure_ascii=False))
    decision = agent.decide(ctx, "Когда сдаём кровлю на Ленина 12?", person, model="m/x", transport="json")
    assert "[m7]" not in decision["reply"] and decision["sources"] == []


# ----------------------------------------------------------------------------- the director's questions
def test_questions_are_added_listed_and_asked_over_the_real_memory(openrouter, client, monkeypatch):  # noqa: F811
    settings_store.set_many({"company_profile": "Строим склады. Акты подписывает директор Ирина Соколова."})
    with SessionLocal() as s:
        s.add(Employee(name="Борис Смирнов", position="Прораб", telegram_chat_id="@boris"))
        s.commit()
        boris = s.query(Employee).filter_by(name="Борис Смирнов").one().id
    r = client.post("/skills/checks", data={"do": "questions", "who": "director",
                                            "lines": "Кто подписывает акты? | Директор Ирина Соколова\nбез разделителя\n"
                                                     "Какой у нас расчётный счёт? | Не знает — в памяти нет"})
    assert r.status_code == 200 and "Вопросы добавлены" in r.text
    client.post("/skills/checks", data={"do": "questions", "who": f"employee:{boris}",
                                        "lines": "Во сколько планёрка? | Не знает"})
    listed = agent_checks.questions()
    assert [q["text"] for q in listed] == ["Кто подписывает акты?", "Какой у нас расчётный счёт?", "Во сколько планёрка?"]
    assert listed[2]["who"] == "Борис Смирнов" and agent_checks.custom_cases() == []
    page = client.get("/skills/checks").text
    assert "Вопросы по компании" in page and "Кто подписывает акты?" in page and "спрашивает Борис Смирнов" in page
    assert "вопрос | правильный ответ" in client.post("/skills/checks", data={"do": "questions", "lines": "просто текст"}).text
    ctx, person, text = agent_checks._live_scenario(SessionLocal().get(AgentCheckCase, listed[0]["id"]))
    assert person.director and text == "Кто подписывает акты?"
    assert any("Ирина Соколова" in f["text"] and f["cite"] == "профиль компании" for f in ctx["known_facts"])
    ctx, person, _ = agent_checks._live_scenario(SessionLocal().get(AgentCheckCase, listed[2]["id"]))
    assert person.employee.id == boris and not person.full and "team" not in ctx
    monkeypatch.setattr(llm, "chat", REAL_CHAT)
    run_id = agent_checks.start([M1], ["json"])
    with SessionLocal() as s:
        mine = {r.case_key: r for r in s.query(AgentCheckResult).filter(AgentCheckResult.run_id == run_id,
                                                                         AgentCheckResult.case_key.like("case:%"))}
    first, unknown = mine[f"case:{listed[0]['id']}"], mine[f"case:{listed[1]['id']}"]
    assert first.topic == "Вопросы по компании" and "нет ссылки на источник" in first.reasons  # «Принял, спасибо.»
    assert "нет ссылки на источник" not in unknown.reasons  # «не знает» needs no source
    groups = {g["group"] for g in agent_checks.summary(run_id)["groups"]}
    assert "Вопросы по компании" in groups and "Ответы со ссылкой" in groups


def test_the_judge_of_a_question_sees_the_memory(monkeypatch):
    seen = []

    def chat(model, messages, **kw):
        seen.append(json.loads(messages[1]["content"].split("<data>")[1].split("</data>")[0]))
        return json.dumps({"ok": False, "why": "дата не та"}, ensure_ascii=False)
    monkeypatch.setattr(llm, "chat", chat)
    case = AgentCheckCase(text="Когда кровля?", expected="25 ноября", ctx={"live": True}, person={})
    reasons = agent_checks.judge_answer(case, {"reply": "20 ноября\n\nИсточник: совещание", "sources": ["m2"]}, facts())
    assert reasons == ["по мнению судьи: дата не та"]
    assert seen[0]["expected"] == "25 ноября" and seen[0]["known_facts"][1]["text"].startswith("Ленина 12")
    assert agent_checks.judge_answer(case, {"reply": ""}, facts()) == ["пустой ответ"]
