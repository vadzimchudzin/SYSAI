"""Reading drawings in PDF without a model: the words of every sheet with their places, the designators of devices
(10M1, 10KM1, 10SQ2 …), the modules of the controller and their channels, the tables of the specification and of the
technological scheme — as compact «cards of sheets» cached by the sha256 of the file.

What it is for: a scheme Э3 of a line is 40–90 sheets A3. Shown to a model as pictures it costs hundreds of thousands
of tokens and small print is misread; its text layer, read here, is a few kilobytes a sheet and exact. The model then
asks «where is 10SQ2», «what stands on the channels of module A1.1», «which mechanisms are on the scheme» and gets
rows with the file, the sheet and the place — and looks at a picture only where the text does not answer.

Only PDF with a text layer (what a CAD exports); a scan has no words — `sheets` says so, and such sheets go to OCR or to
a vision model, cut to the place in question.

Coordinates: points (1/72 inch) from the top left corner of the sheet. `zone` — the column 0–9 of the frame of an A3
sheet (the numbers along its top edge), as people call a place on a scheme."""
import hashlib
import json
import os
import re

MAX_PAGES = 400
CACHE_VERSION = 8
GAP = 0.45  # letters of one word stand closer than this part of their height
# A designator of the scheme: optional number of the mechanism, letters of the kind, a number — 10KM1, 1SF3, QF2, HL1.
# Kinds by GOST 2.710 as the schemes of the customer use them.
KINDS = {
    'M': 'двигатель', 'KM': 'контактор', 'QF': 'автомат', 'SF': 'автомат цепей управления', 'K': 'реле', 'KK': 'тепловое реле',
    'SQ': 'концевой выключатель / датчик положения', 'SR': 'датчик скорости', 'SL': 'датчик уровня', 'SP': 'датчик давления',
    'SK': 'датчик температуры', 'SB': 'кнопка', 'SA': 'переключатель', 'S': 'выключатель', 'HL': 'лампа', 'HA': 'сирена',
    'UZ': 'преобразователь частоты', 'U': 'блок питания / преобразователь', 'YA': 'электромагнит', 'Y': 'клапан',
    'BK': 'датчик температуры', 'BP': 'датчик давления', 'BL': 'датчик уровня', 'BR': 'датчик скорости', 'B': 'датчик',
    'TV': 'трансформатор', 'T': 'трансформатор', 'FU': 'предохранитель', 'F': 'защита', 'R': 'резистор', 'XT': 'клеммник',
    'X': 'клеммник', 'XS': 'розетка', 'XP': 'вилка', 'A': 'устройство', 'PA': 'амперметр', 'PV': 'вольтметр', 'EL': 'освещение',
    'EK': 'нагреватель', 'QS': 'рубильник', 'Q': 'выключатель силовой', 'KT': 'реле времени', 'KV': 'реле напряжения',
}
_DESIGNATOR = re.compile(r'^(\d{0,3})([A-Z]{1,2})(\d{1,3})$')
# what Latin and Cyrillic write alike: a designator typed with a Cyrillic «К» or «М» is the same device
_LOOKALIKE = str.maketrans('АВЕКМНОРСТУХ', 'ABEKMHOPCTYX')
_MODULE = re.compile(r'^(TM\d{3}[A-Z0-9]{2,12}|TM[23][A-Z]{2,4}\d{1,2}[A-Z0-9]{0,6}|TMC\d[A-Z0-9]{2,6}|BMX[A-Z0-9]{4,12})$')
_CHANNEL = re.compile(r'^%?([IQ])(\d{1,2})(?:\.(\d{1,2}))?$')
# controllers that name their channels X… (inputs) and Y… (outputs): Haiwell, Mitsubishi, Delta. Latin letters only —
# a Cyrillic «Х2» is a terminal block
_CHANNEL_XY = re.compile(r'^([XY])(\d{1,3})$')
_WORD = re.compile(r'[0-9A-Za-zА-Яа-яЁё]{2,}')


class DrawError(Exception):
    pass


def fix_text(text: str) -> str:
    """A PDF from some CAD exports keeps Cyrillic as the two bytes of UTF-8 read as one UTF-16 unit («Н», bytes D0 9D,
    comes out as U+D09D «킝»). Such pairs are put back; everything else is left as it is."""
    text = text.replace('\u8496', '№')  # «№» (three bytes of UTF-8) loses its first byte the same way
    if not any('퀀' <= ch <= '폿' for ch in text):
        return text
    out = []
    for ch in text:
        code = ord(ch)
        if 0xD080 <= code <= 0xD3BF and 0x80 <= (code & 0xFF) <= 0xBF:
            try:
                out.append(bytes((code >> 8, code & 0xFF)).decode('utf-8'))
                continue
            except UnicodeDecodeError:
                pass
        out.append(ch)
    return ''.join(out)


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def _pdfium():
    try:
        import pypdfium2
    except ImportError:
        raise DrawError('нет библиотеки pypdfium2: инструмент ставит её сам (requirements.txt) — обновите инструмент '
                        'в Хабе или выполните pip install pypdfium2') from None
    return pypdfium2


def _chars(tp, height: float) -> list:
    """Every visible character of a sheet with its box: [(ch, x, y_top, w, h)]. A CAD draws a text letter by letter, so
    the words are put together here (`_words`), not taken from the runs of the file."""
    out = []
    spaced = False  # the file itself has a space before this character: a new word even when it stands close
    for i in range(tp.count_chars()):
        ch = fix_text(tp.get_text_range(i, 1))
        if not ch or ch.isspace() or ch in '\ufffe\uffff' or ord(ch[0]) < 32:
            spaced = True
            continue
        left, bottom, right, top = tp.get_charbox(i)
        if right <= left and top <= bottom:
            continue
        out.append((ch, left, height - top, right - left, top - bottom, height - bottom, spaced, i))
        spaced = False
    return out


def _words(chars: list) -> list:
    """Characters standing on one line next to each other are a word: [{t, x, y, w, h}]."""
    rows = []  # [the middle of the line, its height, its characters]
    for c in sorted(chars, key=lambda c: (c[2] + c[4] / 2, c[1])):  # by the middle: «р» hangs below the line, «Н» does not
        mid = c[2] + c[4] / 2
        if rows and abs(rows[-1][0] - mid) <= 0.45 * max(rows[-1][1], c[4], 3.0):
            rows[-1][2].append(c)
            rows[-1][1] = max(rows[-1][1], c[4])
            rows[-1][0] += (mid - rows[-1][0]) / len(rows[-1][2])  # the middle of the line follows its letters
        else:
            rows.append([mid, c[4], [c]])
    words = []
    for _, _, row in rows:
        row.sort(key=lambda c: c[1])
        cur = [row[0]]
        for c in row[1:]:
            last = cur[-1]
            size = max(last[4], c[4], 3.0)
            split = c[6] and 0 < c[7] - last[7] <= 3  # a space of the file between these two
            if c[1] - (last[1] + last[3]) <= size * GAP and c[1] >= last[1] - 0.5 and not split:
                cur.append(c)
            else:
                words.append(cur)
                cur = [c]
        words.append(cur)
    out = []
    for w in words:
        x0, x1 = w[0][1], max(c[1] + c[3] for c in w)
        top = min(c[2] for c in w)
        out.append({'t': ''.join(c[0] for c in w), 'x': round(x0, 1), 'y': round(top, 1), 'w': round(x1 - x0, 1),
                    'h': round(max(c[5] for c in w) - top, 1)})
    return out


def read_pages(path: str) -> list:
    """[{page, width, height, words: [{t, x, y, w, h}]}] — every run of text of every sheet with its box (top-left
    origin, points), Cyrillic repaired."""
    pdfium = _pdfium()
    try:
        doc = pdfium.PdfDocument(path)
    except Exception as exc:  # noqa: BLE001 — pdfium raises its own classes
        raise DrawError(f'PDF не читается: {exc}') from None
    pages = []
    try:
        if len(doc) > MAX_PAGES:
            raise DrawError(f'в файле {len(doc)} листов — больше {MAX_PAGES}: разбейте его')
        for n in range(len(doc)):
            page = doc[n]
            width, height = page.get_size()
            tp = page.get_textpage()
            words = _words(_chars(tp, height))
            pages.append({'page': n + 1, 'width': round(width), 'height': round(height), 'words': words})
            tp.close()
            page.close()
    finally:
        doc.close()
    return pages


def zone(x: float, width: float) -> int:
    """The column 0–9 of the frame of the sheet the place is in."""
    return max(0, min(9, int(10 * x / width))) if width else 0


def designator(text: str):
    """('10', 'KM', '1', 'контактор') for «10KM1» (Cyrillic look-alikes too); None — not a designator."""
    t = text.strip().upper().translate(_LOOKALIKE)
    m = _DESIGNATOR.match(t)
    if not m:
        return None
    kind = m.group(2)
    if kind not in KINDS:
        return None
    return m.group(1), kind, m.group(3), KINDS[kind]


def _rows(words: list, tolerance: float = 4.0) -> list:
    """Words gathered into lines of text: the same height on the sheet, left to right."""
    rows = []
    for w in sorted(words, key=lambda w: (w['y'], w['x'])):
        if rows and abs(rows[-1][0] - w['y']) <= tolerance:
            rows[-1][1].append(w)
        else:
            rows.append([w['y'], [w]])
    return [sorted(r, key=lambda w: w['x']) for _, r in rows]


def lines_of(page: dict, gap: float = 14.0) -> list:
    """The sheet as lines of text: [{y, x, text}] — words of one line closer than `gap` are one phrase."""
    out = []
    for row in _rows(page['words']):
        phrase = [row[0]]
        for w in row[1:]:
            last = phrase[-1]
            if w['x'] - (last['x'] + last['w']) <= gap:
                phrase.append(w)
            else:
                out.append(phrase)
                phrase = [w]
        out.append(phrase)
    return [{'y': round(p[0]['y']), 'x': round(p[0]['x']), 'w': round(p[-1]['x'] + p[-1]['w'] - p[0]['x']),
             'text': ' '.join(w['t'] for w in p)} for p in out]


def channel_style(pages: list) -> str:
    """'iq' — the channels of the scheme are I… / Q… (Schneider, Siemens); 'xy' — X… / Y… (Haiwell, Mitsubishi, Delta).
    Decided for the whole file: a scheme uses one way, and «Y3» on a Schneider scheme is a terminal of a module."""
    iq = xy = 0
    for page in pages:
        for w in page['words']:
            for piece in re.split(r'[\s,;()]+', w['t']):
                word = piece.strip().upper()
                if _CHANNEL.match(word):
                    iq += 1
                elif _CHANNEL_XY.match(piece.strip()):
                    xy += 1
    return 'xy' if xy >= 10 and iq < xy / 4 else 'iq'


def card(page: dict, style: str = 'iq') -> dict:
    """What a sheet holds: its designators, the modules of the controller, the channels and how much text there is."""
    width = page['width']
    devices, modules, channels = {}, [], []
    cyr = 0
    for w in page['words']:
        t = w['t']
        cyr += sum(1 for ch in t if 'а' <= ch.lower() <= 'я' or ch in 'ёЁ')
        for piece in re.split(r'[\s,;()]+', t):
            word = piece.strip().upper()
            m = _CHANNEL_XY.match(piece.strip()) if style == 'xy' else _CHANNEL.match(word)
            d = None if m else designator(piece)   # «Q3» next to a module is its output, not a switch
            if m:
                channels.append({'name': word.lstrip('%'), 'dir': 'in' if m.group(1) in ('I', 'X') else 'out',
                                 'x': round(w['x']), 'y': round(w['y'])})
            elif d:
                name = word.translate(_LOOKALIKE)
                devices.setdefault(name, {'name': name, 'mechanism': d[0], 'kind': d[1], 'what': d[3], 'x': round(w['x']),
                                          'y': round(w['y']), 'zone': zone(w['x'], width), 'count': 0})['count'] += 1
            elif _MODULE.match(word):
                modules.append({'name': word, 'x': round(w['x']), 'y': round(w['y'])})
    chars = sum(len(w['t']) for w in page['words'])
    return {'page': page['page'], 'size': [page['width'], page['height']], 'words': len(page['words']), 'chars': chars,
            'cyrillic': cyr, 'scan': chars < 40, 'devices': sorted(devices.values(), key=lambda d: (d['x'], d['y'])),
            'modules': modules, 'channels': channels}


# what may stand on a channel: the sensor or contact of an input, the relay, contactor or lamp of an output
_ON_INPUT = {'SQ', 'SR', 'SL', 'SP', 'SK', 'SB', 'SA', 'S', 'B', 'BK', 'BP', 'BL', 'BR', 'K', 'KM', 'KK', 'QF', 'SF', 'UZ', 'KV', 'KT'}
_ON_OUTPUT = {'K', 'KM', 'HL', 'HA', 'Y', 'YA', 'UZ', 'KT'}


def channel_map(page: dict, reach: float = 26.0, style: str = 'iq') -> list:
    """For a sheet with a module of the controller: every channel label with what is drawn in its column — the nearest
    sensor, button or contact (an input) or relay, contactor, lamp (an output) within `reach` points sideways, and the
    caption written in that column. A guess from the geometry: `distance` says how far the device is, the scheme
    itself is the judge."""
    c = card(page, style)
    phrases = [p for p in lines_of(page) if len(re.findall('[А-Яа-яЁё]', p['text'])) >= 3]
    # a sheet with inputs above and outputs below: a caption belongs to the half its channel is in
    ins = sorted(ch['y'] for ch in c['channels'] if ch['dir'] == 'in')
    outs = sorted(ch['y'] for ch in c['channels'] if ch['dir'] == 'out')
    border = (ins[len(ins) // 2] + outs[len(outs) // 2]) / 2 if ins and outs and abs(ins[len(ins) // 2] - outs[len(outs) // 2]) > 60 else None
    out = []
    for ch in c['channels']:
        fits = _ON_INPUT if ch['dir'] == 'in' else _ON_OUTPUT
        best = None
        for d in c['devices']:
            dx = abs(d['x'] - ch['x'])
            if dx > reach or d['kind'] not in fits:
                continue
            if not d['mechanism'] and len(d['kind']) == 1:
                continue  # «Y3», «K2» without the number of a mechanism: a terminal of the module, not a device
            if border is not None and (d['y'] < border) != (ch['y'] < border):
                continue
            score = dx * 4 + abs(d['y'] - ch['y'])
            if best is None or score < best[0]:
                best = (score, d, dx + abs(d['y'] - ch['y']))
        row = {'channel': ch['name'], 'dir': ch['dir'], 'x': ch['x'], 'y': ch['y'], 'zone': zone(ch['x'], page['width'])}
        if best:
            row.update(device=best[1]['name'], what=best[1]['what'], distance=round(best[2]))
        caption = [p for p in phrases if abs(p['x'] + p.get('w', 0) / 2 - ch['x']) <= reach
                   and page['height'] * 0.10 < p['y'] < page['height'] * 0.82
                   and (border is None or (p['y'] < border) == (ch['y'] < border))]
        if caption:
            row['caption'] = ' '.join(p['text'] for p in sorted(caption, key=lambda p: p['y']))[:90]
        out.append(row)
    for row in out:  # the module drawn nearest to the channel (a sheet may hold two)
        # …and of the right kind: an input is not on a module of outputs (TM3DQ…), an output — not on TM3DI…
        wrong = 'DQ' if row['dir'] == 'in' else 'DI'
        fit = [m for m in c['modules'] if wrong not in m['name']] or c['modules']
        near = min(fit, key=lambda m: abs(m['y'] - row['y']) + abs(m['x'] - row['x']) / 4, default=None)
        row['module'] = near['name'] if near else ''
    return out


# ----------------------------------------------------------------------------- the cache of cards
def cache_file(cache_dir: str, digest: str) -> str:
    return os.path.join(cache_dir, f'{digest[:32]}.json')


def load(path: str, cache_dir: str = '') -> dict:
    """{'file', 'sha256', 'pages': [...]} — read once per content of the file; next time from `cache_dir`."""
    if not os.path.isfile(path):
        raise DrawError(f'нет файла: {path}')
    if not path.lower().endswith('.pdf'):
        raise DrawError('нужен PDF: чертёж DWG / DXF сначала сохраните в PDF (или дайте его инструменту cad)')
    digest = sha256(path)
    if cache_dir:
        try:
            with open(cache_file(cache_dir, digest), encoding='utf-8') as f:
                data = json.load(f)
            if data.get('version') == CACHE_VERSION and data.get('sha256') == digest:
                data['file'] = path
                return data
        except (OSError, ValueError):
            pass
    pages = read_pages(path)
    data = {'version': CACHE_VERSION, 'file': path, 'sha256': digest, 'style': channel_style(pages), 'pages': pages}
    if cache_dir:
        try:
            os.makedirs(cache_dir, exist_ok=True)
            tmp = cache_file(cache_dir, digest) + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, separators=(',', ':'))
            os.replace(tmp, cache_file(cache_dir, digest))
        except OSError:
            pass  # the cache is a convenience: the answer is the same without it
    return data


def pdfs(folder: str, limit: int = 200) -> list:
    out = []
    for base, dirs, files in os.walk(folder):
        dirs[:] = sorted(d for d in dirs if not d.startswith(('.', '_')))
        for name in sorted(files):
            if name.lower().endswith('.pdf') and not name.startswith(('.', '~$')):
                out.append(os.path.join(base, name))
                if len(out) >= limit:
                    return out
    return out


# ----------------------------------------------------------------------------- questions
def sheets(doc: dict) -> dict:
    """The file sheet by sheet, compact: what kinds of devices each sheet holds and whether it has words at all."""
    rows = []
    for page in doc['pages']:
        c = card(page, doc.get('style', 'iq'))
        kinds = {}
        for d in c['devices']:
            kinds[d['kind']] = kinds.get(d['kind'], 0) + 1
        mech = sorted({d['mechanism'] for d in c['devices'] if d['mechanism']}, key=lambda s: int(s))
        rows.append({'page': c['page'], 'size': c['size'], 'chars': c['chars'], 'scan': c['scan'],
                     'devices': len(c['devices']), 'kinds': dict(sorted(kinds.items(), key=lambda kv: -kv[1])),
                     'mechanisms': mech[:20], 'modules': sorted({m['name'] for m in c['modules']}),
                     'channels': len(c['channels']), 'title': title_of(page)})
    return {'file': doc['file'], 'sha256': doc['sha256'], 'channel_style': doc.get('style', 'iq'), 'pages': len(rows), 'scans': sum(1 for r in rows if r['scan']),
            'sheets': rows}


def title_of(page: dict) -> str:
    """The longest Cyrillic phrases of the title block (the bottom right corner of the frame) — what the sheet is."""
    w, h = page['width'], page['height']
    block = [x for x in lines_of(page) if x['x'] > w * 0.55 and x['y'] > h * 0.82]
    best = sorted((x['text'] for x in block if len(re.findall('[А-Яа-яЁё]', x['text'])) >= 6), key=len, reverse=True)
    return ' / '.join(best[:2])[:160]


def devices(doc: dict, kind: str = '', mechanism: str = '') -> list:
    """Every designator of the file once: where it is drawn (sheets), what it is. `kind` — KM, M, SQ…; `mechanism` —
    the number before the letters («10» of 10KM1)."""
    found = {}
    for page in doc['pages']:
        for d in card(page, doc.get('style', 'iq'))['devices']:
            if kind and d['kind'] != kind.upper():
                continue
            if mechanism and d['mechanism'] != str(mechanism):
                continue
            row = found.setdefault(d['name'], {'name': d['name'], 'mechanism': d['mechanism'], 'kind': d['kind'], 'what': d['what'],
                                               'sheets': [], 'count': 0})
            row['sheets'].append(page['page'])
            row['count'] += d['count']
    return sorted(found.values(), key=lambda r: (int(r['mechanism'] or 0), r['kind'], int(re.sub(r'\D', '', r['name'][-3:]) or 0)))


def mechanisms(doc: dict) -> list:
    """The mechanisms of a scheme by the numbers of their devices, with the kinds of devices each has — the list to
    count the specification against. `drive` — the number has a motor: a number without one is a group of auxiliary
    devices or two labels read as one («14» and «1K8» standing close)."""
    by = {}
    for d in devices(doc):
        if not d['mechanism'] or d['kind'] in ('X', 'XT', 'XS', 'XP'):  # the terminals of a mechanism are not its devices
            continue
        row = by.setdefault(d['mechanism'], {'mechanism': d['mechanism'], 'kinds': {}, 'devices': [], 'sheets': set()})
        row['kinds'][d['kind']] = row['kinds'].get(d['kind'], 0) + 1
        row['devices'].append(d['name'])
        row['sheets'].update(d['sheets'])
    out = []
    for key in sorted(by, key=int):
        row = by[key]
        out.append({'mechanism': key, 'drive': bool(row['kinds'].get('M')),
                    'kinds': row['kinds'], 'devices': row['devices'][:40], 'sheets': sorted(row['sheets'])[:12]})
    return out


def find(doc: dict, query: str, limit: int = 40) -> list:
    """Where the text occurs: [{page, zone, x, y, text, near}] — the phrase it is part of and what is written next to
    it. Case and the Latin / Cyrillic spelling of look-alike letters do not matter."""
    q = query.strip().upper().translate(_LOOKALIKE)
    if not q:
        return []
    out = []
    for page in doc['pages']:
        phrases = lines_of(page)
        for i, ph in enumerate(phrases):
            if q not in ph['text'].upper().translate(_LOOKALIKE):
                continue
            near = [p['text'] for p in phrases if p is not ph and abs(p['y'] - ph['y']) <= 26 and abs(p['x'] - ph['x']) <= 150]
            out.append({'page': page['page'], 'zone': zone(ph['x'], page['width']), 'x': ph['x'], 'y': ph['y'], 'text': ph['text'][:200],
                        'near': [t[:60] for t in near[:8]]})
            if len(out) >= limit:
                return out
    return out


def text(doc: dict, page_no: int, area=None, limit: int = 6000) -> dict:
    """The words of one sheet as lines, top to bottom. `area` — [x0, y0, x1, y1] in points, or a zone 0–9."""
    if not 1 <= page_no <= len(doc['pages']):
        raise DrawError(f"листа {page_no} нет: в файле {len(doc['pages'])}")
    page = doc['pages'][page_no - 1]
    box = None
    if isinstance(area, (int, float)) and not isinstance(area, bool):
        step = page['width'] / 10
        box = (area * step, 0, (area + 1) * step, page['height'])
    elif isinstance(area, (list, tuple)) and len(area) == 4:
        box = tuple(float(v) for v in area)
    rows = {}
    for ph in lines_of(page):
        if box and not (box[0] <= ph['x'] <= box[2] and box[1] <= ph['y'] <= box[3]):
            continue
        rows.setdefault(ph['y'], []).append(ph)
    out, used = [], 0
    for y in sorted(rows):
        line = '  '.join(p['text'] for p in sorted(rows[y], key=lambda p: p['x']))
        if used + len(line) > limit:
            return {'page': page_no, 'size': [page['width'], page['height']], 'lines': out, 'truncated': True,
                    'note': 'лист длиннее лимита: возьмите часть — area: зона 0–9 или [x0, y0, x1, y1]'}
        out.append(line)
        used += len(line) + 1
    return {'page': page_no, 'size': [page['width'], page['height']], 'lines': out, 'truncated': False}


def table(doc: dict, page_no: int) -> list:
    """A sheet that is a table (the specification, the list of equipment of a technological scheme): its lines of text
    cut into cells by the gaps between the words — [[cell, …], …]."""
    if not 1 <= page_no <= len(doc['pages']):
        raise DrawError(f"листа {page_no} нет: в файле {len(doc['pages'])}")
    page = doc['pages'][page_no - 1]
    rows = {}
    for ph in lines_of(page, gap=9.0):
        rows.setdefault(round(ph['y'] / 5), []).append(ph)
    return [[p['text'] for p in sorted(rows[k], key=lambda p: p['x'])] for k in sorted(rows)]
