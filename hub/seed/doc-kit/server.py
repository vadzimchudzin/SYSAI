"""MCP-сервер doc-kit: читает образец документа (.docx) и собирает новый документ Word из текста Markdown.

Запускается синхронизатором на ПК (sysai-agent mcp doc-kit) и говорит по stdio построчным JSON-RPC 2.0. Доступы
(папки на чтение/запись) проверяет сам SYSAI по подписанным правилам до вызова — здесь только работа с файлами.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dockit  # noqa: E402
import docxout  # noqa: E402

PROTOCOL = "2025-06-18"
VERSION = "1.1.0"
FILE = {"type": "string", "description": r"Документ: .docx (или .md), например C:\Projects\line\docs\Руководство.docx"}
INSTRUCTIONS = (
    "Документация проекта (руководство оператора, технический паспорт) в Word по образцу. Образец .docx: doc_outline — "
    "разделы, что в каждом, титульный лист, поля, шрифты и заготовка outline_md; doc_text — как образец написан. Новый "
    "документ пишите в Markdown по заготовке (картинки — ![подпись](файл.png \"width=120mm\"), подпись таблицы — строка "
    "«Таблица: …» перед ней), собирайте doc_export (style_from — образец, title — титульный лист, toc — содержание) и "
    "проверяйте doc_check (outline_from — образец). Файл .doc не читается: просите сохранить как .docx. Содержание "
    "заполняет Word при открытии."
)


def _ro(name, title, description, props, required=("file",)):
    return {"name": name, "title": title, "description": description,
            "inputSchema": {"type": "object", "properties": props, "required": list(required), "additionalProperties": False},
            "annotations": {"readOnlyHint": True, "openWorldHint": False}}


def _rw(name, title, description, props, required):
    t = _ro(name, title, description, props, required)
    t["annotations"] = {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False}
    return t


TOOLS = [
    _ro("doc_outline", "Строение образца",
        "Прочитать образец документа (.docx или .md): дерево заголовков (уровень, номер, текст) и что в каждом разделе — "
        "сколько абзацев, списки, таблицы (размер и шапка), рисунки и подписи; тексты титульного листа; размер страницы и "
        "поля в мм; шрифты и размеры стилей «Обычный» и «Заголовок 1–3» и то, чем текст набран на деле; колонтитулы. "
        "outline_md — заготовка нового документа: заголовки образца с подсказкой «[заполнить: …]» под каждым.",
        {"file": FILE}),
    _ro("doc_text", "Текст образца",
        "Текст документа .docx как Markdown: заголовки #, таблицы, списки, листинги, на месте картинок — «[рисунок: "
        "подпись]». section — один раздел по номеру («2.1») или названию, с подразделами; limit — сколько знаков отдать "
        "(20000). Большой документ читайте по разделам (doc_outline покажет их), а не целиком.",
        {"file": FILE, "section": {"type": "string", "description": "Номер или название раздела"},
         "limit": {"type": "integer", "description": "Сколько знаков текста отдать (по умолчанию 20000)"}}),
    _rw("doc_export", "Собрать документ Word",
        "Собрать .docx из текста Markdown: заголовки # – #### (стили Word, нумерация 1, 1.1, 1.1.1), абзацы, **жирный**, "
        "*курсив*, `код`, списки (2 уровня), таблицы с шапкой и подписью («Таблица: …» строкой выше), картинки "
        "![подпись](файл \"width=120mm\") с подписью «Рисунок N — …», > примечание, --- новая страница, ``` листинг. "
        "title — титульный лист (organization сверху, subtitle, city_year внизу); toc — содержание; style_from — образец "
        ".docx: страница, поля, шрифты текста и заголовков берутся из него, иначе А4, поля 30/15/20/20 мм, Times New "
        "Roman 14, интервал 1,5. Нет файла картинки — ошибка со списком, документ не пишется.",
        {"md_file": {"type": "string", "description": r"Текст документа в Markdown (.md), UTF-8"},
         "out_file": {"type": "string", "description": r"Куда записать .docx (в разрешённой на запись папке)"},
         "style_from": {"type": "string", "description": r"Образец .docx, с которого снять страницу и шрифты"},
         "title": {"type": "string", "description": "Название документа на титульном листе (нет — без титульного листа)"},
         "subtitle": {"type": "string", "description": "Строка под названием: объект, обозначение"},
         "organization": {"type": "string", "description": "Организация — вверху титульного листа"},
         "city_year": {"type": "string", "description": "Город и год — внизу титульного листа"},
         "toc": {"type": "boolean", "description": "Вставить содержание (поле Word, заполняется при открытии)"},
         "numbering": {"type": "boolean", "description": "Нумеровать заголовки 1, 1.1, 1.1.1 (по умолчанию да)"}},
        ("md_file", "out_file")),
    _ro("doc_check", "Проверка документа",
        "Проверить документ .docx или .md: пустые разделы, оставшиеся заготовки (TODO, ???, «уточнить», […], [заполнить: "
        "…]), заголовки не по порядку (скачок уровня, номера), рисунки и таблицы без подписи. outline_from — образец: "
        "каких его разделов в документе нет и какие лишние (сравнение по названию без номера).",
        {"file": FILE, "outline_from": {"type": "string", "description": r"Образец (.docx или .md) для сверки разделов"}}),
]


def _text(s, is_error=False, data=None):
    r = {"content": [{"type": "text", "text": s}]}
    if is_error:
        r["isError"] = True
    if data is not None:
        r["structuredContent"] = data
    return r


def _json(obj):
    return json.dumps(obj, ensure_ascii=False)


def _call(name, args):
    try:
        if name == "doc_outline":
            return _text(_json(dockit.outline(args["file"])))
        if name == "doc_text":
            got = dockit.text(args["file"], str(args.get("section") or ""), args.get("limit") or 20000)
            return _text(got["text"], data={"chars": got["chars"], "truncated": got["truncated"]})
        if name == "doc_check":
            return _text(_json(dockit.check(args["file"], str(args.get("outline_from") or ""))))
        if name == "doc_export":
            src, out = str(args["md_file"]), str(args["out_file"])
            if not out.lower().endswith(".docx"):
                return _text("out_file должен кончаться на .docx", True)
            if os.path.splitext(src)[1].lower() == ".docx":
                return _text("md_file — текст Markdown (.md), а не документ Word", True)
            cfg = docxout.config(dockit.style_of(args["style_from"]) if args.get("style_from") else None)
            blob, counts, warnings = docxout.build(
                dockit.read_text(src), os.path.dirname(os.path.abspath(src)), cfg, title=str(args.get("title") or ""),
                subtitle=str(args.get("subtitle") or ""), organization=str(args.get("organization") or ""),
                city_year=str(args.get("city_year") or ""), toc=bool(args.get("toc")), numbering=args.get("numbering") is not False)
            os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
            try:
                with open(out, "wb") as f:
                    f.write(blob)
            except PermissionError:
                return _text(f"файл {out} занят — закройте документ в Word и повторите", True)
            body, page = cfg["body"], cfg["page"]
            look = (f"{'по образцу' if cfg['source'] == 'example' else 'по умолчанию'}: {page['width_mm']:g}×{page['height_mm']:g} мм, "
                    f"{body['font']} {body['size_pt']:g} пт")
            text = (f"Записано → {out}: заголовков {counts['headings']}, абзацев {counts['paragraphs']}, пунктов списков "
                    f"{counts['list_items']}, таблиц {counts['tables']}, рисунков {counts['pictures']}, строк листингов "
                    f"{counts['code_lines']}. Оформление {look}. Предупреждений {len(warnings)}"
                    + ("".join("\n— " + w for w in warnings[:40]) if warnings else "") + "\nЧисло страниц неизвестно"
                    + (": содержание и номера страниц Word заполнит при открытии (на вопрос об обновлении полей — «Да»)." if args.get("toc") else "."))
            return _text(text, data={"written": True, "out_file": out, "counts": counts, "warnings": warnings[:200],
                                     "style": {"source": cfg["source"], "page": page, "body": body}})
        return _text(f"нет инструмента {name}", True)
    except dockit.DocError as exc:
        return _text(str(exc), True)
    except KeyError as exc:
        return _text(f"не хватает поля {exc}", True)
    except OSError as exc:
        return _text(f"файл: {exc}", True)


def _out(msg):
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _handle(msg):
    if not isinstance(msg, dict) or not isinstance(msg.get("method"), str):
        return
    mid, method = msg.get("id"), msg["method"]
    params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
    if mid is None:
        return  # notification
    try:
        if method == "initialize":
            _out({"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": str(params.get("protocolVersion") or PROTOCOL),
                  "capabilities": {"tools": {"listChanged": False}},
                  "serverInfo": {"name": "doc-kit", "title": "Документы Word по образцу", "version": VERSION},
                  "instructions": INSTRUCTIONS}})
        elif method == "ping":
            _out({"jsonrpc": "2.0", "id": mid, "result": {}})
        elif method == "tools/list":
            _out({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            _out({"jsonrpc": "2.0", "id": mid, "result": _call(str(params.get("name") or ""), params.get("arguments") or {})})
        else:
            _out({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Method not found: {method}"}})
    except Exception as exc:  # noqa: BLE001 — одна ошибка не должна ронять сервер
        _out({"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(exc)}})


def main():
    for stream in (sys.stdin, sys.stdout):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        for item in (msg if isinstance(msg, list) else [msg]):
            _handle(item)
    return 0


if __name__ == "__main__":
    sys.exit(main())
