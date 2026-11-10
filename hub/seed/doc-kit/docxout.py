"""doc-kit: Markdown blocks (docmd) → a Word document (.docx) — an OOXML package written with the standard library.

The look comes from a config: GOST-like defaults (A4, margins 30/15/20/20 mm, Times New Roman 14, 1.5 lines, indent
12.5 mm, justified) or the values read from an example document (dockit.style_of). Nothing is sent anywhere: the
addresses below are XML namespace names."""
import datetime
import io
import os
import re
import struct
import zipfile
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape, quoteattr

import docmd
from dockit import NO_NUMBER, NUMBER, PLACEHOLDER, DocError, note_text

NS_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS_WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
NS_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
NS_PIC = "http://schemas.openxmlformats.org/drawingml/2006/picture"
NS_PKG = "http://schemas.openxmlformats.org/package/2006"
NS_DOC = "http://schemas.openxmlformats.org/officeDocument/2006"
CT_W = "application/vnd.openxmlformats-officedocument.wordprocessingml"
XML = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
IMAGE_TYPES = {"png": "image/png", "jpeg": "image/jpeg", "gif": "image/gif", "bmp": "image/bmp"}
MAX_IMAGE = 60 * 1024 * 1024
BAD_CHARS = re.compile("[\\x00-\\x08\\x0b\\x0c\\x0e-\\x1f\\ufffe\\uffff]")
WIDTH = re.compile(r"(?:width|ширина)\s*=\s*(\d+(?:[.,]\d+)?)\s*(mm|мм|cm|см|px|%)?", re.I)

DEFAULTS = {
    "page": {"width_mm": 210.0, "height_mm": 297.0, "orientation": "portrait",
             "margins_mm": {"top": 20.0, "bottom": 20.0, "left": 30.0, "right": 15.0, "header": 12.5, "footer": 12.5}},
    "body": {"font": "Times New Roman", "size_pt": 14.0, "line": 360, "line_rule": "auto", "first_line_mm": 12.5,
             "align": "both", "before_pt": 0.0, "after_pt": 0.0},
    "headings": {1: {"size_pt": 16.0, "bold": True, "before_pt": 12.0, "after_pt": 12.0},
                 2: {"size_pt": 14.0, "bold": True, "before_pt": 12.0, "after_pt": 6.0},
                 3: {"size_pt": 14.0, "bold": True, "before_pt": 12.0, "after_pt": 6.0},
                 4: {"size_pt": 14.0, "bold": True, "italic": True, "before_pt": 6.0, "after_pt": 6.0}},
}


def config(example: dict | None = None) -> dict:
    """The defaults, with what the example sets on top (page, body text, headings 1..4)."""
    cfg = {"page": dict(DEFAULTS["page"], margins_mm=dict(DEFAULTS["page"]["margins_mm"])), "body": dict(DEFAULTS["body"]),
           "headings": {n: dict(h) for n, h in DEFAULTS["headings"].items()}, "source": "default"}
    if not example:
        return cfg
    cfg["source"] = "example"
    page = example.get("page") or {}
    if page.get("width_mm") and page.get("height_mm"):
        cfg["page"].update({k: page[k] for k in ("width_mm", "height_mm", "orientation") if page.get(k)})
        cfg["page"]["margins_mm"].update(page.get("margins_mm") or {})
    body = example.get("body") or {}
    keys = ("font", "size_pt", "line", "line_rule", "first_line_mm", "align", "before_pt", "after_pt")
    cfg["body"].update({k: body[k] for k in keys if body.get(k) is not None})
    if "line" in body and "line_rule" not in body:
        cfg["body"]["line_rule"] = "auto"
    cfg["body"]["first_line_mm"] = max(cfg["body"]["first_line_mm"], 0.0)
    heads = example.get("headings") or {}
    for level in (1, 2, 3, 4):
        src = heads.get(level) or (heads.get(3) if level == 4 else None)
        if src:
            cfg["headings"][level] = {k: src[k] for k in ("font", "size_pt", "bold", "italic", "caps", "color", "align",
                                                          "before_pt", "after_pt", "first_line_mm") if src.get(k) is not None}
    return cfg


def _tw(mm: float) -> int:
    return int(round(mm * 1440 / 25.4))


# ---------------------------------------------------------------------- pictures
def image_size(data: bytes):
    """(kind, width, height in pixels) from the file header; None when it is not PNG / JPEG / GIF / BMP."""
    if data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
        return ("png",) + struct.unpack(">II", data[16:24])
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ("gif",) + struct.unpack("<HH", data[6:10])
    if data[:2] == b"BM" and len(data) >= 26:
        w, h = struct.unpack("<ii", data[18:26])
        return "bmp", abs(w), abs(h)
    if data[:2] == b"\xff\xd8":
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xFF, 0x00, 0x01) or 0xD0 <= marker <= 0xD9:
                i += 2 if marker != 0xFF else 1
                continue
            size = struct.unpack(">H", data[i + 2:i + 4])[0]
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                h, w = struct.unpack(">HH", data[i + 5:i + 9])
                return "jpeg", w, h
            i += 2 + size
    return None


# ---------------------------------------------------------------------- XML pieces
def _text_xml(text: str) -> str:
    out = []
    for n, line in enumerate(BAD_CHARS.sub("", text).split("\n")):
        if n:
            out.append("<w:br/>")
        for k, part in enumerate(line.split("\t")):
            if k:
                out.append("<w:tab/>")
            if part:
                out.append(f'<w:t xml:space="preserve">{escape(part)}</w:t>')
    return "".join(out)


def _rpr(font=None, bold=False, italic=False, caps=False, color=None, size=None) -> str:
    out = []
    if font:
        f = quoteattr(font)
        out.append(f"<w:rFonts w:ascii={f} w:hAnsi={f} w:cs={f} w:eastAsia={f}/>")
    if bold:
        out.append("<w:b/><w:bCs/>")
    if italic:
        out.append("<w:i/><w:iCs/>")
    if caps:
        out.append("<w:caps/>")
    if color:
        out.append(f'<w:color w:val="{color}"/>')
    if size:
        out.append(f'<w:sz w:val="{int(round(size * 2))}"/><w:szCs w:val="{int(round(size * 2))}"/>')
    return f"<w:rPr>{''.join(out)}</w:rPr>" if out else ""


def _run(text: str, bold=False, italic=False, code=False) -> str:
    return f"<w:r>{_rpr('Courier New' if code else None, bold, italic)}{_text_xml(text)}</w:r>" if text else ""


def _runs(text: str, bold=False) -> str:
    return "".join(_run(t, b or bold, i, c) for t, b, i, c in docmd.inline(text))


def _ppr(keep_next=False, keep_lines=False, tabs="", before=None, after=None, line=None, rule="auto", left=None, first=None,
         jc=None, outline=None) -> str:
    out = []
    if keep_next:
        out.append("<w:keepNext/>")
    if keep_lines:
        out.append("<w:keepLines/>")
    out.append(tabs)
    sp = "".join(f' w:{k}="{int(round(v))}"' for k, v in (("before", before), ("after", after), ("line", line)) if v is not None)
    if sp:
        out.append(f"<w:spacing{sp}" + (f' w:lineRule="{rule}"' if line is not None else "") + "/>")
    if left is not None or first is not None:
        out.append(f'<w:ind w:left="{int(left or 0)}" w:firstLine="{int(first or 0)}"/>')
    if jc:
        out.append(f'<w:jc w:val="{jc}"/>')
    if outline is not None:
        out.append(f'<w:outlineLvl w:val="{outline}"/>')
    return f"<w:pPr>{''.join(out)}</w:pPr>"


def _style(sid, name, ppr="", rpr="", based="Normal", next_="", kind="paragraph", default=False, extra="") -> str:
    return (f'<w:style w:type="{kind}"' + (' w:default="1"' if default else "") + f' w:styleId="{sid}"><w:name w:val="{name}"/>'
            + (f'<w:basedOn w:val="{based}"/>' if based else "") + (f'<w:next w:val="{next_}"/>' if next_ else "")
            + "<w:qFormat/>" + ppr + rpr + extra + "</w:style>")


def styles_xml(cfg: dict) -> str:
    body = cfg["body"]
    size, first, line, rule = body["size_pt"], _tw(body["first_line_mm"]), body["line"], body["line_rule"]
    align = body["align"] if body["align"] in ("both", "left", "center", "right") else "both"
    small = max(size - 2, 9)
    text_w = _tw(cfg["page"]["width_mm"] - cfg["page"]["margins_mm"]["left"] - cfg["page"]["margins_mm"]["right"])
    out = [XML, f'<w:styles xmlns:w="{NS_W}"><w:docDefaults><w:rPrDefault>',
           _rpr(body["font"], size=size).replace("</w:rPr>", '<w:lang w:val="ru-RU" w:eastAsia="ru-RU" w:bidi="ar-SA"/></w:rPr>'),
           '</w:rPrDefault><w:pPrDefault><w:pPr><w:spacing w:after="0" w:line="240" w:lineRule="auto"/></w:pPr></w:pPrDefault></w:docDefaults>',
           _style("Normal", "Normal", _ppr(before=body["before_pt"] * 20, after=body["after_pt"] * 20, line=line, rule=rule,
                                           left=0, first=first, jc=align), based="", default=True)]
    for level in (1, 2, 3, 4):
        h = cfg["headings"][level]
        jc = h.get("align") or "left"
        ind = 0 if jc == "center" else _tw(max(h["first_line_mm"], 0)) if h.get("first_line_mm") is not None else first
        out.append(_style(f"Heading{level}", f"heading {level}",
                          _ppr(keep_next=True, keep_lines=True, before=h.get("before_pt", 12) * 20, after=h.get("after_pt", 6) * 20,
                               left=0, first=ind, jc=jc if jc in ("both", "left", "center", "right") else "left", outline=level - 1),
                          _rpr(h.get("font"), h.get("bold", False), h.get("italic", False), h.get("caps", False), h.get("color"),
                               h.get("size_pt")), next_="Normal"))
    plain = {"left": 0, "first": 0, "line": 240, "rule": "auto"}
    out += [
        _style("Title", "Title", _ppr(before=0, after=240, jc="center", **plain), _rpr(bold=True, size=size + 8)),
        _style("Subtitle", "Subtitle", _ppr(before=0, after=120, jc="center", **plain), _rpr(size=size + 2)),
        _style("TitleText", "Title Page Text", _ppr(before=0, after=0, jc="center", **plain)),
        _style("Figure", "Figure", _ppr(keep_next=True, before=120, after=0, jc="center", **plain)),
        _style("Caption", "caption", _ppr(before=60, after=240, jc="center", **plain)),
        _style("TableCaption", "Table Caption", _ppr(keep_next=True, before=240, after=60, jc="left", **plain)),
        _style("TableText", "Table Text", _ppr(before=0, after=0, jc="left", **plain), _rpr(size=small)),
        _style("Note", "Note", ""),
        _style("Code", "Code", _ppr(before=0, after=0, jc="left", **plain), _rpr("Courier New", size=max(size - 4, 8))),
        _style("ListParagraph", "List Paragraph", ""),
        _style("Footer", "footer", _ppr(before=0, after=0, jc="center", **plain), _rpr(size=small)),
        _style("TOCHeading", "TOC Heading", _ppr(keep_next=True, before=0, after=240, jc="center", **plain), _rpr(bold=True, size=size + 2)),
    ]
    tab = f'<w:tabs><w:tab w:val="right" w:leader="dot" w:pos="{text_w}"/></w:tabs>'
    for n in (1, 2, 3):
        out.append(_style(f"TOC{n}", f"toc {n}", _ppr(tabs=tab, before=0, after=60, line=240, left=(n - 1) * _tw(5), first=0, jc="left"),
                          next_="Normal"))
    border = "".join(f'<w:{side} w:val="single" w:sz="4" w:space="0" w:color="000000"/>'
                     for side in ("top", "left", "bottom", "right", "insideH", "insideV"))
    out += ['<w:style w:type="character" w:default="1" w:styleId="DefaultParagraphFont"><w:name w:val="Default Paragraph Font"/></w:style>',
            '<w:style w:type="table" w:default="1" w:styleId="TableNormal"><w:name w:val="Normal Table"/><w:tblPr><w:tblInd w:w="0" '
            'w:type="dxa"/><w:tblCellMar><w:top w:w="0" w:type="dxa"/><w:left w:w="108" w:type="dxa"/><w:bottom w:w="0" w:type="dxa"/>'
            '<w:right w:w="108" w:type="dxa"/></w:tblCellMar></w:tblPr></w:style>',
            '<w:style w:type="table" w:styleId="TableGrid"><w:name w:val="Table Grid"/><w:basedOn w:val="TableNormal"/>'
            f"<w:tblPr><w:tblBorders>{border}</w:tblBorders></w:tblPr></w:style>", "</w:styles>"]
    return "".join(out)


def numbering_xml(cfg: dict, numbered: list) -> str:
    """Two multi-level lists: 0 — dashes, 1 — numbers; num 1 is the bullets, every numbered list restarts with its own num."""
    first = _tw(cfg["body"]["first_line_mm"]) or _tw(7.5)
    out = [XML, f'<w:numbering xmlns:w="{NS_W}">']
    for aid, kind in ((0, "bullet"), (1, "number")):
        out.append(f'<w:abstractNum w:abstractNumId="{aid}"><w:multiLevelType w:val="multilevel"/>')
        for lvl in range(3):
            fmt, mark = ("bullet", "–" if lvl == 0 else "•") if kind == "bullet" else ("decimal", f"%{lvl + 1}." if lvl == 0 else f"%{lvl + 1})")
            out.append(f'<w:lvl w:ilvl="{lvl}"><w:start w:val="1"/><w:numFmt w:val="{fmt}"/><w:suff w:val="space"/>'
                       f'<w:lvlText w:val="{mark}"/><w:lvlJc w:val="left"/><w:pPr><w:ind w:left="{lvl * first}" w:firstLine="{first}"/></w:pPr></w:lvl>')
        out.append("</w:abstractNum>")
    out.append('<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>')
    for num_id, level, start in numbered:
        over = "".join(f'<w:lvlOverride w:ilvl="{lvl}"><w:startOverride w:val="{start if lvl == level else 1}"/></w:lvlOverride>'
                       for lvl in range(3))
        out.append(f'<w:num w:numId="{num_id}"><w:abstractNumId w:val="1"/>{over}</w:num>')
    out.append("</w:numbering>")
    return "".join(out)


# ---------------------------------------------------------------------- the document
class _Writer:
    def __init__(self, cfg: dict, base_dir: str):
        self.cfg, self.base = cfg, base_dir
        page, mar = cfg["page"], cfg["page"]["margins_mm"]
        self.text_w = _tw(page["width_mm"] - mar["left"] - mar["right"])
        self.text_h = _tw(page["height_mm"] - mar["top"] - mar["bottom"])
        self.body, self.warnings, self.missing = [], [], []
        self.media, self.numbered = {}, []                 # {abs path: (rId, part name, kind, w, h, data)}; (numId, level, start)
        self.pending_break = False
        self.counts = {"headings": 0, "paragraphs": 0, "tables": 0, "pictures": 0, "list_items": 0, "code_lines": 0}
        self.toc_entries = []

    def p(self, style: str, content: str, num=None, jc=None) -> None:
        ppr = f'<w:pStyle w:val="{style}"/>'
        if self.pending_break:
            ppr += "<w:pageBreakBefore/>"
            self.pending_break = False
        if num:
            ppr += f'<w:numPr><w:ilvl w:val="{num[1]}"/><w:numId w:val="{num[0]}"/></w:numPr>'
        if jc:
            ppr += f'<w:jc w:val="{jc}"/>'
        self.body.append(f"<w:p><w:pPr>{ppr}</w:pPr>{content}</w:p>")

    def flush_break(self) -> None:
        """A page break before something that is not a paragraph (a table): a paragraph one point high carries it."""
        if self.pending_break:
            self.pending_break = False
            self.body.append('<w:p><w:pPr><w:pageBreakBefore/><w:spacing w:before="0" w:after="0" w:line="20" w:lineRule="exact"/>'
                             '<w:ind w:left="0" w:firstLine="0"/><w:rPr><w:sz w:val="2"/></w:rPr></w:pPr></w:p>')

    # -- pictures
    def load_image(self, block: dict):
        path = block["path"]
        if re.match(r"^[a-z][a-z0-9+.-]*://", path, re.I):
            self.missing.append(f"{path} — картинки берутся только из файлов на диске")
            return None
        full = os.path.normpath(path if os.path.isabs(path) else os.path.join(self.base, path))
        if full in self.media:
            return self.media[full]
        try:
            if os.path.getsize(full) > MAX_IMAGE:
                self.missing.append(f"{path} — файл больше {MAX_IMAGE // (1024 * 1024)} МБ")
                return None
            with open(full, "rb") as f:
                data = f.read()
        except OSError:
            self.missing.append(f"{path} — файла нет ({full})")
            return None
        info = image_size(data)
        if not info or not info[1] or not info[2]:
            self.missing.append(f"{path} — не PNG, JPEG, GIF или BMP")
            return None
        n = len(self.media) + 1
        self.media[full] = (f"rIdImg{n}", f"media/image{n}.{info[0]}", info[0], info[1], info[2], data)
        return self.media[full]

    def image(self, block: dict) -> None:
        got = self.load_image(block)
        if got is None:
            return
        rid, part, _, px_w, px_h, _ = got
        emu_w, text_emu = px_w * 9525, self.text_w * 635
        m = WIDTH.search(block["title"])
        if m:
            value, unit = float(m.group(1).replace(",", ".")), (m.group(2) or "mm").lower()
            emu_w = int(value * {"mm": 36000, "мм": 36000, "cm": 360000, "см": 360000, "px": 9525, "%": text_emu / 100}[unit])
        elif block["title"].strip():
            self.warnings.append(f"картинка {block['path']}: в кавычках понятна только ширина — \"width=120mm\"")
        if emu_w > text_emu:
            if m:
                self.warnings.append(f"картинка {block['path']}: ширина больше поля текста — уменьшена до {text_emu / 36000:.0f} мм")
            emu_w = text_emu
        emu_h = int(emu_w * px_h / px_w)
        max_h = int(self.text_h * 635 * 0.85)
        if emu_h > max_h:
            emu_w, emu_h = int(emu_w * max_h / emu_h), max_h
        self.counts["pictures"] += 1
        n = self.counts["pictures"]
        caption = f"Рисунок {n}" + (f" — {block['caption']}" if block["caption"] else "")
        self.p("Figure", f'<w:r><w:drawing><wp:inline distT="0" distB="0" distL="0" distR="0"><wp:extent cx="{emu_w}" cy="{emu_h}"/>'
               f'<wp:effectExtent l="0" t="0" r="0" b="0"/><wp:docPr id="{n}" name={quoteattr(f"Рисунок {n}")} '
               f'descr={quoteattr(BAD_CHARS.sub("", block["caption"]))}/><wp:cNvGraphicFramePr><a:graphicFrameLocks noChangeAspect="1"/>'
               f'</wp:cNvGraphicFramePr><a:graphic><a:graphicData uri="{NS_PIC}"><pic:pic><pic:nvPicPr><pic:cNvPr id="{n}" '
               f'name={quoteattr(os.path.basename(part))}/><pic:cNvPicPr/></pic:nvPicPr><pic:blipFill><a:blip r:embed="{rid}"/>'
               f'<a:stretch><a:fillRect/></a:stretch></pic:blipFill><pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{emu_w}" cy="{emu_h}"/>'
               '</a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr></pic:pic></a:graphicData></a:graphic></wp:inline>'
               "</w:drawing></w:r>")
        self.p("Caption", _run(caption))

    # -- tables
    def table(self, block: dict) -> None:
        rows = [block["header"]] + block["rows"]
        cols = max(len(r) for r in rows)
        if any(len(r) != cols for r in rows):
            self.warnings.append(f"таблица «{' | '.join(block['header'])[:60]}»: в строках разное число ячеек — дополнены пустыми")
        rows = [r + [""] * (cols - len(r)) for r in rows]
        self.counts["tables"] += 1
        if block["caption"]:
            self.p("TableCaption", _runs(f"Таблица {self.counts['tables']} — {block['caption']}"))
        self.flush_break()
        weights = [min(max(max(len(docmd.plain(r[c])) for r in rows), 4), 40) for c in range(cols)]
        widths = [int(self.text_w * w / sum(weights)) for w in weights]
        aligns = (block["aligns"] + ["left"] * cols)[:cols]
        border = "".join(f'<w:{s} w:val="single" w:sz="4" w:space="0" w:color="000000"/>' for s in ("top", "left", "bottom", "right", "insideH", "insideV"))
        out = [f'<w:tbl><w:tblPr><w:tblStyle w:val="TableGrid"/><w:tblW w:w="{sum(widths)}" w:type="dxa"/><w:tblBorders>{border}</w:tblBorders>'
               '<w:tblLook w:val="04A0"/></w:tblPr><w:tblGrid>', "".join(f'<w:gridCol w:w="{w}"/>' for w in widths), "</w:tblGrid>"]
        for n, row in enumerate(rows):
            out.append("<w:tr>" + ("<w:trPr><w:cantSplit/><w:tblHeader/></w:trPr>" if n == 0 else ""))
            for c, cell in enumerate(row):
                jc = "center" if n == 0 else {"left": "left", "center": "center", "right": "right"}[aligns[c]]
                out.append(f'<w:tc><w:tcPr><w:tcW w:w="{widths[c]}" w:type="dxa"/><w:vAlign w:val="center"/></w:tcPr><w:p><w:pPr>'
                           f'<w:pStyle w:val="TableText"/><w:jc w:val="{jc}"/></w:pPr>{_runs(cell, bold=n == 0)}</w:p></w:tc>')
            out.append("</w:tr>")
        out.append("</w:tbl>")
        self.body.append("".join(out))
        self.body.append('<w:p><w:pPr><w:pStyle w:val="TableText"/></w:pPr></w:p>')

    # -- the title page and the contents
    def title_page(self, title, subtitle, organization, city_year) -> None:
        heights = [int((self.text_h - 700) * k) for k in (0.15, 0.6, 0.25)]

        def lines(text, style):
            return "".join(f'<w:p><w:pPr><w:pStyle w:val="{style}"/></w:pPr>{_run(x.strip())}</w:p>'
                           for x in str(text or "").replace("\\n", "\n").split("\n") if x.strip())

        cells = [(lines(organization, "TitleText"), "top"), (lines(title, "Title") + lines(subtitle, "Subtitle"), "center"),
                 (lines(city_year, "TitleText"), "bottom")]
        out = [f'<w:tbl><w:tblPr><w:tblW w:w="{self.text_w}" w:type="dxa"/><w:tblLayout w:type="fixed"/></w:tblPr><w:tblGrid>'
               f'<w:gridCol w:w="{self.text_w}"/></w:tblGrid>']
        for (content, valign), height in zip(cells, heights):
            out.append(f'<w:tr><w:trPr><w:cantSplit/><w:trHeight w:val="{height}" w:hRule="exact"/></w:trPr><w:tc><w:tcPr>'
                       f'<w:tcW w:w="{self.text_w}" w:type="dxa"/><w:vAlign w:val="{valign}"/></w:tcPr>'
                       + (content or '<w:p><w:pPr><w:pStyle w:val="TitleText"/></w:pPr></w:p>') + "</w:tc></w:tr>")
        out.append("</w:tbl>")
        self.body.append("".join(out))
        self.body.append('<w:p><w:pPr><w:spacing w:before="0" w:after="0" w:line="20" w:lineRule="exact"/><w:ind w:left="0" w:firstLine="0"/>'
                         '<w:rPr><w:sz w:val="2"/></w:rPr></w:pPr></w:p>')
        self.pending_break = True

    def toc(self, entries: list) -> None:
        """A TOC field Word fills on opening; until then it shows the headings without page numbers."""
        self.p("TOCHeading", _run("Содержание"))
        begin = ('<w:r><w:fldChar w:fldCharType="begin" w:dirty="true"/></w:r><w:r><w:instrText xml:space="preserve"> TOC \\o "1-3" \\h \\z \\u '
                 '</w:instrText></w:r><w:r><w:fldChar w:fldCharType="separate"/></w:r>')
        end = '<w:r><w:fldChar w:fldCharType="end"/></w:r>'
        entries = [(lvl, text) for lvl, text in entries if lvl <= 3] or [(1, "Обновите поле: F9 в Word")]
        for n, (level, text) in enumerate(entries):
            self.p(f"TOC{level}", (begin if n == 0 else "") + _run(text) + (end if n == len(entries) - 1 else ""))
        self.pending_break = True

    # -- blocks
    def blocks(self, blocks: list, numbering: bool) -> None:
        counters, skip_under = [0, 0, 0, 0], False
        for b in blocks:
            t = b["t"]
            if t == "h":
                level, text = b["level"], b["text"]
                m = NUMBER.match(docmd.plain(text))
                if m:
                    parts = [int(x) for x in m.group(1).split(".")][:4]
                    counters[:len(parts)] = parts
                    counters[len(parts):] = [0] * (4 - len(parts))
                    skip_under = False
                elif numbering:
                    if level == 1:
                        skip_under = b.get("unnumbered") or bool(NO_NUMBER.match(docmd.plain(text).strip()))
                    if not (skip_under or b.get("unnumbered")):
                        counters[level - 1] += 1
                        counters[level:] = [0] * (4 - level)
                        text = ".".join(str(c) for c in counters[:level] if c) + " " + text
                self.counts["headings"] += 1
                self.toc_entries.append((level, docmd.plain(text)))
                self.p(f"Heading{level}", _runs(text))
            elif t == "p":
                self.counts["paragraphs"] += 1
                self.p("Normal", _runs(b["text"]))
            elif t == "note":
                self.counts["paragraphs"] += 1
                self.p("Note", _runs(note_text(b["text"], b["first"])))
            elif t == "list":
                current = {}
                for item in b["items"]:
                    lvl = item["level"]
                    for deeper in [k for k in current if k > lvl]:
                        del current[deeper]
                    if lvl not in current or current[lvl][0] != item["kind"]:
                        if item["kind"] == "bullet":
                            current[lvl] = ("bullet", 1)
                        else:
                            current[lvl] = ("number", 2 + len(self.numbered))
                            self.numbered.append((current[lvl][1], lvl, item["start"]))
                    self.counts["list_items"] += 1
                    self.p("ListParagraph", _runs(item["text"]), num=(current[lvl][1], lvl))
            elif t == "code":
                for line in b["lines"] or [""]:
                    self.counts["code_lines"] += 1
                    self.p("Code", _run(line.expandtabs(4)))
            elif t == "img":
                self.image(b)
            elif t == "table":
                self.table(b)
            elif t == "break":
                self.pending_break = True

    def document_xml(self, title_page: bool) -> str:
        page, mar = self.cfg["page"], self.cfg["page"]["margins_mm"]
        orient = ' w:orient="landscape"' if page["width_mm"] > page["height_mm"] else ""
        sect = ('<w:sectPr><w:footerReference w:type="default" r:id="rIdFooter"/>'
                f'<w:pgSz w:w="{_tw(page["width_mm"])}" w:h="{_tw(page["height_mm"])}"{orient}/>'
                f'<w:pgMar w:top="{_tw(mar["top"])}" w:right="{_tw(mar["right"])}" w:bottom="{_tw(mar["bottom"])}" w:left="{_tw(mar["left"])}" '
                f'w:header="{_tw(mar.get("header", 12.5))}" w:footer="{_tw(mar.get("footer", 12.5))}" w:gutter="0"/>'
                + ("<w:titlePg/>" if title_page else "") + "</w:sectPr>")
        return (f'{XML}<w:document xmlns:w="{NS_W}" xmlns:r="{NS_R}" xmlns:wp="{NS_WP}" xmlns:a="{NS_A}" xmlns:pic="{NS_PIC}"><w:body>'
                + "".join(self.body) + sect + "</w:body></w:document>")


def _lint(blocks: list) -> list:
    """Warnings about the structure: level jumps, empty sections, placeholders left in the text."""
    out, prev = [], 0
    for n, b in enumerate(blocks):
        texts = [b.get("text", "")] + [i["text"] for i in b.get("items", [])] + [c for r in b.get("rows", []) for c in r]
        for s in texts:
            m = PLACEHOLDER.search(s)
            if m:
                out.append(f"в тексте осталась заготовка «{m.group(0)}»: {s[:60]}")
        if b["t"] != "h":
            continue
        if prev and b["level"] > prev + 1:
            out.append(f"заголовок «{b['text'][:60]}»: уровень перескакивает с {prev} на {b['level']}")
        prev = b["level"]
        nxt = next((x for x in blocks[n + 1:] if x["t"] != "break"), None)
        if nxt is None or (nxt["t"] == "h" and nxt["level"] <= b["level"]):
            out.append(f"раздел «{b['text'][:60]}» пуст")
    return out


def package(parts: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in parts.items():
            z.writestr(zipfile.ZipInfo(name, date_time=datetime.datetime.now().timetuple()[:6]),
                       data if isinstance(data, bytes) else data.encode("utf-8"), zipfile.ZIP_DEFLATED)
    return buf.getvalue()


def verify(blob: bytes) -> list:
    """What is wrong with a .docx package ([] — fine): XML that does not parse, relationships to missing parts,
    parts without a content type, r:id in the document without a relationship."""
    problems = []
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        names = z.namelist()
        roots = {}
        for name in names:
            if name.endswith((".xml", ".rels")):
                try:
                    roots[name] = ET.fromstring(z.read(name))
                except ET.ParseError as exc:
                    problems.append(f"{name}: XML не разбирается ({exc})")
        types = roots.get("[Content_Types].xml")
        if types is None:
            return problems + ["нет [Content_Types].xml"]
        defaults = {e.get("Extension", "").lower() for e in types if e.tag.endswith("}Default")}
        overrides = {e.get("PartName", "") for e in types if e.tag.endswith("}Override")}
        for name in names:
            if name != "[Content_Types].xml" and "/" + name not in overrides and name.rsplit(".", 1)[-1].lower() not in defaults:
                problems.append(f"{name}: нет типа содержимого")
        for over in overrides:
            if over.lstrip("/") not in names:
                problems.append(f"[Content_Types].xml: части {over} нет")
        for name, root in roots.items():
            if not name.endswith(".rels"):
                continue
            folder = os.path.dirname(os.path.dirname(name))
            owner = (folder + "/" if folder else "") + os.path.basename(name)[:-5]
            ids = set()
            for rel in root:
                ids.add(rel.get("Id"))
                target = rel.get("Target", "")
                full = target.lstrip("/") if target.startswith("/") else os.path.normpath(os.path.join(folder, target)).replace(os.sep, "/")
                if rel.get("TargetMode") != "External" and full not in names:
                    problems.append(f"{name}: связь {rel.get('Id')} ведёт к {full}, а такой части нет")
            if owner in roots:
                for el in roots[owner].iter():
                    for key, value in el.attrib.items():
                        if key.startswith("{" + NS_R + "}") and value not in ids:
                            problems.append(f"{owner}: {key.rsplit('}', 1)[-1]}=\"{value}\" без связи")
        for need in ("_rels/.rels", "word/document.xml", "word/styles.xml"):
            if need not in names:
                problems.append(f"нет части {need}")
    return problems


def build(md_text: str, base_dir: str, cfg: dict, title="", subtitle="", organization="", city_year="", toc=False,
          numbering=True) -> tuple:
    """Markdown → (the .docx bytes, counts, warnings). DocError when pictures are missing."""
    blocks, warnings = docmd.parse(md_text)
    w = _Writer(cfg, base_dir)
    w.warnings = warnings + _lint(blocks)
    for b in blocks:                                            # every missing picture is named before anything is written
        if b["t"] == "img":
            w.load_image(b)
    if w.missing:
        raise DocError("Нет картинок, файл не записан:\n" + "\n".join(dict.fromkeys(w.missing)))
    if title:
        w.title_page(title, subtitle, organization, city_year)
    if toc:
        scratch = _Writer(cfg, base_dir)                        # a first pass only for the numbered heading texts
        scratch.media = w.media
        scratch.blocks(blocks, numbering)
        w.toc(scratch.toc_entries)
    w.blocks(blocks, numbering)
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    doc_title = title or next((docmd.plain(b["text"]) for b in blocks if b["t"] == "h"), "")
    kinds = sorted({m[2] for m in w.media.values()})
    rels = [("rIdStyles", "styles", "styles.xml"), ("rIdNumbering", "numbering", "numbering.xml"), ("rIdSettings", "settings", "settings.xml"),
            ("rIdFooter", "footer", "footer1.xml")] + [(m[0], "image", m[1]) for m in w.media.values()]
    parts = {
        "[Content_Types].xml": f'{XML}<Types xmlns="{NS_PKG}/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-'
        'package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>'
        + "".join(f'<Default Extension="{k}" ContentType="{IMAGE_TYPES[k]}"/>' for k in kinds)
        + "".join(f'<Override PartName="/word/{n}.xml" ContentType="{CT_W}.{t}+xml"/>' for n, t in (
            ("document", "document.main"), ("styles", "styles"), ("numbering", "numbering"), ("settings", "settings"), ("footer1", "footer")))
        + '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
        '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/></Types>',
        "_rels/.rels": f'{XML}<Relationships xmlns="{NS_PKG}/relationships">'
        f'<Relationship Id="rId1" Type="{NS_DOC}/relationships/officeDocument" Target="word/document.xml"/>'
        f'<Relationship Id="rId2" Type="{NS_PKG}/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
        f'<Relationship Id="rId3" Type="{NS_DOC}/relationships/extended-properties" Target="docProps/app.xml"/></Relationships>',
        "word/document.xml": w.document_xml(bool(title)),
        "word/styles.xml": styles_xml(cfg),
        "word/numbering.xml": numbering_xml(cfg, w.numbered),
        "word/settings.xml": f'{XML}<w:settings xmlns:w="{NS_W}"><w:defaultTabStop w:val="708"/><w:characterSpacingControl w:val="doNotCompress"/>'
        + ('<w:updateFields w:val="true"/>' if toc else "")
        + '<w:compat><w:doNotExpandShiftReturn/><w:compatSetting w:name="compatibilityMode" w:uri="http://schemas.microsoft.com/office/word" w:val="15"/></w:compat>'
        "</w:settings>",
        "word/footer1.xml": f'{XML}<w:ftr xmlns:w="{NS_W}"><w:p><w:pPr><w:pStyle w:val="Footer"/></w:pPr><w:r><w:fldChar w:fldCharType="begin"/>'
        '</w:r><w:r><w:instrText xml:space="preserve"> PAGE </w:instrText></w:r><w:r><w:fldChar w:fldCharType="separate"/></w:r><w:r>'
        '<w:t xml:space="preserve">1</w:t></w:r><w:r><w:fldChar w:fldCharType="end"/></w:r></w:p></w:ftr>',
        "word/_rels/document.xml.rels": f'{XML}<Relationships xmlns="{NS_PKG}/relationships">'
        + "".join(f'<Relationship Id="{rid}" Type="{NS_DOC}/relationships/{typ}" Target="{target}"/>' for rid, typ, target in rels)
        + "</Relationships>",
        "docProps/core.xml": f'{XML}<cp:coreProperties xmlns:cp="{NS_PKG}/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:dcterms="http://purl.org/dc/terms/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        f"<dc:title>{escape(BAD_CHARS.sub('', doc_title))}</dc:title><dc:creator>SYSAI doc-kit</dc:creator>"
        f'<dcterms:created xsi:type="dcterms:W3CDTF">{now}</dcterms:created><dcterms:modified xsi:type="dcterms:W3CDTF">{now}</dcterms:modified>'
        "</cp:coreProperties>",
        "docProps/app.xml": f'{XML}<Properties xmlns="{NS_DOC}/extended-properties"><Application>SYSAI doc-kit</Application></Properties>',
    }
    for _, part, _, _, _, data in w.media.values():
        parts["word/" + part] = data
    blob = package(parts)
    problems = verify(blob)
    if problems:
        raise DocError("документ собран с ошибкой, файл не записан: " + "; ".join(problems[:10]))
    return blob, dict(w.counts, pages=None), list(dict.fromkeys(w.warnings))
