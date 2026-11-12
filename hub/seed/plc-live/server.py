"""MCP-сервер plc-live: чтение контроллера по OPC UA — только чтение.

Запускается синхронизатором на ПК (sysai-agent mcp plc-live) и говорит по stdio построчным JSON-RPC 2.0. К каким
контроллерам можно подключаться, решает «Сеть контроллеров» доступов SYSAI: обёртка синхронизатора проверяет каждый
вызов по подписанным правилам, сам инструмент — ещё раз. Записи, загрузки, пуска и останова здесь нет вообще.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import plclive  # noqa: E402

PROTOCOL = "2025-06-18"
VERSION = "1.0.0"
PLC = {"type": "string", "description": "Контроллер: адрес (192.168.1.10), адрес:порт или opc.tcp://адрес:4840. Должен "
                                        "быть в «Сети контроллеров» доступов агента"}
NAMES = {"type": "array", "items": {"type": "string"}, "description": "Переменные «Список.Переменная» (GVL.Start_1) — узел "
                                                                    "считается как у Schneider M241 / M251"}
NODES = {"type": "array", "items": {"type": "string"}, "description": "Идентификаторы узлов как есть: ns=2;i=123, "
                                                                    "ns=4;s=|var|Device.Application.GVL.Start"}
LOGIN = {"user": {"type": "string", "description": "Пользователь OPC UA, если анонимный вход закрыт"},
         "password": {"type": "string", "description": "Его пароль (не сохраняется и не пишется в ответ)"},
         "ns": {"type": "integer", "minimum": 0, "description": "Пространство имён для names (по умолчанию 2)"}}
INSTRUCTIONS = (
    "Живой контроллер по сети — только чтение, по OPC UA. plc_scan — кто отвечает (порты OPC UA 4840, Modbus 502, шлюз "
    "Machine Expert 11740) и что сервер OPC UA говорит о себе. plc_read — значения переменных сейчас; имена — "
    "«Список.Переменная» из проекта (plc_hmi_tags инструмента plc-kit, tags_file — его CSV), снимок — out_file в "
    "_work/live. plc_watch — что менялось за время наблюдения: фронты, min / max. Писать в контроллер, загружать, "
    "пускать и останавливать этот инструмент не умеет — это делает инженер в Machine Expert. Адрес контроллера "
    "должен быть в «Сети контроллеров» доступов агента: иначе отказ — попросите инженера добавить его в SYSAI."
)


def _tool(name, title, description, props, required, write=False):
    return {"name": name, "title": title, "description": description,
            "inputSchema": {"type": "object", "properties": props, "required": list(required), "additionalProperties": False},
            "annotations": {"readOnlyHint": not write, "destructiveHint": False, "openWorldHint": True}}


TOOLS = [
    _tool("plc_scan", "Кто отвечает в сети",
          "Какие контроллеры отвечают: по каждому адресу — открытые порты (OPC UA 4840, Modbus TCP 502, шлюз Machine "
          "Expert 11740) и, где есть OPC UA, имя сервера, режимы защиты, разрешён ли анонимный вход. plcs — адреса; "
          "plc_network — сеть целиком (не больше 256 адресов). Сессий не открывает, ничего не пишет.",
          {"plcs": {"type": "array", "items": {"type": "string"}, "description": "Адреса контроллеров"},
           "plc_network": {"type": "string", "description": "Сеть: 192.168.1.0/24"},
           "timeout": {"type": "number", "minimum": 0.1, "maximum": 5, "description": "Сколько ждать порт, с (0,6)"}}, ()),
    _tool("plc_read", "Прочитать переменные",
          "Значения переменных контроллера сейчас: имя, узел, значение, тип; чего сервер не отдал — с причиной. names — "
          "имена из проекта, nodes — узлы как есть, tags_file — CSV тегов (plc_hmi_tags): его имена. out_file — "
          "записать снимок таблицей CSV (в _work/live проекта). Только чтение.",
          {"plc": PLC, "names": NAMES, "nodes": NODES,
           "tags_file": {"type": "string", "description": r"CSV тегов проекта: C:\...\_work\hmi\tags.csv"},
           "out_file": {"type": "string", "description": r"Куда записать снимок: C:\...\_work\live\снимок.csv"}, **LOGIN},
          ("plc",), write=True),
    _tool("plc_watch", "Понаблюдать за переменными",
          "Что менялось: переменные читаются каждые period секунд в течение seconds (не дольше 120); по каждой — первое "
          "и последнее значение, сколько раз менялась, моменты изменений (первые 40), у числа — min и max. Для отладки "
          "логики: нажали кнопку — видно, что сработало. Изменение короче period не увидеть. Только чтение.",
          {"plc": PLC, "names": NAMES, "nodes": NODES,
           "seconds": {"type": "number", "minimum": 1, "maximum": 120, "description": "Сколько наблюдать (10)"},
           "period": {"type": "number", "minimum": 0.2, "maximum": 60, "description": "Как часто читать, с (0,5)"}, **LOGIN},
          ("plc",)),
]


def _text(s, is_error=False):
    r = {"content": [{"type": "text", "text": s}]}
    if is_error:
        r["isError"] = True
    return r


def _json(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _login(args):
    return {"ns": int(args.get("ns") if args.get("ns") is not None else 2), "user": str(args.get("user") or ""),
            "password": str(args.get("password") or "")}


def _call(name, args):
    try:
        if name == "plc_scan":
            found = plclive.scan(args.get("plcs"), str(args.get("plc_network") or ""), float(args.get("timeout") or 0.6))
            return _text(_json({"answered": len(found), "controllers": found}))
        if name == "plc_read":
            names = list(args.get("names") or [])
            if args.get("tags_file"):
                names += plclive.names_from_csv(args["tags_file"])
            got = plclive.read(args["plc"], names, args.get("nodes"), **_login(args))
            if args.get("out_file"):
                got["out_file"] = plclive.save_csv(got["values"], args["out_file"])
                got["values"] = got["values"][:60]   # the whole table is in the file
                got["note"] = "в ответе первые 60 строк; весь снимок — в out_file"
            return _text(_json(got))
        if name == "plc_watch":
            got = plclive.watch(args["plc"], args.get("names"), args.get("nodes"), float(args.get("seconds") or 10),
                                float(args.get("period") or 0.5), **_login(args))
            return _text(_json(got))
        return _text(f"нет инструмента {name}", True)
    except plclive.LiveError as exc:
        return _text(str(exc), True)
    except KeyError as exc:
        return _text(f"не хватает поля {exc}", True)
    except (TypeError, ValueError) as exc:
        return _text(f"неверное значение: {exc}", True)
    except OSError as exc:
        return _text(f"сеть или файл: {exc}", True)


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
                  "serverInfo": {"name": "plc-live", "title": "Контроллер по сети: только чтение", "version": VERSION},
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
