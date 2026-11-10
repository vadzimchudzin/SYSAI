"""MCP-сервер plc-kit: читает, проверяет и собирает программу ПЛК (Schneider Machine Expert / CODESYS, язык ST).

Запускается синхронизатором на ПК (sysai-agent mcp plc-kit) и говорит по stdio построчным JSON-RPC 2.0. Доступы
(папки на чтение/запись) проверяет сам SYSAI по подписанным правилам до вызова — здесь только работа с файлами.
"""
import csv
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import plcbuild  # noqa: E402
import plcme  # noqa: E402

PROTOCOL = "2025-06-18"
VERSION = "1.0.0"
PATH = {"type": "string", "description": r"Проект ПЛК: файл .export или PLCopen .xml из Machine Expert, либо папка с "
                                         r"файлами .st (plc_export_st), например C:\Projects\line\plc"}
INSTRUCTIONS = (
    "Программа ПЛК (Machine Expert / CODESYS, ST). Читать: plc_info → plc_pous → plc_pou; переменные и адреса — "
    "plc_variables, plc_addresses; теги для панели по Modbus — plc_hmi_tags; как проект построен — plc_style. "
    "Создавать: написать папку файлов .st (plc_export_st показывает пример на готовом проекте), проверить plc_check, "
    "собрать plc_build в PLCopen XML — инженер импортирует его в Machine Expert и компилирует. Файл .project закрыт: "
    "просите .export («Проект → Экспорт…»). Графические языки (LD/FBD/CFC) читаются только как объявления."
)


def _ro(name, title, description, props, required=("path",)):
    return {"name": name, "title": title, "description": description,
            "inputSchema": {"type": "object", "properties": props, "required": list(required), "additionalProperties": False},
            "annotations": {"readOnlyHint": True, "openWorldHint": False}}


def _rw(name, title, description, props, required):
    t = _ro(name, title, description, props, required)
    t["annotations"] = {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False}
    return t


TOOLS = [
    _ro("plc_info", "Сведения о проекте ПЛК",
        "Прочитать проект ПЛК: контроллер, задачи (период, какие программы вызывают), сколько программ, блоков и функций, "
        "сколько на ST и сколько графических, списки глобальных переменных, сколько переменных с адресами и сколько "
        "доступно панели по Modbus. С этого начинают разбор.", {"path": PATH}),
    _ro("plc_pous", "Программы и блоки",
        "Список POU: имя, вид (program / function_block / function), язык, сколько строк, сколько входов и выходов.",
        {"path": PATH}),
    _ro("plc_pou", "Текст программы или блока",
        "Объявление и код ST одного POU, списка глобальных переменных или типа данных — как в Machine Expert.",
        {"path": PATH, "name": {"type": "string", "description": "Имя POU, списка переменных (GVL) или типа"}},
        ("path", "name")),
    _ro("plc_variables", "Переменные",
        "Таблица переменных: где объявлена, имя, тип, адрес (%MX, %MW, %IX, %QX), начальное значение, комментарий, "
        "группа (комментарий-заголовок перед ней), регистр и бит Modbus. filter — подстрока имени или комментария; "
        "scope — только из этого списка или POU; addressed — только с адресом.",
        {"path": PATH, "filter": {"type": "string"}, "scope": {"type": "string"},
         "addressed": {"type": "boolean", "description": "Только переменные с адресом"}}),
    _ro("plc_addresses", "Карта адресов",
        "Занятые диапазоны по областям (%M, %I, %Q), первое свободное слово и бит, как контроллер считает биты (по байтам "
        "или по словам) и пересечения: один адрес у двух переменных, слово %MW поверх битов %MX того же регистра.",
        {"path": PATH}),
    _ro("plc_hmi_tags", "Теги для панели (Modbus)",
        "Переменные, до которых панель дотянется по Modbus (адреса %M): имя, тип, комментарий, регистр, бит и адрес в "
        "записи Weintek (4x — регистр с единицы, 4x_Bit — регистр и две цифры бита). out_file — записать CSV для "
        "импорта и сверки; filter — подстрока имени или комментария.",
        {"path": PATH, "filter": {"type": "string"},
         "out_file": {"type": "string", "description": r"Куда записать CSV (в разрешённой на запись папке)"}}),
    _ro("plc_check", "Проверка",
        "Что видно по тексту до компиляции: незакрытые IF/CASE/FOR, скобки, присваивание без «;», имена, которых нет ни в "
        "POU, ни в GVL, повторные объявления, неизвестные типы, пересечения адресов, задачи, вызывающие несуществующие "
        "программы. Пустой список — не гарантия компиляции: собирает Machine Expert.", {"path": PATH}),
    _ro("plc_style", "Стиль проекта",
        "Как проект построен: программы и их роли, классы механизмов (функциональные блоки: входы, выходы, таймеры), их "
        "экземпляры, правила имён, группы переменных, диапазоны адресов. По этому пишут следующий проект в том же стиле. "
        "out_file — записать страницу Markdown.",
        {"path": PATH, "out_file": {"type": "string", "description": r"Куда записать .md (в разрешённой на запись папке)"}}),
    _rw("plc_export_st", "Проект в папку ST",
        "Разложить проект в папку: по файлу .st на каждый POU, список переменных и тип + project.json (контроллер, задачи). "
        "В таком виде проект читают, правят и пишут с нуля; plc_build собирает папку обратно.",
        {"path": PATH, "out_dir": {"type": "string", "description": r"Папка для файлов .st (в разрешённой на запись)"}},
        ("path", "out_dir")),
    _rw("plc_build", "Собрать PLCopen XML",
        "Собрать папку файлов .st в PLCopen XML для Machine Expert («Проект → Импорт PLCopenXML…»). Сначала проверка "
        "(plc_check): с ошибками файл не пишется, если не force. Графические POU в XML не попадают.",
        {"src_dir": {"type": "string", "description": r"Папка с файлами .st и project.json"},
         "out_file": {"type": "string", "description": r"Куда записать .xml (в разрешённой на запись папке)"},
         "force": {"type": "boolean", "description": "Записать и с ошибками проверки (черновик)"}},
        ("src_dir", "out_file")),
]


def _text(s, is_error=False, data=None):
    r = {"content": [{"type": "text", "text": s}]}
    if is_error:
        r["isError"] = True
    if data is not None:
        r["structuredContent"] = data
    return r


_CACHE = {}


def _open(path):
    path = str(path)
    if os.path.isdir(path):
        return plcbuild.load_dir(path)
    if path.lower().endswith('.project'):
        return plcme.Project.load(path)          # says what to export instead
    st = os.stat(path)
    key = os.path.abspath(path)
    got = _CACHE.get(key)
    if got and got[0] == (st.st_mtime_ns, st.st_size):
        return got[1]
    p = plcme.Project.load(path)
    _CACHE.clear()
    _CACHE[key] = ((st.st_mtime_ns, st.st_size), p)
    return p


def _json(obj):
    return json.dumps(obj, ensure_ascii=False)


def _public(v):
    return {k: v[k] for k in ("scope", "name", "type", "address", "init", "comment", "group", "section", "modbus") if k in v}


def _call(name, args):
    try:
        if name == "plc_build":
            p = plcbuild.load_dir(args["src_dir"])
            issues = p.check()
            errors = [i for i in issues if i["level"] == "error"]
            if errors and not args.get("force"):
                lines = [f"{i['where']}{':' + str(i['line']) if i['line'] else ''} — {i['text']}" for i in errors[:60]]
                return _text("Проверка нашла ошибки, файл не записан:\n" + "\n".join(lines), True,
                             {"errors": errors[:200], "written": False})
            blob, skipped = plcbuild.to_plcopen(p)
            out = args["out_file"]
            os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
            with open(out, "wb") as f:
                f.write(blob)
            warn = [i for i in issues if i["level"] == "warning"]
            text = (f"Записано → {out}: POU {len(p.pous) - len(skipped)}, списков переменных {len(p.gvls)}, типов "
                    f"{len(p.types)}, задач {len(p.tasks)}. Ошибок {len(errors)}, предупреждений {len(warn)}. "
                    "Импорт: Machine Expert → выделить Application → «Проект → Импорт PLCopenXML…»; задачу проверьте "
                    "в «Конфигурации задач». Компиляцию и загрузку делает инженер.")
            return _text(text, data={"written": True, "out_file": out, "errors": errors[:200], "warnings": warn[:200],
                                     "skipped": skipped})
        p = _open(args["path"])
        if name == "plc_info":
            return _text(_json(p.info()))
        if name == "plc_pous":
            import plcst
            rows = []
            for x in p.pous:
                d = plcst.parse_declaration(x["declaration"])
                rows.append({"name": x["name"], "kind": x["kind"], "language": x["language"],
                             "lines": len(x["body"].splitlines()),
                             "inputs": sum(1 for v in d["vars"] if v["section"] == "VAR_INPUT"),
                             "outputs": sum(1 for v in d["vars"] if v["section"] == "VAR_OUTPUT"),
                             "parts": [c["name"] for c in x.get("children", [])]})
            return _text(_json({"pous": rows, "global_lists": [g["name"] for g in p.gvls], "types": [t["name"] for t in p.types]}))
        if name == "plc_pou":
            want = str(args["name"]).lower()
            for g in p.gvls + p.types:
                if g["name"].lower() == want:
                    return _text(g["declaration"])
            x = p.pou(args["name"])
            if x["language"] != "st":
                uses = "\n".join(x.get("uses", [])[:600])
                return _text(x["declaration"] + "\n\n// Графический язык: кода текстом нет. Имена в схеме:\n" + uses)
            parts = "".join(f"\n\n// --- {c['part']} {c['name']}\n{c['declaration']}\n{c['body']}" for c in x.get("children", []))
            return _text(x["declaration"].rstrip("\n") + "\n\n" + x["body"] + parts)
        if name == "plc_variables":
            flt, scope = (args.get("filter") or "").lower(), (args.get("scope") or "").lower()
            rows = [v for v in p.variables()
                    if (not flt or flt in v["name"].lower() or flt in (v["comment"] or "").lower())
                    and (not scope or v["scope"].lower() == scope) and (not args.get("addressed") or v["address"])]
            return _text(_json({"count": len(rows), "variables": [_public(v) for v in rows[:3000]]}))
        if name == "plc_addresses":
            a = p.addresses()
            return _text(_json({"bit_numbering": a["bit_numbering"], "ranges": a["ranges"], "free": a["free"],
                                "conflicts": a["conflicts"][:500], "addressed": len(a["variables"])}))
        if name == "plc_hmi_tags":
            flt = (args.get("filter") or "").lower()
            rows = [t for t in p.hmi_tags() if not flt or flt in t["name"].lower() or flt in t["comment"].lower() or flt in t["group"].lower()]
            note = ""
            if args.get("out_file"):
                buf = io.StringIO()
                w = csv.writer(buf, delimiter=";", lineterminator="\n")
                w.writerow(["name", "type", "kind", "address", "register", "bit", "weintek_type", "weintek_address", "group", "comment"])
                for t in rows:
                    w.writerow([t["name"], t["type"], t["kind"], t["address"], t["register"], "" if t["bit"] is None else t["bit"],
                                t["weintek"]["type"], t["weintek"]["address"], t["group"], t["comment"]])
                os.makedirs(os.path.dirname(os.path.abspath(args["out_file"])), exist_ok=True)
                with open(args["out_file"], "w", encoding="utf-8-sig", newline="") as f:
                    f.write(buf.getvalue())
                note = f" CSV → {args['out_file']}"
            return _text(_json({"count": len(rows), "note": "регистры Modbus с нуля; у Weintek с единицы." + note,
                                "tags": rows[:3000]}))
        if name == "plc_check":
            issues = p.check()
            levels = {k: sum(1 for i in issues if i["level"] == k) for k in ("error", "warning", "info")}
            return _text(_json({"summary": levels, "issues": issues[:2000]}))
        if name == "plc_style":
            st = p.style()
            md = plcme.style_markdown(st)
            if args.get("out_file"):
                os.makedirs(os.path.dirname(os.path.abspath(args["out_file"])), exist_ok=True)
                with open(args["out_file"], "w", encoding="utf-8", newline="\n") as f:
                    f.write(md)
            return _text(md, data=st)
        if name == "plc_export_st":
            files = plcbuild.write_dir(p, args["out_dir"])
            return _text(f"Записано файлов: {len(files)} → {args['out_dir']}", data={"files": files})
        return _text(f"нет инструмента {name}", True)
    except plcme.PlcError as exc:
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
                  "serverInfo": {"name": "plc-kit", "title": "ПЛК: Machine Expert, ST", "version": VERSION},
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
