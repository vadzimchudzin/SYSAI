"""MCP-сервер ebpro-cmtp: читает и собирает проекты EasyBuilder Pro (.cmtp/.emtp).

Запускается синхронизатором на ПК (sysai-agent mcp ebpro-cmtp) и говорит по stdio
построчным JSON-RPC 2.0 (как ждёт обёртка SYSAI). Доступы (папки на чтение/запись)
проверяет сам SYSAI по подписанным правилам до вызова — здесь только работа с файлами.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ebpcmtp import Project, public  # noqa: E402

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
    {
        "name": "cmtp_windows",
        "title": "Окна проекта",
        "description": "Список окон проекта EasyBuilder Pro: номер, имя, размер, сколько объектов. С этого начинают "
                       "разбор экранов примера. filter — показать только окна с подстрокой в имени.",
        "inputSchema": {"type": "object", "properties": {"path": PATH,
                        "filter": {"type": "string", "description": "Подстрока имени окна"}},
                        "required": ["path"], "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "cmtp_objects",
        "title": "Объекты окна",
        "description": "Объекты одного окна по порядку: index, вид (Text, ToggleSwitch, SetBit, NumericInput, "
                       "FunctionKey, Picture, Rect…; «?» — вид угадан), рамка bbox [лево, верх, право, низ], подписи "
                       "(текст, шрифт, размер, x, y), прочие тексты и адреса (теги OPC UA, локальные адреса). "
                       "window — имя окна или его номер. kind — показать только объекты этого вида.",
        "inputSchema": {"type": "object", "properties": {"path": PATH,
                        "window": {"type": "string", "description": "Имя окна (точно) или номер, например «Нория 3» или 23"},
                        "kind": {"type": "string", "description": "Только объекты этого вида, например ToggleSwitch"}},
                        "required": ["path", "window"], "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "cmtp_tags",
        "title": "Теги OPC UA",
        "description": "Таблица тегов OPC UA проекта (что HMI читает из ПЛК): имя, тип (BOOL, INT, UINT, UDINT…), "
                       "узел. Нужна, чтобы привязать объекты к нужным тегам. filter — подстрока имени.",
        "inputSchema": {"type": "object", "properties": {"path": PATH,
                        "filter": {"type": "string", "description": "Подстрока имени тега"}},
                        "required": ["path"], "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "cmtp_scene",
        "title": "Как выглядит окно",
        "description": "Окно так, как его покажет панель: размер и цвет фона; у каждого объекта по порядку рисования — "
                       "рамка bbox, заливка и цвет рамки у фигур и текстов, подписи по состояниям (текст, шрифт, размер "
                       "в пунктах, x, y, цвет, выравнивание), картинки состояний из библиотеки проекта (picture: GUID "
                       "файла и прозрачный цвет) или shape — фигура из библиотеки EasyBuilder, которой нет в проекте. "
                       "images_dir — выложить туда картинки окна (BMP с прозрачным цветом — в PNG с прозрачностью); "
                       "images — какой файл у какой картинки. window — имя окна или номер.",
        "inputSchema": {"type": "object", "properties": {"path": PATH,
                        "window": {"type": "string", "description": "Имя окна (точно) или номер"},
                        "images_dir": {"type": "string", "description": "Папка для картинок окна (в разрешённой на запись)"}},
                        "required": ["path", "window"], "additionalProperties": False},
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False},
    },
    {
        "name": "cmtp_edit",
        "title": "Править окна",
        "description": "Правки готовых окон по списку и сборка нового файла проекта (исходник не меняется). "
                       "Виды правок (op): text — заменить текст в окне на любой длины (old → new, object — только "
                       "в этом объекте; рамка «Текста» расширяется под длинный текст); move — сдвинуть объект "
                       "(object, dx, dy в пикселях); tag — перепривязать "
                       "объекты окна с тега OPC UA old на тег new (тот же тип, тег должен быть в таблице проекта; "
                       "object — только этот объект); rename_window — новое имя окна (new); clone_window — копия "
                       "окна window под номером number (по умолчанию — следующий после последнего) с именем new; "
                       "clone_object — копия объекта object в конец того же окна или окна to, со сдвигом dx, dy "
                       "(копию потом правят text/tag/move: её index — следующий после последнего). Правки идут по "
                       "порядку; если одна не прошла — файл не пишется.",
        "inputSchema": {"type": "object", "properties": {"path": PATH,
            "out_file": {"type": "string", "description": r"Куда записать новый проект, например C:\Projects\topol_v2.cmtp"},
            "edits": {"type": "array", "minItems": 1, "description": "Правки по порядку", "items": {
                "type": "object", "properties": {
                    "op": {"type": "string", "enum": ["text", "move", "tag", "rename_window", "clone_window",
                                                      "clone_object"]},
                    "window": {"type": "string", "description": "Имя окна или номер"},
                    "object": {"type": "integer", "description": "index объекта из cmtp_objects"},
                    "old": {"type": "string"}, "new": {"type": "string"},
                    "dx": {"type": "integer"}, "dy": {"type": "integer"},
                    "number": {"type": "integer", "description": "clone_window: номер нового окна"},
                    "to": {"type": "string", "description": "clone_object: окно, куда положить копию"}},
                "required": ["op", "window"], "additionalProperties": False}}},
            "required": ["path", "out_file", "edits"], "additionalProperties": False},
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False},
    },
]

INSTRUCTIONS = ("Инструменты для проектов EasyBuilder Pro (.cmtp/.emtp). Разобрать пример: cmtp_info → cmtp_windows → "
                "cmtp_objects (окно) → cmtp_tags; картинки — cmtp_extract_images. Собрать по образцу: cmtp_edit "
                "(тексты любой длины, сдвиг, перепривязка тегов, имя окна) в новый файл. Доступы к папкам даёт SYSAI.")


_CACHE = {}   # path -> (mtime_ns, size, Project): reading tools on an unchanged file do not decrypt it again


def _load(path):
    st = os.stat(path)
    key = os.path.abspath(path)
    got = _CACHE.get(key)
    if got and got[0] == st.st_mtime_ns and got[1] == st.st_size:
        return got[2]
    p = Project.load(path)
    _CACHE.clear()
    _CACHE[key] = (st.st_mtime_ns, st.st_size, p)
    return p


def _text(s, is_error=False):
    r = {"content": [{"type": "text", "text": s}]}
    if is_error:
        r["isError"] = True
    return r


def _call(name, args):
    try:
        if name == "cmtp_info":
            p = _load(args["path"])
            return _text(json.dumps({"path": args["path"], "version": p.version, "revision": p.revision,
                "revision_min": p.revision_min, "encrypted": p.encrypted, "payload_bytes": len(p.payload),
                "images": sum(1 for _ in p.images()), "windows": len(p.windows()),
                "opcua_tags": len(p.opcua_tags())}, ensure_ascii=False))
        if name == "cmtp_labels":
            p = _load(args["path"])
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
        if name == "cmtp_windows":
            p = _load(args["path"])
            flt = (args.get("filter") or "").lower()
            rows = [{"number": w["number"], "name": w["name"], "width": w["width"], "height": w["height"],
                     "objects": len(w["objects"])} for w in p.windows() if not flt or flt in w["name"].lower()]
            return _text(json.dumps({"count": len(rows), "windows": rows}, ensure_ascii=False))
        if name == "cmtp_objects":
            p = _load(args["path"])
            w = p.window(args["window"])
            kind = args.get("kind")
            objs = [public(o) for o in w["objects"] if not kind or o["kind"] == kind]
            head = dict(public(w), objects=len(w["objects"]))
            return _text(json.dumps({"window": head, "objects": objs}, ensure_ascii=False))
        if name == "cmtp_tags":
            p = _load(args["path"])
            flt = (args.get("filter") or "").lower()
            rows = [t for t in p.opcua_tags() if not flt or flt in t["name"].lower()]
            return _text(json.dumps({"count": len(rows), "tags": rows[:5000]}, ensure_ascii=False))
        if name == "cmtp_scene":
            p = _load(args["path"])
            return _text(json.dumps(p.scene(args["window"], args.get("images_dir") or None), ensure_ascii=False))
        if name == "cmtp_edit":
            p = Project.load(args["path"])
            done, results = [], []
            for n, e in enumerate(args.get("edits") or [], 1):
                op, win = e.get("op"), e.get("window")
                try:
                    num = p.window(win)["number"] if op != "clone_window" else None
                    res = {"op": op, "window": num}
                    if op == "text":
                        k = p.edit_text(win, str(e["old"]), str(e["new"]), e.get("object"))
                        done.append(f"{n}. текст {e['old']!r} → {e['new']!r}: {k} шт.")
                        res.update(object=e.get("object"), count=k)
                    elif op == "move":
                        bb = p.move_object(win, int(e["object"]), int(e.get("dx") or 0), int(e.get("dy") or 0))
                        done.append(f"{n}. объект #{e['object']} сдвинут, рамка {bb}")
                        res.update(object=int(e["object"]), bbox=bb)
                    elif op == "tag":
                        k = p.retag(win, str(e["old"]), str(e["new"]), e.get("object"))
                        done.append(f"{n}. тег {e['old']} → {e['new']}: {k} адрес(ов)")
                        res.update(object=e.get("object"), count=k)
                    elif op == "rename_window":
                        p.rename_window(win, str(e["new"]))
                        done.append(f"{n}. окно {win!r} → {e['new']!r}")
                    elif op == "clone_window":
                        num = p.clone_window(win, e.get("new"), e.get("number"))
                        done.append(f"{n}. окно {win!r} скопировано → {num} : {p.window(num)['name']}")
                        res.update(window=num)
                    elif op == "clone_object":
                        k = p.clone_object(win, int(e["object"]), e.get("to"), int(e.get("dx") or 0), int(e.get("dy") or 0))
                        done.append(f"{n}. объект #{e['object']} скопирован → #{k} в окне {e.get('to') or win!r}")
                        res.update(window=p.window(e["to"])["number"] if e.get("to") else num, object=k)
                    else:
                        return _text(f"правка {n}: нет такого op: {op!r}", True)
                    results.append(res)
                except KeyError as exc:
                    return _text(f"правка {n} ({op}): не хватает поля {exc}; файл не записан", True)
                except ValueError as exc:
                    return _text(f"правка {n} ({op}): {exc}; файл не записан", True)
            p.save(args["out_file"])
            return dict(_text("\n".join(done + [f"Записано → {args['out_file']}"])),
                        structuredContent={"results": results, "out_file": args["out_file"]})
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
                  "serverInfo": {"name": "ebpro-cmtp", "title": "EasyBuilder Pro .cmtp/.emtp", "version": "1.3.0"},
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
