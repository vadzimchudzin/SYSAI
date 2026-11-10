"""Structured Text (IEC 61131-3) as text: declarations, addresses, a structural check.

No compiler here: the real build is Machine Expert's. What this module gives is what can be told from the text alone
-- which variables a POU declares (name, type, address, initial value, comment), where a `%M` address lands in the
Modbus table of the controller, whether the blocks of a body are closed, which names a body uses that nobody declares.
"""
import re

SECTIONS = ('VAR_INPUT', 'VAR_OUTPUT', 'VAR_IN_OUT', 'VAR_GLOBAL', 'VAR_TEMP', 'VAR_STAT', 'VAR_EXTERNAL', 'VAR_CONFIG',
            'VAR_INST', 'VAR')
QUALIFIERS = ('RETAIN', 'PERSISTENT', 'CONSTANT', 'NON_RETAIN')
ELEMENTARY = {'BOOL', 'BYTE', 'WORD', 'DWORD', 'LWORD', 'SINT', 'INT', 'DINT', 'LINT', 'USINT', 'UINT', 'UDINT', 'ULINT',
              'REAL', 'LREAL', 'TIME', 'LTIME', 'DATE', 'TIME_OF_DAY', 'TOD', 'DATE_AND_TIME', 'DT', 'STRING', 'WSTRING',
              'BIT', 'ANY', 'ANY_NUM', 'ANY_INT', 'ANY_REAL', 'ANY_BIT', 'POINTER', '__UXINT', '__XWORD', '__XINT'}
BITS = {'BOOL': 1, 'BIT': 1, 'BYTE': 8, 'SINT': 8, 'USINT': 8, 'WORD': 16, 'INT': 16, 'UINT': 16, 'DWORD': 32, 'DINT': 32,
        'UDINT': 32, 'REAL': 32, 'TIME': 32, 'LWORD': 64, 'LINT': 64, 'ULINT': 64, 'LREAL': 64}
# Blocks of the standard library and of the usual utility libraries: known names, not reported as undeclared.
STANDARD = {'TON', 'TOF', 'TP', 'RTC', 'R_TRIG', 'F_TRIG', 'CTU', 'CTD', 'CTUD', 'SR', 'RS', 'BLINK', 'PID', 'ABS', 'SQRT',
            'LN', 'LOG', 'EXP', 'SIN', 'COS', 'TAN', 'ASIN', 'ACOS', 'ATAN', 'ADD', 'SUB', 'MUL', 'DIV', 'MOD', 'MOVE',
            'SHL', 'SHR', 'ROL', 'ROR', 'AND', 'OR', 'XOR', 'NOT', 'SEL', 'MAX', 'MIN', 'LIMIT', 'MUX', 'GT', 'GE', 'EQ',
            'LE', 'LT', 'NE', 'LEN', 'LEFT', 'RIGHT', 'MID', 'CONCAT', 'INSERT', 'DELETE', 'REPLACE', 'FIND', 'TRUNC',
            'SIZEOF', 'ADR', 'EXPT', 'INDEXOF', 'TIME', 'TRUE', 'FALSE', 'EXIT', 'RETURN', 'CONTINUE', 'THIS', 'SUPER',
            'NULL', 'TO', 'BY', 'AT', 'OF', 'ARRAY', 'STRUCT', 'END_STRUCT', 'REF', 'REFERENCE', 'JMP'}
KEYWORDS = {'IF', 'THEN', 'ELSIF', 'ELSE', 'END_IF', 'CASE', 'END_CASE', 'FOR', 'DO', 'END_FOR', 'WHILE', 'END_WHILE',
            'REPEAT', 'UNTIL', 'END_REPEAT', 'VAR', 'END_VAR'}
OPEN = {'IF': 'END_IF', 'CASE': 'END_CASE', 'FOR': 'END_FOR', 'WHILE': 'END_WHILE', 'REPEAT': 'END_REPEAT'}
CLOSE = {v: k for k, v in OPEN.items()}

_ADDRESS = re.compile(r'^%([IQM])([XBWDL]?)(\d+)(?:\.(\d+))?(?:\.(\d+))?$', re.I)
_IDENT = re.compile(r'[A-Za-z_][A-Za-z0-9_]*')


def blank_comments(text: str) -> str:
    """The text with comments and string / time literals replaced by spaces: positions and lines stay."""
    out, i, n = list(text), 0, len(text)

    def wipe(a, b):
        for k in range(a, min(b, n)):
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
            depth, j = 1, i + 2
            while j < n and depth:
                if text[j:j + 2] == '(*':
                    depth, j = depth + 1, j + 2
                elif text[j:j + 2] == '*)':
                    depth, j = depth - 1, j + 2
                else:
                    j += 1
            wipe(i, j)
            i = j
        elif two == '/*':
            j = text.find('*/', i + 2)
            j = n if j < 0 else j + 2
            wipe(i, j)
            i = j
        elif text[i] in '\'"':
            q, j = text[i], i + 1
            while j < n and text[j] != q and text[j] != '\n':
                j += 2 if text[j] == '$' else 1
            wipe(i + 1, j)
            i = j + 1
        else:
            i += 1
    return ''.join(out)


def _comment_at(text: str, i: int):
    """(comment text, end) of a comment that starts at `i` (after spaces on the same line), or (None, i)."""
    j = i
    while j < len(text) and text[j] in ' \t':
        j += 1
    if text[j:j + 2] == '//':
        e = text.find('\n', j)
        e = len(text) if e < 0 else e
        return text[j + 2:e].strip(), e
    if text[j:j + 2] == '(*':
        e = text.find('*)', j + 2)
        e = len(text) if e < 0 else e + 2
        return text[j + 2:e - 2].strip(), e
    return None, i


def parse_address(address: str, word_bits: bool = False):
    """`%MX12.3`, `%MW624`, `%IX0.5`, `%QW2` -> {'area', 'size', 'bit_start', 'bits'} in bits from the start of the
    area (bytes are numbered the IEC way: %MW n is bytes 2n and 2n+1, %MD n is bytes 4n..4n+3), None if not one.
    word_bits: the controller numbers bits by word (%MX a.b is bit b, 0..15, of word a) -- some targets do (ONI),
    Schneider M241 numbers them by byte (%MX a.b is bit b, 0..7, of byte a)."""
    m = _ADDRESS.match((address or '').strip())
    if not m:
        return None
    area, size = m.group(1).upper(), (m.group(2) or 'X').upper()
    a, b = int(m.group(3)), m.group(4)
    if size == 'X':
        if b is None or int(b) > (15 if word_bits else 7):
            return None
        return {'area': area, 'size': 'X', 'bit_start': a * (16 if word_bits else 8) + int(b), 'bits': 1}
    width = {'B': 8, 'W': 16, 'D': 32, 'L': 64}[size]
    return {'area': area, 'size': size, 'bit_start': a * width, 'bits': width}


def bit_over_7(address: str) -> bool:
    """A bit address whose bit number is 8..15: the sign of numbering bits by word."""
    m = _ADDRESS.match((address or '').strip())
    return bool(m and (m.group(2) or 'X').upper() == 'X' and m.group(4) is not None and 7 < int(m.group(4)) <= 15)


def modbus(address: str, word_bits: bool = False):
    """Where a `%M` address is in the Modbus table of the controller (holding registers, the %MW area):
    {'register' (0-based), 'bit' (0..15, for a bit), 'registers' (how many), 'one_based'}. By bytes, %MX a.b is bit
    (a mod 2) * 8 + b of register a div 2 -- the same memory as %MW (a div 2); by words it is bit b of register a.
    None for inputs / outputs."""
    a = parse_address(address, word_bits)
    if not a or a['area'] != 'M':
        return None
    reg = a['bit_start'] // 16
    out = {'register': reg, 'one_based': reg + 1, 'registers': max(1, a['bits'] // 16)}
    if a['size'] == 'X':
        out['bit'] = a['bit_start'] % 16
    elif a['size'] == 'B':
        out['byte'] = 'low' if a['bit_start'] % 16 == 0 else 'high'
    return out


def _split_type(rest: str):
    """`BOOL := TRUE` -> ('BOOL', 'TRUE'); the `:=` of an initial value at depth 0."""
    depth = 0
    for k, c in enumerate(rest):
        if c in '([':
            depth += 1
        elif c in ')]':
            depth -= 1
        elif c == ':' and rest[k:k + 2] == ':=' and depth == 0:
            return rest[:k].strip(), rest[k + 2:].strip()
    return rest.strip(), None


def parse_declaration(text: str) -> dict:
    """The declaration part of a POU / GVL / data type.

    -> {'kind': program | function_block | function | gvl | type | None, 'name', 'returns' (a function's type),
        'extends', 'sections': [{'section': 'VAR_INPUT', 'qualifiers': ['RETAIN'], 'vars': [...]}],
        'vars': the same variables in one list, each {'name', 'type', 'address', 'init', 'comment', 'group', 'section',
        'qualifiers', 'line'}}. `group` is the last comment that stood on its own line before the variable
        («//Аварии»): how engineers title groups of variables."""
    text = text or ''
    plain = blank_comments(text)
    out = {'kind': None, 'name': None, 'returns': None, 'extends': None, 'sections': [], 'vars': []}
    head = re.search(r'\b(PROGRAM|FUNCTION_BLOCK|FUNCTION|TYPE)\s+([A-Za-z_]\w*)', plain, re.I)
    if head:
        out['kind'] = {'program': 'program', 'function_block': 'function_block', 'function': 'function',
                       'type': 'type'}[head.group(1).lower()]
        out['name'] = head.group(2)
        tail = plain[head.end():plain.find('\n', head.end()) if plain.find('\n', head.end()) >= 0 else len(plain)]
        r = re.match(r'\s*:\s*([^;\n]+)', tail)
        if r and out['kind'] == 'function':
            out['returns'] = r.group(1).strip().rstrip(';').strip()
        e = re.search(r'\bEXTENDS\s+([A-Za-z_][\w.]*)', tail, re.I)
        if e:
            out['extends'] = e.group(1)
    for m in re.finditer(r'\b(VAR(?:_[A-Z_]+)?)\b([^\n]*)\n?(.*?)\bEND_VAR\b', plain, re.S | re.I):
        section = m.group(1).upper()
        if section not in SECTIONS:
            continue
        quals = [q for q in re.findall(r'[A-Z_]+', m.group(2).upper()) if q in QUALIFIERS]
        sec = {'section': section, 'qualifiers': quals, 'vars': _statements(text, plain, m.start(3), m.end(3), section, quals)}
        out['vars'].extend(sec['vars'])
        out['sections'].append(sec)
    if out['kind'] == 'type':
        st = re.search(r'\bSTRUCT\b(.*?)\bEND_STRUCT\b', plain, re.S | re.I)
        if st:
            sec = {'section': 'STRUCT', 'qualifiers': [], 'vars': _statements(text, plain, st.start(1), st.end(1), 'STRUCT', [])}
            out['vars'].extend(sec['vars'])
            out['sections'].append(sec)
    if out['kind'] is None and any(s['section'] == 'VAR_GLOBAL' for s in out['sections']):
        out['kind'] = 'gvl'
    return out


def _statements(text: str, plain: str, a: int, b: int, section: str, quals: list) -> list:
    """Variables declared between `a` and `b` (`names [AT address] : type [:= init];` with their comments)."""
    found, group, pos = [], None, a
    while pos < b:
        end = plain.find(';', pos, b)
        if end < 0:
            break
        for line_m in re.finditer(r'[^\n]*\n?', text[pos:end]):      # comments on their own lines title a group
            if not line_m.group().strip():
                continue
            if plain[pos + line_m.start():pos + line_m.end()].strip():
                break
            c, _ = _comment_at(text, pos + line_m.start())
            if c:
                group = c
        stmt = plain[pos:end]
        comment, cend = _comment_at(text, end + 1)
        line = text.count('\n', 0, pos + (len(stmt) - len(stmt.lstrip()))) + 1
        pos = max(end + 1, cend if comment is not None else end + 1)
        k = _first_colon(stmt)
        if k < 0:
            continue
        left, right = stmt[:k].strip(), stmt[k + 1:].strip()
        address = None
        am = re.search(r'\bAT\s+(%[A-Za-z*]+[\d.*]*)', left, re.I)
        if am:
            address, left = am.group(1).upper(), (left[:am.start()] + left[am.end():]).strip()
        typ, init = _split_type(right)
        typ = re.sub(r'\s+', ' ', typ)
        for name in [x.strip() for x in left.split(',') if x.strip()]:
            if _IDENT.fullmatch(name):
                found.append({'name': name, 'type': typ, 'address': address, 'init': init, 'comment': comment,
                              'group': group, 'section': section, 'qualifiers': quals, 'line': line})
    return found


def _first_colon(stmt: str) -> int:
    """The `:` between the names and the type (not the one of `:=`), -1 if none."""
    for k, c in enumerate(stmt):
        if c == ':' and stmt[k:k + 2] != ':=':
            return k
    return -1


def base_type(typ: str) -> str:
    """`ARRAY [0..9] OF INT` -> INT, `STRING(80)` -> STRING, `POINTER TO X` -> X, `REF_TO X` -> X."""
    t = (typ or '').strip()
    m = re.match(r'ARRAY\s*\[.*?\]\s*OF\s+(.*)$', t, re.I | re.S)
    if m:
        return base_type(m.group(1))
    m = re.match(r'(?:POINTER\s+TO|REFERENCE\s+TO|REF_TO)\s+(.*)$', t, re.I)
    if m:
        return base_type(m.group(1))
    m = re.match(r'([A-Za-z_][\w.]*)', t)
    return m.group(1) if m else t


def identifiers(body: str) -> list:
    """[(name, line, after a dot)] of every identifier of a body, comments and strings left out."""
    plain = blank_comments(body or '')
    out, depth, at = [], 0, 0
    for m in re.finditer(r'(?<![A-Za-z0-9_#%])[A-Za-z_][A-Za-z0-9_]*', plain):       # not T#5S, 16#FF, %MW1
        k = m.start()
        depth += plain.count('(', at, k) - plain.count(')', at, k)
        at = k
        nxt = plain[m.end():m.end() + 40].lstrip()
        if depth > 0 and (nxt.startswith(':=') or nxt.startswith('=>')):   # a named parameter of a call: IN := ...
            continue
        j = k - 1
        while j >= 0 and plain[j] in ' \t':
            j -= 1
        after_dot = j >= 0 and plain[j] == '.'
        if re.fullmatch(r'(?:T|TIME|LTIME|D|DATE|TOD|DT|[A-Z]*INT|BYTE|D?WORD|L?REAL)', m.group(), re.I) \
                and plain[m.end():m.end() + 1] == '#':
            continue
        out.append((m.group(), plain.count('\n', 0, k) + 1, after_dot))
    return out


def check_body(body: str) -> list:
    """Structure of an ST body: [{'level': error | warning, 'line', 'text'}] -- blocks not closed or closed by the wrong
    word, ELSE / ELSIF / UNTIL outside their block, brackets, `=` where `:=` was meant after a lone name, a statement
    without `;`. An empty list does not mean the code compiles: only Machine Expert tells that."""
    plain = blank_comments(body or '')
    issues, stack = [], []

    def add(level, line, text):
        issues.append({'level': level, 'line': line, 'text': text})
    for m in re.finditer(r'[A-Za-z_]\w*', plain):
        w, line = m.group().upper(), plain.count('\n', 0, m.start()) + 1
        if w in OPEN:
            stack.append((w, line))
        elif w in CLOSE:
            if not stack:
                add('error', line, f'{w} без {CLOSE[w]}')
            elif stack[-1][0] != CLOSE[w]:
                add('error', line, f'{w} закрывает {stack[-1][0]} (строка {stack[-1][1]}): нужен {OPEN[stack[-1][0]]}')
                stack.pop()
            else:
                stack.pop()
        elif w in ('ELSIF', 'THEN') and not any(s[0] == 'IF' for s in stack):
            add('error', line, f'{w} вне IF')
        elif w == 'ELSE' and not any(s[0] in ('IF', 'CASE') for s in stack):
            add('error', line, 'ELSE вне IF и CASE')
        elif w == 'UNTIL' and not any(s[0] == 'REPEAT' for s in stack):
            add('error', line, 'UNTIL вне REPEAT')
        elif w == 'DO' and not any(s[0] in ('FOR', 'WHILE') for s in stack):
            add('error', line, 'DO вне FOR и WHILE')
    for w, line in stack:
        add('error', line, f'{w} не закрыт: нет {OPEN[w]}')
    depth, opened = 0, []
    for k, c in enumerate(plain):
        if c in '([':
            opened.append((c, plain.count('\n', 0, k) + 1))
        elif c in ')]':
            if not opened:
                add('error', plain.count('\n', 0, k) + 1, f'лишняя скобка {c}')
            else:
                o, line = opened.pop()
                if (o, c) not in (('(', ')'), ('[', ']')):
                    add('error', plain.count('\n', 0, k) + 1, f'скобка {c} закрывает {o} (строка {line})')
    for o, line in opened:
        add('error', line, f'скобка {o} не закрыта')
    del depth
    # statements: text between `;` -- an assignment or a call that runs into the next line without `;`
    n = 0
    for raw in plain.split('\n'):
        n += 1
        s = raw.strip()
        if not s:
            continue
        if re.match(r'^[A-Za-z_][\w.\[\]]*\s*=\s*[^=]', s) and ':=' not in s and not re.search(r'\b(IF|ELSIF|WHILE|UNTIL|CASE)\b', s, re.I):
            add('warning', n, 'похоже, вместо «:=» стоит «=» (сравнение вместо присваивания)')
        if re.match(r'^[A-Za-z_][\w.\[\]]*\s*:=.*[^;,(\s]$', s) and not re.search(r'\b(AND|OR|XOR|NOT|MOD)$|[+\-*/<>=,(]$', s, re.I):
            nxt = next((x.strip() for x in plain.split('\n')[n:] if x.strip()), '')
            if not re.match(r'^(AND|OR|XOR|MOD|[+\-*/<>=)\],.(])', nxt, re.I):
                add('warning', n, 'присваивание без «;» в конце')
    return issues
