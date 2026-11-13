"""MCP-сервер drawing-kit: чертежи и схемы в PDF читаются без модели — слова листов с местами, обозначения устройств,
модули контроллера и каналы, таблицы спецификации.

Запускается синхронизатором на ПК (sysai-agent mcp drawing-kit) и говорит по stdio построчным JSON-RPC 2.0. Доступы
(папки на чтение) проверяет сам SYSAI по подписанным правилам до вызова — здесь только работа с файлами.
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import drawkit  # noqa: E402

PROTOCOL = "2025-06-18"
VERSION = "1.1.1"
PAGE = 60
PAGE_MAX = 500
FILE = {"type": "string", "description": r"Файл PDF: схема Э3, технологическая схема, спецификация, например "
                                         r"C:\Projects\line\materials\Э3.pdf"}
LIMIT = {"type": "integer", "minimum": 1, "maximum": PAGE_MAX, "description": f"Сколько строк вернуть (по умолчанию {PAGE})"}
OFFSET = {"type": "integer", "minimum": 0, "description": "С какой строки (0 — с первой): для следующей страницы"}
INSTRUCTIONS = (
    "Схемы и чертежи в PDF — текстом, без картинок: у схемы, выгруженной из CAD, есть слой текста, и он точнее и в "
    "сотни раз дешевле снимка листа. Сначала draw_sheets (file — листы: что на каждом; dir — какие PDF есть). Механизмы "
    "и устройства схемы — draw_devices (mechanisms: true — перечень механизмов по номерам, по нему считают "
    "спецификацию). Что стоит на каналах контроллера — draw_channels: канал, устройство в его колонке, подпись. Где "
    "нарисовано 10SQ2 — draw_find. Слова одного листа — draw_text, лист-таблица (спецификация, перечень оборудования) "
    "— draw_table. Место: лист и zone — колонка 0–9 рамки листа. Привязка канала к устройству — догадка по "
    "расположению: сомнительное проверь по листу. Лист без текста (scan: true) этот инструмент не читает."
)


def _ro(name, title, description, props, required=("file",)):
    return {"name": name, "title": title, "description": description,
            "inputSchema": {"type": "object", "properties": props, "required": list(required), "additionalProperties": False},
            "annotations": {"readOnlyHint": True, "openWorldHint": False}}


TOOLS = [
    _ro("draw_sheets", "Листы схемы",
        "Что в файле PDF, лист за листом: название листа из основной надписи, сколько текста, какие устройства (по видам: "
        "M — двигатели, KM — контакторы, SQ / SR — датчики…), номера механизмов, модули контроллера, сколько каналов; scan — "
        "листа без текста этот инструмент не прочтёт. dir вместо file — какие PDF лежат в папке (materials) и сколько "
        "в них листов.",
        {"file": FILE, "dir": {"type": "string", "description": "Папка: перечислить её PDF"}, "limit": LIMIT, "offset": OFFSET}, ()),
    _ro("draw_devices", "Устройства и механизмы схемы",
        "Обозначения схемы без повторов: 10KM1 — механизм 10, вид KM (контактор), на каких листах. kind — только этот "
        "вид (M, KM, QF, UZ, SQ, SR, SB…), mechanism — только этот номер. mechanisms: true — перечень механизмов: номер, "
        "есть ли привод (двигатель), какие устройства и на каких листах; по нему считают, сколько механизмов на схеме.",
        {"file": FILE, "kind": {"type": "string", "description": "Вид устройства: M, KM, QF, UZ, SQ, SR, SL, SB, SA, HL…"},
         "mechanism": {"type": "string", "description": "Номер механизма: «10» у 10KM1"},
         "mechanisms": {"type": "boolean", "description": "Перечень механизмов вместо устройств"}, "limit": LIMIT, "offset": OFFSET}),
    _ro("draw_channels", "Каналы контроллера на схеме",
        "Листы, где нарисован модуль контроллера: каждый канал (I14, Q3), модуль, устройство в колонке канала (датчик, "
        "кнопка, реле, контактор) и подпись этой колонки («Датчик сбега ленты»). page — один лист; без него — все листы "
        "с каналами. Это догадка по расположению на листе: distance — насколько далеко устройство от канала; чего нет "
        "или что сомнительно — смотрите лист (draw_text с zone).",
        {"file": FILE, "page": {"type": "integer", "minimum": 1, "description": "Номер листа"}, "limit": LIMIT, "offset": OFFSET}),
    _ro("draw_find", "Найти на схеме",
        "Где написано: обозначение (10SQ2), марка (TM3DI32K), слово («нория»). В ответе лист, zone, место, фраза и что "
        "написано рядом. Регистр и латиница / кириллица похожих букв не важны. dir вместо file — искать во всех PDF папки.",
        {"file": FILE, "dir": {"type": "string", "description": "Папка: искать во всех её PDF"},
         "query": {"type": "string", "description": "Что найти"}, "limit": LIMIT}, ("query",)),
    _ro("draw_text", "Текст листа",
        "Слова одного листа строками сверху вниз. zone — только колонка 0–9 рамки листа; area — [x0, y0, x1, y1] в "
        "пунктах от левого верхнего угла. limit — сколько знаков (6000).",
        {"file": FILE, "page": {"type": "integer", "minimum": 1, "description": "Номер листа"},
         "zone": {"type": "integer", "minimum": 0, "maximum": 9}, "area": {"type": "array", "items": {"type": "number"}},
         "limit": {"type": "integer", "minimum": 200, "maximum": 20000}}, ("file", "page")),
    _ro("draw_table", "Лист-таблица",
        "Лист, который таблица (спецификация, перечень оборудования технологической схемы): строки и ячейки по "
        "расположению слов. Ячейки многострочных названий приходят соседними строками.",
        {"file": FILE, "page": {"type": "integer", "minimum": 1, "description": "Номер листа"}, "limit": LIMIT, "offset": OFFSET},
        ("file", "page")),
]


def _text(s, is_error=False):
    r = {"content": [{"type": "text", "text": s}]}
    if is_error:
        r["isError"] = True
    return r


def _json(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _page(rows, args, default=PAGE):
    try:
        limit = min(PAGE_MAX, max(1, int(args.get("limit") or default)))
        offset = max(0, int(args.get("offset") or 0))
    except (TypeError, ValueError):
        limit, offset = default, 0
    part = rows[offset:offset + limit]
    meta = {"count": len(rows), "offset": offset, "returned": len(part)}
    if offset + len(part) < len(rows):
        meta["next_offset"] = offset + len(part)
    return part, meta


def _cache() -> str:
    return os.environ.get("SYSAI_DRAWING_CACHE") or os.path.join(tempfile.gettempdir(), "sysai-drawing-kit")


def _doc(args):
    return drawkit.load(args["file"], _cache())


def _call(name, args):
    try:
        if name == "draw_sheets":
            if args.get("dir") and not args.get("file"):
                rows = []
                for path in drawkit.pdfs(args["dir"]):
                    try:
                        s = drawkit.sheets(drawkit.load(path, _cache()))
                        rows.append({"file": path, "pages": s["pages"], "scans": s["scans"],
                                     "devices": sum(r["devices"] for r in s["sheets"]),
                                     "title": next((r["title"] for r in s["sheets"] if r["title"]), "")})
                    except drawkit.DrawError as exc:
                        rows.append({"file": path, "error": str(exc)})
                part, meta = _page(rows, args)
                return _text(_json({**meta, "files": part}))
            s = drawkit.sheets(_doc(args))
            part, meta = _page(s["sheets"], args)
            return _text(_json({"file": s["file"], "pages": s["pages"], "scans": s["scans"], "channel_style": s["channel_style"], **meta, "sheets": part}))
        if name == "draw_devices":
            doc = _doc(args)
            if args.get("mechanisms"):
                rows = drawkit.mechanisms(doc)
                part, meta = _page(rows, args)
                return _text(_json({"with_drive": sum(1 for r in rows if r["drive"]), **meta, "mechanisms": part}))
            rows = drawkit.devices(doc, str(args.get("kind") or ""), str(args.get("mechanism") or ""))
            part, meta = _page(rows, args)
            return _text(_json({**meta, "devices": part}))
        if name == "draw_channels":
            doc = _doc(args)
            pages = doc["pages"]
            if args.get("page"):
                if not 1 <= int(args["page"]) <= len(pages):
                    return _text(f"листа {args['page']} нет: в файле {len(pages)}", True)
                pages = [pages[int(args["page"]) - 1]]
            rows = []
            for page in pages:
                for row in drawkit.channel_map(page, style=doc.get("style", "iq")):
                    rows.append({"page": page["page"], **{k: v for k, v in row.items() if k not in ("x", "y")}})
            part, meta = _page(rows, args)
            return _text(_json({**meta, "with_device": sum(1 for r in rows if r.get("device")), "channels": part}))
        if name == "draw_find":
            files = [args["file"]] if args.get("file") else drawkit.pdfs(args.get("dir") or "")
            if not files:
                return _text("Назовите файл (file) или папку с PDF (dir).", True)
            limit = min(PAGE_MAX, max(1, int(args.get("limit") or 40)))
            rows = []
            for path in files:
                try:
                    for hit in drawkit.find(drawkit.load(path, _cache()), args["query"], limit - len(rows)):
                        rows.append({"file": path, **hit} if len(files) > 1 else hit)
                except drawkit.DrawError:
                    continue
                if len(rows) >= limit:
                    break
            return _text(_json({"count": len(rows), "found": rows}))
        if name == "draw_text":
            area = args.get("area") if args.get("area") else args.get("zone")
            return _text(_json(drawkit.text(_doc(args), int(args["page"]), area, int(args.get("limit") or 6000))))
        if name == "draw_table":
            rows = drawkit.table(_doc(args), int(args["page"]))
            part, meta = _page(rows, args, 120)
            return _text(_json({**meta, "rows": part}))
        return _text(f"нет инструмента {name}", True)
    except drawkit.DrawError as exc:
        return _text(str(exc), True)
    except KeyError as exc:
        return _text(f"не хватает поля {exc}", True)
    except (TypeError, ValueError) as exc:
        return _text(f"неверное значение: {exc}", True)
    except OSError as exc:
        return _text(f"файл: {exc}", True)


def _out(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
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
                  "serverInfo": {"name": "drawing-kit", "title": "Схемы и чертежи в PDF", "version": VERSION},
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


if __name__ == "__main__":
    main()
