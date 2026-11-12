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
import plclink  # noqa: E402
import plcme  # noqa: E402
import plcproj  # noqa: E402
import plcspec  # noqa: E402
import plcstage  # noqa: E402

PROTOCOL = "2025-06-18"
VERSION = "1.5.0"
PATH = {"type": "string", "description": r"Проект ПЛК: файл .project Machine Expert, .export или PLCopen .xml из него, "
                                         r"либо папка с файлами .st (plc_export_st), например C:\Projects\line\plc"}
PROJECT = {"type": "string", "description": r"Файл проекта Machine Expert, например C:\Projects\line\plc\Line.project"}
INSTRUCTIONS = (
    "Программа ПЛК (Machine Expert / CODESYS, ST). Читать: plc_info → plc_pous → plc_pou; переменные и адреса — "
    "plc_variables, plc_addresses; теги для панели по Modbus — plc_hmi_tags; как проект построен — plc_style. "
    "Создавать: написать папку файлов .st (plc_export_st показывает пример на готовом проекте), проверить plc_check, "
    "собрать plc_build в PLCopen XML. До программы — plc_link: по моделям контроллера и панели из ТЗ решает связь "
    "(OPC UA или Modbus) и что из этого следует для переменных; plc_hmi_tags даёт теги панели под выбранную связь. "
    "Ход проекта «под ключ» отмечают plc_stage_update (его показывает окно «Агент SYSAI»). Файл .project Machine "
    "Expert читается и пишется здесь, без программы: все инструменты чтения работают прямо на нём; plc_project_build "
    "делает настоящий .project из папки ST и шаблона (готового проекта с тем же контроллером; plc_project_template "
    "вынимает из него программу) или записывает изменённые .st в существующий проект; plc_io — каналы контроллера и "
    "модулей с переменными, plc_io_map — поставить переменные на каналы. Компиляцию и загрузку в контроллер делает "
    "Machine Expert (инструмент machine-expert). Графические языки (LD/FBD/CFC) читаются как объявления и имена."
)


PAGE = 200       # rows of a table in one answer: the whole answer stays in the conversation and is paid for again
PAGE_MAX = 1000  # with every next request — so a table comes in pages, the whole of it goes to a file (out_file)
PAGING = {"limit": {"type": "integer", "minimum": 1, "maximum": PAGE_MAX,
                    "description": f"Сколько строк вернуть (по умолчанию {PAGE}, не больше {PAGE_MAX})"},
          "offset": {"type": "integer", "minimum": 0, "description": "С какой строки (0 — с первой): для следующей страницы"}}


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
        "доступно панели по Modbus. У файла .project — ещё дерево объектов, устройства, сколько каналов ввода-вывода "
        "занято переменными и сколько переменных опубликовано в OPC UA. С этого начинают разбор.", {"path": PATH}),
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
        f"scope — только из этого списка или POU; addressed — только с адресом. Отвечает страницами по {PAGE} строк "
        "(count — сколько всего): ищите нужное через filter и scope, а не листайте всю таблицу.",
        {"path": PATH, "filter": {"type": "string"}, "scope": {"type": "string"},
         "addressed": {"type": "boolean", "description": "Только переменные с адресом"}, **PAGING}),
    _ro("plc_addresses", "Карта адресов",
        "Занятые диапазоны по областям (%M, %I, %Q), первое свободное слово и бит, как контроллер считает биты (по байтам "
        "или по словам) и пересечения: один адрес у двух переменных, слово %MW поверх битов %MX того же регистра.",
        {"path": PATH}),
    _ro("plc_hmi_tags", "Теги для панели",
        "Переменные, которые увидит панель. Modbus (адреса %M): имя, тип, комментарий, регистр, бит и адрес в записи "
        "Weintek (4x — регистр с единицы, 4x_Bit — регистр и две цифры бита). OPC UA (link: opcua или так сказано в "
        "project.json): переменные простых типов из опубликованных списков — имя «GVL.Имя», тип и node; поле "
        "add_tags готово для cmtp_edit (ebpro-cmtp). out_file — записать CSV со всеми тегами (так и берите всю "
        f"таблицу: в ответе — страница из {PAGE} строк и count); filter — подстрока имени, комментария или группы.",
        {"path": PATH, "filter": {"type": "string"}, **PAGING,
         "link": {"type": "string", "enum": ["modbus", "opcua"], "description": "Связь; по умолчанию — из project.json, иначе Modbus"},
         "out_file": {"type": "string", "description": r"Куда записать CSV (в разрешённой на запись папке)"}}),
    _ro("plc_tag_check", "Теги панели против программы",
        "Сверить теги панели с программой ПЛК (связь OPC UA): каких переменных в программе нет (missing), у каких другой "
        "тип (type), у каких номер node не тот, что даёт контроллер M241 / M251 этому имени (node), какие не простого "
        "типа (not_plain). tags — имена или записи {name, type, node} (ответ cmtp_tags инструмента ebpro-cmtp); "
        "tags_file — файл JSON с ними. Перед сдачей экранов список missing должен быть пуст.",
        {"path": PATH, "tags": {"type": "array", "items": {"type": ["string", "object"]}},
         "tags_file": {"type": "string", "description": "Файл JSON: список тегов или {tags: [...]}"}}),
    _ro("plc_check", "Проверка",
        "Что видно по тексту до компиляции: незакрытые IF/CASE/FOR, скобки, присваивание без «;», имена, которых нет ни в "
        "POU, ни в GVL, повторные объявления, неизвестные типы, пересечения адресов, задачи, вызывающие несуществующие "
        f"программы. Пустой список — не гарантия компиляции: собирает Machine Expert. Сначала ошибки, по {PAGE} "
        "замечаний на страницу; summary — сколько всего.", {"path": PATH, **PAGING}),
    _ro("plc_style", "Стиль проекта",
        "Как проект построен: программы и их роли, классы механизмов (функциональные блоки: входы, выходы, таймеры), их "
        "экземпляры, правила имён, группы переменных, диапазоны адресов. По этому пишут следующий проект в том же стиле. "
        "out_file — записать страницу Markdown.",
        {"path": PATH, "out_file": {"type": "string", "description": r"Куда записать .md (в разрешённой на запись папке)"}}),
    _ro("plc_link", "Связь панели и контроллера",
        "По моделям контроллера и панели решить, как панель говорит с ПЛК: OPC UA (оба умеют — переменные по именам, "
        "без адресов) или Modbus TCP / RTU (адреса %M). Модели можно назвать (controller, panel) или дать текст ТЗ "
        "(text) — инструмент найдёт в нём модели и слова о связи; wish — что просит заказчик. В ответе: link, почему, "
        "разрешение панели, что записать в project.json, что делать дальше и что спросить у инженера (ask), если "
        "решить нельзя. Вызывают до написания программы.",
        {"controller": {"type": "string", "description": "Модель контроллера, например TM241CE40R"},
         "panel": {"type": "string", "description": "Модель панели, например cMT3072X или MT8071iE"},
         "wish": {"type": "string", "description": "Связь, названная заказчиком: OPC UA, Modbus TCP, Modbus RTU"},
         "text": {"type": "string", "description": "Текст ТЗ или спецификации: модели и связь будут найдены в нём"}}, ()),
    _rw("plc_stage_update", "Отметить этап проекта",
        "Ход проекта «под ключ» в pipeline.json папки проекта — его показывает окно «Агент SYSAI» полосой этапов: "
        "materials, decisions, plc, build, hmi, docs. state: work (начал), done (готов), problem (нужен инженер — в "
        "note что именно), todo. facts — решения и итоги: controller, panel, resolution, link, style, machine_expert, "
        "project_file, hmi_file, doc_file. Без stage — только прочитать ход и записать facts. В ответе extra — лишние "
        "файлы в plc/, hmi/, docs/ (там только результат: .project, проект панели, .docx): перенесите их в _work/.",
        {"project_dir": {"type": "string", "description": r"Папка проекта, например C:\Users\me\Documents\SYSAI Проекты\Линия 1"},
         "stage": {"type": "string", "enum": [k for k, _, _ in plcstage.STAGES]},
         "state": {"type": "string", "enum": list(plcstage.STATES)},
         "note": {"type": "string", "description": "Одна-две фразы: что сделано или что мешает"},
         "facts": {"type": "object", "description": "Например {\"controller\": \"TM241CE40R\", \"link\": \"opcua\"}"}},
        ("project_dir",)),
    _rw("plc_spec_new", "Новая спецификация проекта",
        "Создать пустую спецификацию проекта — _work/spec/project.spec.json: project (name, controller, panel, link, "
        "template), expected (сколько механизмов и сигналов на схеме — пересчитайте по схеме до того, как выписывать), "
        "types (виды механизмов: какие сигналы in/out у вида обязаны быть, какие optional), mechanisms ({id, type, title, "
        "in: {сигнал: \"Устройство.Канал\"}, out, params, interlocks: {requires_running, stops}, src}), signals (сигналы "
        "вне механизмов: {id, title, dir, channel, src}). В файле два вида механизмов как образец записи. Заполняйте его "
        "по материалам вместо длинных выписок в разговоре: программа, каналы и экраны делаются по нему.",
        {"project_dir": {"type": "string", "description": "Папка проекта: файл ляжет в _work/spec/project.spec.json"},
         "out_file": {"type": "string", "description": "Или свой путь к файлу .json"},
         "overwrite": {"type": "boolean"}}, ()),
    _ro("plc_spec_check", "Проверка спецификации",
        "Проверить спецификацию проекта: поля, повторы id, полнота (на схеме expected.mechanisms / expected.signals — "
        "столько же в спецификации; у каждого механизма есть все сигналы его вида), каналы («Устройство.Канал», один "
        "канал — один сигнал, вход не на выходе), блокировки называют существующие механизмы. template_file — шаблон "
        "или собранный .project: тогда каждый канал ищется в контроллере (есть ли, вход или выход) и считается, сколько "
        f"каналов свободно. В ответе summary и замечания, сначала ошибки, по {PAGE} на страницу. io_map: true — "
        "добавить готовые changes для plc_io_map (переменная «<id>_<сигнал>»). Этап materials закрывают при "
        "summary.complete.",
        {"project_dir": {"type": "string", "description": "Папка проекта (спецификация — _work/spec/project.spec.json)"},
         "file": {"type": "string", "description": "Или путь к файлу спецификации"},
         "template_file": {"type": "string", "description": "Шаблон или проект .project — сверить каналы с контроллером"},
         "io_map": {"type": "boolean", "description": "Вернуть changes для plc_io_map"}, **PAGING}, ()),
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
    _rw("plc_project_build", "Проект Machine Expert из папки ST",
        "Записать папку ST в настоящий файл .project — без Machine Expert. С template_file — новый проект: шаблон (файл "
        ".project с тем же контроллером, модулями и библиотеками; годится готовый проект прошлых лет или шаблон из "
        "plc_project_template) + программа из папки: типы, списки переменных, функции, блоки, программы, задачи из "
        "project.json, при связи OPC UA — список публикуемых переменных; программа самого шаблона вынимается "
        "(keep_program — оставить и сопоставить по именам). Без template_file — обновление: out_file уже есть, "
        "изменённые .st получают новый текст, новые объекты добавляются, остальное в проекте не трогается. Сначала "
        "plc_check: с ошибками файл не пишется — исправьте .st. После записи файл читается обратно и сверяется с "
        "папкой, а каналы ввода-вывода — с программой: шаблон хранит на каналах переменные своей прежней программы, "
        "и канал с переменной, которой в новой программе нет, Machine Expert не скомпилирует (io: drop_undeclared "
        "снимает такие, clear — все). Компилирует Machine Expert: откройте файл в нём (machine-expert: me_build).",
        {"src_dir": {"type": "string", "description": r"Папка с файлами .st и project.json"},
         "out_file": {"type": "string", "description": r"Файл .project: куда записать новый или какой обновить"},
         "template_file": {"type": "string", "description": "Шаблон: .project с контроллером и модулями (для нового проекта)"},
         "keep_program": {"type": "boolean", "description": "Оставить программу шаблона (объекты сопоставляются по именам)"},
         "overwrite": {"type": "boolean", "description": "Заменить out_file, если он уже есть (для нового проекта)"},
         "io": {"type": "string", "enum": list(plcproj.IO_MODES),
                "description": "Переменные шаблона на каналах: keep — оставить (по умолчанию), drop_undeclared — снять "
                               "те, которых нет в программе, clear — снять все"}},
        ("src_dir", "out_file")),
    _rw("plc_project_template", "Шаблон из готового проекта",
        "Сделать из готового .project шаблон: программа (программы, блоки, списки, типы, папки) вынимается, остаются "
        "контроллер с модулями и их настройками, библиотеки, конфигурация задач, настройки проекта. Каналы "
        "ввода-вывода сохраняют переменные прежней программы — посмотрите plc_io и объявите их в новой программе или "
        "переназначьте plc_io_map; clear_io — снять переменные со всех каналов (чистый шаблон).",
        {"path": PROJECT, "out_file": {"type": "string", "description": r"Куда записать шаблон .project"},
         "overwrite": {"type": "boolean"},
         "clear_io": {"type": "boolean", "description": "Снять переменные со всех каналов ввода-вывода"}}, ("path", "out_file")),
    _ro("plc_io", "Каналы ввода-вывода",
        "Каналы контроллера и модулей из файла .project с переменными на них: устройство, канал (I0, Q3, IW0…), "
        "направление, тип, описание, переменная. «Application.Имя» — переменная программы; new_variable — переменную "
        "объявляет сама привязка. device — только это устройство; free — только свободные каналы; filter — подстрока "
        f"канала, переменной или описания. Отвечает страницами по {PAGE} строк (count — сколько всего).",
        {"path": PROJECT, "device": {"type": "string"}, "free": {"type": "boolean"}, "filter": {"type": "string"}, **PAGING}),
    _rw("plc_io_map", "Переменные на каналы",
        "Поставить переменные программы на каналы устройств в файле .project: changes — [{device, channel, variable}] "
        "(variable — имя переменной программы, например x0_0 или In_Out.Start; пусто — снять). Переменная должна быть "
        "объявлена в программе (в списке без qualified_only — по короткому имени). out_file — куда записать (можно "
        "тот же файл).",
        {"path": PROJECT, "out_file": {"type": "string", "description": "Куда записать (можно тот же файл)"},
         "changes": {"type": "array", "minItems": 1, "maxItems": 2000, "items": {
             "type": "object", "properties": {"device": {"type": "string"}, "channel": {"type": "string"},
                                              "variable": {"type": "string"}}, "required": ["device", "channel"]}}},
        ("path", "out_file", "changes")),
]


def _page(rows, args):
    """(the rows of the page, what to say about the rest) — «count» is always the whole table."""
    try:
        limit = min(PAGE_MAX, max(1, int(args.get("limit") or PAGE)))
        offset = max(0, int(args.get("offset") or 0))
    except (TypeError, ValueError):
        limit, offset = PAGE, 0
    part = rows[offset:offset + limit]
    meta = {"count": len(rows), "offset": offset, "returned": len(part)}
    if offset + len(part) < len(rows):
        meta["next_offset"] = offset + len(part)
        meta["more"] = (f"показаны строки {offset + 1}–{offset + len(part)} из {len(rows)}: сузьте filter или возьмите "
                        f"следующую страницу offset={offset + len(part)}; целиком таблицу в разговор не выгружайте")
    return part, meta


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
    try:
        st = os.stat(path)
    except OSError:
        return plcme.Project.load(path)          # says why the file is not read
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
                    "в «Конфигурации задач». Проект .project, компиляцию и ошибки компилятора даёт инструмент "
                    "machine-expert (me_create по папке ST, me_build).")
            return _text(text, data={"written": True, "out_file": out, "errors": errors[:200], "warnings": warn[:200],
                                     "skipped": skipped})
        if name == "plc_link":
            d = plclink.decide(args.get("controller") or "", args.get("panel") or "", args.get("wish") or "", args.get("text") or "")
            head = (f"Связь: {d['link_ru']}" + ("" if d["sure"] else " (уточнить)") if d["link"] else "Связь решить нельзя")
            lines = [head] + [f"— {w}" for w in d["why"]] + [f"Спросить: {a}" for a in d["ask"]] + d["next"]
            return _text("\n".join(lines), data=d)
        if name in ("plc_spec_new", "plc_spec_check"):
            file = args.get("out_file" if name == "plc_spec_new" else "file") or (
                plcspec.path_of(args["project_dir"]) if args.get("project_dir") else "")
            if not file:
                return _text("Назовите папку проекта (project_dir) или файл спецификации.", True)
            if name == "plc_spec_new":
                name_of = os.path.basename(os.path.normpath(args["project_dir"])) if args.get("project_dir") else ""
                plcspec.new(file, name_of, bool(args.get("overwrite")))
                return _text(f"Спецификация → {file}. Заполните project, expected (счёт по схеме), types, mechanisms, "
                             "signals и проверьте plc_spec_check.", data={"file": file})
            data = plcspec.load(file)
            channels = plcproj.io(args["template_file"]) if args.get("template_file") else None
            got = plcspec.check(data, channels)
            part, meta = _page(got["issues"], args)
            out = {"file": file, "summary": got["summary"], **meta, "issues": part}
            if args.get("io_map"):
                changes = plcspec.io_changes(data)
                out["io_map"] = {"count": len(changes), "changes": changes[:PAGE_MAX]}
            return _text(_json(out), not got["summary"]["complete"])
        if name == "plc_stage_update":
            got = plcstage.update(args["project_dir"], args.get("stage") or "", args.get("state") or "",
                                  args.get("note"), args.get("facts"))
            spec_file = plcspec.path_of(args["project_dir"])
            if os.path.isfile(spec_file):  # the specification is what the stages after «materials» are made from
                try:
                    summary = plcspec.check(plcspec.load(spec_file))["summary"]
                    got["spec"] = {k: summary[k] for k in ("mechanisms", "signals", "errors", "warnings", "complete")}
                    if not summary["complete"]:
                        got["spec_note"] = "Спецификация неполная: plc_spec_check покажет, чего не хватает."
                except plcspec.SpecError as exc:
                    got["spec_note"] = f"Спецификация не читается: {exc}"
            elif args.get("stage") == "materials" and args.get("state") == "done":
                got["spec_note"] = ("Спецификации проекта нет (_work/spec/project.spec.json): создайте plc_spec_new и "
                                    "выпишите в неё механизмы и сигналы — по ней делаются программа, каналы и экраны.")
            stray = plcstage.extra(args["project_dir"])
            if stray:  # the folders of results hold only results: the engineer opens plc/ and sees the file to take
                got["extra"] = stray[:30]
                got["extra_note"] = (f"В папках результата лишних файлов: {len(stray)}{'+' if len(stray) >= 200 else ''}. В plc/ — "
                                     "только .project, в hmi/ — только проект панели, в docs/ — только .docx: исходники, "
                                     "скрипты, таблицы и заметки перенесите в _work/ (st, hmi, docs, gen, notes).")
            return _text(_json(got))
        if name == "plc_project_build":
            p = plcbuild.load_dir(args["src_dir"])
            issues = p.check()
            errors = [i for i in issues if i["level"] == "error"]
            if errors:  # a project that will not compile is not written: the .st is fixed first
                lines = [f"{i['where']}{':' + str(i['line']) if i['line'] else ''} — {i['text']}" for i in errors[:60]]
                return _text("Проверка нашла ошибки, файл не записан:\n" + "\n".join(lines), True,
                             {"errors": errors[:200], "written": False})
            out = args["out_file"]
            if args.get("template_file"):
                rep = plcproj.create(p, args["template_file"], out, bool(args.get("overwrite")), bool(args.get("keep_program")),
                                     str(args.get("io") or "keep"))
                head = f"Новый проект → {out} (шаблон {os.path.basename(args['template_file'])}, контроллер {rep['controller'] or '—'})"
            else:
                if not os.path.isfile(out):
                    return _text(f"Файла {out} нет: для нового проекта назовите template_file — шаблон .project с "
                                 "контроллером (готовый проект или plc_project_template).", True)
                rep = plcproj.update(out, p)
                head = f"Проект обновлён → {out}"
            _CACHE.clear()
            lines = [head, f"создано {len(rep['created'])}, обновлено {len(rep['updated'])}, без изменений {len(rep['kept'])}"
                     + (f", вынуто из шаблона {len(rep['removed'])} ({', '.join(rep['removed'][:30])}"
                        f"{'…' if len(rep['removed']) > 30 else ''})" if rep["removed"] else "")
                     + (f"; задачи: {', '.join(rep['tasks'])}" if rep["tasks"] else "")
                     + (f"; в OPC UA опубликовано переменных: {rep['opcua']}" if "opcua" in rep else "")]
            if rep.get("left"):
                lines.append("В проекте остались объекты, которых нет в папке: " + ", ".join(rep["left"][:40]))
            if rep["skipped"]:
                lines.append("Не записаны (не ST): " + ", ".join(rep["skipped"][:40]))
            lines += [f"Замечание: {w}" for w in rep["warnings"]]
            lines.append("Сверка с папкой после записи: " + ("сходится." if not rep["verify"] else "; ".join(rep["verify"][:20])))
            io_rep = rep["io"]
            if rep["io_removed"]:
                lines.append(f"С каналов снято переменных: {len(rep['io_removed'])} ({', '.join(rep['io_removed'][:20])}"
                             f"{'…' if len(rep['io_removed']) > 20 else ''})")
            lines.append(f"Каналы ввода-вывода: с переменной {io_rep['bound']}, свободных {io_rep['free']}.")
            if io_rep["undeclared"]:
                shown = ", ".join(f"{r['device']}.{r['channel']} → {r['variable']}" for r in io_rep["undeclared"][:20])
                lines.append(f"ОШИБКА привязок: {len(io_rep['undeclared'])} каналов называют переменные, которых в программе "
                             f"нет — Machine Expert не скомпилирует: {shown}{'…' if len(io_rep['undeclared']) > 20 else ''}. "
                             "Объявите эти переменные в списке переменных, переставьте каналы (plc_io_map) или соберите "
                             "снова с io: drop_undeclared.")
            if io_rep["duplicates"]:
                lines.append("Одна переменная на нескольких каналах: " + "; ".join(
                    f"{d['variable']} — {', '.join(d['channels'])}" for d in io_rep["duplicates"][:10]))
            if io_rep["types"]:
                lines.append("Тип переменной не подходит каналу: " + "; ".join(
                    f"{r['device']}.{r['channel']} ({r['channel_type']}) ← {r['variable']} ({r['variable_type']})"
                    for r in io_rep["types"][:10]))
            bad = bool(rep["verify"]) or bool(io_rep["undeclared"])
            lines.append("Дальше: " + ("сначала исправьте привязки каналов, затем " if io_rep["undeclared"] else "") +
                         "откройте файл в Machine Expert и скомпилируйте (machine-expert: me_build) — "
                         "ошибки компилятора исправьте в .st и повторите plc_project_build.")
            rep["written"] = True
            rep["errors"] = errors[:200]
            for key in ("undeclared", "duplicates", "types"):  # the answer stays small; the numbers are whole
                io_rep[key + "_count"] = len(io_rep[key])
                io_rep[key] = io_rep[key][:50]
            return _text("\n".join(lines), bad, rep)
        if name == "plc_project_template":
            rep = plcproj.template(args["path"], args["out_file"], bool(args.get("overwrite")), bool(args.get("clear_io")))
            left = rep["channels_with_variables"]
            return _text(f"Шаблон → {rep['out_file']}: контроллер {rep['controller'] or '—'}, устройств {len(rep['devices'])}, "
                         f"задачи {', '.join(rep['tasks']) or '—'}, каналов ввода-вывода {rep['channels']}; вынуто объектов "
                         f"программы: {len(rep['removed'])} ({', '.join(rep['removed'][:30])}{'…' if len(rep['removed']) > 30 else ''}). "
                         + (f"С каналов снято переменных: {rep['io_cleared']}." if rep["io_cleared"] else
                            f"На {left} каналах остались переменные прежней программы: объявите их в новой, переставьте "
                            "(plc_io_map) или сделайте шаблон с clear_io — иначе проект не скомпилируется." if left else
                            "Каналы свободны."), data=rep)
        if name == "plc_io":
            rows = plcproj.io(args["path"])
            dev = (args.get("device") or "").lower()
            flt = (args.get("filter") or "").lower()
            rows = [r for r in rows if (not dev or r["device"].lower() == dev) and (not args.get("free") or not r["variable"])
                    and (not flt or any(flt in str(r.get(k) or "").lower() for k in ("channel", "variable", "description")))]
            part, meta = _page(rows, args)
            return _text(_json({**meta, "channels": part}))
        if name == "plc_io_map":
            rep = plcproj.set_io(args["path"], args["changes"], args["out_file"])
            _CACHE.clear()
            return _text(f"Каналов изменено: {len(rep['changed'])} → {rep['out_file']}", data=rep)
        p = _open(args["path"])
        if name == "plc_info":
            if str(args["path"]).lower().endswith((".project", ".projectarchive")):
                d = plcproj.probe(str(args["path"]))
                d["program"] = p.info()
                return _text(d["note"] + "\n" + _json({k: d[k] for k in d if k != "note"}), data=d)
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
            part, meta = _page(rows, args)
            return _text(_json({**meta, "variables": [_public(v) for v in part]}))
        if name == "plc_addresses":
            a = p.addresses()
            return _text(_json({"bit_numbering": a["bit_numbering"], "ranges": a["ranges"], "free": a["free"],
                                "conflicts": a["conflicts"][:500], "addressed": len(a["variables"])}))
        if name == "plc_hmi_tags":
            flt = (args.get("filter") or "").lower()
            link = "opcua" if (args.get("link") or p.link.get("kind") or "") == "opcua" else "modbus"
            rows = [t for t in p.hmi_tags(link) if not flt or flt in t["name"].lower() or flt in t["comment"].lower() or flt in t["group"].lower()]
            note = ""
            if args.get("out_file"):
                buf = io.StringIO()
                w = csv.writer(buf, delimiter=";", lineterminator="\n")
                if link == "opcua":
                    w.writerow(["name", "type", "kind", "node", "group", "comment"])
                    for t in rows:
                        w.writerow([t["name"], t["type"], t["kind"], t["node"], t["group"], t["comment"]])
                else:
                    w.writerow(["name", "type", "kind", "address", "register", "bit", "weintek_type", "weintek_address", "group", "comment"])
                    for t in rows:
                        w.writerow([t["name"], t["type"], t["kind"], t["address"], t["register"], "" if t["bit"] is None else t["bit"],
                                    t["weintek"]["type"], t["weintek"]["address"], t["group"], t["comment"]])
                os.makedirs(os.path.dirname(os.path.abspath(args["out_file"])), exist_ok=True)
                with open(args["out_file"], "w", encoding="utf-8-sig", newline="") as f:
                    f.write(buf.getvalue())
                note = f" CSV со всеми {len(rows)} тегами → {args['out_file']}"
            part, meta = _page(rows, args)
            if link == "opcua":
                return _text(_json({**meta, "link": "opcua",
                                    "note": "теги OPC UA: имя «Список.Переменная», node — s:имя (ns возьмёт таблица панели); "
                                            "node_m241 — номер, которым ту же переменную называет сервер контроллера "
                                            "M241 / M251 (ns:2;i:N, считается по имени): если в таблице панели теги "
                                            "с номерами, cmtp_edit add_tags посчитает его сам. add_tags — готовая "
                                            "правка для cmtp_edit." + note,
                                    "add_tags": [{"name": t["name"], "type": t["panel_type"]} for t in part],
                                    "tags": part}))
            return _text(_json({**meta, "link": "modbus", "note": "регистры Modbus с нуля; у Weintek с единицы." + note,
                                "tags": part}))
        if name == "plc_tag_check":
            tags = args.get("tags") or []
            if args.get("tags_file"):
                with open(args["tags_file"], encoding="utf-8-sig") as f:
                    data = json.load(f)
                tags = list(tags) + list(data.get("tags", data) if isinstance(data, dict) else data)
            if not tags:
                return _text("Нужны tags (имена или {name, type, node} из cmtp_tags) либо tags_file.", True)
            r = p.tag_check(tags)
            r["summary"] = (f"тегов панели: {r['ok'] + len(r['missing']) + len(r['type']) + len(r['node']) + len(r['not_plain'])}; "
                            f"сходятся с программой: {r['ok']}; нет в программе: {len(r['missing'])}; другой тип: {len(r['type'])}; "
                            f"не тот номер node: {len(r['node'])}; не простой тип: {len(r['not_plain'])}")
            return _text(_json(r))
        if name == "plc_check":
            issues = p.check()
            levels = {k: sum(1 for i in issues if i["level"] == k) for k in ("error", "warning", "info")}
            order = {"error": 0, "warning": 1, "info": 2}
            issues = sorted(issues, key=lambda i: order.get(i["level"], 3))  # the errors first: they are what stops the build
            part, meta = _page(issues, args)
            return _text(_json({"summary": levels, **meta, "issues": part}))
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
    except (plcme.PlcError, plcproj.ProjectFileError, plcstage.StageError, plcspec.SpecError) as exc:
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
