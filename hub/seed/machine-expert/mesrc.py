"""The folder of ST files (the same one plc-kit writes and checks) as the objects Machine Expert is to hold.

    project.json   {"name", "controller", "panel", "link": {"kind", "lists"}, "tasks": [{"name", "interval", "pous"}]}
    GVL.st         VAR_GLOBAL … END_VAR                         a global variable list
    Motor.st       TYPE Motor : STRUCT … END_STRUCT END_TYPE    a data type
    Noria.st       FUNCTION_BLOCK Noria  VAR… END_VAR  <code>   a POU: the declaration, then the code

The rules of telling the parts are those of plc-kit (`plcbuild.split_source`); they are repeated here because every
tool of the hub is a folder of its own on the engineer's computer.
"""
import json
import os
import re

_NAME = re.compile(r'[A-Za-z_][A-Za-z0-9_]*')


class SourceError(ValueError):
    pass


def _blank_comments(text: str) -> str:
    """The text with comments and strings blanked (same length, same lines): words are then looked for safely."""
    out, i, n = list(text), 0, len(text)

    def wipe(a, b):
        for k in range(a, b):
            if out[k] != '\n':
                out[k] = ' '
    while i < n:
        two = text[i:i + 2]
        if two == '//':
            j = text.find('\n', i)
            j = n if j < 0 else j
            wipe(i, j)
            i = j
        elif two == '(*':
            j = text.find('*)', i + 2)
            j = n if j < 0 else j + 2
            wipe(i, j)
            i = j
        elif text[i] in '\'"':
            q, j = text[i], i + 1
            while j < n and text[j] != q and text[j] != '\n':
                j += 2 if text[j] == '$' else 1
            wipe(i + 1, min(j, n))
            i = min(j, n) + 1
        else:
            i += 1
    return ''.join(out)


def split_source(text: str) -> dict:
    """One .st file -> {'what': pou | gvl | type, 'kind', 'name', 'returns', 'declaration', 'body', 'body_line'}.
    `body_line` is the line of the file (from 1) where the code starts: a compiler message about line N of the
    implementation is line `body_line + N - 1` of the file."""
    text = (text or '').replace('\r\n', '\n').lstrip('﻿')
    plain = _blank_comments(text)
    first = re.search(r'\S+', plain)
    word = first.group().upper() if first else ''
    if word == 'TYPE':
        m = re.search(r'\bTYPE\s+([A-Za-z_]\w*)', plain, re.I)
        return {'what': 'type', 'kind': 'type', 'name': m.group(1) if m else None, 'returns': None,
                'declaration': text.strip('\n'), 'body': '', 'body_line': None}
    if word.startswith('VAR_GLOBAL') or (word.startswith('{') and re.search(r'\bVAR_GLOBAL\b', plain)):
        return {'what': 'gvl', 'kind': 'gvl', 'name': None, 'returns': None, 'declaration': text.strip('\n'), 'body': '',
                'body_line': None}
    head = re.search(r'\b(PROGRAM|FUNCTION_BLOCK|FUNCTION)\s+([A-Za-z_]\w*)[ \t]*(?::[ \t]*([^;\n]+))?', plain, re.I)
    if word not in ('PROGRAM', 'FUNCTION_BLOCK', 'FUNCTION') and not (word.startswith('{') and head):
        raise SourceError('файл ST должен начинаться с PROGRAM, FUNCTION_BLOCK, FUNCTION, TYPE или VAR_GLOBAL')
    pos = plain.find('\n', head.start())
    pos = len(plain) if pos < 0 else pos
    while True:
        m = re.match(r'\s*VAR(?:_[A-Z_]+)?\b.*?\bEND_VAR\b', plain[pos:], re.S | re.I)
        if not m:
            break
        pos += m.end()
    decl = text[:pos].strip('\n')
    rest = text[pos:]
    body = rest.strip('\n')
    lead = len(rest) - len(rest.lstrip('\n'))
    body_line = text[:pos].count('\n') + lead + 1
    kind = {'program': 'program', 'function_block': 'function_block', 'function': 'function'}[head.group(1).lower()]
    returns = (head.group(3) or '').strip() or None
    return {'what': 'pou', 'kind': kind, 'name': head.group(2), 'returns': returns if kind == 'function' else None,
            'declaration': decl, 'body': body, 'body_line': body_line}


def load_dir(src_dir: str) -> dict:
    """-> {'name', 'controller', 'panel', 'link', 'tasks': [...], 'objects': [{'name', 'file', 'what', 'kind',
    'returns', 'declaration', 'body', 'body_line'}]} — data types first, then lists, blocks and functions, programs
    last: an object is created after what it uses."""
    if not os.path.isdir(src_dir):
        raise SourceError(f'нет папки {src_dir}')
    meta = {}
    mp = os.path.join(src_dir, 'project.json')
    if os.path.isfile(mp):
        try:
            with open(mp, encoding='utf-8-sig') as f:
                meta = json.load(f)
        except ValueError as exc:
            raise SourceError(f'project.json: {exc}') from None
        if not isinstance(meta, dict):
            raise SourceError('project.json: нужен объект {"name", "tasks": [...]}')
    files = sorted(f for f in os.listdir(src_dir) if f.lower().endswith('.st'))
    if not files:
        raise SourceError(f'в папке {src_dir} нет файлов .st')
    objects, seen = [], {}
    for fn in files:
        with open(os.path.join(src_dir, fn), encoding='utf-8-sig') as f:
            text = f.read()
        try:
            part = split_source(text)
        except SourceError as exc:
            raise SourceError(f'{fn}: {exc}') from None
        stem = os.path.splitext(fn)[0]
        name = stem if not part['name'] or part['name'].lower() == stem.lower() else part['name']
        if not _NAME.fullmatch(name):
            raise SourceError(f'{fn}: имя «{name}» не годится для объекта ПЛК (латиница, цифры, _)')
        if name.lower() in seen:
            raise SourceError(f'{fn}: объект {name} уже есть в {seen[name.lower()]}')
        seen[name.lower()] = fn
        objects.append(dict(part, name=name, file=fn))
    order = {'type': 0, 'gvl': 1, 'function': 2, 'function_block': 3, 'program': 4}
    objects.sort(key=lambda o: (order[o['kind']], o['name'].lower()))
    tasks = []
    for t in meta.get('tasks') or []:
        if not isinstance(t, dict) or not t.get('name'):
            raise SourceError('project.json: у задачи нужно имя: {"name": "MAST", "interval": "20ms", "pous": ["Main"]}')
        tasks.append({'name': str(t['name']), 'interval': str(t.get('interval') or '20ms'),
                      'priority': str(t.get('priority', '')), 'pous': [str(x) for x in t.get('pous') or []]})
    link = meta.get('link')
    if isinstance(link, str):
        link = {'kind': link}
    return {'name': str(meta.get('name') or os.path.basename(os.path.normpath(src_dir))),
            'controller': str(meta.get('controller') or ''), 'panel': str(meta.get('panel') or ''),
            'link': link if isinstance(link, dict) else {}, 'tasks': tasks, 'objects': objects}


def place(source: dict, message: dict) -> dict:
    """Where in the folder a compiler message points: {'file', 'line'} added when the object is one of the folder."""
    obj = next((o for o in source['objects'] if o['name'].lower() == str(message.get('object') or '').lower()), None)
    if not obj:
        return message
    out = dict(message, file=obj['file'])
    line = message.get('line')
    if line:
        out['file_line'] = (obj['body_line'] + line - 1) if (message.get('part') == 'body' and obj['body_line']) else \
            line if message.get('part') == 'declaration' else None
    return out
