"""How the panel talks to the controller: told from the models named in the terms of reference.

The choice is made once, before the program is written, because it shapes the program: with Modbus every variable of
the panel needs an address in `%M`; with OPC UA the panel reads variables by name and addresses are not needed, but
the lists must be published (the pragma `{attribute 'symbol' := 'readwrite'}` and the symbol configuration).

The tables below hold only what the vendors publish about the series; a model that is not here is not guessed — the
answer says what to ask the engineer. Screen sizes are those of the series datasheets and are to be confirmed in
EasyBuilder Pro when the panel project is created.
"""
import re

OPCUA, MODBUS_TCP, MODBUS_RTU = 'opcua', 'modbus_tcp', 'modbus_rtu'
LINK_RU = {OPCUA: 'OPC UA', MODBUS_TCP: 'Modbus TCP', MODBUS_RTU: 'Modbus RTU (последовательная линия)'}

# Controllers: (pattern of the model, facts). `opcua` — the controller has an OPC UA server; `ethernet` — a port for
# Modbus TCP; `tool` — the program the project is made in.
CONTROLLERS = [
    (r'TM241CEC?\d{2}', {'family': 'Modicon M241 с Ethernet', 'ethernet': True, 'opcua': True, 'serial': True,
                         'tool': 'Machine Expert',
                         'note': 'Сервер OPC UA включается в Machine Expert: Ethernet_1 → OPC UA Server Configuration; '
                                 'нужна прошивка с сервером OPC UA. Modbus TCP (ведомый, порт 502) отдаёт область %MW.'}),
    (r'TM241C\d{2}', {'family': 'Modicon M241 без Ethernet', 'ethernet': False, 'opcua': False, 'serial': True,
                      'tool': 'Machine Expert',
                      'note': 'Портов Ethernet нет: связь только по последовательной линии (Modbus RTU, Serial Line 1/2).'}),
    (r'TM251MES[EC]', {'family': 'Modicon M251', 'ethernet': True, 'opcua': True, 'serial': True,
                       'tool': 'Machine Expert', 'note': 'Сервер OPC UA и Modbus TCP на портах Ethernet.'}),
    (r'TM262[LM]\d{2}', {'family': 'Modicon M262', 'ethernet': True, 'opcua': True, 'serial': True,
                         'tool': 'Machine Expert', 'note': 'Сервер OPC UA и Modbus TCP на портах Ethernet.'}),
    (r'TM258|LMC058', {'family': 'Modicon M258 / LMC058', 'ethernet': True, 'opcua': False, 'serial': True,
                       'tool': 'Machine Expert', 'note': 'Сервера OPC UA у серии в описании нет: Modbus TCP.'}),
    (r'TM221[CM]E?\d{2}', {'family': 'Modicon M221', 'ethernet': None, 'opcua': False, 'serial': True,
                           'tool': 'Machine Expert Basic',
                           'note': 'M221 программируется в Machine Expert Basic (не Machine Expert): этот инструмент '
                                   'его проекты не собирает. Связь — Modbus (TCP у моделей с «E» в названии, иначе RTU).'}),
]
# Panels Weintek: `opcua` — the panel has an OPC UA client; `file` — the EasyBuilder Pro project extension.
PANELS = [
    (r'CMT[- ]?(SVR|FHD|HDM|G0\d|CTRL)\w*', {'series': 'Weintek cMT (без экрана / шлюз)', 'opcua': True, 'ethernet': True,
                                              'file': '.cmtp'}),
    (r'CMT\d{4}\w*', {'series': 'Weintek cMT / cMT X', 'opcua': True, 'ethernet': True, 'file': '.cmtp'}),
    (r'MT6\d{3}IP', {'series': 'Weintek iP без Ethernet', 'opcua': False, 'ethernet': False, 'file': '.emtp'}),
    (r'MT8\d{3}(IE|IP|XE)\w*', {'series': 'Weintek iE / iP / XE', 'opcua': False, 'ethernet': True, 'file': '.emtp'}),
    (r'EMT\d{4}\w*', {'series': 'Weintek eMT', 'opcua': False, 'ethernet': True, 'file': '.emtp'}),
    (r'MT\d{4}\w*', {'series': 'Weintek MT', 'opcua': False, 'ethernet': None, 'file': '.emtp'}),
]
# Screen sizes of the models met most often (series datasheets); others — None: ask or read in EasyBuilder Pro.
RESOLUTIONS = {
    'MT8051IP': (480, 272), 'MT8071IP': (800, 480), 'MT8102IP': (1024, 600), 'MT8071IE': (800, 480),
    'MT8102IE': (1024, 600), 'MT6071IP': (800, 480), 'MT6103IP': (1024, 600),
    'CMT3072': (800, 480), 'CMT3072X': (1024, 600), 'CMT3072XH': (1024, 600), 'CMT2078X': (1024, 600),
    'CMT3090': (1024, 768), 'CMT3092X': (1024, 768), 'CMT3102X': (1024, 600), 'CMT3103': (1024, 600),
    'CMT2108X': (1024, 600), 'CMT3151': (1024, 768), 'CMT3152X': (1024, 768), 'CMT3162X': (1920, 1080),
}
_CONTROLLER_WORD = re.compile(r'\b(TM\d{3}[A-Z]{1,4}\d{0,2}[A-Z]{0,3}|LMC058\w*)\b', re.I)
_PANEL_WORD = re.compile(r'\b(cMT[- ]?[A-Z0-9]{3,8}|eMT\d{4}\w*|MT\d{4}\w{0,3})\b', re.I)
_WISH = [(OPCUA, re.compile(r'OPC[\s\-_]*UA', re.I)),
         (MODBUS_RTU, re.compile(r'Modbus[\s\-_]*RTU|RS[\s\-]*485|RS[\s\-]*232', re.I)),
         (MODBUS_TCP, re.compile(r'Modbus[\s\-_/]*TCP', re.I)),
         ('modbus', re.compile(r'Modbus|Модбас|Модбус', re.I))]


def _norm(model: str) -> str:
    return re.sub(r'[\s\-_]+', '', str(model or '')).upper()


def controller(model: str) -> dict:
    """Facts about a controller model; {'known': False} when the series is not in the table."""
    key = _norm(model)
    for pattern, facts in CONTROLLERS:
        if re.match(pattern, key):
            return dict(facts, model=str(model).strip(), known=True)
    return {'model': str(model or '').strip(), 'known': False}


def panel(model: str) -> dict:
    key = _norm(model)
    for pattern, facts in PANELS:
        if re.match(pattern, key):
            size = RESOLUTIONS.get(key)
            return dict(facts, model=str(model).strip(), known=True, resolution=list(size) if size else None)
    return {'model': str(model or '').strip(), 'known': False, 'resolution': None}


def scan(text: str) -> dict:
    """Models and words about the link met in a text (the terms of reference, a specification)."""
    text = str(text or '')
    seen_c, seen_p = {}, {}
    for m in _CONTROLLER_WORD.finditer(text):
        seen_c.setdefault(_norm(m.group(1)), m.group(1))
    for m in _PANEL_WORD.finditer(text):
        if panel(m.group(1))['known']:
            seen_p.setdefault(_norm(m.group(1)), m.group(1))
    wishes = []
    for kind, rx in _WISH:
        if rx.search(text) and not (kind == 'modbus' and any(w.startswith('modbus') for w in wishes)):
            wishes.append(kind)
    return {'controllers': list(seen_c.values()), 'panels': list(seen_p.values()), 'wishes': wishes}


def decide(controller_model: str = '', panel_model: str = '', wish: str = '', text: str = '') -> dict:
    """The link for the project. -> {'link', 'link_ru', 'sure', 'why': [...], 'ask': [...], 'controller', 'panel',
    'found', 'project_json', 'next': [...]}. `link` is '' when it cannot be told; `ask` then says what to find out."""
    found = scan(text) if text else {'controllers': [], 'panels': [], 'wishes': []}
    ask, why = [], []
    if not controller_model and len(found['controllers']) == 1:
        controller_model = found['controllers'][0]
    elif not controller_model and len(found['controllers']) > 1:
        ask.append('В тексте несколько моделей контроллера: ' + ', '.join(found['controllers']) + ' — какая в этом проекте?')
    if not panel_model and len(found['panels']) == 1:
        panel_model = found['panels'][0]
    elif not panel_model and len(found['panels']) > 1:
        ask.append('В тексте несколько моделей панели: ' + ', '.join(found['panels']) + ' — какая в этом проекте?')
    c, p = controller(controller_model), panel(panel_model)
    wish = {'opc ua': OPCUA, 'opc': OPCUA, 'opcua': OPCUA, 'modbus tcp': MODBUS_TCP, 'modbus_tcp': MODBUS_TCP,
            'tcp': MODBUS_TCP, 'modbus rtu': MODBUS_RTU, 'modbus_rtu': MODBUS_RTU, 'rtu': MODBUS_RTU,
            'modbus': 'modbus'}.get(str(wish or '').strip().lower(), '')
    if not wish:
        named = [w for w in found['wishes']]
        if OPCUA in named and any(w != OPCUA for w in named):
            ask.append('В тексте названы и OPC UA, и Modbus: какая связь между панелью и контроллером?')
        elif named:
            wish = named[0]
            why.append(f'в тексте названа связь: {LINK_RU.get(wish, "Modbus")}')
    if not c['model']:
        ask.append('Модель контроллера не названа (например TM241CE40R).')
    elif not c['known']:
        ask.append(f'Контроллер {c["model"]} не из таблицы инструмента: есть ли у него Ethernet и сервер OPC UA?')
    if not p['model']:
        ask.append('Модель панели не названа (например cMT3072X или MT8071iE).')
    elif not p['known']:
        ask.append(f'Панель {p["model"]} не из таблицы инструмента: умеет ли она OPC UA (клиент) и есть ли Ethernet?')
    link = ''
    both = c.get('known') and p.get('known')
    can_opcua = bool(c.get('opcua') and p.get('opcua'))
    can_tcp = bool(c.get('ethernet') and p.get('ethernet'))
    if both:
        if wish == OPCUA:
            if can_opcua:
                link = OPCUA
            else:
                who = [n for n, x in (('контроллер ' + c['model'], c), ('панель ' + p['model'], p)) if not x.get('opcua')]
                ask.append('Названа связь OPC UA, но её не умеет: ' + ', '.join(who) + '. Менять оборудование или '
                           'делать Modbus?')
        elif wish == MODBUS_RTU:
            link = MODBUS_RTU
        elif wish in (MODBUS_TCP, 'modbus'):
            if can_tcp:
                link = MODBUS_TCP
            elif wish == 'modbus':
                link = MODBUS_RTU
                why.append('Ethernet есть не у обоих устройств — Modbus по последовательной линии')
            else:
                ask.append('Назван Modbus TCP, но Ethernet есть не у обоих устройств.')
        elif not ask:
            if can_opcua:
                link = OPCUA
                why.append(f'{c["model"]} — сервер OPC UA, {p["model"]} — клиент OPC UA: переменные по именам, без адресов')
            elif can_tcp:
                link = MODBUS_TCP
                why.append('OPC UA умеют не оба устройства; Ethernet есть у обоих — Modbus TCP, адреса %M')
            elif c.get('ethernet') is None or p.get('ethernet') is None:
                ask.append('Есть ли Ethernet у контроллера и панели? От этого зависит Modbus TCP или RTU.')
            else:
                link = MODBUS_RTU
                why.append('Ethernet есть не у обоих устройств — Modbus RTU по последовательной линии')
    if link and wish and not why:
        why.append(f'связь задана: {LINK_RU[link]}')
    if c.get('known') and c.get('tool') != 'Machine Expert':
        ask.append(c['note'])
    nxt = []
    if link == OPCUA:
        nxt = ['В project.json: "link": {"kind": "opcua", "lists": ["GVL"]} — списки, которые видит панель.',
               "В начале каждого такого списка — строка {attribute 'symbol' := 'readwrite'} перед VAR_GLOBAL.",
               'Адреса %M переменным панели не нужны. Теги для панели: plc_hmi_tags (link берётся из project.json).',
               'В Machine Expert: machine-expert me_create сам добавит «Конфигурацию символов» с поддержкой OPC UA; '
               'сервер OPC UA контроллера включает инженер (Ethernet_1 → OPC UA Server Configuration).']
    elif link in (MODBUS_TCP, MODBUS_RTU):
        nxt = [f'В project.json: "link": {{"kind": "{link}"}}.',
               'Каждой переменной панели — адрес в %M (биты %MX, слова %MW, REAL и DINT — %MD), подряд и без '
               'пересечений: plc_addresses показывает свободное место.',
               'Теги для панели: plc_hmi_tags — регистры и адреса в записи Weintek.']
        nxt.append('Панель — ведущий Modbus TCP, контроллер — ведомый (порт 502, область %MW).' if link == MODBUS_TCP else
                   'В Machine Expert на Serial Line контроллера ставится Modbus Manager (ведомый): адрес, скорость и '
                   'чётность — как в панели.')
    return {'link': link, 'link_ru': LINK_RU.get(link, ''), 'sure': bool(link) and not ask, 'why': why, 'ask': ask,
            'controller': c, 'panel': p, 'found': found,
            'project_json': {'link': dict({'kind': link}, **({'lists': ['GVL']} if link == OPCUA else {}))} if link else None,
            'next': nxt}
