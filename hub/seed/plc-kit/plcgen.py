"""The program of a controller made from the specification of a project — by code, not by a model.

`project.spec.json` says what mechanisms there are, their signals and channels, what stops what and which routes the
material takes. From it this module writes the folder of ST files `plc_project_build` makes a `.project` of:

    IO.st        raw variables of the channels (x_DI_I0, y_DQ_Q3) — `plc_io_map` puts them on the channels
    Signals.st   a variable per signal of a mechanism (N1_speed, N1_run) and per signal of its own (EStop)
    HMI.st       what the panel reads and writes: commands, modes, states, alarms, routes (published in OPC UA, or with
                 addresses %M for Modbus)
    Mechs.st     the instances of the blocks, one per mechanism
    FB_Mech.st, FB_Pulse.st   the blocks used when a kind of mechanism names no block of its own
    BIND.st      channels → signals (with NOT for a normally closed contact)
    Mech.st      for every mechanism: its protections, its permission (interlocks), the call of its block
    Routes.st    the routes: start from the end of the flow to its beginning, stop from the beginning to the end
    BIND_OUT.st  signals → output channels
    project.json the tasks and the link with the panel

Every file made here starts with the mark `GENERATED`: it is written anew on each run and never edited by hand — what
the generator cannot know (a special sequence, a calculation) lives in files of your own next to these, and
`project.programs` of the specification puts your programs into the task. A file with a name of the generator but
without the mark is never overwritten: the run stops and says so.

The same specification always gives the same text, byte for byte."""
import json
import os
import re

import plcspec
import plcst

MARK = '// GENERATED из project.spec.json инструментом plc_generate — не править: меняйте спецификацию и запускайте снова'
OWN = ('IO', 'Signals', 'HMI', 'Mechs', 'FB_Mech', 'FB_Pulse', 'BIND', 'Mech', 'Routes', 'BIND_OUT')
TYPES = {'BOOL', 'INT', 'UINT', 'DINT', 'UDINT', 'WORD', 'DWORD', 'REAL', 'TIME'}
_ID = re.compile(r'[A-Za-z_][A-Za-z0-9_]*$')

FB_MECH = '''FUNCTION_BLOCK FB_Mech
VAR_INPUT
	auto: BOOL; //автоматический режим: работает по want (от маршрута)
	want: BOOL; //в автомате: механизм должен работать
	m_start: BOOL; //ручной режим: пуск (импульс)
	m_stop: BOOL; //ручной режим: стоп (импульс)
	permit: BOOL; //разрешение: блокировки выполнены, аварийного стопа нет
	fault: BOOL; //сработала защита (уровень)
	reset: BOOL; //сброс аварии
END_VAR
VAR_OUTPUT
	run: BOOL; //включить привод
	alarm: BOOL; //авария: защита сработала и ждёт сброса
	state: INT; //0 стоит, 1 работает, 2 авария, 3 ждёт разрешения
END_VAR
VAR
	req: BOOL; //запрос на работу
END_VAR

IF reset AND NOT fault THEN
	alarm := FALSE;
END_IF
IF fault THEN
	alarm := TRUE;
END_IF
IF auto THEN
	req := want;
ELSE
	IF m_start THEN
		req := TRUE;
	END_IF
	IF m_stop OR alarm OR NOT permit THEN
		req := FALSE; // в ручном режиме после аварии или снятого разрешения сам не запускается
	END_IF
END_IF
run := req AND permit AND NOT alarm;
IF alarm THEN
	state := 2;
ELSIF run THEN
	state := 1;
ELSIF req THEN
	state := 3;
ELSE
	state := 0;
END_IF
'''

FB_PULSE = '''FUNCTION_BLOCK FB_Pulse
VAR_INPUT
	run: BOOL; //механизм включён
	pulse: BOOL; //сигнал импульсного датчика (датчик скорости)
	timeout: TIME; //сколько сигнал может не меняться
END_VAR
VAR_OUTPUT
	fault: BOOL; //при работе сигнал не меняется дольше timeout: механизм стоит или датчик неисправен
END_VAR
VAR
	t_hi: TON;
	t_lo: TON;
END_VAR

t_hi(IN := run AND pulse, PT := timeout);
t_lo(IN := run AND NOT pulse, PT := timeout);
fault := t_hi.Q OR t_lo.Q;
'''


class GenError(Exception):
    pass


def role_of(item) -> tuple:
    """('speed', 'BOOL') for «speed»; ('level', 'INT') for «level:INT» — a signal of a kind and its type."""
    name, _, typ = str(item).partition(':')
    typ = typ.strip().upper() or 'BOOL'
    return name.strip(), typ if typ in TYPES else 'BOOL'


def channel_of(value) -> tuple:
    """(device, channel, inverted) of «DI.I0» / «!DI.I11» (a normally closed contact); None — no channel."""
    text = str(value or '').strip()
    inverted = text.startswith('!')
    m = plcspec.CHANNEL.match(text.lstrip('!').strip())
    return (m.group(1), m.group(2).lstrip('%'), inverted) if m else None


def raw_name(device: str, channel: str, direction: str) -> str:
    safe = re.sub(r'[^A-Za-z0-9]+', '_', f'{device}_{channel}').strip('_')
    return ('x_' if direction == 'in' else 'y_') + safe


def _seconds(value, default: float) -> str:
    try:
        v = float(value)
    except (TypeError, ValueError):
        v = default
    v = max(0.0, min(3600.0, v))
    return f'T#{int(round(v * 1000))}ms'


def _comment(text) -> str:
    return re.sub(r'\s+', ' ', str(text or '')).replace('*)', '* )').strip()[:120]


def _model(data: dict) -> dict:
    """The specification as the generator needs it; everything doubtful is an error here, not in the ST."""
    got = plcspec.check(data)
    errors = [f"{i['where']}: {i['text']}" for i in got['issues'] if i['level'] == 'error']
    if errors:
        raise GenError('спецификация с ошибками — сначала plc_spec_check до complete: true:\n' + '\n'.join(errors[:30]))
    types = data.get('types') or {}
    kinds = {}
    for name, t in types.items():
        roles = {}
        for side in ('in', 'out'):
            for item in list(t.get(side) or []):
                r, typ = role_of(item)
                roles[r] = (side, typ)
        for item in t.get('optional') or []:
            r, typ = role_of(item)
            roles.setdefault(r, ('', typ))
        kinds[name] = {'roles': roles, 'fb': str(t.get('fb') or ''), 'pins': t.get('pins') if isinstance(t.get('pins'), dict) else {},
                       'faults': [str(x) for x in t.get('faults') or []], 'pulse': [str(x) for x in t.get('pulse') or []],
                       'run': str(t.get('run') or ''), 'title': str(t.get('title') or name)}
    mechs, ids = [], set()
    for m in data.get('mechanisms') or []:
        kind = kinds[m['type']]
        sig = []
        for side in ('in', 'out'):
            for role, ch in (m.get(side) or {}).items():
                known = kind['roles'].get(role_of(role)[0])
                sig.append({'role': role_of(role)[0], 'dir': side, 'type': known[1] if known else 'BOOL', 'channel': channel_of(ch)})
        outs = [s['role'] for s in sig if s['dir'] == 'out']
        run = kind['run'] or ('run' if 'run' in outs else (outs[0] if outs else ''))
        params = m.get('params') if isinstance(m.get('params'), dict) else {}
        locks = m.get('interlocks') if isinstance(m.get('interlocks'), dict) else {}
        mechs.append({'id': m['id'], 'type': m['type'], 'title': str(m.get('title') or m['id']), 'signals': sig, 'run': run,
                      'kind': kind, 'params': params, 'requires': [str(x) for x in locks.get('requires_running') or []],
                      'stops': [str(x) for x in locks.get('stops') or []], 'src': str(m.get('src') or '')})
        ids.add(m['id'])
    own = []
    for s in data.get('signals') or []:
        own.append({'id': s['id'], 'dir': s['dir'], 'type': role_of('x:' + str(s.get('type') or 'BOOL'))[1], 'channel': channel_of(s.get('channel')),
                    'title': str(s.get('title') or s['id']), 'role': str(s.get('role') or '')})
    routes = []
    for r in data.get('routes') or []:
        if not isinstance(r, dict) or not _ID.match(str(r.get('id') or '')):
            raise GenError('routes: у маршрута нужен id латиницей: {"id": "R1", "title": "…", "mechanisms": ["N1", "C1"]}')
        chain = [str(x) for x in r.get('mechanisms') or []]
        missing = [x for x in chain if x not in ids]
        if missing or len(chain) < 1:
            raise GenError(f"routes.{r['id']}: " + (f"механизмов {', '.join(missing)} в спецификации нет" if missing else 'нет механизмов'))
        if len(set(chain)) != len(chain):
            raise GenError(f"routes.{r['id']}: механизм назван дважды")
        routes.append({'id': str(r['id']), 'title': str(r.get('title') or r['id']), 'chain': chain})
    if len({r['id'].lower() for r in routes}) != len(routes):
        raise GenError('routes: id маршрута повторяется')
    names = {m['id'].lower() for m in mechs} | {s['id'].lower() for s in own}
    for r in routes:
        if r['id'].lower() in names:
            raise GenError(f"routes.{r['id']}: такой id уже есть у механизма или сигнала")
    project = data.get('project') or {}
    programs = [str(p) for p in project.get('programs') or [] if _ID.match(str(p))]
    return {'project': project, 'mechs': mechs, 'signals': own, 'routes': routes, 'programs': programs,
            'link': str(project.get('link') or 'opcua').lower()}


# ----------------------------------------------------------------------------- the files
def _gvl(lines: list, published: bool = False) -> str:
    head = [MARK]
    if published:
        head.append("{attribute 'symbol' := 'readwrite'}")
    return '\n'.join(head + ['VAR_GLOBAL'] + lines + ['END_VAR']) + '\n'


def _program(name: str, body: list, variables=()) -> str:
    out = [MARK, f'PROGRAM {name}']
    if variables:
        out += ['VAR'] + list(variables) + ['END_VAR']
    return '\n'.join(out + [''] + body) + '\n'


def _io(model: dict) -> tuple:
    """(IO.st, BIND.st, BIND_OUT.st, the changes for plc_io_map, signals without a channel)."""
    raw, bind, bind_out, changes, loose = [], [], [], [], []
    seen = {}

    def one(var, direction, typ, channel, title):
        if channel is None:
            loose.append(var)
            return
        device, ch, inverted = channel
        name = raw_name(device, ch, direction)
        if name.lower() not in seen:
            seen[name.lower()] = True
            raw.append(f"\t{name}: {typ}; //{device}.{ch} — {_comment(title)}")
            changes.append({'device': device, 'channel': ch, 'variable': f'IO.{name}'})
        neg = 'NOT ' if inverted and typ == 'BOOL' else ''
        if direction == 'in':
            bind.append(f"{var} := {neg}IO.{name}; //{_comment(title)}")
        else:
            bind_out.append(f"IO.{name} := {neg}{var}; //{_comment(title)}")
    for m in model['mechs']:
        for s in m['signals']:
            one(f"{m['id']}_{s['role']}", s['dir'], s['type'], s['channel'], f"{m['title']}: {s['role']}")
    for s in model['signals']:
        one(s['id'], s['dir'], s['type'], s['channel'], s['title'])
    return (_gvl(raw or ['\t//каналов в спецификации нет']),
            _program('BIND', bind or ['; //входов с каналами в спецификации нет']),
            _program('BIND_OUT', bind_out or ['; //выходов с каналами в спецификации нет']), changes, loose)


def _signals(model: dict) -> str:
    lines = []
    for m in model['mechs']:
        lines.append(f"\t//{_comment(m['title'])} ({m['type']})" + (f" — {_comment(m['src'])}" if m['src'] else ''))
        for s in m['signals']:
            lines.append(f"\t{m['id']}_{s['role']}: {s['type']}; //{'вход' if s['dir'] == 'in' else 'выход'}")
        lines.append(f"\t{m['id']}_fault: BOOL; //сработала защита")
        lines.append(f"\t{m['id']}_permit: BOOL; //блокировки выполнены")
        lines.append(f"\t{m['id']}_want: BOOL; //маршрут требует работы")
    if model['signals']:
        lines.append('\t//Сигналы вне механизмов')
        for s in model['signals']:
            lines.append(f"\t{s['id']}: {s['type']}; //{_comment(s['title'])}")
    lines.append('\tSTOP_ALL: BOOL; //любой аварийный стоп: снимает разрешение у всех механизмов')
    return _gvl(lines)


def _hmi(model: dict) -> str:
    """Commands and states for the panel. OPC UA — published by name; Modbus — addresses %M in order: bits first, the
    words after them (a bit and a word never share a register)."""
    bits, words = [], []
    bits.append(('Alarms_reset', 'сброс аварий'))
    for m in model['mechs']:
        t = _comment(m['title'])
        bits += [(f"{m['id']}_auto", f'{t}: автоматический режим'), (f"{m['id']}_cmd_start", f'{t}: пуск (ручной режим)'),
                 (f"{m['id']}_cmd_stop", f'{t}: стоп (ручной режим)'), (f"{m['id']}_on", f'{t}: работает'),
                 (f"{m['id']}_alarm", f'{t}: авария')]
        words.append((f"{m['id']}_state", f'{t}: 0 стоит, 1 работает, 2 авария, 3 ждёт разрешения'))
    for r in model['routes']:
        t = _comment(r['title'])
        bits += [(f"{r['id']}_cmd_start", f'маршрут {t}: пуск'), (f"{r['id']}_cmd_stop", f'маршрут {t}: стоп'),
                 (f"{r['id']}_active", f'маршрут {t}: включён')]
    modbus = model['link'].startswith('modbus')
    lines = []
    for n, (name, title) in enumerate(bits):
        at = f' AT %MX{n // 8}.{n % 8}' if modbus else ''
        lines.append(f'\t{name}{at}: BOOL; //{title}')
    first_word = (len(bits) + 15) // 16  # the bits fill bytes 0…; the words start at the next whole register
    for n, (name, title) in enumerate(words):
        at = f' AT %MW{first_word + n}' if modbus else ''
        lines.append(f'\t{name}{at}: INT; //{title}')
    return _gvl(lines, published=not modbus)


def _mechs(model: dict, own_fbs: dict) -> tuple:
    """(Mechs.st, Mech.st, the outputs nothing drives, remarks)."""
    inst, body, undriven, notes = [], [], [], []
    stops = [s for s in model['signals'] if s['dir'] == 'in' and s['role'].lower() in ('estop', 'stop_all', 'аварийный стоп')]
    body.append('STOP_ALL := ' + (' OR '.join(s['id'] for s in stops) if stops else 'FALSE') + '; //аварийные стопы (сигналы с role: estop)')
    if not stops:
        notes.append('ни у одного сигнала нет role: "estop" — аварийный стоп в разрешения механизмов не входит')
    body.append('')
    variables = []
    for m in model['mechs']:
        mid, kind = m['id'], m['kind']
        have = {s['role']: s for s in m['signals']}
        body.append(f"(* {mid} — {_comment(m['title'])} *)")
        faults = []
        for f in kind['faults']:
            neg = f.startswith('!')
            role = f.lstrip('!')
            if role in have and have[role]['dir'] == 'in':
                faults.append(('NOT ' if neg else '') + f'{mid}_{role}')
        for role in kind['pulse']:
            if role in have and have[role]['dir'] == 'in':
                variables.append(f'\t{mid}_{role}_mon: FB_Pulse; //контроль импульсов: {_comment(m["title"])}, {role}')
                body.append(f"{mid}_{role}_mon(run := {mid}_on, pulse := {mid}_{role}, timeout := {_seconds(m['params'].get(role + '_timeout_s'), 5)});")
                faults.append(f'{mid}_{role}_mon.fault')
        if not kind['faults'] and not kind['pulse']:
            notes.append(f"{mid}: у вида «{m['type']}» не названы защиты (faults, pulse) — авария механизма никогда не возникнет")
        body.append(f"{mid}_fault := " + (' OR '.join(faults) if faults else 'FALSE') + ';')
        permit = ['NOT STOP_ALL'] + [f'{x}_on' for x in m['requires']]
        blockers = [o['id'] for o in model['mechs'] if mid in o['stops']]
        permit += [f'{x}_on' for x in blockers if x not in m['requires']]
        body.append(f"{mid}_permit := " + ' AND '.join(permit) + ';')
        fb = kind['fb']
        if fb:
            if fb.lower() not in own_fbs:
                raise GenError(f"{mid}: блок {fb} вида «{m['type']}» не найден в папке — положите туда {fb}.st (plc_export_st из "
                               'проекта-образца) или уберите fb из types')
            decl = own_fbs[fb.lower()]
            pins = []
            for pin, source in kind['pins'].items():
                where = decl.get(str(pin).lower())
                if where is None:
                    raise GenError(f"{mid}: у блока {fb} нет входа или выхода «{pin}» (types.{m['type']}.pins)")
                src = str(source)
                special = {'@auto': f'{mid}_auto', '@want': f'{mid}_want', '@permit': f'{mid}_permit', '@fault': f'{mid}_fault',
                           '@reset': 'Alarms_reset', '@m_start': f'{mid}_cmd_start', '@m_stop': f'{mid}_cmd_stop',
                           '@on': f'{mid}_on', '@alarm': f'{mid}_alarm', '@state': f'{mid}_state'}
                if src in special:
                    expr = special[src]
                elif src.startswith('='):
                    expr = src[1:].replace('{id}', mid).strip()
                elif src in have:
                    expr = f'{mid}_{src}'
                else:
                    continue  # a signal this mechanism does not have (optional): the pin keeps its default
                pins.append(f"{pin} {'=>' if where == 'out' else ':='} {expr}")
            if '@on' not in kind['pins'].values():
                notes.append(f"{mid}: в pins блока {fb} нет «@on» — панель не узнает, что механизм работает")
            inst.append(f"\t{mid}: {fb}; //{_comment(m['title'])}")
            body.append(f"{mid}(" + (',\n\t'.join(pins)) + ');')
        else:
            inst.append(f"\t{mid}: FB_Mech; //{_comment(m['title'])}")
            body.append(f"{mid}(auto := {mid}_auto, want := {mid}_want, m_start := {mid}_cmd_start, m_stop := {mid}_cmd_stop,\n"
                        f"\tpermit := {mid}_permit, fault := {mid}_fault, reset := Alarms_reset,\n"
                        f"\trun => {mid}_on, alarm => {mid}_alarm, state => {mid}_state);")
            if m['run']:
                body.append(f"{mid}_{m['run']} := {mid}_on;")
            else:
                notes.append(f'{mid}: у механизма нет выхода — блок считает, но ничего не включает')
        for s in m['signals']:
            if s['dir'] == 'out' and s['role'] != m['run'] and not (fb and s['role'] in kind['pins'].values()):
                undriven.append(f"{mid}_{s['role']}")
        body.append('')
    return _gvl(inst or ['\t//механизмов нет']), _program('Mech', body, variables), undriven, notes


def _routes(model: dict) -> str:
    """A route starts its mechanisms from the end of the flow to its beginning (nothing is fed onto a conveyor that
    stands) and stops them from the beginning to the end (what is on the way runs out). A mechanism of several routes
    works while any of them wants it."""
    variables, body = [], []
    wants = {m['id']: [] for m in model['mechs']}
    params = {m['id']: m['params'] for m in model['mechs']}
    for r in model['routes']:
        rid, chain = r['id'], r['chain']
        body.append(f"(* Маршрут {rid} — {_comment(r['title'])}: {' → '.join(chain)} *)")
        body += [f'IF {rid}_cmd_start THEN', f'\t{rid}_active := TRUE;', 'END_IF',
                 f'IF {rid}_cmd_stop OR STOP_ALL THEN', f'\t{rid}_active := FALSE;', 'END_IF']
        for i, mid in enumerate(chain):
            after = chain[i + 1] if i + 1 < len(chain) else ''     # the next one down the flow: must run before this starts
            before = chain[i - 1] if i > 0 else ''                  # the one up the flow: must stop before this stops
            start = f'{rid}_active'
            if after:
                variables.append(f'\t{rid}_{mid}_up: TON; //{mid} пускается после {after}')
                body.append(f"{rid}_{mid}_up(IN := {rid}_active AND {after}_on, PT := {_seconds(params[mid].get('start_delay_s'), 3)});")
                start = f'{rid}_{mid}_up.Q'
            stop = ''
            if before:
                variables.append(f'\t{rid}_{mid}_down: TOF; //{mid} останавливается после {before}')
                body.append(f"{rid}_{mid}_down(IN := {before}_on, PT := {_seconds(params[mid].get('stop_delay_s'), 3)});")
                stop = f' OR (NOT {rid}_active AND {mid}_on AND {rid}_{mid}_down.Q)'
            variables.append(f'\t{rid}_{mid}_w: BOOL; //{rid} требует {mid}')
            body.append(f'{rid}_{mid}_w := {start}{stop};')
            wants[mid].append(f'{rid}_{mid}_w')
        body.append('')
    body.append('(* Чего требуют маршруты от каждого механизма *)')
    for m in model['mechs']:
        body.append(f"{m['id']}_want := " + (' OR '.join(wants[m['id']]) if wants[m['id']] else 'FALSE') + ';')
    return _program('Routes', body, variables)


def _project_json(model: dict) -> dict:
    pous = ['BIND', 'Routes', 'Mech'] + [p for p in model['programs'] if p not in OWN] + ['BIND_OUT']
    link = {'kind': model['link']}
    if model['link'] == 'opcua':
        link['lists'] = ['HMI']
    project = model['project']
    return {'name': str(project.get('name') or ''), 'controller': str(project.get('controller') or ''),
            'panel': str(project.get('panel') or ''), 'link': link,
            'tasks': [{'name': 'MAST', 'interval': '20ms', 'priority': 15, 'pous': pous}], 'generated': True}


def render(data: dict, own_fbs=None) -> dict:
    """{'files': {name: text}, 'project': project.json, 'io_map': [...], 'report': {...}} — nothing is written."""
    model = _model(data)
    io, bind, bind_out, changes, loose = _io(model)
    mechs, mech, undriven, notes = _mechs(model, own_fbs or {})
    files = {'IO.st': io, 'Signals.st': _signals(model), 'HMI.st': _hmi(model), 'Mechs.st': mechs, 'BIND.st': bind,
             'Mech.st': mech, 'Routes.st': _routes(model), 'BIND_OUT.st': bind_out}
    builtin = any(not m['kind']['fb'] for m in model['mechs'])
    if builtin or not model['mechs']:
        files['FB_Mech.st'] = MARK + '\n' + FB_MECH
    if any(m['kind']['pulse'] for m in model['mechs']):
        files['FB_Pulse.st'] = MARK + '\n' + FB_PULSE
    for r in model['routes']:  # a mechanism of a route that waits for one no route of its own starts
        for mid in r['chain']:
            m = next(x for x in model['mechs'] if x['id'] == mid)
            for need in m['requires']:
                if need not in r['chain']:
                    notes.append(f"маршрут {r['id']}: {mid} работает, только пока работает {need}, а {need} в этом маршруте нет — "
                                 'добавьте его в маршрут или включайте отдельно')
    if not model['routes']:
        notes.append('в спецификации нет маршрутов (routes): в автоматическом режиме механизмы никто не включит')
    report = {'mechanisms': len(model['mechs']), 'routes': len(model['routes']), 'channels': len(changes),
              'signals_without_channel': loose, 'outputs_nothing_drives': undriven, 'notes': notes,
              'tasks': _project_json(model)['tasks'][0]['pous']}
    return {'files': files, 'project': _project_json(model), 'io_map': changes, 'report': report}


def _own_fbs(out_dir: str) -> dict:
    """{name of a function block lying in the folder, lower case: {pin, lower case: 'in' | 'out'}} — blocks of your own
    (taken from a sample project) the kinds of mechanisms may name."""
    out = {}
    try:
        names = sorted(os.listdir(out_dir))
    except OSError:
        return out
    for fn in names:
        if not fn.lower().endswith('.st'):
            continue
        try:
            with open(os.path.join(out_dir, fn), encoding='utf-8-sig') as f:
                text = f.read()
        except OSError:
            continue
        m = re.search(r'^\s*FUNCTION_BLOCK\s+([A-Za-z_]\w*)', plcst.blank_comments(text), re.M)
        if not m or text.lstrip().startswith(MARK):
            continue
        pins = {}
        for part, side in re.findall(r'\bVAR_(INPUT|OUTPUT|IN_OUT)\b(.*?)\bEND_VAR\b', plcst.blank_comments(text), re.S):
            for name in re.findall(r'^\s*([A-Za-z_]\w*)\s*(?:AT\s+%\S+\s*)?:', side, re.M):
                pins[name.lower()] = 'out' if part == 'OUTPUT' else 'in'
        out[m.group(1).lower()] = pins
    return out


def generate(spec_file: str, out_dir: str) -> dict:
    """Writes the folder of ST files from the specification. Files of the generator are replaced; a file of yours with
    one of its names stops the run; other files of the folder are not touched. -> the report with what was written."""
    data = plcspec.load(spec_file)
    os.makedirs(out_dir, exist_ok=True)
    got = render(data, _own_fbs(out_dir))
    for name in got['files']:
        path = os.path.join(out_dir, name)
        if os.path.isfile(path):
            with open(path, encoding='utf-8-sig') as f:
                head = f.readline().strip()
            if head != MARK:
                raise GenError(f'{name} в папке {out_dir} написан не генератором: переименуйте свой файл — это имя занято '
                               'генератором, и он его перезаписывает')
    meta_path = os.path.join(out_dir, 'project.json')
    if os.path.isfile(meta_path):
        try:
            with open(meta_path, encoding='utf-8-sig') as f:
                old = json.load(f)
        except ValueError:
            old = {}
        if isinstance(old, dict) and not old.get('generated') and old.get('tasks'):
            raise GenError('project.json в папке написан не генератором: перенесите его задачи в project.programs '
                           'спецификации и уберите файл')
    written, removed = [], []
    for name in sorted(os.listdir(out_dir)):  # a file of the generator the new run does not make (FB_Pulse no longer needed)
        if name.endswith('.st') and name not in got['files'] and name[:-3] in OWN:
            with open(os.path.join(out_dir, name), encoding='utf-8-sig') as f:
                if f.readline().strip() == MARK:
                    os.remove(os.path.join(out_dir, name))
                    removed.append(name)
    for name, text in sorted(got['files'].items()):
        with open(os.path.join(out_dir, name), 'w', encoding='utf-8', newline='\n') as f:
            f.write(text)
        written.append(name)
    with open(meta_path, 'w', encoding='utf-8', newline='\n') as f:
        json.dump(got['project'], f, ensure_ascii=False, indent=1)
        f.write('\n')
    written.append('project.json')
    return {**got['report'], 'out_dir': out_dir, 'written': written, 'removed': removed, 'io_map': got['io_map']}
