"""hub/seed/doc-kit — the documentation tool (an example .docx → the outline; Markdown → .docx) stays valid for
SYSAI_Hub and keeps working.

No customer document is needed: one example is written by hand the way Russian Word writes it (styles «Заголовок 1»,
direct run formatting, a table, a drawing, a header), another one comes from the exporter itself."""
import io
import json
import os
import struct
import subprocess
import sys
import zipfile
import zlib
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest

from app.net import hub

SEED = Path(__file__).resolve().parent.parent / "hub" / "seed" / "doc-kit"
sys.path.append(str(SEED))          # at the end: other seed tools have a `server` module of their own
import dockit  # noqa: E402
import docmd  # noqa: E402
import docxout  # noqa: E402

TOOLS = ["doc_outline", "doc_text", "doc_export", "doc_check"]
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS = (f'xmlns:w="{W}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
      'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
      'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
      'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"')

MD = """# Введение

Документ описывает **линию** приёмки *зерна* и переменную `noria2_status`.\\
Вторая строка & <теги> и a_b_c.

# Назначение

## Состав

Текст раздела.

- нория
  - привод
  - датчик
- транспортёр

1. Включить питание
2. Нажать «Пуск»

Таблица: Параметры
| Параметр | Значение | Ед. |
|---|:-:|--:|
| Скорость | 1,5 | м/с |
| Ток \\| фаза | 12 |

![Общий вид](img/pic.png "width=120mm")

![Схема](img/photo.jpg)

> Не открывать кожух при работе.

---

## Программа

```st
IF PUSK THEN
    y0_0 := TRUE;
END_IF
```

# 5 Уже с номером

Текст.

## Подраздел

Текст.

# Приложение А

## Схемы

Текст.
"""


def _png(w, h):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    raw = b"".join(b"\x00" + bytes([200, 60, 60]) * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


def _jpeg_header(w, h):
    """Enough of a JPEG for its size to be read: SOI, APP0, SOF0."""
    return (b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xc0\x00\x11\x08" + struct.pack(">HH", h, w)
            + b"\x03\x01\x11\x00\x02\x11\x01\x03\x11\x01\xff\xd9")


def _project(tmp_path, md=MD):
    (tmp_path / "img").mkdir(exist_ok=True)
    (tmp_path / "img" / "pic.png").write_bytes(_png(300, 150))
    (tmp_path / "img" / "photo.jpg").write_bytes(_jpeg_header(2000, 1000))
    path = tmp_path / "manual.md"
    path.write_text(md, encoding="utf-8")
    return str(path)


def _build(tmp_path, md=MD, cfg=None, **kw):
    blob, counts, warnings = docxout.build(md, str(tmp_path), cfg or docxout.config(), **kw)
    out = tmp_path / "out.docx"
    out.write_bytes(blob)
    return str(out), counts, warnings


def _p(text, style="", rpr="", ppr=""):
    style = f'<w:pStyle w:val="{style}"/>' if style else ""
    return f'<w:p><w:pPr>{style}{ppr}</w:pPr><w:r><w:rPr>{rpr}</w:rPr><w:t xml:space="preserve">{text}</w:t></w:r></w:p>'


def _foreign(tmp_path, name="sample.docx") -> str:
    """A .docx the way Russian Word / LibreOffice writes it: numeric styleIds with Russian names, the text set with
    direct formatting (Arial 12 over a Calibri 11 style), a landscape A4, a header, a drawing with a fallback copy."""
    arial = '<w:rFonts w:ascii="Arial" w:hAnsi="Arial"/><w:sz w:val="24"/>'
    body_ppr = '<w:spacing w:line="276" w:lineRule="auto"/><w:ind w:firstLine="709"/><w:jc w:val="both"/>'
    drawing = ('<w:drawing><wp:inline><wp:extent cx="100" cy="100"/><a:graphic><a:graphicData><a:blip r:embed="rId9"/></a:graphicData>'
               '</a:graphic></wp:inline></w:drawing>')
    cell = lambda t, span="": f"<w:tc><w:tcPr>{span}</w:tcPr><w:p><w:r><w:t>{t}</w:t></w:r></w:p></w:tc>"  # noqa: E731
    document = (
        f'<w:document {NS}><w:body>'
        + _p("ОАО «Завод»") + _p("ПАСПОРТ") + "<w:p/>"
        + '<w:p><w:pPr><w:pStyle w:val="11"/></w:pPr><w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText> TOC \\o "1-3" </w:instrText>'
          '</w:r><w:r><w:fldChar w:fldCharType="separate"/></w:r><w:r><w:t>1 Общие сведения</w:t></w:r><w:r><w:tab/><w:t>3</w:t></w:r></w:p>'
        + '<w:p><w:pPr><w:pStyle w:val="11"/></w:pPr><w:r><w:t>2 Технические данные</w:t></w:r><w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>'
        + _p("1 Общие сведения", "1")
        + _p("Изделие предназначено для приёмки зерна.", rpr=arial, ppr=body_ppr)
        + _p("Изготовитель: ОАО «Завод», ??? уточнить адрес.", rpr=arial, ppr=body_ppr)
        + _p("1.1 Комплектность", "2")
        + _p("шкаф управления", ppr='<w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr>')
        + _p("панель оператора", ppr='<w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr>')
        + _p("первый шаг", ppr='<w:numPr><w:ilvl w:val="0"/><w:numId w:val="2"/></w:numPr>')
        + _p("2 Технические данные", "1")
        + _p("Таблица 1 – Основные параметры")
        + "<w:tbl><w:tr>" + cell("Наименование") + cell("Значение", '<w:gridSpan w:val="2"/>') + "</w:tr><w:tr>" + cell("Напряжение, В")
        + cell("380") + cell("±10 %") + "</w:tr></w:tbl>"
        + f'<w:p><w:r><mc:AlternateContent><mc:Choice Requires="wps">{drawing}</mc:Choice><mc:Fallback>{drawing}<w:t>копия</w:t></mc:Fallback>'
          '</mc:AlternateContent></w:r></w:p>'
        + _p("Рис. 1. Шкаф управления")
        + f"<w:p><w:r>{drawing}</w:r></w:p>" + _p("Текст сразу после второй картинки.", rpr=arial, ppr=body_ppr)
        + _p("3 Гарантии", "1")
        + '<w:sectPr><w:headerReference w:type="default" r:id="rId5"/><w:pgSz w:w="16838" w:h="11906" w:orient="landscape"/>'
          '<w:pgMar w:top="850" w:right="567" w:bottom="1134" w:left="1417" w:header="708" w:footer="708"/></w:sectPr></w:body></w:document>')
    styles = (
        f'<w:styles xmlns:w="{W}"><w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:asciiTheme="minorHAnsi"/><w:sz w:val="22"/></w:rPr>'
        '</w:rPrDefault></w:docDefaults>'
        '<w:style w:type="paragraph" w:default="1" w:styleId="a"><w:name w:val="Обычный"/></w:style>'
        '<w:style w:type="paragraph" w:styleId="1"><w:name w:val="Заголовок 1"/><w:basedOn w:val="a"/><w:pPr><w:jc w:val="center"/>'
        '<w:spacing w:before="240" w:after="120"/></w:pPr><w:rPr><w:rFonts w:ascii="Arial" w:hAnsi="Arial"/><w:b/><w:caps/><w:sz w:val="32"/></w:rPr></w:style>'
        '<w:style w:type="paragraph" w:styleId="2"><w:name w:val="Заголовок 2"/><w:basedOn w:val="a"/><w:rPr><w:rFonts w:ascii="Arial"/><w:b/>'
        '<w:i/><w:sz w:val="26"/></w:rPr></w:style>'
        '<w:style w:type="paragraph" w:styleId="11"><w:name w:val="toc 1"/><w:basedOn w:val="a"/></w:style></w:styles>')
    numbering = (f'<w:numbering xmlns:w="{W}"><w:abstractNum w:abstractNumId="0"><w:lvl w:ilvl="0"><w:numFmt w:val="bullet"/></w:lvl></w:abstractNum>'
                 '<w:abstractNum w:abstractNumId="1"><w:lvl w:ilvl="0"><w:numFmt w:val="decimal"/></w:lvl></w:abstractNum>'
                 '<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num><w:num w:numId="2"><w:abstractNumId w:val="1"/></w:num></w:numbering>')
    theme = ('<a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><a:themeElements><a:fontScheme name="x"><a:majorFont>'
             '<a:latin typeface="Cambria"/></a:majorFont><a:minorFont><a:latin typeface="Calibri"/></a:minorFont></a:fontScheme></a:themeElements></a:theme>')
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    pkg = "http://schemas.openxmlformats.org/package/2006/relationships"
    parts = {
        "[Content_Types].xml": '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>',
        "_rels/.rels": f'<Relationships xmlns="{pkg}"><Relationship Id="rId1" Type="{rel}/officeDocument" Target="word/document.xml"/></Relationships>',
        "word/document.xml": document, "word/styles.xml": styles, "word/numbering.xml": numbering, "word/theme/theme1.xml": theme,
        "word/header1.xml": f'<w:hdr xmlns:w="{W}"><w:p><w:r><w:t>ПС 1234.001</w:t></w:r></w:p></w:hdr>',
        "word/_rels/document.xml.rels": f'<Relationships xmlns="{pkg}"><Relationship Id="rId1" Type="{rel}/styles" Target="styles.xml"/>'
        f'<Relationship Id="rId2" Type="{rel}/numbering" Target="numbering.xml"/><Relationship Id="rId3" Type="{rel}/theme" Target="theme/theme1.xml"/>'
        f'<Relationship Id="rId5" Type="{rel}/header" Target="header1.xml"/><Relationship Id="rId9" Type="{rel}/image" Target="media/i.png"/></Relationships>',
        "word/media/i.png": _png(4, 4),
    }
    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as z:
        for part, data in parts.items():
            z.writestr(part, data)
    return str(path)


def _files():
    return {str(p.relative_to(SEED)).replace(os.sep, "/"): p.read_bytes() for p in SEED.rglob("*")
            if p.is_file() and "__pycache__" not in p.parts}


def _server(calls):
    lines = [{"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}}, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}]
    lines += [{"jsonrpc": "2.0", "id": 10 + i, "method": "tools/call", "params": {"name": n, "arguments": a}}
              for i, (n, a) in enumerate(calls)]
    out = subprocess.run([sys.executable, str(SEED / "server.py")], input="\n".join(json.dumps(x) for x in lines) + "\n",
                         capture_output=True, text=True, timeout=60, encoding="utf-8").stdout.splitlines()
    return [json.loads(x) for x in out]


def test_the_tool_passes_the_hub_check():
    meta, report = hub.check(_files())
    assert report["errors"] == [] and meta["name"] == "doc-kit" and meta["kind"] == "mcp" and meta["version"] == "1.0.0"
    # the only remarks: the XML namespace names in the code look like addresses (the tool opens no connection)
    assert {w["what"] for w in report["warnings"]} == {"внешний адрес schemas.openxmlformats.org", "внешний адрес schemas.microsoft.com",
                                                       "внешний адрес purl.org", "внешний адрес www.w3.org"}
    assert [t.strip() for t in meta["tools"].split(",")] == TOOLS
    assert not any(w in src for src in (f.read_text(encoding="utf-8") for f in SEED.glob("*.py"))
                   for w in ("import socket", "urllib", "http.client", "requests", "subprocess", "import docx\n", "from docx "))
    body = _files()["TOOL.md"].decode("utf-8")
    assert all(f"## {s}" in body for s in ("Когда применять", "Как запускать", "Что умеет", "Разметка Markdown", "Пример как образец",
                                           "Ограничения", "Ошибки"))


def test_markdown_blocks_and_inline():
    blocks, warnings = docmd.parse(MD)
    assert warnings == []
    assert [b["t"] for b in blocks] == ["h", "p", "h", "h", "p", "list", "table", "img", "img", "note", "break", "h", "code", "h", "p",
                                        "h", "p", "h", "h", "p"]
    assert blocks[1]["text"].split("\n")[1] == "Вторая строка & <теги> и a_b_c."
    assert [(i["kind"], i["level"], i["text"]) for i in blocks[5]["items"]] == [
        ("bullet", 0, "нория"), ("bullet", 1, "привод"), ("bullet", 1, "датчик"), ("bullet", 0, "транспортёр"),
        ("number", 0, "Включить питание"), ("number", 0, "Нажать «Пуск»")]
    table = blocks[6]
    assert table["caption"] == "Параметры" and table["aligns"] == ["left", "center", "right"] and table["rows"][1] == ["Ток | фаза", "12"]
    assert blocks[7] == {"t": "img", "caption": "Общий вид", "path": "img/pic.png", "title": "width=120mm"}
    assert blocks[12]["lines"] == ["IF PUSK THEN", "    y0_0 := TRUE;", "END_IF"]
    assert docmd.inline("a **b** *c* `d_e` x_y_z ***f*** 2 * 3") == [
        ("a ", False, False, False), ("b", True, False, False), (" ", False, False, False), ("c", False, True, False),
        (" ", False, False, False), ("d_e", False, False, True), (" x_y_z ", False, False, False), ("f", True, True, False),
        (" 2 * 3", False, False, False)]
    odd, warn = docmd.parse("###### Глубоко\n\nТаблица: без таблицы\n\nтекст ![x](y.png) в строке\n\n```\nкод")
    assert odd[0]["level"] == 4 and odd[1] == {"t": "p", "text": "Таблица: без таблицы", "raw": "Таблица: без таблицы"}
    assert len(warn) == 4 and "не закрыт" in " ".join(warn) and "отдельную строку" in " ".join(warn)


def test_image_sizes_are_read_from_the_headers():
    assert docxout.image_size(_png(300, 150)) == ("png", 300, 150)
    assert docxout.image_size(_jpeg_header(2000, 1000)) == ("jpeg", 2000, 1000)
    assert docxout.image_size(b"GIF89a" + struct.pack("<HH", 40, 30) + b"\x00" * 10) == ("gif", 40, 30)
    assert docxout.image_size(b"BM" + b"\x00" * 16 + struct.pack("<ii", 64, -48) + b"\x00" * 8) == ("bmp", 64, 48)
    assert docxout.image_size(b"<svg/>") is None


def test_export_writes_a_valid_package_that_reads_back(tmp_path):
    _project(tmp_path)
    out, counts, warnings = _build(tmp_path, title="Руководство оператора", subtitle="Линия приёмки", organization="ООО «СиСАТ»",
                                   city_year="Минск 2026", toc=True)
    assert counts == {"headings": 8, "paragraphs": 6, "tables": 1, "pictures": 2, "list_items": 6, "code_lines": 3, "pages": None}
    assert warnings == ["таблица «Параметр | Значение | Ед.»: в строках разное число ячеек — дополнены пустыми"]
    blob = Path(out).read_bytes()
    assert docxout.verify(blob) == []
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        names = z.namelist()
        assert names[0] == "[Content_Types].xml" and set(names) == {
            "[Content_Types].xml", "_rels/.rels", "word/document.xml", "word/styles.xml", "word/numbering.xml", "word/settings.xml",
            "word/footer1.xml", "word/_rels/document.xml.rels", "word/media/image1.png", "word/media/image2.jpeg", "docProps/core.xml",
            "docProps/app.xml"}
        roots = {n: ET.fromstring(z.read(n)) for n in names if n.endswith((".xml", ".rels"))}     # every XML part parses
        document = z.read("word/document.xml").decode("utf-8")
        settings, numbering, styles = (z.read(f"word/{n}.xml").decode("utf-8") for n in ("settings", "numbering", "styles"))
        assert z.read("word/media/image1.png") == _png(300, 150)
    w = "{" + W + "}"
    doc = roots["word/document.xml"]
    assert all(t.get("{http://www.w3.org/XML/1998/namespace}space") == "preserve" for t in doc.iter(w + "t"))
    assert [p.find(f"{w}pPr/{w}pStyle").get(w + "val") for p in doc.iter(w + "p") if p.find(f"{w}pPr/{w}pStyle") is not None
            and p.find(f"{w}pPr/{w}pStyle").get(w + "val").startswith("Heading")] == ["Heading1", "Heading1", "Heading2", "Heading2", "Heading1",
                                                                                      "Heading2", "Heading1", "Heading2"]
    assert 'TOC \\o "1-3" \\h \\z \\u' in document and '<w:updateFields w:val="true"/>' in settings
    assert "Вторая строка &amp; &lt;теги&gt; и a_b_c." in document and "<w:br/>" in document and "<w:titlePg/>" in document
    assert '<w:pgSz w:w="11906" w:h="16838"/>' in document and 'w:top="1134" w:right="850" w:bottom="1134" w:left="1701"' in document
    assert "<w:tblHeader/>" in document and document.count("<w:pageBreakBefore/>") == 3          # contents, text, the «---»
    assert f'cx="{120 * 36000}" cy="{60 * 36000}"' in document                                   # width=120mm, the ratio kept
    assert f'cx="{9354 * 635}"' in document                                                      # 2000 px is cut to the text width (165 mm)
    assert numbering.count("<w:num ") == 2 and "<w:startOverride" in numbering and 'w:lvlText w:val="–"' in numbering
    assert 'w:ascii="Times New Roman"' in styles and '<w:sz w:val="28"/>' in styles and 'w:line="360" w:lineRule="auto"' in styles
    assert '<w:ind w:left="0" w:firstLine="709"/><w:jc w:val="both"/>' in styles and '<w:name w:val="heading 1"/>' in styles
    footer = roots["word/footer1.xml"]
    assert "PAGE" in "".join(e.text for e in footer.iter(w + "instrText"))

    o = dockit.outline(out)
    assert o["title_page"] == ["ООО «СиСАТ»", "Руководство оператора", "Линия приёмки", "Минск 2026"] and o["toc"] and o["page_numbers"]
    assert [(h["level"], h["number"], h["title"]) for h in o["headings"]] == [
        (1, "", "Введение"), (1, "1", "Назначение"), (2, "1.1", "Состав"), (2, "1.2", "Программа"), (1, "5", "Уже с номером"),
        (2, "5.1", "Подраздел"), (1, "", "Приложение А"), (2, "", "Схемы")]
    part = o["headings"][2]
    assert (part["paragraphs"], part["lists"], part["list_items"], part["pictures"]) == (2, 1, 6, 2)
    assert part["tables"] == [{"rows": 3, "cols": 3, "header": ["Параметр", "Значение", "Ед."], "caption": "Таблица 1 — Параметры"}]
    assert part["captions"] == ["Рисунок 1 — Общий вид", "Рисунок 2 — Схема"] and o["headings"][3]["code_lines"] == 3
    assert o["totals"] == {"paragraphs": 10, "list_items": 6, "pictures": 2, "code_lines": 3, "tables": 1, "headings": 8}
    assert o["page"] == {"width_mm": 210.0, "height_mm": 297.0, "orientation": "portrait",
                         "margins_mm": {"top": 20.0, "bottom": 20.0, "left": 30.0, "right": 15.0, "header": 12.5, "footer": 12.5}}
    assert o["styles"]["normal"] == {"style_id": "Normal", "style_name": "Normal", "font": "Times New Roman", "size_pt": 14.0, "align": "both",
                                     "first_line_mm": 12.5, "line_spacing": 1.5}
    assert o["styles"]["heading1"]["size_pt"] == 16.0 and o["styles"]["heading1"]["bold"] and o["effective"]["body"]["font"] == "Times New Roman"
    assert "## 1.1 Состав\n[заполнить: 2 абзаца, список из 6 пунктов, таблица 3×3 (Параметр | Значение | Ед.), 2 рисунка]" in o["outline_md"]
    assert "## 1.2 Программа\n[заполнить: листинг 3 строки]" in o["outline_md"] and o["heading_numbering"] == "typed"

    text = dockit.text(out)["text"]
    for piece in ("# Введение\n\nДокумент описывает линию приёмки зерна и переменную noria2_status.\nВторая строка & <теги> и a_b_c.",
                  "## 1.1 Состав\n\nТекст раздела.\n\n- нория\n   - привод\n   - датчик\n- транспортёр\n1. Включить питание\n2. Нажать «Пуск»",
                  "| Параметр | Значение | Ед. |\n|---|---|---|\n| Скорость | 1,5 | м/с |\n| Ток \\| фаза | 12 |  |",
                  "[рисунок: Рисунок 1 — Общий вид]", "Примечание — Не открывать кожух при работе.",
                  "```\nIF PUSK THEN\n    y0_0 := TRUE;\nEND_IF\n```"):
        assert piece in text
    one = dockit.text(out, section="1.2")
    assert one["text"].startswith("## 1.2 Программа") and "Уже с номером" not in one["text"] and not one["truncated"]
    assert dockit.text(out, section="назначение")["text"].count("#") == 5          # the section with its subsections
    cut = dockit.text(out, limit=300)
    assert cut["truncated"] and cut["chars"] > 300 and "текст обрезан" in cut["text"]
    with pytest.raises(dockit.DocError, match="нет раздела «Нет такого». Разделы: Введение; 1 Назначение"):
        dockit.text(out, section="Нет такого")


def test_export_options_and_warnings(tmp_path):
    _project(tmp_path)
    out, counts, warnings = _build(tmp_path, md="## Сразу второй\n\n#### Прыжок\n\n# Пустой\n\n# Конец {-}\n\nТекст TODO.\n", numbering=False)
    assert [h["text"] for h in dockit.outline(out)["headings"]] == ["Сразу второй", "Прыжок", "Пустой", "Конец"]
    assert warnings == ["заголовок «Прыжок»: уровень перескакивает с 2 на 4", "раздел «Прыжок» пуст", "раздел «Пустой» пуст",
                        "в тексте осталась заготовка «TODO»: Текст TODO."]
    document = zipfile.ZipFile(out).read("word/document.xml").decode("utf-8")
    assert "<w:titlePg/>" not in document and "TOC" not in document and "<w:tbl>" not in document
    assert "updateFields" not in zipfile.ZipFile(out).read("word/settings.xml").decode("utf-8")
    # numbering: «Конец {-}» and what stands under an unnumbered heading get no number
    out, _, _ = _build(tmp_path, md="# А\n\nт\n\n## Б\n\nт\n\n# В {-}\n\n## Г\n\nт\n\n# Д\n\nт\n")
    assert [h["text"] for h in dockit.outline(out)["headings"]] == ["1 А", "1.1 Б", "В", "Г", "2 Д"]
    # a control character and a quote in a caption do not break the XML
    out, _, _ = _build(tmp_path, md='# A\n\nx\x01y "q" <b> &amp;\n\n![a "b" <c>](img/pic.png)\n')
    assert docxout.verify(Path(out).read_bytes()) == [] and 'xy "q" <b> &amp;' in dockit.text(out)["text"]


def test_a_missing_picture_stops_the_export(tmp_path):
    _project(tmp_path)
    (tmp_path / "img" / "not.png").write_text("<svg/>", encoding="utf-8")
    md = MD + "\n![нет](img/none.png)\n\n![не картинка](img/not.png)\n\n![сеть](https://example.com/a.png)\n"
    with pytest.raises(dockit.DocError) as err:
        docxout.build(md, str(tmp_path), docxout.config())
    text = str(err.value)
    assert text.startswith("Нет картинок, файл не записан:") and len(text.splitlines()) == 4
    assert "img/none.png — файла нет" in text and "img/not.png — не PNG, JPEG, GIF или BMP" in text and "только из файлов на диске" in text


def test_reads_a_document_written_by_another_program(tmp_path):
    path = _foreign(tmp_path)
    o = dockit.outline(path)
    assert o["title_page"] == ["ОАО «Завод»", "ПАСПОРТ"] and o["toc"] and o["header"] == ["ПС 1234.001"] and o["footer"] == []
    assert [(h["level"], h["number"], h["title"]) for h in o["headings"]] == [
        (1, "1", "Общие сведения"), (2, "1.1", "Комплектность"), (1, "2", "Технические данные"), (1, "3", "Гарантии")]
    first, second, third = o["headings"][0], o["headings"][1], o["headings"][2]
    assert first["paragraphs"] == 2 and (second["lists"], second["list_items"]) == (1, 3)
    assert third["tables"] == [{"rows": 2, "cols": 3, "header": ["Наименование", "Значение", ""], "caption": "Таблица 1 – Основные параметры"}]
    assert third["pictures"] == 2 and third["captions"] == ["Рис. 1. Шкаф управления"] and third["paragraphs"] == 1     # the fallback copy is not counted
    assert o["page"] == {"width_mm": 297.0, "height_mm": 210.0, "orientation": "landscape",
                         "margins_mm": {"top": 15.0, "bottom": 20.0, "left": 25.0, "right": 10.0, "header": 12.5, "footer": 12.5}}
    assert o["styles"]["normal"] == {"style_id": "a", "style_name": "Обычный", "font": "Calibri", "size_pt": 11.0}
    assert o["styles"]["heading1"] == {"style_id": "1", "style_name": "Заголовок 1", "font": "Arial", "size_pt": 16.0, "bold": True, "caps": True,
                                       "align": "center", "before_pt": 12.0, "after_pt": 6.0}
    assert o["styles"]["heading2"]["italic"] and "heading3" not in o["styles"]
    assert o["effective"]["body"] == {"font": "Arial", "size_pt": 12.0, "align": "both", "first_line_mm": 12.5, "line_spacing": 1.15}
    assert "# 2 Технические данные\n[заполнить: 1 абзац, таблица 2×3 (Наименование | Значение | ), 2 рисунка]" in o["outline_md"]
    text = dockit.text(path)["text"]
    assert text.startswith("ОАО «Завод»\n\nПАСПОРТ\n\n# 1 Общие сведения") and "копия" not in text and "Технические данные\t3" not in text
    assert "- шкаф управления\n- панель оператора\n1. первый шаг" in text and "Таблица 1 – Основные параметры" in text
    assert "[рисунок: Рис. 1. Шкаф управления]" in text and "[рисунок]\n" in text
    assert "| Наименование | Значение |  |\n|---|---|---|\n| Напряжение, В | 380 | ±10 % |" in text
    # not a .docx
    (tmp_path / "old.doc").write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64)
    with pytest.raises(dockit.DocError, match="старый двоичный формат"):
        dockit.outline(str(tmp_path / "old.doc"))
    (tmp_path / "fake.docx").write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64)
    with pytest.raises(dockit.DocError, match="старый двоичный формат"):
        dockit.outline(str(tmp_path / "fake.docx"))
    with zipfile.ZipFile(tmp_path / "book.docx", "w") as z:
        z.writestr("xl/workbook.xml", "<x/>")
    with pytest.raises(dockit.DocError, match="нет word/document.xml"):
        dockit.outline(str(tmp_path / "book.docx"))


def test_style_from_takes_the_look_of_the_example(tmp_path):
    _project(tmp_path)
    cfg = docxout.config(dockit.style_of(_foreign(tmp_path)))
    assert cfg["source"] == "example" and cfg["body"]["font"] == "Arial" and cfg["body"]["size_pt"] == 12.0 and cfg["body"]["line"] == 276
    assert cfg["headings"][1]["align"] == "center" and cfg["headings"][1]["caps"] and cfg["headings"][2]["italic"]
    assert cfg["headings"][3] == docxout.DEFAULTS["headings"][3]                    # the example has no «Заголовок 3»
    out, _, _ = _build(tmp_path, cfg=cfg)
    assert docxout.verify(Path(out).read_bytes()) == []
    o = dockit.outline(out)
    assert o["page"]["orientation"] == "landscape" and o["page"]["margins_mm"] == {"top": 15.0, "bottom": 20.0, "left": 25.0, "right": 10.0,
                                                                                  "header": 12.5, "footer": 12.5}
    assert o["styles"]["normal"] == {"style_id": "Normal", "style_name": "Normal", "font": "Arial", "size_pt": 12.0, "align": "both",
                                     "first_line_mm": 12.5, "line_spacing": 1.15}
    h1 = o["styles"]["heading1"]
    assert (h1["font"], h1["size_pt"], h1["bold"], h1["caps"], h1["align"], h1["before_pt"]) == ("Arial", 16.0, True, True, "center", 12.0)
    assert 'w:orient="landscape"' in zipfile.ZipFile(out).read("word/document.xml").decode("utf-8")
    # the exporter's own output is an example too
    again = docxout.config(dockit.style_of(out))
    assert again["body"] == cfg["body"] and again["page"] == cfg["page"]


def test_check_finds_what_is_left_undone(tmp_path):
    sample = _foreign(tmp_path)
    got = dockit.check(sample)
    texts = [(i["level"], i["where"], i["text"]) for i in got["issues"]]
    assert got["summary"] == {"error": 0, "warning": 4, "info": 0} and got["totals"] == {"headings": 4, "tables": 1, "pictures": 2}
    assert ("warning", "раздел «3 Гарантии»", "раздел пуст: под заголовком нет текста") in texts
    assert ("warning", "раздел «2 Технические данные»", "рисунок без подписи «Рисунок N — …»") in texts
    assert sum(1 for t in texts if "осталась заготовка" in t[2] and t[1] == "раздел «1 Общие сведения»") == 2
    md = tmp_path / "new.md"
    md.write_text("# 1 Общие сведения\n\nТекст [заполнить: 2 абзаца].\n\n### 1.1.1 Глубоко\n\n[…]\n\n# 3 Гарантии.\n\n| а | б |\n|---|---|\n| 1 | 2 |\n\n"
                  "![](x.png)\n\n# Монтаж\n\nТекст.\n", encoding="utf-8")
    got = dockit.check(str(md), outline_from=sample)
    texts = [i["text"] for i in got["issues"]]
    assert got["missing"] == ["1.1 Комплектность", "2 Технические данные"] and got["extra"] == ["1.1.1 Глубоко", "Монтаж"]
    for piece in ("осталась заготовка «[заполнить»: …Текст [заполнить: 2 абзаца].…", "осталась заготовка «[…]»: …[…]…",
                  "уровень заголовка перескакивает с 1 на 3", "номер 1.1.1: раздела 1.1 перед ним нет", "номера не по порядку: после 1 идёт 3",
                  "таблица без подписи «Таблица N — …» (а | б)", "рисунок без подписи «Рисунок N — …»", "нет раздела образца «1.1 Комплектность»",
                  "нет раздела образца «2 Технические данные»"):
        assert piece in texts
    assert [(i["where"], i["text"]) for i in got["issues"] if i["level"] == "info"] == [
        ("раздел «1.1.1 Глубоко»", "такого раздела нет в образце"), ("раздел «Монтаж»", "такого раздела нет в образце")]
    # a finished document made from the example's outline is clean
    _project(tmp_path, md="# 1 Общие сведения\n\nТекст.\n\n## 1.1 Комплектность\n\n- шкаф\n\n# 2 Технические данные\n\nТаблица: Параметры\n"
                          "| а | б |\n|---|---|\n| 1 | 2 |\n\n![Шкаф](img/pic.png)\n\n# 3 Гарантии\n\nТекст.\n")
    out, _, warnings = _build(tmp_path, md=(tmp_path / "manual.md").read_text(encoding="utf-8"))
    assert warnings == [] and dockit.check(out, outline_from=sample) == {
        "summary": {"error": 0, "warning": 0, "info": 0}, "totals": {"headings": 4, "tables": 1, "pictures": 1}, "missing": [], "extra": [],
        "issues": []}
    (tmp_path / "empty.md").write_text("\n", encoding="utf-8")
    assert dockit.check(str(tmp_path / "empty.md"))["summary"]["error"] == 1
    # a Markdown example gives its outline too
    o = dockit.outline(str(md))
    assert o["format"] == "md" and [h["text"] for h in o["headings"]] == ["1 Общие сведения", "1.1.1 Глубоко", "3 Гарантии.", "Монтаж"]
    assert "page" not in o and "# 3 Гарантии.\n[заполнить: таблица 2×2 (а | б), 1 рисунок]" in o["outline_md"]


def test_server_reads_exports_and_checks(tmp_path):
    md, sample = _project(tmp_path), _foreign(tmp_path)
    out = tmp_path / "out" / "Руководство.docx"
    replies = _server([
        ("doc_outline", {"file": sample}), ("doc_text", {"file": sample, "section": "Комплектность"}),
        ("doc_export", {"md_file": md, "out_file": str(out), "style_from": sample, "title": "Руководство оператора", "toc": True,
                        "organization": "ООО «СиСАТ»", "city_year": "Минск 2026"}),
        ("doc_outline", {"file": str(out)}), ("doc_check", {"file": str(out), "outline_from": sample}),
        ("doc_export", {"md_file": md, "out_file": str(tmp_path / "out" / "x.pdf")}),
        ("doc_export", {"md_file": str(tmp_path / "nope.md"), "out_file": str(tmp_path / "out" / "x.docx")}),
        ("doc_export", {"md_file": md}), ("doc_text", {"file": str(tmp_path / "old.doc")}), ("doc_nope", {}),
    ])
    assert replies[0]["result"]["serverInfo"] == {"name": "doc-kit", "title": "Документы Word по образцу", "version": "1.0.0"}
    assert [t["name"] for t in replies[1]["result"]["tools"]] == TOOLS
    assert [t["annotations"]["readOnlyHint"] for t in replies[1]["result"]["tools"]] == [True, True, False, True]
    r = [x["result"] for x in replies[2:]]
    assert json.loads(r[0]["content"][0]["text"])["styles"]["heading1"]["style_name"] == "Заголовок 1"
    assert r[1]["content"][0]["text"] == "## 1.1 Комплектность\n\n- шкаф управления\n- панель оператора\n1. первый шаг\n"
    assert r[2]["structuredContent"]["written"] and r[2]["structuredContent"]["counts"]["pictures"] == 2
    assert r[2]["structuredContent"]["style"]["source"] == "example" and "по образцу: 297×210 мм, Arial 12 пт" in r[2]["content"][0]["text"]
    assert docxout.verify(out.read_bytes()) == []
    made = json.loads(r[3]["content"][0]["text"])
    assert made["title_page"] == ["ООО «СиСАТ»", "Руководство оператора", "Минск 2026"] and made["totals"]["headings"] == 8
    assert made["styles"]["normal"]["font"] == "Arial" and made["page"]["orientation"] == "landscape"
    checked = json.loads(r[4]["content"][0]["text"])
    assert checked["missing"] == ["1 Общие сведения", "1.1 Комплектность", "2 Технические данные", "3 Гарантии"] and len(checked["extra"]) == 8
    assert r[5]["isError"] and "должен кончаться на .docx" in r[5]["content"][0]["text"]
    assert r[6]["isError"] and r[6]["content"][0]["text"].startswith("файл: ") and not (tmp_path / "out" / "x.docx").exists()
    assert r[7]["isError"] and "не хватает поля 'out_file'" in r[7]["content"][0]["text"]
    assert r[8]["isError"] and r[8]["content"][0]["text"].startswith("файл: ")
    assert r[9]["isError"] and "нет инструмента doc_nope" in r[9]["content"][0]["text"]
