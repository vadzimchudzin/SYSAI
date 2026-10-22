"""Reading documents: text out of PDF, DOCX, XLSX, CSV/TXT; scans and photos through the vision model.

The text is kept on the document, cut into chunks for memory search, and a short summary is made. DOCX and XLSX are
read with the standard library (they are zip files of XML), PDF with pypdf. A PDF without a text layer and an image go
to the vision model; when the model is not available the document stays «pending» and is tried again later.
"""
import base64
import io
import logging
import re
import zipfile
import xml.etree.ElementTree as ET

from . import config, llm, settings_store
from .db import Document, DocumentChunk, SessionLocal

log = logging.getLogger("sysai.doctext")

MAX_TEXT = 200_000
CHUNK = 1200
OVERLAP = 150
MAX_ATTEMPTS = 3
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_attempts: dict = {}


class NeedsVision(Exception):
    """No text layer: the document must be read by the vision model."""


def _docx(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        root = ET.fromstring(z.read("word/document.xml"))
    lines = []
    for para in root.iter(_W + "p"):
        text = "".join(t.text or "" for t in para.iter(_W + "t"))
        if text.strip():
            lines.append(text.strip())
    return "\n".join(lines)


def _xlsx(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = z.namelist()
        shared = []
        if "xl/sharedStrings.xml" in names:
            for si in ET.fromstring(z.read("xl/sharedStrings.xml")).iter(_S + "si"):
                shared.append("".join(t.text or "" for t in si.iter(_S + "t")))
        titles = {}
        if "xl/workbook.xml" in names:
            for i, sheet in enumerate(ET.fromstring(z.read("xl/workbook.xml")).iter(_S + "sheet"), start=1):
                titles[i] = sheet.get("name") or f"Лист {i}"
        lines = []
        sheets = sorted((n for n in names if re.match(r"xl/worksheets/sheet\d+\.xml$", n)),
                        key=lambda n: int(re.findall(r"\d+", n)[-1]))
        for name in sheets:
            number = int(re.findall(r"\d+", name)[-1])
            lines.append(f"Лист: {titles.get(number, number)}")
            for count, row in enumerate(ET.fromstring(z.read(name)).iter(_S + "row")):
                if count >= 2000:
                    lines.append("…")
                    break
                cells = []
                for c in row.iter(_S + "c"):
                    v = c.find(_S + "v")
                    if c.get("t") == "s" and v is not None and (v.text or "").isdigit():
                        value = shared[int(v.text)] if int(v.text) < len(shared) else ""
                    elif c.get("t") == "inlineStr":
                        value = "".join(t.text or "" for t in c.iter(_S + "t"))
                    else:
                        value = v.text if v is not None else ""
                    cells.append((value or "").strip())
                if any(cells):
                    lines.append("\t".join(cells).rstrip())
    return "\n".join(lines)


def _pdf(data: bytes) -> str:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    pages = [(page.extract_text() or "").strip() for page in reader.pages[:200]]
    text = "\n\n".join(p for p in pages if p)
    if len(re.sub(r"\s", "", text)) < 30 * max(1, min(len(pages), 5)):
        raise NeedsVision()
    return text


def _vision(data: bytes, mime: str, filename: str) -> str:
    """A scan or a photo: the vision model copies the text out (nothing is invented; unreadable parts are marked)."""
    model = str(settings_store.get("vision_model") or "google/gemini-2.5-flash")
    b64 = base64.b64encode(data).decode()
    if mime == "application/pdf" or filename.lower().endswith(".pdf"):
        part = {"type": "file", "file": {"filename": filename or "document.pdf",
                                         "file_data": f"data:application/pdf;base64,{b64}"}}
    else:
        part = {"type": "image_url", "image_url": {"url": f"data:{mime or 'image/jpeg'};base64,{b64}"}}
    raw = llm.chat(model, [{"role": "system", "content": "Перепиши весь текст документа как есть, сохраняя порядок, "
                                                          "таблицы — строками через табуляцию. Ничего не добавляй и не "
                                                          "пересказывай; нечитаемое отметь [неразборчиво]. Если текста нет, "
                                                          "опиши изображение одной фразой в квадратных скобках."},
                           {"role": "user", "content": [{"type": "text", "text": "Документ:"}, part]}],
                   temperature=0.0, max_tokens=8000, timeout=180)
    return raw.strip()


def extract(data: bytes, mime: str, filename: str, kind: str) -> str:
    low = (filename or "").lower()
    if kind == "docx" and low.endswith(".docx"):
        return _docx(data)
    if kind == "xlsx" and low.endswith(".xlsx"):
        return _xlsx(data)
    if low.endswith((".csv", ".txt", ".md")) or mime.startswith("text/"):
        for encoding in ("utf-8", "cp1251"):
            try:
                return data.decode(encoding)
            except UnicodeDecodeError:
                continue
        return data.decode("utf-8", "replace")
    if kind == "pdf":
        try:
            return _pdf(data)
        except NeedsVision:
            return _vision(data, "application/pdf", filename)
    if kind == "image":
        return _vision(data, mime, filename)
    return ""  # old .doc/.xls and unknown formats: kept, not read


def chunks(text: str) -> list:
    """Pieces of about CHUNK characters along paragraph borders, with a small overlap."""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n|\n", text or "") if p.strip()]
    out, current = [], ""
    for p in paragraphs:
        while len(p) > CHUNK:  # a very long paragraph is cut
            head, p = p[:CHUNK], p[CHUNK - OVERLAP:]
            if current:
                out.append(current)
                current = ""
            out.append(head)
        if len(current) + len(p) + 1 > CHUNK and current:
            out.append(current)
            current = current[-OVERLAP:] + "\n" + p if OVERLAP else p
        else:
            current = (current + "\n" + p).strip()
    if current:
        out.append(current)
    return out


def _summary(text: str, filename: str) -> str:
    if not config.llm_enabled() or len(text) < 200:
        return ""
    try:
        from . import voice
        raw = llm.chat(voice.model(), [{"role": "system", "content": "Опиши документ в 1–2 предложениях: что это, от кого "
                                                                      "и кому, о чём, главные суммы и даты, если есть. "
                                                                      "Только факты из текста."},
                                       {"role": "user", "content": f"{filename}\n<data>{text[:12000]}</data>"}],
                       temperature=0.1, max_tokens=300, timeout=90)
        return raw.strip()[:800]
    except llm.LLMError as exc:
        log.info("summary of %s not made: %s", filename, exc)
        return ""


def process(document_id: int) -> str:
    """Read one document. Returns the new text_status."""
    with SessionLocal() as s:
        doc = s.get(Document, document_id)
        if doc is None or doc.data is None or doc.text_status in ("done", "empty"):
            return doc.text_status if doc else "missing"
        data, mime, filename, kind = bytes(doc.data), doc.mime or "", doc.filename or "", doc.kind or "other"
    try:
        text = extract(data, mime, filename, kind)
        status = "done" if text.strip() else "empty"
    except llm.LLMError as exc:
        _attempts[document_id] = _attempts.get(document_id, 0) + 1
        log.info("document %s waits for the vision model: %s", document_id, exc)
        if _attempts[document_id] < MAX_ATTEMPTS:
            return "pending"
        text, status = "", "failed"
    except Exception as exc:  # noqa: BLE001  a broken file must not stop the queue
        log.warning("document %s not read: %s", document_id, exc)
        text, status = "", "failed"
    text = text[:MAX_TEXT]
    summary = _summary(text, filename) if status == "done" else ""
    with SessionLocal() as s:
        doc = s.get(Document, document_id)
        doc.text, doc.text_status, doc.summary = text, status, summary
        s.query(DocumentChunk).filter_by(document_id=document_id).delete()
        for position, piece in enumerate(chunks(text)):
            s.add(DocumentChunk(document_id=document_id, position=position, text=piece))
        s.commit()
    return status


def run_pending() -> None:
    """Background loop: documents not read yet (new ones, or the vision model was down)."""
    with SessionLocal() as s:
        ids = [d.id for d in s.query(Document.id).filter(Document.text_status == "pending").order_by(Document.id).limit(5)]
    for document_id in ids:
        process(document_id)
