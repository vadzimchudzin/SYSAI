"""Documents from the company's templates (plan №17): a DOCX with fields {{…}} uploaded in the panel; the assistant fills
it from the order and the company memory, asks for what is missing, sends the draft to the person who asked and after
their «ок» keeps it in the document library and sends it to whom they said. The fields are filled inside the template,
so its look stays; the PDF is made through WeasyPrint."""
import io
import xml.etree.ElementTree as ET
import zipfile

import pypdf
import pytest

from app import agent, cases, doc_templates, settings_store
from app.db import Case, DocDraft, DocTemplate, Document, SessionLocal
from test_ai_mode import FakeBackend, FakeModel, make_meeting, tgacc  # noqa: F401  (fixtures)
from test_ai_mode import incoming as employee_says
from test_cases import CaseModel, cmodel, incoming, office  # noqa: F401  (fixtures)

W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
STYLES = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
          f'<w:styles {W}><w:style w:type="paragraph" w:styleId="Normal"><w:name w:val="Normal"/></w:style></w:styles>')
# Word cuts «{{Дата}}» into pieces when a part of it was typed or formatted separately: the field is found all the same.
BODY = ('<w:p><w:pPr><w:jc w:val="center"/></w:pPr><w:r><w:rPr><w:b/></w:rPr><w:t>ГАРАНТИЙНОЕ ПИСЬМО</w:t></w:r></w:p>'
        '<w:p><w:r><w:t xml:space="preserve">Дата: {{</w:t></w:r><w:r><w:rPr><w:i/></w:rPr><w:t>Да</w:t></w:r>'
        '<w:r><w:t>та}}</w:t></w:r></w:p>'
        '<w:p><w:r><w:t xml:space="preserve">Кому: {{Получатель}}</w:t></w:r></w:p>'
        '<w:p><w:r><w:t xml:space="preserve">Гарантируем оплату {{ Сумма }} руб. до {{Срок оплаты}}.</w:t></w:r></w:p>'
        '<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Сумма</w:t></w:r></w:p></w:tc>'
        '<w:tc><w:p><w:r><w:rPr><w:b/></w:rPr><w:t>{{Сумма}}</w:t></w:r></w:p></w:tc></w:tr></w:tbl>')
HEADER = '<w:p><w:r><w:t>ООО «Стройка» · письмо от {{Дата}}</w:t></w:r></w:p>'
VALUES = {"Дата": "14.11.2025", "Получатель": "ООО «Монолит»", "Сумма": "120 000", "Срок оплаты": "01.12.2025"}
BOSS = (7, "boss")


def make_docx(body: str = BODY, header: str | None = HEADER) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/'
                   'package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-'
                   'package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override '
                   'PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.'
                   'wordprocessingml.document.main+xml"/></Types>')
        z.writestr("_rels/.rels",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.'
                   'openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.'
                   'openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
                   '</Relationships>')
        z.writestr("word/document.xml", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                                        f"<w:document {W}><w:body>{body}<w:sectPr/></w:body></w:document>")
        if header:
            z.writestr("word/header1.xml", f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:hdr {W}>{header}</w:hdr>')
        z.writestr("word/styles.xml", STYLES)
    return buf.getvalue()


def part(data: bytes, name: str) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return z.read(name).decode("utf-8")


def guarantee(output: str = "both") -> int:
    return doc_templates.save_template(title="Гарантийное письмо", description="Гарантия оплаты заказчику",
                                       filename="гарантийное.docx", data=make_docx(), output=output,
                                       hints={"Сумма": "в рублях, цифрами"}, by="Вадим")


def say(user, text, message_id):
    agent.handle_incoming(incoming(user[0], user[1], text=text, message_id=message_id))


def drafts():
    with SessionLocal() as s:
        return [(d.status, d.values.get("Сумма"), d.shown_at is not None) for d in s.query(DocDraft).order_by(DocDraft.id)]


# ----------------------------------------------------------------------------- the DOCX inside
def test_fields_are_found_in_the_order_of_the_document_even_when_word_cut_them():
    assert doc_templates.fields_of(make_docx()) == ["Дата", "Получатель", "Сумма", "Срок оплаты"]
    assert doc_templates.fields_of(make_docx(header=None, body='<w:p><w:r><w:t>без полей</w:t></w:r></w:p>')) == []
    with pytest.raises(doc_templates.TemplateError):
        doc_templates.fields_of(b"not a zip")


def test_filling_keeps_the_rest_of_the_document_as_it_was():
    data = make_docx()
    out = doc_templates.fill(data, {**VALUES, "Получатель": "ООО «A&B» <Север>\nг. Минск"})
    document = part(out, "word/document.xml")
    ET.fromstring(document)  # still a well-formed Word document
    assert "{{" not in document and "}}" not in document
    assert document.count("<w:b/>") == part(data, "word/document.xml").count("<w:b/>")  # the formatting stays
    assert '<w:jc w:val="center"/>' in document and "<w:i/>" in document and "<w:tbl>" in document
    assert "A&amp;B" in document and "&lt;Север&gt;" in document and "<w:br/>" in document
    assert part(out, "word/styles.xml") == STYLES  # what has no fields is not touched
    assert "письмо от 14.11.2025" in part(out, "word/header1.xml")
    text = doc_templates.text_of(out)
    assert "Дата: 14.11.2025" in text and "Гарантируем оплату 120 000 руб. до 01.12.2025." in text
    with pytest.raises(doc_templates.TemplateError, match="Срок оплаты"):
        doc_templates.fill(data, {k: v for k, v in VALUES.items() if k != "Срок оплаты"})
    sample = doc_templates.fill(data, {"Дата": "[Дата]"}, leave_missing=True)
    assert "{{Получатель}}" in doc_templates.text_of(sample) and "[Дата]" in doc_templates.text_of(sample)


def test_the_pdf_is_made_from_the_filled_document():
    pdf = doc_templates.to_pdf(doc_templates.fill(make_docx(), VALUES), "Гарантийное письмо")
    assert pdf.startswith(b"%PDF")
    text = " ".join(page.extract_text() for page in pypdf.PdfReader(io.BytesIO(pdf)).pages)
    assert "Монолит" in text and "120 000" in text


# ----------------------------------------------------------------------------- the panel
def test_templates_are_uploaded_explained_and_switched_in_the_panel(client):
    page = client.get("/documents").text
    assert "Шаблоны документов" in page and "Шаблонов пока нет" in page
    r = client.post("/documents/templates", data={"title": "Гарантийное письмо", "description": "Гарантия оплаты",
                                                  "output": "both"},
                    files={"file": ("гарантийное.docx", make_docx(), doc_templates.DOCX_MIME)}, follow_redirects=False)
    assert r.status_code == 303 and "msg=saved" in r.headers["location"]
    with SessionLocal() as s:
        t = s.query(DocTemplate).one()
        template_id = t.id
        assert [f["name"] for f in t.fields] == ["Дата", "Получатель", "Сумма", "Срок оплаты"] and t.output == "both"
    page = client.get("/documents").text
    assert "{{Срок оплаты}}" in page and "DOCX и PDF" in page and "пример заполнения" in page
    client.post("/documents/templates", data={"template_id": str(template_id), "title": "Гарантийное письмо",
                                              "description": "Гарантия оплаты", "output": "pdf",
                                              "field_0": "Дата", "hint_0": "сегодня, если не сказали",
                                              "field_2": "Сумма", "hint_2": "в рублях"})
    with SessionLocal() as s:
        t = s.get(DocTemplate, template_id)
        assert t.output == "pdf" and {f["name"]: f["hint"] for f in t.fields}["Сумма"] == "в рублях"
        view = doc_templates.view(s)
    assert view[0]["fields"][0] == {"name": "Дата", "hint": "сегодня, если не сказали"} and view[0]["output"] == "PDF"
    sample = client.get(f"/documents/templates/{template_id}/sample")
    assert sample.status_code == 200 and "[Срок оплаты]" in doc_templates.text_of(sample.content)
    assert client.get(f"/documents/templates/{template_id}/file").content == make_docx()
    bad = client.post("/documents/templates", data={"title": "Акт"},
                      files={"file": ("акт.pdf", b"%PDF-1.4", "application/pdf")}, follow_redirects=False)
    assert "error=" in bad.headers["location"]
    assert ".docx" in client.get(bad.headers["location"]).text
    empty = client.post("/documents/templates", data={"title": "Пустой"}, follow_redirects=True,
                        files={"file": ("пустой.docx", make_docx(body="<w:p><w:r><w:t>текст</w:t></w:r></w:p>", header=None),
                                        doc_templates.DOCX_MIME)})
    assert "нет полей" in empty.text
    client.post(f"/documents/templates/{template_id}/toggle")
    with SessionLocal() as s:
        assert not s.get(DocTemplate, template_id).active and doc_templates.view(s) == []
    client.post(f"/documents/templates/{template_id}/delete")
    with SessionLocal() as s:
        assert s.query(DocTemplate).count() == 0


# ----------------------------------------------------------------------------- the conversation
def test_the_draft_goes_to_the_asker_and_after_ok_is_kept_and_sent(office, tgacc, cmodel, client):
    template_id = guarantee()
    seen = {}

    def make(facts, amount="120 000", reply="Готово, ниже черновик. Если всё верно, ответь «ок» — сохраню и отправлю Анне."):
        seen.update(facts)
        values = {**VALUES, "Сумма": amount}
        return {"reply": reply, "actions": [{"type": "make_document", "template_id": template_id, "values": values,
                                             "title": "Гарантийное письмо ООО Монолит", "send_to": office["anna"]}]}

    cmodel.decisions.append(make)
    say(BOSS, "Сделай гарантийное письмо «Монолиту» на 120 000 до 1 декабря и после проверки отправь Анне", 1)
    template = seen["document_templates"][0]
    assert template["id"] == template_id and template["output"] == "DOCX и PDF"
    assert [f["name"] for f in template["fields"]] == ["Дата", "Получатель", "Сумма", "Срок оплаты"]
    assert {"name": "Сумма", "hint": "в рублях, цифрами"} in template["fields"]
    assert tgacc.sent[-1] == ("7", "Готово, ниже черновик. Если всё верно, ответь «ок» — сохраню и отправлю Анне.")
    assert [(ref, name) for ref, name, _ in tgacc.files] == [("7", "Гарантийное письмо ООО Монолит.docx"),
                                                             ("7", "Гарантийное письмо ООО Монолит.pdf")]
    assert "120 000" in doc_templates.text_of(tgacc.file_data[0]) and tgacc.file_data[1].startswith(b"%PDF")
    assert drafts() == [("draft", "120 000", True)]
    assert not any(ref == "@anna" for ref, _ in tgacc.sent) and not any(ref == "@anna" for ref, *_ in tgacc.files)

    cmodel.decisions.append(lambda facts: seen.update(facts) or make(facts, "130 000", "Поправил, вот новый черновик."))
    say(BOSS, "Сумму поправь на 130 000", 2)
    assert seen["document_draft"]["values"]["Сумма"] == "120 000" and seen["document_draft"]["send_to"] == "Анна Смирнова"
    assert drafts() == [("replaced", "120 000", True), ("draft", "130 000", True)]
    assert len(tgacc.files) == 4 and "130 000" in doc_templates.text_of(tgacc.file_data[2])

    cmodel.decisions.append(lambda facts: {"reply": "Сохранил и отправил Анне.", "actions": [
        {"type": "confirm_document", "draft_id": facts["document_draft"]["id"], "ok": True}]})
    say(BOSS, "ок", 3)
    assert drafts()[-1][0] == "sent"
    sent = [(ref, name, caption) for ref, name, caption in tgacc.files if ref == "@anna"]
    assert [name for _, name, _ in sent] == ["Гарантийное письмо ООО Монолит.docx", "Гарантийное письмо ООО Монолит.pdf"]
    assert sent[0][2].startswith("[deliver]") and sent[1][2] == ""
    with SessionLocal() as s:
        docs = s.query(Document).filter_by(chat_id="generated").order_by(Document.id).all()
        assert [(d.kind, d.text_status) for d in docs] == [("docx", "pending"), ("pdf", "done")]
        assert "130 000" in docs[1].text and docs[0].sha256 and docs[0].data
        assert s.query(DocTemplate).one().uses == 2
    page = client.get("/documents").text
    assert "Сделано по шаблонам" in page and "сохранён и отправлен" in page and "кому: Анна Смирнова" in page
    assert "Гарантийное письмо ООО Монолит.docx" in page and "по шаблону" in page  # in the library too
    with SessionLocal() as s:
        draft_id = s.query(DocDraft).order_by(DocDraft.id.desc()).first().id
    assert client.get(f"/documents/drafts/{draft_id}/pdf").content.startswith(b"%PDF")
    assert client.get(f"/documents/drafts/{draft_id}/xlsx").status_code == 404


def test_what_is_missing_is_asked_not_invented(office, tgacc, cmodel):
    template_id = guarantee("docx")
    cmodel.decisions.append({"reply": "Готово, ниже черновик.", "actions": [
        {"type": "make_document", "template_id": template_id,
         "values": {k: v for k, v in VALUES.items() if k not in ("Сумма", "Срок оплаты")}}]})
    cmodel.decisions.append({"reply": "Какая сумма и до какого числа оплатим?", "actions": []})
    say(BOSS, "Подготовь гарантийное письмо для «Монолита»", 1)
    assert drafts() == [] and tgacc.files == []
    assert tgacc.sent[-1] == ("7", "Какая сумма и до какого числа оплатим?")
    retry = cmodel.calls[-1]["user"]
    assert "«Сумма»" in retry and "«Срок оплаты»" in retry  # the model was told what is missing


def test_a_cancelled_draft_is_not_kept(office, tgacc, cmodel):
    template_id = guarantee("docx")
    cmodel.decisions.append({"reply": "Ниже черновик, проверь.", "actions": [
        {"type": "make_document", "template_id": template_id, "values": VALUES}]})
    say(BOSS, "Сделай гарантийное письмо", 1)
    assert [name for _, name, _ in tgacc.files] == ["Гарантийное письмо.docx"]
    cmodel.decisions.append(lambda facts: {"reply": "Хорошо, не сохраняю.", "actions": [
        {"type": "confirm_document", "draft_id": facts["document_draft"]["id"], "ok": False}]})
    say(BOSS, "не надо, отбой", 2)
    assert drafts() == [("cancelled", "120 000", True)]
    with SessionLocal() as s:
        assert s.query(Document).count() == 0
    with pytest.raises(doc_templates.TemplateError):
        doc_templates.decide(1, "7", True)


def test_a_document_the_director_must_approve_reaches_the_employee_after_yes(tgacc, cmodel):
    make_meeting(task_status="sent")
    template_id = guarantee("docx")
    settings_store.set_many({"agent_skill_modes": {"make_document": "approve"}})
    cmodel.decisions.append(lambda facts: {"reply": "Передал руководителю на одобрение.", "actions": [
        {"type": "make_document", "template_id": facts["document_templates"][0]["id"], "values": VALUES}]})
    agent.handle_incoming(employee_says(text="Сделай гарантийное письмо «Монолиту»"))
    assert drafts() == [] and tgacc.files == []
    with SessionLocal() as s:
        case = s.query(Case).filter_by(kind="skill").one()
        case_id = case.id
        assert case.status == "awaiting_approval"
    assert cases.approve(case_id, "Вадим")
    assert drafts() == [("draft", "120 000", True)]
    ref, name, caption = tgacc.files[-1]
    assert ref == "42" and name == "Гарантийное письмо.docx" and "ответьте «ок»" in caption
    assert template_id


def test_a_template_skill_turned_off_hides_the_templates(office, tgacc, cmodel):
    guarantee()
    settings_store.set_many({"agent_skills_off": ["make_document"]})
    seen = {}
    cmodel.decisions.append(lambda facts: seen.update(facts) or {"reply": "Такие документы я сейчас не делаю.",
                                                                 "actions": []})
    say(BOSS, "Сделай гарантийное письмо", 1)
    assert "document_templates" not in seen and "Документ по шаблону" in seen["skills_off"]
