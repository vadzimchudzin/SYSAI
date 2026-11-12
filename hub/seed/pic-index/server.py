"""MCP-сервер pic-index: индекс библиотеки картинок оборудования для экранов панели (HMI).

Запускается синхронизатором на ПК (sysai-agent mcp pic-index) и говорит по stdio построчным JSON-RPC 2.0. Доступы
(папки на чтение/запись) проверяет сам SYSAI по подписанным правилам до вызова — здесь только работа с файлами.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import picindex  # noqa: E402

PROTOCOL = "2025-06-18"
VERSION = "1.0.1"
DIR = {"type": "string", "description": r"Папка с картинками (вложенные папки тоже), например C:\Projects\line\hmi\pictures"}
STATE = {"type": "string", "description": "Состояние: " + ", ".join(picindex.STATES)}
INSTRUCTIONS = (
    "Библиотека картинок оборудования для экранов панели. Найти набор картинок по смыслу: pics_find (запрос «нория», "
    "«conveyor», «задвижка открыта») — в ответе states: состояние → файл, это и передают объекту экрана. Что есть в "
    "папке — pics_list; что с библиотекой не так — pics_check. Индекс pic-index.json лежит в папке с картинками и "
    "обновляется сам; pics_index_update — пересчитать явно. Инструмент не смотрит внутрь картинок: только имена файлов "
    "и папок. Нераспознанные картинки посмотри сам и подпиши pics_label_update — подпись переживает переименование."
)


def _ro(name, title, description, props, required=("dir",)):
    return {"name": name, "title": title, "description": description,
            "inputSchema": {"type": "object", "properties": props, "required": list(required), "additionalProperties": False},
            "annotations": {"readOnlyHint": True, "openWorldHint": False}}


def _rw(name, title, description, props, required):
    t = _ro(name, title, description, props, required)
    t["annotations"] = {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False}
    return t


TOOLS = [
    _rw("pics_index_update", "Обновить индекс картинок",
        "Обойти папку и обновить pic-index.json: у каждой картинки формат, размер, оборудование, состояние, положение и "
        "набор (по имени файла и папок). Неизменённые файлы не перечитываются; full — перечитать все. В ответе: сколько "
        "всего, добавлено, изменено, перемещено, удалено, сколько наборов и первые 50 картинок без распознанного "
        "оборудования — их стоит посмотреть и подписать.",
        {"dir": DIR, "full": {"type": "boolean", "description": "Перечитать все файлы, а не только изменённые"}}, ("dir",)),
    _ro("pics_find", "Найти картинки по смыслу",
        "Наборы картинок по запросу, лучшие первыми: «нория», «elevator», «шнек влево», «задвижка открыта». Понимает "
        "русское и латинское написание, падежи и синонимы. У набора: equipment, base, folder, size (ШxВ, если у всех "
        "одинаковый), states — состояние → файл (относительно dir), files, note. state — только наборы, где есть эти "
        "состояния (через запятую). Индекс при необходимости обновляется сам.",
        {"dir": DIR, "query": {"type": "string", "description": "Что найти: оборудование и уточнения, по-русски или латиницей"},
         "state": {"type": "string", "description": "Нужные состояния через запятую: " + ", ".join(picindex.STATES)},
         "limit": {"type": "integer", "description": "Сколько наборов вернуть (по умолчанию 10, не больше 100)"}},
        ("dir", "query")),
    _ro("pics_list", "Что есть в библиотеке",
        "Обзор для планирования экранов: виды оборудования с числом наборов и картинок, список наборов (имя, папка, какие "
        "состояния есть, размер) и unlabeled — сколько картинок без распознанного оборудования. equipment — только этот вид.",
        {"dir": DIR, "equipment": {"type": "string", "description": "Вид оборудования: «нория», «conveyor»…"}}),
    _rw("pics_label_update", "Подписать картинку",
        "Ручная подпись одной картинки — когда имя ничего не говорит, а ты посмотрел картинку или инженер сказал, что на "
        "ней. Подпись главнее имени, хранится в индексе по содержимому файла и переживает переименование и пересчёт. "
        "Пустая строка стирает поле. В ответе — запись картинки.",
        {"dir": DIR, "file": {"type": "string", "description": "Картинка: полный путь (dir + путь из pics_find); имя файла из самой dir можно без папки"},
         "equipment": {"type": "string", "description": "Оборудование: «нория», «задвижка»…"}, "state": STATE,
         "orientation": {"type": "string", "description": "Положение: " + ", ".join(picindex.ORIENTATIONS)},
         "base": {"type": "string", "description": "Имя набора: картинки одной папки с одним base — один набор"},
         "note": {"type": "string", "description": "Заметка: что на картинке, для чего она"}},
        ("dir", "file")),
    _ro("pics_check", "Проверка библиотеки",
        "Что не так: в наборе нет типовых состояний (есть on, но нет off или alarm), картинки одного набора разного "
        "размера, одинаковые картинки, нераспознанные, тяжёлые для панели (больше 1 МБ или 1024 точек). "
        "Список {level, file или set, text}.", {"dir": DIR}),
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
        if name == "pics_index_update":
            return _text(_json(picindex.refresh(args["dir"], full=bool(args.get("full")))[1]))
        if name == "pics_find":
            return _text(_json(picindex.find(args["dir"], args["query"], args.get("state") or "", args.get("limit") or 10)))
        if name == "pics_list":
            return _text(_json(picindex.overview(args["dir"], args.get("equipment") or "")))
        if name == "pics_label_update":
            fields = {k: args.get(k) for k in ("equipment", "state", "orientation", "base", "note")}
            return _text(_json(picindex.label(args["dir"], args["file"], **fields)))
        if name == "pics_check":
            return _text(_json(picindex.check(args["dir"])))
        return _text(f"нет инструмента {name}", True)
    except picindex.PicError as exc:
        return _text(str(exc), True)
    except KeyError as exc:
        return _text(f"не хватает поля {exc}", True)
    except (TypeError, ValueError) as exc:
        return _text(f"неверное значение: {exc}", True)
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
                  "serverInfo": {"name": "pic-index", "title": "Картинки оборудования для экранов", "version": VERSION},
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
