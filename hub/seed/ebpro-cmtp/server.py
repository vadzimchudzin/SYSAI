"""MCP-сервер ebpro-cmtp: читает и собирает проекты EasyBuilder Pro (.cmtp/.emtp).

Запускается синхронизатором на ПК (sysai-agent mcp ebpro-cmtp) и говорит по stdio
построчным JSON-RPC 2.0 (как ждёт обёртка SYSAI). Доступы (папки на чтение/запись)
проверяет сам SYSAI по подписанным правилам до вызова — здесь только работа с файлами.
"""
import base64
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ebpcmtp  # noqa: E402
from ebpcmtp import Project, public  # noqa: E402
import ebpedit  # noqa: E402

PROTOCOL = "2025-06-18"
PATH = {"type": "string", "description": r"Полный путь к файлу проекта, например C:\Projects\topol.emtp"}

PAGE = 200         # rows of a table in one answer; an answer stays in the conversation and is paid for with every
PAGE_OBJECTS = 40  # next request, so a table comes in pages and a window — in parts
PAGE_LINES = 200  # objects of a window as lines: a fifth of the same objects as JSON
PAGE_MAX = 1000
LIMIT = {"type": "integer", "minimum": 1, "maximum": PAGE_MAX, "description": "Сколько строк вернуть"}
OFFSET = {"type": "integer", "minimum": 0, "description": "С какой строки (0 — с первой): для следующей страницы"}

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
                        "filter": {"type": "string", "description": "Показать только подписи с этой подстрокой"},
                        "limit": LIMIT, "offset": OFFSET},
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
                       "window — имя окна или его номер. kind — показать только объекты этого вида. В ответе "
                       f"по строке на объект (format говорит, что в строке), до {PAGE_LINES} строк; count — сколько "
                       "всего, window.kinds — сколько каких. detail: true — объекты целиком (шрифты подписей, "
                       f"устройства и узлы адресов), по {PAGE_OBJECTS} на страницу: берите так только те, что будете "
                       "править, через kind и offset.",
        "inputSchema": {"type": "object", "properties": {"path": PATH,
                        "window": {"type": "string", "description": "Имя окна (точно) или номер, например «Нория 3» или 23"},
                        "kind": {"type": "string", "description": "Только объекты этого вида, например ToggleSwitch"},
                        "detail": {"type": "boolean", "description": "Объекты целиком (JSON), а не строками"},
                        "limit": LIMIT, "offset": OFFSET},
                        "required": ["path", "window"], "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "cmtp_tags",
        "title": "Теги OPC UA",
        "description": "Таблица тегов OPC UA проекта (что HMI читает из ПЛК): имя, тип (BOOL, INT, UINT, UDINT…), "
                       "узел. Нужна, чтобы привязать объекты к нужным тегам. filter — подстрока имени: ищите "
                       f"нужные теги им, таблица отвечает страницами по {PAGE} строк (count — сколько всего).",
        "inputSchema": {"type": "object", "properties": {"path": PATH,
                        "filter": {"type": "string", "description": "Подстрока имени тега"},
                        "limit": LIMIT, "offset": OFFSET},
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
                       "images — какой файл у какой картинки. window — имя окна или номер. В ответе страница из "
                       f"{PAGE_OBJECTS} объектов (count — сколько всего, window.kinds — сколько каких); kind — только "
                       "объекты этого вида, offset — следующая страница.",
        "inputSchema": {"type": "object", "properties": {"path": PATH,
                        "window": {"type": "string", "description": "Имя окна (точно) или номер"},
                        "images_dir": {"type": "string", "description": "Папка для картинок окна (в разрешённой на запись)"},
                        "kind": {"type": "string", "description": "Только объекты этого вида"},
                        "limit": LIMIT, "offset": OFFSET},
                        "required": ["path", "window"], "additionalProperties": False},
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False},
    },
    {
        "name": "cmtp_links",
        "title": "Переходы между окнами",
        "description": "Функциональные клавиши, что открывают окна: в каком окне и какой объект, что делает "
                       "(window — сменить окно, popup — всплывающее окно, back — назад, close — закрыть всплывающее), "
                       "куда ведёт (target — номер окна). Нужна, чтобы понять меню проекта и перепривязать кнопки. "
                       "window — только кнопки этого окна.",
        "inputSchema": {"type": "object", "properties": {"path": PATH,
                        "window": {"type": "string", "description": "Имя окна или номер"},
                        "limit": LIMIT, "offset": OFFSET},
                        "required": ["path"], "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "cmtp_check",
        "title": "Проверить проект",
        "description": "Что пойдёт не так на панели: объект за краем окна (outside), подпись шире объекта (text_fit), "
                       "кнопка ведёт в несуществующее окно (link_missing), адрес с тегом не из таблицы OPC UA "
                       "(tag_missing), нет файла картинки (picture_missing), в окно не ведёт ни одна кнопка "
                       "(unreachable, справка), одинаковые имена окон (duplicate_name), не все объекты распознаны "
                       "(not_parsed). Уровни: error, warning, info. Проверяют после сборки и перед сдачей. "
                       "window — только это окно.",
        "inputSchema": {"type": "object", "properties": {"path": PATH,
                        "window": {"type": "string", "description": "Имя окна или номер"},
                        "limit": LIMIT, "offset": OFFSET},
                        "required": ["path"], "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "cmtp_render",
        "title": "Картинка окна",
        "description": "Нарисовать окно проекта в PNG так, как его покажет панель (фон, фигуры, картинки первого "
                       "состояния, подписи шрифтами системы; фигуры библиотеки EasyBuilder — простыми кнопками, "
                       "полями и лампами) — посмотреть результат без EasyBuilder. out_file — куда записать .png; "
                       "scale — масштаб 0.1–2 (для окон 1920×1080 хватит 0.5); marks — номера объектов (index) в "
                       "углу каждого, чтобы называть их в правках; states — какие состояния отдельных объектов показать; "
                       "show — вернуть картинку и в ответе. Это статический предпросмотр без связи с ПЛК.",
        "inputSchema": {"type": "object", "properties": {"path": PATH,
                        "window": {"type": "string", "description": "Имя окна или номер"},
                        "out_file": {"type": "string", "description": r"Куда записать PNG, например C:\SYSAI\shots\w23.png"},
                        "scale": {"type": "number", "minimum": 0.1, "maximum": 2},
                        "marks": {"type": "boolean"}, "show": {"type": "boolean"},
                        "states": {"type": "object", "description": "Индекс объекта → номер состояния картинки (с 0); это только предпросмотр",
                                   "additionalProperties": {"type": "integer", "minimum": 0}}},
                        "required": ["path", "window", "out_file"], "additionalProperties": False},
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False},
    },
    {
        "name": "cmtp_edit",
        "title": "Править проект",
        "description": "Правки проекта по списку и сборка файла (out_file; тот же путь, что path, — правка на месте, "
                       "с блокировкой от одновременной записи). Правки идут по порядку; если одна не прошла — файл "
                       "не пишется. Виды (op): text — заменить текст в окне на любой длины (old → new, object — "
                       "только в этом объекте; рамка «Текста» расширяется); move — сдвинуть объект (object, dx, dy); "
                       "resize — новый размер объекта (object, width, height в пикселях; угол на месте); "
                       "style — цвет заливки и рамки фигуры или фона окна, цвет и размер подписи состояния; "
                       "bind_tag — один существующий слот OPC UA на тег того же типа (slot 1–3, new); tag — "
                       "перепривязать объекты окна с тега OPC UA old на new (тот же тип, тег из таблицы; object — "
                       "только этот); rename_window — имя окна (new); clone_window — копия окна window под номером "
                       "number (по умолчанию — после последнего) с именем new; clone_object — копия объекта object в "
                       "конец того же окна или окна to со сдвигом dx, dy; delete_object — удалить объект object "
                       "(следующие сдвигаются на один index); delete_window — удалить окно (номер освобождается; в "
                       "ответе — кнопки, что ещё ведут в него); link — кнопку object окна window направить в окно to "
                       "(кнопка уже должна открывать окно или всплывающее окно); add_tags — добавить теги OPC UA в "
                       "таблицу (tags: [{name, type, node}], node по умолчанию ns таблицы;s=name; типы — те, что в "
                       "проекте уже есть); import_window — взять окно window из проекта-донора from (та же версия "
                       "EasyBuilder) под номером number с именем new; import_objects — взять объекты objects окна "
                       "window донора from в окно to со сдвигом dx, dy. При взятии из донора: картинки переносятся "
                       "в библиотеку (одинаковые не дублируются), теги, которых нет в проекте, — из таблицы донора, "
                       "picture_states — заменить картинки отдельного объекта по состояниям PNG-файлами или GUID библиотеки "
                       "(число состояний не меняется). map_tags {тег донора: тег проекта} — перепривязать, map_windows {окно донора: окно проекта} "
                       "— куда вести кнопки, map_devices {устройство донора: устройство проекта}; кнопки на окна, "
                       "взятые в этом же списке, ведут на их новые номера сами.",
        "inputSchema": {"type": "object", "properties": {"path": PATH,
            "out_file": {"type": "string", "description": r"Куда записать проект, например C:\Projects\topol_v2.cmtp"},
            "edits": {"type": "array", "minItems": 1, "description": "Правки по порядку", "items": {
                "type": "object", "properties": {
                    "op": {"type": "string", "enum": ["text", "move", "resize", "tag", "rename_window", "clone_window",
                                                      "clone_object", "delete_object", "delete_window", "link",
                                                      "add_tags", "import_window", "import_objects", "picture_states", "style", "bind_tag"]},
                    "window": {"type": "string", "description": "Имя окна или номер (у import_* — окно донора)"},
                    "object": {"type": "integer", "description": "index объекта из cmtp_objects"},
                    "objects": {"type": "array", "items": {"type": "integer"}, "description": "import_objects: index объектов донора; clone_object: несколько объектов разом — все index как до этой правки, в ответе index копий"},
                    "old": {"type": "string"}, "new": {"type": "string"},
                    "dx": {"type": "integer"}, "dy": {"type": "integer"},
                    "width": {"type": "integer"}, "height": {"type": "integer"},
                    "fill": {"type": ["string", "null"], "description": "Цвет заливки #RRGGBB; null — без заливки"},
                    "frame": {"type": "string", "description": "Цвет рамки #RRGGBB"},
                    "frame_width": {"type": "integer", "minimum": 0, "maximum": 20},
                    "text_color": {"type": "string", "description": "Цвет подписи #RRGGBB"},
                    "font_size": {"type": "integer", "minimum": 1, "maximum": 100},
                    "label": {"type": "integer", "minimum": 0, "description": "Индекс состояния подписи, по умолчанию 0"},
                    "slot": {"type": "integer", "enum": [1, 2, 3], "description": "Номер существующего OPC UA-адреса"},
                    "number": {"type": "integer", "description": "clone_window / import_window: номер нового окна"},
                    "to": {"type": "string", "description": "clone_object / import_objects: окно, куда положить; link: окно, куда вести"},
                    "layer": {"type": "string", "description": "clone_object / import_objects: слой окна (имя или номер с 0), в конец которого встаёт копия; по умолчанию у clone_object — слой с тем же именем, что у оригинала, иначе последний"},
                    "from": {"type": "string", "description": r"import_*: файл проекта-донора, например C:\Projects\donor.cmtp"},
                    "tags": {"type": "array", "description": "add_tags: [{name, type, node}]", "items": {
                        "type": "object", "properties": {"name": {"type": "string"}, "type": {"type": "string"},
                                                         "node": {"type": "string"}},
                        "required": ["name", "type"], "additionalProperties": False}},
                    "states": {"type": "array", "minItems": 1, "maxItems": 100, "items": {
                        "type": "object", "properties": {"image_file": {"type": "string", "description": "Полный путь к PNG"},
                                                            "image": {"type": "string", "description": "GUID картинки проекта"},
                                                            "transparent": {"type": ["string", "null"]}},
                        "additionalProperties": False}},
                    "map_tags": {"type": "object", "additionalProperties": {"type": "string"},
                                 "description": "import_*: {тег донора: тег проекта}; clone_window / clone_object: {тег оригинала: тег копии} — теги копии перепривязываются сразу (теги должны быть в таблице: add_tags раньше в этом же списке)"},
                    "map_texts": {"type": "object", "additionalProperties": {"type": "string"},
                                  "description": "clone_window: {текст оригинала: текст копии} — заголовок и подписи копии"},
                    "link_to": {"type": "string", "description": "clone_object: окно, которое открывают скопированные кнопки перехода"},
                    "map_windows": {"type": "object", "additionalProperties": {"type": "string"}},
                    "map_devices": {"type": "object", "additionalProperties": {"type": "string"}}},
                "required": ["op"], "additionalProperties": False}}},
            "required": ["path", "out_file", "edits"], "additionalProperties": False},
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False},
    },
]

INSTRUCTIONS = ("Инструменты для проектов EasyBuilder Pro (.cmtp/.emtp). Разобрать: cmtp_info → cmtp_windows → "
                "cmtp_objects (окно) → cmtp_links (меню) → cmtp_tags. Посмотреть окно без EasyBuilder: cmtp_render "
                "(PNG, marks — номера объектов). Собрать: cmtp_edit — тексты, сдвиг, размер, теги, копии и удаление "
                "окон и объектов, кнопки на окна, новые теги, окна и объекты из проекта-донора. После сборки — "
                "cmtp_check. Доступы к папкам даёт SYSAI.")


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


class _Lock:
    """<file>.sysai-lock while a project is written: the canvas, the agent and its helpers do not overwrite each
    other's edits. A lock older than two minutes is a leftover and is taken over."""

    def __init__(self, path, wait=20.0):
        self.path, self.wait, self.fd = f"{os.path.abspath(path)}.sysai-lock", wait, None

    def __enter__(self):
        end = time.monotonic() + self.wait
        while True:
            try:
                self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self.fd, f"{os.getpid()} {time.time():.0f}".encode())
                return self
            except FileExistsError:
                try:
                    if time.time() - os.stat(self.path).st_mtime > 120:
                        os.remove(self.path)
                        continue
                except OSError:
                    continue
                if time.monotonic() > end:
                    raise ValueError("проект сейчас пишет другой агент или холст — повторите через минуту") from None
                time.sleep(0.2)

    def __exit__(self, *exc):
        if self.fd is not None:
            os.close(self.fd)
            try:
                os.remove(self.path)
            except OSError:
                pass
        return False


def _text(s, is_error=False):
    r = {"content": [{"type": "text", "text": s}]}
    if is_error:
        r["isError"] = True
    return r


def _page(rows, args, default=PAGE):
    """(the rows of the page, what to say about the rest) — «count» is always the whole table."""
    try:
        limit = min(PAGE_MAX, max(1, int(args.get("limit") or default)))
        offset = max(0, int(args.get("offset") or 0))
    except (TypeError, ValueError):
        limit, offset = default, 0
    part = rows[offset:offset + limit]
    meta = {"count": len(rows), "offset": offset, "returned": len(part)}
    if offset + len(part) < len(rows):
        meta["next_offset"] = offset + len(part)
        meta["more"] = (f"показаны {offset + 1}–{offset + len(part)} из {len(rows)}: сузьте запрос (filter, kind, window) "
                        f"или возьмите следующую страницу offset={offset + len(part)}; целиком в разговор не выгружайте")
    return part, meta


LINE_FORMAT = "index вид лево,верх,право,низ [слой] «подпись» слот=адрес (слоты: 1 чтение, 2 запись, 3 доп., 0 прочие)"


def _line(o: dict) -> str:
    """An object of a window as one line — what is needed to choose and to edit it; the whole of it is `detail`."""
    out = [str(o.get("index")), str(o.get("kind")), ",".join(str(int(v)) for v in o.get("bbox") or [])]
    if o.get("layer"):
        out.append(f"[{o['layer']}]")
    for lab in (o.get("labels") or [])[:3]:
        out.append("«" + str(lab.get("text") or "").replace("\n", " ")[:60] + "»")
    for t in (o.get("texts") or [])[:2]:
        out.append("“" + str(t if isinstance(t, str) else (t.get("text") if isinstance(t, dict) else t))[:40] + "”")
    by = {}
    for a in o.get("addresses") or []:
        by.setdefault(str(a.get("tag") or a.get("address") or ""), []).append(str(a.get("slot")))
    for where, slots in by.items():
        if where:
            out.append(f"{','.join(slots)}={where}")
    return " ".join(out)


def _kinds(objs):
    out = {}
    for o in objs:
        out[o["kind"]] = out.get(o["kind"], 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


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
            part, meta = _page(rows, args)
            return _text(json.dumps({**meta, "labels": part}, ensure_ascii=False))
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
            head = dict(public(w), objects=len(w["objects"]), kinds=_kinds(w["objects"]))
            if args.get("detail"):
                part, meta = _page(objs, args, PAGE_OBJECTS)
                return _text(json.dumps({"window": head, **meta, "objects": part}, ensure_ascii=False))
            part, meta = _page(objs, args, PAGE_LINES)
            return _text(json.dumps({"window": head, **meta, "format": LINE_FORMAT, "lines": [_line(o) for o in part]},
                                    ensure_ascii=False))
        if name == "cmtp_tags":
            p = _load(args["path"])
            flt = (args.get("filter") or "").lower()
            rows = [t for t in p.opcua_tags() if not flt or flt in t["name"].lower()]
            part, meta = _page(rows, args)
            return _text(json.dumps({**meta, "tags": part}, ensure_ascii=False))
        if name == "cmtp_scene":
            p = _load(args["path"])
            scene = p.scene(args["window"], args.get("images_dir") or None)
            kind = args.get("kind")
            objs = [o for o in scene["objects"] if not kind or o["kind"] == kind]
            part, meta = _page(objs, args, PAGE_OBJECTS)
            used = {st["key"] for o in part for st in o.get("picture") or ()}
            scene["window"]["kinds"] = _kinds(scene["objects"])
            return _text(json.dumps({"window": scene["window"], **meta, "objects": part,
                                     "images": {k: v for k, v in scene["images"].items() if k in used}}, ensure_ascii=False))
        if name == "cmtp_links":
            p = _load(args["path"])
            rows = p.links(args.get("window") or None)
            names = {w["number"]: w["name"] for w in p.windows()}
            for r in rows:
                r["window_name"] = names.get(r["window"])
                if r["target"] is not None:
                    r["target_name"] = names.get(r["target"])
            part, meta = _page(rows, args)
            return _text(json.dumps({**meta, "links": part}, ensure_ascii=False))
        if name == "cmtp_check":
            p = _load(args["path"])
            rows = ebpedit.check(p, args.get("window") or None)
            levels = {k: sum(1 for r in rows if r["level"] == k) for k in ("error", "warning", "info")}
            order = {"error": 0, "warning": 1, "info": 2}
            rows = sorted(rows, key=lambda r: order.get(r["level"], 3))  # the errors first
            part, meta = _page(rows, args)
            return _text(json.dumps({"summary": levels, **meta, "issues": part}, ensure_ascii=False))
        if name == "cmtp_render":
            import ebprender
            p = _load(args["path"])
            png = ebprender.render(p, args["window"], args.get("scale") or 1, bool(args.get("marks")), args.get("states"))
            out = args["out_file"]
            os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
            with open(out, "wb") as f:
                f.write(png)
            w = p.window(args["window"])
            res = _text(f"Окно {w['number']} «{w['name']}» {w['width']}×{w['height']} → {out} ({len(png)} байт)")
            if args.get("show") and len(png) <= 3_000_000:
                res["content"].append({"type": "image", "data": base64.b64encode(png).decode(), "mimeType": "image/png"})
            return res
        if name == "cmtp_edit":
            out = args["out_file"]
            with _Lock(out):
                return _edit(args)
        return _text(f"нет такого инструмента: {name}", True)
    except FileNotFoundError as exc:
        return _text(f"файл не найден: {exc}", True)
    except ValueError as exc:
        return _text(f"не выходит: {exc}", True)
    except OSError as exc:
        return _text(f"ошибка файла: {exc}", True)


def _edit(args):
    p = Project.load(args["path"])
    done, results, notes, pending = [], [], [], []
    donors, linked = {}, {}

    def removed(wn, i):         # object i of window wn is gone: the keys waiting for their window follow
        for x in list(pending):
            if x["window"] == wn and (i is None or x["object"] == i):
                pending.remove(x)
            elif x["window"] == wn and x["object"] > i:
                x["object"] -= 1

    def inserted(wn, at, k):    # k objects went in at index `at` (before the last one)
        for x in pending:
            if x["window"] == wn and x["object"] >= at:
                x["object"] += k

    def settle(path):           # keys waiting for a donor window that is here now lead to it
        for x in list(pending):
            if x["path"] == path and x["donor_target"] in linked[path]:
                to = linked[path][x["donor_target"]]
                ebpedit.link(p, x["window"], x["object"], to)
                done.append(f"{x['n']}. кнопка #{x['object']} окна {x['window']} → окно {to} (взято из донора)")
                pending.remove(x)

    def donor(e):
        path = os.path.abspath(str(e["from"]))
        if path not in donors:
            donors[path] = ebpedit.open_donor(path, p)
            linked[path] = {}
        return path, donors[path]

    for n, e in enumerate(args.get("edits") or [], 1):
        op, win = e.get("op"), e.get("window")
        try:
            num = p.window(win)["number"] if op in ("text", "move", "resize", "tag", "rename_window", "clone_object",
                                                    "delete_object", "link", "picture_states", "style", "bind_tag") else None
            res = {"op": op, "window": num}
            if op == "text":
                k = p.edit_text(win, str(e["old"]), str(e["new"]), e.get("object"))
                done.append(f"{n}. текст {e['old']!r} → {e['new']!r}: {k} шт.")
                res.update(object=e.get("object"), count=k)
            elif op == "move":
                bb = p.move_object(win, int(e["object"]), int(e.get("dx") or 0), int(e.get("dy") or 0))
                done.append(f"{n}. объект #{e['object']} сдвинут, рамка {bb}")
                res.update(object=int(e["object"]), bbox=bb)
            elif op == "resize":
                bb = ebpedit.resize(p, win, int(e["object"]), e.get("width"), e.get("height"))
                done.append(f"{n}. объект #{e['object']}: новый размер, рамка {bb}")
                res.update(object=int(e["object"]), bbox=bb)
            elif op == "picture_states":
                if not isinstance(e["states"], list) or not 1 <= len(e["states"]) <= 100:
                    raise ValueError("нужно от 1 до 100 состояний")
                states, total = [], 0
                for state in e["states"]:
                    state = dict(state)
                    if "image_file" in state:
                        path = state.pop("image_file")
                        if not os.path.isabs(path):
                            raise ValueError("image_file: нужен полный путь к PNG")
                        with open(path, "rb") as f:
                            state["png"] = f.read(8 * 1024 * 1024 + 1)
                        total += len(state["png"])
                        if total > 32 * 1024 * 1024:
                            raise ValueError("суммарный размер PNG больше 32 МБ")
                    states.append(state)
                ids = ebpedit.picture_states(p, win, int(e["object"]), states)
                done.append(f"{n}. объект #{e['object']}: заменены картинки, состояний {len(ids)}")
                res.update(object=int(e["object"]), images=ids)
            elif op == "style":
                props = {k: e[k] for k in ("fill", "frame", "frame_width", "text_color", "font_size", "label") if k in e}
                changed = ebpedit.style(p, win, int(e["object"]), props)
                done.append(f"{n}. объект #{e['object']}: изменено оформление {changed}")
                res.update(object=int(e["object"]), style=changed)
            elif op == "bind_tag":
                changed = ebpedit.bind_tag(p, win, int(e["object"]), e["slot"], str(e["new"]))
                done.append(f"{n}. объект #{e['object']}: адрес {changed['slot']} → {changed['tag']}")
                res.update(object=int(e["object"]), **changed)
            elif op == "tag":
                k = p.retag(win, str(e["old"]), str(e["new"]), e.get("object"))
                done.append(f"{n}. тег {e['old']} → {e['new']}: {k} адрес(ов)")
                res.update(object=e.get("object"), count=k)
            elif op == "rename_window":
                p.rename_window(win, str(e["new"]))
                done.append(f"{n}. окно {win!r} → {e['new']!r}")
            elif op == "clone_window":
                num = p.clone_window(win, e.get("new"), e.get("number"))
                tail = ""
                if e.get("map_tags"):       # the copy at once on its own tags and texts: «one more mechanism»
                    used = {a["tag"] for o in p.window(num)["objects"] for a in o["addresses"] if a.get("tag")}
                    k = sum(p.retag(num, old, str(new)) for old, new in e["map_tags"].items() if old in used)
                    left = sorted(used - set(e["map_tags"]))
                    tail += f"; адресов перепривязано: {k}"
                    if left:
                        tail += f"; остались теги оригинала: {', '.join(left[:8])}{'…' if len(left) > 8 else ''}"
                    res.update(retagged=k, tags_left=left)
                if e.get("map_texts"):
                    have = {t for o in p.window(num)["objects"] for _, _, t in o["_texts"]}
                    k = sum(p.edit_text(num, old, str(new)) for old, new in e["map_texts"].items() if old in have)
                    tail += f"; текстов заменено: {k}"
                done.append(f"{n}. окно {win!r} скопировано → {num} : {p.window(num)['name']}{tail}")
                res.update(window=num)
            elif op == "clone_object":
                tn = p.window(e["to"])["number"] if e.get("to") else num
                same = tn == num
                # objects: several at once, every index as it is BEFORE this edit (the copies shift the rest)
                src = [int(i) for i in e["objects"]] if e.get("objects") else [int(e["object"])]
                todo, copies = list(src), []
                while todo:
                    i = todo.pop(0)
                    before = len(p.window(tn)["objects"])
                    k = p.clone_object(win, i, e.get("to"), int(e.get("dx") or 0), int(e.get("dy") or 0),
                                       e.get("layer"))
                    if k < before:
                        inserted(tn, k, 1)
                    copies = [c + 1 if c >= k else c for c in copies] + [k]
                    if same:
                        todo = [j + 1 if j >= k else j for j in todo]
                # the copies at once: map_tags {tag of the original: tag of the copy}, link_to — the window the
                # copied keys open (the keys that open a window), so one edit makes «one more mechanism»
                extra = []
                if e.get("map_tags") or e.get("link_to") is not None:
                    retagged = linked_keys = 0
                    for c in copies:
                        o = p.window(tn)["objects"][c]
                        for old in sorted({a["tag"] for a in o["addresses"] if a.get("tag")} & set(e.get("map_tags") or {})):
                            retagged += p.retag(tn, old, str(e["map_tags"][old]), c)
                        if e.get("link_to") is not None:
                            o = p.window(tn)["objects"][c]
                            fa = ebpcmtp.fk_action(p.payload, o)
                            if fa and fa[1] in (0, 2):
                                ebpedit.link(p, tn, c, e["link_to"])
                                linked_keys += 1
                    if e.get("map_tags"):
                        extra.append(f"адресов перепривязано: {retagged}")
                    if e.get("link_to") is not None:
                        extra.append(f"кнопок на окно {e['link_to']}: {linked_keys}")
                    res.update(retagged=retagged, linked=linked_keys)
                tail = ("; " + ", ".join(extra)) if extra else ""
                if len(src) == 1:
                    done.append(f"{n}. объект #{src[0]} скопирован → #{copies[0]} в окне {e.get('to') or win!r}{tail}")
                    res.update(window=tn, object=copies[0])
                else:
                    done.append(f"{n}. объекты {src} скопированы → {copies} в окне {e.get('to') or win!r}{tail}")
                    res.update(window=tn, object=copies[0], objects=copies)
            elif op == "delete_object":
                ebpedit.delete_object(p, win, int(e["object"]))
                removed(num, int(e["object"]))
                done.append(f"{n}. объект #{e['object']} окна {win!r} удалён")
                res.update(object=int(e["object"]))
            elif op == "delete_window":
                w = p.window(win)
                left = ebpedit.delete_window(p, win)
                removed(w["number"], None)
                for m in linked.values():
                    for k in [k for k, v in m.items() if v == w["number"]]:
                        del m[k]
                res.update(window=w["number"], links=left)
                done.append(f"{n}. окно {w['number']} «{w['name']}» удалено" + (
                    f"; в него ещё ведут кнопки: " + ", ".join(f"окно {x['window']} #{x['object']}" for x in left)
                    if left else ""))
            elif op == "link":
                t = ebpedit.link(p, win, int(e["object"]), e["to"])
                pending[:] = [x for x in pending if (x["window"], x["object"]) != (num, int(e["object"]))]
                done.append(f"{n}. кнопка #{e['object']} окна {win!r} ведёт в окно {t}")
                res.update(object=int(e["object"]), target=t)
            elif op == "add_tags":
                added = ebpedit.add_tags(p, e["tags"])
                done.append(f"{n}. тегов добавлено: {len(added)}" + (f" ({', '.join(added[:10])}"
                            + ("…" if len(added) > 10 else "") + ")" if added else ""))
                res.update(added=added)
            elif op == "import_window":
                path, d = donor(e)
                r = ebpedit.import_window(p, d, win, e.get("number"), e.get("new"), e.get("map_tags"),
                                          e.get("map_windows"), e.get("map_devices"), linked[path])
                num = r["number"]
                done.append(f"{n}. окно донора {win!r} → {num} : {p.window(num)['name']}")
                res.update(window=num)
                notes += [f"{n}. {x}" for x in r["notes"] if "the key leads to donor window" not in x]
                pending += [dict(ln, n=n, path=path, window=num) for ln in r["pending"]]
                settle(path)
            elif op == "import_objects":
                path, d = donor(e)
                objs = e.get("objects") if e.get("objects") is not None else [e["object"]]
                before = len(p.window(e["to"])["objects"])
                r = ebpedit.import_objects(p, d, win, objs, e["to"], e.get("dx") or 0, e.get("dy") or 0,
                                           e.get("map_tags"), e.get("map_windows"), e.get("map_devices"), linked[path],
                                           e.get("layer"))
                tw = p.window(e["to"])["number"]
                if r["first"] < before:
                    inserted(tw, r["first"], r["count"])
                done.append(f"{n}. объекты донора {objs} → окно {tw}, #{r['first']}…#{r['first'] + r['count'] - 1}")
                res.update(window=tw, object=r["first"], count=r["count"])
                notes += [f"{n}. {x}" for x in r["notes"] if "the key leads to donor window" not in x]
                pending += [dict(ln, n=n, path=path, window=tw) for ln in r["pending"]]
            else:
                return _text(f"правка {n}: нет такого op: {op!r}", True)
            results.append(res)
        except KeyError as exc:
            return _text(f"правка {n} ({op}): не хватает поля {exc}; файл не записан", True)
        except ValueError as exc:
            return _text(f"правка {n} ({op}): {exc}; файл не записан", True)
    for ln in pending:          # keys into donor windows not taken: the number stays, say what it is here
        dn = ln["donor_target"]
        here = next((w["name"] for w in p.windows() if w["number"] == dn), None)
        notes.append(f"{ln['n']}. кнопка #{ln['object']} окна {ln['window']} ведёт в окно донора {dn} "
                     f"«{ln['donor_name']}», его не брали: номер оставлен"
                     + (f" (здесь это «{here}»)" if here else " (здесь такого окна нет)"))
    p.save(args["out_file"])
    text = "\n".join(done + (["Заметки:"] + notes if notes else []) + [f"Записано → {args['out_file']}"])
    return dict(_text(text), structuredContent={"results": results, "notes": notes, "out_file": args["out_file"]})


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
                  "serverInfo": {"name": "ebpro-cmtp", "title": "EasyBuilder Pro .cmtp/.emtp", "version": "1.8.0"},
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
