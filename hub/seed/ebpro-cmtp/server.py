"""MCP-сервер ebpro-cmtp: читает и собирает проекты EasyBuilder Pro (.cmtp/.emtp).

Запускается синхронизатором на ПК (sysai-agent mcp ebpro-cmtp) и говорит по stdio
построчным JSON-RPC 2.0 (как ждёт обёртка SYSAI). Доступы (папки на чтение/запись)
проверяет сам SYSAI по подписанным правилам до вызова — здесь только работа с файлами.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ebpcmtp import Project  # noqa: E402

PROTOCOL = "2025-06-18"
PATH = {"type": "string", "description": r"Полный путь к файлу проекта, например C:\Projects\topol.emtp"}

TOOLS = [
    {
        "name": "cmtp_info",
        "title": "Сведения о проекте",
        "description": "Прочитать проект EasyBuilder Pro (.cmtp/.emtp): версия EasyBuilder, редакция формата, "
                       "зашифрован ли, размер проекта внутри, сколько встроенных картинок, сколько окон. "
                       "С этого начинают разбор присланного примера.",
        "inputSchema": {"type": "object", "properties": {"path": PATH}, "required": ["path"], "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "cmtp_labels",
        "title": "Тексты и имена окон",
        "description": "Выписать все подписи и имена окон проекта (тексты UTF-16) с их смещениями — чтобы понять "
                       "структуру экранов примера и найти, что можно переименовать. filter — показать только с подстрокой.",
        "inputSchema": {"type": "object", "properties": {"path": PATH,
                        "filter": {"type": "string", "description": "Показать только подписи с этой подстрокой"}},
                        "required": ["path"], "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "cmtp_extract_images",
        "title": "Достать картинки",
        "description": "Вынуть встроенные картинки проекта (PNG/JPEG из библиотеки картинок и фото) в папку — "
                       "пополнить память примеров и переиспользовать ассеты. out_dir — куда сохранить (в разрешённой папке).",
        "inputSchema": {"type": "object", "properties": {"path": PATH,
                        "out_dir": {"type": "string", "description": r"Папка для картинок, например C:\SYSAI\assets\topol"}},
                        "required": ["path", "out_dir"], "additionalProperties": False},
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False},
    },
    {
        "name": "cmtp_unpack",
        "title": "Расшифровать проект",
        "description": "Расшифровать внутренний блок проекта (снять AES) в .bin — для глубокого разбора формата. "
                       "out_file — куда записать (в разрешённой папке на запись).",
        "inputSchema": {"type": "object", "properties": {"path": PATH,
                        "out_file": {"type": "string", "description": r"Куда записать .bin"}},
                        "required": ["path", "out_file"], "additionalProperties": False},
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False},
    },
    {
        "name": "cmtp_pack",
        "title": "Собрать проект",
        "description": "Собрать проект обратно из расшифрованного .bin: зашифровать и записать .cmtp/.emtp. "
                       "revision и version берут из cmtp_info исходного примера. Полезно после правки блока. "
                       "in_file — .bin, out_file — куда записать проект.",
        "inputSchema": {"type": "object", "properties": {
            "in_file": {"type": "string", "description": "Расшифрованный .bin"},
            "out_file": {"type": "string", "description": r"Куда записать .cmtp/.emtp"},
            "revision": {"type": "integer", "description": "Редакция формата из cmtp_info (например 378)"},
            "version": {"type": "string", "description": "Версия EasyBuilder из cmtp_info (например 6.07.02.420)"}},
            "required": ["in_file", "out_file", "revision", "version"], "additionalProperties": False},
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False},
    },
    {
        "name": "cmtp_relabel",
        "title": "Переименовать подпись",
        "description": "Поменять одну подпись или имя окна на текст той же длины и пересобрать проект — безопасная "
                       "правка «по примеру», меняются только нужные байты. old — что заменить, new — на что (та же длина).",
        "inputSchema": {"type": "object", "properties": {"path": PATH,
            "out_file": {"type": "string", "description": r"Куда записать новый проект"},
            "old": {"type": "string", "description": "Текущая подпись"},
            "new": {"type": "string", "description": "Новая подпись (та же длина)"}},
            "required": ["path", "out_file", "old", "new"], "additionalProperties": False},
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False},
    },
]

INSTRUCTIONS = ("Инструменты для проектов EasyBuilder Pro (.cmtp/.emtp). Чтобы разобрать присланный пример: "
                "cmtp_info → cmtp_labels → cmtp_extract_images. Чтобы собрать по образцу: правьте расшифрованный "
                "блок и cmtp_pack, или cmtp_relabel для быстрой замены подписи. Доступы к папкам даёт SYSAI.")


def _text(s, is_error=False):
    r = {"content": [{"type": "text", "text": s}]}
    if is_error:
        r["isError"] = True
    return r


def _call(name, args):
    try:
        if name == "cmtp_info":
            p = Project.load(args["path"])
            return _text(json.dumps({"path": args["path"], "version": p.version, "revision": p.revision,
                "revision_min": p.revision_min, "encrypted": p.encrypted, "payload_bytes": len(p.payload),
                "images": sum(1 for _ in p.images())}, ensure_ascii=False))
        if name == "cmtp_labels":
            p = Project.load(args["path"])
            flt = (args.get("filter") or "").lower()
            rows = [{"offset": o, "text": s} for o, s in p.labels() if not flt or flt in s.lower()]
            return _text(json.dumps({"count": len(rows), "labels": rows[:2000]}, ensure_ascii=False))
        if name == "cmtp_extract_images":
            p = Project.load(args["path"])
            out = args["out_dir"]
            os.makedirs(out, exist_ok=True)
            n = 0
            for im in p.images():
                blob = p.payload[im["offset"]:im["offset"] + im["length"]]
                open(os.path.join(out, f"img_{im['offset']:08d}.{im['kind']}"), "wb").write(blob)
                n += 1
            return _text(f"Сохранено картинок: {n} → {out}")
        if name == "cmtp_unpack":
            p = Project.load(args["path"])
            open(args["out_file"], "wb").write(p.payload)
            return _text(f"Расшифрован блок проекта ({len(p.payload)} байт) → {args['out_file']} "
                         f"(revision={p.revision}, version={p.version})")
        if name == "cmtp_pack":
            payload = open(args["in_file"], "rb").read()
            rev = int(args["revision"])
            Project(rev, str(args["version"]), rev >= 240, payload).save(args["out_file"])
            return _text(f"Собран проект → {args['out_file']}")
        if name == "cmtp_relabel":
            p = Project.load(args["path"])
            off = next((o for o, s in p.labels() if s == args["old"]), None)
            if off is None:
                return _text(f"Подпись не найдена: {args['old']!r}", True)
            p.replace_label(off, args["old"], args["new"])
            p.save(args["out_file"])
            return _text(f"Заменено {args['old']!r} → {args['new']!r} → {args['out_file']}")
        return _text(f"нет такого инструмента: {name}", True)
    except FileNotFoundError as exc:
        return _text(f"файл не найден: {exc}", True)
    except ValueError as exc:
        return _text(f"не выходит: {exc}", True)
    except OSError as exc:
        return _text(f"ошибка файла: {exc}", True)


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
                  "serverInfo": {"name": "ebpro-cmtp", "title": "EasyBuilder Pro .cmtp/.emtp", "version": "1.0.0"},
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
