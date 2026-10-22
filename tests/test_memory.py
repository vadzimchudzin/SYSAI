"""Sprint 4: memory (company, departments, people, notes, documents), skills, search and self-learning."""
import io
import json
import zipfile

import pytest

from app import agent, analyze, cases, config, doctext, llm, memory, settings_store
from app.db import (Department, Document, DocumentChunk, Employee, EmployeeDepartment, Meeting, MemoryVector, Note,
                    SessionLocal, Skill)

PROFILE = """ООО «СисЭйТи» проектирует и монтирует системы вентиляции и кондиционирования в Минской области.

Основные заказчики — застройщики жилых домов и торговые центры; главный объект года — ТЦ «Галерея Борисов».

Акты выполненных работ подписывает только директор Вадим Чудин; без его подписи акт заказчику не отправляют.

Рабочий день с 9:00 до 18:00, планёрка по понедельникам в 10:00 в переговорной."""


@pytest.fixture
def company():
    return seed_company()


def seed_company():
    settings_store.set_many({"company_profile": PROFILE, "company_name": "СисЭйТи"})
    with SessionLocal() as s:
        depts = {k: Department(name=n, description=d, regulations=r) for k, n, d, r in (
            ("acc", "Бухгалтерия", "учёт, зарплата, акты сверки с контрагентами",
             "Счета на оплату согласует главный бухгалтер, платежи свыше 5000 BYN — директор."),
            ("sup", "Снабжение", "закупка оборудования и материалов, работа с поставщиками",
             "Заявку на закупку подают через снабжение не позднее чем за 5 рабочих дней."),
            ("eng", "Технический отдел", "проектирование, чертежи, исполнительная документация",
             "Исполнительную документацию сдают заказчику вместе с актом КС-2."))}
        s.add_all(depts.values())
        s.flush()
        people = {}
        for key, name, pos, dept, resp, comp, style, aliases in (
                ("vadim", "Вадим Чудин", "Директор", None, "подписание договоров и актов, финансы", "", "", "Вадим"),
                ("anna", "Анна Смирнова", "Главный бухгалтер", "acc", "акты сверки, счета, зарплата", "1С",
                 "Пишет коротко, документы присылает только в PDF", "Аня"),
                ("petr", "Пётр Иванов", "Юрист", None, "договоры с заказчиками и претензии", "договорное право",
                 "Предпочитает голосовые сообщения после обеда", "Петя"),
                ("olga", "Ольга Кузнецова", "Менеджер по снабжению", "sup", "закупка вентиляционного оборудования",
                 "переговоры с поставщиками", "", "Оля"),
                ("ivan", "Иван Петров", "Инженер-проектировщик", "eng", "чертежи и исполнительная документация",
                 "AutoCAD, Revit", "", "Ваня"),
                ("igor", "Игорь Соколов", "Прораб", "eng", "монтаж на объекте ТЦ «Галерея Борисов»", "монтаж",
                 "Звонить до 8 утра бесполезно — он на объекте с 7:30", "")):
            e = Employee(name=name, position=pos, responsibilities=resp, competencies=comp, communication_style=style,
                         aliases=aliases, telegram_chat_id=f"@{key}")
            if dept:
                e.department_memberships.append(EmployeeDepartment(department=depts[dept], role=pos))
            s.add(e)
            people[key] = e
        s.flush()
        s.add_all([
            Note(employee_id=people["olga"].id, kind="fact", source="meeting", confidence="high",
                 text="Основной поставщик вентиляторов — компания «ВентСнаб», договор до конца 2026 года"),
            Note(employee_id=people["igor"].id, kind="fact", source="conversation", confidence="medium",
                 text="Монтаж на ТЦ «Галерея Борисов» планирует закончить к 15 декабря"),
            Note(employee_id=people["petr"].id, kind="fact", source="meeting", confidence="high",
                 text="Ведёт претензию к ООО «СтройМонтаж» по просрочке оплаты"),
            Note(department_id=depts["acc"].id, kind="company", source="manual", confidence="high",
                 text="Зарплата выплачивается 5 и 20 числа каждого месяца"),
            Skill(title="Как запросить исполнительную документацию",
                  when_to_use="запрос исполнительной документации, КС-2, актов скрытых работ у подрядчиков",
                  body="Назвать объект и период, перечислить комплект: исполнительные схемы, акты скрытых работ, КС-2. "
                       "Срок — 3 рабочих дня."),
            Skill(title="Тон с подрядчиками", kind="tone", when_to_use="переписка с подрядчиками и субподрядчиками",
                  body="На «вы», без давления, всегда со сроком и с благодарностью за ответ."),
        ])
        doc = Document(employee_id=people["anna"].id, chat_id="43", message_id=1, filename="Акт сверки ВентСнаб.txt",
                       mime="text/plain", sha256="x", kind="other", text_status="pending",
                       data="Акт сверки взаимных расчётов с ООО «ВентСнаб» за 3 квартал 2026 года.\n"
                            "Задолженность СисЭйТи перед ВентСнаб на 30.09.2026 составляет 12 480,00 BYN.".encode())
        s.add(doc)
        s.commit()
        ids = {k: v.id for k, v in people.items()}
        ids["doc"] = doc.id
        ids.update({f"dept_{k}": v.id for k, v in depts.items()})
    doctext.process(ids["doc"])
    return ids


# (question, expected key prefix in the top 5, from the point of view of the director)
GOLDEN = [
    ("Кто подписывает акты выполненных работ?", "company:2"),
    ("Чем занимается наша компания?", "company:0"),
    ("Кто наши основные заказчики?", "company:1"),
    ("Во сколько планёрка и где?", "company:3"),
    ("Кто отвечает за акты сверки?", "person:anna"),
    ("Кто у нас юрист?", "person:petr"),
    ("Кто занимается закупкой вентиляционного оборудования?", "person:olga"),
    ("Кто делает чертежи?", "person:ivan"),
    ("Кто прораб на Галерее Борисов?", "person:igor"),
    ("Как Анна предпочитает получать документы?", "person:anna"),
    ("Когда лучше звонить Игорю?", "person:igor"),
    ("Кто основной поставщик вентиляторов?", "note"),
    ("Когда закончат монтаж в торговом центре?", "note"),
    ("Что с претензией к СтройМонтажу?", "note"),
    ("Когда выплачивается зарплата?", "note"),
    ("Кто согласует платежи больше 5000?", "department:acc"),
    ("За сколько дней подавать заявку на закупку?", "department:sup"),
    ("Когда сдают исполнительную документацию заказчику?", "department:eng"),
    ("Какая задолженность перед ВентСнабом по акту сверки?", "chunk"),
    ("Как запросить у подрядчика исполнительную документацию?", "skill"),
]


def _key(prefix: str, ids: dict) -> str:
    kind, _, name = prefix.partition(":")
    if kind == "person":
        return f"person:{ids[name]}"
    if kind == "department":
        return f"department:{ids['dept_' + name]}"
    return prefix


def test_golden_twenty_questions_find_the_right_memory(company):
    misses = []
    for question, prefix in GOLDEN:
        keys = [h["key"] for h in memory.search(question, k=5)]
        want = _key(prefix, company)
        if not any(k == want or (":" not in prefix and k.startswith(prefix + ":")) for k in keys):
            misses.append((question, want, keys))
    assert not misses, misses  # words only, no model: all 20 must be found


def test_stems_join_word_forms():
    assert memory.stem("сверки") == memory.stem("сверку") == memory.stem("сверка")
    assert memory.stem("договоров") == memory.stem("договорами") == memory.stem("договоры")


def test_an_employee_never_sees_notes_about_colleagues(company):
    hits = memory.search("поставщик вентиляторов ВентСнаб", k=10, viewer=company["igor"])
    assert all(h["type"] != "note" or h["owner"] == company["igor"] for h in hits)
    assert all(h["type"] != "person" or h["owner"] == company["igor"] for h in hits)
    own = memory.search("монтаж Галерея", k=10, viewer=company["igor"])
    assert any(h["type"] == "note" for h in own)
    company_wide = memory.search("Кто подписывает акты", k=5, viewer=company["igor"])
    assert company_wide[0]["key"] == "company:2"


def test_archived_notes_are_forgotten(company):
    with SessionLocal() as s:
        note = s.query(Note).filter(Note.text.like("%ВентСнаб%")).one()
    assert memory.edit_note(note.id, archived=True, by="Вадим")
    assert all(h["key"] != f"note:{note.id}" for h in memory.search("поставщик вентиляторов", k=10))


def test_embeddings_find_meaning_without_shared_words(company, monkeypatch):
    calls = []

    def fake_embed(texts, model):
        calls.append(len(texts))
        return [[1.0, 0.0] if ("претензи" in t or "судиться" in t) else [0.0, 1.0] for t in texts]
    monkeypatch.setattr(config, "llm_enabled", lambda: True)
    monkeypatch.setattr(llm, "embed", fake_embed)
    hits = memory.search("с кем мы собираемся судиться", k=3)
    assert any(h["key"].startswith("note:") and "СтройМонтаж" in h["text"] for h in hits[:2])
    assert "претензи" in hits[0]["text"]  # the lawyer's card or the claim itself — both are about the claim
    with SessionLocal() as s:
        assert s.query(MemoryVector).count() >= 15
    calls.clear()
    memory.search("ещё раз про суд", k=3)
    assert calls == [1]  # only the question is embedded again: item vectors are reused


def test_memory_reaches_the_conversation_prompt(company, monkeypatch):
    from app.db import ConversationMessage
    from types import SimpleNamespace
    with SessionLocal() as s:
        row = ConversationMessage(chat_id="7", message_id=1, role="user", status="agent_pending",
                                  text="Кто у нас подписывает акты?", result={})
        s.add(row)
        s.commit()
        ctx = agent._context(s, row, {"name": "Вадим"}, None, reviewer=True, full=True, director=True)
    assert ctx["known_facts"][0]["type"] == "Компания" and "директор" in ctx["known_facts"][0]["text"]
    with SessionLocal() as s:
        igor = s.get(Employee, company["igor"])
        row = ConversationMessage(chat_id="99", message_id=2, role="user", status="agent_pending",
                                  text="Кто поставляет вентиляторы?", result={})
        s.add(row)
        s.commit()
        ctx = agent._context(s, row, {"name": "Игорь"}, igor, reviewer=False, full=False)
    assert not any("ВентСнаб" in f["text"] and f["type"] == "Заметка" for f in ctx["known_facts"])


def test_skills_and_communication_style_reach_case_messages(company, monkeypatch):
    seen = []

    def fake_chat(model, messages, **kw):
        seen.append((messages[0]["content"], json.loads(messages[1]["content"].split("<data>")[1].split("</data>")[0])))
        return json.dumps({"text": "ok"})
    monkeypatch.setattr(llm, "chat", fake_chat)
    cases._write("request", {"task": {"title": "Запросить у подрядчика исполнительную документацию по КС-2"},
                             "person": {"name": "Анна", "communication_style": "только PDF"}})
    system, facts = seen[-1]
    assert facts["skills"][0]["title"] == "Как запросить исполнительную документацию"
    assert "следуй ему" in system and facts["person"]["communication_style"] == "только PDF"
    with SessionLocal() as s:
        assert s.query(Skill).filter_by(title="Как запросить исполнительную документацию").one().uses == 1


def test_company_profile_reaches_the_meeting_analysis(company, monkeypatch):
    captured = {}
    monkeypatch.setattr(analyze.config, "llm_enabled", lambda: True)

    def fake(model, system, text, *a, **k):
        captured["system"] = system
        return {"title": "x", "summary": "", "participants": [], "topics": [], "decisions": [], "tasks": [],
                "open_questions": [], "risks": [], "notes": [], "next_meeting": None}
    monkeypatch.setattr(analyze, "_request_verified_report", fake)
    import datetime as dt
    analyze.analyze([{"start": 0, "end": 3, "speaker": "Спикер 1", "text": "Обсудили акты."}], [],
                    {"report_model": "m", "company_profile": PROFILE}, dt.date(2026, 10, 22))
    assert "Галерея Борисов" in captured["system"]


# ----------------------------------------------------------------------------- reading documents
def _docx(paragraphs):
    xml = ('<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
           '<w:body>' + "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs) + "</w:body></w:document>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", xml)
    return buf.getvalue()


def _xlsx(rows):
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    strings, cells = [], []
    for r, row in enumerate(rows, start=1):
        xs = []
        for c, value in enumerate(row):
            ref = f"{'ABCDEFG'[c]}{r}"
            if isinstance(value, str):
                strings.append(value)
                xs.append(f'<c r="{ref}" t="s"><v>{len(strings) - 1}</v></c>')
            else:
                xs.append(f'<c r="{ref}"><v>{value}</v></c>')
        cells.append(f'<row r="{r}">{"".join(xs)}</row>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/workbook.xml", f'<workbook {ns}><sheets><sheet name="Смета" sheetId="1"/></sheets></workbook>')
        z.writestr("xl/sharedStrings.xml", f'<sst {ns}>' + "".join(f"<si><t>{x}</t></si>" for x in strings) + "</sst>")
        z.writestr("xl/worksheets/sheet1.xml", f'<worksheet {ns}><sheetData>{"".join(cells)}</sheetData></worksheet>')
    return buf.getvalue()


def _pdf(text):
    from weasyprint import HTML
    return HTML(string=f"<p>{text}</p>").write_pdf()


def test_docx_xlsx_pdf_are_read_without_a_model():
    assert "Договор поставки № 15" in doctext.extract(_docx(["Договор поставки № 15", "Срок — 30 дней"]),
                                                       "", "договор.docx", "docx")
    table = doctext.extract(_xlsx([["Позиция", "Сумма"], ["Вентилятор ВКР-4", 1250]]), "", "смета.xlsx", "xlsx")
    assert "Лист: Смета" in table and "Вентилятор ВКР-4\t1250" in table
    assert "Акт выполненных работ" in doctext.extract(_pdf("Акт выполненных работ № 17 от 20.10.2026 на сумму 4 500 BYN"),
                                                      "application/pdf", "акт.pdf", "pdf")


def test_scans_and_photos_go_to_the_vision_model(monkeypatch):
    sent = []

    def fake_chat(model, messages, **kw):
        sent.append(messages[1]["content"][1])
        return "СЧЁТ № 88 от 21.10.2026, сумма 980 BYN"
    monkeypatch.setattr(llm, "chat", fake_chat)
    assert "СЧЁТ № 88" in doctext.extract(b"\xff\xd8 jpeg", "image/jpeg", "фото.jpg", "image")
    assert sent[-1]["type"] == "image_url" and sent[-1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    blank = _pdf("&nbsp;")  # a PDF without a text layer, like a scan
    assert "СЧЁТ № 88" in doctext.extract(blank, "application/pdf", "скан.pdf", "pdf")
    assert sent[-1]["type"] == "file" and sent[-1]["file"]["file_data"].startswith("data:application/pdf;base64,")


def test_vision_outage_leaves_the_document_pending_then_failed(monkeypatch):
    def down(*a, **k):
        raise llm.LLMError("модель недоступна")
    monkeypatch.setattr(llm, "chat", down)
    with SessionLocal() as s:
        d = Document(chat_id="1", message_id=5, filename="скан.jpg", mime="image/jpeg", sha256="y", kind="image",
                     data=b"\xff\xd8", text_status="pending")
        s.add(d)
        s.commit()
        did = d.id
    assert doctext.process(did) == "pending"
    doctext.process(did)
    assert doctext.process(did) == "failed"


def test_chunks_cover_long_text_with_overlap():
    text = "\n".join(f"Пункт {i}. " + "слово " * 40 for i in range(40))
    pieces = doctext.chunks(text)
    assert len(pieces) > 5 and all(len(p) <= doctext.CHUNK + doctext.OVERLAP + 300 for p in pieces)
    assert "Пункт 0." in pieces[0] and "Пункт 39." in pieces[-1]


def test_questions_about_a_document_are_answered_from_its_text(company, monkeypatch):
    def fake_chat(model, messages, **kw):
        assert "12 480,00 BYN" in messages[1]["content"]
        return "Задолженность — 12 480,00 BYN на 30.09.2026."
    monkeypatch.setattr(llm, "chat", fake_chat)
    result = memory.answer_about_document(company["doc"], "Какая задолженность?")
    assert "12 480,00" in result["answer"] and result["pieces"]


# ----------------------------------------------------------------------------- learning after a meeting
TRANSCRIPT = [
    {"start": 0, "end": 10, "speaker": "Вадим", "text": "Ольга, с этого месяца все закупки фильтров идут только через ВентСнаб."},
    {"start": 10, "end": 20, "speaker": "Ольга", "text": "Хорошо, я буду согласовывать заявки на фильтры каждую пятницу."},
    {"start": 20, "end": 30, "speaker": "Вадим", "text": "Бухгалтерия теперь принимает счета только до четверга включительно."},
]


def test_meeting_teaches_memory_only_facts_with_real_quotes(company, monkeypatch):
    with SessionLocal() as s:
        m = Meeting(title="Планёрка", status="done", transcript=TRANSCRIPT, report={}, options={})
        s.add(m)
        s.commit()
        mid = m.id
    olga, acc = company["olga"], company["dept_acc"]
    proposed = {"facts": [
        {"subject": "person", "employee_id": olga, "text": "Согласовывает заявки на фильтры каждую пятницу",
         "quote": "я буду согласовывать заявки на фильтры каждую пятницу", "confidence": "high"},
        {"subject": "department", "department_id": acc, "text": "Счета принимаются только до четверга включительно",
         "quote": "Бухгалтерия теперь принимает счета только до четверга включительно", "confidence": "high"},
        {"subject": "person", "employee_id": olga, "text": "Ольга ленивая и всё забывает",
         "quote": "Ольга, с этого месяца все закупки", "confidence": "low"},
        {"subject": "person", "employee_id": olga, "text": "Ольга часто болеет весной",
         "quote": "Ольга, с этого месяца все закупки фильтров", "confidence": "low"},
        {"subject": "person", "employee_id": olga, "text": "Подписывает договоры с банком",
         "quote": "Ольга подписывает договоры с банком", "confidence": "medium"},  # not said
        {"subject": "person", "employee_id": 9999, "text": "Неизвестный отвечает за склад",
         "quote": "все закупки фильтров идут только через ВентСнаб", "confidence": "medium"},
        {"subject": "person", "employee_id": olga,
         "text": "Основной поставщик вентиляторов — компания «ВентСнаб», договор до конца 2026 года",
         "quote": "все закупки фильтров идут только через ВентСнаб", "confidence": "high"},  # already known
    ]}
    monkeypatch.setattr(llm, "chat", lambda *a, **k: json.dumps(proposed, ensure_ascii=False))
    settings_store.set_many({"memory_autolearn": True})
    assert memory.learn_from_meeting(mid) == 2
    with SessionLocal() as s:
        learned = s.query(Note).filter_by(meeting_id=mid).order_by(Note.id).all()
        assert [n.text for n in learned] == ["Согласовывает заявки на фильтры каждую пятницу",
                                             "Счета принимаются только до четверга включительно"]
        assert learned[0].quote and learned[0].confidence == "high" and learned[1].department_id == acc
    assert memory.learn_from_meeting(mid) == 0  # once per meeting
    hits = memory.search("до какого дня принимают счета", k=3)
    assert any("четверга" in h["text"] for h in hits)


def test_learning_tolerates_a_name_in_subject(company, monkeypatch):
    """Found by the live check: the model wrote «Ольга Кузнецова» instead of "person" and every fact was lost."""
    with SessionLocal() as s:
        m = Meeting(title="Планёрка", status="done", transcript=TRANSCRIPT, report={}, options={})
        s.add(m)
        s.commit()
        mid = m.id
    proposed = {"facts": [
        {"subject": "Ольга Кузнецова", "employee_id": company["olga"], "department_id": None,
         "text": "Ольга будет согласовывать заявки на фильтры каждую пятницу",
         "quote": "я буду согласовывать заявки на фильтры каждую пятницу", "confidence": "high"},
        {"subject": "Бухгалтерия", "employee_id": None, "department_id": company["dept_acc"],
         "text": "Бухгалтерия принимает счета только до четверга включительно",
         "quote": "Бухгалтерия теперь принимает счета только до четверга включительно", "confidence": "high"}]}
    monkeypatch.setattr(llm, "chat", lambda *a, **k: json.dumps(proposed, ensure_ascii=False))
    settings_store.set_many({"memory_autolearn": True})
    assert memory.learn_from_meeting(mid) == 2
    with SessionLocal() as s:
        notes = s.query(Note).filter_by(meeting_id=mid).order_by(Note.id).all()
        assert notes[0].employee_id == company["olga"] and notes[1].department_id == company["dept_acc"]


def test_autolearn_can_be_switched_off(company, monkeypatch):
    settings_store.set_many({"memory_autolearn": False})
    monkeypatch.setattr(llm, "chat", lambda *a, **k: pytest.fail("the model must not be called"))
    assert memory.learn_from_meeting(1) == 0


@pytest.mark.parametrize("text, ok", [("Отвечает за закупку фильтров", True), ("Иван ленивый", False),
                                      ("Беременна, уходит в декрет", False), ("Плохо работает с клиентами", False),
                                      ("ок", False)])
def test_personal_data_guard(text, ok):
    assert (memory.acceptable_fact(text) == "") is ok


def test_quote_must_be_in_the_transcript():
    t = analyze.transcript_text(TRANSCRIPT)
    assert memory.quote_found("Бухгалтерия теперь принимает счета только до четверга", t)
    assert memory.quote_found("бухгалтерия теперь принимает счета только до четверга включительно!", t)
    assert not memory.quote_found("Бухгалтерия принимает счета по пятницам", t)
    assert not memory.quote_found("счета", t)


# ----------------------------------------------------------------------------- pages
def test_memory_page_search_and_editing(client, company):
    page = client.get("/memory").text
    assert "Галерея Борисов" in page and "Бухгалтерия" in page and "Анна Смирнова" in page
    found = client.get("/memory?q=Кто подписывает акты").text
    assert "Акты выполненных работ подписывает только директор" in found
    assert client.post("/memory/company", data={"company_profile": PROFILE + "\n\nНовый абзац про склад.",
                                                "memory_autolearn": "on"}, follow_redirects=False).status_code == 303
    assert "склад" in settings_store.get("company_profile")
    r = client.post("/memory/notes", data={"text": "Склад работает до 17:00", "department_id": str(company["dept_sup"])},
                    follow_redirects=False)
    assert r.status_code == 303
    with SessionLocal() as s:
        note = s.query(Note).filter_by(text="Склад работает до 17:00").one()
        assert note.source == "manual" and note.department_id == company["dept_sup"]
    client.post(f"/memory/notes/{note.id}", data={"text": "Склад работает до 17:30", "confidence": "medium"})
    client.post(f"/memory/notes/{note.id}", data={"action": "archive"})
    with SessionLocal() as s:
        note = s.get(Note, note.id)
        assert note.text == "Склад работает до 17:30" and note.archived and note.edited_by
    client.post(f"/memory/departments/{company['dept_acc']}", data={"description": "учёт", "regulations": "Новый регламент"})
    with SessionLocal() as s:
        assert s.get(Department, company["dept_acc"]).regulations == "Новый регламент"


def test_person_card(client, company):
    from app.db import PersonMemory
    with SessionLocal() as s:
        s.add(PersonMemory(chat_id="43", employee_id=company["anna"], summary="Любит, когда напоминают заранее"))
        s.commit()
    page = client.get(f"/memory/people/{company['anna']}").text
    assert "Главный бухгалтер" in page and "только в PDF" in page and "Любит, когда напоминают" in page
    client.post(f"/memory/people/{company['anna']}", data={"communication_style": "Лучше писать утром"})
    client.post(f"/memory/people/{company['anna']}/forget-summary")
    with SessionLocal() as s:
        assert s.get(Employee, company["anna"]).communication_style == "Лучше писать утром"
        assert s.query(PersonMemory).one().summary == ""
    assert client.get("/memory/people/9999").status_code == 404


def test_skills_page(client, company):
    assert "Тон с подрядчиками" in client.get("/skills").text
    client.post("/skills", data={"title": "Проверка перед отправкой акта", "kind": "scenario", "when_to_use": "акты",
                                 "body": "Проверить подпись директора и печать.", "active": "on"})
    with SessionLocal() as s:
        skill = s.query(Skill).filter_by(title="Проверка перед отправкой акта").one()
    assert client.post("/skills", data={"title": "", "body": ""}).status_code == 400
    client.post(f"/skills/{skill.id}/toggle")
    with SessionLocal() as s:
        assert s.get(Skill, skill.id).active is False
    assert "Проверка перед отправкой акта" in client.get(f"/skills?edit={skill.id}").text
    client.post(f"/skills/{skill.id}/delete")
    with SessionLocal() as s:
        assert s.get(Skill, skill.id) is None


def test_document_page_with_text_and_question(client, company, monkeypatch):
    monkeypatch.setattr(llm, "chat", lambda *a, **k: "Задолженность 12 480,00 BYN.")
    page = client.get(f"/documents/{company['doc']}/view?q=Какая задолженность").text
    assert "12 480,00 BYN" in page and "Акт сверки взаимных расчётов" in page
    assert "Акт сверки ВентСнаб" in client.get("/documents?q=задолженность").text  # full-text search
    assert client.post(f"/documents/{company['doc']}/reread", follow_redirects=False).status_code == 303


def test_memory_is_for_superusers_only(client, company):
    from app import accounts
    from app.db import User
    with SessionLocal() as s:
        s.add(User(username="viewer2", password_hash=accounts.hash_password("viewer-pass-123"), role="user",
                   status="approved"))
        s.commit()
    client.get("/logout")
    client.post("/login", data={"username": "viewer2", "password": "viewer-pass-123"})
    for url in ("/memory", f"/memory/people/{company['anna']}", "/skills", f"/documents/{company['doc']}/view"):
        assert client.get(url, follow_redirects=False).status_code == 403
