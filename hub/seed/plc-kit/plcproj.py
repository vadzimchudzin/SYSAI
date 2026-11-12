"""The `.project` file of Machine Expert / CODESYS V3: read, changed and written here, without the program.

The format was taken from the program itself (EcoStruxure Machine Expert 2.2 = CODESYS 3.5.19.40) and checked on
real projects: every byte of three customer projects is reproduced, and what this module writes is opened and saved
again by the CODESYS engine with the same objects. docs/opo/MACHINE_EXPERT.md has the whole map.

* `mearchive` — the container (the «integrity check» wrapper: AES over a ZIP, the key is in the header), the ZIP, the
  binary archives of the objects and the two shared tables (strings, schemas).
* `meproject` — the objects: programs, blocks, functions, methods, actions, types, variable lists, folders, tasks,
  the channels of the devices, the OPC UA list.
* this module — the file as plc-kit sees it: the same `plcme.Project` model that `.export` and PLCopen XML give
  (so every tool of plc-kit works on a `.project`), and writing a folder of ST files into a project.

A new project is made from a template: a `.project` that already holds the controller, its modules, libraries and
settings (the devices are described by Machine Expert's own device repository, which is not on this computer). Any
finished project of the same controller is a template: `template()` takes the program out of it.
"""
import io as _io
import os
import zipfile

import mearchive
import meproject
import plcme
import plcst

MAGIC_CLOSED = mearchive.CONTAINER_MAGIC
NULL = meproject.NULL
TYPES = {g: k for g, k in meproject.KINDS.items()}
GRAPHIC = {'25e509de-33d4-4447-93f8-c9e4ea381c8b': 'ld', '32d3375e-c010-41e2-9e43-b2fbf4f2b374': 'cfc'}


class ProjectFileError(ValueError):
    pass


def _read(path: str) -> bytes:
    try:
        with open(path, 'rb') as f:
            data = f.read()
    except OSError as exc:
        raise ProjectFileError(f'файл не читается: {exc}') from None
    if data[:16] == MAGIC_CLOSED:
        return data
    if not zipfile.is_zipfile(_io.BytesIO(data)):
        raise ProjectFileError('не файл проекта Machine Expert / CODESYS: ни архив, ни контейнер проекта')
    with zipfile.ZipFile(_io.BytesIO(data)) as z:
        names = z.namelist()
        if mearchive.STRING_TABLE_ENTRY in names:
            return data
        inner = [n for n in names if n.lower().endswith('.project')]
        if not inner:
            raise ProjectFileError('в архиве нет таблицы строк проекта — это не .project')
        return z.read(inner[0])            # .projectarchive: the project with its libraries and devices


def open_project(path: str) -> meproject.MEProject:
    data = _read(str(path))
    try:
        return meproject.MEProject.open(data)
    except (mearchive.FormatError, KeyError, IndexError, zipfile.BadZipFile) as exc:
        raise ProjectFileError(f'файл проекта не разобран: {exc}') from None


def container(path: str) -> str:
    with open(path, 'rb') as f:
        head = f.read(16)
    if head == MAGIC_CLOSED:
        return 'sealed'
    with zipfile.ZipFile(path) as z:
        return 'zip' if mearchive.STRING_TABLE_ENTRY in z.namelist() else 'archive'


def _folder_path(p, n):
    parts, f, seen = [], p.folder_of(n), set()
    while f is not None and f.guid not in seen:
        seen.add(f.guid)
        parts.append(f.name)
        f = p.folder_of(f)
    return '/'.join(reversed(parts))


def model(p: meproject.MEProject, path: str = '') -> plcme.Project:
    """The project as the model of plc-kit (the one `.export` gives)."""
    out = plcme.Project(source='machine-expert-project')
    out.path = str(path)
    pous = {}
    for n in p.objects():
        if n.kind == 'pou':
            decl, body, lang = p.text(n)
            language = 'st' if lang == 'st' else 'graphic'
            pou = {'name': n.name, 'declaration': decl or '', 'body': (body or '') if language == 'st' else '',
                   'language': language, 'folder': _folder_path(p, n), 'children': []}
            if language != 'st':
                pou['uses'] = p.names_in_drawing(n)
                pou['graphic'] = GRAPHIC.get(lang, 'graphic')
            pou.update({k: v for k, v in plcst.parse_declaration(decl or '').items() if k in ('kind', 'returns', 'extends')})
            pou['kind'] = pou['kind'] or 'program'
            out.pous.append(pou)
            pous[n.guid] = pou
        elif n.kind in ('gvl', 'persistent'):
            out.gvls.append({'name': n.name, 'declaration': p.text(n)[0] or '', 'folder': _folder_path(p, n)})
        elif n.kind == 'type':
            out.types.append({'name': n.name, 'declaration': p.text(n)[0] or '', 'folder': _folder_path(p, n)})
        elif n.kind == 'device':
            parent = p.nodes.get(n.parent)
            out.devices.append({'name': n.name, 'parent': parent.name if parent else ''})
        elif n.kind == 'application':
            out.application = n.name
    for n in p.objects():
        if n.kind in ('method', 'action', 'property') and n.parent in pous:
            decl, body, lang = p.text(n)
            pous[n.parent]['children'].append({'name': n.name, 'part': n.kind, 'declaration': decl or '',
                                               'body': (body or '') if lang == 'st' else ''})
    out.tasks = [{'name': t['name'], 'kind': t['kind'], 'priority': t['priority'], 'interval': t['interval'],
                  'pous': t['pous']} for t in p.tasks()]
    c = p.controller()
    out.controller = c.name if c else ''
    out.io_variables = {r['variable'].lower() for r in p.io_channels() if r['variable'] and r['new_variable']}
    published = p.opcua()
    if published:
        lists = list(dict.fromkeys(v.split('.', 1)[0] for v in published if '.' in v))
        out.link = {'kind': 'opcua', 'lists': lists}
    out.name = os.path.splitext(os.path.basename(str(path)))[0] if path else ''
    return out


def load(path: str) -> plcme.Project:
    return model(open_project(path), path)


def probe(path: str) -> dict:
    """What the file is and what it holds: the container, the tree of objects, tasks, channels, the OPC UA list."""
    p = open_project(path)
    kind = container(str(path))
    counts, objects = {}, []
    for n in p.objects():
        k = n.kind if n.kind in meproject.KINDS.values() else 'other'
        counts[k] = counts.get(k, 0) + 1
        if k in ('pou', 'gvl', 'type', 'task', 'device', 'application', 'interface', 'persistent', 'folder'):
            parent = p.nodes.get(n.parent)
            row = {'kind': k, 'name': n.name, 'path': p.path(parent) if parent else ''}
            if k == 'pou':
                row['language'] = 'st' if p.text(n)[2] == 'st' else GRAPHIC.get(p.text(n)[2], 'graphic')
            objects.append(row)
    objects.sort(key=lambda o: (o['kind'], o['path'], o['name']))
    channels = p.io_channels()
    published = p.opcua()
    c = p.controller()
    how = {'sealed': 'с проверкой целостности (так сохраняет Machine Expert)', 'zip': 'открытый архив',
           'archive': 'архив проекта (.projectarchive)'}[kind]
    return {'container': kind, 'counts': counts, 'objects': objects, 'tasks': p.tasks(),
            'controller': c.name if c else '',
            'channels': len(channels), 'channels_with_variables': sum(1 for r in channels if r['variable']),
            'opcua_variables': None if published is None else len(published),
            'note': f'Файл проекта Machine Expert, {how}: читается и пишется без программы. Программа, переменные, '
                    'адреса, теги — теми же инструментами, что для .export (plc_pous, plc_pou, plc_variables, '
                    'plc_hmi_tags, plc_export_st); каналы ввода-вывода — plc_io; новый проект из папки ST — '
                    'plc_project_build. Компиляция и загрузка в контроллер — в Machine Expert (machine-expert).'}


def _save(p: meproject.MEProject, out_file: str, overwrite: bool, sealed=True) -> int:
    if os.path.exists(out_file) and not overwrite:
        raise ProjectFileError(f'файл {out_file} уже есть: назовите другой или overwrite')
    os.makedirs(os.path.dirname(os.path.abspath(out_file)), exist_ok=True)
    data = p.to_bytes(sealed=sealed)
    tmp = out_file + '.tmp'
    with open(tmp, 'wb') as f:
        f.write(data)
    os.replace(tmp, out_file)
    return len(data)


def _verify(out_file: str, source) -> list:
    """Read the written file back and compare it with what was asked: the list of differences (empty — all there)."""
    back = load(out_file)
    bad = []
    have = {x['name'].lower(): x for x in back.pous}
    for x in source.pous:
        if x.get('language', 'st') != 'st':
            continue
        got = have.get(x['name'].lower())
        if got is None:
            bad.append(f"нет POU {x['name']}")
        elif meproject.MEProject._norm(x['declaration']) != got['declaration'] or meproject.MEProject._norm(x.get('body') or '') != got['body']:
            bad.append(f"текст {x['name']} отличается")
    for kind, mine, theirs in (('списка', source.gvls, back.gvls), ('типа', source.types, back.types)):
        have = {x['name'].lower(): x for x in theirs}
        for x in mine:
            got = have.get(x['name'].lower())
            if got is None or meproject.MEProject._norm(x['declaration']) != got['declaration']:
                bad.append(f"нет или отличается текст {kind} {x['name']}")
    return bad


def create(source, template_file: str, out_file: str, overwrite=False, keep_program=False) -> dict:
    """A new `.project`: the template (controller, modules, libraries, settings) with the program of `source` (the
    model of a folder of ST files). With `keep_program` what the template holds stays and is matched by name."""
    p = open_project(template_file)
    try:
        rep = p.apply(source, clean=not keep_program)
    except meproject.ProjectError as exc:
        raise ProjectFileError(str(exc)) from None
    rep['bytes'] = _save(p, out_file, overwrite)
    rep['out_file'] = out_file
    rep['verify'] = _verify(out_file, source)
    c = p.controller()
    rep['controller'] = c.name if c else ''
    return rep


def update(file: str, source, out_file: str = '') -> dict:
    """Write the changed ST files into an existing project (objects are matched by name; nothing is removed)."""
    p = open_project(file)
    try:
        rep = p.apply(source, clean=False)
    except meproject.ProjectError as exc:
        raise ProjectFileError(str(exc)) from None
    out = out_file or file
    rep['bytes'] = _save(p, out, True)
    rep['out_file'] = out
    rep['verify'] = _verify(out, source)
    return rep


def template(file: str, out_file: str, overwrite=False) -> dict:
    """A template from a finished project: the program is taken out, the controller with its modules stays."""
    p = open_project(file)
    removed = p.strip_program()
    size = _save(p, out_file, overwrite)
    c = p.controller()
    return {'out_file': out_file, 'bytes': size, 'removed': removed, 'controller': c.name if c else '',
            'devices': [n.name for n in p.objects() if n.kind == 'device'], 'tasks': [t['name'] for t in p.tasks()],
            'channels': len(p.io_channels())}


def io(path: str) -> list:
    return open_project(path).io_channels()


def set_io(file: str, changes: list, out_file: str = '') -> dict:
    """Put variables on channels: [{'device', 'channel', 'variable'}]."""
    p = open_project(file)
    done = []
    try:
        for c in changes:
            was = p.set_io(str(c['device']), str(c['channel']), str(c.get('variable') or ''))
            done.append({'device': was['device'], 'channel': was['channel'], 'was': was['variable'],
                         'now': str(c.get('variable') or '') or None})
    except meproject.ProjectError as exc:
        raise ProjectFileError(str(exc)) from None
    out = out_file or file
    return {'out_file': out, 'bytes': _save(p, out, True), 'changed': done}
