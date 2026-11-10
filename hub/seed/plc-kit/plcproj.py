"""The `.project` file of Machine Expert / CODESYS V3: what can be told about it without the program.

Found by taking sample projects apart byte by byte (docs/opo/MACHINE_EXPERT.md has the whole map):

* **Open container** — a ZIP archive. Every object of the project tree is two entries named by its GUID:
  `<guid>.meta` (who it is: GUID, parent, name, type) and `<guid>.object` (the content). `*.auxiliary` entries hold the
  shared string table (`__shared_data_storage_string_table__`: every name and every line of text of the project,
  each once), the schema table (the field layout of every stored type), the profile and the compile caches.
* **Closed container** — 16 bytes `23 89 ED 33 14 BD 93 4B 8D A9 5D 91 08 C5 32 35` and then blocks of 16 bytes with
  no structure left (the length is always 16 + 16·n): the same archive, encrypted. The key is inside the program;
  this tool does not open such a file — Machine Expert does (tool machine-expert, `me_export`).

Of the open container this module reads the object tree: names, types and parents. The text of POUs is in the string
table, but the order of lines is kept in `.object` in a schema-driven binary form that is not decoded here.
"""
import re
import zipfile

MAGIC_CLOSED = bytes.fromhex('2389ed3314bd934b8da95d9108c53235')
STRINGS = '__shared_data_storage_string_table__.auxiliary'
_GUID = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
NULL = '00000000-0000-0000-0000-000000000000'
# Type GUIDs of the object tree (the same in CODESYS and Machine Expert; checked on sample projects).
TYPES = {
    '6f9dac99-8de1-4efc-8465-68ac443b7d08': 'pou',
    'f8a58466-d7f6-439f-bbb8-d4600e41d099': 'method',
    '5a3b8626-d3e9-4f37-98b5-66420063d91e': 'property',
    '792f2eb6-721e-4e64-ba20-bc98351056db': 'accessor',
    '8ac092e5-3128-4e26-9e7e-11016c6684f2': 'action',
    '6654496c-404d-479a-aad2-8551054e5f1e': 'interface',
    'f89f7675-27f1-46b3-8abb-b7da8e774ffd': 'interface_method',
    'ffbfa93a-b94d-45fc-a329-229860183b1d': 'gvl',
    '261bd6e6-249c-4232-bb6f-84c2fbeef430': 'persistent_vars',
    '2db5746d-d284-4425-9f7f-2663a34b0ebc': 'type',
    '738bea1e-99bb-4f04-90bb-a7a567e74e3a': 'folder',
    '225bfe47-7336-4dbc-9419-4105a7c831fa': 'device',
    '639b491f-5557-464c-af91-1471bac9f549': 'application',
    'ae1de277-a207-4a28-9efb-456c06bd52f3': 'task_configuration',
    '98a2708a-9b18-4f31-82ed-a1465b24fa2d': 'task',
    'adb5cb65-8e1d-4a00-b70a-375ea27582f3': 'library_manager',
    'f18bec89-9fef-401d-9953-2f11739a6808': 'visualization',
    '8753fe6f-4a22-4320-8103-e553c4fc8e04': 'project_settings',
    '085afe48-c5d8-4ea5-ab0d-b35701fa6009': 'project_information',
}
HOW_TO_OPEN = ('Откройте его через Machine Expert: инструмент machine-expert, me_export (PLCopen XML или папка ST), '
               'либо вручную «Проект → Экспорт…» / «Экспорт PLCopenXML…».')


class ProjectFileError(ValueError):
    pass


def _varint(b: bytes, i: int):
    v = s = 0
    while True:
        c = b[i]
        i += 1
        v |= (c & 0x7F) << s
        s += 7
        if not c & 0x80:
            return v, i


def _strings(blob: bytes) -> dict:
    """The shared string table: a count, then (index, length, UTF-8 bytes) records."""
    out = {}
    _, i = _varint(blob, 0)
    while i < len(blob):
        k, i = _varint(blob, i)
        n, i = _varint(blob, i)
        out[k] = blob[i:i + n].decode('utf-8', 'replace')
        i += n
    return out


def _meta(blob: bytes, strings: dict):
    """One `.meta`: a 16-byte head, u32 length, 0x0F, then string-table indexes as varints — schema, GUID, parent,
    name, the properties, the type GUID, the embedded type GUIDs — and 8 bytes of time. -> (guid, parent, name, type)."""
    if len(blob) < 30 or blob[20] != 0x0F:
        return None
    body = blob[20:-8]
    try:
        _, i = _varint(body, 1)
        g, i = _varint(body, i)
        par, i = _varint(body, i)
        nm, i = _varint(body, i)
    except IndexError:
        return None
    guid, parent, name = strings.get(g, ''), strings.get(par, ''), strings.get(nm, '')
    if not _GUID.match(guid):
        return None
    # the type is found from the end: «type, count, [list marker, ids…]» must use up the body exactly
    for j in range(i, len(body)):
        try:
            t, k = _varint(body, j)
            count, k = _varint(body, k)
            ids = []
            if count:
                marker, k = _varint(body, k)
                if marker < 128:
                    continue
                for _ in range(count):
                    x, k = _varint(body, k)
                    ids.append(x)
        except IndexError:
            continue
        if k == len(body) and _GUID.match(strings.get(t, '')) and all(_GUID.match(strings.get(x, '')) for x in ids):
            return guid, parent, name, strings[t]
    return guid, parent, name, ''


def probe(path: str) -> dict:
    """What the file is: {'container': 'zip' | 'encrypted' | 'archive', 'objects': [...], 'counts': {...}, 'note'}."""
    try:
        with open(path, 'rb') as f:
            head = f.read(16)
    except OSError as exc:
        raise ProjectFileError(f'файл не читается: {exc}') from None
    if head == MAGIC_CLOSED:
        return {'container': 'encrypted', 'objects': [], 'counts': {},
                'note': 'Файл .project сохранён в закрытом виде (зашифрованный контейнер Machine Expert / CODESYS): '
                        'без программы его содержимое не прочитать. ' + HOW_TO_OPEN}
    if not zipfile.is_zipfile(path):
        raise ProjectFileError('не файл проекта Machine Expert / CODESYS: ни архив, ни закрытый контейнер')
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        inner = [n for n in names if n.lower().endswith('.project')]
        if STRINGS not in names:
            if inner:        # .projectarchive: the project and its libraries, devices, compile information
                return {'container': 'archive', 'objects': [], 'counts': {'entries': len(names)}, 'project': inner[0],
                        'note': f'Это архив проекта: внутри {inner[0]} и {len(names) - 1} файлов библиотек и устройств. '
                                'Распакуйте его в Machine Expert («Файл → Архив проекта → Извлечь архив…») или '
                                'machine-expert: me_export.'}
            raise ProjectFileError('в архиве нет таблицы строк проекта — это не .project')
        strings = _strings(z.read(STRINGS))
        objects, unread = [], 0
        for n in names:
            if not n.endswith('.meta'):
                continue
            m = _meta(z.read(n), strings)
            if not m:
                unread += 1
                continue
            guid, parent, name, typ = m
            objects.append({'guid': guid, 'parent': '' if parent == NULL else parent, 'name': name,
                            'kind': TYPES.get(typ, 'other'), 'type': typ})
    by_guid = {o['guid']: o for o in objects}
    for o in objects:
        chain, cur, seen = [], by_guid.get(o['parent']), set()
        while cur and cur['guid'] not in seen:
            seen.add(cur['guid'])
            chain.append(cur['name'])
            cur = by_guid.get(cur['parent'])
        o['path'] = '/'.join(reversed(chain))
    counts = {}
    for o in objects:
        counts[o['kind']] = counts.get(o['kind'], 0) + 1
    shown = [o for o in objects if o['kind'] in ('pou', 'gvl', 'type', 'task', 'device', 'application', 'interface',
                                                'persistent_vars', 'visualization')]
    shown.sort(key=lambda o: (o['kind'], o['path'], o['name']))
    return {'container': 'zip', 'counts': counts, 'unread': unread, 'strings': len(strings),
            'objects': [{k: o[k] for k in ('kind', 'name', 'path')} for o in shown],
            'note': 'Файл .project в открытом виде: видно дерево объектов (имена программ, блоков, списков переменных, '
                    'задач, устройств). Текст программ и переменные из него этот инструмент не достаёт. ' + HOW_TO_OPEN}
