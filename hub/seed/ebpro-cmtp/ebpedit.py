"""Editing a whole EasyBuilder Pro project (reverse-engineered on 6.10): delete windows and objects, take windows
and objects from a donor project (tags, pictures and window links follow), point function keys at windows, add
OPC UA tags, resize objects, check a project. Every change is checked after the fact (class numbering, window
numbers, picture and tag tables); a change that does not check out leaves the project as it was."""
import hashlib
import re
import struct
import uuid

from ebpcmtp import (FK_ACTIONS, Project, _ALIGN, _DRAW_DEF, _PART_DEF, _SLOT, _TAG, _check_range, _cstr_at,
                     _map_info, _nested_classes, _text_em, _attrs_at, class_defs, cstring, fk_action, opc_tag_name,
                     count_edit, insert_point, layer_of, m241_node_id, map_slots, opcua_table)

SYSTEM_WINDOWS = 10         # windows 0-9 belong to EasyBuilder (common window, keypads' base, messages...)


def _whole(w: dict, what: str) -> None:
    if w['declared'] != len(w['objects']):
        raise ValueError(f'window {w["name"]!r}: not every object is recognised, {what} is not safe')


def _sure(w: dict, what: str) -> None:
    if not w.get('_end_sure'):
        raise ValueError(f'window {w["name"]!r}: where its record ends is not known, {what} is not safe')


def _numbers(p: Project) -> dict:
    return {w['guid']: w['number'] for w in p.windows()}


def _guarded(p: Project, fn):
    """Run fn(); if it raises, the project is left as it was."""
    old = p.payload
    try:
        return fn()
    except Exception:
        p.payload = old
        raise


# -- deleting -----------------------------------------------------------------------------------------------------
def delete_object(p: Project, window, index: int) -> None:
    """Delete one object of a window (later objects move up by one index)."""
    pay, ws = p.payload, p.windows()
    w, (o,) = p._objects(window, index)
    if o['kind'] == 'Window':
        raise ValueError('the window object itself cannot be deleted (delete the window: delete_window)')
    _whole(w, 'deleting from it')
    if o is w['objects'][-1]:
        _sure(w, 'deleting its last object')
    s, e = o['offset'], o['offset'] + o['length']
    if pay[s:s + 2] == b'\xff\xff':
        raise ValueError('this object defines the widget class of the whole project; it cannot be deleted')
    info = _map_info(pay, ws)
    _check_range(pay, s, e, info, 'deleting')
    before = _numbers(p)
    cnt = count_edit(w, -1, layer_of(w, o['index']))

    def run():
        p._insert(info, [(s, e - s, b'', -1)], [cnt])
        if _numbers(p) != before:
            raise ValueError('window numbers moved after the delete; the project is left unchanged')
    _guarded(p, run)


def delete_window(p: Project, window) -> list:
    """Delete a window; its number becomes free. Returns the function keys that still lead to it."""
    pay, ws = p.payload, p.windows()
    w = p.window(window)
    if w['number'] < SYSTEM_WINDOWS:
        raise ValueError(f'window {w["number"]} belongs to EasyBuilder; it is not deleted')
    _whole(w, 'deleting it')
    _sure(w, 'deleting it')
    s, e = w['offset'] - 4, w['_rec_end']
    if pay[s:s + 4] != _SLOT:
        raise ValueError('the number slot of the window is not where expected')
    if any(pay.find(d, s, e) != -1 for d in (_PART_DEF, _DRAW_DEF)):
        raise ValueError('the window defines the widget classes of the project; it cannot be deleted')
    info = _map_info(pay, ws)
    _check_range(pay, s, e, info, 'deleting')
    before = _numbers(p)
    before.pop(w['guid'], None)
    left = [ln for ln in p.links() if ln['target'] == w['number'] and ln['window'] != w['number']]

    def run():
        p._insert(info, [(s, e - s, bytes(4), -map_slots(w))])
        if _numbers(p) != before:
            raise ValueError('window numbers moved after the delete; the project is left unchanged')
    _guarded(p, run)
    return left


# -- function keys --------------------------------------------------------------------------------------------------
def link(p: Project, window, index: int, to) -> int:
    """Point a function key that opens a window (full-screen or pop-up) at window `to`. Returns its number."""
    w, (o,) = p._objects(window, index)
    fa = fk_action(p.payload, o)
    if fa is None:
        raise ValueError(f'object #{index} of {w["name"]!r} is not a function key with a known action')
    if fa[1] not in (0, 2):
        raise ValueError(f'function key #{index} does not open a window (its action: '
                         f'{FK_ACTIONS.get(fa[1], fa[1])}); set «change window» or «pop-up window» in EasyBuilder once')
    n = _target_number(p, to)
    p._splice([(fa[0] + 2, fa[0] + 4, struct.pack('<H', n))])
    return n


def _target_number(p: Project, ref) -> int:
    try:
        return p.window(ref)['number']
    except ValueError:
        if isinstance(ref, int) or str(ref).strip().isdigit():
            n = int(ref)
            if 0 <= n <= 1999:
                return n        # a window that does not exist yet (cmtp_check reports it)
        raise


# -- OPC UA tags ------------------------------------------------------------------------------------------------------
_NODE = re.compile(r'^ns[:=](\d+);([si])[:=](.+)$')


def _tag_bytes(name: str, typ: str, ns: int, ident: str, mid: bytes, style: str = 'a', kind: str = 's') -> bytes:
    full = f'{name} (ns:{ns};{kind}:{ident})({ident})'.encode('utf-8')
    idb, tb = ident.encode('utf-8'), typ.encode('ascii')
    if len(full) > 0xffff or len(idb) > 0xffff:
        raise ValueError(f'tag name too long: {name!r}')
    mid = mid[:-1] + (b'\x00' if kind == 'i' else b'\x01')
    return ((_TAG if style == 'a' else _TAG[1:]) + len(full).to_bytes(2, 'big') + full + b'\x00' + bytes([len(tb)])
            + tb + mid + ns.to_bytes(2, 'big') + len(idb).to_bytes(2, 'big') + idb + (bytes(4) if style == 'a' else b''))


def _tag_insert(pay: bytes, tab: dict, entries: list) -> list:
    """Edits adding raw entries to the table: before EasyBuilder's own «UaStatus Code» when it is the last one."""
    if not entries:
        return []
    last = tab['entries'][-1]
    at = last['start'] if last['ns'] == 10 and last['name'].startswith('UaStatus') else tab['end']
    return [(at, at, b''.join(entries)),
            (tab['count_at'], tab['count_at'] + 4, struct.pack('<I', tab['count'] + len(entries)))]


def add_tags(p: Project, tags: list) -> list:
    """Add OPC UA tags to the table: [{name, type, node?}] (node by default ns of the table, s=name). Types: those
    the table already uses. Returns the names added (a tag already there with the same type and node is skipped)."""
    pay = p.payload
    tab = opcua_table(pay)
    if tab is None:
        raise ValueError('the project has no OPC UA tag table; add the OPC UA server and one tag in EasyBuilder first')
    user = [e for e in tab['entries'] if e['ns'] != 10] or tab['entries']
    have = {e['name']: e for e in tab['entries']}
    types = sorted({e['type'] for e in user})
    ns0 = user[0]['ns']
    numeric = sum(1 for e in user if e.get('numeric')) * 2 > len(user)
    raw, added = [], []
    for t in tags:
        if not isinstance(t, dict) or not str(t.get('name') or '').strip():
            raise ValueError('every tag needs a name and a type')
        name, typ = str(t['name']).strip(), str(t.get('type') or '').strip().upper()
        # a project whose tags carry numbers (the Schneider M241 / M251 server) gets the number of the new tag
        # worked out from its name; with string identifiers the name is the identifier
        node = str(t.get('node') or (f'ns:{ns0};i:{m241_node_id(name)}' if numeric else f'ns:{ns0};s:{name}')).strip()
        m = _NODE.match(node)
        if not m or (m.group(2) == 'i' and not m.group(3).isdigit()):
            raise ValueError(f'tag {name}: node must look like ns:2;s:GVL.X or ns:2;i:123, not {node!r}')
        ns, kind, ident = int(m.group(1)), m.group(2), m.group(3)
        node = f'ns:{ns};{kind}:{ident}'
        if name in have:
            if have[name]['type'] != typ or have[name]['node'] != node:
                raise ValueError(f'tag {name} is already in the table as {have[name]["type"]} {have[name]["node"]}')
            continue
        if typ not in types:
            raise ValueError(f'tag {name}: type {typ or "?"} is not used in this project yet (there are: '
                             f'{", ".join(types)}); add one such tag in EasyBuilder first')
        raw.append(_tag_bytes(name, typ, ns, ident, user[0]['mid'], tab['style'], kind))
        have[name] = {'type': typ, 'node': node}
        added.append(name)
    if not raw:
        return []

    def run():
        p._splice(_tag_insert(pay, tab, raw))
        new = opcua_table(p.payload)
        if new is None or new['count'] != tab['count'] + len(raw) or \
                not set(added) <= {e['name'] for e in new['entries']}:
            raise ValueError('the tag table does not read back after adding; the project is left unchanged')
    _guarded(p, run)
    return added


# -- object appearance and precise OPC UA binding -----------------------------------------------------------------
def _color(value):
    if not isinstance(value, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        raise ValueError('colour must be #RRGGBB')
    return bytes.fromhex(value[1:])


def style(p: Project, window, index: int, changes: dict) -> dict:
    """Change known appearance fields only; other object and state data stay intact."""
    w, (o,) = p._objects(window, index)
    allowed = {'fill', 'frame', 'frame_width', 'text_color', 'font_size', 'label'}
    if not isinstance(changes, dict) or set(changes) - allowed or not (set(changes) - {'label'}):
        raise ValueError('no supported appearance properties')
    edits, expected = [], {}
    end = o['offset'] + o['length']
    fields = set(changes) & {'fill', 'frame', 'frame_width'}
    if fields:
        if o['kind'] not in ('Window', 'Rect', 'Line'):
            raise ValueError('fill and frame are only supported for Window, Rect and Line')
        if o['kind'] == 'Window' and (fields - {'fill'} or changes.get('fill') is None):
            raise ValueError('window supports only an opaque background fill')
        a = _attrs_at(p.payload, o, w)
        if a is None or a + 45 > end:
            raise ValueError('object appearance is not recognised')
        if 'fill' in changes:
            fill = changes['fill']
            if fill is None:
                edits.append((a, a + 1, b'\x00'))
                expected['fill'] = None
            else:
                rgb = _color(fill)
                if o['kind'] != 'Window':
                    edits.append((a, a + 1, b'\x01'))
                edits.append((a + 1, a + 4, rgb))
                expected['fill'] = '#' + rgb.hex()
        if 'frame' in changes:
            rgb = _color(changes['frame'])
            edits.append((a + 5, a + 8, rgb))
            expected['frame'] = '#' + rgb.hex()
        if 'frame_width' in changes:
            width = changes['frame_width']
            if not isinstance(width, int) or isinstance(width, bool) or not 0 <= width <= 20:
                raise ValueError('frame_width must be an integer from 0 to 20')
            edits.append((a + 44, a + 45, bytes([width])))
            expected['frame_width'] = width
    lab_fields = set(changes) & {'text_color', 'font_size'}
    label = changes.get('label', 0)
    if lab_fields:
        if not isinstance(label, int) or isinstance(label, bool) or not 0 <= label < len(o['_label_pos']):
            raise ValueError('label state not found')
        at = o['_label_pos'][label][0]
        if at - 1 < o['offset'] or at + 13 > end:
            raise ValueError('label properties are not recognised')
        if 'text_color' in changes:
            rgb = _color(changes['text_color'])
            edits.append((at + 8, at + 11, rgb))
            expected['text_color'] = '#' + rgb.hex()
        if 'font_size' in changes:
            size = changes['font_size']
            if not isinstance(size, int) or isinstance(size, bool) or not 1 <= size <= 100:
                raise ValueError('font_size must be an integer from 1 to 100 points')
            edits.append((at - 1, at, bytes([size])))
            expected['font_size'] = size
    def run():
        p._splice(edits)
        look = p.scene(w['number'])['objects'][index]
        for key, value in expected.items():
            got = (look['labels'][label]['color'] if key == 'text_color' else
                   look['labels'][label]['size'] if key == 'font_size' else look.get(key))
            if got != value:
                raise ValueError(f'appearance {key} does not read back; project unchanged')
        return expected
    return _guarded(p, run)


def bind_tag(p: Project, window, index: int, slot: int, tag: str) -> dict:
    """Rebind one already-OPC-UA slot to a tag of the same known type; never alter device or slot type."""
    w, (o,) = p._objects(window, index)
    if not isinstance(slot, int) or isinstance(slot, bool) or slot not in (1, 2, 3):
        raise ValueError('slot must be 1, 2 or 3')
    if len(o['_dev']) != 3 or len(o['_addr']) < 3:
        raise ValueError('object address slots are not recognised')
    start, end, address = o['_addr'][slot - 1]
    old = opc_tag_name(address)
    tags = {t['name']: t for t in p.opcua_tags()}
    if old not in tags:
        raise ValueError('slot must already contain an OPC UA tag from the project table')
    if address != f'{old} ({tags[old]["node"]})':
        raise ValueError('slot node differs from the project tag table; verify it in EasyBuilder')
    if tag not in tags:
        raise ValueError(f'tag {tag!r} is not in the OPC UA tag table')
    if tags[old]['type'] != tags[tag]['type']:
        raise ValueError(f'type mismatch: {old} is {tags[old]["type"]}, {tag} is {tags[tag]["type"]}')
    def run():
        p._splice([(start, end, cstring(f'{tag} ({tags[tag]["node"]})'))])
        w2, (o2,) = p._objects(w['number'], index)
        if len(o2['_addr']) < 3 or opc_tag_name(o2['_addr'][slot - 1][2]) != tag:
            raise ValueError('address does not read back; project unchanged')
        if [d[2] for d in o2['_dev']] != [d[2] for d in o['_dev']]:
            raise ValueError('device changed unexpectedly; project unchanged')
        return {'slot': slot, 'old': old, 'tag': tag, 'type': tags[tag]['type']}
    return _guarded(p, run)


# -- size ---------------------------------------------------------------------------------------------------------------
def resize(p: Project, window, index: int, width=None, height=None) -> list:
    """New size of an object in pixels (its top-left corner stays); centred / right-aligned labels follow. Returns
    the new frame."""
    pay = p.payload
    w, (o,) = p._objects(window, index)
    if o['bbox'] is None or o['_bbox_at'] is None or o['kind'] == 'Window':
        raise ValueError(f'object #{index} of {w["name"]!r} has no frame to resize')
    l, t, r, b = o['bbox']
    nw = r - l + 1 if width is None else int(width)
    nh = b - t + 1 if height is None else int(height)
    if not (3 <= nw <= 4000 and 3 <= nh <= 4000):
        raise ValueError('width and height go from 3 to 4000 pixels')
    nb = [l, t, l + nw - 1, t + nh - 1]
    dw, dh = nw - (r - l + 1), nh - (b - t + 1)
    edits = [(o['_bbox_at'], o['_bbox_at'] + 16, struct.pack('<iiii', *nb))]
    end = o['offset'] + o['length']
    for at, x, y in o['_label_pos']:
        if not (l - 5 <= x <= r + 5 and t - 5 <= y <= b + 5) or (x, y) == (l, t):
            continue        # a label at the corner is laid out over the whole object: it stays
        align = _ALIGN.get(pay[at + 12], 'left') if at + 12 < end else 'left'
        nx = x + (dw // 2 if align == 'center' else dw if align == 'right' else 0)
        ny = y if o['kind'] == 'Text' else y + dh // 2
        if (nx, ny) != (x, y):
            edits.append((at, at + 8, struct.pack('<ii', nx, ny)))
    p._splice(edits)
    return nb


# -- taking windows and objects from a donor project ----------------------------------------------------------------
def open_donor(donor, p: Project) -> Project:
    d = donor if isinstance(donor, Project) else Project.load(donor)
    if d.revision != p.revision:
        raise ValueError(f'the donor is written by EasyBuilder {d.version} (format {d.revision}), the project by '
                         f'{p.version} (format {p.revision}): open the donor in the same EasyBuilder as the project, '
                         f'save it and take that file')
    if not d.windows():
        raise ValueError('no windows in the donor project')
    return d


def _donor_info(d: Project) -> dict:
    cache = getattr(d, '_donor_cache', None)
    if cache and cache[0] is d.payload:
        return cache[1]
    pay, ws = d.payload, d.windows()
    try:
        info = _map_info(pay, ws)
        info['sure'] = True
    except ValueError:
        defs = class_defs(pay)
        info = {'defs': defs, 'nested': _nested_classes(pay, ws, defs, ws[-1]['_rec_end']), 'sure': False}
    d._donor_cache = (pay, info)
    return info


# Kinds that may hold sub-objects (trends: limits; option lists, part_1c: CCharDouble) — taken only when the
# donor's class numbering checks out, so that every sub-object is seen.
_NESTED_KINDS = ('Trend?', 'OptionList?', 'part_1c')


class Graft:
    """Rewrites donor records for the project: class references, tags, devices, pictures and window links; collects
    what the project gets in addition (picture files and objects, tag table entries) and notes for the person."""

    def __init__(self, p: Project, d: Project, info: dict, tags=None, windows=None, devices=None, linked=None):
        self.p, self.d, self.info = p, d, info
        self.dinfo = _donor_info(d)
        if info['ptag'] is None:
            raise ValueError('the project has no widget written by reference to take the class from')
        ptab, dtab = opcua_table(p.payload), opcua_table(d.payload)
        self.ptab = ptab
        self.T = {e['name']: e for e in (ptab['entries'] if ptab else ())}
        self.D = {e['name']: e for e in (dtab['entries'] if dtab else ())}
        self.tagmap = {str(k): str(v) for k, v in (tags or {}).items()}
        self.devmap = {str(k): str(v) for k, v in (devices or {}).items()}
        self.winmap = {}
        for k, v in (windows or {}).items():
            self.winmap[d.window(k)['number']] = v
        self.linked = linked if linked is not None else {}
        self.devices = {dev for w in p.windows() for o in w['objects'] for _, _, dev in o['_dev'] if dev}
        self.new_tags, self.notes, self.pending = [], [], []
        # pictures
        dlib, _ = d._library()
        dsec, _ = d.picture_section()
        plib, _ = p._library()
        psec, pspan = p.picture_section()
        self.dlib, self.plib, self.psec, self.pspan = dlib, plib, psec, pspan
        self.dgroups = {g['key']: g for g in (dsec or {}).get('groups', ())}
        self.pgroups = {g['key']: g for g in (psec or {}).get('groups', ())}
        self.pkeys = set(self.pgroups)
        self.phash = {}
        for g, (o, n) in plib.items():
            self.phash.setdefault(hashlib.sha1(p.payload[o:o + n]).digest(), g)
        self.file_map, self.key_map = {}, {}
        self.new_files, self.new_groups = [], []

    # tags
    def _tag(self, t: str) -> str:
        name = opc_tag_name(t)
        tn = self.tagmap.get(name, name)
        if tn in self.T:
            src = self.D.get(name)
            if src and src['type'] != self.T[tn]['type']:
                raise ValueError(f'type mismatch: {name} is {src["type"]} in the donor, {tn} is {self.T[tn]["type"]}')
            return f'{tn} ({self.T[tn]["node"]})'
        if tn != name:
            raise ValueError(f'tag {tn!r} is not in the OPC UA tag table of the project')
        e = self.D.get(name)
        if e is None:
            self.notes.append(f'tag {name} is in the tag table of neither project')
            return t
        if self.ptab is None:
            self.notes.append(f'tag {name}: the project has no OPC UA tag table — add the server in EasyBuilder')
            return t
        self.new_tags.append(self.d.payload[e['start']:e['end']])
        self.T[name] = e
        return t

    # pictures
    def _file(self, f: bytes) -> bytes:
        if f in self.file_map:
            return self.file_map[f]
        o, n = self.dlib[f]
        data = self.d.payload[o:o + n]
        h = hashlib.sha1(data).digest()
        if h in self.phash:
            nf = self.phash[h]
        else:
            nf = f if f not in self.plib and f not in self.file_map.values() else uuid.uuid4().bytes
            self.new_files.append((nf, data))
            self.phash[h] = nf
        self.file_map[f] = nf
        return nf

    def _picture(self, key: bytes) -> bytes:
        if key in self.key_map:
            return self.key_map[key]
        g = self.dgroups[key]
        if any(f not in self.dlib for f, _ in g['states']):
            self.notes.append(f'picture {key.hex()} has files missing in the donor; left as it is')
            self.key_map[key] = key
            return key
        files = [self._file(f) for f, _ in g['states']]
        pg = self.pgroups.get(key)
        if pg and [f for f, _ in pg['states']] == files and [t for _, t in pg['states']] == [t for _, t in g['states']]:
            self.key_map[key] = key
            return key
        if self.psec is None or not self.psec['complete'] or not self.psec['count']:
            raise ValueError('the picture objects of the project were not recognised (a project without pictures?); '
                             'put any picture into the project in EasyBuilder once, then take objects with pictures')
        nk = key if key not in self.pkeys else uuid.uuid4().bytes
        dp = self.d.payload
        states = b''.join(b'\x02\x04\x01\x00\x01\x00' + nf + dp[at + 16:at + 28] for nf, at in zip(files, g['state_at']))
        self.new_groups.append(b'\x01\x04\x01\x00' + struct.pack('<H', len(g['name_raw'])) + g['name_raw']
                               + struct.pack('<I', len(files)) + states + nk)
        self.pkeys.add(nk)
        self.key_map[key] = nk
        return nk

    # windows
    def _window(self, n: int, where: str, slot) -> int:
        if n in self.linked:
            return self.linked[n]
        if n in self.winmap:
            return _target_number(self.p, self.winmap[n])
        try:
            name = self.d.window(n)['name']
        except ValueError:
            name = '?'
        self.notes.append(f'{where}: the key leads to donor window {n} «{name}», not taken — the number is kept')
        self.pending.append({'object': slot, 'donor_target': n, 'donor_name': name})
        return n

    def graft(self, s: int, e: int, objs: list, shift=(0, 0), where='', slots=None) -> bytes:
        """Donor bytes [s, e) rewritten for the project (objs — the donor objects in that range, slots — their
        indexes in the project window)."""
        dp, info = self.d.payload, self.info
        _check_range(dp, s, e, self.dinfo, 'taking')
        edits = []
        dx, dy = shift
        for j, o in enumerate(objs):
            slot = slots[j] if slots else o['index']
            if o.get('_compact'):
                raise ValueError(f'opaque {o["kind"]} cannot be taken until its contents are understood')
            if not self.dinfo.get('sure') and o['kind'] in _NESTED_KINDS:
                raise ValueError(f'object #{o["index"]} ({o["kind"]}) may hold sub-objects and the class numbering of '
                                 f'the donor does not check out; build this one in EasyBuilder')
            at, oe = o['offset'], o['offset'] + o['length']
            if o['_cls'] == 'P':
                n = len(_PART_DEF) if dp[at:at + 2] == b'\xff\xff' else 2
                edits.append((at, at + n, info['ptag']))
            else:
                if info['dtag'] is None:
                    raise ValueError('the project has no shape written by reference to take the class from; '
                                     'draw two rectangles in EasyBuilder once')
                n = len(_DRAW_DEF) if dp[at:at + 2] == b'\xff\xff' else 2
                edits.append((at, at + n, info['dtag']))
            for a, b_, t in o['_addr']:
                if opc_tag_name(t):
                    nt = self._tag(t)
                    if nt != t:
                        edits.append((a, b_, cstring(nt)))
            for a, b_, dev in o['_dev']:
                if dev in self.devmap:
                    edits.append((a, b_, cstring(self.devmap[dev])))
                elif dev and dev not in self.devices and dev not in self.devmap.values():
                    self.notes.append(f'{where} #{slot}: device «{dev}» is not used in the project (map it: map_devices)')
                    self.devices.add(dev)
            fa = fk_action(dp, o)
            if fa and fa[1] in (0, 2):
                n2 = self._window(fa[2], f'{where} #{slot}', slot)
                if n2 != fa[2]:
                    edits.append((fa[0] + 2, fa[0] + 4, struct.pack('<H', n2)))
            for key in self.dgroups:
                k = dp.find(key, at, oe)
                while k != -1:
                    nk = self._picture(key)
                    if nk != key:
                        edits.append((k, k + 16, nk))
                    k = dp.find(key, k + 16, oe)
            if dx or dy:
                if o['bbox'] is None or o['_bbox_at'] is None:
                    raise ValueError(f'object #{o["index"]} has no frame to shift')
                l, t, r, b = o['bbox']
                nb = [l + dx, t + dy, r + dx, b + dy]
                if min(nb) < 0:
                    raise ValueError('the copy would get negative coordinates')
                edits.append((o['_bbox_at'], o['_bbox_at'] + 16, struct.pack('<IIII', *nb)))
                for la, x, y in o['_label_pos']:
                    if l - 5 <= x <= r + 5 and t - 5 <= y <= b + 5:
                        edits.append((la, la + 8, struct.pack('<ii', x + dx, y + dy)))
        out, last = [], s
        for a, b_, new in sorted(edits):
            if a < last:
                raise ValueError('overlapping rewrites in a donor record')
            out += [dp[last:a], new]
            last = b_
        out.append(dp[last:e])
        return b''.join(out)

    def extra(self) -> list:
        """Edits of the project outside the inserted records: new picture files and objects, new tags."""
        pay, ed = self.p.payload, []
        if self.new_files:
            start, end, n = self.pspan
            ed.append((end, end, b''.join(b'\xff' + g + struct.pack('<I', len(data)) + data for g, data in self.new_files)))
            ed.append((start - 4, start, struct.pack('<I', n + len(self.new_files))))
        if self.new_groups:
            sec = self.psec
            ed.append((sec['end'], sec['end'], b''.join(self.new_groups)))
            ed.append((sec['count_at'], sec['count_at'] + 4, struct.pack('<I', sec['count'] + len(self.new_groups))))
        if self.new_tags:
            ed += _tag_insert(pay, self.ptab, self.new_tags)
        return ed

    def verify(self) -> None:
        p = self.p
        if self.new_files or self.new_groups:
            p._lib_cache = None
            sec, span = p.picture_section()
            lib, groups = p._library()
            if span[2] != self.pspan[2] + len(self.new_files) or sec is None or not sec['complete'] or \
                    sec['count'] != self.psec['count'] + len(self.new_groups) or \
                    any(k not in groups for k in self.key_map.values() if k in self.pkeys and k not in self.pgroups):
                raise ValueError('the pictures do not read back after adding')
        if self.new_tags:
            tab = opcua_table(p.payload)
            if tab is None or tab['count'] != self.ptab['count'] + len(self.new_tags):
                raise ValueError('the tag table does not read back after adding')


def _free_slot(p: Project, number=None):
    """(number, offset of its empty 4-byte slot) for a new window."""
    pay, ws = p.payload, p.windows()
    used = {x['number']: x for x in ws}
    number = max(used) + 1 if number is None else int(number)
    if number in used:
        raise ValueError(f'window number {number} is taken by {used[number]["name"]!r}')
    if not 3 <= number <= 1999:
        raise ValueError('window numbers go from 3 to 1999')
    prev = max((x for x in ws if x['number'] < number), key=lambda x: x['number'])
    nxt = min((x for x in ws if x['number'] > number), key=lambda x: x['number'], default=None)
    _sure(prev, f'placing window {number} after it')
    at = prev['_rec_end'] + 4 * (number - prev['number'] - 1)
    if nxt is not None:
        limit = nxt['offset'] - 4
    else:
        limit = re.compile(rb'\x00*').match(pay, prev['_rec_end']).end() - 1024
    if at + 4 > limit or pay[at:at + 4] != bytes(4):
        raise ValueError(f'no free slot for window number {number}')
    return number, at, prev


def import_window(p: Project, donor, src, number=None, name=None, tags=None, windows=None, devices=None,
                  linked=None) -> dict:
    """Take window `src` of the donor project into the project as window `number` (default: after the last one),
    named `name` (default: as in the donor). tags {donor tag: project tag}, windows {donor window: project window},
    devices {donor device: project device}; tags missing in the project come from the donor's table, pictures from
    the donor's library. linked {donor number: project number} — windows already taken (the keys follow them).
    Returns {number, notes, pending}."""
    d = open_donor(donor, p)
    dw = d.window(src)
    _whole(dw, 'taking it')
    _sure(dw, 'taking it')
    pay, ws = p.payload, p.windows()
    number, at, prev = _free_slot(p, number)
    info = _map_info(pay, ws)
    linked = linked if linked is not None else {}
    linked[dw['number']] = number
    g = Graft(p, d, info, tags, windows, devices, linked)
    rec = bytearray(g.graft(dw['offset'], dw['_rec_end'], dw['objects'], where=f'window {number}'))
    r = _cstr_at(rec, 0)
    if r is None or r[0] != dw['name']:
        raise ValueError('the donor window name is not where expected')
    body = rec[r[1]:]
    guid = uuid.uuid4().bytes
    body[24:40] = guid
    data = _SLOT + cstring(dw['name'] if name is None else str(name)) + bytes(body)
    before = _numbers(p)

    def run():
        p._insert(info, [(at, 4, data, map_slots(dw))], g.extra())
        nums = _numbers(p)
        if nums.get(guid.hex()) != number or any(nums.get(k) != v for k, v in before.items()):
            raise ValueError(f'window number {number} cannot be placed reliably; take {prev["number"] + 1}')
        g.verify()
    _guarded(p, run)
    return {'number': number, 'notes': g.notes, 'pending': g.pending}


def import_objects(p: Project, donor, src, objects, to, dx=0, dy=0, tags=None, windows=None, devices=None,
                   linked=None, layer=None) -> dict:
    """Take objects (indexes) of donor window `src` to the end of the project's window `to` (of its layer `layer`,
    name or number; the last one by default), shifted by dx, dy.
    Returns {first: index of the first copy, count, notes, pending}."""
    d = open_donor(donor, p)
    dw = d.window(src)
    _whole(dw, 'taking from it')
    idx = [int(i) for i in (objects if isinstance(objects, (list, tuple)) else [objects])]
    if not idx:
        raise ValueError('no objects to take')
    objs = []
    for i in idx:
        if not 0 <= i < len(dw['objects']):
            raise ValueError(f'donor window {dw["name"]!r} has no object #{i}')
        o = dw['objects'][i]
        if o['kind'] == 'Window':
            raise ValueError('the window object itself is not taken (take the whole window: import_window)')
        if o is dw['objects'][-1]:
            _sure(dw, 'taking its last object')
        objs.append(o)
    pay, ws = p.payload, p.windows()
    tw = p.window(to)
    _whole(tw, 'inserting into it')
    at, first, lay = insert_point(tw, layer)
    info = _map_info(pay, ws)
    g = Graft(p, d, info, tags, windows, devices, linked)
    rec = b''.join(g.graft(o['offset'], o['offset'] + o['length'], [o], (int(dx or 0), int(dy or 0)),
                           where=f'window {tw["number"]}', slots=[first + j]) for j, o in enumerate(objs))
    cnt = count_edit(tw, len(objs), lay)
    before = _numbers(p)

    def run():
        p._insert(info, [(at, 0, rec, len(objs))], [cnt] + g.extra())
        if _numbers(p) != before:
            raise ValueError('window numbers moved after the insert; the project is left unchanged')
        g.verify()
    _guarded(p, run)
    return {'first': first, 'count': len(objs), 'notes': g.notes, 'pending': g.pending}


# -- image states -------------------------------------------------------------------------------------------------
def picture_states(p: Project, window, index: int, states: list) -> list:
    """Replace one object's picture group without modifying other objects sharing it.

    Each state is {'image': existing file GUID hex OR 'png': bytes, 'transparent': '#rrggbb' or None}.
    Number of states must remain unchanged: object state programming is not modified.
    """
    w, (obj,) = p._objects(window, index)
    _whole(w, 'changing pictures in it')
    if obj['kind'] not in ('Picture', 'BitLamp', 'MultiState?', 'WordLamp?', 'ToggleSwitch', 'FunctionKey'):
        raise ValueError('this object type is not supported for picture replacement')
    if not isinstance(states, list) or not states or len(states) > 100:
        raise ValueError('pictures need 1 to 100 states')
    pay = p.payload
    lib, groups = p._library()
    sec, span = p.picture_section()
    if not sec or not sec['complete'] or not sec['count']:
        raise ValueError('the picture library is not completely understood')
    s, e = obj['offset'], obj['offset'] + obj['length']
    hits = []
    for key in groups:
        at = pay.find(key, s, e)
        while at >= 0:
            hits.append((at, key))
            at = pay.find(key, at + 16, e)
    if len(hits) != 1:
        raise ValueError('the object does not have one unambiguous picture group')
    at, oldkey = hits[0]
    if len(states) != len(groups[oldkey]):
        raise ValueError(f'the object has {len(groups[oldkey])} picture states; the count cannot be changed here')
    known_hash = {hashlib.sha256(pay[o:o+n]).digest(): key for key, (o, n) in lib.items()}
    new_files, new_states = [], []
    for st in states:
        if not isinstance(st, dict) or ('image' in st) == ('png' in st):
            raise ValueError('each state needs one existing image GUID or PNG')
        col = st.get('transparent')
        if col is not None and (not isinstance(col, str) or not re.fullmatch(r'#[0-9a-fA-F]{6}', col)):
            raise ValueError('transparent must be a colour #RRGGBB or null')
        if 'image' in st:
            try:
                key = bytes.fromhex(st['image'])
            except (TypeError, ValueError):
                raise ValueError('bad image GUID') from None
            if key not in lib:
                raise ValueError('image GUID is not in the project library')
        else:
            data = st['png']
            _validate_png(data)
            digest = hashlib.sha256(data).digest()
            key = known_hash.get(digest)
            if key is None:
                key = uuid.uuid4().bytes
                known_hash[digest] = key
                new_files.append((key, data))
        new_states.append((key, col.lower() if col else None))
    name = b'SYSAI picture states'
    group_key = uuid.uuid4().bytes
    rec = b'\x01\x04\x01\x00' + struct.pack('<H', len(name)) + name + struct.pack('<I', len(new_states))
    for key, col in new_states:
        rgb = bytes.fromhex(col[1:]) if col else b'\xff\xff\xff'
        rec += b'\x02\x04\x01\x00\x01\x00' + key + struct.pack('<II', bool(col), 0) + rgb + b'\x00'
    rec += group_key
    start, end, n = span
    edits = [(at, at + 16, group_key), (sec['end'], sec['end'], rec),
             (sec['count_at'], sec['count_at'] + 4, struct.pack('<I', sec['count'] + 1))]
    if new_files:
        edits += [(end, end, b''.join(b'\xff' + key + struct.pack('<I', len(data)) + data for key, data in new_files)),
                  (start - 4, start, struct.pack('<I', n + len(new_files)))]
    before = _numbers(p)
    addresses = list(obj['addresses'])
    def run():
        p._splice(edits)
        _map_info(p.payload, p.windows())
        if _numbers(p) != before or p.window(window)['objects'][index]['addresses'] != addresses:
            raise ValueError('window or addresses changed unexpectedly')
        _, after = p._library()
        if after.get(group_key) != new_states:
            raise ValueError('picture states do not read back')
        return [k.hex() for k, _ in new_states]
    return _guarded(p, run)


def _validate_png(data: bytes) -> None:
    """Check a bounded PNG's chunk boundaries, CRCs and image header without decoding pixels."""
    import zlib
    if not isinstance(data, bytes) or len(data) > 8 * 1024 * 1024 or not data.startswith(b'\x89PNG\r\n\x1a\n'):
        raise ValueError('image must be a PNG of at most 8 MB')
    pos, ihdr, idat, end = 8, False, False, False
    while pos + 12 <= len(data):
        n = struct.unpack_from('>I', data, pos)[0]
        if n > len(data) - pos - 12:
            break
        typ = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + n]
        crc = struct.unpack_from('>I', data, pos + 8 + n)[0]
        if zlib.crc32(typ + body) & 0xffffffff != crc:
            raise ValueError('PNG checksum failed')
        if not ihdr:
            if typ != b'IHDR' or n != 13:
                raise ValueError('PNG header is missing')
            width, height = struct.unpack_from('>II', body)
            depth, color, compression, filt, interlace = body[8:13]
            depths = {0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8), 4: (8, 16), 6: (8, 16)}
            if not (1 <= width <= 4096 and 1 <= height <= 4096):
                raise ValueError('PNG dimensions must be 1–4096 pixels')
            if color not in depths or depth not in depths[color] or compression or filt or interlace not in (0, 1):
                raise ValueError('PNG header is invalid')
            ihdr = True
        if typ == b'IDAT':
            idat = True
        pos += n + 12
        if typ == b'IEND':
            end = n == 0
            break
    if not (ihdr and idat and end and pos == len(data)):
        raise ValueError('PNG is incomplete')


# -- checking --------------------------------------------------------------------------------------------------------
def check(p: Project, window=None) -> list:
    """What would go wrong on the panel: [{level (error/warning/info), window, object, code, text}]. Codes:
    outside (object beyond its window), text_fit (a label wider than its object), link_missing (a key leads to no
    window), tag_missing (an address not in the OPC UA tag table), picture_missing, unreachable (no key leads to the
    window), duplicate_name, not_parsed (objects not recognised)."""
    pay, ws = p.payload, p.windows()
    out = []
    # how wide a font really is: EasyBuilder itself fits the frame of a Text object to its text, so the frames of
    # the project's own texts give the width of each font (a narrow one, like GOST Type, is not flagged for nothing)
    seen = {}
    for w in ws:
        for o in w['objects']:
            if o['kind'] == 'Text' and o['bbox'] and len(o['_lab']) == 1 and o['_lab'][0][0].strip():
                text, font, size = o['_lab'][0]
                em = max(_text_em(line) for line in text.splitlines() or [text]) * size
                if em > 0 and len(text) >= 4:
                    seen.setdefault(font, []).append((o['bbox'][2] - o['bbox'][0] + 1) / em)
    factor = {f: sorted(v)[len(v) // 2] for f, v in seen.items() if len(v) >= 5}

    def add(level, w, o, code, text):
        out.append({'level': level, 'window': w['number'] if w else None, 'object': o, 'code': code, 'text': text})
    nums = {w['number']: w for w in ws}
    tags = {t['name'] for t in p.opcua_tags()}
    _, groups = p._library()
    sec, _ = p.picture_section()
    known = {g['key'] for g in (sec or {}).get('groups', ())}
    links = p.links()
    targets = {ln['target'] for ln in links if ln['target'] is not None}
    sel = [p.window(window)] if window is not None else ws
    names = {}
    for w in ws:
        names.setdefault(w['name'], []).append(w['number'])
    for w in sel:
        if w['declared'] != len(w['objects']):
            add('warning', w, None, 'not_parsed', f'{w["declared"]} objects declared, {len(w["objects"])} recognised')
        if w['name'] and len(names.get(w['name'], ())) > 1:
            add('warning', w, None, 'duplicate_name', f'the name «{w["name"]}» is also window '
                f'{", ".join(str(n) for n in names[w["name"]] if n != w["number"])}')
        if w['number'] >= SYSTEM_WINDOWS and w['number'] not in targets and window is None:
            add('info', w, None, 'unreachable', 'no function key leads here (fine for the start window, keypads, '
                'windows opened from the PLC)')
        user = w['number'] >= SYSTEM_WINDOWS
        for o in w['objects']:
            if o.get('_compact'):
                add('warning', w, o['index'], 'opaque_object',
                    f'{o["kind"]} is only recognised as a record boundary; its contents are not decoded')
            if o['kind'] == 'Window' or not o['bbox']:
                continue
            l, t, r, b = o['bbox']
            if user and (r >= w['width'] + 4 or b >= w['height'] + 4):      # a frame drawn 1–3 px over the edge is usual
                add('warning', w, o['index'], 'outside', f'{o["kind"]} {o["bbox"]} goes beyond the window '
                    f'{w["width"]}×{w["height"]} — not seen on the panel' + (' at all' if l >= w['width'] or
                                                                              t >= w['height'] else ' in full'))
            for (at, x, y), (text, font, size) in zip(o['_label_pos'], o['_lab']) if user else ():
                if not text:
                    continue
                width = max(_text_em(line) for line in text.splitlines() or [text]) * size * factor.get(font, 1.35)
                if width > (r - l + 1) * 1.12 + 6:
                    add('warning', w, o['index'], 'text_fit', f'«{text[:40]}» needs about {round(width)} px, '
                        f'the object is {r - l + 1} px wide')
                    break
            for a in o['addresses']:
                if a.get('tag') and tags and a['tag'] not in tags:
                    add('error', w, o['index'], 'tag_missing', f'tag {a["tag"]} is not in the OPC UA tag table')
            fa = fk_action(pay, o)
            if fa and fa[1] in (0, 2) and fa[2] not in nums:
                add('error', w, o['index'], 'link_missing', f'the key opens window {fa[2]}, there is no such window')
            if o['_label_pos']:
                slot = o['_label_pos'][-1][0] + 15
                g = bytes(pay[slot:slot + 16])
                if g in known and g not in groups:
                    add('error', w, o['index'], 'picture_missing', 'a file of its picture is not in the library')
    return out
