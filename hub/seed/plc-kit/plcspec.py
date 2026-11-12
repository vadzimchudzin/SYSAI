"""The specification of a project — `_work/spec/project.spec.json`: every mechanism and every signal of the object in
one compact file (10–20 KB for a whole line). It is the contract between the model, which reads the terms of reference
and the schemes once, and the code, which checks it and builds from it; the program, the channels and the screens are
made from this file, not from what was said in a conversation.

    {"project":    {"name", "controller", "panel", "link": "opcua" | "modbus_tcp" | "modbus_rtu", "template"},
     "expected":   {"mechanisms": 12, "signals": 96},      counted on the scheme itself, before anything is written out
     "types":      {"noria": {"title": "Нория", "in": ["speed", "belt"], "out": ["run"], "optional": ["vfd_ready"],
                              "faults": ["belt"], "pulse": ["speed"]}},      what the generator makes protections of
     "mechanisms": [{"id": "N1", "type": "noria", "title": "Нория 1",
                     "in": {"speed": "DI.I0", "belt": "DI.I1"}, "out": {"run": "DQ.Q0"},
                     "params": {"start_delay_s": 5}, "interlocks": {"requires_running": ["C1"], "stops": ["S2"]},
                     "src": "Э3 лист 7; ТЗ п.4.2"}],
     "signals":    [{"id": "EStop", "title": "Аварийный стоп", "dir": "in", "channel": "!DI.I5", "role": "estop", "src": "Э3 лист 2"}],
     "routes":     [{"id": "R1", "title": "Приём в силос 1", "mechanisms": ["N1", "C1"]}]}        from the start of the flow

A channel is «Устройство.Канал» as `plc_io` names them («DI.I0», «Module_5.I12», «DQ.Q3»), «!» before it — a normally
closed contact; "" — the signal has no
channel (it comes over the network or is not wired yet — the check says so).

`check` answers what a compiler never will: is anything of the scheme missing (the counts), does every mechanism have
all the signals of its kind, does one channel carry two signals, does an input sit on an output, do the interlocks
name mechanisms that exist — and, with the template, do the channels exist in the controller at all."""
import json
import os
import re

LINKS = ('opcua', 'modbus_tcp', 'modbus_rtu')
ID = re.compile(r'[A-Za-z_][A-Za-z0-9_]{0,39}$')
CHANNEL = re.compile(r'([A-Za-z_][\w]*)\.(%?[A-Za-z]{1,3}\d+(?:\.\d+)?)$')
FILE = os.path.join('_work', 'spec', 'project.spec.json')
MAX_BYTES = 2_000_000

SKELETON = {
    'project': {'name': '', 'controller': '', 'panel': '', 'link': 'opcua', 'template': ''},
    'expected': {'mechanisms': 0, 'signals': 0},
    'types': {
        'noria': {'title': 'Нория', 'in': ['speed', 'belt', 'local_stop'], 'out': ['run'], 'optional': ['vfd_ready', 'plug'],
                  'faults': ['belt', 'local_stop', 'plug'], 'pulse': ['speed']},
        'conveyor': {'title': 'Конвейер', 'in': ['speed', 'local_stop'], 'out': ['run'], 'optional': ['plug'],
                     'faults': ['local_stop', 'plug'], 'pulse': ['speed']},
    },
    'mechanisms': [],
    'signals': [],
    'routes': [],
}


class SpecError(Exception):
    pass


def path_of(project_dir: str) -> str:
    return os.path.join(project_dir, FILE)


def new(out_file: str, name: str = '', overwrite: bool = False) -> dict:
    """An empty specification with two kinds of mechanisms as an example of how a kind is written."""
    if os.path.exists(out_file) and not overwrite:
        raise SpecError(f'файл уже есть: {out_file} (overwrite — заменить)')
    data = json.loads(json.dumps(SKELETON))
    data['project']['name'] = str(name or '')[:120]
    os.makedirs(os.path.dirname(os.path.abspath(out_file)), exist_ok=True)
    with open(out_file, 'w', encoding='utf-8', newline='\n') as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
        f.write('\n')
    return data


def load(file: str) -> dict:
    try:
        if os.path.getsize(file) > MAX_BYTES:
            raise SpecError('спецификация больше 2 МБ: это не она')
        with open(file, encoding='utf-8-sig') as f:
            data = json.load(f)
    except OSError as exc:
        raise SpecError(f'спецификация не читается: {exc}') from None
    except ValueError as exc:
        raise SpecError(f'спецификация — не JSON: {exc}') from None
    if not isinstance(data, dict):
        raise SpecError('спецификация — объект JSON с полями project, types, mechanisms, signals')
    return data


def _role(item) -> str:
    """The name of a signal of a kind: «level:INT» (a signal that is a number) → «level»."""
    return str(item).partition(':')[0].strip()


def _direction(channel: str) -> str:
    """'in' / 'out' / '' by the name of a channel: I0, IW3, %IX0.1 — an input, Q5, QW1 — an output."""
    m = CHANNEL.match(channel)
    name = m.group(2).lstrip('%').upper() if m else ''
    return 'in' if name.startswith('I') else 'out' if name.startswith('Q') else ''


def signals(data: dict) -> list:
    """Every signal of the specification as a row: {owner, role, dir, channel, title, src} — of the mechanisms first."""
    rows = []
    for m in data.get('mechanisms') or []:
        if not isinstance(m, dict):
            continue
        for side in ('in', 'out'):
            part = m.get(side)
            for role, channel in (part.items() if isinstance(part, dict) else ()):
                rows.append({'owner': str(m.get('id') or ''), 'role': str(role), 'dir': side, 'channel': str(channel or ''),
                             'title': str(m.get('title') or ''), 'src': str(m.get('src') or '')})
    for s in data.get('signals') or []:
        if isinstance(s, dict):
            rows.append({'owner': '', 'role': str(s.get('id') or ''), 'dir': str(s.get('dir') or ''),
                         'channel': str(s.get('channel') or ''), 'title': str(s.get('title') or ''), 'src': str(s.get('src') or '')})
    return rows


def check(data: dict, channels=None) -> dict:
    """-> {'summary': {...}, 'issues': [{level, where, text}]}. `channels` — the rows of `plcproj.io` of the template
    (or of the built project): then every channel of the specification is looked up in the controller."""
    issues = []

    def add(level, where, text):
        issues.append({'level': level, 'where': where, 'text': text})

    project = data.get('project') if isinstance(data.get('project'), dict) else {}
    if not isinstance(data.get('project'), dict):
        add('error', 'project', 'нет раздела project: {name, controller, panel, link, template}')
    for key in ('name', 'controller'):
        if not str(project.get(key) or '').strip():
            add('error', 'project', f'не заполнено поле {key}')
    link = str(project.get('link') or '').strip().lower()
    if link and link not in LINKS:
        add('error', 'project', f'link: {link} — нужно одно из {", ".join(LINKS)}')
    elif not link:
        add('warning', 'project', 'связь с панелью (link) не решена: opcua, modbus_tcp или modbus_rtu — plc_link подскажет')

    types = data.get('types') if isinstance(data.get('types'), dict) else {}
    if not isinstance(data.get('types'), dict):
        add('error', 'types', 'нет раздела types: виды механизмов и сигналы, которые у каждого вида должны быть')
    for name, t in types.items():
        if not isinstance(t, dict):
            add('error', f'types.{name}', 'вид механизма — объект {title, in, out, optional}')
            continue
        for side in ('in', 'out', 'optional', 'faults', 'pulse'):
            if not isinstance(t.get(side, []), list) or not all(isinstance(x, str) for x in t.get(side, [])):
                add('error', f'types.{name}', f'{side} — список имён сигналов')
        roles = {_role(x) for side in ('in', 'out', 'optional') for x in (t.get(side) if isinstance(t.get(side), list) else [])}
        for side in ('faults', 'pulse'):   # what the generator makes protections of: signals the kind really has
            for x in (t.get(side) if isinstance(t.get(side), list) else []):
                if isinstance(x, str) and x.lstrip('!') not in roles:
                    add('error', f'types.{name}', f'{side}: сигнала «{x.lstrip("!")}» у вида нет (in, out, optional)')
        if 'pins' in t and not isinstance(t.get('pins'), dict):
            add('error', f'types.{name}', 'pins — объект «вход или выход блока: сигнал»')
        if not (t.get('in') or t.get('out')):
            add('warning', f'types.{name}', 'у вида нет ни одного обязательного сигнала: проверка полноты его не видит')

    mechanisms = data.get('mechanisms') if isinstance(data.get('mechanisms'), list) else []
    if not isinstance(data.get('mechanisms'), list):
        add('error', 'mechanisms', 'нет раздела mechanisms (список механизмов)')
    ids, by_type = {}, {}
    for n, m in enumerate(mechanisms, 1):
        if not isinstance(m, dict):
            add('error', f'mechanisms[{n}]', 'механизм — объект {id, type, title, in, out, src}')
            continue
        mid = str(m.get('id') or '')
        where = mid or f'mechanisms[{n}]'
        if not ID.match(mid):
            add('error', where, 'id — латиница, цифры и _, не с цифры (он станет именем экземпляра в программе)')
        elif mid.lower() in ids:
            add('error', where, f'id повторяется (механизмы {ids[mid.lower()]} и {n} по порядку)')
        ids.setdefault(mid.lower(), n)
        if not str(m.get('title') or '').strip():
            add('warning', where, 'нет названия (title): его увидит оператор на экране и в руководстве')
        if not str(m.get('src') or '').strip():
            add('warning', where, 'не сказано, откуда взят (src: файл, лист, пункт ТЗ) — инженеру нечем проверить')
        kind = str(m.get('type') or '')
        by_type[kind] = by_type.get(kind, 0) + 1
        t = types.get(kind)
        if not isinstance(t, dict):
            add('error', where, f'вид «{kind}» не описан в types' if kind else 'не назван вид механизма (type)')
            continue
        for side in ('in', 'out'):
            have = m.get(side) if isinstance(m.get(side), dict) else {}
            if side in m and not isinstance(m.get(side), dict):
                add('error', where, f'{side} — объект «сигнал: канал»')
            missing = [_role(r) for r in t.get(side, []) if isinstance(r, str) and _role(r) not in have]
            if missing:
                add('error', where, f'у вида «{kind}» должны быть сигналы {side}: {", ".join(missing)} — их нет. Нет на схеме — '
                                    'спросите инженера; не нужен этому виду — перенесите в optional')
            known = {_role(r) for r in list(t.get(side, [])) + list(t.get('optional', [])) if isinstance(r, str)}
            extra = [r for r in have if r not in known]
            if extra:
                add('warning', where, f'сигналы {side}, которых у вида «{kind}» нет: {", ".join(extra)} — допишите в types '
                                      '(in/out или optional) или уберите')
    for n, m in enumerate(mechanisms, 1):
        locks = m.get('interlocks') if isinstance(m, dict) and isinstance(m.get('interlocks'), dict) else {}
        for rule, names in locks.items():
            for other in (names if isinstance(names, list) else [names]):
                if rule not in ('requires_running', 'stops'):
                    add('warning', str(m.get('id') or f'mechanisms[{n}]'), f'блокировка «{rule}» генератору неизвестна: он знает '
                                                                         'requires_running (работает, пока работают они) и '
                                                                         'stops (они работают, пока работает он)')
                    break
                if str(other).lower() not in ids:
                    add('error', str(m.get('id') or f'mechanisms[{n}]'), f'блокировка {rule}: механизма {other} в спецификации нет')
                elif str(other).lower() == str(m.get('id') or '').lower():
                    add('error', str(m.get('id')), f'блокировка {rule}: механизм ссылается сам на себя')

    route_ids = set()
    for n, r in enumerate(data.get('routes') if isinstance(data.get('routes'), list) else [], 1):
        where = str(r.get('id') or f'routes[{n}]') if isinstance(r, dict) else f'routes[{n}]'
        if not isinstance(r, dict) or not ID.match(str(r.get('id') or '')):
            add('error', where, 'маршрут — объект {id латиницей, title, mechanisms: [от начала потока к концу]}')
            continue
        if str(r['id']).lower() in route_ids or str(r['id']).lower() in ids:
            add('error', where, 'id маршрута повторяется или занят механизмом')
        route_ids.add(str(r['id']).lower())
        chain = r.get('mechanisms') if isinstance(r.get('mechanisms'), list) else []
        if not chain:
            add('error', where, 'в маршруте нет механизмов (mechanisms: от начала потока к концу)')
        for other in chain:
            if str(other).lower() not in ids:
                add('error', where, f'механизма {other} в спецификации нет')
        if len({str(x).lower() for x in chain}) != len(chain):
            add('error', where, 'механизм назван в маршруте дважды')

    seen = set()
    for n, s in enumerate(data.get('signals') if isinstance(data.get('signals'), list) else [], 1):
        if not isinstance(s, dict):
            add('error', f'signals[{n}]', 'сигнал — объект {id, title, dir, channel, src}')
            continue
        sid = str(s.get('id') or '')
        where = sid or f'signals[{n}]'
        if not ID.match(sid):
            add('error', where, 'id — латиница, цифры и _, не с цифры')
        elif sid.lower() in seen or sid.lower() in ids:
            add('error', where, 'id повторяется')
        seen.add(sid.lower())
        if s.get('dir') not in ('in', 'out'):
            add('error', where, 'dir — in (вход контроллера) или out (выход)')

    rows = signals(data)
    used = {}
    known = None
    if channels is not None:
        known = {f"{r['device']}.{r['channel']}".lower(): r for r in channels}
    no_channel = 0
    for r in rows:
        where = f"{r['owner']}.{r['role']}" if r['owner'] else r['role']
        ch = r['channel'].strip()
        if not ch:
            no_channel += 1
            continue
        ch = ch.lstrip('!').strip()   # «!DI.I11» — a normally closed contact: the generator puts NOT on it
        if not CHANNEL.match(ch):
            add('error', where, f'канал «{ch}» — нужно «Устройство.Канал», как в plc_io: DI.I0, Module_5.I12, DQ.Q3 '
                                '(«!» впереди — нормально замкнутый контакт)')
            continue
        used.setdefault(ch.lower(), []).append(where)
        way = _direction(ch)
        if way and r['dir'] in ('in', 'out') and way != r['dir']:
            add('error', where, f"{'вход' if r['dir'] == 'in' else 'выход'} стоит на канале {ch} — это "
                                f"{'выход' if way == 'out' else 'вход'} контроллера")
        if known is not None:
            row = known.get(ch.lower())
            if row is None:
                add('error', where, f'канала {ch} в контроллере шаблона нет (plc_io покажет, какие есть)')
            elif row.get('direction') in ('input', 'output') and r['dir'] in ('in', 'out') \
                    and (row['direction'] == 'input') != (r['dir'] == 'in'):
                add('error', where, f"канал {ch} в шаблоне — {'вход' if row['direction'] == 'input' else 'выход'}, а сигнал — "
                                    f"{'вход' if r['dir'] == 'in' else 'выход'}")
    for ch, owners in used.items():
        if len(owners) > 1:
            add('error', 'каналы', f'на канале {ch} два сигнала: {", ".join(owners[:6])}')
    if no_channel:
        add('warning', 'каналы', f'сигналов без канала: {no_channel} — они придут по сети или ещё не разведены; проверьте по схеме')

    expected = data.get('expected') if isinstance(data.get('expected'), dict) else {}
    counted = {'mechanisms': len(mechanisms), 'signals': len(rows)}
    for key, ru in (('mechanisms', 'механизмов'), ('signals', 'сигналов')):
        want = expected.get(key)
        if not isinstance(want, int) or isinstance(want, bool) or want <= 0:
            add('error', 'expected', f'не сказано, сколько {ru} на схеме (expected.{key}): пересчитайте по схеме, а не по '
                                     'тому, что уже выписано — иначе пропущенное не найти')
        elif want != counted[key]:
            add('error', 'expected', f'на схеме {ru}: {want}, в спецификации: {counted[key]} — '
                                     f"{'не хватает' if want > counted[key] else 'лишних'} {abs(want - counted[key])}")
    free = None
    if known is not None:
        free = sum(1 for key in known if key not in used)
    levels = {k: sum(1 for i in issues if i['level'] == k) for k in ('error', 'warning')}
    order = {'error': 0, 'warning': 1}
    issues.sort(key=lambda i: order.get(i['level'], 2))
    summary = {'mechanisms': counted['mechanisms'], 'signals': counted['signals'],
               'inputs': sum(1 for r in rows if r['dir'] == 'in'), 'outputs': sum(1 for r in rows if r['dir'] == 'out'),
               'by_type': dict(sorted(by_type.items(), key=lambda kv: -kv[1])), 'without_channel': no_channel,
               'errors': levels['error'], 'warnings': levels['warning'], 'complete': levels['error'] == 0}
    if free is not None:
        summary['template_channels'] = len(known)
        summary['template_channels_free'] = free
    return {'summary': summary, 'issues': issues}


def io_changes(data: dict, prefix: str = '') -> list:
    """What `plc_io_map` needs to put the signals of the specification on their channels: [{device, channel, variable}],
    the variable is «<id механизма>_<сигнал>» (or the id of a signal of its own) — the names the program declares."""
    out = []
    for r in signals(data):
        m = CHANNEL.match(r['channel'].strip())
        if not m:
            continue
        name = f"{r['owner']}_{r['role']}" if r['owner'] else r['role']
        out.append({'device': m.group(1), 'channel': m.group(2), 'variable': prefix + name})
    return out
