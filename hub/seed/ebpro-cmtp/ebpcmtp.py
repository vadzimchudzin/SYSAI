"""ebpcmtp — Weintek EasyBuilder Pro project container toolkit.

Reverse-engineered from EasyBuilder Pro 6.10.02.300 (x86). Supports the
``.cmtp`` (cMT series) and ``.emtp`` (eMT/iE/iP/mTV series) project files.

CONTAINER GRAMMAR (little-endian)
---------------------------------
    u32   revision            inner project revision (e.g. 378 == v6.07.x)
    cstr  version             MFC wide CString: 0xFF 0xFE 0xFF, u8 nchars,
                              then nchars UTF-16LE code units
    u32   revision_min        second revision word. Equal to the first in
                              most files; newer saves (>= ~v6.05) use a
                              slightly higher value here (e.g. 293 / 295,
                              433 / 437), read as a minimum-compatible mark.
    -- if revision >= 240 (current "encrypted" format): --
    u32   payload_len         length of the ciphertext, a multiple of 16
    u8[]  payload             AES-128-ECB(KEY) of the plaintext project blob
    -- else (legacy format, revision <= ~223): --
    u8[]  payload             plaintext project blob, stored verbatim

KEY
---
AES-128, ECB, no padding. The 16-byte key is the ASCII string
``F9FC048E41F94047``. It is embedded verbatim in EasyBuilder Pro.exe,
UtilityManager.dll and HmiSearchWidget.dll (found by decrypting the
most common ciphertext block to a uniform plaintext block).

INNER PROJECT BLOB
------------------
A proprietary MFC CArchive-style stream. A small set of CRuntimeClass tags
(CPartObject, CPortObject, CTagObject, CMail*, CEBRecipeContainerS, ...)
plus a bespoke record stream keyed by decimal "sentinel" magic numbers
(111111 == 0x1B207 per window, 112233 == 0x1B669). Human-facing text is
stored as UTF-16LE runs; bitmaps/photos are stored as a u32 little-endian
byte length immediately followed by the raw PNG/JPEG bytes.

This module gives lossless (byte-identical) unwrap/rewrap, so a project can
be used as a *template* and edited at the content level (labels, window
names, embedded images) with confidence that the file stays valid. Full
object-graph authoring is a separate, larger effort (see README).
"""
from __future__ import annotations
import bisect
import collections
import re
import struct
from dataclasses import dataclass
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

KEY = b'F9FC048E41F94047'
NEW_FORMAT_MIN_REVISION = 240
_CSTR_PREFIX = b'\xff\xfe\xff'


def _aes() -> Cipher:
    return Cipher(algorithms.AES(KEY), modes.ECB())


def _read_cstring(data: bytes, p: int):
    if data[p:p + 3] != _CSTR_PREFIX:
        raise ValueError(f'expected MFC wide CString at offset {p}')
    n = data[p + 3]
    p += 4
    s = data[p:p + 2 * n].decode('utf-16le')
    return s, p + 2 * n


def _write_cstring(s: str) -> bytes:
    if len(s) > 254:
        raise ValueError('version string too long for a u8-length CString')
    return _CSTR_PREFIX + bytes([len(s)]) + s.encode('utf-16le')


@dataclass
class Project:
    """A parsed EasyBuilder Pro project container."""
    revision: int
    version: str
    encrypted: bool
    payload: bytes          # the *plaintext* inner project blob
    header_len: int = 0     # bytes consumed by the container header
    revision_min: int | None = None   # 2nd revision word; None == same as revision

    # -- container round-trip -------------------------------------------------
    @classmethod
    def load(cls, path_or_bytes) -> 'Project':
        data = path_or_bytes if isinstance(path_or_bytes, (bytes, bytearray)) \
            else open(path_or_bytes, 'rb').read()
        data = bytes(data)
        p = 0
        rev1 = struct.unpack_from('<I', data, p)[0]; p += 4
        version, p = _read_cstring(data, p)
        rev2 = struct.unpack_from('<I', data, p)[0]; p += 4
        if rev1 >= NEW_FORMAT_MIN_REVISION:
            payload_len = struct.unpack_from('<I', data, p)[0]; p += 4
            ct = data[p:p + payload_len]
            if len(ct) != payload_len:
                raise ValueError(f'truncated payload: {len(ct)} != {payload_len}')
            if payload_len % 16:
                raise ValueError(f'payload not 16-byte aligned: {payload_len}')
            dec = _aes().decryptor()
            payload = dec.update(ct) + dec.finalize()
            encrypted = True
        else:
            payload = data[p:]
            encrypted = False
        return cls(rev1, version, encrypted, payload, p,
                   None if rev2 == rev1 else rev2)

    def dump(self) -> bytes:
        out = bytearray()
        out += struct.pack('<I', self.revision)
        out += _write_cstring(self.version)
        out += struct.pack('<I', self.revision if self.revision_min is None
                           else self.revision_min)
        if self.encrypted:
            pad = (-len(self.payload)) % 16
            body = self.payload + b'\x00' * pad
            enc = _aes().encryptor()
            ct = enc.update(body) + enc.finalize()
            out += struct.pack('<I', len(ct))
            out += ct
        else:
            out += self.payload
        return bytes(out)

    def save(self, path) -> None:
        open(path, 'wb').write(self.dump())

    # -- content helpers ------------------------------------------------------
    def labels(self):
        """Yield (offset, text) for every UTF-16LE printable run (>=2 chars)."""
        pay = self.payload
        i, n = 0, len(pay)
        while i + 4 <= n:
            j = i
            while j + 1 < n and 0x20 <= pay[j] <= 0x7e and pay[j + 1] == 0:
                j += 2
            if j - i >= 4:
                yield i, pay[i:j].decode('utf-16le')
                i = j
            else:
                i += 2

    def replace_label(self, offset: int, old: str, new: str) -> None:
        """Overwrite a UTF-16LE label in place. Length must match (the inner
        stream stores explicit counts elsewhere, so same-length edits are the
        safe, lossless path)."""
        if len(new) != len(old):
            raise ValueError('replacement must have the same length as the original')
        b = bytearray(self.payload)
        exp = old.encode('utf-16le')
        if bytes(b[offset:offset + len(exp)]) != exp:
            raise ValueError('old text not found at the given offset')
        b[offset:offset + len(exp)] = new.encode('utf-16le')
        self.payload = bytes(b)

    def images(self):
        """Yield dicts for every length-prefixed embedded bitmap (PNG/JPEG).
        Each: {offset, length, kind}. Offset points at the image bytes; the
        u32 length sits at offset-4."""
        pay = self.payload
        for sig, kind in ((b'\x89PNG\r\n\x1a\n', 'png'), (b'\xff\xd8\xff', 'jpg')):
            i = 0
            while True:
                i = pay.find(sig, i)
                if i < 0:
                    break
                if i >= 4:
                    ln = struct.unpack_from('<I', pay, i - 4)[0]
                    if 0 < ln <= len(pay) - i:
                        yield {'offset': i, 'length': ln, 'kind': kind}
                        i += ln
                        continue
                i += 1

    # -- windows and objects (EasyBuilder Pro 6.10 inner blob) ------------------
    def windows(self) -> list:
        """Windows in stream order (the position is the EasyBuilder window number
        in the checked projects), each with its objects -- see parse_windows()."""
        cache = getattr(self, '_windows_cache', None)
        if cache is None or cache[0] is not self.payload:
            cache = (self.payload, parse_windows(self.payload))
            self._windows_cache = cache
        return cache[1]

    def window(self, ref) -> dict:
        """A window by exact name, or by number (int or digit string)."""
        ws = self.windows()
        for w in ws:
            if w['name'] == ref:
                return w
        if isinstance(ref, int) or (isinstance(ref, str) and ref.strip().isdigit()):
            n = int(ref)
            if 0 <= n < len(ws):
                return ws[n]
        raise ValueError(f'no window {ref!r} in the project')

    def opcua_tags(self) -> list:
        """The OPC UA client tag table: [{name, type, node}]."""
        return parse_opcua_tags(self.payload)

    def _splice(self, edits) -> None:
        pay, out, last = self.payload, [], 0
        for s, e, new in sorted(edits):
            if s < last:
                raise ValueError('overlapping edits')
            out += [pay[last:s], new]
            last = e
        out.append(pay[last:])
        self.payload = b''.join(out)

    def _objects(self, window, index=None):
        w = self.window(window)
        if index is None:
            return w, w['objects']
        if not 0 <= int(index) < len(w['objects']):
            raise ValueError(f'window {w["name"]!r} has no object #{index}')
        return w, [w['objects'][int(index)]]

    def edit_text(self, window, old: str, new: str, index=None) -> int:
        """Replace a text of a window (a Text, a state label, a title...) with a
        text of ANY length; EasyBuilder re-fits the frame on load (checked in
        6.10). index limits it to one object. Returns how many were replaced."""
        w, objs = self._objects(window, index)
        hits = [(s, e) for o in objs for s, e, t in o['_texts'] if t == old]
        if not hits:
            raise ValueError(f'text {old!r} not found in window {w["name"]!r}')
        self._splice([(s, e, cstring(new)) for s, e in hits])
        return len(hits)

    def move_object(self, window, index: int, dx: int, dy: int) -> list:
        """Move one object: its frame and the label positions inside it (the
        same bytes EasyBuilder itself changes on a drag). Returns the new frame."""
        w, (o,) = self._objects(window, index)
        if o['bbox'] is None or o['kind'] == 'Window':
            raise ValueError(f'object #{index} of {w["name"]!r} has no movable frame')
        l, t, r, b = o['bbox']
        nb = [l + dx, t + dy, r + dx, b + dy]
        if min(nb) < 0:
            raise ValueError('the object would get negative coordinates')
        edits = [(o['_bbox_at'], o['_bbox_at'] + 16, struct.pack('<IIII', *nb))]
        for at, x, y in o['_label_pos']:
            if l - 5 <= x <= r + 5 and t - 5 <= y <= b + 5:
                edits.append((at, at + 8, struct.pack('<II', x + dx, y + dy)))
        self._splice(edits)
        return nb

    def retag(self, window, old: str, new: str, index=None) -> int:
        """Re-point objects of a window from OPC UA tag `old` to tag `new` (tag
        names, e.g. GVL.START_3 -> GVL.START_11). The new tag must be in the
        project's OPC UA tag table with the same data type. Returns the count of
        replaced addresses (read and write slots count separately)."""
        tags = {t['name']: t for t in self.opcua_tags()}
        if new not in tags:
            raise ValueError(f'tag {new!r} is not in the OPC UA tag table of the project')
        if old in tags and tags[old]['type'] != tags[new]['type']:
            raise ValueError(f'type mismatch: {old} is {tags[old]["type"]}, {new} is {tags[new]["type"]}')
        w, objs = self._objects(window, index)
        hits = [(s, e) for o in objs for s, e, t in o['_addr'] if opc_tag_name(t) == old]
        if not hits:
            raise ValueError(f'tag {old!r} is not used in window {w["name"]!r}')
        self._splice([(s, e, cstring(f'{new} ({tags[new]["node"]})')) for s, e in hits])
        return len(hits)

    def rename_window(self, window, new: str) -> None:
        """Rename a window (the name in the window list); any length."""
        w = self.window(window)
        r = _cstr_at(self.payload, w['offset'])
        if r is None or r[0] != w['name']:
            raise ValueError('window name not where expected')
        self._splice([(w['offset'], r[1], cstring(new))])


# Convenience module-level API --------------------------------------------------
def unwrap(data) -> Project:
    return Project.load(data)


# =============================================================================
# Windows and objects -- EasyBuilder Pro 6.10 inner blob (reverse-engineered)
# =============================================================================
# The blob is an MFC CArchive. CPartObject (widgets) and CDrawObject (shapes)
# records are written with WriteObject: the first occurrence carries the class
# definition (FF FF, schema, name), later ones the class tag 0x8000|index. Each
# class AND each object takes the next map index, so inserting objects would
# shift every later class index -- this module only edits in place.
#
# Window:  cstring name, u32 width, u32 height, 10 x 00, 01 00 00 00 00 00,
#          16-byte GUID, u16 len + layer name (UTF-8), u32 object count, objects.
# Widget:  tag, u8 type, cstring name, 12 bytes, 3 x cstring device,
#          20 x 00 + 3 address-type bytes, 3 x cstring address (1 read, 2 write,
#          3 extra), type-specific data with the frame (4 x u32 l, t, r, b after
#          4 zero bytes, then 00 + colours) and labels (cstring text, cstring
#          font, u8 size, u32 x, u32 y).
# Shape:   tag, u8 shape (2 line, 4 rect), ..., frame 4 x u32 at +31 after the tag.
# OPC UA tag table: 00 03 'TAG', u16be len + 'name (ns:2;s:id)(id)', u16be len + type.

PART_KINDS = {0x2d: 'Window', 0x15: 'Text', 0x16: 'Picture', 0x17: 'BitLamp', 0x18: 'MultiState?',
              0x19: 'SetBit', 0x1a: 'SetWord?', 0x1b: 'ToggleSwitch', 0x1d: 'NumericInput',
              0x1e: 'NumericDisplay', 0x1f: 'ASCII?', 0x20: 'ASCII', 0x21: 'FunctionKey', 0x24: 'WordLamp?',
              0x29: 'Trend?', 0x40: 'OptionList?', 0x50: 'QRCode'}
DRAW_KINDS = {2: 'Line', 4: 'Rect'}
_PART_DEF = b'\xff\xff\x01\x00\x0b\x00CPartObject'
_DRAW_DEF = b'\xff\xff\x01\x00\x0b\x00CDrawObject'
_CLASS_DEF = re.compile(rb'\xff\xff\x01\x00[\x03-\x40]\x00C[A-Za-z]')
_DRAW_SIG = rb'([\x00-\x40])[\x00-\xff]\x00\x00\x00\x01'
_OPC = re.compile(r'^(?P<name>.+?) \((?P<node>ns[:=]\d+;.+)\)$')
_FONT = re.compile(r'^[A-Za-z][A-Za-z0-9 _\-]{1,48}$')


def cstring(s: str) -> bytes:
    """Encode an MFC wide CString (FF FE FF, length u8 | FF u16 | FF FFFF u32, UTF-16LE)."""
    raw = s.encode('utf-16le')
    n = len(raw) // 2
    if n < 0xFF:
        ln = bytes([n])
    elif n < 0xFFFE:
        ln = b'\xff' + struct.pack('<H', n)
    else:
        ln = b'\xff\xff\xff' + struct.pack('<I', n)
    return _CSTR_PREFIX + ln + raw


def _cstr_at(pay: bytes, i: int):
    """(text, end) of the MFC wide CString at i, or None."""
    if pay[i:i + 3] != _CSTR_PREFIX or i + 4 > len(pay):
        return None
    j = i + 3
    n = pay[j]
    j += 1
    if n == 0xFF:
        if j + 2 > len(pay):
            return None
        n = struct.unpack_from('<H', pay, j)[0]
        j += 2
        if n == 0xFFFF:
            if j + 4 > len(pay):
                return None
            n = struct.unpack_from('<I', pay, j)[0]
            j += 4
    if n > 1_000_000 or j + 2 * n > len(pay):
        return None
    try:
        return pay[j:j + 2 * n].decode('utf-16le'), j + 2 * n
    except UnicodeDecodeError:
        return None


def cstrings(pay: bytes, start: int = 0, end=None) -> list:
    """Every MFC wide CString in pay[start:end] as (offset, end, text)."""
    end = len(pay) if end is None else end
    out = []
    i = pay.find(_CSTR_PREFIX, start, end)
    while i != -1:
        r = _cstr_at(pay, i)
        if r is None:
            i = pay.find(_CSTR_PREFIX, i + 1, end)
        else:
            out.append((i, r[1], r[0]))
            i = pay.find(_CSTR_PREFIX, r[1], end)
    return out


def opc_tag_name(address: str):
    """'GVL.X (ns:2;s:GVL.X)' -> 'GVL.X'; None if it is not an OPC UA address."""
    m = _OPC.match(address)
    return m.group('name') if m else None


def parse_opcua_tags(pay: bytes) -> list:
    out, i = [], pay.find(b'\x00\x03TAG')
    while i != -1:
        p = i + 5
        n = int.from_bytes(pay[p:p + 2], 'big')
        full = pay[p + 2:p + 2 + n]
        q = p + 2 + n
        m = int.from_bytes(pay[q:q + 2], 'big')
        typ = pay[q + 2:q + 2 + m]
        mm = re.match(rb'^(.+?) \((ns[:=]\d+;.+?)\)\((.*)\)$', full)
        if mm and 0 < m < 40:
            out.append({'name': mm.group(1).decode('utf-8', 'replace'), 'type': typ.decode('ascii', 'replace'),
                        'node': mm.group(2).decode('utf-8', 'replace')})
        i = pay.find(b'\x00\x03TAG', i + 5)
    return out


def _heads(pay: bytes) -> list:
    """(offset, class, type, header length) of every widget / shape record."""
    fp = pay.find(_PART_DEF)
    if fp == -1:
        return []
    base = 1 + 2 * len(_CLASS_DEF.findall(pay, 0, fp))       # map index of CPartObject
    ptag = struct.pack('<H', 0x8000 | base)
    prefs = [m.start() for m in re.finditer(re.escape(ptag) + rb'.\xff\xfe\xff', pay, re.S)]
    heads = [(fp, 'P', pay[fp + len(_PART_DEF)], len(_PART_DEF) + 1)] + [(k, 'P', pay[k + 2], 3) for k in prefs]
    fd = pay.find(_DRAW_DEF)
    if fd != -1:
        heads.append((fd, 'D', pay[fd + len(_DRAW_DEF)], len(_DRAW_DEF)))
        # CDrawObject index = CPartObject + every widget written before its definition + 1
        dtag = struct.pack('<H', 0x8000 | (base + 1 + 1 + bisect.bisect_left(prefs, fd)))
        drefs = [m.start() for m in re.compile(re.escape(dtag) + _DRAW_SIG).finditer(pay, fd)]
        if not drefs:   # unexpected layout: take the most frequent class tag in front of a shape signature
            c = collections.Counter(pay[m.start():m.start() + 2]
                                    for m in re.compile(rb'[\x01-\xff][\x80-\xbf]' + _DRAW_SIG).finditer(pay, fd))
            if c:
                dtag = c.most_common(1)[0][0]
                drefs = [m.start() for m in re.compile(re.escape(dtag) + _DRAW_SIG).finditer(pay, fd)]
        heads += [(k, 'D', pay[k + 2], 2) for k in drefs]
    return sorted(heads)


def _window_header(pay: bytes, e: int):
    if e + 46 > len(pay):
        return None
    w, h = struct.unpack_from('<II', pay, e)
    if not (16 <= w <= 8000 and 16 <= h <= 8000):
        return None
    if pay[e + 8:e + 18] != bytes(10) or pay[e + 18:e + 24] != b'\x01\x00\x00\x00\x00\x00':
        return None
    ln = struct.unpack_from('<H', pay, e + 40)[0]
    if ln > 64 or e + 46 + ln > len(pay):
        return None
    try:
        layer = pay[e + 42:e + 42 + ln].decode('utf-8')
    except UnicodeDecodeError:
        return None
    count = struct.unpack_from('<I', pay, e + 42 + ln)[0]
    if count > 100000:
        return None
    return {'width': w, 'height': h, 'guid': pay[e + 24:e + 40].hex(), 'layer': layer, 'declared': count,
            '_first': e + 46 + ln}


def _part_head(pay: bytes, p: int):
    """Common widget head -> (dict or None, offset after it)."""
    r = _cstr_at(pay, p)
    if r is None:
        return None, p
    name, p = r
    p += 12
    devices = []
    for _ in range(3):
        r = _cstr_at(pay, p)
        if r is None:
            return None, p
        devices.append(r[0])
        p = r[1]
    atypes = pay[p + 20:p + 23]
    p += 23
    addrs = []
    for _ in range(3):
        r = _cstr_at(pay, p)
        if r is None:
            return None, p
        addrs.append((p, r[1], r[0]))
        p = r[1]
    return {'name': name, 'devices': devices, 'atypes': atypes, 'addrs': addrs}, p


def _find_bbox(pay: bytes, p: int, end: int):
    fallback = (None, None)
    for k in range(max(p, 4), min(end, p + 2500) - 24):
        if pay[k + 16] != 0 or pay[k - 4:k] != b'\x00\x00\x00\x00':
            continue
        l, t, r, b = struct.unpack_from('<IIII', pay, k)
        if l <= r <= 4000 and t <= b <= 4000 and r - l >= 3 and b - t >= 3:
            col = pay[k + 17:k + 24]
            if any(col[:3]) or any(col[4:7]):
                return k, [l, t, r, b]
            if fallback[0] is None:
                fallback = (k, [l, t, r, b])
    return fallback


def _address(slot, device, atype, address):
    a = {'slot': slot, 'device': device, 'atype': f'{atype:02x}' if atype is not None else None, 'address': address}
    m = _OPC.match(address)
    if m:
        a['tag'], a['node'] = m.group('name'), m.group('node')
    return a


def _parse_object(pay, cs, starts, o, end, cls, typ, hl, index) -> dict:
    obj = {'index': index, 'kind': None, 'type': typ, 'bbox': None, 'labels': [], 'texts': [], 'addresses': [],
           'offset': o, 'length': end - o, '_bbox_at': None, '_texts': [], '_addr': [], '_label_pos': []}
    if cls == 'D':
        obj['kind'] = DRAW_KINDS.get(typ, f'draw_{typ}')
        at = o + hl + 31
        if at + 16 <= end:
            obj['bbox'], obj['_bbox_at'] = list(struct.unpack_from('<IIII', pay, at)), at
        body = min(end, at + 16)
    else:
        obj['kind'] = PART_KINDS.get(typ, f'part_{typ:02x}')
        head, body = _part_head(pay, o + hl)
        if head:
            for k, (s, e, a) in enumerate(head['addrs']):
                obj['_addr'].append((s, e, a))
                if a not in ('', '0'):
                    obj['addresses'].append(_address(k + 1, head['devices'][k], head['atypes'][k], a))
        at, bb = _find_bbox(pay, body, end)
        obj['bbox'], obj['_bbox_at'] = bb, at
    a = bisect.bisect_left(starts, body)
    strs = []
    while a < len(cs) and cs[a][0] < end:
        strs.append(cs[a])
        a += 1
    i = 0
    while i < len(strs):
        s, e, t = strs[i]
        nxt = strs[i + 1] if i + 1 < len(strs) else None
        if nxt and nxt[0] == e and _FONT.match(nxt[2]) and nxt[1] + 9 <= end:
            size = pay[nxt[1]]
            x, y = struct.unpack_from('<II', pay, nxt[1] + 1)
            if 1 <= size and x <= 8000 and y <= 8000:
                obj['_texts'].append((s, e, t))
                obj['_label_pos'].append((nxt[1] + 1, x, y))
                if t:
                    obj['labels'].append({'text': t, 'font': nxt[2], 'size': size, 'x': x, 'y': y})
                i += 2
                continue
        if _OPC.match(t):
            obj['_addr'].append((s, e, t))
            obj['addresses'].append(_address(0, None, None, t))
        elif t:
            obj['_texts'].append((s, e, t))
            obj['texts'].append(t)
        i += 1
    return obj


def parse_windows(pay: bytes) -> list:
    """Every window with its objects. Object dicts: index, kind, type, bbox
    [l, t, r, b], labels [{text, font, size, x, y}], texts, addresses
    [{slot, device, atype, address, tag?, node?}], offset, length (+ private
    _fields used by the editors)."""
    cs = cstrings(pay)
    starts = [c[0] for c in cs]
    heads = _heads(pay)
    hoffs = [h[0] for h in heads]
    wins = []
    for o, e, name in cs:
        hdr = _window_header(pay, e)
        if hdr:
            wins.append({'number': len(wins), 'name': name, 'offset': o, **hdr})
    for i, w in enumerate(wins):
        end = wins[i + 1]['offset'] if i + 1 < len(wins) else len(pay)
        a, z = bisect.bisect_left(hoffs, w['_first']), bisect.bisect_left(hoffs, end)
        if i + 1 == len(wins) and z > a:    # the last window ends where the settings start (a long zero run)
            zr = re.compile(rb'\x00{256,}').search(pay, heads[z - 1][0])
            end = zr.start() if zr else end
        objs = []
        for k in range(a, z):
            o, cls, typ, hl = heads[k]
            oe = heads[k + 1][0] if k + 1 < z else end
            objs.append(_parse_object(pay, cs, starts, o, oe, cls, typ, hl, len(objs)))
        w['objects'] = objs
        w['_end'] = end
    return wins


def public(d: dict) -> dict:
    """A window / object dict without the private editor fields and offsets."""
    return {k: v for k, v in d.items() if not k.startswith('_') and k not in ('offset', 'length', 'objects')}
