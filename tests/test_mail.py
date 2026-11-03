"""№22–23 «Почтальон»: the mailbox is read (only read) every few minutes, each letter is kept once with its attachments
in «Документы»; each letter gets a type from the list, the client or object, the case or task and labels — only what
the letter and the offered lists back — and the panel counts how often the sorting is right."""
import datetime as dt
import email.utils
import hashlib
import json
from email.message import EmailMessage

import pytest

from app import deadlines, doc_intake, entities, llm, mail, settings_store
from app.db import Case, Document, Employee, Entity, EntityLink, MailMessage, SessionLocal, Task


class FakeBox:
    """A mailbox: letters (uid, raw, day); records every look."""

    def __init__(self):
        self.letters, self.validity, self.calls, self.fail, self.labels = [], "777", [], "", []

    def add(self, raw, day=None, uid=None):
        uid = uid or (max([u for u, _, _ in self.letters] or [0]) + 1)
        self.letters.append((uid, raw, day or deadlines.today()))
        return uid

    def test(self):
        if self.fail:
            raise mail.MailError(self.fail)
        return len(self.letters)

    def fetch(self, *, uidvalidity, after_uid, since, limit):
        self.calls.append({"uidvalidity": uidvalidity, "after_uid": after_uid, "since": since, "limit": limit})
        if self.fail:
            raise mail.MailError(self.fail)
        same = uidvalidity == self.validity and after_uid > 0
        uids = sorted(u for u, _, day in self.letters if (u > after_uid if same else day >= since))
        take = uids[:limit]
        return {"uidvalidity": self.validity, "exists": len(self.letters), "more": len(uids) > limit,
                "letters": [{"uid": u, "raw": raw, "size": len(raw), "cut": False} for u, raw, _ in self.letters if u in take]}

    def label(self, uidvalidity, items):
        self.labels.append((uidvalidity, items))


@pytest.fixture
def box(monkeypatch):
    fake = FakeBox()
    monkeypatch.setattr(mail, "_box", lambda cfg: fake)
    settings_store.set_many({"mail_enabled": True, "mail_service": "yandex", "mail_address": "office@stroydom.by",
                             "mail_password": mail.seal("app-pass-123"), "company_name": "СтройДом"})
    return fake


def letter(subject="Счёт на оплату", text="Добрый день! Высылаем счёт № 15 за бетон.", sender=("Иван Петров", "ivan@monolit.by"),
           html=None, attachments=(), message_id=None, in_reply_to=None, headers=None, when=None):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = email.utils.formataddr(sender)
    msg["To"] = "office@stroydom.by"
    msg["Date"] = email.utils.format_datetime(when or dt.datetime.now(dt.timezone.utc))
    msg["Message-ID"] = message_id or email.utils.make_msgid(domain="monolit.by")
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    for k, v in (headers or {}).items():
        msg[k] = v
    if text is not None:
        msg.set_content(text)
        if html:
            msg.add_alternative(html, subtype="html")
    elif html:
        msg.set_content(html, subtype="html")
    for name, data, mime in attachments:
        main, sub = mime.split("/")
        msg.add_attachment(data, maintype=main, subtype=sub, filename=name)
    return msg.as_bytes()


class SortModel:
    """The model of the sorting: answers from a queue (a callable gets the meta and the text); records the calls."""

    def __init__(self):
        self.answers, self.calls, self.down = [], [], False

    def __call__(self, model_name, messages, **kwargs):
        assert "Ты сортируешь входящее письмо" in messages[0]["content"]
        user = messages[1]["content"]
        meta = json.loads(user.split("<data>")[1].split("</data>")[0])
        text = user.split("Текст письма:\n<data>", 1)[1].rsplit("</data>", 1)[0]
        self.calls.append({"meta": meta, "text": text, "system": messages[0]["content"]})
        if self.down:
            raise llm.LLMError("модель недоступна")
        item = self.answers.pop(0) if self.answers else {"kind": "other"}
        return json.dumps(item(meta, text) if callable(item) else item, ensure_ascii=False)


@pytest.fixture
def smodel(monkeypatch):
    fake = SortModel()
    monkeypatch.setattr(llm, "chat", fake)
    return fake


def rows():
    with SessionLocal() as s:
        return s.query(MailMessage).order_by(MailMessage.id).all()


# ----------------------------------------------------------------------------- reading a letter
def test_a_letter_is_read_with_its_cyrillic_headers_html_text_and_real_attachments_only():
    raw = letter(subject="Акт сверки за ноябрь — «Монолит»", text=None,
                 html="<html><head><style>p{color:red}</style><title>x</title></head><body><p>Добрый&nbsp;день!</p>"
                      "<p>Во вложении акт сверки.<br>Подпишите, пожалуйста, до пятницы.</p><ul><li>акт</li><li>счёт</li></ul>"
                      "<script>alert(1)</script></body></html>",
                 attachments=[("Акт сверки.pdf", b"%PDF-1.4 " + b"x" * 2000, "application/pdf"),
                              ("logo.png", b"\x89PNG" + b"0" * 500, "image/png")])
    parsed = mail.parse(raw)
    assert parsed["subject"] == "Акт сверки за ноябрь — «Монолит»"
    assert (parsed["from_name"], parsed["from_addr"]) == ("Иван Петров", "ivan@monolit.by")
    assert parsed["to"] == "office@stroydom.by" and parsed["message_id"].startswith("<")
    assert parsed["text"] == "Добрый день!\n\nВо вложении акт сверки.\nПодпишите, пожалуйста, до пятницы.\n\n• акт\n• счёт"
    assert "color" not in parsed["text"] and "alert" not in parsed["text"]
    assert [a["filename"] for a in parsed["attachments"]] == ["Акт сверки.pdf"]  # the tiny logo is not a document
    assert parsed["sent_at"].tzinfo is not None


def test_the_new_part_of_a_reply_is_told_from_the_quoted_letters_but_a_forward_is_kept_whole():
    fresh, quoted = mail.split_quoted("Да, подтверждаем.\n\n12.11.2025, 10:15, Анна <anna@stroydom.by> пишет:\n> Подтвердите заказ")
    assert fresh == "Да, подтверждаем." and quoted.startswith("12.11.2025") and "Подтвердите" in quoted
    outlook = "Согласны.\nОт: Анна\nОтправлено: 12 ноября\nКому: Иван\nТема: Заказ\nПодтвердите заказ"
    assert mail.split_quoted(outlook)[0] == "Согласны."
    forward = "Посмотри.\n---------- Forwarded message ---------\nОт: Банк\nТема: Выписка\nОстаток 100"
    assert mail.split_quoted(forward) == (forward, "")


def test_imap_names_and_keywords():
    assert mail.imap_utf7("INBOX") == "INBOX" and mail.imap_utf7("Входящие") == "&BBIERQQ+BDQETwRJBDgENQ-"
    assert mail.imap_utf7("A&B") == "A&-B"
    assert mail.keyword("Счёт") == "SYSAI_schet" and mail.keyword("ждёт ответа") == "SYSAI_zhdet_otveta"
    assert mail._imap_date(dt.date(2025, 3, 7)) == "07-Mar-2025"


# ----------------------------------------------------------------------------- keeping the letters (№22)
def test_new_letters_are_kept_once_and_their_attachments_go_to_documents(box):
    box.add(letter(subject="Счёт № 15", attachments=[("счёт_15.pdf", b"%PDF-1.4 " + b"1" * 3000, "application/pdf")]))
    box.add(letter(subject="Вопрос по объекту", text="Когда приедет бригада?"))
    assert mail.check_now() == {"new": 2, "more": False, "error": ""}
    first, second = rows()
    assert (first.subject, first.from_addr, first.mailbox, first.uid, first.status) == (
        "Счёт № 15", "ivan@monolit.by", "office@stroydom.by", 1, "new")
    assert second.text == "Когда приедет бригада?" and not second.attachments
    [att] = first.attachments
    with SessionLocal() as s:
        doc = s.get(Document, att["document_id"])
        assert (doc.chat_id, doc.message_id, doc.filename, doc.kind, doc.text_status) == (
            "mail", first.id * 100 + 1, "счёт_15.pdf", "pdf", "pending")
        assert doc.facts == {"mail_id": first.id} and doc.caption == "Письмо от Иван Петров <ivan@monolit.by>: «Счёт № 15»"
        assert doc.sha256 == hashlib.sha256(b"%PDF-1.4 " + b"1" * 3000).hexdigest()
    state = settings_store.get("mail_state")
    assert (state["uidvalidity"], state["last_uid"], state["ok"], state["new"]) == ("777", 2, True, 2)
    # the next look asks only for what is above the last UID; nothing is kept twice
    assert mail.check_now()["new"] == 0 and box.calls[-1]["after_uid"] == 2 and box.calls[-1]["uidvalidity"] == "777"
    box.add(letter(subject="Ещё письмо"))
    assert mail.check_now()["new"] == 1 and len(rows()) == 3
    with SessionLocal() as s:
        assert s.query(Document).filter_by(chat_id="mail").count() == 1


def test_the_first_look_takes_only_the_last_days_and_a_rebuilt_box_does_not_double_letters(box):
    settings_store.set_many({"mail_history_days": 3})
    today = deadlines.today()
    box.add(letter(subject="Старое"), day=today - dt.timedelta(days=10))
    same = letter(subject="Свежее", message_id="<fresh-1@monolit.by>")
    box.add(same, day=today - dt.timedelta(days=1))
    assert mail.check_now()["new"] == 1 and box.calls[0]["since"] == today - dt.timedelta(days=3)
    assert [r.subject for r in rows()] == ["Свежее"]
    # the server rebuilt the folder: new UIDVALIDITY, new UIDs — the same Message-ID is not kept again
    box.validity, box.letters = "999", [(5, same, today)]
    assert mail.check_now()["new"] == 0 and box.calls[-1]["uidvalidity"] == "777"
    assert settings_store.get("mail_state")["uidvalidity"] == "999" and len(rows()) == 1


def test_a_refusing_box_is_shown_and_not_knocked_on_every_few_minutes(box):
    box.fail = "почта не пустила: проверьте адрес и пароль приложения (AUTHENTICATIONFAILED)"
    assert mail.check_now()["error"].startswith("почта не пустила")
    st = mail.status()
    assert st["ok"] is False and "AUTHENTICATIONFAILED" in st["error"]
    calls = len(box.calls)
    mail.run_due()
    assert len(box.calls) == calls  # the next try in half an hour, not in three minutes
    box.fail = ""
    assert mail.check_now()["error"] == "" and mail.status()["ok"] is True


def test_nothing_is_read_while_the_box_is_off_or_not_connected(box):
    box.add(letter())
    settings_store.set_many({"mail_enabled": False})
    mail.run_due()
    assert box.calls == [] and rows() == []
    settings_store.set_many({"mail_enabled": True, "mail_password": ""})
    assert not mail.enabled()
    mail.run_due()
    assert box.calls == []


def test_the_connection_form_seals_the_password_and_never_shows_it(client, box):
    r = client.post("/mail/settings", data={"present_flags": "1", "mail_service": "gmail", "mail_address": "Office@StroyDom.by",
                                            "mail_password": "abcd efgh ijkl mnop", "password_action": "replace",
                                            "mail_enabled": "1", "mail_folder": "INBOX", "mail_poll_minutes": "5",
                                            "mail_history_days": "2"}, follow_redirects=False)
    assert r.status_code == 303 and "msg=saved" in r.headers["location"]
    sealed = settings_store.get("mail_password")
    assert "abcd" not in sealed and mail.unseal(sealed) == "abcdefghijklmnop"  # Google's groups of four, without spaces
    cfg = mail.connection()
    assert (cfg["address"], cfg["imap_host"], cfg["imap_port"], cfg["smtp_host"]) == (
        "office@stroydom.by", "imap.gmail.com", 993, "smtp.gmail.com")
    page = client.get("/mail").text
    assert "abcdefghijklmnop" not in page and sealed not in page and "сохранён" in page
    # an empty field keeps the password; enabling without one is refused
    client.post("/mail/settings", data={"present_flags": "1", "mail_service": "gmail", "mail_address": "office@stroydom.by",
                                        "password_action": "keep", "mail_enabled": "1"})
    assert mail.unseal(settings_store.get("mail_password")) == "abcdefghijklmnop"
    r = client.post("/mail/settings", data={"present_flags": "1", "mail_service": "gmail", "mail_address": "office@stroydom.by",
                                            "password_action": "clear", "mail_enabled": "1"}, follow_redirects=False)
    assert "error=" in r.headers["location"] and settings_store.get("mail_enabled") is False
    r = client.post("/mail/settings", data={"mail_service": "gmail", "mail_address": "не адрес"}, follow_redirects=False)
    assert "error=" in r.headers["location"]


def test_the_test_button_signs_in_and_the_check_button_looks_into_the_box(client, box):
    box.add(letter(subject="Привет"))
    r = client.post("/mail/test", follow_redirects=False)
    assert "msg=" in r.headers["location"]
    assert "подключено: office@stroydom.by" in client.get(r.headers["location"]).text
    box.fail = "почта не пустила: проверьте адрес и пароль приложения"
    r = client.post("/mail/test", follow_redirects=False)
    assert "error=" in r.headers["location"]
    box.fail = ""
    client.post("/mail/check")
    assert [x.subject for x in rows()] == ["Привет"]


# ----------------------------------------------------------------------------- the sorting (№23)
def monolit(contacts="Иван Петров, ivan@monolit.by"):
    with SessionLocal() as s:
        e = Entity(kind="contractor", name="ООО «Монолит»", aliases=[], contacts=contacts)
        s.add(e)
        s.commit()
        entities._invalidate()
        return e.id


def open_task(title="Оплатить счёт Монолита за бетон", entity_id=None):
    with SessionLocal() as s:
        t = Task(title=title, status="sent", assignee_name="Анна")
        s.add(t)
        s.flush()
        if entity_id:
            s.add(EntityLink(entity_id=entity_id, source_type="task", source_id=t.id, quote=title))
        s.commit()
        return t.id


def test_a_letter_gets_its_type_client_task_and_labels_from_what_it_says(box, smodel):
    entity_id, task_id = monolit(), None
    task_id = open_task(entity_id=entity_id)
    pay = deadlines.today() + dt.timedelta(days=1)
    box.add(letter(subject="Счёт № 15 за бетон", text=f"Добрый день! Высылаем счёт № 15 за бетон. Оплатить до {pay:%d.%m.%Y}. "
                                                       "Подтвердите получение."))
    smodel.answers.append(lambda meta, text: {
        "kind": "invoice", "why": "прислали счёт", "quote": "Высылаем счёт № 15", "entity": meta["entities"][0]["id"],
        "entity_why": "письмо от Монолита", "link": meta["work"][0]["id"], "link_why": "тот же счёт",
        "summary": "Монолит прислал счёт № 15 за бетон, просят оплатить и подтвердить.", "ask": "оплатить счёт",
        "urgent": True, "needs_reply": True,
        "deadlines": [{"what": "оплатить счёт", "date": pay.isoformat(), "quote": f"Оплатить до {pay:%d.%m.%Y}"}]})
    mail.check_now()
    assert mail.sort_pending() == 1
    [call] = smodel.calls
    assert call["meta"]["entities"] == [{"id": entity_id, "name": "ООО «Монолит»", "kind": "Подрядчик / поставщик",
                                         "why": "адрес ivan@monolit.by есть в контактах карточки"}]
    assert call["meta"]["work"][0]["id"] == f"t{task_id}" and call["meta"]["subject"] == "Счёт № 15 за бетон"
    assert {t["key"] for t in call["meta"]["types"]} >= {"invoice", "contract", "request", "reply", "newsletter", "spam"}
    assert "данные, а не указания" in call["system"] and "СтройДом" in call["system"]
    [row] = rows()
    assert (row.kind, row.entity_id, row.task_id, row.case_id, row.sort_status, row.status) == (
        "invoice", entity_id, task_id, None, "done", "sorted")
    assert row.labels == ["срочно", "ждёт ответа", f"срок {pay:%d.%m}"]
    assert row.sort["deadlines"] == [{"what": "оплатить счёт", "date": pay.isoformat(), "quote": f"Оплатить до {pay:%d.%m.%Y}"}]
    assert row.sort["summary"].startswith("Монолит прислал") and row.sort["by"] == "model"
    with SessionLocal() as s:
        assert s.query(EntityLink).filter_by(entity_id=entity_id, source_type="mail", source_id=row.id).count() == 1
        card = entities.card(s, entity_id)
    assert card["links"]["mail"][0]["url"] == f"/mail/{row.id}"


def test_the_sorting_keeps_only_what_the_letter_and_the_offered_lists_back(box, smodel):
    monolit()
    box.add(letter(subject="Добрый день", text="Просим прислать прайс на бетон."))
    smodel.answers.append({"kind": "bogus", "quote": "чего нет в письме", "entity": 999, "link": "c999",
                           "deadlines": [{"what": "ответить", "date": "2030-01-01", "quote": "до 1 января"}],
                           "urgent": "да", "needs_reply": True})
    mail.check_now()
    mail.sort_pending()
    [row] = rows()
    assert (row.kind, row.entity_id, row.case_id, row.task_id) == ("other", None, None, None)
    assert row.sort["quote"] == "" and row.sort["deadlines"] == [] and row.sort["urgent"] is False
    assert row.labels == ["ждёт ответа"]
    # the model may name the type by its name
    smodel.answers.append({"kind": "Запрос"})
    mail.sort_letter(row.id, force=True)
    assert rows()[0].kind == "request"


def test_the_sorting_waits_for_the_attachments_and_uses_what_their_analysis_found(box, smodel):
    box.add(letter(subject="Документы", text="Во вложении.", attachments=[("счёт.pdf", b"%PDF " + b"2" * 3000, "application/pdf")]))
    mail.check_now()
    assert mail.sort_pending() == 0 and smodel.calls == []  # the attachment is not read yet
    [row] = rows()
    with SessionLocal() as s:
        doc = s.get(Document, row.attachments[0]["document_id"])
        doc.text, doc.text_status = "Счёт № 7 от 01.12.2025 ООО «Монолит» к оплате 500,00", "done"
        doc.facts_status, doc.facts = "done", {"mail_id": row.id, "kind": "invoice", "fields": {
            "number": {"value": "7", "quote": "Счёт № 7"}}}
        s.commit()
    assert mail.sort_pending() == 1
    att = smodel.calls[0]["meta"]["attachments"][0]
    assert att["filename"] == "счёт.pdf" and att["type"] == "Счёт" and "№ 7" in att["what"]


def test_when_the_model_is_down_the_letter_is_sorted_by_its_headers_after_a_few_tries(box, smodel):
    smodel.down = True
    box.add(letter(subject="Скидки недели", headers={"List-Unsubscribe": "<mailto:unsub@shop.by>"}, sender=("Магазин", "news@shop.by")))
    mail.check_now()
    [row] = rows()
    assert mail.sort_letter(row.id) == "pending" and mail.sort_letter(row.id) == "pending"
    assert rows()[0].sort_status == "" and rows()[0].sort["attempts"] == 2
    assert mail.sort_letter(row.id) == "done"
    row = rows()[0]
    assert (row.kind, row.sort["by"]) == ("newsletter", "rules") and "модель недоступна" in row.sort["error"]
    assert "массовая рассылка" in smodel.calls[0]["meta"]["signals"][0]


def test_a_reply_in_the_same_conversation_inherits_the_client_and_the_case(box, smodel):
    entity_id = monolit(contacts="")
    with SessionLocal() as s:
        c = Case(title="Получить от Монолита акт за ноябрь", status="waiting")
        s.add(c)
        s.commit()
        case_id = c.id
    box.add(letter(subject="Акт за ноябрь", text="Пришлём акт завтра. ООО «Монолит».", message_id="<a1@monolit.by>"))
    smodel.answers.append(lambda meta, text: {"kind": "reply", "entity": meta["entities"][0]["id"],
                                              "link": f"c{case_id}" if any(w["id"] == f"c{case_id}" for w in meta["work"]) else None})
    mail.check_now()
    mail.sort_pending()
    first = rows()[0]
    assert (first.entity_id, first.case_id) == (entity_id, case_id)
    box.add(letter(subject="Re: Акт за ноябрь", text="Высылаем, как обещали.", in_reply_to="<a1@monolit.by>"))
    smodel.answers.append({"kind": "act", "entity": None, "link": None})
    mail.check_now()
    mail.sort_pending()
    second = rows()[1]
    assert second.thread == "<a1@monolit.by>" and (second.entity_id, second.case_id) == (entity_id, case_id)
    assert set(second.sort["inherited"]) == {"entity", "link"} and second.sort["link_why"] == "та же переписка"
    assert any(e["why"] == "та же переписка" for e in smodel.calls[1]["meta"]["entities"])


def test_the_list_of_types_is_edited_on_the_page_and_known_names_keep_their_keys(client, box, smodel):
    r = client.post("/mail/kinds", data={"text": "Счёт — счета на оплату\nТендер — приглашения к участию в тендерах\n"
                                                 "Рассылка: реклама и новости\n\nСчёт — дубль"}, follow_redirects=False)
    assert "msg=kinds" in r.headers["location"]
    kinds = mail.kinds()
    assert list(kinds) == ["invoice", "t1", "newsletter", "other"]
    assert kinds["t1"] == {"name": "Тендер", "hint": "приглашения к участию в тендерах"}
    box.add(letter(subject="Приглашение к тендеру"))
    smodel.answers.append({"kind": "t1"})
    mail.check_now()
    mail.sort_pending()
    assert rows()[0].kind == "t1" and [t["key"] for t in smodel.calls[0]["meta"]["types"]] == list(kinds)
    assert "Тендер" in client.get("/mail").text
    assert "error=" in client.post("/mail/kinds", data={"text": "  "}, follow_redirects=False).headers["location"]


def test_right_or_fixed_marks_count_the_accuracy_of_the_sorting(client, box, smodel):
    entity_id = monolit()
    box.add(letter(subject="Счёт № 1"))
    box.add(letter(subject="Договор поставки", sender=("Пётр", "petr@other.by")))
    smodel.answers += [{"kind": "invoice"}, {"kind": "invoice"}]
    mail.check_now()
    mail.sort_pending()
    a, b = rows()
    client.post(f"/mail/{a.id}/check", data={"ok": "1", "back": "list"})
    client.post(f"/mail/{b.id}/check", data={"ok": "0", "kind": "contract", "entity_id": str(entity_id)})
    a, b = rows()
    assert a.checked["ok"] is True and (b.kind, b.entity_id, b.checked["ok"], b.checked["was"]["kind"]) == (
        "contract", entity_id, False, "invoice")
    with SessionLocal() as s:
        assert mail.accuracy(s) == {"checked": 2, "right": 1, "share": 50}
    page = client.get("/mail").text
    assert "верно 1 из 2" in page and "Договор поставки" in page
    letter_page = client.get(f"/mail/{b.id}").text
    assert "было «Счёт»" in letter_page and "ООО «Монолит»" in letter_page
    assert "error=" in client.post(f"/mail/{b.id}/check", data={"ok": "0", "kind": "nope"},
                                   follow_redirects=False).headers["location"]


def test_the_type_and_labels_go_into_the_box_only_when_asked(box, smodel):
    entity_id = monolit()
    box.add(letter(subject="Срочно: счёт"))
    smodel.answers.append(lambda meta, text: {"kind": "invoice", "entity": meta["entities"][0]["id"], "urgent": True})
    mail.check_now()
    mail.sort_pending()
    mail.label_pending()
    assert box.labels == []
    settings_store.set_many({"mail_labels": True})
    assert mail.label_pending() == 1
    assert box.labels == [("777", [(1, ["Счёт", "ООО «Монолит»", "срочно"])])]
    assert rows()[0].sort["box"] == "ok" and mail.label_pending() == 0
    assert entity_id


def test_an_attachment_goes_with_its_letter_not_by_the_document_rules_on_its_own(box):
    box.add(letter(attachments=[("счёт.pdf", b"%PDF " + b"3" * 3000, "application/pdf")]))
    mail.check_now()
    [row] = rows()
    with SessionLocal() as s:
        anna = Employee(name="Анна", telegram_chat_id="55", active=True)
        s.add(anna)
        s.flush()
        doc = s.get(Document, row.attachments[0]["document_id"])
        doc.text_status, doc.facts_status = "done", "done"
        doc.facts = {"mail_id": row.id, "kind": "invoice", "route": {"status": "planned", "to_id": anna.id, "to": "Анна"}}
        s.commit()
        doc_id = doc.id
    assert doc_intake.carry_out(doc_id, late=True)["status"] == "in_mail"
    with SessionLocal() as s:
        assert s.query(Case).count() == 0
        assert "в письме" not in doc_intake.ROUTE_RU["in_mail"] and "вместе с письмом" in doc_intake.ROUTE_RU["in_mail"]


def test_the_pages_show_the_letter_its_attachments_and_the_way_back(client, box, smodel):
    box.add(letter(subject="Счёт № 15", text="Высылаем счёт.\n\nВчера, Анна <anna@stroydom.by> пишет:\n> Пришлите счёт",
                   attachments=[("счёт_15.pdf", b"%PDF " + b"4" * 3000, "application/pdf")]))
    smodel.answers.append({"kind": "invoice", "why": "прислали счёт", "summary": "Монолит прислал счёт."})
    mail.check_now()
    [row] = rows()
    with SessionLocal() as s:
        doc = s.get(Document, row.attachments[0]["document_id"])
        doc.text_status = "done"
        doc.facts_status = "skipped"
        s.commit()
    mail.sort_pending()
    page = client.get("/mail").text
    assert "Счёт № 15" in page and "Монолит прислал счёт." in page and "счёт_15.pdf" in page and "Иван Петров" in page
    one = client.get(f"/mail/{row.id}").text
    assert "Высылаем счёт." in one and "Ранее в переписке" in one and "прислали счёт" in one
    assert "ivan@monolit.by" in one
    doc_page = client.get(f"/documents/{row.attachments[0]['document_id']}/view").text
    assert f'href="/mail/{row.id}"' in doc_page and "почта: Иван Петров" in doc_page
    assert client.get("/mail/9999").status_code == 404


# ----------------------------------------------------------------------------- the real IMAP client, on a fake server
class FakeImap:
    """imaplib.IMAP4_SSL as the servers answer: EXAMINE, UID SEARCH, UID FETCH, UID STORE."""
    instances = []

    def __init__(self, host, port, ssl_context=None, timeout=None):
        self.host, self.port, self.commands, self.untagged_responses = host, port, [], {}
        self.capabilities = ("IMAP4REV1", "X-GM-EXT-1")
        self.messages = {7: letter(subject="Первое"), 8: letter(subject="Второе")}
        self.password_ok = True
        FakeImap.instances.append(self)

    def login(self, user, password):
        self.commands.append(("LOGIN", user))
        if password != "app-pass-123":
            import imaplib
            raise imaplib.IMAP4.error(b"[AUTHENTICATIONFAILED] Invalid credentials")
        return "OK", [b"done"]

    def select(self, mailbox, readonly=False):
        self.commands.append(("EXAMINE" if readonly else "SELECT", mailbox))
        self.untagged_responses = {"UIDVALIDITY": [b"4242"], "EXISTS": [b"2"]}
        return "OK", [b"2"]

    def uid(self, command, *args):
        self.commands.append(("UID " + command,) + tuple(a for a in args if a is not None))
        if command == "SEARCH":
            if args[1] == "UID":
                low = int(args[2].split(":")[0])
                found = [u for u in self.messages if u >= low] or [max(self.messages)]  # «N:*» gives the last one anyway
            else:
                found = list(self.messages)
            return "OK", [" ".join(map(str, found)).encode()]
        if command == "FETCH" and args[1] == "(RFC822.SIZE)":
            return "OK", [f"{i} (UID {u} RFC822.SIZE {len(self.messages[int(u)])})".encode()
                          for i, u in enumerate(args[0].split(","), 1)]
        if command == "FETCH":
            u = int(args[0])
            return "OK", [(f"1 (UID {u} BODY[] {{{len(self.messages[u])}}}".encode(), self.messages[u]), b")"]
        if command == "STORE":
            return "OK", [b"1 (X-GM-LABELS (x))"]
        return "NO", [b"unknown"]

    def logout(self):
        self.commands.append(("LOGOUT",))


def test_the_imap_client_reads_only_and_asks_for_what_is_new(monkeypatch):
    import imaplib
    FakeImap.instances.clear()
    monkeypatch.setattr(imaplib, "IMAP4_SSL", FakeImap)
    cfg = {"address": "office@stroydom.by", "password": "app-pass-123", "imap_host": "imap.gmail.com", "imap_port": 993,
           "folder": "INBOX"}
    got = mail.ImapBox(cfg).fetch(uidvalidity="", after_uid=0, since=dt.date(2025, 3, 7), limit=25)
    conn = FakeImap.instances[-1]
    assert got["uidvalidity"] == "4242" and [x["uid"] for x in got["letters"]] == [7, 8] and got["more"] is False
    assert mail.parse(got["letters"][0]["raw"])["subject"] == "Первое"
    assert ("EXAMINE", '"INBOX"') in conn.commands and ("UID SEARCH", "SINCE", "07-Mar-2025") in conn.commands
    assert ("UID FETCH", "7", "(BODY.PEEK[])") in conn.commands  # PEEK: the letter does not become «прочитано»
    assert not any(c[0] in ("SELECT", "UID STORE") for c in conn.commands) and conn.commands[-1] == ("LOGOUT",)
    # the next look: only above the last UID; «8:*» answering with the last letter itself is not a new one
    got = mail.ImapBox(cfg).fetch(uidvalidity="4242", after_uid=8, since=dt.date(2025, 3, 7), limit=25)
    assert got["letters"] == [] and ("UID SEARCH", "UID", "9:*") in FakeImap.instances[-1].commands
    # labels: Gmail labels «SYSAI/…» in modified UTF-7, the folder opened for writing only then
    mail.ImapBox(cfg).label("4242", [(7, ["Счёт", "срочно"])])
    store = [c for c in FakeImap.instances[-1].commands if c[0] == "UID STORE"]
    assert store == [("UID STORE", "7", "+X-GM-LABELS", f'("SYSAI/{mail.imap_utf7("Счёт")}" "SYSAI/{mail.imap_utf7("срочно")}")')]
    assert ("SELECT", '"INBOX"') in FakeImap.instances[-1].commands
    with pytest.raises(mail.MailError, match="пересобрана"):
        mail.ImapBox(cfg).label("1", [(7, ["Счёт"])])
    with pytest.raises(mail.MailError, match="почта не пустила"):
        mail.ImapBox({**cfg, "password": "wrong"}).test()
