"""№24–25 «Почтальон»: a sorted letter goes to whoever handles it — the rules «кто чем занимается», the same
conversation, the task, the rules of the documents; otherwise the agent suggests and a leader decides — with its
summary, attachments and tasks from its deadlines, through the case engine. «Не моё» brings it back and counts as a
routing error. The answer is a draft by the person's words, the conversation and the knowledge base; it goes out through
SMTP only after «ок»."""
import datetime as dt
import json
import types

import pytest

from app import agent, agent_skills, cases, deadlines, digest, llm, mail, mail_reply, mail_route, settings_store
from app.db import Case, Document, Employee, MailDraft, MailMessage, MailRule, SessionLocal, Task
from test_ai_mode import FakeBackend, incoming, tgacc  # noqa: F401  (fixtures)
from test_mail import FakeBox, box, letter, rows  # noqa: F401  (fixtures)


def _data(text: str):
    return json.loads(text.split("<data>", 1)[1].split("</data>", 1)[0])


class MailModel:
    """Every model the letters meet: the sorting, the hint «кому», the draft, the conversation, the case writer."""

    def __init__(self):
        self.sorts, self.hints, self.replies, self.decisions, self.calls = [], [], [], [], []

    def __call__(self, model_name, messages, **kwargs):
        system, user = messages[0]["content"], messages[1]["content"]
        self.calls.append({"system": system, "user": user})
        if "Ты сортируешь входящее письмо" in system:
            item = self.sorts.pop(0) if self.sorts else {"kind": "other"}
            return json.dumps(item, ensure_ascii=False)
        if "кому из сотрудников передать письмо" in system:
            item = self.hints.pop(0) if self.hints else {"to": None, "why": "", "sure": False}
            return json.dumps(item(_data(user)) if callable(item) else item, ensure_ascii=False)
        if "Ты готовишь ответ на деловое письмо" in system:
            item = self.replies.pop(0)
            return json.dumps(item(_data(user)) if callable(item) else item, ensure_ascii=False)
        if "Тебе пишет человек" in system:
            item = self.decisions.pop(0)
            return json.dumps(item(_data(user)) if callable(item) else item, ensure_ascii=False)
        if "Ты ведёшь дело" in system:
            facts = _data(user)
            return json.dumps({"text": f"[{facts['purpose']}] {facts.get('case', {}).get('title', '')}"}, ensure_ascii=False)
        return json.dumps({"text": "[other]", "covered_task_ids": []}, ensure_ascii=False)

    def of(self, words: str) -> list:
        return [c for c in self.calls if words in c["system"]]


@pytest.fixture
def mm(monkeypatch):
    fake = MailModel()
    monkeypatch.setattr(llm, "chat", fake)
    return fake


class FakeSmtp:
    def __init__(self):
        self.sent, self.fail, self.tests = [], None, 0

    def test(self):
        self.tests += 1

    def send(self, msg):
        if self.fail:
            raise self.fail
        self.sent.append(msg)


@pytest.fixture
def smtp(monkeypatch):
    fake = FakeSmtp()
    monkeypatch.setattr(mail_reply, "_smtp", lambda cfg: fake)
    return fake


def people():
    with SessionLocal() as s:
        anna = Employee(name="Анна Орлова", position="бухгалтер", responsibilities="счета и оплата", telegram_chat_id="@anna")
        ivan = Employee(name="Иван Петров", position="прораб", responsibilities="поставки на объект", telegram_chat_id="@ivan")
        s.add_all([anna, ivan])
        s.commit()
        return anna.id, ivan.id


def arrive(box, mm, raw, answer, **kwargs):
    box.add(raw, **kwargs)
    mm.sorts.append(answer)
    mail.check_now()
    mail.sort_pending()
    return rows()[-1]


def get(mail_id):
    with SessionLocal() as s:
        return s.get(MailMessage, mail_id)


def anna_says(text, message_id=7):
    agent.handle_incoming(incoming(user_id=43, username="anna", name="Анна", text=text, message_id=message_id))


def to_anna(tg):
    return [t for ref, t in tg.sent if ref == "@anna"]


# ----------------------------------------------------------------------------- whom the letter goes to
def test_a_letter_goes_by_the_rule_with_its_summary_and_a_task_from_its_deadline(box, tgacc, mm):
    anna, _ = people()
    mail_route.save_rule(kind="invoice", to=f"e:{anna}", by="Директор")
    pay = deadlines.today() + dt.timedelta(days=3)
    row = arrive(box, mm, letter(subject="Счёт № 15 за бетон", text=f"Высылаем счёт № 15. Оплатить до {pay:%d.%m.%Y}."),
                 {"kind": "invoice", "why": "счёт", "quote": "Высылаем счёт № 15", "summary": "Монолит прислал счёт № 15.",
                  "ask": "оплатить счёт", "needs_reply": False,
                  "deadlines": [{"what": "оплатить счёт", "date": pay.isoformat(), "quote": f"Оплатить до {pay:%d.%m.%Y}"}]})
    route = row.route
    assert (route["status"], route["how"], route["to_id"], row.status) == ("sent", "rule", anna, "routed")
    [text] = to_anna(tgacc)
    assert text.startswith("📧 Письмо от Иван Петров") and "Тема: «Счёт № 15 за бетон»" in text
    assert "Монолит прислал счёт № 15." in text and "Просят: оплатить счёт" in text
    assert "Передаю вам по правилу: Счёт." in text and "Если письмо не вам — напишите «не моё»." in text
    assert f"Поставил задачу: «Оплатить счёт — письмо «Счёт № 15 за бетон»» до {pay:%d.%m}." in text
    with SessionLocal() as s:
        [task] = s.query(Task).all()
        assert (task.assignee_id, task.deadline, task.status, task.deadline_source) == (anna, pay, "sent", "stated")
        case = s.get(Case, route["case_id"])
        assert case.kind == "mail" and case.task_id == task.id and route["task_ids"] == [task.id]
        assert [(x.tool, x.status) for x in case.steps][:2] == [("mail_notice", "done"), ("wait_reply", "waiting")]
        assert "Иван Петров" in cases.describe_plan(s, case) or case.steps[0].args["text"] == text
    assert mail_route.short(get(row.id)) == "→ Анна Орлова"
    # «оплатила»: the task is done, the case reports to the leaders
    mm.decisions.append(lambda f: {"reply": "Отлично, отмечу.", "actions": [
        {"type": "task_status", "task_id": f["assistant_waiting_for_you"][0]["task_id"], "status": "done"}]})
    anna_says("оплатила")
    with SessionLocal() as s:
        assert s.get(Task, task.id).status == "done" and s.get(Case, route["case_id"]).status == "done"
    assert any(ref == "@boss" and t.startswith("[report] Письмо «Счёт № 15 за бетон» → Анна Орлова") for ref, t in tgacc.sent)


def test_the_attachments_follow_the_notice_and_personal_data_stays_in_the_panel(box, tgacc, mm):
    anna, _ = people()
    mail_route.save_rule(senders="@monolit.by", to=f"e:{anna}")
    box.add(letter(subject="Документы", text="Во вложении счёт и паспорт водителя.",
                   attachments=[("счёт.pdf", b"%PDF " + b"2" * 3000, "application/pdf"),
                                ("паспорт.pdf", b"%PDF " + b"3" * 3000, "application/pdf")]))
    mail.check_now()
    [row] = rows()
    with SessionLocal() as s:
        for a in row.attachments:
            d = s.get(Document, a["document_id"])
            d.text_status, d.facts_status = "done", "done"
            d.sensitive = a["filename"] == "паспорт.pdf"
        s.commit()
    mm.sorts.append({"kind": "invoice", "summary": "Счёт и паспорт водителя."})
    mail.sort_pending()
    [text] = to_anna(tgacc)
    assert "Вложения — ниже (1)." in text and "Не пересылаю в чат (личные данные): «паспорт.pdf»" in text
    assert [(ref, name) for ref, name, _ in tgacc.files] == [("@anna", "счёт.pdf")]
    assert get(row.id).route["files"] == 1


def test_the_same_conversation_goes_to_the_same_person_and_newsletters_and_old_letters_stay(box, tgacc, mm):
    anna, ivan = people()
    mail_route.save_rule(keywords="бетон", to=f"e:{ivan}")
    first = arrive(box, mm, letter(subject="Поставка бетона", message_id="<a1@monolit.by>"), {"kind": "request"})
    assert first.route["to_id"] == ivan and first.route["how"] == "rule"
    second = arrive(box, mm, letter(subject="Re: Поставка", text="И ещё вопрос по графику.", in_reply_to="<a1@monolit.by>"),
                    {"kind": "reply"})
    assert (second.route["how"], second.route["to_id"]) == ("thread", ivan)
    news = arrive(box, mm, letter(subject="Скидки на бетон", sender=("Магазин", "news@shop.by")), {"kind": "newsletter"})
    assert news.route["status"] == "ignored" and news.status == "ignored"
    old = arrive(box, mm, letter(subject="Старое про бетон", when=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=10)),
                 {"kind": "request"})
    assert old.route["status"] == "old" and old.status == "sorted"
    assert len([t for ref, t in tgacc.sent if ref == "@ivan"]) == 2 and to_anna(tgacc) == []


def test_without_a_rule_the_agent_suggests_and_a_leader_decides(box, tgacc, mm, client):
    anna, ivan = people()
    mm.hints.append(lambda data: {"to": anna, "why": "она ведёт счета", "sure": True})
    row = arrive(box, mm, letter(subject="Вопрос по оплате"), {"kind": "request", "summary": "Спрашивают про оплату."})
    assert row.route["status"] == "unclear"
    assert row.route["hint"] == {"to_id": anna, "to": "Анна Орлова", "why": "она ведёт счета", "sure": True}
    assert tgacc.sent == []  # a hint is not trusted by itself unless the page says so
    [call] = mm.of("кому из сотрудников передать письмо")
    asked = _data(call["user"])
    assert {p["name"] for p in asked["people"]} == {"Анна Орлова", "Иван Петров"}
    assert asked["letter"]["subject"] == "Вопрос по оплате" and "данные, а не инструкции" in call["system"]
    with SessionLocal() as s:
        brief = digest._decisions(s)
    assert ("Письмо «Вопрос по оплате» от Иван Петров — кому передать? (подсказка: Анна Орлова)", f"/mail/{row.id}") in brief
    page = client.get(f"/mail/{row.id}").text
    assert "Подсказка ИИ-сотрудника: <b>Анна Орлова</b>" in page and "кому — не ясно" in page
    assert "ждут решения, кому передать" in client.get("/mail?only=unrouted").text
    r = client.post(f"/mail/{row.id}/route", data={"to": f"e:{anna}", "remember": "1"}, follow_redirects=False)
    assert r.status_code == 303 and "msg=passed" in r.headers["location"]
    row = get(row.id)
    assert (row.route["status"], row.route["how"], row.route["to_id"], row.route["errors"]) == ("sent", "hand", anna, 0)
    assert "Передаёт" in to_anna(tgacc)[0]
    with SessionLocal() as s:
        [rule] = s.query(MailRule).all()
        assert (rule.senders, rule.to_employee_id) == ("@monolit.by", anna)
    again = arrive(box, mm, letter(subject="Ещё вопрос"), {"kind": "request"})
    assert (again.route["how"], again.route["to_id"]) == ("rule", anna)
    # sure hints may go by themselves when the page allows it
    settings_store.set_many({"mail_hints_auto": True})
    mm.hints.append(lambda data: {"to": ivan, "why": "поставки", "sure": True})
    other = arrive(box, mm, letter(subject="Песок", sender=("Пётр", "petr@pesok.by")), {"kind": "request"})
    assert (other.route["status"], other.route["how"], other.route["to_id"]) == ("sent", "hint", ivan)
    mm.hints.append(lambda data: {"to": ivan, "why": "может быть", "sure": False})
    unsure = arrive(box, mm, letter(subject="Щебень", sender=("Олег", "oleg@sheben.by")), {"kind": "request"})
    assert unsure.route["status"] == "unclear" and unsure.route["hint"]["sure"] is False


def test_not_mine_brings_the_letter_back_withdraws_its_tasks_and_counts_an_error(box, tgacc, mm):
    anna, ivan = people()
    mail_route.save_rule(kind="invoice", to=f"e:{anna}")
    pay = deadlines.today() + dt.timedelta(days=2)
    row = arrive(box, mm, letter(subject="Счёт № 7", text=f"Счёт № 7. Оплатить до {pay:%d.%m.%Y}."), {
        "kind": "invoice", "summary": "Счёт № 7.",
        "deadlines": [{"what": "оплатить", "date": pay.isoformat(), "quote": f"Оплатить до {pay:%d.%m.%Y}"}]})
    case_id, [task_id] = row.route["case_id"], row.route["task_ids"]
    mm.decisions.append(lambda f: {"reply": "Понял, верну руководителю.", "actions": [
        {"type": "mail_pass", "mail_id": f["mail_letters"][0]["id"], "to": None, "note": "счета ведёт Иван"}]})
    anna_says("это не моё, счета ведёт Иван")
    facts = _data(mm.of("Тебе пишет человек")[0]["user"])
    assert facts["mail_letters"][0]["routed_to"] == "Анна Орлова" and facts["mail_letters"][0]["subject"] == "Счёт № 7"
    row = get(row.id)
    assert (row.route["status"], row.route["errors"], row.status) == ("unclear", 1, "sorted")
    with SessionLocal() as s:
        assert s.get(Task, task_id).status == "cancelled" and s.get(Case, case_id).status == "cancelled"
        assert mail_route.accuracy(s) == {"total": 1, "wrong": 1, "ok": 0, "share": 100}
    told = [t for ref, t in tgacc.sent if ref == "@boss"]
    assert any("не по адресу (счета ведёт Иван)" in t and "Кому передать?" in t for t in told)
    with SessionLocal() as s:  # the page tells the earlier route in words, not by its code
        history = mail_route.view(s, s.get(MailMessage, row.id))["history"]
    assert history and all(h["status_ru"] and h["status_ru"] != h.get("status") for h in history)
    # the leader passes it on from the chat and asks to remember
    mm.decisions.append(lambda f: {"reply": "Передал Ивану.", "actions": [
        {"type": "mail_pass", "mail_id": [x for x in f["mail_letters"] if x.get("waits_for_decision")][0]["id"],
         "to": ivan, "remember": True}]})
    agent.handle_incoming(incoming(user_id=7, username="boss", name="Вадим", text="передай это письмо Ивану, впредь такие ему"))
    row = get(row.id)
    assert (row.route["status"], row.route["to_id"], row.route["how"], row.route["errors"]) == ("sent", ivan, "hand", 1)
    [text] = [t for ref, t in tgacc.sent if ref == "@ivan"]
    assert "Передаёт" in text and "Поставил задачу" in text
    with SessionLocal() as s:
        assert s.query(MailRule).filter_by(to_employee_id=ivan, senders="@monolit.by").count() == 1
        assert s.query(Task).filter_by(assignee_id=ivan, status="sent").count() == 1


def test_letters_are_not_forwarded_while_forwarding_or_the_ai_employee_is_off(box, mm):
    anna, _ = people()
    mail_route.save_rule(to=f"e:{anna}", keywords="счёт")
    row = arrive(box, mm, letter(), {"kind": "invoice"})
    assert row.route["status"] == "off" and "ИИ-сотрудник" in row.route["why"]
    with pytest.raises(mail_route.RouteError, match="ИИ-сотрудник"):
        mail_route.pass_on(row.id, anna, by="Директор")
    settings_store.set_many({"mail_forward": False, "ai_employee": True})
    row = arrive(box, mm, letter(subject="Счёт 2"), {"kind": "invoice"})
    assert row.route["status"] == "off" and "выключена" in row.route["why"]


def test_rules_worth_making_are_suggested_from_what_people_passed_by_hand(box):
    anna, _ = people()
    with SessionLocal() as s:
        for i in range(2):
            s.add(MailMessage(mailbox="office@stroydom.by", folder="INBOX", uidvalidity="1", uid=i + 1, direction="in",
                              from_addr=f"x{i}@pesok.by", subject="Песок", sort_status="done", status="routed",
                              route={"status": "sent", "how": "hand", "to_id": anna, "to": "Анна Орлова"}))
        s.commit()
        assert mail_route.suggestions(s) == [{"sender": "@pesok.by", "to": f"e:{anna}", "to_name": "Анна Орлова", "count": 2}]
    mail_route.save_rule(senders="@pesok.by", to=f"e:{anna}")
    with SessionLocal() as s:
        assert mail_route.suggestions(s) == []


# ----------------------------------------------------------------------------- the answers
def test_the_answer_is_drafted_by_the_persons_words_and_goes_only_after_ok(box, tgacc, mm, smtp):
    anna, _ = people()
    mail_route.save_rule(kind="request", to=f"e:{anna}")
    settings_store.set_many({"mail_drafts_auto": False, "mail_signature": "С уважением,\nАнна Орлова, СтройДом",
                             "mail_sender_name": "СтройДом"})
    row = arrive(box, mm, letter(subject="Цена бетона", text="Добрый день! Сколько стоит куб М300 с доставкой?",
                                 message_id="<q1@monolit.by>"),
                 {"kind": "request", "summary": "Спрашивают цену бетона М300.", "needs_reply": True})
    assert "Чтобы ответить, напишите, что сказать, — подготовлю черновик письма." in to_anna(tgacc)[0]
    mm.decisions.append(lambda f: {"reply": "Ниже черновик — проверьте.", "actions": [
        {"type": "mail_reply", "mail_id": f["mail_letters"][0]["id"], "points": "185 рублей за куб с доставкой"}]})
    mm.replies.append(lambda d: {"text": "Добрый день, Иван!\n\nКуб бетона М300 с доставкой стоит 185 рублей, привезём за 48 часов.",
                                 "missing": ["день доставки"]})
    anna_says("ответь, что 185 рублей за куб с доставкой")
    facts = _data(mm.of("Тебе пишет человек")[0]["user"])
    assert facts["mail_letters"][0]["needs_reply"] is True and facts["mail_can_reply"] is True
    [asked] = mm.of("Ты готовишь ответ на деловое письмо")
    data = _data(asked["user"])
    assert data["points"] == "185 рублей за куб с доставкой" and "Сколько стоит куб" in data["letter"]["text"]
    assert "Подпись не пиши" in asked["system"] and "СтройДом" in asked["system"]
    reply, shown = tgacc.sent[-2:]
    assert reply == ("43", "Ниже черновик — проверьте.") and shown[0] == "43"
    assert shown[1].startswith("✉️ Черновик ответа на «Цена бетона» → ivan@monolit.by:")
    assert "⚠️ Проверьте: 48 — этого нет ни в письме, ни в ваших словах, ни в базе знаний." in shown[1]
    assert "Не хватило данных: день доставки." in shown[1] and "«Ок» — отправлю с office@stroydom.by" in shown[1]
    assert "Анна Орлова, СтройДом" in shown[1] and smtp.sent == []
    # a correction makes the second version of the same draft
    mm.decisions.append(lambda f: {"reply": "Поправил — ниже.", "actions": [
        {"type": "mail_reply", "mail_id": f["mail_draft"]["mail_id"], "points": "привезём завтра"}]})
    mm.replies.append(lambda d: {"text": d["previous"].split("\n\nС уважением")[0].replace("за 48 часов", "завтра"),
                                 "missing": []})
    anna_says("напиши, что привезём завтра", message_id=8)
    assert _data(mm.of("Ты готовишь ответ")[1]["user"])["previous"].startswith("Добрый день, Иван!")
    assert "(вариант 2)" in tgacc.sent[-1][1] and "⚠️" not in tgacc.sent[-1][1]
    with SessionLocal() as s:
        assert [d.status for d in s.query(MailDraft).order_by(MailDraft.id)] == ["replaced", "draft"]
    assert smtp.sent == []
    # «ок»: it goes as an answer in the same conversation
    mm.decisions.append(lambda f: {"reply": "Отправляю.", "actions": [
        {"type": "confirm_mail_reply", "draft_id": f["mail_draft"]["id"], "ok": True}]})
    anna_says("ок", message_id=9)
    [msg] = smtp.sent
    assert (msg["To"], msg["Subject"], msg["In-Reply-To"]) == ("ivan@monolit.by", "Re: Цена бетона", "<q1@monolit.by>")
    assert "<q1@monolit.by>" in msg["References"] and "office@stroydom.by" in msg["From"] and "СтройДом" in msg["From"]
    body = msg.get_content()
    assert "привезём завтра" in body and "Анна Орлова, СтройДом" in body and "> Добрый день! Сколько стоит" in body
    assert tgacc.sent[-1] == ("43", "✅ Ответ ушёл: «Re: Цена бетона» → ivan@monolit.by.")
    row = get(row.id)
    assert row.status == "answered" and row.draft["sent_id"]
    with SessionLocal() as s:
        out = s.get(MailMessage, row.draft["sent_id"])
        assert (out.direction, out.in_reply_to, out.thread, out.to_addrs) == ("out", "<q1@monolit.by>", row.thread,
                                                                              "ivan@monolit.by")
        assert s.query(MailDraft).filter_by(status="sent").count() == 1
        assert mail_reply.examples(s)[0].startswith("Добрый день, Иван!")  # what went is the style next time


def test_a_letter_that_waits_for_an_answer_comes_with_a_draft_and_a_refused_send_can_be_repeated(box, tgacc, mm, smtp):
    anna, _ = people()
    mail_route.save_rule(kind="request", to=f"e:{anna}")
    mm.replies.append({"text": "Добрый день!\n\nСпасибо за запрос: уточним цену и вернёмся с ответом.", "missing": ["цена"]})
    row = arrive(box, mm, letter(subject="Запрос цены", text="Пришлите цену на песок."),
                 {"kind": "request", "needs_reply": True, "summary": "Просят цену на песок."})
    notice, draft = to_anna(tgacc)
    assert "Следующим сообщением — черновик ответа." in notice and draft.startswith("✉️ Черновик ответа на «Запрос цены»")
    assert _data(mm.of("Ты готовишь ответ")[0]["user"])["points"] is None
    smtp.fail = OSError("connection refused")
    mm.decisions.append(lambda f: {"reply": "Отправляю.", "actions": [
        {"type": "confirm_mail_reply", "draft_id": f["mail_draft"]["id"], "ok": True}]})
    anna_says("ок")
    assert tgacc.sent[-1][0] == "43" and tgacc.sent[-1][1].startswith("Ответ не ушёл: письмо не ушло: connection refused")
    with SessionLocal() as s:
        [d] = s.query(MailDraft).all()
        assert d.status == "failed" and mail_reply.pending_view(s, "43", anna)["not_sent"].startswith("письмо не ушло")
    smtp.fail = None
    mm.decisions.append(lambda f: {"reply": "Отправляю ещё раз.", "actions": [
        {"type": "confirm_mail_reply", "draft_id": f["mail_draft"]["id"], "ok": True}]})
    anna_says("ок, ещё раз", message_id=8)
    assert len(smtp.sent) == 1 and tgacc.sent[-1][1].startswith("✅ Ответ ушёл")
    assert get(row.id).status == "answered"


def test_no_means_no_answer_and_nothing_goes_without_ok(box, tgacc, mm, smtp):
    anna, _ = people()
    mail_route.save_rule(kind="request", to=f"e:{anna}")
    mm.replies.append({"text": "Добрый день!\n\nСпасибо, получили.", "missing": []})
    arrive(box, mm, letter(subject="Запрос"), {"kind": "request", "needs_reply": True})
    mm.decisions.append(lambda f: {"reply": "Хорошо, не отвечаю.", "actions": [
        {"type": "confirm_mail_reply", "draft_id": f["mail_draft"]["id"], "ok": False}]})
    anna_says("не надо отвечать")
    mail_reply.run_pending()
    with SessionLocal() as s:
        assert [d.status for d in s.query(MailDraft)] == ["cancelled"]
        assert mail_reply.pending_view(s, "43", anna) is None
    assert smtp.sent == []
    with pytest.raises(mail_reply.ReplyError, match="не подтверждён"):
        with SessionLocal() as s:
            draft_id = s.query(MailDraft).one().id
        mail_reply.send(draft_id)


def test_an_approved_answer_cut_off_by_a_restart_goes_later_and_a_cut_send_is_not_repeated_by_itself(box, smtp):
    anna, _ = people()
    box.add(letter(subject="Вопрос", message_id="<z1@monolit.by>"))
    mail.check_now()
    [row] = rows()
    old = mail_reply.now() - dt.timedelta(minutes=10)
    with SessionLocal() as s:
        s.add_all([MailDraft(mail_id=row.id, employee_id=anna, to_addrs="ivan@monolit.by", subject="Re: Вопрос", text="Ответ 1",
                             status="approved", decided_at=old),
                   MailDraft(mail_id=row.id, employee_id=anna, to_addrs="ivan@monolit.by", subject="Re: Вопрос", text="Ответ 2",
                             status="sending", decided_at=old)])
        s.commit()
    mail_reply.run_pending()
    assert [m.get_content().split("\n")[0] for m in smtp.sent] == ["Ответ 1"]
    with SessionLocal() as s:
        assert [(d.text, d.status) for d in s.query(MailDraft).order_by(MailDraft.id)] == [("Ответ 1", "sent"), ("Ответ 2", "failed")]


def test_the_unbacked_numbers_of_a_draft_are_found():
    hay = "Счёт № 15 от 01.12.2025 на 120 000 руб. Куб М300."
    assert mail_reply.unbacked("Оплатим счёт № 15 на 120000 руб. до 05.12.2025, куб М300 и 7 машин, 48 часов", hay) == [
        "05.12.2025", "48"]
    assert mail_reply._re("Re: Цена") == "Re: Цена" and mail_reply._re("Цена") == "Re: Цена"


# ----------------------------------------------------------------------------- the skills and the page
def test_the_mail_skills_are_checked_against_what_the_model_was_shown():
    ctx = {"mail_letters": [{"id": 5}], "mail_draft": {"id": 9, "mail_id": 5}, "mail_can_reply": True,
           "team": [{"id": 3, "name": "Иван", "reachable": True}, {"id": 4, "name": "Пётр", "reachable": False}]}
    boss = agent_skills.Person(employee=types.SimpleNamespace(id=2), reviewer=True, full=True)

    def check(action, c=ctx):
        return agent_skills.check(agent_skills.normalize(action), c, boss)
    assert check({"type": "mail_reply", "mail_id": "5", "points": "да"}) == ""
    assert "mail_letters" in check({"type": "mail_reply", "mail_id": 6})
    assert "не настроена" in check({"type": "mail_reply", "mail_id": 5}, {**ctx, "mail_can_reply": False})
    assert check({"type": "confirm_mail_reply", "draft_id": 9, "ok": "да"}) == ""
    assert "mail_draft" in check({"type": "confirm_mail_reply", "draft_id": 8, "ok": True})
    assert check({"type": "mail_pass", "mail_id": 5, "to": "null"}) == ""
    assert check({"type": "mail_pass", "mail_id": 5, "to": "3", "remember": "true"}) == ""
    assert "Telegram" in check({"type": "mail_pass", "mail_id": 5, "to": 4})
    assert "team" in check({"type": "mail_pass", "mail_id": 5, "to": 99})
    worker = agent_skills.Person(employee=types.SimpleNamespace(id=3))
    assert not agent_skills.available(agent_skills.SKILLS["mail_reply"], worker)  # the box is not read
    settings_store.set_many({"mail_enabled": True})
    assert agent_skills.available(agent_skills.SKILLS["mail_reply"], worker)
    assert not agent_skills.available(agent_skills.SKILLS["mail_pass"], agent_skills.Person())
    assert agent_skills.denied({"type": "mail_pass", "mail_id": 5, "to": 4}, worker)
    assert not agent_skills.denied({"type": "mail_pass", "mail_id": 5, "to": None}, worker)
    assert "передать письмо" in agent_skills.describe({"type": "mail_pass", "mail_id": 5, "to": 3})
    assert agent_skills.PAIRED["confirm_mail_reply"] == "mail_reply"


def test_the_rules_and_the_settings_are_kept_on_the_mail_page(client, box, smtp):
    anna, ivan = people()
    r = client.post("/mail/rules", data={"kind": "invoice", "senders": "Ivanov@Krinica.by, krinica.by",
                                         "keywords": "бетон; смета", "to": f"e:{anna}", "make_tasks": "1"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/mail?msg=rule#rules"
    assert "error=" in client.post("/mail/rules", data={"senders": "не адрес", "to": f"e:{anna}"},
                                   follow_redirects=False).headers["location"]
    assert "error=" in client.post("/mail/rules", data={"kind": "invoice"}, follow_redirects=False).headers["location"]
    with SessionLocal() as s:
        [rule] = s.query(MailRule).all()
        assert (rule.kind, rule.senders, rule.keywords, rule.make_tasks) == ("invoice", "ivanov@krinica.by, @krinica.by",
                                                                            "бетон, смета", True)
    page = client.get("/mail").text
    assert "Кто чем занимается" in page and "Ответы на письма" in page
    assert "Счёт от ivanov@krinica.by, @krinica.by со словами «бетон», «смета»" in page
    client.post(f"/mail/rules/{rule.id}/toggle")
    with SessionLocal() as s:
        assert s.get(MailRule, rule.id).active is False
    client.post("/mail/route-settings", data={"mail_hints_auto": "1"})
    assert (settings_store.get("mail_forward"), settings_store.get("mail_hints_auto"),
            settings_store.get("mail_drafts_auto")) == (False, True, False)
    client.post("/mail/reply-settings", data={"mail_sender_name": "  СтройДом  ", "mail_signature": "С уважением",
                                              "mail_reply_examples": "Пример 1\n---\nПример 2"})
    assert settings_store.get("mail_sender_name") == "СтройДом"
    with SessionLocal() as s:
        assert mail_reply.examples(s) == ["Пример 1", "Пример 2"]
    r = client.post("/mail/smtp-test", follow_redirects=False)
    assert smtp.tests == 1 and "msg=" in r.headers["location"]
    assert "отправка работает: вход на smtp.yandex" in client.get(r.headers["location"]).text
    client.post(f"/mail/rules/{rule.id}/delete")
    with SessionLocal() as s:
        assert s.query(MailRule).count() == 0
