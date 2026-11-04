"""Documents from the company's templates (plan №17).

A DOCX template has fields {{Дата}}, {{Получатель}}… The fields are filled inside the template itself, so the look of
the document stays exactly as it was made in Word (fonts, tables, the letterhead); a field split by Word into several
pieces of text is found all the same. A PDF is made from the filled document through WeasyPrint. The assistant fills
the fields from the order and the company memory and asks for what is missing (agent_skills: make_document); the draft
goes to the person who asked, and after their «ок» it is kept in the document library and sent to the person named.
"""
import datetime as dt
import hashlib
import html
import io
import logging
import re
import zipfile
import xml.etree.ElementTree as ET

from . import userbot
from .db import DocDraft, DocTemplate, Document, Employee, SessionLocal, now

log = logging.getLogger("sysai.documents")

FIELD = re.compile(r"\{\{\s*([^{}]{1,80}?)\s*\}\}")
MAX_TEMPLATE = 5 * 1024 * 1024
OUTPUTS = {"docx": "DOCX", "pdf": "PDF", "both": "DOCX и PDF"}
DRAFT_DAYS = 3  # a draft nobody answered is not asked about after that
_TEXT = re.compile(r"(<w:t(?:\s[^>]*)?>)(.*?)(</w:t>)", re.S)
_PART = re.compile(r"word/(document|header\d*|footer\d*|footnotes|endnotes)\.xml$")
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


class TemplateError(ValueError):
    """The template or the values cannot make a document; the text says why, in words for people."""


# ----------------------------------------------------------------------------- the DOCX inside
def _parts(z: zipfile.ZipFile) -> list:
    return [n for n in z.namelist() if _PART.match(n)]


def _segments(xml: str) -> list:
    """The XML cut after every paragraph: a field never spans two paragraphs."""
    return re.split(r"(?<=</w:p>)", xml)


def _texts(segment: str) -> list:
    return [html.unescape(m.group(2)) for m in _TEXT.finditer(segment)]


def _escape(text: str) -> str:
    out = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    out = out.replace("\r\n", "\n").replace("\n", '</w:t><w:br/><w:t xml:space="preserve">')
    return out.replace("\t", '</w:t><w:tab/><w:t xml:space="preserve">')


def _fill_segment(segment: str, values: dict, missing: list) -> str:
    matches = list(_TEXT.finditer(segment))
    texts = [html.unescape(m.group(2)) for m in matches]
    joined = "".join(texts)
    if "{{" not in joined:
        return segment
    covered, starts = [False] * len(joined), {}
    for m in FIELD.finditer(joined):
        name = " ".join(m.group(1).split())
        if name not in values:
            if name not in missing:
                missing.append(name)
            continue
        for k in range(m.start(), m.end()):
            covered[k] = True
        starts[m.start()] = str(values[name])
    if not starts:
        return segment
    out, last, pos = [], 0, 0
    for m, text in zip(matches, texts):
        a, b = pos, pos + len(text)
        pos = b
        new = "".join((starts.get(j, "") + ("" if covered[j] else joined[j])) for j in range(a, b))
        if new == text:
            continue
        out.append(segment[last:m.start()])
        out.append('<w:t xml:space="preserve">' + _escape(new) + "</w:t>")
        last = m.end()
    out.append(segment[last:])
    return "".join(out)


def fields_of(data: bytes) -> list:
    """The names of the fields {{…}} of a DOCX, in the order of the document (headers and footers too)."""
    names = []
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for part in _parts(z):
                xml = z.read(part).decode("utf-8")
                for segment in _segments(xml):
                    for m in FIELD.finditer("".join(_texts(segment))):
                        name = " ".join(m.group(1).split())
                        if name not in names:
                            names.append(name)
    except (zipfile.BadZipFile, KeyError, UnicodeDecodeError) as exc:
        raise TemplateError("это не документ Word (.docx)") from exc
    return names


def fill(data: bytes, values: dict, *, leave_missing: bool = False) -> bytes:
    """The template with its fields replaced by the values; everything else stays byte for byte."""
    values = {" ".join(str(k).split()): ("" if v is None else str(v)) for k, v in (values or {}).items()}
    missing: list = []
    source = io.BytesIO(data)
    target = io.BytesIO()
    try:
        with zipfile.ZipFile(source) as zin, zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                content = zin.read(item.filename)
                if _PART.match(item.filename):
                    xml = content.decode("utf-8")
                    content = "".join(_fill_segment(x, values, missing) for x in _segments(xml)).encode("utf-8")
                zout.writestr(item, content)
    except (zipfile.BadZipFile, UnicodeDecodeError) as exc:
        raise TemplateError("это не документ Word (.docx)") from exc
    if missing and not leave_missing:
        raise TemplateError("не хватает полей: " + ", ".join(missing))
    return target.getvalue()


def text_of(data: bytes) -> str:
    """The plain text of a DOCX (the library keeps it for the search)."""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        root = ET.fromstring(z.read("word/document.xml"))
    def line(paragraph) -> str:
        out = []
        for node in paragraph.iter():
            if node.tag == _W + "t":
                out.append(node.text or "")
            elif node.tag in (_W + "br", _W + "cr"):
                out.append("\n")
            elif node.tag == _W + "tab":
                out.append("\t")
        return "".join(out).strip()
    lines = [line(p) for p in root.iter(_W + "p")]
    return "\n".join(x for x in lines if x)


# ----------------------------------------------------------------------------- the PDF through WeasyPrint
def _on(props, tag: str) -> bool:
    node = props.find(_W + tag) if props is not None else None
    return node is not None and node.get(_W + "val", "true") not in ("0", "false", "none")


def _runs_html(paragraph) -> str:
    out = []
    for run in paragraph.iter(_W + "r"):
        props = run.find(_W + "rPr")
        pieces = []
        for node in run:
            if node.tag == _W + "t":
                pieces.append(html.escape(node.text or ""))
            elif node.tag == _W + "tab":
                pieces.append("&emsp;")
            elif node.tag in (_W + "br", _W + "cr"):
                pieces.append("<br>")
        text = "".join(pieces)
        if not text:
            continue
        if _on(props, "b"):
            text = f"<b>{text}</b>"
        if _on(props, "i"):
            text = f"<i>{text}</i>"
        if props is not None and props.find(_W + "u") is not None and props.find(_W + "u").get(_W + "val") != "none":
            text = f"<u>{text}</u>"
        out.append(text)
    return "".join(out)


def _paragraph_html(paragraph) -> str:
    props = paragraph.find(_W + "pPr")
    style = (props.find(_W + "pStyle").get(_W + "val", "") if props is not None and props.find(_W + "pStyle") is not None
             else "")
    align = (props.find(_W + "jc").get(_W + "val", "") if props is not None and props.find(_W + "jc") is not None else "")
    css = {"center": "center", "right": "right", "end": "right", "both": "justify"}.get(align)
    body = _runs_html(paragraph) or "&nbsp;"
    tag = "h1" if style.lower() in ("title", "заголовок") else "h2" if re.match(r"(heading|заголовок)\s*\d", style, re.I) else "p"
    return f'<{tag}{f" style=\"text-align:{css}\"" if css else ""}>{body}</{tag}>'


def html_of(data: bytes, title: str = "") -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        root = ET.fromstring(z.read("word/document.xml"))
    body = root.find(_W + "body")
    parts = []
    for node in list(body) if body is not None else []:
        if node.tag == _W + "p":
            parts.append(_paragraph_html(node))
        elif node.tag == _W + "tbl":
            rows = []
            for row in node.iter(_W + "tr"):
                cells = ["<td>" + "".join(_paragraph_html(p) for p in cell.iter(_W + "p")) + "</td>"
                         for cell in row.findall(_W + "tc")]
                rows.append("<tr>" + "".join(cells) + "</tr>")
            parts.append("<table>" + "".join(rows) + "</table>")
    return ("<!doctype html><html lang=\"ru\"><head><meta charset=\"utf-8\"><title>" + html.escape(title) + "</title>"
            "<style>@page{size:A4;margin:20mm 18mm}body{font-family:'Times New Roman','Liberation Serif','DejaVu Serif',"
            "serif;font-size:12pt;line-height:1.35}p{margin:0 0 6pt}h1{font-size:15pt;text-align:center;margin:0 0 10pt}"
            "h2{font-size:13pt;margin:10pt 0 6pt}table{border-collapse:collapse;width:100%;margin:6pt 0}"
            "td{border:1px solid #444;padding:3pt 5pt;vertical-align:top}td p{margin:0}</style></head><body>"
            + "".join(parts) + "</body></html>")


def to_pdf(data: bytes, title: str = "") -> bytes:
    from weasyprint import HTML
    return HTML(string=html_of(data, title)).write_pdf()


# ----------------------------------------------------------------------------- the templates of the panel
def check_upload(filename: str, data: bytes) -> list:
    """The fields of an uploaded template, or TemplateError with the reason."""
    if not str(filename or "").lower().endswith(".docx"):
        raise TemplateError("нужен шаблон в формате .docx (Word)")
    if not data:
        raise TemplateError("файл пустой")
    if len(data) > MAX_TEMPLATE:
        raise TemplateError("шаблон больше 5 МБ")
    names = fields_of(data)
    if not names:
        raise TemplateError("в шаблоне нет полей: впишите в документ, например, {{Дата}} или {{Получатель}}")
    return names


def save_template(*, template_id=None, title: str, description: str = "", filename: str = "", data: bytes = b"",
                  output: str = "docx", hints: dict | None = None, by: str = "") -> int:
    with SessionLocal() as s:
        t = s.get(DocTemplate, template_id) if template_id else None
        if template_id and t is None:
            raise TemplateError("шаблон не найден")
        if t is None:
            if not data:
                raise TemplateError("загрузите файл шаблона .docx")
            t = DocTemplate(data=b"")
            s.add(t)
        old_hints = {f["name"]: f.get("hint", "") for f in (t.fields or [])}
        if data:
            names = check_upload(filename, data)
            t.data, t.filename = data, str(filename)[:255]
        else:
            names = [f["name"] for f in t.fields or []]
        hints = {**old_hints, **{k: v for k, v in (hints or {}).items()}}
        t.fields = [{"name": n, "hint": str(hints.get(n) or "").strip()[:300]} for n in names]
        t.title = (title or "").strip()[:200] or t.title or "Шаблон"
        t.description = (description or "").strip()[:2000]
        t.output = output if output in OUTPUTS else "docx"
        t.updated_by = by[:120]
        s.commit()
        return t.id


def sample(template_id: int) -> tuple:
    """(filename, bytes) of the template filled with the names of its fields: how the result will look."""
    with SessionLocal() as s:
        t = s.get(DocTemplate, template_id)
        if t is None:
            raise TemplateError("шаблон не найден")
        values = {f["name"]: f"[{f['name']}]" for f in t.fields or []}
        return _file_name(t.title, "docx"), fill(bytes(t.data), values, leave_missing=True)


def view(s) -> list:
    """The active templates for the assistant: what each is for and its fields with hints."""
    out = []
    for t in s.query(DocTemplate).filter_by(active=True).order_by(DocTemplate.title).limit(30):
        out.append({"id": t.id, "title": t.title, "when": t.description or None,
                    "fields": [{"name": f["name"], **({"hint": f["hint"]} if f.get("hint") else {})}
                               for f in t.fields or []],
                    "output": OUTPUTS.get(t.output, "DOCX")})
    return out


def panel_list(s) -> list:
    """Every template for the Documents page, the switched-off ones too."""
    return [{"id": t.id, "title": t.title, "description": t.description or "", "filename": t.filename or "",
             "fields": list(t.fields or []), "output": t.output, "output_ru": OUTPUTS.get(t.output, "DOCX"),
             "active": t.active, "uses": t.uses or 0, "updated": t.updated_at, "by": t.updated_by or ""}
            for t in s.query(DocTemplate).order_by(DocTemplate.active.desc(), DocTemplate.title)]


def toggle(template_id: int) -> bool:
    with SessionLocal() as s:
        t = s.get(DocTemplate, template_id)
        if t is None:
            raise TemplateError("шаблон не найден")
        t.active = not t.active
        s.commit()
        return t.active


def delete(template_id: int) -> None:
    with SessionLocal() as s:
        t = s.get(DocTemplate, template_id)
        if t is None:
            raise TemplateError("шаблон не найден")
        for draft in s.query(DocDraft).filter_by(template_id=t.id):
            draft.template_id = None
            if draft.status == "draft":
                draft.status = "cancelled"
        s.delete(t)
        s.commit()


def original(template_id: int) -> tuple:
    with SessionLocal() as s:
        t = s.get(DocTemplate, template_id)
        if t is None:
            raise TemplateError("шаблон не найден")
        return t.filename or _file_name(t.title, "docx"), bytes(t.data)


def draft_file(draft_id: int, kind: str) -> tuple:
    """(filename, mime, bytes) of a draft for the panel."""
    with SessionLocal() as s:
        draft = s.get(DocDraft, draft_id)
        if draft is None:
            raise TemplateError("документ не найден")
        for filename, mime, content in files_of(draft):
            if filename.endswith("." + kind):
                return filename, mime, content
    raise TemplateError("такого файла у документа нет")


# ----------------------------------------------------------------------------- drafts
def _file_name(title: str, ext: str) -> str:
    name = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", " ", str(title or "документ")).strip(" .") or "документ"
    return f"{name[:120]}.{ext}"


def files_of(draft) -> list:
    """[(filename, mime, bytes)] of a draft: what the template makes."""
    out = []
    if draft.docx:
        out.append((_file_name(draft.title, "docx"), DOCX_MIME, bytes(draft.docx)))
    if draft.pdf:
        out.append((_file_name(draft.title, "pdf"), "application/pdf", bytes(draft.pdf)))
    return out


def make_draft(template_id: int, values: dict, *, chat: str, requested_by: str = "", employee_id=None,
               title: str = "", send_to=None) -> int:
    """The filled document waiting for the «ок» of the person who asked; an older draft of this chat is replaced."""
    with SessionLocal() as s:
        t = s.get(DocTemplate, template_id)
        if t is None or not t.active:
            raise TemplateError("шаблон не найден или выключен")
        names = [f["name"] for f in t.fields or []]
        clean = {n: str((values or {}).get(n) or "").strip() for n in names}
        missing = [n for n in names if not clean[n]]
        if missing:
            raise TemplateError("не хватает полей: " + ", ".join(missing))
        if send_to:
            person = s.get(Employee, send_to)
            if person is None or not person.active or not person.telegram_chat_id:
                raise TemplateError("получателя нет в справочнике или у него нет Telegram")
        document = fill(bytes(t.data), clean)
        name = (title or "").strip() or t.title
        pdf, error = None, ""
        if t.output in ("pdf", "both"):
            try:
                pdf = to_pdf(document, name)
            except Exception as exc:  # noqa: BLE001  the DOCX is made all the same
                log.warning("PDF of template %s failed: %s", t.id, exc)
                error = f"PDF не получился, отправлен DOCX: {exc}"[:500]
        for old in s.query(DocDraft).filter_by(chat_id=str(chat), status="draft"):
            old.status = "replaced"
        draft = DocDraft(template_id=t.id, chat_id=str(chat), requested_by=str(requested_by or "")[:120],
                         employee_id=employee_id, title=name[:300], values=clean, send_to_id=send_to or None,
                         status="draft", docx=document if t.output in ("docx", "both") or pdf is None else None,
                         pdf=pdf, error=error)
        t.uses = (t.uses or 0) + 1
        s.add(draft)
        s.commit()
        return draft.id


def show_pending(chat: str, explain: bool = False) -> bool:
    """The files of the newest draft of this chat go to the person (after the assistant's reply), once. `explain`: the
    file comes without a reply that asks to check it (the director approved the request), so its caption asks."""
    with SessionLocal() as s:
        draft = (s.query(DocDraft).filter_by(chat_id=str(chat), status="draft").order_by(DocDraft.id.desc()).first())
        if draft is None or draft.shown_at is not None:
            return False
        files, draft_id = files_of(draft), draft.id
        person = s.get(Employee, draft.send_to_id) if draft.send_to_id else None
        caption = ("Черновик: проверьте. Если всё верно, ответьте «ок» — сохраню"
                   + (f" и отправлю: {person.name}" if person else "") + "; если нет — напишите, что поправить."
                   if explain else "")
    for index, (filename, _, content) in enumerate(files):
        userbot.send_document(chat, filename, content, caption if index == 0 else "", answering=True)
    with SessionLocal() as s:
        s.get(DocDraft, draft_id).shown_at = now()
        s.commit()
    return True


def pending_view(s, chat: str):
    """The draft of this chat waiting for an answer (the assistant sees it as document_draft)."""
    draft = s.query(DocDraft).filter_by(chat_id=str(chat), status="draft").order_by(DocDraft.id.desc()).first()
    if draft is None:
        return None
    created = draft.created_at if draft.created_at.tzinfo else draft.created_at.replace(tzinfo=dt.timezone.utc)
    if now() - created > dt.timedelta(days=DRAFT_DAYS):
        return None
    template = s.get(DocTemplate, draft.template_id) if draft.template_id else None
    person = s.get(Employee, draft.send_to_id) if draft.send_to_id else None
    return {"id": draft.id, "template": template.title if template else "", "template_id": draft.template_id,
            "title": draft.title, "values": draft.values, "send_to": person.name if person else None,
            "shown": draft.shown_at is not None}


def decide(draft_id: int, chat: str, ok: bool, by: str = "") -> dict:
    """«Ок»: the document is kept in the library and sent to send_to; «нет»: the draft is cancelled."""
    with SessionLocal() as s:
        draft = s.get(DocDraft, draft_id)
        if draft is None or str(draft.chat_id) != str(chat):
            raise TemplateError("черновик не найден")
        if draft.status not in ("draft", "approved"):
            raise TemplateError("по этому черновику уже решено")
        if not ok:
            if draft.status != "draft":
                raise TemplateError("документ уже сохранён")
            draft.status, draft.decided_at = "cancelled", now()
            s.commit()
            return {"cancelled": True}
        if draft.status == "draft":
            ids = []
            # The DOCX is read like any file of the library (text, summary, search); its PDF twin only keeps the text.
            twin = text_of(bytes(draft.docx))[:200_000] if draft.docx and draft.pdf else ""
            for index, (filename, mime, content) in enumerate(files_of(draft)):
                doc = Document(chat_id="generated", message_id=draft.id * 2 + index, filename=filename, mime=mime,
                               size=len(content), sha256=hashlib.sha256(content).hexdigest(),
                               kind="pdf" if mime == "application/pdf" else "docx", data=content,
                               caption=f"Документ по шаблону для {draft.requested_by or 'сотрудника'}"[:500],
                               employee_id=draft.employee_id,
                               text=twin if mime != DOCX_MIME else "",
                               text_status="done" if mime != DOCX_MIME and twin else "pending")
                s.add(doc)
                s.flush()
                ids.append(doc.id)
            draft.document_ids, draft.decided_at = ids, now()
            draft.status = "approved" if draft.send_to_id else "kept"
            s.commit()
        person = s.get(Employee, draft.send_to_id) if draft.send_to_id else None
        files, title, author = files_of(draft), draft.title, draft.requested_by or by
        out = {"saved": list(draft.document_ids or []), "sent_to": None}
        if person is None:
            return out
        ref, name, address = person.telegram_chat_id, person.name, None
        from . import voice
        address = voice.address_of(person)
    from . import cases
    try:
        caption = cases._write("deliver", {"from": author, "task": {"title": title},
                                           "person": {"name": name, "address": address}})
        for index, (filename, _, content) in enumerate(files):
            userbot.send_document(ref, filename, content, caption if index == 0 else "")
    except Exception as exc:  # noqa: BLE001
        with SessionLocal() as s:
            s.get(DocDraft, draft_id).error = str(exc)[:500]
            s.commit()
        raise TemplateError(f"документ сохранён, но {name} его не получил(а): {exc}") from exc
    with SessionLocal() as s:
        draft = s.get(DocDraft, draft_id)
        draft.status, draft.error = "sent", ""
        s.commit()
    out["sent_to"] = name
    return out


def recent(s, limit: int = 30) -> list:
    """The latest documents made from templates, for the panel."""
    rows = []
    for d in s.query(DocDraft).order_by(DocDraft.id.desc()).limit(limit):
        template = s.get(DocTemplate, d.template_id) if d.template_id else None
        person = s.get(Employee, d.send_to_id) if d.send_to_id else None
        rows.append({"id": d.id, "title": d.title, "template": template.title if template else "—",
                     "by": d.requested_by, "at": d.created_at, "status": d.status, "send_to": person.name if person else "",
                     "has_docx": bool(d.docx), "has_pdf": bool(d.pdf), "error": d.error or "",
                     "values": d.values or {}})
    return rows
