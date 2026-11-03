"""№18: incoming documents — what each one is (every value with its quote from the file), what does not add up, and
where it goes by the «что кому» rules: the sender hears it in the answer, the file and its deadlines go to the person."""
import datetime as dt
import hashlib
import json
from decimal import Decimal

import pytest

from app import agent, agent_checks, deadlines, doc_intake, doctext, llm
from app.db import (Case, Department, Document, EmployeeDepartment, Entity, EntityLink, Reminder, SessionLocal, Task)
from test_ai_mode import FakeBackend, tgacc  # noqa: F401  (fixtures)
from test_cases import CaseModel, doc, incoming, office  # noqa: F401


def invoice_text(day, pay, vat="200,00", total="1 200,00"):
    return (f"СЧЁТ-ФАКТУРА № 15 от {day:%d.%m.%Y}\n"
            "Поставщик: ООО «Монолит», УНП 190123456, г. Минск\n"
            "Покупатель: ООО «СтройДом»\n"
            "Объект: ул. Ленина, 12\n"
            "1. Бетон М300, м3: 10 × 100,00 = 1 000,00\n"
            "Итого без НДС: 1 000,00\n"
            f"НДС 20%: {vat}\n"
            f"Всего к оплате: {total} BYN\n"
            f"Оплатить до {pay:%d.%m.%Y}.\n")


def invoice_answer(day, pay, vat="200,00", total="1 200,00"):
    """What a good model gives for invoice_text."""
    return {"kind": "invoice", "fields": {
        "number": {"value": "15", "quote": "СЧЁТ-ФАКТУРА № 15"},
        "date": {"value": day.isoformat(), "quote": f"от {day:%d.%m.%Y}"},
        "counterparty": {"value": "ООО «Монолит»", "quote": "Поставщик: ООО «Монолит»"},
        "unp": {"value": "190123456", "quote": "УНП 190123456"},
        "amount_without_vat": {"value": "1000", "quote": "Итого без НДС: 1 000,00"},
        "vat": {"value": vat, "quote": f"НДС 20%: {vat}"},
        "vat_rate": {"value": "20", "quote": "НДС 20%"},
        "amount": {"value": total, "quote": f"Всего к оплате: {total} BYN"},
        "currency": {"value": "BYN", "quote": f"{total} BYN"},
        "subject": {"value": "Бетон М300"},
        "object": {"value": "ул. Ленина, 12", "quote": "Объект: ул. Ленина, 12"}},
        "deadlines": [{"what": "оплатить счёт", "date": pay.isoformat(), "quote": f"Оплатить до {pay:%d.%m.%Y}"}],
        "items": [{"name": "Бетон М300", "qty": "10", "price": "100,00", "sum": "1 000,00"}]}


def dates():
    today = deadlines.today()
    return today - dt.timedelta(days=1), today + dt.timedelta(days=6)


class IntakeModel(CaseModel):
    """The case fakes plus the document analysis: its answers come from a queue (a callable gets the text)."""

    def __init__(self):
        super().__init__()
        self.intake, self.intake_calls, self.intake_down = [], [], False

    def __call__(self, model_name, messages, **kwargs):
        if "Ты разбираешь входящий документ" in messages[0]["content"]:
            user = messages[1]["content"]
            meta = json.loads(user.split("<data>")[1].split("</data>")[0])
            text = user.split("Текст документа:\n<data>", 1)[1].rsplit("</data>", 1)[0]
            self.intake_calls.append({"meta": meta, "text": text})
            if self.intake_down:
                raise llm.LLMError("модель недоступна")
            item = self.intake.pop(0) if self.intake else {"kind": "other", "fields": {}}
            return json.dumps(item(text) if callable(item) else item, ensure_ascii=False)
        return super().__call__(model_name, messages, **kwargs)


@pytest.fixture
def imodel(monkeypatch):
    fake = IntakeModel()
    monkeypatch.setattr(llm, "chat", fake)
    monkeypatch.setattr(doc_intake, "_read_now", lambda: True)  # the file is read before the answer, as in production
    return fake


def stored(text, filename="счёт_15.txt", **kw):
    """A received document already read (the panel and the background loop work on these)."""
    with SessionLocal() as s:
        d = Document(filename=filename, mime="text/plain", size=len(text.encode()),
                     sha256=hashlib.sha256(text.encode()).hexdigest(), kind="document",
                     chat_id=kw.pop("chat_id", "44"), message_id=kw.pop("message_id", 900), data=text.encode(),
                     text=text, text_status="done", **kw)
        s.add(d)
        s.commit()
        return d.id


def facts_of(document_id):
    with SessionLocal() as s:
        d = s.get(Document, document_id)
        return d.facts_status, dict(d.facts or {})


# ----------------------------------------------------------------------------- numbers, dates, quotes, arithmetic
def test_numbers_and_dates_as_they_are_written():
    assert doc_intake.amount("12 000,50") == Decimal("12000.50") == doc_intake.amount("12000.50")
    assert doc_intake.amount(12000.5) == Decimal("12000.5") and doc_intake.amount("1 200 руб.") == Decimal("1200")
    assert doc_intake.amount("1,234.50") == Decimal("1234.50") and doc_intake.amount("нет суммы") is None
    found = doc_intake.numbers_in("Итого: 3 150,00")
    assert Decimal("3150.00") in found and Decimal("150.00") in found  # a table cell may be either
    assert doc_intake.money(Decimal("1200")) == "1 200,00" and doc_intake.money(Decimal("-5.5")) == "-5,50"
    assert doc_intake.dates_in("от 12.11.2025, до 3 декабря 2025 и 2025-12-05") == [
        dt.date(2025, 11, 12), dt.date(2025, 12, 3), dt.date(2025, 12, 5)]
    assert doc_intake.currency("бел. руб.") == "BYN" and doc_intake.currency("USD") == "USD"
    assert doc_intake.currency("российских рублей") == "RUB"


def test_a_value_is_kept_only_when_its_quote_is_in_the_file_and_holds_it():
    day, pay = dates()
    text = invoice_text(day, pay)
    raw = invoice_answer(day, pay)
    raw["fields"]["number"] = {"value": "16", "quote": "Счёт № 16"}                         # not in the file
    raw["fields"]["amount"] = {"value": "1 300,00", "quote": "Всего к оплате: 1 200,00 BYN"}  # the quote says otherwise
    raw["fields"]["date"] = {"value": (day - dt.timedelta(days=3)).isoformat(), "quote": f"от {day:%d.%m.%Y}"}
    raw["deadlines"].append({"what": "подписать акт", "date": pay.isoformat(), "quote": "Подписать до пятницы"})
    raw["items"].append({"name": "Доставка", "qty": "1", "price": "77,00", "sum": "77,00"})  # no such sum in the file
    facts = doc_intake.clean(raw, text)
    assert set(facts["fields"]) == {"counterparty", "unp", "amount_without_vat", "vat", "vat_rate", "currency",
                                    "subject", "object"}
    assert {u["field"] for u in facts["unverified"]} == {"number", "amount", "date"}
    assert [d["what"] for d in facts["deadlines"]] == ["оплатить счёт"]
    assert [i["name"] for i in facts["items"]] == ["Бетон М300"]
    assert facts["fields"]["vat"] == {"value": "200.00", "quote": "НДС 20%: 200,00"}
    assert doc_intake.clean({"kind": "Счёт-фактура", "fields": {"vat_rate": {"value": 0, "quote": "без НДС"}}},
                            "Счёт-фактура № 3. Сумма 100,00, без НДС.")["fields"]["vat_rate"]["value"] == "0"
    assert doc_intake.clean({"kind": "Счёт-фактура"}, "")["kind"] == "invoice"


def test_what_does_not_add_up_is_named_in_plain_words():
    def f(**values):
        return {"fields": {k: {"value": v} for k, v in values.items()}, "items": [], "deadlines": []}
    assert doc_intake.problems(f(amount="1200", amount_without_vat="1000", vat="200", vat_rate="20", unp="190123456")) == []
    assert doc_intake.problems(f(amount="1250", amount_without_vat="1000", vat="200")) == [
        "Сумма без НДС 1 000,00 + НДС 200,00 = 1 200,00, а итог в документе 1 250,00"]
    assert doc_intake.problems(f(amount_without_vat="1000", vat="180", vat_rate="20")) == [
        "НДС 20% от 1 000,00 — это 200,00, а в документе 180,00"]
    assert doc_intake.problems(f(amount="1200", vat="200", vat_rate="20")) == []  # 20/120 of the total
    rows = f(amount_without_vat="1000")
    rows["items"] = [{"name": "Бетон", "qty": "10", "price": "90", "sum": "1000"}, {"name": "Доставка", "sum": "50"}]
    assert doc_intake.problems(rows) == ["Строка «Бетон»: 10 × 90,00 = 900,00, а в документе 1 000,00",
                                         "Сумма строк 1 050,00 не сходится с суммой без НДС 1 000,00"]
    assert doc_intake.problems(f(unp="19012345")) == ["УНП «19012345» — должно быть 9 цифр"]
    late = f(date="2025-11-12")
    late["deadlines"] = [{"what": "оплатить", "date": "2025-11-10"}]
    assert doc_intake.problems(late) == ["Срок «оплатить» (10.11.2025) раньше даты документа (12.11.2025)"]
    ahead = (deadlines.today() + dt.timedelta(days=30)).isoformat()
    assert doc_intake.problems(f(date=ahead))[0].endswith("— ещё не наступила")
    day, pay = dates()
    facts = doc_intake.clean(invoice_answer(day, pay), invoice_text(day, pay))
    assert doc_intake.line(facts) == f"Счёт № 15 от {day:%d.%m.%Y} · ООО «Монолит» · 1 200,00 BYN, НДС 200,00"


# ----------------------------------------------------------------------------- in the conversation
def test_an_invoice_is_told_passed_to_the_person_of_the_rule_with_a_task_and_a_reminder(office, tgacc, imodel):
    day, pay = dates()
    rule_id = doc_intake.save_rule(kind="invoice", to=f"e:{office['anna']}", by="SYSAI")
    imodel.intake.append(invoice_answer(day, pay))
    seen = {}

    def decide(facts):
        seen.update(facts["file_received"])
        return {"reply": "Счёт № 15 от ООО «Монолит» на 1 200,00 BYN. Передам Анне и поставлю задачу оплатить.",
                "actions": []}
    imodel.decisions.append(decide)
    agent.handle_incoming(incoming(7, "boss", message_id=600,
                                   document=doc(invoice_text(day, pay).encode(), "счёт_15.txt", "text/plain")))
    intake = seen["intake"]
    assert intake["doc_type"] == "Счёт" and intake["problems"] == []
    assert intake["requisites"]["number"] == "15" and intake["requisites"]["amount"] == "1 200,00"
    assert intake["requisites"]["unp"] == "190123456" and intake["requisites"]["date"] == f"{day:%d.%m.%Y}"
    assert intake["route"] == {"to": "Анна Смирнова", "rule": "Счёт → Анна Смирнова",
                               "tasks": [f"оплатить счёт до {pay:%d.%m.%Y}"], "reminders": True}
    assert tgacc.sent[-1] == ("7", "Счёт № 15 от ООО «Монолит» на 1 200,00 BYN. Передам Анне и поставлю задачу оплатить.")
    ref, filename, caption = tgacc.files[-1]
    assert ref == "@anna" and filename == "счёт_15.txt" and len(tgacc.files) == 1
    with SessionLocal() as s:
        d = s.query(Document).one()
        route = d.facts["route"]
        assert d.facts_status == "done" and route["status"] == "sent" and route["rule_id"] == rule_id
        assert d.facts["told"] and route["case_id"] and s.get(Case, route["case_id"]).status == "done"
        task = s.get(Task, route["task_ids"][0])
        assert task.assignee_id == office["anna"] and task.deadline == pay and task.deadline_source == "stated"
        assert task.title.startswith("Оплатить счёт — Счёт № 15") and task.meeting_id is None
        assert f"Оплатить до {pay:%d.%m.%Y}" in task.deadline_quote
        reminder = s.get(Reminder, route["reminder_ids"][0])
        assert reminder.recipients[0]["ref"] == "@anna" and reminder.topic.startswith("Срок по документу: оплатить счёт")
        assert reminder.due_at.date() < pay  # the working day before
        card = s.query(Entity).one()  # the counterparty got a card; the object is only linked when its card exists
        assert card.name == "ООО «Монолит»" and card.unp == "190123456" and card.sources["unp"]["id"] == d.id
        assert s.query(EntityLink).filter_by(entity_id=card.id, source_type="document", source_id=d.id).count() == 1
    meta = imodel.intake_calls[0]["meta"]
    assert meta["filename"] == "счёт_15.txt" and meta["today"] == deadlines.today().isoformat()


def test_whom_the_person_names_wins_over_the_rule(office, tgacc, imodel):
    day, pay = dates()
    doc_intake.save_rule(kind="invoice", to=f"e:{office['anna']}")
    imodel.intake.append(invoice_answer(day, pay))
    imodel.decisions.append(lambda facts: {"reply": "Перешлю Петру.", "actions": [
        {"type": "deliver_files", "to": office["petya"], "document_ids": [facts["file_received"]["document_id"]]}]})
    agent.handle_incoming(incoming(7, "boss", text="Это Пете", message_id=601,
                                   document=doc(invoice_text(day, pay).encode(), "счёт_15.txt", "text/plain")))
    assert [f[0] for f in tgacc.files] == ["@petya"]
    status, facts = facts_of(1)
    assert facts["route"]["status"] == "by_person"
    with SessionLocal() as s:
        assert s.query(Task).count() == 0


def test_a_department_rule_goes_to_its_head_and_the_head_s_own_file_stays_with_them(office, tgacc, imodel):
    day, pay = dates()
    with SessionLocal() as s:
        dep = Department(name="Бухгалтерия")
        dep.memberships.append(EmployeeDepartment(employee_id=office["ivan"]))
        dep.memberships.append(EmployeeDepartment(employee_id=office["anna"], is_primary=True))
        s.add(dep)
        s.commit()
        dep_id = dep.id
    doc_intake.save_rule(keywords="монолит, бетон", to=f"d:{dep_id}", make_tasks=True)
    imodel.intake.append(invoice_answer(day, pay))
    seen = {}
    imodel.decisions.append(lambda facts: seen.update(facts["file_received"]) or {"reply": "Спасибо!", "actions": []})
    agent.handle_incoming(incoming(43, "anna", message_id=602,
                                   document=doc(invoice_text(day, pay).encode(), "счёт_15.txt", "text/plain")))
    assert seen["intake"]["route"]["already_theirs"] and seen["intake"]["route"]["to"] == "Анна Смирнова"
    assert tgacc.files == []  # she sent it herself
    status, facts = facts_of(1)
    assert facts["route"]["status"] == "self" and facts["route"]["department"] == "Бухгалтерия"
    with SessionLocal() as s:
        assert [t.assignee_id for t in s.query(Task)] == [office["anna"]]


def test_a_file_analyzed_after_the_answer_is_passed_on_later_and_the_sender_hears_it(office, tgacc, imodel, monkeypatch):
    monkeypatch.setattr(doc_intake, "_read_now", lambda: False)  # the reading is slower than the answer
    monkeypatch.setattr(doc_intake, "LATE_SECONDS", -1)
    day, pay = dates()
    doc_intake.save_rule(kind="invoice", to=f"e:{office['anna']}")
    imodel.intake.append(invoice_answer(day, pay))
    seen = {}
    imodel.decisions.append(lambda facts: seen.update(facts["file_received"]) or
                            {"reply": "Посмотрю файл и напишу.", "actions": []})
    agent.handle_incoming(incoming(7, "boss", message_id=603,
                                   document=doc(invoice_text(day, pay).encode(), "счёт_15.txt", "text/plain")))
    assert seen["intake"] == {"pending": True} and tgacc.files == []
    doctext.process(1)
    doc_intake.run_pending()
    assert [f[0] for f in tgacc.files] == ["@anna"]
    ref, text = tgacc.sent[-1]
    assert ref == "7" and text == (f"Разобрал «счёт_15.txt»: Счёт № 15 от {day:%d.%m.%Y} · ООО «Монолит» · 1 200,00 BYN, "
                                   "НДС 200,00. Отправил по правилу: Анна Смирнова, поставил задачи по срокам.")
    status, facts = facts_of(1)
    assert facts["route"]["status"] == "late" and facts["told"]
    doc_intake.run_pending()  # once
    assert len(tgacc.files) == 1 and [r for r, _ in tgacc.sent].count("7") == 2


def test_the_checks_know_the_route_keeps_the_promise():
    tell = agent_checks.scenario("intake-tell")
    ctx, _, _ = tell.build()
    good = {"reply": "Счёт № 15 от ООО «Монолит» на 1 200 BYN. Передам Анне Петровой и поставлю задачу оплатить.",
            "actions": []}
    assert tell.evaluate(good, ctx) == []
    none = agent_checks.scenario("intake-no-route")
    ctx, _, _ = none.build()
    reasons = none.evaluate({"reply": "Договор с ИП Козловым. Передам Анне.", "actions": []}, ctx)
    assert any("обещание без действия" in r for r in reasons) and any("переспросить" in r for r in reasons)


# ----------------------------------------------------------------------------- what is never passed on
def test_confidential_and_case_documents_stay_where_they_are(office, tgacc, imodel):
    day, pay = dates()
    doc_intake.save_rule(to=f"e:{office['anna']}")  # any document
    with SessionLocal() as s:
        case = Case(title="Взять у Ивана акт", kind="ask", status="running")
        s.add(case)
        s.commit()
        case_id = case.id
    secret = stored(invoice_text(day, pay), "ведомость_зарплата.txt", sensitive=True)
    in_case = stored(invoice_text(day, pay), "акт.txt", message_id=901, case_id=case_id)
    imodel.intake += [invoice_answer(day, pay), invoice_answer(day, pay)]
    assert doc_intake.analyze(secret) == "done" and doc_intake.analyze(in_case) == "done"
    assert facts_of(secret)[1]["route"]["status"] == "sensitive" and facts_of(in_case)[1]["route"]["status"] == "in_case"
    doc_intake.carry_out(secret)
    doc_intake.carry_out(in_case)
    assert tgacc.files == []
    with SessionLocal() as s:
        view = doc_intake.view(s.get(Document, secret))
    assert view["route"] == {"to": None, "why": "конфиденциальный — по правилам не пересылается"}


def test_old_generated_and_unreadable_files_and_a_model_outage(office, imodel):
    day, pay = dates()
    old = stored(invoice_text(day, pay), facts_status="old")
    made = stored(invoice_text(day, pay), "письмо.txt", message_id=902, chat_id="generated")
    scan = stored("[фото без текста]", "фото.jpg", message_id=903)
    assert doc_intake.analyze(old) == "old" and doc_intake.analyze(made) == "skipped" and doc_intake.analyze(scan) == "skipped"
    assert facts_of(made)[1]["skipped"] == "сделан по шаблону" and imodel.intake_calls == []
    with SessionLocal() as s:
        assert doc_intake.view(s.get(Document, old)) == {}
        assert doc_intake.view(s.get(Document, scan)) == {"not_analyzed": "нет текста"}
    imodel.intake_down = True
    fresh = stored(invoice_text(day, pay), message_id=904)
    assert [doc_intake.analyze(fresh) for _ in range(3)] == ["pending", "pending", "failed"]
    with SessionLocal() as s:
        assert doc_intake.view(s.get(Document, fresh)) == {"not_analyzed": "не удалось разобрать (модель недоступна)"}
    doc_intake.run_pending()  # an old one is not analyzed by the background loop either
    assert len(imodel.intake_calls) == 3 and facts_of(old)[0] == "old"


def test_switched_off_nothing_is_analyzed(office, tgacc, imodel):
    from app import settings_store
    settings_store.set_many({"doc_intake": False})
    day, pay = dates()
    seen = {}
    imodel.decisions.append(lambda facts: seen.update(facts["file_received"]) or {"reply": "Спасибо!", "actions": []})
    agent.handle_incoming(incoming(7, "boss", message_id=604,
                                   document=doc(invoice_text(day, pay).encode(), "счёт_15.txt", "text/plain")))
    assert "intake" not in seen and imodel.intake_calls == []


# ----------------------------------------------------------------------------- the panel
def test_rules_are_made_ordered_switched_and_removed_on_the_documents_page(client, office):
    r = client.post("/documents/rules", data={"kind": "invoice", "keywords": "бетон; арматура",
                                              "to": f"e:{office['anna']}", "make_tasks": "on"}, follow_redirects=True)
    assert "Правило сохранено" in r.text and "Счёт со словами «бетон», «арматура» → Анна Смирнова" in r.text
    r = client.post("/documents/rules", data={"kind": "", "to": "e:999999"}, follow_redirects=True)
    assert "такого сотрудника нет в справочнике" in r.text
    r = client.post("/documents/rules", data={"kind": "", "to": ""}, follow_redirects=True)
    assert "выберите, кому передавать" in r.text
    client.post("/documents/rules", data={"kind": "act", "to": f"e:{office['petya']}"})
    with SessionLocal() as s:
        first, second = [x["id"] for x in doc_intake.rules_view(s)]
    client.post(f"/documents/rules/{second}/up")
    client.post(f"/documents/rules/{first}/toggle")
    with SessionLocal() as s:
        rows = doc_intake.rules_view(s)
    assert [x["id"] for x in rows] == [second, first] and [x["active"] for x in rows] == [True, False]
    assert rows[1]["make_tasks"] and not rows[0]["make_tasks"]
    client.post(f"/documents/rules/{first}/delete")
    assert client.post("/documents/rules/999/toggle").status_code == 404
    with SessionLocal() as s:
        assert [x["id"] for x in doc_intake.rules_view(s)] == [second]
    assert client.post("/documents/intake", follow_redirects=True).status_code == 200 and not doc_intake.enabled()
    client.post("/documents/intake")
    assert doc_intake.enabled()


def test_the_document_page_shows_quotes_problems_and_passes_on_by_a_rule_added_later(client, office, tgacc, imodel):
    day, pay = dates()
    text = invoice_text(day, pay, vat="180,00", total="1 180,00")
    raw = invoice_answer(day, pay, vat="180,00", total="1 180,00")
    raw["fields"]["number"] = {"value": "16", "quote": "Счёт № 16"}
    d = stored(text, employee_id=office["petya"])
    imodel.intake.append(raw)
    r = client.post(f"/documents/{d}/analyze", follow_redirects=True)
    assert "Разобран заново" in r.text and "Не сходится" in r.text
    assert "НДС 20% от 1 000,00 — это 200,00, а в документе 180,00" in r.text
    assert "«УНП 190123456»" in r.text and "Не взято — цитата не нашлась в файле (1)" in r.text
    assert "правила нет" in r.text and "Передать по правилам" in r.text and "ООО «Монолит»</a>" in r.text
    r = client.post(f"/documents/{d}/route", follow_redirects=True)
    assert "нет правила для такого документа" in r.text and tgacc.files == []
    doc_intake.save_rule(kind="invoice", to=f"e:{office['anna']}")
    r = client.post(f"/documents/{d}/route", follow_redirects=True)
    assert "передан по правилам" in r.text and tgacc.files[-1][0] == "@anna"
    r = client.post(f"/documents/{d}/route", follow_redirects=True)
    assert "документ уже передан" in r.text and len(tgacc.files) == 1
    r = client.get("/documents")
    assert "→ Анна Смирнова" in r.text and "не сходится: 1" in r.text.lower()


def test_analyzing_again_from_the_panel_does_not_pass_the_file_on_by_itself(client, office, tgacc, imodel, monkeypatch):
    monkeypatch.setattr(doc_intake, "LATE_SECONDS", -1)
    day, pay = dates()
    doc_intake.save_rule(kind="invoice", to=f"e:{office['anna']}")
    d = stored(invoice_text(day, pay), facts_status="old")
    imodel.intake.append(invoice_answer(day, pay))
    client.post(f"/documents/{d}/analyze")
    status, facts = facts_of(d)
    assert status == "done" and facts["route"]["status"] == "ready" and facts["route"]["to"] == "Анна Смирнова"
    doc_intake.run_pending()
    assert tgacc.files == []
    r = client.get(f"/documents/{d}/view")
    assert "готов к передаче по правилам" in r.text and "Передать по правилам" in r.text
    client.post(f"/documents/{d}/route")
    assert tgacc.files[-1][0] == "@anna"


def test_the_admin_marks_analyses_right_or_wrong_and_the_page_counts_them(client, office, imodel):
    day, pay = dates()
    first = stored(invoice_text(day, pay))
    second = stored(invoice_text(day, pay), "счёт_16.txt", message_id=905)
    unread = stored("[скан]", "фото.jpg", message_id=906)
    imodel.intake += [invoice_answer(day, pay), invoice_answer(day, pay)]
    doc_intake.analyze(first)
    doc_intake.analyze(second)
    r = client.get("/documents")
    assert "цель: реквизиты и маршрут верны хотя бы в 9 документах из 10" in r.text
    r = client.post(f"/documents/{first}/check", data={"ok": "1"}, follow_redirects=True)
    assert "Отмечено" in r.text and "отмечено: верно" in r.text
    client.post(f"/documents/{second}/check", data={"ok": "0", "note": "не тот срок"})
    r = client.post(f"/documents/{unread}/check", data={"ok": "1"}, follow_redirects=True)
    assert "документ ещё не разобран" in r.text
    r = client.get("/documents")
    assert "верно 1 из 2" in r.text and "счёт_16.txt</a> (не тот срок)" in r.text
