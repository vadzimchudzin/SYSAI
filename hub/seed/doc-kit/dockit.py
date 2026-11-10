"""doc-kit: reads an example document (.docx — a zip of XML parts, or .md) into plain blocks, and from the blocks
gives the outline, the text and the checks. The Word writer is docxout.py, the Markdown parser is docmd.py.

Blocks (dicts, key "t"): h (level, text, number, title), p (text, list, ilvl, pics, code, toc, cap, caption),
tbl (rows, pics, caption). Only the standard library; nothing is sent anywhere: the addresses below are XML
namespace names."""
import os
import posixpath
import re
import zipfile
import xml.etree.ElementTree as ET
from collections import Counter

import docmd

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"
PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
MAX_PART = 96 * 1024 * 1024
TWIPS_MM = 1440 / 25.4

HEADING_NAME = re.compile(r"^(?:heading|заголовок)\s*([1-9])$", re.I)
TOC_NAME = re.compile(r"^(?:toc|оглавление|contents)\s*(?:\d|heading)|^заголовок оглавления", re.I)
CODE_NAME = re.compile(r"^(?:code|source code|html preformatted|листинг|код)$", re.I)
NUMBER = re.compile(r"^(\d+(?:\.\d+)*)\.?\s+(\S.*)$", re.S)
PIC_CAPTION = re.compile(r"^(?:рисунок|рис\.?|figure|fig\.?)(?=[\s\d]|$)", re.I)
TBL_CAPTION = re.compile(r"^(?:таблица|табл\.?|table)\s*№?\s*[A-ZА-Я]?\.?\d+", re.I)
PLACEHOLDER = re.compile(r"\bTODO\b|\bFIXME\b|\?\?\?|уточнить|\[\s*(?:…|\.\.\.)\s*\]|\[заполнить|\[рисунок", re.I)
NO_NUMBER = re.compile(r"^(?:введение|содержание|оглавление|заключение|аннотация|реферат|приложение\b.*|"
                       r"список (?:литературы|использованных источников|сокращений).*|перечень сокращений.*|"
                       r"лист регистрации изменений)$", re.I)


class DocError(Exception):
    pass


def _w(tag: str) -> str:
    return f"{{{W}}}{tag}"


T, TAB, BR, CR, RUN, P, TBL, TR, TC, SDT = (_w(x) for x in ("t", "tab", "br", "cr", "r", "p", "tbl", "tr", "tc", "sdt"))
PPR, RPR, DEL, MOVE_FROM, FLD, INSTR, FLD_SIMPLE = (_w(x) for x in ("pPr", "rPr", "del", "moveFrom", "fldChar", "instrText", "fldSimple"))
DRAWING, PICT, OBJECT, FALLBACK = _w("drawing"), _w("pict"), _w("object"), f"{{{MC}}}Fallback"


def _val(el, name: str, attr: str = "val"):
    """The w:attr of the child w:name (None when there is no such child)."""
    child = el.find(_w(name)) if el is not None else None
    return child.get(_w(attr)) if child is not None else None


def _on(el, name: str):
    """A toggle like w:b: True / False, None when absent."""
    child = el.find(_w(name)) if el is not None else None
    if child is None:
        return None
    return child.get(_w("val"), "1") not in ("0", "false", "off")


def _num(value, div: float = 1.0, digits: int = 1):
    try:
        return round(int(float(value)) / div, digits)
    except (TypeError, ValueError):
        return None


def _why_not_docx(path: str) -> str:
    try:
        with open(path, "rb") as f:
            head = f.read(8)
    except OSError:
        head = b""
    if head.startswith(b"\xd0\xcf\x11\xe0"):
        return "файл .doc — старый двоичный формат Word: откройте его в Word и сохраните как .docx"
    if head.startswith(b"{\\rtf"):
        return "это RTF: откройте в Word и сохраните как .docx"
    if head.startswith(b"%PDF"):
        return "это PDF: нужен исходный документ Word (.docx) или текст Markdown (.md)"
    return "не документ Word (.docx): файл не открывается как архив"


def _fmt(ppr, rpr, theme: dict) -> dict:
    """The formatting written in w:pPr / w:rPr — only what is set."""
    out = {}
    if rpr is not None:
        fonts = rpr.find(_w("rFonts"))
        if fonts is not None:
            name = fonts.get(_w("ascii")) or fonts.get(_w("hAnsi")) or fonts.get(_w("cs"))
            if not name:
                ref = fonts.get(_w("asciiTheme")) or fonts.get(_w("hAnsiTheme")) or ""
                name = theme.get("major" if ref.startswith("major") else "minor") if ref else None
            if name:
                out["font"] = name
        size = _num(_val(rpr, "sz"), 2)
        if size:
            out["size_pt"] = size
        for key, tag in (("bold", "b"), ("italic", "i"), ("caps", "caps")):
            flag = _on(rpr, tag)
            if flag is not None:
                out[key] = flag
        color = _val(rpr, "color")
        if color and re.fullmatch(r"[0-9A-Fa-f]{6}", color):
            out["color"] = color.upper()
    if ppr is not None:
        jc = _val(ppr, "jc")
        if jc:
            out["align"] = {"start": "left", "end": "right", "distribute": "both", "justify": "both"}.get(jc, jc)
        sp = ppr.find(_w("spacing"))
        if sp is not None:
            for key, attr in (("before_pt", "before"), ("after_pt", "after")):
                v = _num(sp.get(_w(attr)), 20)
                if v is not None:
                    out[key] = v
            if sp.get(_w("line")):
                out["line"], out["line_rule"] = int(float(sp.get(_w("line")))), sp.get(_w("lineRule")) or "auto"
        ind = ppr.find(_w("ind"))
        if ind is not None:
            first, hang = _num(ind.get(_w("firstLine")), TWIPS_MM), _num(ind.get(_w("hanging")), TWIPS_MM)
            if first is not None or hang is not None:
                out["first_line_mm"] = first if first is not None else -hang
            left = _num(ind.get(_w("left")) or ind.get(_w("start")), TWIPS_MM)
            if left is not None:
                out["left_mm"] = left
        lvl = _val(ppr, "outlineLvl")
        if lvl is not None and lvl.isdigit():
            out["outline"] = int(lvl)
        num = ppr.find(_w("numPr"))
        if num is not None:
            out["num"] = (_val(num, "numId") or "", int(_val(num, "ilvl") or 0))
    return out


class _Styles:
    """word/styles.xml: the formatting of a style through its basedOn chain, the heading level of a style."""

    def __init__(self, root, theme: dict):
        self.theme, self.by_id, self.default, self._cache = theme, {}, None, {}
        self.base = {}
        if root is None:
            return
        defaults = root.find(_w("docDefaults"))
        if defaults is not None:
            rpr, ppr = defaults.find(f"{_w('rPrDefault')}/{RPR}"), defaults.find(f"{_w('pPrDefault')}/{PPR}")
            self.base = _fmt(ppr, rpr, theme)
        for st in root.findall(_w("style")):
            sid = st.get(_w("styleId")) or ""
            self.by_id[sid] = {"id": sid, "name": _val(st, "name") or sid, "based": _val(st, "basedOn"), "type": st.get(_w("type")),
                               "fmt": _fmt(st.find(PPR), st.find(RPR), theme)}
            if st.get(_w("type")) == "paragraph" and st.get(_w("default")) in ("1", "true") and self.default is None:
                self.default = sid

    def chain(self, sid) -> list:
        out = []
        while sid in self.by_id and len(out) < 20 and self.by_id[sid] not in out:
            out.append(self.by_id[sid])
            sid = self.by_id[sid]["based"]
        return out

    def props(self, sid) -> dict:
        if sid not in self._cache:
            merged = dict(self.base)
            for st in reversed(self.chain(sid)):
                merged.update(st["fmt"])
            self._cache[sid] = merged
        return self._cache[sid]

    def name(self, sid) -> str:
        return self.by_id[sid]["name"] if sid in self.by_id else (sid or "")

    def level(self, sid) -> int:
        """1..9 for a heading style (by name, by styleId, through basedOn, by the outline level), else 0."""
        for st in self.chain(sid):
            m = HEADING_NAME.match(st["name"].strip()) or HEADING_NAME.match(st["id"].strip())
            if m:
                return int(m.group(1))
        lvl = self.props(sid).get("outline")
        return lvl + 1 if lvl is not None and lvl < 9 else 0

    def find_heading(self, level: int):
        """The styleId of «heading N» / «Заголовок N» itself (not of a style based on it)."""
        for sid, st in self.by_id.items():
            m = HEADING_NAME.match(st["name"].strip()) or HEADING_NAME.match(sid.strip())
            if st["type"] == "paragraph" and m and int(m.group(1)) == level:
                return sid
        return None


class Docx:
    """A .docx read into blocks, with the page setup, the styles and the header / footer texts."""

    def __init__(self, path: str):
        self.path = str(path)
        try:
            z = zipfile.ZipFile(self.path)
        except zipfile.BadZipFile:
            raise DocError(_why_not_docx(self.path)) from None
        with z:
            names = set(z.namelist())
            main = "word/document.xml"
            for typ, target in self._rels(z, "").values():
                if typ.endswith("/officeDocument") and target in names:
                    main = target
            if main not in names:
                raise DocError("в архиве нет word/document.xml — это не .docx (файл .odt или .xlsx? сохраните как .docx)")
            rels = self._rels(z, main)
            by_type = {}
            for typ, target in rels.values():
                by_type.setdefault(typ.rsplit("/", 1)[-1], []).append(target)
            theme = {}
            troot = self._xml(z, (by_type.get("theme") or [""])[0])
            if troot is not None:
                for kind in ("major", "minor"):
                    latin = troot.find(f".//{{{A}}}{kind}Font/{{{A}}}latin")
                    if latin is not None and latin.get("typeface"):
                        theme[kind] = latin.get("typeface")
            self.styles = _Styles(self._xml(z, (by_type.get("styles") or ["word/styles.xml"])[0]), theme)
            self._lists = self._numbering(self._xml(z, (by_type.get("numbering") or ["word/numbering.xml"])[0]))
            root = self._xml(z, main)
            body = root.find(_w("body")) if root is not None else None
            if body is None:
                raise DocError("в документе нет текста (w:body)")
            self._fields, self.has_toc, self.auto_numbered = [], False, 0
            self.blocks = []
            self._body(body, self.blocks, False)
            sect = body.find(_w("sectPr"))
            if sect is None:
                found = body.findall(f".//{_w('sectPr')}")
                sect = found[-1] if found else None
            self.page = self._page(sect)
            self.headers, self.footers, self.page_numbers = [], [], False
            for kind, store in (("header", self.headers), ("footer", self.footers)):
                for target in by_type.get(kind, []):
                    part = self._xml(z, target)
                    if part is None:
                        continue
                    for p in part.iter(P):
                        text = "".join(t.text or "" for t in p.iter(T)).strip()
                        if text and text not in store:
                            store.append(text)
                    instr = " ".join((e.text or "") for e in part.iter(INSTR)) + " ".join(e.get(_w("instr"), "") for e in part.iter(FLD_SIMPLE))
                    if re.search(r"\bPAGE\b", instr):
                        self.page_numbers = True
        annotate(self.blocks)

    # ------------------------------------------------------------------ parts
    @staticmethod
    def _xml(z, name: str):
        if not name:
            return None
        try:
            info = z.getinfo(name)
        except KeyError:
            return None
        if info.file_size > MAX_PART:
            raise DocError(f"{name}: часть документа больше {MAX_PART // (1024 * 1024)} МБ — файл не читается")
        data = z.read(name)
        if b"<!DOCTYPE" in data[:4096] or b"<!ENTITY" in data[:4096]:
            raise DocError(f"{name}: в XML есть DOCTYPE — такой файл не читается")
        try:
            return ET.fromstring(data)
        except ET.ParseError as exc:
            raise DocError(f"{name}: повреждённый XML ({exc})") from None

    @classmethod
    def _rels(cls, z, part: str) -> dict:
        """{rId: (type, path in the archive)} of a part ("" — the package itself)."""
        folder, base = posixpath.split(part)
        root = cls._xml(z, posixpath.join(folder, "_rels", base + ".rels"))
        out = {}
        for rel in (root if root is not None else []):
            if rel.get("TargetMode") == "External":
                continue
            target = rel.get("Target") or ""
            full = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join(folder, target))
            out[rel.get("Id")] = (rel.get("Type") or "", full)
        return out

    @staticmethod
    def _numbering(root) -> dict:
        """{(numId, ilvl): "bullet" | "number" | None}."""
        out = {}
        if root is None:
            return out
        abstract = {}
        for a in root.findall(_w("abstractNum")):
            abstract[a.get(_w("abstractNumId"))] = {int(lvl.get(_w("ilvl")) or 0): _val(lvl, "numFmt") for lvl in a.findall(_w("lvl"))}
        for n in root.findall(_w("num")):
            for ilvl, fmt in abstract.get(_val(n, "abstractNumId"), {}).items():
                out[(n.get(_w("numId")), ilvl)] = None if fmt == "none" else "bullet" if fmt in ("bullet", None) else "number"
        return out

    @staticmethod
    def _page(sect) -> dict:
        size = sect.find(_w("pgSz")) if sect is not None else None
        mar = sect.find(_w("pgMar")) if sect is not None else None
        if size is None:
            return {}
        w, h = _num(size.get(_w("w")), TWIPS_MM), _num(size.get(_w("h")), TWIPS_MM)
        page = {"width_mm": w, "height_mm": h,
                "orientation": "landscape" if size.get(_w("orient")) == "landscape" or (w and h and w > h) else "portrait"}
        if mar is not None:
            page["margins_mm"] = {k: _num(mar.get(_w(k)), TWIPS_MM) for k in ("top", "bottom", "left", "right", "header", "footer")
                                  if _num(mar.get(_w(k)), TWIPS_MM) is not None}
        return page

    # ------------------------------------------------------------------ body
    def _body(self, parent, out: list, in_toc: bool) -> None:
        for el in parent:
            if el.tag == P:
                out.append(self._para(el, in_toc))
            elif el.tag == TBL:
                out.append(self._table(el, in_toc))
            elif el.tag == SDT:
                gallery = el.find(f"{_w('sdtPr')}/{_w('docPartObj')}/{_w('docPartGallery')}")
                toc = in_toc or (gallery is not None and "table of contents" in (gallery.get(_w("val")) or "").lower())
                content = el.find(_w("sdtContent"))
                if content is not None:
                    self._body(content, out, toc)
            elif el.tag in (_w("customXml"), _w("ins"), _w("smartTag")):
                self._body(el, out, in_toc)

    def _in_toc_field(self) -> bool:
        return any(f["instr"].strip().upper().startswith("TOC") for f in self._fields)

    def _para(self, p, in_toc: bool = False) -> dict:
        ppr = p.find(PPR)
        sid = _val(ppr, "pStyle") or self.styles.default
        props = dict(self.styles.props(sid))
        props.update(_fmt(ppr, None, self.styles.theme))
        text, runs, state = [], [], {"pics": 0}
        toc = in_toc or self._in_toc_field() or bool(TOC_NAME.match(self.styles.name(sid).strip()))

        def walk(el):
            nonlocal toc
            for ch in el:
                tag = ch.tag
                if tag == T:
                    text.append(ch.text or "")
                elif tag == TAB:
                    text.append("\t")
                elif tag in (BR, CR):
                    if ch.get(_w("type")) not in ("page", "column"):
                        text.append("\n")
                elif tag == _w("noBreakHyphen"):
                    text.append("-")
                elif tag == DRAWING:
                    if any(e.tag.endswith(("}blip", "}chart", "}relIds")) for e in ch.iter()):
                        state["pics"] += 1
                elif tag in (PICT, OBJECT):
                    if any(e.tag.endswith("}imagedata") for e in ch.iter()):
                        state["pics"] += 1
                elif tag == FLD:
                    kind = ch.get(_w("fldCharType"))
                    if kind == "begin":
                        self._fields.append({"instr": ""})
                    elif kind == "end" and self._fields:
                        self._fields.pop()
                    elif kind == "separate" and self._in_toc_field():
                        toc = True
                elif tag == INSTR:
                    if self._fields:
                        self._fields[-1]["instr"] += ch.text or ""
                elif tag == FLD_SIMPLE:
                    if (ch.get(_w("instr")) or "").strip().upper().startswith("TOC"):
                        toc = True
                    walk(ch)
                elif tag == RUN:
                    before = sum(len(x) for x in text)
                    walk(ch)
                    n = len("".join(text)[before:].strip())
                    if n:
                        f = _fmt(None, ch.find(RPR), self.styles.theme)
                        runs.append((f.get("font", props.get("font")), f.get("size_pt", props.get("size_pt")),
                                     f.get("bold", props.get("bold", False)), f.get("italic", props.get("italic", False)), n))
                elif tag not in (PPR, RPR, DEL, MOVE_FROM, FALLBACK):
                    walk(ch)

        walk(p)
        toc = toc or self._in_toc_field()
        if toc:
            self.has_toc = True
        body = re.sub(r"[ \t]*\n[ \t]*", "\n", "".join(text)).strip()
        level = 0 if toc else self.styles.level(sid)
        block = {"t": "p", "text": body, "pics": state["pics"], "style": self.styles.name(sid), "fmt": props, "runs": runs}
        if toc:
            block["toc"] = True
        if level and body:
            m = NUMBER.match(body)
            block.update({"t": "h", "level": level, "number": m.group(1) if m else "", "title": m.group(2).strip() if m else body})
            if not m and props.get("num") and props["num"][0] not in ("", "0"):
                self.auto_numbered += 1
            return block
        num = props.get("num")
        if num and num[0] not in ("", "0") and body:
            kind = self._lists.get(num, "bullet")
            if kind:
                block["list"], block["ilvl"] = kind, min(num[1], 2)
        if CODE_NAME.match(self.styles.name(sid).strip()):
            block["code"] = True
            block["text"] = "".join(text).rstrip()
        return block

    def _table(self, tbl, in_toc: bool = False) -> dict:
        rows, pics = [], 0

        def own_rows(el):                                       # rows of this table, not of the tables nested in cells
            for ch in el:
                if ch.tag == TR:
                    yield ch
                elif ch.tag in (SDT, _w("sdtContent"), _w("customXml"), _w("ins")):
                    yield from own_rows(ch)

        for tr in own_rows(tbl):
            row = []
            for tc in tr.findall(TC):
                parts = []
                for p in tc.iter(P):
                    b = self._para(p, in_toc)
                    pics += b["pics"]
                    if b["text"]:
                        parts.append(b["text"])
                row.append("\n".join(parts))
                span = _val(tc.find(_w("tcPr")), "gridSpan")
                row += [""] * (int(span) - 1 if span and span.isdigit() else 0)
            if row:
                rows.append(row)
        return {"t": "tbl", "rows": rows, "pics": pics}


# ---------------------------------------------------------------------- blocks
def _heading(level: int, text: str) -> dict:
    m = NUMBER.match(text)
    return {"t": "h", "level": level, "text": text, "number": m.group(1) if m else "", "title": m.group(2).strip() if m else text}


def note_text(text: str, first: bool = True) -> str:
    """A blockquote is a note: «Примечание — …» (the word is not repeated when the text already starts with it)."""
    return text if not first or re.match(r"(?:примечани|внимание|note\b)", text, re.I) else "Примечание — " + text


def md_blocks(text: str) -> list:
    """Markdown → the same blocks a .docx gives (plain text, no inline markup)."""
    out = []
    for b in docmd.parse(text)[0]:
        t = b["t"]
        if t == "h":
            out.append(_heading(b["level"], docmd.plain(b["text"])))
        elif t == "p":
            out.append({"t": "p", "text": docmd.plain(b["text"]), "pics": 0})
        elif t == "note":
            out.append({"t": "p", "text": note_text(docmd.plain(b["text"]), b["first"]), "pics": 0})
        elif t == "list":
            out += [{"t": "p", "text": docmd.plain(i["text"]), "pics": 0, "list": i["kind"], "ilvl": i["level"]} for i in b["items"]]
        elif t == "code":
            out += [{"t": "p", "text": line, "pics": 0, "code": True} for line in b["lines"]]
        elif t == "img":
            out.append({"t": "p", "text": "", "pics": 1, "caption": b["caption"]})
        elif t == "table":
            rows = [[docmd.plain(c) for c in r] for r in [b["header"]] + b["rows"]]
            out.append({"t": "tbl", "rows": rows, "pics": 0, "caption": b["caption"]})
    annotate(out)
    return out


def annotate(blocks: list) -> None:
    """Find the captions: «Рисунок …» right after (or before) a picture, «Таблица N …» right before a table.
    A one-column table before the first heading is the layout of a title page: it becomes paragraphs."""
    first = next((n for n, b in enumerate(blocks) if b["t"] == "h"), len(blocks))
    for n in range(first - 1, -1, -1):
        b = blocks[n]
        if b["t"] == "tbl" and b["rows"] and max(len(r) for r in b["rows"]) == 1:
            lines = [x for r in b["rows"] for x in r[0].split("\n") if x.strip()]
            blocks[n:n + 1] = [{"t": "p", "text": x.strip(), "pics": 0} for x in lines]

    def near(n: int, step: int):
        n += step
        while 0 <= n < len(blocks) and blocks[n]["t"] == "p" and not blocks[n]["text"] and not blocks[n].get("pics"):
            n += step
        return blocks[n] if 0 <= n < len(blocks) and blocks[n]["t"] == "p" and not blocks[n].get("cap") else None

    for n, b in enumerate(blocks):
        if b["t"] == "p" and b.get("pics") and "caption" not in b:
            if PIC_CAPTION.match(b["text"]):
                b["caption"], b["cap"] = b["text"], "pic"
                continue
            for step in (1, -1):
                other = near(n, step)
                if other is not None and not other.get("pics") and PIC_CAPTION.match(other["text"]):
                    b["caption"], other["cap"] = other["text"], "pic"
                    break
            else:
                b["caption"] = ""
        elif b["t"] == "tbl" and "caption" not in b:
            other = near(n, -1)
            if other is not None and TBL_CAPTION.match(other["text"]):
                b["caption"], other["cap"] = other["text"], "tbl"
            else:
                b["caption"] = ""


def read_text(path: str) -> str:
    with open(path, "rb") as f:
        data = f.read()
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1251", errors="replace")


def load(path: str):
    """(blocks, Docx or None) of a .docx or a Markdown file."""
    path = str(path)
    ext = os.path.splitext(path)[1].lower()
    if ext in (".md", ".markdown", ".txt"):
        return md_blocks(read_text(path)), None
    if ext in (".doc", ".rtf", ".pdf", ".odt"):
        with open(path, "rb"):
            pass
        raise DocError(_why_not_docx(path) if ext != ".odt" else "файл .odt: сохраните его как .docx")
    with open(path, "rb") as f:
        head = f.read(4)
    if head.startswith(b"PK"):
        doc = Docx(path)
        return doc.blocks, doc
    if ext in (".docx", ".docm", ".dotx"):
        raise DocError(_why_not_docx(path))
    raise DocError("нужен документ Word (.docx) или текст Markdown (.md)")


def sections(blocks: list) -> list:
    """[{heading or None, blocks}] — the first one is everything before the first heading."""
    out = [{"h": None, "blocks": []}]
    for b in blocks:
        if b["t"] == "h":
            out.append({"h": b, "blocks": []})
        else:
            out[-1]["blocks"].append(b)
    return out


def stats(items: list) -> dict:
    """What a section holds: paragraphs, lists, tables, pictures, code lines."""
    st = {"paragraphs": 0, "lists": 0, "list_items": 0, "tables": [], "pictures": 0, "captions": [], "code_lines": 0}
    in_list = False
    for b in items:
        is_item = b["t"] == "p" and bool(b.get("list")) and not b.get("toc")
        if is_item and not in_list:
            st["lists"] += 1
        in_list = is_item
        if b["t"] == "tbl":
            rows = b["rows"]
            st["tables"].append({"rows": len(rows), "cols": max((len(r) for r in rows), default=0),
                                 "header": [c.replace("\n", " ")[:60] for c in rows[0]] if rows else [], "caption": b.get("caption", "")})
            st["pictures"] += b.get("pics", 0)
            continue
        if b.get("toc"):
            continue
        if b.get("pics"):
            st["pictures"] += b["pics"]
            if b.get("caption"):
                st["captions"].append(b["caption"])
        if b.get("code"):
            st["code_lines"] += 1
        elif is_item:
            st["list_items"] += 1
        elif b["text"] and not b.get("cap"):
            st["paragraphs"] += 1
    return st


def _pl(n: int, one: str, few: str, many: str) -> str:
    word = one if n % 10 == 1 and n % 100 != 11 else few if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else many
    return f"{n} {word}"


def hint(st: dict) -> str:
    """«3 абзаца, таблица 5×4 (Параметр | Значение | …), 2 рисунка»."""
    parts = []
    if st["paragraphs"]:
        parts.append(_pl(st["paragraphs"], "абзац", "абзаца", "абзацев"))
    if st["lists"]:
        parts.append(("список" if st["lists"] == 1 else _pl(st["lists"], "список", "списка", "списков")) + " из "
                     + _pl(st["list_items"], "пункта", "пунктов", "пунктов"))
    for t in st["tables"]:
        head = " | ".join(c[:30] for c in t["header"][:4]) + (" | …" if len(t["header"]) > 4 else "")
        parts.append(f"таблица {t['rows']}×{t['cols']}" + (f" ({head})" if head.strip(" |") else ""))
    if st["pictures"]:
        parts.append(_pl(st["pictures"], "рисунок", "рисунка", "рисунков"))
    if st["code_lines"]:
        parts.append("листинг " + _pl(st["code_lines"], "строка", "строки", "строк"))
    return ", ".join(parts)


def _dominant(pairs):
    """The value with the biggest total weight among (value, weight); None when there is none."""
    c = Counter()
    for value, weight in pairs:
        if value is not None:
            c[value] += weight
    return c.most_common(1)[0][0] if c else None


def _line(fmt: dict):
    if not fmt.get("line"):
        return None
    return round(fmt["line"] / 240, 2) if fmt.get("line_rule", "auto") == "auto" else f"{fmt.get('line_rule')} {fmt['line'] / 20:g} pt"


def _public_fmt(fmt: dict) -> dict:
    out = {k: fmt[k] for k in ("font", "size_pt", "bold", "italic", "caps", "color", "align", "first_line_mm", "before_pt", "after_pt")
           if fmt.get(k) not in (None, False)}
    if _line(fmt) is not None:
        out["line_spacing"] = _line(fmt)
    return out


def effective(blocks: list) -> dict:
    """The formatting really used in the text (direct formatting counts): body and heading1..4, weighted by length."""
    groups = {}
    for b in blocks:
        if "fmt" not in b or not b["text"] or b.get("toc") or b.get("cap") or b.get("code"):
            continue
        key = f"heading{b['level']}" if b["t"] == "h" else None if b.get("list") else "body"
        if key:
            groups.setdefault(key, []).append(b)
    out = {}
    for key, items in groups.items():
        fmt = {}
        for name, idx in (("font", 0), ("size_pt", 1), ("bold", 2), ("italic", 3)):
            fmt[name] = _dominant((r[idx], r[4]) for b in items for r in b["runs"])
        for name in ("align", "line", "line_rule", "first_line_mm", "before_pt", "after_pt", "color", "caps"):
            fmt[name] = _dominant((b["fmt"].get(name), max(len(b["text"]), 1)) for b in items)
        out[key] = {k: v for k, v in fmt.items() if v is not None}
    return out


def style_of(path: str) -> dict:
    """What an example .docx gives the writer: {"page", "body", "headings": {1: …}} — only what the example sets."""
    doc = Docx(path)
    eff = effective(doc.blocks)
    styles = doc.styles
    body = dict(styles.props(styles.default))
    body.update(eff.get("body", {}))
    heads = {}
    for level in (1, 2, 3, 4):
        sid = styles.find_heading(level)
        fmt = dict(styles.props(sid)) if sid else {}
        fmt.update(eff.get(f"heading{level}", {}))
        if fmt and (sid or f"heading{level}" in eff):
            heads[level] = fmt
    return {"page": doc.page, "body": body, "headings": heads}


# ---------------------------------------------------------------------- outline
def outline(path: str) -> dict:
    blocks, doc = load(path)
    secs = sections(blocks)
    heads, md, totals = [], [], Counter()
    title_page = [b["text"] for b in secs[0]["blocks"] if b["t"] == "p" and b["text"] and not b.get("toc")][:40]
    if title_page:
        md.append("<!-- титульный лист образца: " + " / ".join(x.replace("\n", " ")[:80] for x in title_page[:12]).replace("--", "—") + " -->\n")
    for s in secs:
        st = stats(s["blocks"])
        for key in ("paragraphs", "list_items", "pictures", "code_lines"):
            totals[key] += st[key]
        totals["tables"] += len(st["tables"])
        if s["h"] is None:
            continue
        h = s["h"]
        heads.append({"level": h["level"], "number": h["number"], "title": h["title"], "text": h["text"], **st})
        md.append("#" * min(h["level"], 6) + " " + h["text"].replace("\n", " "))
        if hint(st):
            md.append(f"[заполнить: {hint(st)}]")
        md.append("")
    totals["headings"] = len(heads)
    out = {"file": str(path), "format": "docx" if doc else "md", "totals": dict(totals), "title_page": title_page,
           "headings": heads[:3000]}
    if doc:
        typed = sum(1 for h in heads if h["number"])
        out["heading_numbering"] = "typed" if typed * 2 > len(heads) else "auto" if doc.auto_numbered else "none"
        out["toc"] = doc.has_toc
        out["page"] = doc.page
        styles = {}
        for key, sid in [("normal", doc.styles.default)] + [(f"heading{n}", doc.styles.find_heading(n)) for n in (1, 2, 3)]:
            if sid in doc.styles.by_id:
                styles[key] = {"style_id": sid, "style_name": doc.styles.name(sid), **_public_fmt(doc.styles.props(sid))}
        out["styles"] = styles
        out["effective"] = {k: _public_fmt(v) for k, v in sorted(effective(blocks).items())}
        out["header"], out["footer"], out["page_numbers"] = doc.headers[:20], doc.footers[:20], doc.page_numbers
    out["outline_md"] = "\n".join(md).rstrip() + "\n"
    return out


# ---------------------------------------------------------------------- text
def norm(text: str) -> str:
    """A heading for comparing: no number, lower case, ё → е, no punctuation."""
    m = NUMBER.match(text.strip())
    s = (m.group(2) if m else text).lower().replace("ё", "е")
    return " ".join(re.sub(r"[^\w\s]", " ", s).split())


def _pick(blocks: list, section: str) -> list:
    want, want_n = section.strip().rstrip("."), norm(section)
    heads = [(n, b) for n, b in enumerate(blocks) if b["t"] == "h"]
    hit = next((x for x in heads if x[1]["number"] == want or x[1]["text"].strip() == section.strip()), None) \
        or next((x for x in heads if want_n and norm(x[1]["text"]) == want_n), None) \
        or next((x for x in heads if want_n and want_n in norm(x[1]["text"])), None)
    if hit is None:
        names = "; ".join(b["text"][:60] for _, b in heads[:40])
        raise DocError(f"нет раздела «{section}». Разделы: {names or 'заголовков нет'}")
    start, level = hit[0], hit[1]["level"]
    end = next((n for n, b in heads if n > start and b["level"] <= level), len(blocks))
    return blocks[start:end]


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", "<br>")


def to_markdown(blocks: list) -> str:
    out, counters, in_code = [], {}, False
    for b in blocks:
        code = b["t"] == "p" and bool(b.get("code"))
        if code != in_code:
            out += ["```"] if code else ["```", ""]
            in_code = code
        if code:
            out.append(b["text"])
            continue
        if b["t"] == "p" and b.get("list") and b["text"]:
            lvl = b.get("ilvl", 0)
            for deeper in [k for k in counters if k > lvl]:
                del counters[deeper]
            count = counters[lvl][1] + 1 if lvl in counters and counters[lvl][0] == b["list"] else 1
            counters[lvl] = (b["list"], count)
            mark = f"{count}." if b["list"] == "number" else "-"
            out.append("   " * lvl + f"{mark} " + b["text"].replace("\n", " "))
            continue
        if counters:
            counters.clear()
            out.append("")
        if b["t"] == "h":
            out += ["#" * min(b["level"], 6) + " " + b["text"].replace("\n", " "), ""]
        elif b["t"] == "tbl":
            rows = b["rows"]
            cols = max((len(r) for r in rows), default=0)
            if not cols:
                continue
            for n, r in enumerate(rows):
                out.append("| " + " | ".join(_cell(c) for c in r + [""] * (cols - len(r))) + " |")
                if n == 0:
                    out.append("|" + "---|" * cols)
            out.append("")
        elif b.get("toc") or (b.get("cap") == "pic" and not b.get("pics")):
            continue
        else:
            if b["text"] and not (b.get("pics") and b.get("caption") == b["text"]):
                out += [b["text"], ""]
            if b.get("pics"):
                out += ([f"[рисунок: {b['caption']}]" if b.get("caption") else "[рисунок]"] * 1) + [""]
    if in_code:
        out.append("```")
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip() + "\n"


def text(path: str, section: str = "", limit: int = 60000) -> dict:
    blocks, doc = load(path)
    if section:
        blocks = _pick(blocks, section)
    body = to_markdown(blocks) if doc or section else read_text(str(path))
    limit = max(int(limit or 60000), 200)
    cut = len(body) > limit
    return {"text": body[:limit] + ("\n\n[… текст обрезан: задайте section или увеличьте limit]" if cut else ""),
            "chars": len(body), "truncated": cut}


# ---------------------------------------------------------------------- check
def check(path: str, outline_from: str = "") -> dict:
    blocks, _ = load(path)
    issues = []

    def add(level, where, msg):
        issues.append({"level": level, "where": where, "text": msg})

    secs = sections(blocks)
    heads = [s["h"] for s in secs[1:]]
    if not any(b["t"] == "tbl" or b["text"] for b in blocks):
        add("error", "документ", "документ пуст")
    elif not heads:
        add("warning", "документ", "нет ни одного заголовка: разделы не размечены стилями заголовков")
    prev, last = 0, {}
    for n, s in enumerate(secs[1:], 1):
        h = s["h"]
        where = f"раздел «{h['text'][:80]}»"
        st = stats(s["blocks"])
        nxt = secs[n + 1]["h"] if n + 1 < len(secs) else None
        if not (st["paragraphs"] or st["list_items"] or st["tables"] or st["pictures"] or st["code_lines"]) \
                and not (nxt and nxt["level"] > h["level"]):
            add("warning", where, "раздел пуст: под заголовком нет текста")
        if prev and h["level"] > prev + 1:
            add("warning", where, f"уровень заголовка перескакивает с {prev} на {h['level']}")
        elif not prev and h["level"] > 1:
            add("info", where, f"первый заголовок — уровня {h['level']}, а не 1")
        prev = h["level"]
        if h["number"]:
            parts = tuple(int(x) for x in h["number"].split("."))
            if len(parts) != h["level"]:
                add("warning", where, f"номер {h['number']} не соответствует уровню заголовка {h['level']}")
            else:
                parent, before = parts[:-1], last.get(parts[:-1])
                if parent and parent not in last.get("seen", set()):
                    add("warning", where, f"номер {h['number']}: раздела {'.'.join(map(str, parent))} перед ним нет")
                elif before is not None and parts[-1] != before + 1:
                    add("warning", where, f"номера не по порядку: после {'.'.join(map(str, parent + (before,)))} идёт {h['number']}")
                elif before is None and parts[-1] > 1:
                    add("warning", where, f"нумерация начинается с {h['number']}")
                last[parent] = parts[-1]
                last.setdefault("seen", set()).add(parts)
    for s in secs:
        where = f"раздел «{s['h']['text'][:80]}»" if s["h"] else "начало документа"
        if s["h"]:
            for m in PLACEHOLDER.finditer(s["h"]["text"]):
                add("warning", where, f"в заголовке осталась заготовка «{m.group(0)}»")
        for b in s["blocks"]:
            cells = [c for r in b["rows"] for c in r] if b["t"] == "tbl" else [b["text"]]
            for cell in cells:
                for m in list(PLACEHOLDER.finditer(cell))[:5]:
                    a, z = max(m.start() - 30, 0), min(m.end() + 30, len(cell))
                    add("warning", where, f"осталась заготовка «{m.group(0)}»: …{cell[a:z].replace(chr(10), ' ')}…")
            if b["t"] == "tbl" and s["h"] and not b.get("caption") and b["rows"]:
                head = " | ".join(c[:20] for c in b["rows"][0][:3])
                add("warning", where, f"таблица без подписи «Таблица N — …» ({head})")
            if b["t"] == "p" and b.get("pics") and not b.get("caption"):
                add("warning", where, "рисунок без подписи «Рисунок N — …»")
    result = {}
    if outline_from:
        sample = [b for b in load(outline_from)[0] if b["t"] == "h"]
        have = {norm(h["text"]) for h in heads}
        want = {norm(h["text"]) for h in sample}
        missing = [h["text"] for h in sample if norm(h["text"]) not in have]
        extra = [h["text"] for h in heads if norm(h["text"]) not in want]
        for name in missing:
            add("warning", "образец", f"нет раздела образца «{name[:100]}»")
        for name in extra:
            add("info", f"раздел «{name[:80]}»", "такого раздела нет в образце")
        result = {"missing": missing, "extra": extra}
    summary = {k: sum(1 for i in issues if i["level"] == k) for k in ("error", "warning", "info")}
    totals = Counter()
    for s in secs:
        st = stats(s["blocks"])
        totals["tables"] += len(st["tables"])
        totals["pictures"] += st["pictures"]
    return {"summary": summary, "totals": {"headings": len(heads), "tables": totals["tables"], "pictures": totals["pictures"]},
            **result, "issues": issues[:2000]}
