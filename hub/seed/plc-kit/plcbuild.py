"""A PLC project as a folder of ST files, and as PLCopen XML for Machine Expert.

The folder is what the agent writes and the engineer reads:

    project.json          {"name", "controller", "tasks": [{"name", "interval", "priority", "pous": [...]}]}
    GVL.st                VAR_GLOBAL ... END_VAR                          (a global variable list; any number of them)
    Noria_class.st        FUNCTION_BLOCK Noria_class VAR_INPUT ... END_VAR <body>
    Main.st               PROGRAM Main VAR ... END_VAR <body>
    Motor.st              TYPE Motor : STRUCT ... END_STRUCT END_TYPE     (a data type)

A file is told by how it starts; in a POU the body is everything after the declaration (the header and the VAR blocks
that follow it). `to_plcopen` writes the PLCopen XML (TC6 2.01) that Machine Expert imports with «Проект → Импорт
PLCopenXML…»: every POU with its ST body, the variables both as elements and as the plain text of the declaration
(so comments and layout survive in CODESYS), the global lists and the tasks.
"""
import datetime
import json
import os
import re
import xml.etree.ElementTree as ET

import plcme
import plcst

NS = plcme.PLCOPEN_NS
XHTML = plcme.XHTML_NS
CDS = 'http://www.3s-software.com/plcopenxml/'
PLCOPEN_ELEMENTARY = {'BOOL', 'BYTE', 'WORD', 'DWORD', 'LWORD', 'SINT', 'INT', 'DINT', 'LINT', 'USINT', 'UINT', 'UDINT',
                      'ULINT', 'REAL', 'LREAL', 'TIME', 'DATE', 'DT', 'TOD'}
SECTION_TAG = {'VAR_INPUT': 'inputVars', 'VAR_OUTPUT': 'outputVars', 'VAR_IN_OUT': 'inOutVars', 'VAR': 'localVars',
               'VAR_TEMP': 'tempVars', 'VAR_EXTERNAL': 'externalVars', 'VAR_STAT': 'localVars', 'VAR_INST': 'localVars'}
_NAME = re.compile(r'[A-Za-z_][A-Za-z0-9_]*')


def split_source(text: str) -> dict:
    """One .st file -> {'what': pou | gvl | type, 'name', 'kind', 'declaration', 'body'}."""
    text = (text or '').replace('\r\n', '\n').lstrip('﻿')
    plain = plcst.blank_comments(text)
    first = re.search(r'\S+', plain)
    word = first.group().upper() if first else ''
    if word == 'TYPE':
        d = plcst.parse_declaration(text)
        return {'what': 'type', 'name': d['name'], 'kind': 'type', 'declaration': text.strip('\n'), 'body': ''}
    if word.startswith('VAR_GLOBAL') or (word.startswith('{') and re.search(r'\bVAR_GLOBAL\b', plain)):
        return {'what': 'gvl', 'name': None, 'kind': 'gvl', 'declaration': text.strip('\n'), 'body': ''}
    if word not in ('PROGRAM', 'FUNCTION_BLOCK', 'FUNCTION'):
        raise plcme.PlcError('файл ST должен начинаться с PROGRAM, FUNCTION_BLOCK, FUNCTION, TYPE или VAR_GLOBAL')
    # the declaration: the header line and the VAR ... END_VAR blocks right after it
    pos = plain.find('\n', first.start())
    pos = len(plain) if pos < 0 else pos
    while True:
        m = re.match(r'\s*VAR(?:_[A-Z_]+)?\b.*?\bEND_VAR\b', plain[pos:], re.S | re.I)
        if not m:
            break
        pos += m.end()
    decl, body = text[:pos].strip('\n'), text[pos:].strip('\n')
    d = plcst.parse_declaration(decl)
    return {'what': 'pou', 'name': d['name'], 'kind': d['kind'], 'declaration': decl, 'body': body}


def load_dir(src_dir: str) -> plcme.Project:
    """The folder of ST files as a project."""
    if not os.path.isdir(src_dir):
        raise plcme.PlcError(f'нет папки {src_dir}')
    meta = {}
    mp = os.path.join(src_dir, 'project.json')
    if os.path.isfile(mp):
        try:
            with open(mp, encoding='utf-8-sig') as f:
                meta = json.load(f)
        except ValueError as exc:
            raise plcme.PlcError(f'project.json: {exc}') from None
        if not isinstance(meta, dict):
            raise plcme.PlcError('project.json: нужен объект {"name", "tasks": [...]}')
    p = plcme.Project(name=str(meta.get('name') or os.path.basename(os.path.normpath(src_dir))), source='st-folder')
    p.controller = str(meta.get('controller') or '')
    files = sorted(f for f in os.listdir(src_dir) if f.lower().endswith('.st'))
    if not files:
        raise plcme.PlcError(f'в папке {src_dir} нет файлов .st')
    seen = {}
    for fn in files:
        with open(os.path.join(src_dir, fn), encoding='utf-8-sig') as f:
            text = f.read()
        try:
            part = split_source(text)
        except plcme.PlcError as exc:
            raise plcme.PlcError(f'{fn}: {exc}') from None
        stem = os.path.splitext(fn)[0]
        name = stem if not part['name'] or part['name'].lower() == stem.lower() else part['name']   # IEC names: no case
        if not _NAME.fullmatch(name):
            raise plcme.PlcError(f'{fn}: имя «{name}» не годится для объекта ПЛК (латиница, цифры, _)')
        if name.lower() in seen:
            raise plcme.PlcError(f'{fn}: объект {name} уже есть в {seen[name.lower()]}')
        seen[name.lower()] = fn
        if part['what'] == 'pou':
            d = plcst.parse_declaration(part['declaration'])
            p.pous.append({'name': name, 'kind': part['kind'], 'declaration': part['declaration'], 'body': part['body'],
                           'language': 'st', 'folder': '', 'children': [], 'returns': d['returns'], 'extends': d['extends']})
        elif part['what'] == 'gvl':
            p.gvls.append({'name': name, 'declaration': part['declaration'], 'folder': ''})
        else:
            p.types.append({'name': name, 'declaration': part['declaration'], 'folder': ''})
    for t in meta.get('tasks') or []:
        if not isinstance(t, dict) or not t.get('name'):
            raise plcme.PlcError('project.json: у задачи нужно имя: {"name": "MAST", "interval": "20ms", "pous": ["Main"]}')
        p.tasks.append({'name': str(t['name']), 'kind': 'Cyclic', 'priority': str(t.get('priority', '15')),
                        'interval': str(t.get('interval') or '20ms'), 'pous': [str(x) for x in t.get('pous') or []]})
    return p


def write_dir(p: plcme.Project, out_dir: str) -> list:
    """The project as a folder of ST files (see the module text); the files written. Drawn POUs (LD / FBD / CFC) get
    their declaration and a note: their code is not text."""
    os.makedirs(out_dir, exist_ok=True)
    written = []

    def put(name, text):
        path = os.path.join(out_dir, name)
        with open(path, 'w', encoding='utf-8', newline='\n') as f:
            f.write(text.rstrip('\n') + '\n')
        written.append(path)
    for g in p.gvls:
        put(f"{g['name']}.st", g['declaration'])
    for t in p.types:
        put(f"{t['name']}.st", t['declaration'])
    for x in p.pous:
        if x['language'] == 'st':
            put(f"{x['name']}.st", x['declaration'].rstrip('\n') + '\n\n' + x['body'])
        else:
            uses = '\n'.join(f'//   {u}' for u in x.get('uses', [])[:400])
            put(f"{x['name']}.st", x['declaration'].rstrip('\n') + '\n\n// Графический язык (LD / FBD / CFC): кода текстом нет.\n'
                '// Имена, которые встречаются в схеме:\n' + uses)
    meta = {'name': p.name, 'controller': getattr(p, 'controller', ''), 'source': p.source,
            'tasks': [{'name': t['name'], 'interval': t['interval'], 'priority': t['priority'], 'pous': t['pous']} for t in p.tasks]}
    put('project.json', json.dumps(meta, ensure_ascii=False, indent=2))
    return written


# ------------------------------------------------------------------------------------------------------ PLCopen XML
def _q(tag):
    return f'{{{NS}}}{tag}'


def _type_into(e, typ: str):
    """Writes the type `typ` as the child of `e` (<type>, <baseType> or <returnType>)."""
    t = (typ or '').strip()
    up = t.upper()
    m = re.match(r'ARRAY\s*\[(.*?)\]\s*OF\s+(.*)$', t, re.I | re.S)
    if m:
        dims = [d.strip() for d in m.group(1).split(',')]
        if all(re.fullmatch(r'-?\w+\s*\.\.\s*-?\w+', d) for d in dims):
            arr = ET.SubElement(e, _q('array'))
            for d in dims:
                lo, hi = [x.strip() for x in d.split('..')]
                ET.SubElement(arr, _q('dimension'), lower=lo, upper=hi)
            _type_into(ET.SubElement(arr, _q('baseType')), m.group(2))
            return
    m = re.fullmatch(r'(W?STRING)\s*(?:[\[(]\s*(\w+)\s*[\])])?', up)
    if m:
        st = ET.SubElement(e, _q(m.group(1).lower()))
        if m.group(2):
            st.set('length', m.group(2))
    elif up in PLCOPEN_ELEMENTARY:
        ET.SubElement(e, _q(up))
    elif up in ('TIME_OF_DAY', 'DATE_AND_TIME'):
        ET.SubElement(e, _q('TOD' if up == 'TIME_OF_DAY' else 'DT'))
    else:
        ET.SubElement(e, _q('derived'), name=t)


def _xhtml(parent, text: str):
    x = ET.SubElement(parent, f'{{{XHTML}}}xhtml')
    x.text = text
    return x


def _variables(parent, variables: list):
    for v in variables:
        attrs = {'name': v['name']}
        if v.get('address'):
            attrs['address'] = v['address']
        e = ET.SubElement(parent, _q('variable'), attrs)
        _type_into(ET.SubElement(e, _q('type')), v['type'])
        if v.get('init') and re.fullmatch(r'[\w.#+\-$\' ]+', v['init']):
            ET.SubElement(ET.SubElement(e, _q('initialValue')), _q('simpleValue'), value=v['init'])
        if v.get('comment'):
            _xhtml(ET.SubElement(e, _q('documentation')), v['comment'])


def _plain_text(parent, text: str):
    data = ET.SubElement(ET.SubElement(parent, _q('addData')), _q('data'),
                         {'name': CDS + 'interfaceasplaintext', 'handleUnknown': 'implementation'})
    _xhtml(ET.SubElement(data, _q('InterfaceAsPlainText')), text)


def _duration(interval: str) -> str:
    m = re.fullmatch(r'\s*(?:T#)?\s*(\d+(?:\.\d+)?)\s*(ms|s|us)?\s*', str(interval or ''), re.I)
    if not m:
        return 'PT0.02S'
    n, unit = float(m.group(1)), (m.group(2) or 'ms').lower()
    sec = n / 1000 if unit == 'ms' else n / 1e6 if unit == 'us' else n
    return 'PT' + (f'{sec:.6f}'.rstrip('0').rstrip('.') or '0') + 'S'


def to_plcopen(p: plcme.Project):
    """(the project as PLCopen XML in UTF-8, names of the POUs left out). Only ST POUs are written: a drawn POU
    (LD / FBD / CFC) has no text to write."""
    for prefix, uri in (('', NS), ('xhtml', XHTML)):
        ET.register_namespace(prefix, uri)
    now = datetime.datetime.now().replace(microsecond=0).isoformat()
    root = ET.Element(_q('project'))
    ET.SubElement(root, _q('fileHeader'), companyName='', productName='SYSAI plc-kit', productVersion='1.0.0',
                  creationDateTime=now)
    ch = ET.SubElement(root, _q('contentHeader'), name=p.name or 'Project', modificationDateTime=now)
    ci = ET.SubElement(ch, _q('coordinateInfo'))
    for lang in ('fbd', 'ld', 'sfc'):
        ET.SubElement(ET.SubElement(ci, _q(lang)), _q('scaling'), x='1', y='1')
    types = ET.SubElement(root, _q('types'))
    dts = ET.SubElement(types, _q('dataTypes'))
    for t in p.types:
        d = plcst.parse_declaration(t['declaration'])
        dt = ET.SubElement(dts, _q('dataType'), name=t['name'])
        bt = ET.SubElement(dt, _q('baseType'))
        st = next((s for s in d['sections'] if s['section'] == 'STRUCT'), None)
        if st is not None:
            _variables(ET.SubElement(bt, _q('struct')), st['vars'])
        else:
            m = re.search(r'\bTYPE\s+\w+\s*:\s*(.*?);?\s*END_TYPE', plcst.blank_comments(t['declaration']), re.S | re.I)
            enum = re.fullmatch(r'\(\s*(.*?)\s*\)(?:\s*\w+)?', m.group(1).strip(), re.S) if m else None
            if enum:
                values = ET.SubElement(ET.SubElement(bt, _q('enum')), _q('values'))
                for item in enum.group(1).split(','):
                    name, _, val = item.partition(':=')
                    if name.strip():
                        ET.SubElement(values, _q('value'), {'name': name.strip(), **({'value': val.strip()} if val.strip() else {})})
            else:
                _type_into(bt, m.group(1).strip() if m else 'INT')
        _plain_text(dt, t['declaration'])
    pous = ET.SubElement(types, _q('pous'))
    skipped = []
    for x in p.pous:
        if x['language'] != 'st':
            skipped.append(x['name'])
            continue
        d = plcst.parse_declaration(x['declaration'])
        pe = ET.SubElement(pous, _q('pou'), name=x['name'],
                           pouType={'program': 'program', 'function_block': 'functionBlock', 'function': 'function'}[x['kind']])
        iface = ET.SubElement(pe, _q('interface'))
        if x['kind'] == 'function':
            _type_into(ET.SubElement(iface, _q('returnType')), d['returns'] or 'BOOL')
        for sec in d['sections']:
            tag = SECTION_TAG.get(sec['section'])
            if not tag:
                continue
            attrs = {a: 'true' for a, w in (('retain', 'RETAIN'), ('persistent', 'PERSISTENT'), ('constant', 'CONSTANT'))
                     if w in sec['qualifiers']}
            _variables(ET.SubElement(iface, _q(tag), attrs), sec['vars'])
        _plain_text(iface, x['declaration'])
        _xhtml(ET.SubElement(ET.SubElement(pe, _q('body')), _q('ST')), x['body'])
    inst = ET.SubElement(root, _q('instances'))
    confs = ET.SubElement(inst, _q('configurations'))
    if p.tasks:
        res = ET.SubElement(ET.SubElement(confs, _q('configuration'), name=getattr(p, 'controller', '') or 'PLC'),
                            _q('resource'), name=getattr(p, 'application', '') or 'Application')
        for t in p.tasks:
            te = ET.SubElement(res, _q('task'), name=t['name'], interval=_duration(t['interval']),
                               priority=str(t.get('priority') or '15'))
            for name in t['pous']:
                ET.SubElement(te, _q('pouInstance'), name=name, typeName='')
    if p.gvls:
        add = ET.SubElement(root, _q('addData'))
        for g in p.gvls:          # one <data> holds one element
            data = ET.SubElement(add, _q('data'), {'name': CDS + 'globalvars', 'handleUnknown': 'implementation'})
            d = plcst.parse_declaration(g['declaration'])
            quals = {q for s in d['sections'] for q in s['qualifiers']}
            attrs = {a: 'true' for a, w in (('retain', 'RETAIN'), ('persistent', 'PERSISTENT'), ('constant', 'CONSTANT'))
                     if w in quals and len(d['sections']) == 1}
            ge = ET.SubElement(data, _q('globalVars'), dict(name=g['name'], **attrs))
            _variables(ge, d['vars'])
            _plain_text(ge, g['declaration'])
    ET.indent(root, space='  ')
    blob = b'<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(root, encoding='utf-8')
    return blob, skipped
