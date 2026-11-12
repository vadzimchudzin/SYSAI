"""A PLC project (Schneider Machine Expert / CODESYS V3) as data.

Read from the project file itself and from what the engineer can save:
- `.project` -- the file of Machine Expert (module plcproj);
- `.export` -- Machine Expert «Проект → Экспорт…» (the native export, an XML of archived objects);
- PLCopen XML (`.xml`) -- «Проект → Экспорт PLCopenXML…».

The model: POUs (programs, function blocks, functions) with their declaration and ST body, global variable lists,
data types, tasks (interval, the POUs they call), devices. On top of it: the table of variables with addresses and
where each `%M` address is in the Modbus table, address conflicts, a structural check of the ST, the «style» of the
project (how it is built: classes of mechanisms, their instances, names, ranges of addresses).
"""
import collections
import os
import re
import zlib
import xml.etree.ElementTree as ET

import plcst

# Type GUIDs of the objects of a CODESYS V3 project (the same in every Machine Expert export).
T_ENTRY = '{6198ad31-4b98-445c-927f-3258a0e82fe3}'
T_POU = '6f9dac99-8de1-4efc-8465-68ac443b7d08'
T_GVL = 'ffbfa93a-b94d-45fc-a329-229860183b1d'
T_DUT = '2db5746d-d284-4425-9f7f-2663a34b0ebc'
T_TASK = '98a2708a-9b18-4f31-82ed-a1465b24fa2d'
T_DEVICE = '225bfe47-7336-4dbc-9419-4105a7c831fa'
T_APPLICATION = '639b491f-5557-464c-af91-1471bac9f549'
T_FOLDER = '738bea1e-99bb-4f04-90bb-a7a567e74e3a'
T_METHOD = 'f8a58466-d7f6-439f-bbb8-d4600e41d099'
T_ACTION = '8ac092e5-3128-4e26-9e7e-11016c6684f2'
T_PROPERTY = '5a3b8626-d3e9-4f37-98b5-66420063d91e'
# The implementation of a POU: its editor. Text is ST (or IL); the others are drawn, their code is not text.
IMPL = {'{3b83b776-fb25-43b8-99f2-3c507c9143fc}': 'st'}
PLCOPEN_NS = 'http://www.plcopen.org/xml/tc6_0200'
XHTML_NS = 'http://www.w3.org/1999/xhtml'


class PlcError(ValueError):
    pass


def _child(e, name):
    for c in e:
        if c.get('Name') == name:
            return c
    return None


def _text(e, name, default=''):
    c = _child(e, name) if e is not None else None
    return (c.text or default) if c is not None else default


def _blob(e):
    """The text of a declaration / implementation part: TextBlobForSerialisation, else its lines."""
    if e is None:
        return None
    for x in e.iter('Single'):
        if x.get('Name') == 'TextBlobForSerialisation':
            return (x.text or '').replace('\r\n', '\n')
    lines = [x.text or '' for x in e.iter('Single') if x.get('Name') == 'Text' and x.get('Type') == 'string']
    return None if not lines else '\n'.join(lines)


class Project:
    def __init__(self, name='', source=''):
        self.name, self.source = name, source
        self.pous, self.gvls, self.types, self.tasks, self.devices = [], [], [], [], []
        self.path, self._vars, self.word_bits = '', None, False
        self.link = {}      # how the panel talks to the controller: {'kind': opcua | modbus_tcp | modbus_rtu, 'lists'}
        self.io_variables = set()   # names the I/O mapping of the devices declares itself (read from a .project)

    # ------------------------------------------------------------------------------------------------ reading
    @classmethod
    def load(cls, path: str) -> 'Project':
        if str(path).lower().endswith(('.project', '.projectarchive')):
            import plcproj
            try:
                return plcproj.load(str(path))
            except plcproj.ProjectFileError as exc:
                raise PlcError(str(exc)) from None
        try:
            root = ET.parse(path).getroot()
        except ET.ParseError as exc:
            raise PlcError(f'не XML: {exc}. Нужен .export или PLCopen XML из Machine Expert') from None
        except OSError as exc:
            raise PlcError(f'файл не читается: {exc}') from None
        if root.tag == 'ExportFile':
            p = cls._from_export(root)
        elif root.tag in (f'{{{PLCOPEN_NS}}}project', 'project'):
            p = cls._from_plcopen(root)
        else:
            raise PlcError(f'не экспорт Machine Expert и не PLCopen XML (корень <{root.tag}>)')
        p.path = str(path)
        if not p.name:
            p.name = os.path.splitext(os.path.basename(str(path)))[0]
        return p

    @classmethod
    def _from_export(cls, root) -> 'Project':
        p = cls(source='machine-expert-export')
        names, parents, raw = {}, {}, []
        for e in root.iter('Single'):
            if e.get('Type') != T_ENTRY:
                continue
            mo = _child(e, 'MetaObject')
            if mo is None:
                continue
            guid, name = _text(mo, 'Guid'), _text(mo, 'Name')
            names[guid], parents[guid] = name, _text(mo, 'ParentGuid')
            path_e = _child(e, 'Path')
            raw.append({'guid': guid, 'name': name, 'type': _text(mo, 'TypeGuid'), 'parent': _text(mo, 'ParentGuid'),
                        'obj': _child(e, 'Object'), 'path': [x.text or '' for x in path_e] if path_e is not None else []})
        if not raw:
            raise PlcError('в экспорте нет объектов')
        kinds = {r['guid']: r['type'] for r in raw}
        for r in raw:
            obj, folder = r['obj'], '/'.join(r['path'])
            if r['type'] == T_POU and obj is not None:
                impl = _child(obj, 'Implementation')
                language = IMPL.get(impl.get('Type') if impl is not None else '', 'graphic')
                decl = _blob(_child(obj, 'Interface')) or ''
                body = _blob(impl) if language == 'st' else None
                pou = {'name': r['name'], 'declaration': decl, 'body': body or '', 'language': language, 'folder': folder,
                       'guid': r['guid'], 'children': []}
                if language != 'st' and impl is not None:
                    # a drawn POU (LD / FBD / CFC): its code is not text; the names it uses are in the elements
                    seen = collections.OrderedDict()
                    for x in impl.iter('Single'):
                        if x.get('Name') == 'Text' and x.get('Type') == 'string' and (x.text or '').strip():
                            seen.setdefault(x.text.strip())
                    pou['uses'] = list(seen)
                pou.update({k: v for k, v in plcst.parse_declaration(decl).items() if k in ('kind', 'returns', 'extends')})
                pou['kind'] = pou['kind'] or 'program'
                p.pous.append(pou)
            elif r['type'] == T_GVL and obj is not None:
                p.gvls.append({'name': r['name'], 'declaration': _blob(_child(obj, 'Interface')) or '', 'folder': folder})
            elif r['type'] == T_DUT and obj is not None:
                p.types.append({'name': r['name'], 'declaration': _blob(_child(obj, 'Interface')) or '', 'folder': folder})
            elif r['type'] == T_TASK and obj is not None:
                iv = _child(obj, 'Interval')
                pl = _child(obj, 'PouList')
                p.tasks.append({'name': r['name'], 'kind': _text(obj, 'Kindoftask'), 'priority': _text(obj, 'Priority'),
                                'interval': (_text(iv, 'Time') + _text(iv, 'Unit')) if iv is not None else '',
                                'pous': [_text(x, 'Name') for x in pl] if pl is not None else []})
            elif r['type'] == T_DEVICE:
                p.devices.append({'name': r['name'], 'parent': names.get(r['parent'], '')})
            elif r['type'] == T_APPLICATION:
                p.application = r['name']
            elif r['type'] in (T_METHOD, T_ACTION, T_PROPERTY) and obj is not None and kinds.get(r['parent']) == T_POU:
                owner = next((x for x in p.pous if x['guid'] == r['parent']), None)
                child = {'name': r['name'], 'part': {T_METHOD: 'method', T_ACTION: 'action', T_PROPERTY: 'property'}[r['type']],
                         'declaration': _blob(_child(obj, 'Interface')) or '', 'body': _blob(_child(obj, 'Implementation')) or ''}
                if owner is not None:
                    owner['children'].append(child)
        parents_of = {d['parent'] for d in p.devices}
        top = [d['name'] for d in p.devices if not d['parent'] and d['name'] in parents_of] \
            or [d['name'] for d in p.devices if not d['parent'] and d['name'] != 'FdtConnections']
        p.controller = top[0] if top else ''
        for pou in p.pous:
            pou.pop('guid', None)
        return p

    @classmethod
    def _from_plcopen(cls, root) -> 'Project':
        ns = {'p': PLCOPEN_NS}

        def plain(e):
            # the text is in an <xhtml> child; around it there is only the indentation of the file
            if e is None:
                return ''
            x = next((c for c in e.iter() if c.tag == f'{{{XHTML_NS}}}xhtml'), None)
            return ''.join(x.itertext()) if x is not None else ''.join(e.itertext()).strip()
        p = cls(source='plcopen')
        ch = root.find('p:contentHeader', ns)
        p.name = ch.get('name', '') if ch is not None else ''

        def as_text(node):
            for data in node.iter(f'{{{PLCOPEN_NS}}}data'):
                if data.get('name', '').endswith('interfaceasplaintext'):
                    return plain(data).strip('\n')
            return None

        def var_lines(owner):
            out = []
            for v in owner.findall('p:variable', ns):
                t = v.find('p:type', ns)
                typ = _plcopen_type(t[0]) if t is not None and len(t) else 'BOOL'
                addr = f" AT {v.get('address')}" if v.get('address') else ''
                init = v.find('p:initialValue/p:simpleValue', ns)
                doc = plain(v.find('p:documentation', ns)).strip()
                out.append(f"\t{v.get('name')}{addr} : {typ}{' := ' + init.get('value') if init is not None else ''};"
                           + (f' // {doc}' if doc else ''))
            return out
        sections = (('inputVars', 'VAR_INPUT'), ('outputVars', 'VAR_OUTPUT'), ('inOutVars', 'VAR_IN_OUT'),
                    ('localVars', 'VAR'), ('tempVars', 'VAR_TEMP'), ('externalVars', 'VAR_EXTERNAL'))
        for pou in root.iterfind('.//p:pous/p:pou', ns):
            kind = {'program': 'program', 'functionBlock': 'function_block', 'function': 'function'}.get(pou.get('pouType'), 'program')
            iface = pou.find('p:interface', ns)
            decl = as_text(iface) if iface is not None else None
            if decl is None:
                head = {'program': 'PROGRAM', 'function_block': 'FUNCTION_BLOCK', 'function': 'FUNCTION'}[kind] + ' ' + pou.get('name')
                rt = iface.find('p:returnType', ns) if iface is not None else None
                if kind == 'function' and rt is not None and len(rt):
                    head += ' : ' + _plcopen_type(rt[0])
                lines = [head]
                for tag, word in sections:
                    for sec in (iface.findall(f'p:{tag}', ns) if iface is not None else ()):
                        q = ''.join(f' {w}' for a, w in (('retain', 'RETAIN'), ('persistent', 'PERSISTENT'), ('constant', 'CONSTANT'))
                                    if sec.get(a) == 'true')
                        lines += [word + q] + var_lines(sec) + ['END_VAR']
                decl = '\n'.join(lines)
            body_e = pou.find('p:body', ns)
            st = body_e.find('p:ST', ns) if body_e is not None else None
            language = 'st' if st is not None else 'graphic'
            d = {'name': pou.get('name'), 'declaration': decl, 'body': plain(st).strip('\n') if st is not None else '',
                 'language': language, 'folder': '', 'children': [], 'kind': kind}
            d.update({k: v for k, v in plcst.parse_declaration(decl).items() if k in ('returns', 'extends')})
            p.pous.append(d)
        for gv in root.iter(f'{{{PLCOPEN_NS}}}globalVars'):
            decl = as_text(gv)
            if decl is None:
                q = ''.join(f' {w}' for a, w in (('retain', 'RETAIN'), ('persistent', 'PERSISTENT'), ('constant', 'CONSTANT'))
                            if gv.get(a) == 'true')
                decl = '\n'.join(['VAR_GLOBAL' + q] + var_lines(gv) + ['END_VAR'])
            p.gvls.append({'name': gv.get('name', 'GVL'), 'declaration': decl, 'folder': ''})
        for dt in root.iterfind('.//p:dataTypes/p:dataType', ns):
            decl = as_text(dt)
            if decl is None:
                st = dt.find('p:baseType/p:struct', ns)
                if st is not None:
                    decl = '\n'.join([f"TYPE {dt.get('name')} :", 'STRUCT'] + var_lines(st) + ['END_STRUCT', 'END_TYPE'])
                else:
                    bt = dt.find('p:baseType', ns)
                    decl = f"TYPE {dt.get('name')} : {_plcopen_type(bt[0]) if bt is not None and len(bt) else 'INT'};\nEND_TYPE"
            p.types.append({'name': dt.get('name'), 'declaration': decl, 'folder': ''})
        for task in root.iter(f'{{{PLCOPEN_NS}}}task'):
            p.tasks.append({'name': task.get('name', ''), 'kind': 'Cyclic' if task.get('interval') else '',
                            'priority': task.get('priority', ''), 'interval': _interval(task.get('interval', '')),
                            'pous': [x.get('typeName') or x.get('name') for x in task.findall('p:pouInstance', ns)]})
        return p

    # ------------------------------------------------------------------------------------------------ the model
    def pou(self, name: str) -> dict:
        for x in self.pous:
            if x['name'].lower() == str(name).lower():
                return x
        raise PlcError(f'нет POU «{name}»; есть: {", ".join(x["name"] for x in self.pous)}')

    def variables(self) -> list:
        """Every declared variable: [{'scope' (the GVL or POU), 'scope_kind', 'name', 'type', 'address', 'init',
        'comment', 'group', 'section', 'qualifiers', 'modbus'}]."""
        if self._vars is None:
            rows = []
            for g in self.gvls:
                rows += [dict(v, scope=g['name'], scope_kind='gvl') for v in plcst.parse_declaration(g['declaration'])['vars']]
            for x in self.pous:
                rows += [dict(v, scope=x['name'], scope_kind=x['kind']) for v in plcst.parse_declaration(x['declaration'])['vars']]
            # bits numbered by word (%MX0.8 ... %MX0.15) anywhere: the whole controller counts them so
            self.word_bits = any(plcst.bit_over_7(v['address']) for v in rows if v['address'])
            for v in rows:
                v['modbus'] = plcst.modbus(v['address'], self.word_bits)
            self._vars = rows
        return self._vars

    def info(self) -> dict:
        lang = collections.Counter(x['language'] for x in self.pous)
        vs = self.variables()
        return {'name': self.name, 'source': self.source, 'controller': getattr(self, 'controller', ''),
                'application': getattr(self, 'application', ''),
                'pous': len(self.pous), 'st': lang.get('st', 0), 'graphic': lang.get('graphic', 0),
                'programs': sum(1 for x in self.pous if x['kind'] == 'program'),
                'function_blocks': sum(1 for x in self.pous if x['kind'] == 'function_block'),
                'functions': sum(1 for x in self.pous if x['kind'] == 'function'),
                'gvls': [g['name'] for g in self.gvls], 'types': [t['name'] for t in self.types],
                'tasks': self.tasks, 'variables': len(vs), 'addressed': sum(1 for v in vs if v['address']),
                'modbus': sum(1 for v in vs if v['modbus']), 'bit_numbering': 'word' if self.word_bits else 'byte',
                'devices': [d['name'] for d in self.devices][:60], 'link': self.link or None}

    # ------------------------------------------------------------------------------------------------ addresses
    def addresses(self) -> dict:
        """The memory map: {'variables': addressed variables sorted by place, 'conflicts': [...], 'ranges': per
        area the used span, 'free': the first free register and bit of %M}. A conflict is two variables on the
        same bits: the same address twice, or a word (%MW) over bits (%MX) of the same register."""
        used, rows = [], []
        for v in self.variables():      # (sets self.word_bits)
            a = plcst.parse_address(v['address'], self.word_bits) if v['address'] else None
            if not a:
                continue
            width = a['bits']
            if a['size'] != 'X':
                width = max(width, plcst.BITS.get(plcst.base_type(v['type']).upper(), width)) if a['size'] != 'B' else width
            row = dict(v, area=a['area'], bit_start=a['bit_start'], bits=width)
            rows.append(row)
            used.append((a['area'], a['bit_start'], a['bit_start'] + width, row))
        used.sort(key=lambda u: (u[0], u[1], u[2]))
        conflicts, seen = [], set()
        for i, (area, s, e, row) in enumerate(used):
            for area2, s2, e2, row2 in used[i + 1:]:
                if area2 != area or s2 >= e:
                    break
                key = (row['scope'], row['name'], row2['scope'], row2['name'])
                if key in seen:
                    continue
                seen.add(key)
                same = row['address'] == row2['address']
                conflicts.append({'level': 'warning' if same else 'error',
                                  'a': f"{row['scope']}.{row['name']}", 'a_address': row['address'],
                                  'b': f"{row2['scope']}.{row2['name']}", 'b_address': row2['address'],
                                  'text': ('один адрес ' + row['address'] if same else
                                           f"{row['address']} и {row2['address']} занимают одну память")
                                  + f": {row['name']} и {row2['name']}"})
        ranges = {}
        for area, s, e, row in used:
            r = ranges.setdefault(area, {'first': row['address'], 'last': row['address'], 'count': 0, 'bits_end': 0})
            r['count'] += 1
            if e >= r['bits_end']:
                r['bits_end'], r['last'] = e, row['address']
        free = None
        if 'M' in ranges:
            reg = (ranges['M']['bits_end'] + 15) // 16
            free = {'register': reg, 'word': f'%MW{reg}', 'bit': f'%MX{reg if self.word_bits else reg * 2}.0'}
        for r in ranges.values():
            r.pop('bits_end')
        rows.sort(key=lambda r: (r['area'], r['bit_start']))
        return {'variables': rows, 'conflicts': conflicts, 'ranges': ranges, 'free': free,
                'bit_numbering': 'word' if self.word_bits else 'byte'}

    def opcua_lists(self) -> list:
        """The global lists the panel sees over OPC UA: those named in project.json, else every list."""
        want = [str(n).lower() for n in (self.link.get('lists') or [])]
        return [g for g in self.gvls if not want or g['name'].lower() in want]

    def opcua_variables(self) -> list:
        """The names (`List.Variable`) the controller is to publish over OPC UA: what a panel can bind to."""
        return [t['name'] for t in self.hmi_tags('opcua')]

    def numeric_nodes(self) -> bool:
        """The OPC UA server of a Schneider M241 / M251 names variables by numbers (ns=2;i=N), not by strings."""
        return bool(re.search(r'T?M2[45]1', str(getattr(self, 'controller', '') or ''), re.I))

    def tag_check(self, tags: list) -> dict:
        """Panel tags against the program: [{name, type?, node?}] or names. Returns {'ok', 'missing' (no such
        variable), 'type' (another type in the program), 'node' (the number of the node is not the one the
        controller gives this name), 'unused' (variables of the program no tag points to — count only)}."""
        have = {t['name'].lower(): t for t in self.hmi_tags('opcua')}
        every = {f"{g['name']}.{v['name']}".lower(): v for g in self.gvls
                 for v in plcst.parse_declaration(g['declaration'])['vars']}
        out = {'ok': 0, 'missing': [], 'type': [], 'node': [], 'not_plain': []}
        seen = set()
        for t in tags:
            t = {'name': t} if isinstance(t, str) else t
            name = str(t.get('name') or '').strip()
            key = name.lower()
            if not name or key in seen:
                continue
            seen.add(key)
            if key not in have:
                # a variable the I/O mapping declares lives in the implicit list IoConfig_Globals_Mapping
                if key.startswith('ioconfig_globals_mapping.') and key.split('.', 1)[1] in self.io_variables:
                    out['ok'] += 1
                    continue
                if key in every:
                    out['not_plain'].append({'name': name, 'type': every[key]['type']})
                else:
                    out['missing'].append(name)
                continue
            v, bad = have[key], False
            if t.get('type') and panel_type(str(t['type'])) != panel_type(v['type']):
                out['type'].append({'name': name, 'panel': str(t['type']).upper(), 'plc': v['type']})
                bad = True
            m = re.match(r'ns[:=]\d+;i[:=](\d+)$', str(t.get('node') or ''))
            if m and int(m.group(1)) != m241_node_id(v['name']):
                out['node'].append({'name': name, 'panel': int(m.group(1)), 'plc': m241_node_id(v['name'])})
                bad = True
            out['ok'] += not bad
        out['unused'] = len(set(have) - seen)
        return out

    def hmi_tags(self, link: str = '') -> list:
        """The variables a panel can reach, ready for the HMI.

        Modbus (the default): those at `%M` -- [{'name', 'type', 'comment', 'group', 'address', 'register', 'bit',
        'weintek': {'type': '4x' | '4x_Bit', 'address'}, 'kind': bit | word | dword | real}]. Weintek counts
        registers from 1; a bit is written as register and two digits of the bit.

        OPC UA (`link` or project.json says so): the variables of the published lists that have a plain type --
        [{'name': 'GVL.Start', 'type', 'comment', 'group', 'node': 's:GVL.Start', 'kind'}]; instances of function
        blocks and structures are left out (a panel binds to plain values)."""
        if (link or self.link.get('kind') or '') == 'opcua':
            out = []
            for g in self.opcua_lists():
                for v in plcst.parse_declaration(g['declaration'])['vars']:
                    base = plcst.base_type(v['type']).upper()
                    if base not in plcst.ELEMENTARY or v['type'].strip().upper().startswith('ARRAY'):
                        continue
                    kind = ('bit' if base == 'BOOL' else 'real' if base in ('REAL', 'LREAL') else 'string' if 'STRING' in base
                            else 'dword' if plcst.BITS.get(base, 16) >= 32 else 'word')
                    name = f"{g['name']}.{v['name']}"
                    out.append({'name': name, 'scope': g['name'], 'type': base, 'panel_type': panel_type(base),
                                'comment': v['comment'] or '', 'group': v['group'] or '', 'node': f's:{name}',
                                'node_m241': f'ns:2;i:{m241_node_id(name)}', 'kind': kind})
            return out
        out = []
        for v in self.addresses()['variables']:
            m = v['modbus']
            if not m:
                continue
            base = plcst.base_type(v['type']).upper()
            kind = 'bit' if 'bit' in m else 'real' if base in ('REAL', 'LREAL') else 'dword' if plcst.BITS.get(base, 16) >= 32 else 'word'
            w = ({'type': '4x_Bit', 'address': f"{m['one_based']}{m['bit']:02d}"} if kind == 'bit'
                 else {'type': '4x', 'address': str(m['one_based'])})
            out.append({'name': v['name'], 'scope': v['scope'], 'type': v['type'], 'comment': v['comment'] or '',
                        'group': v['group'] or '', 'address': v['address'], 'register': m['register'], 'bit': m.get('bit'),
                        'kind': kind, 'weintek': w})
        return out

    # ------------------------------------------------------------------------------------------------ checks
    def check(self) -> list:
        """What the text tells before Machine Expert builds it: [{'level', 'where', 'line', 'text'}] -- broken
        blocks and brackets in ST, names used but declared nowhere, two declarations of a name, unknown types,
        address conflicts, tasks calling POUs that are not there, drawn POUs (not checked)."""
        issues = []

        def add(level, where, text, line=None):
            issues.append({'level': level, 'where': where, 'line': line, 'text': text})
        self.variables()
        globals_ = collections.OrderedDict()
        for g in self.gvls:
            seen = {}
            for v in plcst.parse_declaration(g['declaration'])['vars']:
                low = v['name'].lower()
                if low in seen:
                    add('error', g['name'], f"переменная {v['name']} объявлена дважды (строки {seen[low]} и {v['line']})", v['line'])
                seen[low] = v['line']
                if low in globals_ and globals_[low] != g['name']:
                    add('warning', g['name'], f"{v['name']} есть и в {globals_[low]}", v['line'])
                globals_.setdefault(low, g['name'])
        known_types = {t['name'].lower() for t in self.types} | {x['name'].lower() for x in self.pous} \
            | {t.lower() for t in plcst.ELEMENTARY} | {s.lower() for s in plcst.STANDARD}
        pou_names = {x['name'].lower() for x in self.pous}
        gvl_names = {g['name'].lower() for g in self.gvls}
        for scope, decl in [(g['name'], g['declaration']) for g in self.gvls] + [(x['name'], x['declaration']) for x in self.pous] \
                + [(t['name'], t['declaration']) for t in self.types]:
            for line, what in plcst.unclosed(decl):
                add('error', scope, what, line)
            for v in plcst.parse_declaration(decl)['vars']:
                if v['name'].upper() in plcst.RESERVED or v['name'].upper() in plcst.ELEMENTARY:
                    add('error', scope, f"имя {v['name']} занято языком: переменную так назвать нельзя", v['line'])
                bt = plcst.base_type(v['type'])
                if '.' not in bt and bt.lower() not in known_types and not re.match(r'^\(', v['type'].strip()):
                    add('warning', scope, f"тип {bt} переменной {v['name']} не найден в проекте (библиотека?)", v['line'])
                if v['address'] and not plcst.parse_address(v['address'], self.word_bits) and '*' not in v['address']:
                    add('error', scope, f"адрес {v['address']} переменной {v['name']} записан неверно", v['line'])
        for x in self.pous:
            if x['language'] != 'st':
                add('info', x['name'], 'графический язык (LD / FBD / CFC): код не текст, не проверяется')
                continue
            for i in plcst.check_body(x['body']):
                add(i['level'], x['name'], i['text'], i['line'])
            d = plcst.parse_declaration(x['declaration'])
            local, seen = {}, {}
            for v in d['vars']:
                low = v['name'].lower()
                if low in seen:
                    add('error', x['name'], f"переменная {v['name']} объявлена дважды", v['line'])
                seen[low] = v['line']
                local[low] = v
            unknown = collections.OrderedDict()
            for name, line, after_dot in plcst.identifiers(x['body']):
                low = name.lower()
                if after_dot or low in local or low in globals_ or low in pou_names or low in gvl_names \
                        or low in known_types or name.upper() in plcst.KEYWORDS or name.upper() in plcst.STANDARD \
                        or name.upper() in plcst.SYSTEM_LISTS \
                        or low == x['name'].lower():
                    continue
                fits = plcst.conversion(name)
                if fits:
                    continue                      # INT_TO_REAL, TO_INT: the standard library has it
                if fits is False:
                    add('error', x['name'], f'преобразования {name} нет: типы пишут как в IEC (INT_TO_REAL, DINT_TO_TIME, '
                                            'REAL_TO_INT), а не FLOAT / INTEGER / DOUBLE', line)
                    continue
                unknown.setdefault(name, line)
            for name, line in unknown.items():
                add('warning', x['name'], f'имя {name} нигде не объявлено (ни в POU, ни в GVL)', line)
        for c in self.addresses()['conflicts']:
            add(c['level'], 'адреса', c['text'])
        kinds = {x['name'].lower(): x['kind'] for x in self.pous}
        task_names = set()
        for t in self.tasks:
            where = f"задача {t['name']}"
            if t['name'].lower() in task_names:
                add('error', where, 'задача с таким именем уже есть')
            task_names.add(t['name'].lower())
            if not re.fullmatch(r'[A-Za-z_]\w*', t['name']):
                add('error', where, 'имя задачи — латиница, цифры и _, не с цифры')
            for name in t['pous']:
                low = name.split('.')[0].lower() if name else ''
                if name and low not in pou_names:
                    add('error', where, f'вызывает {name}, такой программы нет')
                elif name and kinds.get(low) != 'program':
                    add('error', where, f'вызывает {name}, а это не программа (PROGRAM): блок или функцию задача вызвать '
                                        'не может — вызовите его из программы')
            if self.source == 'st-folder':  # written by hand in project.json: what Machine Expert takes
                prio = str(t.get('priority', '')).strip()
                if not prio.isdigit() or not 0 <= int(prio) <= 31:
                    add('error', where, f'приоритет «{prio}»: нужно целое от 0 до 31')
                if str(t.get('kind') or 'Cyclic').lower() == 'cyclic' and not plcst.interval_ms(t.get('interval')):
                    add('error', where, f"период «{t.get('interval')}»: нужно как 20ms, 1s или T#100ms")
        if self.pous and not self.tasks and self.source != 'plcopen':
            add('warning', 'проект', 'нет ни одной задачи: программы никто не вызывает')
        kind = self.link.get('kind') or ''
        if kind == 'opcua':
            # what a project publishes: lists of variables, and also the variables of a program and the mapping of the
            # channels (Machine Expert's symbol configuration names all three)
            have = {g['name'].lower() for g in self.gvls} | {x['name'].lower() for x in self.pous if x['kind'] == 'program'} \
                | {n.lower() for n in plcst.SYSTEM_LISTS}
            for n in self.link.get('lists') or []:
                if str(n).lower() not in have:
                    add('error', 'связь', f'project.json: {n} для OPC UA — нет ни такого списка переменных, ни программы')
            for g in self.opcua_lists():
                if not re.search(r"\{\s*attribute\s+'symbol'", g['declaration'], re.I):
                    add('warning', g['name'], "связь OPC UA: перед VAR_GLOBAL нет строки {attribute 'symbol' := 'readwrite'} — "
                                              'панель не увидит переменные этого списка')
            if not self.hmi_tags():
                add('warning', 'связь', 'связь OPC UA: в опубликованных списках нет ни одной переменной простого типа')
        elif kind in ('modbus_tcp', 'modbus_rtu') and self.gvls and not self.hmi_tags():
            add('warning', 'связь', 'связь Modbus: ни у одной переменной нет адреса %M — панели нечего читать')
        elif kind and kind not in ('modbus_tcp', 'modbus_rtu'):
            add('error', 'связь', f'project.json: link.kind «{kind}» — нужно opcua, modbus_tcp или modbus_rtu')
        return issues

    # ------------------------------------------------------------------------------------------------ the style
    def style(self) -> dict:
        """How the project is built -- what an engineer would copy into the next one:
        classes (function blocks of mechanisms: inputs, outputs, timers), their instances and where they are called,
        the programs and their roles, the naming of variables, the groups of the global list, the address ranges."""
        gl = [v for v in self.variables() if v['scope_kind'] == 'gvl']
        fb_names = {x['name'].lower(): x['name'] for x in self.pous if x['kind'] == 'function_block'}
        instances = collections.defaultdict(list)
        for v in self.variables():
            bt = plcst.base_type(v['type']).lower()
            if bt in fb_names:
                instances[fb_names[bt]].append({'name': v['name'], 'scope': v['scope']})
        classes = []
        for x in self.pous:
            if x['kind'] != 'function_block':
                continue
            d = plcst.parse_declaration(x['declaration'])
            sec = collections.Counter(v['section'] for v in d['vars'])
            timers = [v['name'] for v in d['vars'] if plcst.base_type(v['type']).upper() in ('TON', 'TOF', 'TP')]
            classes.append({'name': x['name'], 'language': x['language'], 'inputs': [v['name'] for v in d['vars'] if v['section'] == 'VAR_INPUT'],
                            'outputs': [v['name'] for v in d['vars'] if v['section'] == 'VAR_OUTPUT'],
                            'retain': [v['name'] for v in d['vars'] if 'RETAIN' in v['qualifiers'] or 'PERSISTENT' in v['qualifiers']],
                            'locals': sec.get('VAR', 0), 'timers': timers, 'lines': len(x['body'].splitlines()),
                            'instances': [i['name'] for i in instances.get(x['name'], [])]})
        programs = []
        called = {name.split('.')[0].lower() for t in self.tasks for name in t['pous']}
        for x in self.pous:
            if x['kind'] != 'program':
                continue
            names = {n.lower() for n, _, dot in plcst.identifiers(x['body']) if not dot} | {u.lower().split('.')[0] for u in x.get('uses', [])}
            inst_called = sorted({i['name'] for lst in instances.values() for i in lst if i['name'].lower() in names})
            programs.append({'name': x['name'], 'language': x['language'], 'lines': len(x['body'].splitlines()),
                             'in_task': x['name'].lower() in called, 'calls_instances': len(inst_called),
                             'role': _role(x, len(inst_called))})
        groups = collections.OrderedDict()
        for v in gl:
            g = groups.setdefault(v['group'] or '(без заголовка)', {'variables': 0, 'addressed': 0})
            g['variables'] += 1
            g['addressed'] += 1 if v['address'] else 0
        names = [v['name'] for v in gl]
        prefixes = collections.Counter()
        for i in (x['name'] for lst in instances.values() for x in lst):
            n = sum(1 for nm in names if nm.lower().startswith(i.lower() + '_'))
            if n:
                prefixes[i] = n
        cyr = sum(1 for v in gl if v['comment'] and re.search('[А-Яа-яЁё]', v['comment']))
        commented = sum(1 for v in gl if v['comment'])
        return {
            'project': self.name, 'controller': getattr(self, 'controller', ''),
            'tasks': self.tasks,
            'programs': programs, 'classes': classes,
            'data_types': [t['name'] for t in self.types],
            'global_lists': [{'name': g['name'], 'variables': len(plcst.parse_declaration(g['declaration'])['vars'])} for g in self.gvls],
            'groups': [dict(name=k, **v) for k, v in groups.items()],
            'naming': {
                'case': _case(names), 'class_suffix': _suffix([c['name'] for c in classes]),
                'instance_prefix_variables': dict(prefixes.most_common(40)),
                'io_names': sorted({re.sub(r'\d+', 'N', v['name']) for v in gl if re.fullmatch(r'[xyXY]\d+_\d+', v['name'])}),
                'comments': {'share': round(commented / len(gl), 2) if gl else 0, 'russian': cyr >= max(1, commented // 2)},
            },
            'addresses': {k: v for k, v in self.addresses().items() if k in ('ranges', 'free', 'bit_numbering')},
            'address_conflicts': len(self.addresses()['conflicts']),
        }


_PANEL_TYPES = {'BYTE': 'USINT', 'WORD': 'UINT', 'DWORD': 'UDINT', 'LWORD': 'ULINT'}


def panel_type(plc_type: str) -> str:
    """The type a panel shows for a variable over OPC UA: bit strings come as unsigned numbers (WORD — UINT)."""
    t = str(plc_type or '').strip().upper()
    return _PANEL_TYPES.get(t, t)


def m241_node_id(name: str) -> int:
    """The number the OPC UA server of a Schneider M241 / M251 gives a variable (ns=2;i=N): CRC-32 of
    «List.Variable» without the final inversion. Checked on the 582 tags of a real panel project."""
    return zlib.crc32(name.encode('ascii', 'replace')) ^ 0xFFFFFFFF


def _role(pou: dict, inst_called: int) -> str:
    body = pou['body']
    name = pou['name'].lower()
    if pou['language'] != 'st':
        return 'графическая программа: вызовы блоков' if pou.get('uses') else 'графическая программа'
    assigns = len(re.findall(r':=', plcst.blank_comments(body)))
    io = len(re.findall(r'\b[xyXY]\d+_\d+\b', body))
    if io and io * 2 >= assigns:
        return 'привязка входов и выходов к именам'
    if inst_called:
        return 'вызовы блоков механизмов'
    if 'alarm' in name or 'авар' in name:
        return 'аварии'
    return 'логика: условия пуска и блокировки'


def _case(names: list) -> str:
    if not names:
        return ''
    snake = sum(1 for n in names if '_' in n)
    camel = sum(1 for n in names if '_' not in n and re.search(r'[a-z][A-Z]', n))
    return 'snake_case' if snake >= camel else 'CamelCase'


def _suffix(names: list) -> str:
    c = collections.Counter(n.rsplit('_', 1)[-1] for n in names if '_' in n)
    if not c:
        return ''
    word, n = c.most_common(1)[0]
    return '_' + word if n * 2 >= len(names) else ''


def _plcopen_type(e) -> str:
    tag = e.tag.split('}')[-1]
    if tag == 'derived':
        return e.get('name', '')
    if tag == 'array':
        dims = ', '.join(f"{d.get('lower')}..{d.get('upper')}" for d in e if d.tag.endswith('dimension'))
        bt = next((c for c in e if c.tag.endswith('baseType')), None)
        return f"ARRAY [{dims}] OF {_plcopen_type(bt[0]) if bt is not None and len(bt) else 'INT'}"
    if tag in ('string', 'wstring'):
        return tag.upper() + (f"({e.get('length')})" if e.get('length') else '')
    if tag == 'pointer':
        bt = next((c for c in e if c.tag.endswith('baseType')), None)
        return 'POINTER TO ' + (_plcopen_type(bt[0]) if bt is not None and len(bt) else 'BYTE')
    return tag


def _interval(value: str) -> str:
    """`PT0.02S` (PLCopen) -> `20ms`; anything else as it is."""
    m = re.fullmatch(r'PT(\d+(?:\.\d+)?)S', value or '')
    if not m:
        return value or ''
    ms = float(m.group(1)) * 1000
    return f'{ms:g}ms'


def style_markdown(st: dict) -> str:
    """The style as a page an engineer reads (and an agent follows when it writes the next project)."""
    out = [f"# Стиль проекта ПЛК «{st['project']}»", '']
    if st.get('controller'):
        out.append(f"Контроллер в дереве проекта: `{st['controller']}`.")
    for t in st['tasks']:
        out.append(f"Задача `{t['name']}`: {t.get('kind') or 'циклическая'}, {t['interval']}, приоритет {t['priority']}; "
                   f"вызывает по порядку: {', '.join('`' + x + '`' for x in t['pous']) or '—'}.")
    out += ['', '## Программы', '', '| Программа | Язык | Строк | В задаче | Роль |', '|---|---|---|---|---|']
    for x in st['programs']:
        out.append(f"| `{x['name']}` | {'ST' if x['language'] == 'st' else 'графический'} | {x['lines']} | "
                   f"{'да' if x['in_task'] else 'нет'} | {x['role']} |")
    out += ['', '## Классы механизмов (функциональные блоки)', '',
            '| Блок | Входов | Выходов | Таймеры | Сохраняемые | Строк | Экземпляры |', '|---|---|---|---|---|---|---|']
    for c in st['classes']:
        inst = ', '.join(c['instances'][:8]) + (' …' if len(c['instances']) > 8 else '')
        out.append(f"| `{c['name']}` | {len(c['inputs'])} | {len(c['outputs'])} | {len(c['timers'])} | {len(c['retain'])} | "
                   f"{c['lines']} | {len(c['instances'])}: {inst or '—'} |")
    n = st['naming']
    out += ['', '## Имена', '', f"- Стиль имён: {n['case'] or '—'}; окончание классов: `{n['class_suffix'] or '—'}`.",
            f"- Входы и выходы модулей: {', '.join('`' + x + '`' for x in n['io_names']) or '—'}.",
            f"- Комментарии у {int(n['comments']['share'] * 100)} % глобальных переменных, "
            f"{'на русском' if n['comments']['russian'] else 'не на русском'}."]
    if n['instance_prefix_variables']:
        top = list(n['instance_prefix_variables'].items())[:8]
        out.append('- Переменные механизма идут с его именем впереди: ' + ', '.join(f'`{k}_…` ({v})' for k, v in top) + '.')
    out += ['', '## Глобальные списки и группы', '']
    for g in st['global_lists']:
        out.append(f"- `{g['name']}`: {g['variables']} переменных.")
    out += ['', '| Группа (заголовок-комментарий) | Переменных | С адресом |', '|---|---|---|']
    for g in st['groups'][:80]:
        out.append(f"| {g['name']} | {g['variables']} | {g['addressed']} |")
    a = st['addresses']
    out += ['', '## Адреса', '', f"- Биты считаются по {'словам (%MX слово.бит 0..15)' if a.get('bit_numbering') == 'word' else 'байтам (%MX байт.бит 0..7)'}."]
    for area, r in (a.get('ranges') or {}).items():
        out.append(f"- Область %{area}: {r['count']} переменных, от `{r['first']}` до `{r['last']}`.")
    if a.get('free'):
        out.append(f"- Первое свободное место: `{a['free']['word']}` (бит `{a['free']['bit']}`).")
    if st.get('address_conflicts'):
        out.append(f"- Пересечений адресов: {st['address_conflicts']} (см. plc_addresses).")
    return '\n'.join(out) + '\n'
