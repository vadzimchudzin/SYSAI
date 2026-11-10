"""doc-kit: the Markdown the agent writes a document in — parsed into blocks for the Word exporter and the checks.

Blocks (dicts, key "t"): h (level, text), p (text), list (items: kind, level, text, start), table (header, rows, aligns,
caption), img (caption, path, title), code (lines), note (text, first), break. Inline markup stays in "text" and is
split into runs by inline()."""
import re

FENCE = re.compile(r"^\s{0,3}(```+|~~~+)\s*[\w+#.-]*\s*$")
HEAD = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
RULE = re.compile(r"^\s{0,3}([-*_])(?:\s*\1){2,}\s*$")
ITEM = re.compile(r"^(\s*)([-*+•–]|\d{1,4}[.)])\s+(.*)$")
IMAGE = re.compile(r'^\s*!\[(.*?)\]\(\s*(<[^>]*>|[^"]*?)\s*(?:"([^"]*)")?\s*\)\s*$')
TABLE_SEP = re.compile(r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$")
TABLE_CAPTION = re.compile(r"^(?:Таблица|Table)\s*:\s*(.+)$", re.I)
CELL_SPLIT = re.compile(r"(?<!\\)\|")
UNNUMBERED = re.compile(r"\s*\{\s*(?:-|\.unnumbered)\s*\}\s*$")
INLINE = re.compile(
    r"(\\[\\`*_{}\[\]()#+\-.!|>~<])"                               # 1 an escaped character
    r"|(`+)(.+?)\2"                                                 # 2, 3 code
    r"|(\*\*\*)(?=\S)(.+?)(?<=\S)\*\*\*"                            # 4, 5 bold italic
    r"|(\*\*)(?=\S)(.+?)(?<=\S)\*\*"                                # 6, 7 bold
    r"|(?<![\w*])(\*)(?=[^\s*])(.+?)(?<=[^\s*])\*(?![\w*])"         # 8, 9 italic
    r"|\[([^\]]+)\]\(([^)\s]+)\)"                                   # 10, 11 a link
    r"|(<br\s*/?>)", re.I | re.S)                                   # 12 a line break


def inline(text: str, bold: bool = False, italic: bool = False) -> list:
    """«a **b** `c`» → [(text, bold, italic, code)]; "\\n" inside a text is a line break."""
    out, pos = [], 0
    for m in INLINE.finditer(text):
        if m.start() > pos:
            out.append((text[pos:m.start()], bold, italic, False))
        pos = m.end()
        if m.group(1):
            out.append((m.group(1)[1], bold, italic, False))
        elif m.group(2):
            out.append((m.group(3).strip() or m.group(3), bold, italic, True))
        elif m.group(4):
            out += inline(m.group(5), True, True)
        elif m.group(6):
            out += inline(m.group(7), True, italic)
        elif m.group(8):
            out += inline(m.group(9), bold, True)
        elif m.group(10):
            label, url = m.group(10), m.group(11)
            out += inline(label, bold, italic)
            if url not in label and not url.startswith("#"):
                out.append((f" ({url})", bold, italic, False))
        else:
            out.append(("\n", bold, italic, False))
    if pos < len(text):
        out.append((text[pos:], bold, italic, False))
    return [r for r in out if r[0]]


def plain(text: str) -> str:
    """The text without the inline markup."""
    return "".join(r[0] for r in inline(text))


def _cells(line: str) -> list:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    return [c.strip().replace("\\|", "|") for c in CELL_SPLIT.split(s)]


def _join(lines: list) -> str:
    """Paragraph lines → one text: a line ending with two spaces or a backslash breaks the line."""
    out = []
    for n, raw in enumerate(lines):
        last = n == len(lines) - 1
        hard = not last and (raw.endswith("  ") or raw.rstrip().endswith("\\"))
        s = raw.strip()
        if hard and s.endswith("\\"):
            s = s[:-1].rstrip()
        out.append(s + ("" if last else "\n" if hard else " "))
    return "".join(out)


def _list(lines: list, i: int) -> tuple:
    """The list starting at line i → (block, next line)."""
    items, indents = [], []
    n = len(lines)
    while i < n:
        line = lines[i].expandtabs(4)
        m = ITEM.match(line)
        if m and not RULE.match(line):
            indent = len(m.group(1))
            while indents and indent < indents[-1]:
                indents.pop()
            if not indents or indent > indents[-1]:
                indents.append(indent)
            marker = m.group(2)
            numbered = marker[0].isdigit()
            items.append({"kind": "number" if numbered else "bullet", "level": min(len(indents) - 1, 2),
                          "text": m.group(3).strip(), "start": int(marker[:-1]) if numbered else 1})
            i += 1
        elif not line.strip():
            j = i
            while j < n and not lines[j].strip():
                j += 1
            nxt = lines[j].expandtabs(4) if j < n else ""
            if j < n and (ITEM.match(nxt) and not RULE.match(nxt) or nxt.startswith("  ")) and not FENCE.match(nxt):
                i = j
            else:
                break
        elif line.startswith("  ") or not (HEAD.match(line) or FENCE.match(line) or RULE.match(line) or line.lstrip().startswith((">", "|"))
                                           or IMAGE.match(line) or not lines[i - 1].strip()):
            items[-1]["text"] += " " + line.strip()             # a continuation line of the item
            i += 1
        else:
            break
    return {"t": "list", "items": items}, i


def parse(text: str) -> tuple:
    """Markdown → (blocks, warnings)."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("﻿").split("\n")
    blocks, warnings, para = [], [], []
    n, i = len(lines), 0

    def flush():
        if not para:
            return
        body = _join(para)
        para.clear()
        m = TABLE_CAPTION.match(body) if "\n" not in body else None
        if m:
            blocks.append({"t": "tcap", "text": m.group(1).strip(), "raw": body})
            return
        if "![" in body and re.search(r"!\[[^\]]*\]\([^)]*\)", body):
            warnings.append(f"картинка внутри абзаца не вставляется — вынесите её на отдельную строку: «{body[:60]}»")
        blocks.append({"t": "p", "text": body})

    while i < n:
        line = lines[i]
        fence = FENCE.match(line)
        if fence:
            flush()
            mark, code, j = fence.group(1), [], i + 1
            while j < n and not (lines[j].strip().startswith(mark[:3]) and not lines[j].strip().strip(mark[0])):
                code.append(lines[j].rstrip())
                j += 1
            if j >= n:
                warnings.append(f"блок кода со строки {i + 1} не закрыт (нет закрывающих ```)")
            blocks.append({"t": "code", "lines": code})
            i = j + 1
            continue
        s = line.strip()
        if not s:
            flush()
            i += 1
            continue
        if s.startswith("<!--"):
            flush()
            while i < n and "-->" not in lines[i]:
                i += 1
            i += 1
            continue
        m = HEAD.match(line)
        if m:
            flush()
            level, title = len(m.group(1)), m.group(2).strip()
            if level > 4:
                warnings.append(f"заголовок уровня {level} «{title[:60]}» записан как уровень 4")
            unnumbered = bool(UNNUMBERED.search(title))
            blocks.append({"t": "h", "level": min(level, 4), "text": UNNUMBERED.sub("", title), "unnumbered": unnumbered})
            i += 1
            continue
        if RULE.match(line):
            flush()
            blocks.append({"t": "break"})
            i += 1
            continue
        if "|" in line and i + 1 < n and "-" in lines[i + 1] and TABLE_SEP.match(lines[i + 1]):
            flush()
            header = _cells(line)
            aligns = ["center" if c.startswith(":") and c.endswith(":") else "right" if c.endswith(":") else "left"
                      for c in _cells(lines[i + 1])]
            rows, j = [], i + 2
            while j < n and lines[j].strip() and "|" in lines[j]:
                rows.append(_cells(lines[j]))
                j += 1
            caption = blocks.pop()["text"] if blocks and blocks[-1]["t"] == "tcap" else ""
            blocks.append({"t": "table", "header": header, "rows": rows, "aligns": aligns, "caption": caption})
            i = j
            continue
        m = IMAGE.match(line)
        if m:
            flush()
            path = m.group(2).strip()
            if path.startswith("<") and path.endswith(">"):
                path = path[1:-1]
            blocks.append({"t": "img", "caption": plain(m.group(1)).strip(), "path": path.strip(), "title": m.group(3) or ""})
            i += 1
            continue
        if s.startswith(">"):
            flush()
            quote = []
            while i < n and lines[i].strip().startswith(">"):
                quote.append(re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            first, part = True, []
            for q in quote + [""]:
                if q.strip():
                    part.append(q)
                elif part:
                    blocks.append({"t": "note", "text": _join(part), "first": first})
                    first, part = False, []
            continue
        if ITEM.match(line.expandtabs(4)) and (not para or ITEM.match(line.expandtabs(4)).group(2) in ("-", "*", "+", "•", "–", "1.", "1)")):
            flush()
            block, i = _list(lines, i)
            blocks.append(block)
            continue
        para.append(line)
        i += 1
    flush()
    for b in blocks:                                            # «Таблица: …» with no table after it is a paragraph
        if b["t"] == "tcap":
            warnings.append(f"после строки «{b['raw'][:60]}» нет таблицы — оставлена абзацем")
            b.update({"t": "p", "text": b["raw"]})
    return blocks, warnings
