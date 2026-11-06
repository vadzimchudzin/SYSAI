"""MCP-сервер ebpro-gui: агент ведёт настоящий EasyBuilder Pro — смотрит на его окна и жмёт мышь и клавиши, как человек.

Говорит по stdio построчным JSON-RPC 2.0 (как ждёт обёртка SYSAI). Работает только на Windows и только с окнами
EasyBuilder; окна загрузки в панель, паролей и прошивки оставляет человеку. Одна мышь — один ведущий: кто начал,
тот держит EasyBuilder 90 секунд после своего последнего действия (или до eb_free), остальные ждут.
"""
import base64
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gui import Driver, GuiError, _public  # noqa: E402

PROTOCOL = "2025-06-18"
VERSION = "1.0.0"
DRIVER = None          # tests put a Driver with a fake backend here


def driver() -> Driver:
    global DRIVER
    if DRIVER is None:
        if os.name != "nt":
            raise GuiError("ebpro-gui работает только на Windows (там, где стоит EasyBuilder Pro)")
        from win32 import Win32
        DRIVER = Driver(Win32())
    return DRIVER


WHO = {"type": "string", "description": "Кто ведёт: «Агент» или имя помощника — пока он ведёт, другие ждут"}
WINDOW = {"type": ["string", "integer"], "description": "Окно EasyBuilder: номер из eb_windows или часть заголовка; "
          "по умолчанию — то, что сейчас впереди (или главное)"}
MODE = {"type": "string", "enum": ["real", "post"], "description": "real (по умолчанию) — настоящие мышь и "
        "клавиатура, окно выходит вперёд; post — сообщения окну без мыши (не всякая кнопка их понимает)"}
SHOT = {"type": "boolean", "description": "После действия вернуть картинку окна (через полсекунды)"}
SCALE = {"type": "number", "description": "Масштаб картинки: 1 (по умолчанию), 0.5 — вдвое меньше"}
XY = "от левого верхнего угла окна в пикселях — как на картинке eb_shot при scale 1"


def _obj(props, required=()):
    return {"type": "object", "properties": props, "required": list(required), "additionalProperties": False}


TOOLS = [
    {"name": "eb_edit", "title": "Открыть проект в EasyBuilder",
     "description": "Запустить EasyBuilder Pro с проектом (.cmtp/.emtp) и дождаться его окна. EasyBuilder ищется "
                    "сам (EBPRO_EXE, выбор в окне «Агент SYSAI», C:\\EBpro); exe — указать явно.",
     "inputSchema": _obj({"path": {"type": "string", "description": "Полный путь к проекту"},
                          "exe": {"type": "string", "description": "Путь к EasyBuilderPro.exe (если не нашёлся сам)"},
                          "wait": {"type": "number", "description": "Сколько секунд ждать окно (по умолчанию 60)"}},
                         ["path"]),
     "annotations": {"readOnlyHint": False, "openWorldHint": False}},
    {"name": "eb_windows", "title": "Окна EasyBuilder",
     "description": "Открытые окна EasyBuilder (главное, диалоги, симулятор): номер, заголовок, место, размер, "
                    "какое впереди и кто сейчас ведёт EasyBuilder.",
     "inputSchema": _obj({}), "annotations": {"readOnlyHint": True, "openWorldHint": False}},
    {"name": "eb_shot", "title": "Картинка окна EasyBuilder",
     "description": "Снимок окна EasyBuilder (PNG) — посмотреть, что на экране, и найти, куда нажать: координаты "
                    "для eb_click — от левого верхнего угла картинки. out_file — ещё и сохранить.",
     "inputSchema": _obj({"window": WINDOW, "scale": SCALE,
                          "out_file": {"type": "string", "description": "Куда сохранить PNG (необязательно)"},
                          "show": {"type": "boolean", "description": "Вернуть картинку в ответе (по умолчанию да)"}}),
     "annotations": {"readOnlyHint": True, "openWorldHint": False}},
    {"name": "eb_controls", "title": "Поля и кнопки окна",
     "description": "Поля, кнопки и списки окна EasyBuilder, которые видит Windows (у диалогов — почти все, у ленты "
                    "и рабочего поля — нет): номер поля, класс, текст, место. Номер поля — для eb_write control.",
     "inputSchema": _obj({"window": WINDOW, "filter": {"type": "string", "description": "Только с этим текстом"}}),
     "annotations": {"readOnlyHint": True, "openWorldHint": False}},
    {"name": "eb_click", "title": "Нажать мышью",
     "description": f"Нажать мышью в окне EasyBuilder: x, y — {XY}. Двойной щелчок — double, правая кнопка — button.",
     "inputSchema": _obj({"who": WHO, "window": WINDOW, "x": {"type": "integer"}, "y": {"type": "integer"},
                          "button": {"type": "string", "enum": ["left", "right", "middle"]},
                          "double": {"type": "boolean"}, "mode": MODE, "shot": SHOT, "scale": SCALE},
                         ["who", "x", "y"]),
     "annotations": {"readOnlyHint": False, "openWorldHint": False}},
    {"name": "eb_drag", "title": "Перетащить мышью",
     "description": f"Перетащить с нажатой левой кнопкой из (x, y) в (to_x, to_y) — {XY}: подвинуть объект, "
                    "выделить рамкой, растянуть.",
     "inputSchema": _obj({"who": WHO, "window": WINDOW, "x": {"type": "integer"}, "y": {"type": "integer"},
                          "to_x": {"type": "integer"}, "to_y": {"type": "integer"}, "shot": SHOT, "scale": SCALE},
                         ["who", "x", "y", "to_x", "to_y"]),
     "annotations": {"readOnlyHint": False, "openWorldHint": False}},
    {"name": "eb_keys", "title": "Нажать клавиши",
     "description": "Нажать клавиши в EasyBuilder: «ctrl+s», «alt, h», «enter», «ctrl+shift+f7», «delete»; "
                    "через запятую — одна за другой.",
     "inputSchema": _obj({"who": WHO, "window": WINDOW, "keys": {"type": "string"}, "mode": MODE,
                          "shot": SHOT, "scale": SCALE}, ["who", "keys"]),
     "annotations": {"readOnlyHint": False, "openWorldHint": False}},
    {"name": "eb_write", "title": "Напечатать текст",
     "description": "Напечатать текст в EasyBuilder (в поле, где курсор) или вписать в поле control из eb_controls "
                    "(заменит его текст). Русские буквы — как есть.",
     "inputSchema": _obj({"who": WHO, "window": WINDOW, "text": {"type": "string"},
                          "control": {"type": "integer", "description": "Номер поля из eb_controls"},
                          "mode": MODE, "shot": SHOT, "scale": SCALE}, ["who", "text"]),
     "annotations": {"readOnlyHint": False, "openWorldHint": False}},
    {"name": "eb_wait", "title": "Дождаться окна",
     "description": "Дождаться, пока у EasyBuilder появится окно с этим текстом в заголовке (или закроется — gone).",
     "inputSchema": _obj({"title": {"type": "string"}, "gone": {"type": "boolean"},
                          "timeout": {"type": "number", "description": "Секунд, по умолчанию 30, до 300"}}, ["title"]),
     "annotations": {"readOnlyHint": True, "openWorldHint": False}},
    {"name": "eb_close", "title": "Закрыть окно",
     "description": "Закрыть окно EasyBuilder, как крестиком (главное окно спросит, сохранить ли проект).",
     "inputSchema": _obj({"who": WHO, "window": WINDOW}, ["who", "window"]),
     "annotations": {"readOnlyHint": False, "openWorldHint": False}},
    {"name": "eb_free", "title": "Отпустить EasyBuilder",
     "description": "Закончил — отпустить EasyBuilder, чтобы его мог вести другой агент или человек.",
     "inputSchema": _obj({"who": WHO}, ["who"]), "annotations": {"readOnlyHint": False, "openWorldHint": False}},
]

INSTRUCTIONS = ("Ведёт настоящий EasyBuilder Pro на этом ПК. Цикл: eb_shot (посмотреть) → eb_click / eb_keys / "
                "eb_write (сделать, shot — сразу увидеть итог) → снова eb_shot. Координаты — от угла картинки при "
                "scale 1. Диалоги: eb_controls и eb_write control. Сохранить — eb_keys «ctrl+s». Загрузку в панель, "
                "пароли и прошивку делает человек. Закончил — eb_free.")


def _text(s, is_error=False):
    r = {"content": [{"type": "text", "text": s}]}
    if is_error:
        r["isError"] = True
    return r


def _json(data):
    return _text(json.dumps(data, ensure_ascii=False))


def _picture(res, d, ref, scale, delay=0.0):
    if delay:
        time.sleep(delay)
    w, data, nw, nh, step = d.shot(ref, scale)
    res["content"].append({"type": "image", "data": base64.b64encode(data).decode(), "mimeType": "image/png"})
    res["content"][0]["text"] += f"\nКартинка: окно {w['hwnd']} «{w['title']}» {nw}×{nh}" + \
        (f" (уменьшено в {step} раза: координаты ×{step})" if step > 1 else "")
    return res


def _done(d, w, what, args):
    res = _text(f"{what} — окно {w['hwnd']} «{w['title']}»")
    if args.get("shot"):
        _picture(res, d, None, args.get("scale") or 1, 0.5)
    return res


def _call(name, args):
    try:
        d = driver()
        who, ref, mode = args.get("who"), args.get("window"), args.get("mode") or "real"
        if mode not in ("real", "post"):
            raise GuiError("mode: real или post")
        if name == "eb_windows":
            ws = [_public(w) for w in d.windows()]
            h = d.lock.holder()
            return _json({"count": len(ws), "windows": ws,
                          "driver": {"who": h[0], "seconds_left": int(h[1]) + 1} if h else None})
        if name == "eb_edit":
            w, ws = d.edit(args["path"], args.get("exe") or None, args.get("wait") or 60)
            return _json({"opened": args["path"], "window": _public(w), "windows": [_public(x) for x in ws]})
        if name == "eb_shot":
            w, data, nw, nh, step = d.shot(ref, args.get("scale") or 1)
            info = f"Окно {w['hwnd']} «{w['title']}» {nw}×{nh}" + \
                   (f" (уменьшено в {step} раза: координаты ×{step})" if step > 1 else "")
            if args.get("out_file"):
                out = os.path.abspath(args["out_file"])
                os.makedirs(os.path.dirname(out), exist_ok=True)
                with open(out, "wb") as f:
                    f.write(data)
                info += f" → {out}"
            res = _text(info)
            if args.get("show", True):
                res["content"].append({"type": "image", "data": base64.b64encode(data).decode(),
                                       "mimeType": "image/png"})
            return res
        if name == "eb_controls":
            w, rows = d.controls(ref, args.get("filter") or "")
            return _json({"window": _public(w), "count": len(rows), "controls": rows})
        if name == "eb_click":
            w = d.click(who, ref, args["x"], args["y"], args.get("button") or "left", bool(args.get("double")), mode)
            return _done(d, w, f"Нажато ({args['x']}, {args['y']})", args)
        if name == "eb_drag":
            w = d.drag(who, ref, args["x"], args["y"], args["to_x"], args["to_y"])
            return _done(d, w, f"Перетащено ({args['x']}, {args['y']}) → ({args['to_x']}, {args['to_y']})", args)
        if name == "eb_keys":
            w = d.keys(who, ref, args["keys"], mode)
            return _done(d, w, f"Нажато «{args['keys']}»", args)
        if name == "eb_write":
            w = d.write(who, ref, args["text"], args.get("control"), mode)
            return _done(d, w, f"Напечатано {len(str(args['text']))} знак(ов)", args)
        if name == "eb_wait":
            w = d.wait(args["title"], bool(args.get("gone")), args.get("timeout") or 30)
            return _text(f"Окна «{args['title']}» больше нет" if w is None else
                         f"Есть окно {w['hwnd']} «{w['title']}»")
        if name == "eb_close":
            w = d.close(who, ref)
            return _text(f"Закрываю окно {w['hwnd']} «{w['title']}»")
        if name == "eb_free":
            was = d.lock.free(who)
            return _text("EasyBuilder свободен" if was else "EasyBuilder и так никто не вёл")
        return _text(f"нет такого инструмента: {name}", True)
    except KeyError as exc:
        return _text(f"не хватает параметра {exc}", True)
    except (GuiError, OSError, ValueError, TypeError) as exc:
        return _text(str(exc), True)


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
                  "serverInfo": {"name": "ebpro-gui", "title": "EasyBuilder Pro: мышь и клавиатура", "version": VERSION},
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
