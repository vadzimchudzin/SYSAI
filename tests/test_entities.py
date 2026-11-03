"""№19: objects and clients — one card per client, object or organization with its other spellings, where it comes up
(meetings, tasks, documents, messages — with the words), facts with their source; the AI employee answers about it."""
import json

import pytest

from app import agent, entities, llm, memory, settings_store
from app.db import ConversationMessage, Document, Employee, Entity, EntityLink, Meeting, Note, SessionLocal, Task
from test_ai_mode import FakeBackend, FakeModel, incoming, model, tgacc  # noqa: F401  (fixtures)


# ----------------------------------------------------------------------------- names and cards
def test_a_name_is_compared_without_its_legal_form_and_quote_marks():
    assert entities.key("ОАО «Криница»") == entities.key('"Криница"') == entities.key("КРИНИЦА") == "криница"
    assert entities.key("Общество с ограниченной ответственностью «Монолит-Строй»") == "монолит строй"
    assert entities.key("ЧУП «Альфа»") == "альфа" and entities.key("ИП Козлов А.В.") == "козлов а в"
    assert entities.key("ООО") == ""


def test_one_card_for_one_name_or_unp_new_spellings_are_kept_look_alikes_are_offered():
    settings_store.set_many({"company_name": "СтройДом"})
    with SessionLocal() as s:
        a = entities.get_or_create(s, "ОАО «Криница»", "client")
        same = entities.get_or_create(s, "Криница", "org", "document")
        assert same.id == a.id and a.kind == "client" and a.aliases == ["Криница"]
        b = entities.get_or_create(s, "Пивзавод Криница", unp="100345678",
                                   origin={"type": "document", "id": 5, "title": "счёт.pdf", "quote": "УНП 100345678"})
        assert b.id != a.id and b.unp == "100345678" and b.sources["unp"]["id"] == 5 and b.sources["name"]["id"] == 5
        assert entities.get_or_create(s, "ОАО «Пивзавод»", unp="100 345 678").id == b.id  # the same УНП
        assert entities.get_or_create(s, "ООО") is None
        assert entities.get_or_create(s, "ООО «СтройДом»") is None  # our own company is not a client
        s.commit()
        dups = entities.duplicates(s)
    assert [({x["a"]["id"], x["b"]["id"]}, x["why"]) for x in dups] == [({a.id, b.id}, "одно название входит в другое")]


def test_mentions_understand_russian_endings_and_keep_the_sentence():
    with SessionLocal() as s:
        k = entities.get_or_create(s, "ОАО «Криница»", "client")
        o = entities.get_or_create(s, "Ленина 12", "object")
        s.commit()
        found = entities.mentions(s, "Вчера были на Ленина 12. Акт Кринице отправили! Остальное — потом.")
        assert found == {o.id: "Вчера были на Ленина 12.", k.id: "Акт Кринице отправили!"}
        assert entities.mentions(s, "Криничный переулок, Ленина 120") == {}


def test_a_duplicate_is_merged_with_its_spellings_links_and_facts():
    a_id = entities.save(name="ОАО «Криница»", kind="client", unp="100345678", by="SYSAI")
    b_id = entities.save(name="Криница-М", address="г. Минск, ул. Аранская, 15", by="SYSAI")
    with SessionLocal() as s:
        assert [(x["a"]["id"], x["b"]["id"], x["why"]) for x in entities.duplicates(s)] == [(a_id, b_id, "похожие названия")]
        entities.link(s, b_id, "document", 77, "акт Кринице-М")
        entities.link(s, a_id, "document", 77, "акт Кринице")
        entities.link(s, b_id, "task", 5, "забрать акт")
        s.commit()
    note = entities.add_fact(b_id, "Платят с задержкой в две недели", by="SYSAI")
    entities.merge(a_id, b_id, by="SYSAI")
    with SessionLocal() as s:
        a, b = s.get(Entity, a_id), s.get(Entity, b_id)
        assert b.merged_into_id == a_id and not b.active and entities.resolve(s, b).id == a_id
        assert a.aliases == ["Криница-М"] and a.address == "г. Минск, ул. Аранская, 15" and a.unp == "100345678"
        assert sorted((l.source_type, l.source_id) for l in s.query(EntityLink).filter_by(entity_id=a_id)) == [
            ("document", 77), ("task", 5)]
        assert s.get(Note, note).entity_id == a_id and entities.duplicates(s) == []
        assert entities.card(s, b_id)["merged_into"] == {"id": a_id, "name": "ОАО «Криница»"}
    with pytest.raises(entities.EntityError):
        entities.merge(a_id, a_id)
    with pytest.raises(entities.EntityError, match="уже у карточки «ОАО «Криница»»"):
        entities.save(None, name="Криница м")
    entities.delete(a_id)
    with SessionLocal() as s:
        assert s.get(Note, note).entity_id is None and s.query(EntityLink).count() == 0
        assert s.get(Entity, b_id).merged_into_id is None


# ----------------------------------------------------------------------------- where cards come from and come up
def test_a_meeting_teaches_facts_about_a_client_and_an_object_and_links_itself_and_its_tasks(monkeypatch):
    settings_store.set_many({"memory_autolearn": True})
    known = entities.save(name="ОАО «Криница»", kind="client")
    transcript = [
        {"start": 0, "end": 10, "speaker": "Вадим",
         "text": "Криница оплачивает акты с задержкой в три недели, счёт им выставляем до 25-го."},
        {"start": 10, "end": 20, "speaker": "Анна",
         "text": "По объекту на Заводской заказчик просит закончить кровлю до конца ноября."}]
    with SessionLocal() as s:
        m = Meeting(title="Планёрка", status="done", transcript=transcript, report={"summary": "Криница и Заводская"},
                    options={})
        m.tasks.append(Task(title="Выставить счёт Кринице до 25-го", status="sent"))
        s.add(m)
        s.commit()
        mid, tid = m.id, m.tasks[0].id
    proposed = {"facts": [
        {"subject": "entity", "entity_id": known, "text": "Оплачивает акты с задержкой в три недели",
         "quote": "Криница оплачивает акты с задержкой в три недели", "confidence": "high"},
        {"subject": "entity", "entity_name": "Склад на Заводской", "entity_kind": "object",
         "text": "Заказчик просит закончить кровлю до конца ноября",
         "quote": "По объекту на Заводской заказчик просит закончить кровлю до конца ноября", "confidence": "medium"},
        {"subject": "entity", "entity_name": "Монолит", "entity_kind": "contractor", "text": "Поставляет бетон",
         "quote": "Монолит возит нам бетон", "confidence": "low"}]}  # not said: neither a card nor a fact
    seen = []
    monkeypatch.setattr(llm, "chat", lambda model, messages, **k: seen.append(messages) or
                        json.dumps(proposed, ensure_ascii=False))
    assert memory.learn_and_link(mid) == 2
    assert '"name": "ОАО «Криница»"' in seen[0][1]["content"]  # the model knows the cards there are
    with SessionLocal() as s:
        cards = {e.name: e for e in s.query(Entity)}
        assert set(cards) == {"ОАО «Криница»", "Склад на Заводской"}
        place = cards["Склад на Заводской"]
        assert place.kind == "object" and place.source == "meeting" and place.sources["name"]["id"] == mid
        notes = {n.entity_id: n for n in s.query(Note).filter(Note.entity_id.isnot(None))}
        assert notes[known].text == "Оплачивает акты с задержкой в три недели" and notes[known].meeting_id == mid
        assert notes[place.id].quote.startswith("По объекту на Заводской")
        links = {(l.entity_id, l.source_type, l.source_id) for l in s.query(EntityLink)}
        assert {(known, "meeting", mid), (known, "task", tid), (place.id, "meeting", mid)} <= links
        card = entities.card(s, known)
    assert card["facts"][0]["source"] == "совещание «Планёрка»" and card["facts"][0]["url"] == f"/meetings/{mid}"
    assert card["links"]["meeting"][0]["title"] == "Планёрка" and card["links"]["task"][0]["id"] == tid


def test_new_messages_and_tasks_are_linked_and_a_new_card_looks_through_the_history():
    with SessionLocal() as s:
        s.add(ConversationMessage(chat_id="43", message_id=1, role="user", text="Когда Криница подпишет акт?",
                                  status="agent_done"))
        s.commit()
    entities.run_pending()  # no cards yet: nothing to look for
    k = entities.save(name="ОАО «Криница»", kind="client")
    with SessionLocal() as s:
        assert [l.quote for l in s.query(EntityLink).filter_by(entity_id=k)] == ["Когда Криница подпишет акт?"]
        s.add(ConversationMessage(chat_id="43", message_id=2, role="assistant", text="Криница обещала подписать в пятницу.",
                                  status="sent"))
        s.add(Task(title="Забрать у Криницы подписанный акт", status="sent"))
        s.commit()
    entities.run_pending()
    entities.run_pending()  # once
    with SessionLocal() as s:
        assert sorted(l.source_type for l in s.query(EntityLink).filter_by(entity_id=k)) == ["message", "message", "task"]


def test_the_conversation_answers_in_the_context_of_the_card(tgacc, model):
    with SessionLocal() as s:
        s.add(Employee(name="Иван Петров", telegram_chat_id="@ivan", position="Инженер"))
        s.commit()
    agent.invalidate_roster()
    k = entities.save(name="ОАО «Криница»", kind="client", unp="100345678", contacts="Ольга, +375 29 111-22-33")
    entities.add_fact(k, "Счёт выставлять до 25-го числа", by="SYSAI")
    seen = {}
    model.decisions.append(lambda facts: seen.update(facts) or {"reply": "УНП Криницы — 100345678.", "actions": []})
    agent.handle_incoming(incoming(42, "ivan", text="Какой УНП у Криницы?", message_id=11))
    card = seen["about_entities"][0]
    assert card["name"] == "ОАО «Криница»" and card["unp"] == "100345678" and card["kind"] == "Клиент"
    assert card["contacts"] == "Ольга, +375 29 111-22-33"
    assert card["facts"] == [{"text": "Счёт выставлять до 25-го числа", "source": "вручную"}]
    assert "meetings" not in card and "open_tasks" not in card  # the team's work is not shown to an employee
    assert "about_entities" in agent.GENERAL_RULES


def test_those_who_see_the_team_get_meetings_open_tasks_and_documents_of_the_card():
    k = entities.save(name="ОАО «Криница»", kind="client")
    with SessionLocal() as s:
        m = Meeting(title="Планёрка", status="done", report={"summary": "Обсудили сверку с Криницей"}, options={})
        m.tasks.append(Task(title="Акт сверки с Криницей", status="sent", assignee_name="Анна"))
        m.tasks.append(Task(title="Старый счёт Кринице", status="done"))
        s.add(m)
        s.add(Document(filename="акт_Криница.pdf", chat_id="43", message_id=5, size=1, sha256="x", kind="pdf", data=b"x",
                       text="", text_status="done", facts={"kind": "act", "fields": {"number": {"value": "7"}}}))
        s.add(Document(filename="ведомость_Криница.pdf", chat_id="43", message_id=6, size=1, sha256="y", kind="pdf",
                       data=b"y", text="", text_status="done", sensitive=True))
        s.commit()
    entities.rescan([k])
    with SessionLocal() as s:
        full = entities.for_prompt(s, "Что с Криницей?", True)[0]
        brief = entities.for_prompt(s, "Что с Криницей?", False)[0]
    assert full["meetings"] == [{"title": "Планёрка", "date": None}]
    assert [t["title"] for t in full["open_tasks"]] == ["Акт сверки с Криницей"]
    assert [d["filename"] for d in full["documents"]] == ["акт_Криница.pdf"]  # not the confidential one
    assert full["documents"][0]["what"] == "Акт выполненных работ № 7"
    assert set(brief) == {"name", "kind"}


def test_the_memory_search_finds_the_card():
    entities.save(name="ОАО «Криница»", kind="client", unp="100345678", address="г. Минск, ул. Аранская, 15")
    with SessionLocal() as s:
        item = next(i for i in memory.items(s) if i["type"] == "entity")
    assert "УНП: 100345678" in item["text"] and item["url"].startswith("/entities/")
    assert memory.search("какой УНП у Криницы", k=3)[0]["type"] == "entity"


# ----------------------------------------------------------------------------- the page
def test_the_page_makes_edits_merges_and_deletes_cards(client):
    r = client.post("/entities", data={"name": "ОАО «Криница»", "kind": "client", "aliases": "пивзавод; Криница-Минск",
                                       "unp": "100 345 678"}, follow_redirects=True)
    assert "Карточка сохранена" in r.text and 'value="100345678"' in r.text and "пивзавод, Криница-Минск" in r.text
    kid = int(r.url.path.rsplit("/", 1)[1])
    r = client.post("/entities", data={"name": "Криница", "kind": "org"}, follow_redirects=True)
    assert "такое название уже у карточки «ОАО «Криница»»" in r.text
    r = client.post("/entities", data={"name": "ООО", "kind": "org"}, follow_redirects=True)
    assert "название слишком короткое" in r.text
    r = client.post("/entities", data={"name": "Крыница", "kind": "org", "address": "Минск"}, follow_redirects=True)
    dup = int(r.url.path.rsplit("/", 1)[1])
    r = client.get("/entities")
    assert "Возможные дубли" in r.text and "похожие названия" in r.text
    r = client.post(f"/entities/{kid}/merge", data={"gone": str(dup), "back": "list"}, follow_redirects=True)
    assert "Объединено" in r.text and "Возможные дубли" not in r.text
    r = client.post(f"/entities/{kid}/facts", data={"text": "Счёт выставлять до 25-го"}, follow_redirects=True)
    assert "Факт добавлен" in r.text and "Счёт выставлять до 25-го" in r.text and "вручную (SYSAI)" in r.text
    with SessionLocal() as s:
        note = s.query(Note).filter_by(entity_id=kid).one().id
    r = client.post(f"/entities/{kid}/facts/{note}/archive", follow_redirects=True)
    assert "Факт убран в архив" in r.text and "в архиве" in r.text
    assert client.post(f"/entities/{kid}/facts/999/archive").status_code == 404
    r = client.post(f"/entities/{kid}", data={"name": "ОАО «Криница»", "kind": "client", "unp": "100345678",
                                              "contacts": "Ольга, бухгалтерия"}, follow_redirects=True)
    assert "Ольга, бухгалтерия" in r.text and "вручную (SYSAI)" in r.text
    r = client.get("/entities?q=криница")
    assert "ОАО «Криница»" in r.text and "Крыница</b>" not in r.text
    assert 'href="/entities"' in r.text  # in the menu
    r = client.post(f"/entities/{kid}/delete", follow_redirects=True)
    assert "Карточка удалена" in r.text and client.get(f"/entities/{kid}").status_code == 404
    assert "Это дубль" not in client.get(f"/entities/{dup}").text  # the merged one is a card of its own again


def test_the_list_of_main_objects_and_clients_is_pasted_at_once(client):
    entities.save(name="Криница", kind="org")
    text = ("ОАО «Криница»; 100 345 678\n"
            "• Беларуськалий — клиент\n"
            "Склад на Заводской; объект\n"
            "\n"
            "ООО\n"
            "ЧУП «Стройресурс»\tподрядчик")
    r = client.post("/entities/bulk", data={"text": text, "kind": "client"}, follow_redirects=True)
    assert "Новых карточек: 3, уже были: 1." in r.text
    with SessionLocal() as s:
        cards = {e.name: (e.kind, e.unp or "") for e in s.query(Entity)}
    assert cards == {"Криница": ("client", "100345678"), "Беларуськалий": ("client", ""),
                     "Склад на Заводской": ("object", ""), "ЧУП «Стройресурс»": ("contractor", "")}
