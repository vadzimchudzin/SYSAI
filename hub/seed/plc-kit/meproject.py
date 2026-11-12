"""Machine Expert / CODESYS V3.5 `.project` as an object tree that can be read, changed and written without the program.

Built on `mearchive` (the container, the zip, the binary archives and the shared string / schema tables). This
module knows the project objects: programs, function blocks, functions, methods, actions, data types, variable
lists, folders and tasks — how each is laid out in a file saved by Machine Expert 2.2 (CODESYS 3.5.19.40), and
writes them the same way.

    p = MEProject.open('Line.project')
    for o in p.objects(): print(o.path, o.kind)
    p.text(o) -> (declaration, implementation)
    p.add_pou('Main', 'PROGRAM Main\nVAR\nEND_VAR', 'x := 1;', parent=p.application())
    p.set_task('MAST', interval='20ms', pous=['Main'])
    p.save('New.project')

What the program derives itself is dropped on a change and rebuilt by Machine Expert at the next open or
compile: the pre-compile caches (`*.precompileinfo.auxiliary`).
"""
import base64
import datetime
import hashlib
import json
import re
import uuid

import mearchive as M

NULL = M.NULL_GUID

T_META = '81297157-7ec9-45ce-845e-84cab2b88ade'
T_POU = '6f9dac99-8de1-4efc-8465-68ac443b7d08'
T_METHOD = 'f8a58466-d7f6-439f-bbb8-d4600e41d099'
T_ACTION = '8ac092e5-3128-4e26-9e7e-11016c6684f2'
T_PROPERTY = '5a3b8626-d3e9-4f37-98b5-66420063d91e'
T_ACCESSOR = '792f2eb6-721e-4e64-ba20-bc98351056db'
T_GVL = 'ffbfa93a-b94d-45fc-a329-229860183b1d'
T_PERSIST = '261bd6e6-249c-4232-bb6f-84c2fbeef430'
T_DUT = '2db5746d-d284-4425-9f7f-2663a34b0ebc'
T_FOLDER = '738bea1e-99bb-4f04-90bb-a7a567e74e3a'
T_DEVICE = '225bfe47-7336-4dbc-9419-4105a7c831fa'
T_PLCLOGIC = '40b404f9-e5dc-42c6-907f-c89f4a517386'
T_APP = '639b491f-5557-464c-af91-1471bac9f549'
T_TASKCFG = 'ae1de277-a207-4a28-9efb-456c06bd52f3'
T_TASK = '98a2708a-9b18-4f31-82ed-a1465b24fa2d'
T_LIBMAN = 'adb5cb65-8e1d-4a00-b70a-375ea27582f3'
T_SETTINGS = '8753fe6f-4a22-4320-8103-e553c4fc8e04'
T_INFO = '085afe48-c5d8-4ea5-ab0d-b35701fa6009'
T_INTERFACE = '6654496c-404d-479a-aad2-8551054e5f1e'

# parts of an object
T_DECL = 'a9ed5b7e-75c5-4651-af16-d2c27e98cb94'      # textual declaration ("Interface")
T_ST = '3b83b776-fb25-43b8-99f2-3c507c9143fc'        # ST implementation
T_TEXTDOC = 'f3878285-8e4f-490b-bb1b-9acbb7eb04db'
T_TIME = '68e48988-4399-454b-ae43-e500b779c242'
T_WATCHDOG = '97c20fcf-2c50-4dbb-8faa-06ff4f1e4275'
T_POUCALL = 'f194d1ef-7376-42ce-a729-4a5485a97a46'
T_FOLDER_PROP = '829a18f2-c514-4f6e-9634-1df173429203'
VIEW_POUS = '21af5390-2942-461a-bf89-951aaf6999f1'   # the structured view folders live in

# construction types (as the program writes them)
C_GUID = 'System.Guid'
C_PROPS = '{2c41fa04-1834-41c1-816e-303c7aa2c05b}'
C_SPECIAL = '{0db3d7bb-cde0-4416-9a7b-ce49a0124323}'
C_LEVEL = '{8e575c5b-1d37-49c6-941b-5c0ec7874787}'
C_KINDOFTASK = '{38cb1f21-e463-4240-9171-f74fb561a060}'
C_TASKPARAM = '{3e5ba045-1fc5-40ec-9ce7-4b62ec8ccfdf}'
C_POULIST = '{33f568bb-4a2c-48cb-a762-34cca3066d03}'
C_FOLDER_PARENTS = '{fa2ee218-a39b-4b6d-b249-49dbddbd168a}'

KINDS = {T_POU: 'pou', T_METHOD: 'method', T_ACTION: 'action', T_PROPERTY: 'property', T_ACCESSOR: 'accessor',
         T_GVL: 'gvl', T_PERSIST: 'persistent', T_DUT: 'type', T_FOLDER: 'folder', T_DEVICE: 'device',
         T_PLCLOGIC: 'plc_logic', T_APP: 'application', T_TASKCFG: 'task_configuration', T_TASK: 'task',
         T_LIBMAN: 'library_manager', T_SETTINGS: 'project_settings', T_INFO: 'project_information',
         T_INTERFACE: 'interface'}
CODE_KINDS = ('pou', 'method', 'action', 'property', 'accessor', 'gvl', 'persistent', 'type', 'interface')


class ProjectError(ValueError):
    pass


def ticks(dt=None) -> int:
    """.NET ticks (100 ns since 0001-01-01, UTC) — the Timestamp of a meta object."""
    dt = dt or datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    return (dt - datetime.datetime(1, 1, 1)) // datetime.timedelta(microseconds=1) * 10


def _guid(v):
    return str(v).lower() if v else NULL


def _g(value):
    """A Guid value the way the archive holds it."""
    return M.Prim(M.F_TYPECONVERTER, _guid(value), C_GUID)


def line_ids(text: str, first_id: int = 1):
    """The line table Machine Expert keeps beside a text (`<guid>_decl_lineids.auxiliary`): where each line ends
    and its permanent number. -> (bytes of the entry, the last number used)."""
    ends, pos = [], 0
    while True:
        k = text.find('\n', pos)
        if k < 0:
            break
        ends.append(k + 1)
        pos = k + 1
    ends.append(len(text) + 1)
    n = len(ends)
    size = 2 * (n + 1)
    ids = list(range(first_id, first_id + n))
    doc = {'EndOffsets': ends + [0] * (size - n), 'LineIds': ids + [0] * (size - n), 'LineCount': n, 'GapLine': -1,
           'GapWidth': 0, 'Hash': base64.b64encode(hashlib.sha256(text.encode('utf-8')).digest()).decode('ascii')}
    return json.dumps(doc, separators=(',', ':')).encode('ascii'), first_id + n - 1


class Node:
    """One object of the project tree (what a `.meta` entry says)."""
    __slots__ = ('guid', 'parent', 'name', 'type', 'meta')

    def __init__(self, meta):
        self.meta = meta
        self.guid = _guid(meta['Guid'])
        self.parent = _guid(meta['ParentGuid'])
        self.name = meta['Name']
        self.type = _guid(meta['TypeGuid'])

    @property
    def kind(self):
        return KINDS.get(self.type, self.type)

    def __repr__(self):
        return f'<{self.kind} {self.name} {self.guid}>'


class MEProject:
    def __init__(self, archive: M.Project):
        self.a = archive
        self.nodes = {}
        for g in archive.object_guids():
            n = Node(archive.load(g + '.meta'))
            self.nodes[n.guid] = n
        self._stamp = ticks()

    # ---- open / save ----------------------------------------------------------------------------------------
    @classmethod
    def open(cls, src) -> 'MEProject':
        return cls(M.Project.open(src))

    def to_bytes(self, sealed=None) -> bytes:
        now = datetime.datetime.now()
        stamp = (now.year, now.month, now.day, now.hour, now.minute, now.second - now.second % 2)
        return self.a.to_bytes(sealed=sealed, date_time=stamp)

    def save(self, path, sealed=None) -> int:
        data = self.to_bytes(sealed)
        with open(path, 'wb') as f:
            f.write(data)
        return len(data)

    # ---- the tree -------------------------------------------------------------------------------------------
    def objects(self):
        return list(self.nodes.values())

    def node(self, ref) -> Node:
        if isinstance(ref, Node):
            return ref
        n = self.nodes.get(_guid(ref))
        if not n:
            raise ProjectError(f'в проекте нет объекта {ref}')
        return n

    def children(self, parent=None, kind=None):
        pg = NULL if parent is None else self.node(parent).guid
        return [n for n in self.nodes.values() if n.parent == pg and (kind is None or n.kind == kind)]

    def descendants(self, parent):
        out = []
        for c in self.children(parent):
            out.append(c)
            out += self.descendants(c)
        return out

    def find(self, name=None, kind=None, parent=None, deep=True):
        pool = self.nodes.values() if parent is None else (self.descendants(parent) if deep else self.children(parent))
        return [n for n in pool if (name is None or n.name.lower() == name.lower()) and (kind is None or n.kind == kind)]

    def path(self, ref) -> str:
        n, parts = self.node(ref), []
        while n:
            parts.append(n.name)
            n = self.nodes.get(n.parent)
        return '/'.join(reversed(parts))

    def folder_of(self, ref):
        """The folder an object is shown in (folders are a view: the object keeps its real parent)."""
        props = self.node(ref).meta['Properties']
        fp = props.get(T_FOLDER_PROP) if len(props) else None
        if fp is None:
            return None
        g = _guid(fp['ParentObjects'].get(VIEW_POUS))
        return self.nodes.get(g)

    def applications(self):
        return [n for n in self.nodes.values() if n.kind == 'application']

    def application(self) -> Node:
        apps = self.applications()
        if not apps:
            raise ProjectError('в проекте нет приложения (Application): нужен шаблон с контроллером')
        return apps[0]

    def controller(self):
        """The top device that holds the application."""
        apps = self.applications()
        n = apps[0] if apps else None
        while n and not (n.kind == 'device' and n.parent == NULL):
            n = self.nodes.get(n.parent)
        return n

    # ---- reading --------------------------------------------------------------------------------------------
    def object(self, ref) -> M.Obj:
        return self.a.load(self.node(ref).guid + '.object')

    @staticmethod
    def _doc_text(part):
        if part is None or not isinstance(part, M.Obj) or part.is_null or 'TextDocument' not in part:
            return None
        return part['TextDocument']['TextBlobForSerialisation']

    def text(self, ref):
        """-> (declaration | None, implementation | None, language): language is 'st' or the type of a graphic body."""
        o = self.object(ref)
        decl = self._doc_text(o.get('Interface'))
        impl_part = o.get('Implementation')
        impl = self._doc_text(impl_part)
        lang = None
        if isinstance(impl_part, M.Obj) and not impl_part.is_null:
            lang = 'st' if impl_part.type_guid == T_ST else impl_part.type_guid
        return decl, impl, lang

    def tasks(self):
        out = []
        for n in self.nodes.values():
            if n.kind != 'task':
                continue
            o = self.object(n)
            iv = o['Interval']
            out.append({'name': n.name, 'guid': n.guid, 'kind': o['Kindoftask'], 'priority': o['Priority'],
                        'interval': f"{iv['Time']}{iv['Unit']}", 'pous': [c['Name'] for c in o['PouList']]})
        return out

    # ---- writing: common ------------------------------------------------------------------------------------
    def _touch(self):
        """Something of the program changed: the caches Machine Expert builds from the program are stale."""
        for name in [n for n in self.a.entries if n.lower().endswith('.precompileinfo.auxiliary')]:
            self.a.remove(name)

    def _meta(self, guid, parent, name, type_guid, embedded, folder=None):
        props = M.Dict(M.F_HH_DICT, C_PROPS, (M.F_NULL, None), (M.F_NULL, None))
        if folder:
            props = self._folder_props(folder)
        emb = None
        if embedded:
            emb = M.Coll(M.F_HOM_ARRAY, C_GUID, (M.F_TYPECONVERTER, C_GUID), [_guid(e) for e in embedded])
        m = M.Obj(T_META, [
            ('Guid', M.F_TYPECONVERTER, C_GUID, _guid(guid)),
            ('ParentGuid', M.F_TYPECONVERTER, C_GUID, _guid(parent)),
            ('Name', M.F_STRING, None, name),
            ('Properties', props.fmt, props.ctype, props),
            ('TypeGuid', M.F_TYPECONVERTER, C_GUID, _guid(type_guid)),
            ('EmbeddedTypeGuids', *( (emb.fmt, emb.ctype, emb) if emb else (M.F_NULL, None, None) )),
            ('Timestamp', M.F_LONG, None, self._next_stamp()),
        ])
        return m

    def _next_stamp(self):
        self._stamp += 10000
        return self._stamp

    def _folder_props(self, folder):
        f = self.node(folder)
        if f.kind != 'folder':
            raise ProjectError(f'{f.name} — не папка')
        parents = M.Dict(M.F_HH_DICT, C_FOLDER_PARENTS, (M.F_TYPECONVERTER, C_GUID), (M.F_TYPECONVERTER, C_GUID),
                         [[VIEW_POUS, f.guid]])
        prop = M.Obj(T_FOLDER_PROP, [('ParentObjects', parents.fmt, parents.ctype, parents)])
        return M.Dict(M.F_HH_DICT, C_PROPS, (M.F_TYPECONVERTER, C_GUID), (M.F_ARCHIVABLE, None), [[T_FOLDER_PROP, prop]])

    def _put(self, meta, obj):
        n = Node(meta)
        self.a.store(n.guid + '.meta', meta)
        self.a.store(n.guid + '.object', obj)
        self.nodes[n.guid] = n
        self._touch()
        return n

    def _check_name(self, name, parent):
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name or ''):
            raise ProjectError(f'имя «{name}» не годится для объекта ПЛК (латиница, цифры, _)')
        pg = NULL if parent is None else self.node(parent).guid
        for n in self.nodes.values():
            if n.parent == pg and n.name.lower() == name.lower():
                raise ProjectError(f'объект {name} уже есть в {self.path(n)}')

    def _textdoc(self, guid, text, which):
        return M.Obj(T_TEXTDOC, [
            ('TextBlobForSerialisation', M.F_STRING, None, text),
            ('LineInfoPersistence', M.F_STRING, None, f'{guid}_{which}_LineIds'),
        ])

    def _decl(self, guid, text):
        return M.Obj(T_DECL, [('TextDocument', M.F_ARCHIVABLE, None, self._textdoc(guid, text, 'Decl'))])

    def _impl(self, guid, text):
        return M.Obj(T_ST, [('TextDocument', M.F_ARCHIVABLE, None, self._textdoc(guid, text, 'Impl'))])

    def _lines(self, guid, decl, impl):
        """Write the line tables of an object's texts; -> the last line number used."""
        last = 0
        if decl is not None:
            data, last = line_ids(decl, last + 1)
            self.a.store(f'{guid}_decl_lineids.auxiliary', data)
        if impl is not None:
            data, last = line_ids(impl, last + 1)
            self.a.store(f'{guid}_impl_lineids.auxiliary', data)
        return last

    @staticmethod
    def _norm(text):
        return (text or '').replace('\r\n', '\n').replace('\r', '\n')

    # ---- writing: objects -----------------------------------------------------------------------------------
    def add_folder(self, name, parent=None, folder=None) -> Node:
        """A folder. `parent` — where it lives (None: the project level, an application, a device); `folder` — the
        folder it is shown in."""
        if not str(name or '').strip():
            raise ProjectError('у папки нужно имя')
        pg = NULL if parent is None else self.node(parent).guid
        guid = str(uuid.uuid4())
        meta = self._meta(guid, pg, name, T_FOLDER, None, folder)
        obj = M.Obj(T_FOLDER, [('StructuredViewGuid', M.F_TYPECONVERTER, C_GUID, VIEW_POUS)])
        return self._put(meta, obj)

    def add_pou(self, name, declaration, implementation='', parent=None, folder=None) -> Node:
        """A program, function block or function on ST: what it is, the declaration says."""
        self._check_name(name, parent)
        pg = NULL if parent is None else self.node(parent).guid
        guid = str(uuid.uuid4())
        decl, impl = self._norm(declaration), self._norm(implementation)
        last = self._lines(guid, decl, impl)
        empty = M.Coll(M.F_HOM_LIST, 'System.Collections.ArrayList', (M.F_NULL, None), [])
        obj = M.Obj(T_POU, [
            ('SpecialFunc', M.F_TYPECONVERTER, C_SPECIAL, 'None'),
            ('Implementation', M.F_ARCHIVABLE, None, self._impl(guid, impl)),
            ('Interface', M.F_ARCHIVABLE, None, self._decl(guid, decl)),
            ('UniqueIdGenerator', M.F_STRING, None, str(last)),
            ('POULevel', M.F_TYPECONVERTER, C_LEVEL, 'Standard'),
            ('ChildObjectGuids', empty.fmt, empty.ctype, empty),
            ('AddAttributeSubsequent', M.F_BOOL, None, False),
        ])
        return self._put(self._meta(guid, pg, name, T_POU, [T_DECL, T_ST], folder), obj)

    def add_method(self, pou, name, declaration, implementation='') -> Node:
        owner = self.node(pou)
        self._check_name(name, owner)
        guid = str(uuid.uuid4())
        decl, impl = self._norm(declaration), self._norm(implementation)
        self._lines(guid, decl, impl)
        obj = M.Obj(T_METHOD, [
            ('Implementation', M.F_ARCHIVABLE, None, self._impl(guid, impl)),
            ('Interface', M.F_ARCHIVABLE, None, self._decl(guid, decl)),
        ])
        return self._put(self._meta(guid, owner.guid, name, T_METHOD, [T_DECL, T_ST]), obj)

    def add_action(self, pou, name, implementation='') -> Node:
        owner = self.node(pou)
        self._check_name(name, owner)
        guid = str(uuid.uuid4())
        impl = self._norm(implementation)
        last = self._lines(guid, None, impl)
        obj = M.Obj(T_ACTION, [
            ('Implementation', M.F_ARCHIVABLE, None, self._impl(guid, impl)),
            ('UniqueIdGenerator', M.F_STRING, None, str(last)),
        ])
        return self._put(self._meta(guid, owner.guid, name, T_ACTION, [T_ST]), obj)

    def add_gvl(self, name, declaration, parent=None, folder=None) -> Node:
        self._check_name(name, parent)
        pg = NULL if parent is None else self.node(parent).guid
        guid = str(uuid.uuid4())
        decl = self._norm(declaration)
        self._lines(guid, decl, None)
        obj = M.Obj(T_GVL, [
            ('Interface', M.F_ARCHIVABLE, None, self._decl(guid, decl)),
            ('NetVarProperties', M.F_NULL, None, None),
            ('ParameterList', M.F_BOOL, None, False),
            ('AddAttributeSubsequent', M.F_BOOL, None, False),
        ])
        return self._put(self._meta(guid, pg, name, T_GVL, [T_DECL], folder), obj)

    def add_type(self, name, declaration, parent=None, folder=None) -> Node:
        self._check_name(name, parent)
        pg = NULL if parent is None else self.node(parent).guid
        guid = str(uuid.uuid4())
        decl = self._norm(declaration)
        last = self._lines(guid, decl, None)
        obj = M.Obj(T_DUT, [
            ('Interface', M.F_ARCHIVABLE, None, self._decl(guid, decl)),
            ('UniqueIdGenerator', M.F_STRING, None, str(last)),
        ])
        return self._put(self._meta(guid, pg, name, T_DUT, [T_DECL], folder), obj)

    def set_text(self, ref, declaration=None, implementation=None):
        """Replace the texts of an object (None — leave as is). The implementation must be ST."""
        n = self.node(ref)
        o = self.object(n)
        decl_old, impl_old, lang = self.text(n)
        if declaration is not None:
            if 'Interface' not in o or decl_old is None:
                raise ProjectError(f'у объекта {n.name} нет текстового объявления')
            o['Interface']['TextDocument']['TextBlobForSerialisation'] = self._norm(declaration)
        if implementation is not None:
            if lang != 'st':
                raise ProjectError(f'объект {n.name} в проекте написан не на ST')
            o['Implementation']['TextDocument']['TextBlobForSerialisation'] = self._norm(implementation)
        decl, impl = self._doc_text(o.get('Interface')), self._doc_text(o.get('Implementation')) if lang == 'st' else None
        last = self._lines(n.guid, decl, impl)
        if 'UniqueIdGenerator' in o:
            o['UniqueIdGenerator'] = str(max(last, int(o['UniqueIdGenerator'] or 0)))
        n.meta['Timestamp'] = self._next_stamp()
        self.a.store(n.guid + '.meta', n.meta)
        self.a.store(n.guid + '.object', o)
        self._touch()

    def rename(self, ref, name):
        n = self.node(ref)
        n.meta['Name'] = name
        n.meta['Timestamp'] = self._next_stamp()
        n.name = name
        self.a.store(n.guid + '.meta', n.meta)
        self._touch()

    def remove(self, ref):
        """Delete an object with everything inside it and what is shown in it (a folder)."""
        n = self.node(ref)
        doomed = [n] + self.descendants(n)
        if n.kind == 'folder':
            for x in list(self.nodes.values()):
                f = self.folder_of(x)
                if f is not None and f.guid == n.guid and x not in doomed:
                    doomed += [x] + self.descendants(x)
        for x in doomed:
            for ext in ('.meta', '.object'):
                try:
                    self.a.remove(x.guid + ext)
                except KeyError:
                    pass
            for name in [e for e in self.a.entries if e.lower().startswith(x.guid)]:
                self.a.remove(name)
            self.nodes.pop(x.guid, None)
        self._touch()
        return [x.name for x in doomed]

    # ---- writing: tasks -------------------------------------------------------------------------------------
    @staticmethod
    def _interval(text):
        m = re.fullmatch(r'\s*(?:T#)?\s*(\d+)\s*(ms|us|µs|s)?\s*', str(text), re.I)
        if not m:
            raise ProjectError(f'период задачи «{text}» не понят: нужно, например, 20ms')
        unit = (m.group(2) or 'ms').lower()
        if unit == 's':
            return str(int(m.group(1)) * 1000), 'ms'
        return m.group(1), 'µs' if unit in ('us', 'µs') else 'ms'

    def _pou_calls(self, names):
        items = [M.Obj(T_POUCALL, [('Name', M.F_STRING, None, x), ('Comment', M.F_STRING, None, '')]) for x in names]
        return M.Coll(M.F_HOM_LIST, C_POULIST, (M.F_ARCHIVABLE, None) if items else (M.F_NULL, None), items)

    def set_task(self, name, interval=None, priority=None, pous=None) -> Node:
        """Set the period, the priority and the programs of a task; the task is created if the configuration has none
        with this name (a cyclic one, laid out as Machine Expert lays it)."""
        found = self.find(name, 'task')
        if found:
            n = found[0]
            o = self.object(n)
        else:
            cfgs = [x for x in self.nodes.values() if x.kind == 'task_configuration']
            if not cfgs:
                raise ProjectError('в проекте нет «Конфигурации задач»: нужен шаблон с контроллером')
            guid = str(uuid.uuid4())

            def params():
                return M.Coll(M.F_HOM_LIST, C_TASKPARAM, (M.F_NULL, None), [])
            time = M.Obj(T_TIME, [('Time', M.F_STRING, None, '100'), ('Unit', M.F_STRING, None, 'ms')])
            watchdog = M.Obj(T_WATCHDOG, [('Enabled', M.F_BOOL, None, False), ('Time', M.F_ARCHIVABLE, None, time),
                                          ('Sensitivity', M.F_STRING, None, '1')])
            ival = M.Obj(T_TIME, [('Time', M.F_STRING, None, '20'), ('Unit', M.F_STRING, None, 'ms')])
            fields = [('AllowEmpty', M.F_BOOL, None, False), ('Kindoftask', M.F_TYPECONVERTER, C_KINDOFTASK, 'Cyclic')]
            for f in ('CustomParameters', 'IntervalParameters', 'EventParameters', 'ExternalEventParameters',
                      'FreewheelingParameters'):
                c = params()
                fields.append((f, c.fmt, c.ctype, c))
            calls = self._pou_calls([])
            fields += [('Priority', M.F_STRING, None, '15'), ('Watchdog', M.F_ARCHIVABLE, None, watchdog),
                       ('Interval', M.F_ARCHIVABLE, None, ival), ('Event', M.F_STRING, None, ''),
                       ('ExternalEvent', M.F_STRING, None, ''),
                       ('TaskGuid', M.F_TYPECONVERTER, C_GUID, str(uuid.uuid4())),
                       ('EventPOUGuid', M.F_TYPECONVERTER, C_GUID, str(uuid.uuid4())),
                       ('PouList', calls.fmt, calls.ctype, calls), ('Implicit', M.F_BOOL, None, False),
                       ('WithinSPSTimeSlicing', M.F_BOOL, None, True), ('Core', M.F_INT, None, -1),
                       ('Core2', M.F_INT, None, -1), ('TaskGroup', M.F_STRING, None, 'IEC-Tasks'),
                       ('SynchTask', M.F_STRING, None, '')]
            o = M.Obj(T_TASK, fields)
            n = self._put(self._meta(guid, cfgs[0].guid, name, T_TASK, None), o)
        if interval is not None:
            t, u = self._interval(interval)
            o['Interval']['Time'], o['Interval']['Unit'] = t, u
        if priority not in (None, ''):
            o['Priority'] = str(priority)
        if pous is not None:
            calls = self._pou_calls(list(pous))
            o.set('PouList', calls, calls.fmt, calls.ctype)
        n.meta['Timestamp'] = self._next_stamp()
        self.a.store(n.guid + '.meta', n.meta)
        self.a.store(n.guid + '.object', o)
        self._touch()
        return n

    # ---- input / output channels of the devices -------------------------------------------------------------
    def _channels(self, dev):
        """Every channel of a device object that can carry a variable: (element, direction)."""
        out = []

        def walk(v, direction):
            if isinstance(v, M.Obj) and not v.is_null:
                if 'ChannelType' in v and isinstance(v['ChannelType'], str):
                    direction = v['ChannelType']
                im = v.get('IoMapping')
                if isinstance(im, M.Obj) and not im.is_null and 'Mappings' in im:
                    out.append((v, direction))
                for x in v.values:
                    walk(x, direction)
            elif isinstance(v, M.Coll):
                if isinstance(v.items, list):
                    for x in v.items:
                        walk(x, direction)
            elif isinstance(v, M.Dict):
                for _, x in v.items:
                    walk(x, direction)
        walk(dev, None)
        return out

    @staticmethod
    def _channel_row(el, direction):
        vn = el.get('VisibleName')
        label = vn.get('Default') if isinstance(vn, M.Obj) and not vn.is_null else None
        ds = el.get('Description')
        text = ds.get('Default') if isinstance(ds, M.Obj) and not ds.is_null else None
        maps = el['IoMapping']['Mappings']
        lst = maps['Mappings'] if isinstance(maps, M.Obj) and not maps.is_null and 'Mappings' in maps else []
        var, create = None, False
        for m_ in lst:
            if isinstance(m_, M.Obj) and not m_.is_null and m_.get('Variable'):
                var, create = m_['Variable'], bool(m_.get('CreateVariable', False))
                break
        typ = el.get('Type') if isinstance(el.get('Type'), str) else None
        return {'channel': label or str(el.get('Identifier') or ''), 'id': str(el.get('Identifier') or ''),
                'direction': (direction or '').lower() or None, 'type': typ, 'description': text or '',
                'variable': var, 'new_variable': create}

    def io_channels(self):
        """The channels of the controller and its modules with the variables put on them:
        [{'device', 'path', 'channel' (I0, Q3, IW0…), 'direction', 'type', 'description', 'variable', 'new_variable'}].
        `variable` is as Machine Expert keeps it: `Application.Name` — a variable of the program, a bare name with
        `new_variable` — one the mapping itself declares."""
        rows = []
        for n in self.nodes.values():
            if n.kind != 'device':
                continue
            for el, direction in self._channels(self.object(n)):
                rows.append(dict(self._channel_row(el, direction), device=n.name, path=self.path(n)))
        return rows

    def set_io(self, device, channel, variable):
        """Put a variable of the program on a channel (`variable` — its name; '' takes the variable off). The channel
        is told by its label (I0, Q3) or identifier. -> the row of the channel as it was."""
        devs = [n for n in self.nodes.values() if n.kind == 'device' and device.lower() in (n.name.lower(), n.guid)]
        if not devs:
            raise ProjectError(f'в проекте нет устройства {device}')
        for n in devs:
            o = self.object(n)
            for el, direction in self._channels(o):
                row = self._channel_row(el, direction)
                if channel.lower() not in (row['channel'].lower(), row['id'].lower()):
                    continue
                maps = el['IoMapping']['Mappings']
                if not isinstance(maps, M.Obj) or maps.is_null or 'Mappings' not in maps:
                    raise ProjectError(f'канал {channel} устройства {n.name} не принимает переменную')
                items = []
                if variable:
                    name = variable if '.' in variable else f'{self.application().name}.{variable}'
                    items = [M.Obj('47edf8ea-3f84-452c-b998-e18f878578d3', [('Variable', M.F_STRING, None, name)])]
                coll = maps['Mappings']
                new = M.Coll(coll.fmt, coll.ctype, (M.F_ARCHIVABLE, None) if items else (M.F_NULL, None), items)
                maps.set('Mappings', new, new.fmt, new.ctype)
                n.meta['Timestamp'] = self._next_stamp()
                self.a.store(n.guid + '.meta', n.meta)
                self.a.store(n.guid + '.object', o)
                self._touch()
                return dict(row, device=n.name)
        raise ProjectError(f'у устройства {device} нет канала {channel}')

    # ---- OPC UA: what the controller publishes --------------------------------------------------------------
    T_OPCUA = '54fe9644-1b3c-4386-ae33-e37a105a31f4'      # «OPC UA Symbol Configuration» of Machine Expert

    def opcua(self):
        """The variables the controller publishes over OPC UA (None — the project has no such object)."""
        for n in self.nodes.values():
            if n.type == self.T_OPCUA:
                return list(self.object(n)['SelectedVariables'])
        return None

    def set_opcua(self, variables):
        """Publish exactly these variables (`List.Name`). The object must be in the project (the template has it
        when its controller serves OPC UA)."""
        for n in self.nodes.values():
            if n.type != self.T_OPCUA:
                continue
            o = self.object(n)
            names = list(dict.fromkeys(variables))
            sel = o['SelectedVariables']
            new = M.Coll(sel.fmt, sel.ctype, (M.F_STRING, None) if names else (M.F_NULL, None), names)
            o.set('SelectedVariables', new, new.fmt, new.ctype)
            if 'SelectedVariablesWithAccessRights' in o:
                d = o['SelectedVariablesWithAccessRights']
                old = dict((k, v) for k, v in d.items)
                nd = M.Dict(d.fmt, d.ctype, (M.F_STRING, None) if names else (M.F_NULL, None),
                            (M.F_INT, None) if names else (M.F_NULL, None), [[k, old.get(k, 8)] for k in names])
                o.set('SelectedVariablesWithAccessRights', nd, nd.fmt, nd.ctype)
            n.meta['Timestamp'] = self._next_stamp()
            self.a.store(n.guid + '.meta', n.meta)
            self.a.store(n.guid + '.object', o)
            self._touch()
            return len(names)
        raise ProjectError('в проекте нет объекта «OPC UA Symbol Configuration»: добавьте его в шаблон в Machine Expert')

    # ---- drawn POUs -----------------------------------------------------------------------------------------
    def names_in_drawing(self, ref):
        """The names a drawn POU (LD / FBD / CFC) uses: the texts of its elements, in order, each once."""
        o = self.object(ref)
        impl = o.get('Implementation')
        seen = {}

        def walk(v):
            if isinstance(v, M.Obj) and not v.is_null:
                for k, x in zip(v.names, v.values):
                    if isinstance(x, str):
                        if k in ('Operand', 'BoxType', 'Text', 'Label', 'Variable', 'Expression', 'TypeName') and x.strip():
                            seen.setdefault(x.strip())
                    else:
                        walk(x)
            elif isinstance(v, M.Coll):
                if isinstance(v.items, list):
                    for x in v.items:
                        walk(x)
            elif isinstance(v, M.Dict):
                for _, x in v.items:
                    walk(x)
        walk(impl)
        return list(seen)

    # ---- the program as a whole -----------------------------------------------------------------------------
    PROGRAM_KINDS = ('pou', 'method', 'action', 'property', 'accessor', 'gvl', 'persistent', 'type', 'interface', 'folder')

    def strip_program(self):
        """Take the program out: programs, blocks, functions, lists, types and their folders. Devices, libraries,
        the task configuration and the settings stay — what is left is a template. -> names removed."""
        gone = []
        for n in list(self.nodes.values()):
            if n.guid in self.nodes and n.kind in self.PROGRAM_KINDS:
                gone += self.remove(n)
        for n in self.nodes.values():
            if n.kind == 'task':
                self.set_task(n.name, pous=[])
        if self.opcua():
            self.set_opcua([])
        return gone

    def home(self):
        """Where the program lives: the application of the controller, or the project level when there is none."""
        apps = self.applications()
        return apps[0] if apps else None

    def _folder_path(self, path, parent):
        """The folder `A/B` under `parent`, made when missing. -> its node (None for an empty path)."""
        cur = None
        for part in [x for x in (path or '').split('/') if x.strip()]:
            pg = NULL if parent is None else self.node(parent).guid
            found = None
            for n in self.nodes.values():
                if n.kind == 'folder' and n.parent == pg and n.name.lower() == part.lower():
                    f = self.folder_of(n)
                    if (f.guid if f else None) == (cur.guid if cur else None):
                        found = n
                        break
            cur = found or self.add_folder(part, parent, cur)
        return cur

    def apply(self, source, clean=False):
        """Write a program into the project. `source` — the model plc-kit reads from a folder of ST files (`types`,
        `gvls`, `pous`, `tasks`, `link`). With `clean` what the project held before is taken out first (the project
        was a template or a donor); otherwise objects are matched by name: found ones get the new text, new ones
        are added, the rest stay. -> {'created', 'updated', 'removed', 'kept', 'skipped', 'warnings', 'tasks'}."""
        rep = {'created': [], 'updated': [], 'removed': [], 'kept': [], 'skipped': [], 'warnings': [], 'tasks': []}
        if clean:
            rep['removed'] = self.strip_program()
        home = self.home()
        where = home.guid if home is not None else NULL
        by_name = {n.name.lower(): n for n in self.nodes.values()
                   if n.parent == where and n.kind in ('pou', 'gvl', 'type', 'persistent')}
        wanted = set()

        def put(kind, item, body=None):
            name = item['name']
            wanted.add(name.lower())
            old = by_name.get(name.lower())
            decl = item['declaration']
            if old is not None:
                want_kind = {'type': 'type', 'gvl': 'gvl', 'pou': 'pou'}[kind]
                if old.kind != want_kind and not (want_kind == 'gvl' and old.kind == 'persistent'):
                    raise ProjectError(f'{name}: в проекте это {old.kind}, а в папке — {want_kind}')
                d0, b0, lang = self.text(old)
                if kind == 'pou' and lang != 'st':
                    raise ProjectError(f'объект {name} в проекте написан не на ST: текстом его не заменить')
                if self._norm(decl) != d0 or (kind == 'pou' and self._norm(body) != (b0 or '')):
                    self.set_text(old, decl, body if kind == 'pou' else None)
                    rep['updated'].append(name)
                else:
                    rep['kept'].append(name)
                return old
            folder = self._folder_path(item.get('folder') or '', home)
            if kind == 'type':
                n = self.add_type(name, decl, home, folder)
            elif kind == 'gvl':
                n = self.add_gvl(name, decl, home, folder)
            else:
                n = self.add_pou(name, decl, body or '', home, folder)
            rep['created'].append(name)
            return n
        for t in source.types:
            put('type', t)
        for g in source.gvls:
            put('gvl', g)
        order = {'function': 0, 'function_block': 1, 'program': 2}
        for x in sorted(source.pous, key=lambda x: order.get(x.get('kind'), 3)):
            if x.get('language', 'st') != 'st':
                rep['skipped'].append(x['name'])
                continue
            n = put('pou', x, x.get('body') or '')
            have = {c.name.lower(): c for c in self.children(n)}
            for c in x.get('children') or []:
                old = have.get(c['name'].lower())
                if c.get('part') == 'method':
                    if old:
                        self.set_text(old, c.get('declaration') or '', c.get('body') or '')
                    else:
                        self.add_method(n, c['name'], c.get('declaration') or f"METHOD {c['name']}\n", c.get('body') or '')
                elif c.get('part') == 'action':
                    if old:
                        self.set_text(old, None, c.get('body') or '')
                    else:
                        self.add_action(n, c['name'], c.get('body') or '')
                else:
                    rep['skipped'].append(f"{x['name']}.{c['name']}")
        if not clean:
            rep['left'] = sorted(n.name for k, n in by_name.items() if k not in wanted)
        # tasks
        programs = {n.name.lower() for n in self.nodes.values() if n.kind == 'pou'}
        named = {str(t['name']).lower() for t in source.tasks}
        if source.tasks and any(n.kind == 'task_configuration' for n in self.nodes.values()):
            if clean:
                for n in [n for n in self.nodes.values() if n.kind == 'task' and n.name.lower() not in named]:
                    rep['removed'] += self.remove(n)
            for t in source.tasks:
                missing = [x for x in t.get('pous') or [] if x.lower() not in programs]
                if missing:
                    rep['warnings'].append(f"задача {t['name']}: в проекте нет программ {', '.join(missing)}")
                self.set_task(t['name'], t.get('interval') or None, t.get('priority') or None, t.get('pous') or [])
                rep['tasks'].append(t['name'])
        elif source.tasks:
            rep['warnings'].append('в проекте нет «Конфигурации задач» (нет контроллера): задачи не записаны')
        # OPC UA
        link = getattr(source, 'link', None) or {}
        if link.get('kind') == 'opcua':
            if self.opcua() is None:
                rep['warnings'].append('связь OPC UA: в проекте нет объекта «OPC UA Symbol Configuration» — добавьте его '
                                       'в шаблон в Machine Expert (переменные списков помечены и без него)')
            else:
                rep['opcua'] = self.set_opcua(getattr(source, 'opcua_variables', lambda: [])())
        return rep
