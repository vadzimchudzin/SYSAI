"""MCP-сервер machine-expert: проект EcoStruxure Machine Expert из папки ST, компиляция с ошибками компилятора,
экспорт закрытого .project, связь с контроллером.

Запускается синхронизатором на ПК (sysai-agent mcp machine-expert) и говорит по stdio построчным JSON-RPC 2.0.
Сам Machine Expert делает работу своим скриптовым интерфейсом (Python внутри программы): сервер пишет задание,
запускает программу без окна со скриптом me_job.py и читает результат. Доступы (папки на чтение/запись) проверяет
SYSAI по подписанным правилам до вызова.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mehost  # noqa: E402
import mesrc  # noqa: E402

PROTOCOL = "2025-06-18"
VERSION = "0.1.0"
FILE = {"type": "string", "description": r"Файл проекта Machine Expert, например C:\Projects\line\plc\Line.project"}
SRC = {"type": "string", "description": r"Папка с файлами .st и project.json (её пишут и проверяют с plc-kit)"}
TIMEOUT = {"type": "integer", "minimum": 30, "maximum": 3600,
           "description": "Сколько секунд ждать Machine Expert (по умолчанию 900: программа запускается долго)"}
INSTRUCTIONS = (
    "EcoStruxure Machine Expert на этом ПК. Проект с нуля: папка ST (plc-kit: plc_check без ошибок) → me_create "
    "(шаблон проекта компании template_file или модель контроллера) → в ответе ошибки компилятора с файлом и строкой → "
    "исправить файлы .st → me_update → повторять до нуля ошибок. me_build — только компиляция. me_export — достать "
    "программу из любого .project (и закрытого) в папку ST или PLCopen XML. me_scan — контроллеры в сети; me_online — "
    "вход в контроллер или симуляцию: загрузка, пуск, чтение и запись переменных. Загрузка, пуск, стоп и запись в "
    "настоящий контроллер — только с confirm и только после слов инженера «загружай»: это работающее оборудование. "
    "me_find — что установлено; me_devices — контроллеры репозитория; me_open — открыть проект инженеру."
)


def _ro(name, title, description, props, required=()):
    return {"name": name, "title": title, "description": description,
            "inputSchema": {"type": "object", "properties": props, "required": list(required), "additionalProperties": False},
            "annotations": {"readOnlyHint": True, "openWorldHint": False}}


def _rw(name, title, description, props, required, destructive=False):
    t = _ro(name, title, description, props, required)
    t["annotations"] = {"readOnlyHint": False, "destructiveHint": destructive, "openWorldHint": False}
    return t


TOOLS = [
    _ro("me_find", "Где Machine Expert",
        "Какие Machine Expert (или CODESYS) установлены на этом ПК и чем будут запускаться скрипты: программа, версия, "
        "профиль. С этого начинают: если пусто — компиляции не будет, скажи инженеру.", {}),
    _ro("me_devices", "Контроллеры репозитория",
        "Устройства из репозитория Machine Expert, в имени которых есть filter (например TM241CE40): имя, версия, "
        "идентификатор. Чтобы назвать контроллер в me_create точно так, как его знает программа.",
        {"filter": {"type": "string", "description": "Часть имени, например TM241"}, "timeout_s": TIMEOUT}, ("filter",)),
    _rw("me_create", "Создать проект Machine Expert",
        "Новый файл .project из папки ST: объекты (типы, списки переменных, блоки, программы), задачи из project.json, "
        "при связи OPC UA — «Конфигурация символов»; затем компиляция. Основа — template_file (шаблон проекта компании: "
        "контроллер, модули, библиотеки уже настроены — так надёжнее) или controller (модель из репозитория, например "
        "TM241CE40R; без неё берётся controller из project.json). В ответе: что создано и ошибки компилятора с файлом "
        ".st и строкой.",
        {"src_dir": SRC, "out_file": {"type": "string", "description": r"Куда записать .project (в разрешённой на запись папке)"},
         "template_file": {"type": "string", "description": "Шаблон: готовый .project с контроллером и модулями"},
         "controller": {"type": "string", "description": "Модель контроллера, если шаблона нет"},
         "device_name": {"type": "string", "description": "Имя контроллера в дереве (по умолчанию MyController)"},
         "overwrite": {"type": "boolean", "description": "Заменить файл, если он уже есть"}, "timeout_s": TIMEOUT},
        ("src_dir", "out_file")),
    _rw("me_update", "Обновить проект из папки ST",
        "Записать в существующий .project текущие файлы .st (новые объекты создаются, изменённые получают новый текст; "
        "остальное в проекте — модули, настройки — не трогается), обновить задачи и скомпилировать. Круг исправления "
        "ошибок: поправил .st → me_update → смотри ошибки.",
        {"file": FILE, "src_dir": SRC, "timeout_s": TIMEOUT}, ("file", "src_dir")),
    _ro("me_build", "Компиляция",
        "Скомпилировать проект в Machine Expert и вернуть сообщения компилятора: ошибки и предупреждения с объектом и "
        "строкой. src_dir — папка ST этого проекта: тогда у сообщений будет файл .st и строка в нём.",
        {"file": FILE, "src_dir": SRC, "timeout_s": TIMEOUT}, ("file",)),
    _rw("me_export", "Достать программу из .project",
        "Открыть .project в Machine Expert (в том числе закрытый, который не читается как файл) и записать программу: "
        "out_dir — папкой файлов .st с project.json (её читает plc-kit: стиль, проверка, теги) или out_file — PLCopen "
        "XML. Графические POU (LD/FBD/CFC) в папку ST попадают только объявлением.",
        {"file": FILE, "out_dir": {"type": "string", "description": "Папка для файлов .st"},
         "out_file": {"type": "string", "description": "Файл PLCopen .xml"}, "timeout_s": TIMEOUT}, ("file",)),
    _ro("me_scan", "Контроллеры в сети",
        "Найти контроллеры в сети через шлюз Machine Expert: имя узла, тип, адрес. Имя или адрес потом называют в "
        "me_online.", {"gateway": {"type": "string", "description": "Имя шлюза, если их несколько"}, "timeout_s": TIMEOUT}),
    _rw("me_online", "Контроллер: вход, загрузка, переменные",
        "Войти в контроллер проекта и выйти: simulation — встроенная симуляция Machine Expert (без оборудования: "
        "проект загружается в неё, можно читать и писать переменные); иначе настоящий контроллер — ip, node (имя узла "
        "из me_scan) или address, либо как записано в проекте. download — загрузить проект (иначе вход без загрузки), "
        "start / stop — пуск и стоп приложения, write — записать значения {\"GVL.SP_1\": \"50\"}, read — прочитать "
        "список переменных (так проверяют, что все переменные панели есть в контроллере). Для настоящего контроллера "
        "download, start, stop и write требуют confirm: true — только после явного согласия инженера.",
        {"file": FILE, "simulation": {"type": "boolean"},
         "ip": {"type": "string", "description": "IP-адрес контроллера"},
         "node": {"type": "string", "description": "Имя узла контроллера (из me_scan)"},
         "address": {"type": "string", "description": "Адрес в записи шлюза, например 0301.A0C3"},
         "gateway": {"type": "string"},
         "download": {"type": "boolean"}, "start": {"type": "boolean"}, "stop": {"type": "boolean"},
         "read": {"type": "array", "items": {"type": "string"}, "maxItems": 2000,
                  "description": "Переменные: GVL.Start_1, Main.step"},
         "write": {"type": "object", "description": "Переменная → значение (текстом, как в Machine Expert: TRUE, 12, 3.5)"},
         "user": {"type": "string"}, "password": {"type": "string"},
         "confirm": {"type": "boolean", "description": "Инженер разрешил менять настоящий контроллер"},
         "timeout_s": TIMEOUT}, ("file",), destructive=True),
    _ro("me_open", "Открыть проект инженеру",
        "Открыть .project в окне Machine Expert — чтобы инженер посмотрел и проверил сам.", {"file": FILE}, ("file",)),
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


def _build_text(build: dict, source=None) -> tuple:
    """(lines for the agent, the build with files and lines put in)."""
    rows = {"errors": [], "warnings": []}
    for key in rows:
        for m in build.get(key) or []:
            rows[key].append(mesrc.place(source, m) if source else m)
    c = build.get("counts") or {}
    lines = [f"Компиляция: ошибок {c.get('error', 0)}, предупреждений {c.get('warning', 0)}."]
    for key, word in (("errors", "ОШИБКА"), ("warnings", "предупреждение")):
        for m in rows[key][:40 if key == "errors" else 15]:
            at = (f"{m['file']}:{m['file_line']}" if m.get("file_line") else m.get("file") or m.get("object") or "проект")
            where = f" ({m['object']}, {m['where']})" if m.get("where") and m.get("object") else ""
            lines.append(f"{word} {at} — {(m.get('code') + ': ') if m.get('code') else ''}{m['text']}{where}")
    return lines, dict(build, **rows)


def _run(job: dict, args: dict) -> dict:
    out = mehost.run(job, float(args.get("timeout_s") or mehost.DEFAULT_TIMEOUT))
    if not out.get("ok"):
        raise mehost.HostError(str(out.get("error") or "Machine Expert вернул ошибку без текста")
                               + ("\n" + "\n".join(out["warnings"]) if out.get("warnings") else ""))
    return out


def _notes(out: dict) -> list:
    return [f"Замечание: {w}" for w in out.get("warnings") or []]


def _call(name, args):
    try:
        if name == "me_find":
            found = mehost.installs()
            if not found:
                return _text("Machine Expert на этом ПК не найден. Компиляция и связь с контроллером недоступны: скажи "
                             "инженеру поставить EcoStruxure Machine Expert или назвать программу (переменная SYSAI_ME_EXE).",
                             data={"installs": []})
            lines = [f"{i['product']} {i['version']} — {i['exe']}" + (f" (профиль {i['profile']})" if i["profile"] else "")
                     for i in found]
            return _text("\n".join(lines), data={"installs": found, "commands": [t for t, _ in mehost.commands(mehost.SCRIPT)]})
        if name == "me_devices":
            out = _run({"action": "devices", "filter": str(args["filter"])}, args)
            return _text(_json(out["result"]), data=out["result"])
        if name in ("me_create", "me_update"):
            src = mesrc.load_dir(args["src_dir"])
            job = {"action": "create" if name == "me_create" else "update", "objects": src["objects"],
                   "tasks": src["tasks"], "link": src["link"]}
            if name == "me_create":
                out_file = str(args["out_file"])
                if not out_file.lower().endswith(".project"):
                    return _text("out_file: нужен файл с расширением .project", True)
                controller = str(args.get("controller") or src["controller"] or "")
                if not args.get("template_file") and not controller:
                    return _text("Нужна основа проекта: template_file (шаблон .project с контроллером и модулями) или "
                                 "controller (модель, например TM241CE40R) — либо controller в project.json.", True)
                job.update(file=os.path.abspath(out_file), template_file=str(args.get("template_file") or ""),
                           controller=controller, device_name=str(args.get("device_name") or ""),
                           overwrite=bool(args.get("overwrite")))
            else:
                job["file"] = os.path.abspath(str(args["file"]))
            out = _run(job, args)
            res = out["result"]
            lines, build = _build_text(res["build"], src)
            res["build"] = build
            o = res["objects"]
            head = (f"{'Создан' if name == 'me_create' else 'Обновлён'} {res['file']}"
                    + (f" ({res['from']})" if res.get("from") else "") + f": объектов создано {len(o['created'])}, "
                    f"изменено {len(o['updated'])}, без изменений {len(o['same'])}; задачи: {', '.join(res['tasks']) or 'не заданы'}"
                    + (f"; конфигурация символов OPC UA: {res['symbols']}" if res.get("symbols") else "") + ".")
            extra = [f"В проекте есть объекты, которых нет в папке ST: {', '.join(o['extra'][:30])}."] if o.get("extra") else []
            tail = (["Ошибок нет: проект компилируется. Отметь этап build (plc_stage_update) и покажи файл инженеру (me_open)."]
                    if build["ok"] else ["Исправь файлы .st по списку и вызови me_update — до нуля ошибок."])
            return _text("\n".join([head] + extra + lines + _notes(out) + tail), data=res)
        if name == "me_build":
            src = mesrc.load_dir(args["src_dir"]) if args.get("src_dir") else None
            out = _run({"action": "build", "file": os.path.abspath(str(args["file"]))}, args)
            lines, build = _build_text(out["result"]["build"], src)
            out["result"]["build"] = build
            return _text("\n".join(lines + _notes(out)), data=out["result"])
        if name == "me_export":
            if bool(args.get("out_dir")) == bool(args.get("out_file")):
                return _text("Назови одно: out_dir (папка файлов .st) или out_file (PLCopen .xml).", True)
            job = {"action": "export", "file": os.path.abspath(str(args["file"]))}
            if args.get("out_dir"):
                job.update(format="st", out_dir=os.path.abspath(str(args["out_dir"])))
            else:
                job.update(format="plcopen", out_file=os.path.abspath(str(args["out_file"])))
            out = _run(job, args)
            res = out["result"]
            if res.get("out_dir"):
                text = (f"Программа из {res['file']} → {res['out_dir']}: файлов .st {len(res['files'])}, задач "
                        f"{len(res['tasks'])}. Дальше — plc-kit по этой папке (plc_info, plc_style, plc_check).")
                if res.get("skipped"):
                    text += "\nНе записано текстом: " + "; ".join(res["skipped"][:40])
            else:
                text = f"PLCopen XML → {res['out_file']}. Его читает plc-kit (plc_info)."
            return _text("\n".join([text] + _notes(out)), data=res)
        if name == "me_scan":
            out = _run({"action": "scan", "gateway": str(args.get("gateway") or "")}, args)
            res = out["result"]
            lines = [f"Шлюз {res['gateway']}: контроллеров {res['count']}."] + \
                    [f"- {c['name']} — {c['type']} ({c['vendor']}), адрес {c['address']}" for c in res["controllers"][:60]]
            return _text("\n".join(lines + _notes(out)), data=res)
        if name == "me_online":
            real = not args.get("simulation")
            changes = [k for k in ("download", "start", "stop") if args.get(k)] + (["write"] if args.get("write") else [])
            if real and changes and not args.get("confirm"):
                return _text("Это настоящий контроллер, а " + ", ".join(changes) + " меняют работающее оборудование. "
                             "Спроси инженера и повтори с confirm: true только после его явного «да». Без оборудования "
                             "проверяй в симуляции: simulation: true.", True)
            write = args.get("write") or {}
            if not isinstance(write, dict):
                return _text("write: объект {переменная: значение}", True)
            job = {"action": "online", "file": os.path.abspath(str(args["file"])), "simulation": not real,
                   "download": bool(args.get("download")), "start": bool(args.get("start")), "stop": bool(args.get("stop")),
                   "read": [str(x) for x in args.get("read") or []], "write": {str(k): v for k, v in write.items()}}
            for key in ("ip", "node", "address", "gateway", "user", "password"):
                if args.get(key):
                    job[key] = str(args[key])
            out = _run(job, args)
            res = out["result"]
            lines = [f"Контроллер {res['controller']}: {res['target']}."]
            if res.get("build") and not res["build"]["ok"]:
                blines, res["build"] = _build_text(res["build"])
                return _text("\n".join(lines + [res.get("note") or ""] + blines + _notes(out)), True, res)
            lines.append(("Вход выполнен" + (", проект загружен" if res.get("downloaded") else ", без загрузки")
                          + (f", состояние: {res['state']}" if res.get("state") else "") + ".") if res.get("logged_in")
                         else "Вход не выполнен.")
            for title, key in (("Записано", "written"), ("Прочитано", "values")):
                rows = res.get(key) or {}
                bad = {k: v for k, v in rows.items() if not v["ok"]}
                if rows:
                    lines.append(f"{title}: {len(rows) - len(bad)} из {len(rows)}.")
                    lines += [f"- {k} = {v['value']}" for k, v in list(rows.items())[:60] if v["ok"]]
                    lines += [f"- НЕТ {k}: {v['error']}" for k, v in list(bad.items())[:60]]
            return _text("\n".join(lines + _notes(out)), data=res)
        if name == "me_open":
            how = mehost.open_in_program(os.path.abspath(str(args["file"])))
            return _text(f"Открыто в Machine Expert: {args['file']} ({how}).")
        return _text(f"нет инструмента {name}", True)
    except (mehost.HostError, mesrc.SourceError) as exc:
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
                  "serverInfo": {"name": "machine-expert", "title": "Machine Expert: проект, компиляция, контроллер",
                                 "version": VERSION},
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
