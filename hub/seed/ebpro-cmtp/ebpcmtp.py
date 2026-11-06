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
import math
import os
import re
import struct
import uuid
import zlib
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
        """Written whole or not at all: a temporary file next to it, then put in its place."""
        path = os.fspath(path)
        tmp = f'{path}.sysai-tmp'
        with open(tmp, 'wb') as f:
            f.write(self.dump())
        try:
            os.replace(tmp, path)
        except OSError:
            os.remove(tmp)
            raise

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

    # -- the look of a window: to draw it as the panel shows it -----------------
    def _library(self):
        cache = getattr(self, '_lib_cache', None)
        if cache is None or cache[0] is not self.payload:
            lib, start, end, n = _library_scan(self.payload)
            sec = picture_section(self.payload, end) if lib else None
            groups = {g['key']: g['states'] for g in (sec or {}).get('groups', ())
                      if g['states'] and all(f in lib for f, _ in g['states'])}
            cache = (self.payload, lib, groups, sec, (start, end, n))
            self._lib_cache = cache
        return cache[1], cache[2]

    def picture_section(self):
        """The picture objects (see picture_section()) and the library span (start, end, entries)."""
        self._library()
        return self._lib_cache[3], self._lib_cache[4]

    def pictures(self) -> dict:
        """The picture library: {GUID hex: {offset, length, format}}."""
        lib, _ = self._library()
        return {g.hex(): {'offset': o, 'length': n, 'format': picture_format(self.payload[o:o + 4])}
                for g, (o, n) in lib.items()}

    def picture(self, guid: str, transparent: str = None):
        """(bytes, format) of a library picture; a bitmap with a transparent colour comes as PNG with alpha."""
        lib, _ = self._library()
        try:
            o, n = lib[bytes.fromhex(guid)]
        except (KeyError, ValueError):
            raise ValueError(f'no picture {guid} in the project') from None
        data = self.payload[o:o + n]
        kind = picture_format(data)
        if kind == 'bmp':
            try:
                return bmp_png(data, transparent), 'png'
            except (ValueError, IndexError, struct.error):
                return data, kind
        return data, kind

    def scene(self, window, images_dir=None) -> dict:
        """How the window looks on the panel: size and background colour; per object (in drawing order) its
        frame, the colours of a shape (fill — None when not filled, frame, frame width), its labels state by
        state (text, font, size in points, x, y, colour, alignment), the pictures of its states (library
        files, transparent colour) or `shape` — an EasyBuilder library shape the project does not keep.
        images_dir: the window's pictures are written there (<GUID>[-<colour>].<ext>), `images` maps them."""
        w = self.window(window)
        _, groups = self._library()
        objs = [_look(self.payload, o, w, groups) for o in w['objects']]
        bg = next((o.get('fill') for o in objs if o['kind'] == 'Window' and o.get('fill')), None)
        out = {'window': {'number': w['number'], 'name': w['name'], 'width': w['width'], 'height': w['height'],
                          'background': bg}, 'objects': objs, 'images': {}}
        for o in objs:
            for st in o.get('picture') or ():
                st['key'] = st['image'] + ('-' + st['transparent'][1:] if st['transparent'] else '')
        if images_dir is not None:
            os.makedirs(images_dir, exist_ok=True)
            for o in objs:
                for st in o.get('picture') or ():
                    key = st['key']
                    if key in out['images']:
                        continue
                    data, kind = self.picture(st['image'], st['transparent'])
                    path = os.path.join(images_dir, f'{key}.{kind}')
                    if not os.path.exists(path) or os.path.getsize(path) != len(data):
                        with open(path, 'wb') as f:
                            f.write(data)
                    out['images'][key] = path
        return out

    # -- windows and objects (EasyBuilder Pro 6.10 inner blob) ------------------
    def windows(self) -> list:
        """Windows in stream order, each with its EasyBuilder number (counted by
        the number slots, empty numbers included) and objects -- see parse_windows()."""
        cache = getattr(self, '_windows_cache', None)
        if cache is None or cache[0] is not self.payload:
            cache = (self.payload, parse_windows(self.payload))
            self._windows_cache = cache
        return cache[1]

    def window(self, ref) -> dict:
        """A window by exact name, or by its EasyBuilder number (int or digit string)."""
        ws = self.windows()
        for w in ws:
            if w['name'] == ref:
                return w
        if isinstance(ref, int) or (isinstance(ref, str) and ref.strip().isdigit()):
            n = int(ref)
            for w in ws:
                if w['number'] == n:
                    return w
        raise ValueError(f'no window {ref!r} in the project')

    def links(self, window=None) -> list:
        """Function keys that open windows: [{window, object, action (window/popup/back/close), target}]."""
        out = []
        for w in ([self.window(window)] if window is not None else self.windows()):
            for o in w['objects']:
                fa = fk_action(self.payload, o)
                if fa and fa[1] in (0, 1, 2, 3):
                    act = FK_ACTIONS[fa[1]]
                    out.append({'window': w['number'], 'object': o['index'], 'action': act,
                                'target': fa[2] if fa[1] in (0, 2) else None,
                                'labels': [lab['text'] for lab in o['labels']][:2]})
        return out

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
        text of ANY length. EasyBuilder keeps the frame as it is, so the frame of a
        Text object is widened for a longer text (estimated from the character
        widths). index limits it to one object. Returns how many were replaced."""
        w, objs = self._objects(window, index)
        hits = [(s, e) for o in objs for s, e, t in o['_texts'] if t == old]
        if not hits:
            raise ValueError(f'text {old!r} not found in window {w["name"]!r}')
        edits = [(s, e, cstring(new)) for s, e in hits]
        ratio = _text_em(new) / max(_text_em(old), 0.3)
        for o in objs:
            if o['kind'] == 'Text' and o['bbox'] and ratio > 1 and any(t == old for _, _, t in o['_texts']):
                l, t, r, b = o['bbox']
                edits.append((o['_bbox_at'], o['_bbox_at'] + 16,
                              struct.pack('<IIII', l, t, l + math.ceil((r - l) * ratio) + 4, b)))
        self._splice(edits)
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
                edits.append((at, at + 8, struct.pack('<ii', x + dx, y + dy)))
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

    # -- new windows and objects (copies of existing ones) ------------------------
    def clone_window(self, src, name=None, number=None) -> int:
        """Copy a window (all its objects) to a free window number (default: the
        first one after the last window), optionally under a new name. The copy
        gets a new GUID, like EasyBuilder's own copy. Returns the new number."""
        pay, ws = self.payload, self.windows()
        w = self.window(src)
        if w['declared'] != len(w['objects']):
            raise ValueError(f'window {w["name"]!r}: not every object is recognised, copying it is not safe')
        if not w.get('_end_sure'):
            raise ValueError(f'window {w["name"]!r}: where its record ends is not known, copying it is not safe')
        used = {x['number']: x for x in ws}
        number = max(used) + 1 if number is None else int(number)
        if number in used:
            raise ValueError(f'window number {number} is taken by {used[number]["name"]!r}')
        if not 3 <= number <= 1999:
            raise ValueError('window numbers go from 3 to 1999')
        prev = max((x for x in ws if x['number'] < number), key=lambda x: x['number'])
        if not prev.get('_end_sure'):
            raise ValueError(f'window {prev["number"]}: where its record ends is not known, placing window '
                             f'{number} after it is not safe; take another number')
        nxt = min((x for x in ws if x['number'] > number), key=lambda x: x['number'], default=None)
        at = prev['_rec_end'] + 4 * (number - prev['number'] - 1)
        if nxt is not None:
            limit = nxt['offset'] - 4
        else:   # the empty slots after the last window: keep clear of the settings after them
            limit = re.compile(rb'\x00*').match(pay, prev['_rec_end']).end() - 1024
        if at + 4 > limit or pay[at:at + 4] != bytes(4):
            raise ValueError(f'no free slot for window number {number}')
        info = _map_info(pay, ws)
        rec = _clone_bytes(pay, w['offset'], w['_rec_end'], info)
        body = bytearray(rec[_cstr_at(rec, 0)[1]:])
        body[24:40] = uuid.uuid4().bytes
        data = _SLOT + cstring(w['name'] if name is None else name) + bytes(body)
        self._insert(info, [(at, 4, data, w['declared'])])
        if not any(x['number'] == number and x['guid'] == body[24:40].hex() for x in self.windows()):
            self.payload = pay      # the empty slots before it cannot be told from the end of the window before
            raise ValueError(f'window number {number} cannot be placed reliably; take {prev["number"] + 1}')
        return number

    def clone_object(self, window, index: int, to=None, dx: int = 0, dy: int = 0) -> int:
        """Copy one object (tags, texts, look) to the end of the object list of the
        same window or of window `to`, shifted by dx, dy. Returns the copy's index."""
        pay, ws = self.payload, self.windows()
        w, (o,) = self._objects(window, index)
        if o['kind'] == 'Window':
            raise ValueError('the window object itself cannot be copied (use clone_window)')
        d = w if to is None else self.window(to)
        for x in (w, d):
            if x['declared'] != len(x['objects']):
                raise ValueError(f'window {x["name"]!r}: not every object is recognised, inserting is not safe')
        if o is w['objects'][-1] and not w.get('_end_sure'):
            raise ValueError(f'window {w["name"]!r}: where its record ends is not known, copying its last object '
                             f'is not safe')
        ins, pos = insert_point(d)
        info = _map_info(pay, ws)
        s, e = o['offset'], o['offset'] + o['length']
        rec = bytearray(_clone_bytes(pay, s, e, info))
        cut = (e - s) - len(rec)        # a class definition at the start became a 2-byte reference
        if dx or dy:
            if o['bbox'] is None:
                raise ValueError(f'object #{index} has no frame to shift')
            l, t, r, b = o['bbox']
            nb = [l + dx, t + dy, r + dx, b + dy]
            if min(nb) < 0:
                raise ValueError('the copy would get negative coordinates')
            struct.pack_into('<IIII', rec, o['_bbox_at'] - s - cut, *nb)
            for at, x, y in o['_label_pos']:
                if l - 5 <= x <= r + 5 and t - 5 <= y <= b + 5:
                    struct.pack_into('<ii', rec, at - s - cut, x + dx, y + dy)
        cnt = d['_first'] - 4
        self._insert(info, [(ins, 0, bytes(rec), 1)],
                     [(cnt, cnt + 4, struct.pack('<I', d['declared'] + 1))])
        return pos

    def _insert(self, info, inserts, extra=()) -> None:
        """Splice records in or out ([(offset, bytes replaced, data, map entries — negative for removed ones)]) and
        renumber the classes defined after them; checked after the fact."""
        old = self.payload
        edits = _renumber_edits(old, info, [(at, k) for at, _, _, k in inserts])
        self._splice(edits + [(at, at + rm, data) for at, rm, data, _ in inserts] + list(extra))
        try:
            new = _map_info(self.payload, self.windows())
            if new['counts'] != info['counts']:
                raise ValueError('class references do not add up after the insert')
        except ValueError as exc:
            self.payload = old
            raise ValueError(f'{exc}; the project is left unchanged') from None


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


# OPC UA client tag table: u32 count (some 70 bytes before the first entry, after the server address), then the
# entries one after another: u16be 3 'TAG', u16be length + 'name (ns:2;s:id)(id)', u8 length + description,
# u8 length + type (BOOL, INT, UINT, UDINT...), u32 0, u32 500, 7 x 00, 01, u16be namespace, u16be length +
# identifier, u32 0. The last one is EasyBuilder's own «UaStatus Code» (ns 10).
_TAG = b'\x00\x03TAG'
_FULL = re.compile(rb'^(.+?) \((ns[:=]\d+;.+?)\)(?:\((.*)\))?$', re.S)


def _tag_entry(pay: bytes, i: int):
    """The tag table entry at i as a dict (start, end, name, type, node, ns, id), None if it is not one."""
    p = i + 5
    if pay[i:p] != _TAG or p + 2 > len(pay):
        return None
    n = int.from_bytes(pay[p:p + 2], 'big')
    full = pay[p + 2:p + 2 + n]
    p += 2 + n
    if p + 1 > len(pay):
        return None
    p += 1 + pay[p]                                   # description
    if p + 1 > len(pay):
        return None
    tl = pay[p]
    typ = pay[p + 1:p + 1 + tl]
    p += 1 + tl + 16
    if not 0 < tl < 40 or p + 4 > len(pay) or pay[p - 1] != 1:
        return None
    mid = bytes(pay[p - 16:p])
    ns = int.from_bytes(pay[p:p + 2], 'big')
    il = int.from_bytes(pay[p + 2:p + 4], 'big')
    ident = pay[p + 4:p + 4 + il]
    p += 4 + il + 4
    m = _FULL.match(full)
    if not m or p > len(pay):
        return None
    try:
        return {'start': i, 'end': p, 'name': m.group(1).decode('utf-8'), 'type': typ.decode('ascii'),
                'node': m.group(2).decode('utf-8'), 'ns': ns, 'id': ident.decode('utf-8'), 'mid': mid}
    except UnicodeDecodeError:
        return None


def opcua_table(pay: bytes):
    """{'count_at', 'count', 'entries': [...], 'end'} of the OPC UA tag table, None if there is none or it does
    not hold together (count, entries one after another)."""
    i = pay.find(_TAG)
    if i == -1:
        return None
    entries = []
    while True:
        e = _tag_entry(pay, i)
        if e is None:
            break
        entries.append(e)
        i = e['end']
    if not entries:
        return None
    first = entries[0]['start']
    for back in range(4, 400):
        if first - back >= 0 and struct.unpack_from('<I', pay, first - back)[0] == len(entries):
            return {'count_at': first - back, 'count': len(entries), 'entries': entries, 'end': entries[-1]['end']}
    return None


def parse_opcua_tags(pay: bytes) -> list:
    tab = opcua_table(pay)
    if tab is not None:
        return [{'name': e['name'], 'type': e['type'], 'node': e['node']} for e in tab['entries']]
    out, i = [], pay.find(_TAG)     # not one table: every entry that reads
    while i != -1:
        e = _tag_entry(pay, i)
        if e:
            out.append({'name': e['name'], 'type': e['type'], 'node': e['node']})
        i = pay.find(_TAG, i + 5)
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
    devices, devs = [], []
    for _ in range(3):
        r = _cstr_at(pay, p)
        if r is None:
            return None, p
        devices.append(r[0])
        devs.append((p, r[1], r[0]))
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
    return {'name': name, 'devices': devices, 'devs': devs, 'atypes': atypes, 'addrs': addrs}, p


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
           'offset': o, 'length': end - o, '_bbox_at': None, '_texts': [], '_addr': [], '_label_pos': [],
           '_lab': [], '_cls': cls, '_hl': hl, '_dev': []}
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
            obj['_dev'] = head['devs']
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
            x, y = struct.unpack_from('<ii', pay, nxt[1] + 1)     # a label may stand left of / above the window
            if 1 <= size and -8000 <= x <= 8000 and -8000 <= y <= 8000:
                obj['_texts'].append((s, e, t))
                obj['_label_pos'].append((nxt[1] + 1, x, y))
                obj['_lab'].append((t, nxt[2], size))
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
    _fields used by the editors). Window numbers follow the slot table: an
    unused number is an empty 4-byte slot between two windows."""
    cs = cstrings(pay)
    starts = [c[0] for c in cs]
    heads = _heads(pay)
    hoffs = [h[0] for h in heads]
    wins = []
    for o, e, name in cs:
        hdr = _window_header(pay, e)
        if hdr:
            wins.append({'number': len(wins), 'position': len(wins), 'name': name, 'offset': o, **hdr})
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
    _number_windows(pay, wins)
    return wins


# Window slots. From window 3 on every used window number is the flag 01 00 00 00 + the window
# record, an unused number is 00 00 00 00 (checked against EasyBuilder 6.10: «Пневмозаслонки» is
# 78 and «Screen Saver» 80 with one empty slot after each). A record ends after its last object;
# an object ends a fixed number of bytes after its last label (per object type, learnt from the
# objects that are followed by another object), which tells the record end from empty slots.
_SLOT = b'\x01\x00\x00\x00'


def _object_tails(wins) -> dict:
    t = collections.defaultdict(collections.Counter)
    for w in wins:
        for o in w['objects'][:-1]:
            if o['_label_pos']:
                at = max(p[0] for p in o['_label_pos'])
                t[(o['kind'], o['type'])][o['offset'] + o['length'] - (at + 8)] += 1
    return t


def _record_end(pay, w, limit, tails, last):
    """(record end, empty slots between it and limit, whether the end is known) for window w."""
    o = w['objects'][-1] if w['objects'] else None
    # no zeros right before the limit: no empty slot there, the record ends at the limit
    plain = not last and limit >= 4 and pay[limit - 4:limit] != bytes(4)
    if not o or not o['_label_pos'] or w['declared'] != len(w['objects']):
        return limit, 0, plain
    base = max(p[0] for p in o['_label_pos']) + 8
    meas = limit - base
    cands = sorted(tails.get((o['kind'], o['type']), ()))
    if last:
        ge = [t for t in cands if t >= meas and not any(pay[limit:base + t])]
        if ge:
            return base + ge[0], 0, True
    ok = [t for t in cands if t <= meas and (meas - t) % 4 == 0 and not any(pay[base + t:limit])]
    if not ok:
        return limit, 0, plain
    return base + ok[-1], (meas - ok[-1]) // 4, True


def _number_windows(pay, wins) -> None:
    tails, n = _object_tails(wins), 0
    for i, w in enumerate(wins):
        last = i + 1 == len(wins)
        flag = not last and pay[wins[i + 1]['offset'] - 4:wins[i + 1]['offset']] == _SLOT
        limit = w['_end'] if last else wins[i + 1]['offset'] - (4 if flag else 0)
        end, gap, sure = _record_end(pay, w, limit, tails, last)
        w['number'], w['_rec_end'], w['_end_sure'] = n, end, sure and (flag or last)
        if w['objects']:    # the last object ends with the record, not at the next window
            lo = w['objects'][-1]
            lo['length'] = max(0, end - lo['offset'])
        n += 1 + (gap if flag else 0)


def insert_point(w: dict):
    """(offset, index) where new objects go into window w: after its last object; when where its record ends is
    not known — right before the last object (its start is known; the last object stays on top)."""
    if w.get('_end_sure'):
        return w['_rec_end'], len(w['objects'])
    if len(w['objects']) < 2:
        raise ValueError(f'window {w["name"]!r}: where its record ends is not known, inserting is not safe')
    return w['objects'][-1]['offset'], len(w['objects']) - 1


def public(d: dict) -> dict:
    """A window / object dict without the private editor fields and offsets."""
    return {k: v for k, v in d.items()
            if not k.startswith('_') and k not in ('offset', 'length', 'objects', 'position')}


# A FunctionKey (EasyBuilder 6.08.02 and later) keeps what it does right after one of its strings: u8 action,
# 0x0d, u16 value, 08 00. Action 0 — change the full-screen window to window <value>, 2 — open the pop-up window
# <value>, 3 — close this pop-up, 1 — back to the previous window, 8 — a key of a keypad (value — the character).
FK_ACTIONS = {0: 'window', 1: 'back', 2: 'popup', 3: 'close', 8: 'key'}


def fk_action(pay: bytes, o: dict):
    """(offset of the action byte, action, value) of a FunctionKey object; None — not found or not one."""
    if o.get('kind') != 'FunctionKey':
        return None
    end = o['offset'] + o['length']
    hits = [(e, pay[e], struct.unpack_from('<H', pay, e + 2)[0]) for _, e, _ in cstrings(pay, o['offset'], end)
            if e + 6 <= end and pay[e + 1] == 0x0d and pay[e + 4:e + 6] == b'\x08\x00' and pay[e] <= 0x30]
    return hits[0] if len(hits) == 1 else None


def _text_em(s: str) -> float:
    """Rough width of a text in em (Arial-like proportions)."""
    w = 0.0
    for ch in s:
        if ch in ' .,:;\'"!|()[]{}/\\-iIjlftr1':
            w += 0.32
        elif ch in 'mwMWШЩЖЮшщжюФфЫы@%':
            w += 0.85
        elif ch.isdigit():
            w += 0.56
        elif ch.isupper():
            w += 0.68
        else:
            w += 0.55
    return w


_ANY_DEF = re.compile(rb'\xff\xff[\x00-\x09]\x00([\x03-\x40])\x00(?=[A-Za-z_])')


def class_defs(pay: bytes) -> list:
    """Every MFC class definition (FF FF, u16 schema, u16 length, name) as (offset, name, end)."""
    out = []
    for m in _ANY_DEF.finditer(pay):
        n, p = m.group(1)[0], m.end()
        s = pay[p:p + n]
        if len(s) == n and all(48 <= c < 58 or 65 <= c < 91 or 97 <= c < 123 or c == 95 for c in s):
            out.append((m.start(), s.decode(), p + n))
    return out


def _plain_ref(pay: bytes, k: int) -> bool:
    """A 16-bit class reference candidate sits among plain data (zeros, ASCII, FF/FE)."""
    ctx = pay[k - 8:k] + pay[k + 2:k + 10]
    if sum(1 for c in ctx if c == 0 or 0x20 <= c < 0x7f or c in (0xff, 0xfe)) < 10:
        return False
    return _CSTR_PREFIX not in pay[k - 2:k + 3]


def _image_spans(pay: bytes) -> list:
    lib, _, _, _ = _library_scan(pay)
    spans = [(o, o + n) for o, n in lib.values()]      # library files of any format (BMP pixels too)
    for sig in (b'\x89PNG\r\n\x1a\n', b'\xff\xd8\xff'):
        i = pay.find(sig)
        while i != -1:
            ln = struct.unpack_from('<I', pay, i - 4)[0] if i >= 4 else 0
            if 0 < ln <= len(pay) - i:
                spans.append((i, i + ln))
                i = pay.find(sig, i + ln)
            else:
                i = pay.find(sig, i + 1)
    return sorted(spans)


# The picture library (before the windows): u32 count, then per picture FF, its GUID, u32 length and the file
# (PNG, BMP, JPEG). After it the picture objects the widgets point to, by GUID: 01 04 01 00 00 00, u32 count of
# states, per state 02 04 01 00 [01 00], the GUID of its file, u32 «transparent colour on», u32 0, the colour.
# A widget names its picture object (or an EasyBuilder shape, not kept in the project) 15 bytes after the
# x of its last label; labels: text, font, size (pt), u32 x, u32 y, colour (R G B 0), alignment.
_PIC_START = re.compile(rb'\xff[\x00-\xff]{20}(?:\x89PNG|BM|\xff\xd8\xff|GIF8)')
_PIC_STATE = b'\x02\x04\x01\x00'
_PIC_CLASS = b'\x01\x00\x01\x00'


def _library_scan(pay: bytes):
    """({GUID: (offset, length)}, start of the first entry, end, entries) of the picture library."""
    for m in _PIC_START.finditer(pay):
        s = m.start()
        n = struct.unpack_from('<I', pay, s - 4)[0] if s >= 4 else 0
        if not 0 < n <= 100000:
            continue
        out, at = {}, s
        for _ in range(n):
            if at + 21 > len(pay) or pay[at] != 0xff:
                break
            ln = struct.unpack_from('<I', pay, at + 17)[0]
            if at + 21 + ln > len(pay):
                break
            out[bytes(pay[at + 1:at + 17])] = (at + 21, ln)
            at += 21 + ln
        else:
            return out, s, at, n
    return {}, 0, 0, 0


def picture_library(pay: bytes):
    """({GUID: (offset, length)}, end) of the project's picture library ({}, 0 — none found)."""
    out, _, end, _ = _library_scan(pay)
    return out, end


# The picture objects follow the library: 6 bytes (04 00 00 00 00 00), u32 count, then the objects. The first one
# starts with its class (01 00 01 00, u16 length, «class PicGroup<class BitmapPicture>»), every one with 01 04 (the
# later ones 01 04 01 00), u16 length + name, u32 count of states, per state 02 04 01 00 01 00 (the very first
# state: its class 01 00 01 00 u16 «class BitmapPicture», then 02 04 01 00), the GUID of its file, u32
# «transparent colour on», u32 0, the colour (R G B 0); after the states the GUID the widgets name it by.
def picture_section(pay: bytes, end: int):
    """{'at', 'count_at', 'count', 'groups': [{start, end, name, states [(file GUID, transparent)], key}], 'end',
    'complete'} of the picture objects at `end` (the library end), None if they are not there."""
    if end <= 0 or end + 10 > len(pay):
        return None
    cnt = struct.unpack_from('<I', pay, end + 6)[0]
    if cnt > 100000 or pay[end + 1:end + 6] != bytes(5):
        return None
    q = end + 10
    sec = {'at': end, 'count_at': end + 6, 'count': cnt, 'groups': [], 'end': q, 'complete': cnt == 0}
    if cnt == 0:
        return sec
    if pay[q:q + 4] != _PIC_CLASS:
        return None
    q += 6 + struct.unpack_from('<H', pay, q + 4)[0]
    for i in range(cnt):
        s = q
        if pay[q:q + 2] != b'\x01\x04' or (i and pay[q + 2:q + 4] != b'\x01\x00'):
            return sec
        q += 4 if i else 2
        n = struct.unpack_from('<H', pay, q)[0]
        raw = bytes(pay[q + 2:q + 2 + n])
        q += 2 + n
        ns = struct.unpack_from('<I', pay, q)[0]
        q += 4
        if ns > 1000:
            return sec
        states, at = [], []
        for _ in range(ns):
            if pay[q:q + 4] == _PIC_CLASS:
                q += 6 + struct.unpack_from('<H', pay, q + 4)[0]
                if pay[q:q + 4] != _PIC_STATE:
                    return sec
                q += 4
            elif pay[q:q + 6] == _PIC_STATE + b'\x01\x00':
                q += 6
            else:
                return sec
            if q + 44 > len(pay):
                return sec
            on = struct.unpack_from('<I', pay, q + 16)[0]
            states.append((bytes(pay[q:q + 16]), _rgb(pay, q + 24) if on else None))
            at.append(q)
            q += 28
        sec['groups'].append({'start': s, 'end': q + 16, 'name': raw.decode('utf-8', 'replace'), 'name_raw': raw,
                              'states': states, 'state_at': at, 'key': bytes(pay[q:q + 16])})
        q += 16
        sec['end'] = q
    sec['complete'] = True
    return sec


def picture_groups(pay: bytes, lib: dict, start: int, stop: int = None) -> dict:
    """{GUID of a picture object: [(GUID of the file, transparent colour '#rrggbb' or None) per state]} — the
    picture objects whose files are all in the library."""
    sec = picture_section(pay, start)
    out = {}
    for g in (sec or {}).get('groups', ()):
        if g['states'] and all(f in lib for f, _ in g['states']):
            out[g['key']] = g['states']
    return out


def _rgb(pay: bytes, i: int) -> str:
    return '#%02x%02x%02x' % (pay[i], pay[i + 1], pay[i + 2])


def picture_format(data: bytes) -> str:
    for sig, kind in ((b'\x89PNG', 'png'), (b'BM', 'bmp'), (b'\xff\xd8\xff', 'jpg'), (b'GIF8', 'gif')):
        if data[:len(sig)] == sig:
            return kind
    return 'bin'


def bmp_png(data: bytes, transparent: str = None) -> bytes:
    """A Windows bitmap (1/4/8/24/32 bits, uncompressed) as PNG; pixels of the transparent colour get alpha 0."""
    if data[:2] != b'BM' or len(data) < 54:
        raise ValueError('not a bitmap')
    off, = struct.unpack_from('<I', data, 10)
    hsize, w, h, _, bits, comp = struct.unpack_from('<IiiHHI', data, 14)
    if comp not in (0, 3) or bits not in (1, 4, 8, 24, 32) or not 0 < w <= 10000 or not 0 < abs(h) <= 10000:
        raise ValueError('this bitmap is not supported')
    pal = []
    if bits <= 8:
        n = struct.unpack_from('<I', data, 46)[0] or 1 << bits
        p = 14 + hsize
        pal = [(data[p + 4 * i + 2], data[p + 4 * i + 1], data[p + 4 * i]) for i in range(n)]
    key = tuple(int(transparent[i:i + 2], 16) for i in (1, 3, 5)) if transparent else None
    stride = (bits * w + 31) // 32 * 4
    rows = []
    for y in range(abs(h)):
        src = off + (abs(h) - 1 - y if h > 0 else y) * stride
        row = bytearray(b'\x00')
        for x in range(w):
            if bits >= 24:
                q = src + x * (bits // 8)
                rgb = (data[q + 2], data[q + 1], data[q])
            else:
                v = (data[src + x * bits // 8] >> (8 - bits - x * bits % 8)) & ((1 << bits) - 1)
                rgb = pal[v] if v < len(pal) else (0, 0, 0)
            row += bytes(rgb) + (b'\x00' if rgb == key else b'\xff')
        rows.append(bytes(row))

    def chunk(kind, body):
        return struct.pack('>I', len(body)) + kind + body + struct.pack('>I', zlib.crc32(kind + body) & 0xffffffff)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', w, abs(h), 8, 6, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(b''.join(rows), 6)) + chunk(b'IEND', b''))


def _attrs_at(pay: bytes, o: dict, w: dict):
    """Where the shape attributes start (fill on, fill colour, frame colour … frame width at +44), or None."""
    if o['_bbox_at'] is not None:
        return o['_bbox_at'] + 16
    if o['kind'] == 'Window':
        k = pay.find(struct.pack('<IIII', 0, 0, w['width'] - 1, w['height'] - 1), o['offset'], o['offset'] + o['length'])
        return k + 16 if k >= 0 else None
    return None


_ALIGN = {0: 'left', 1: 'center', 2: 'right', 3: 'right'}


def _look(pay: bytes, o: dict, w: dict, groups: dict) -> dict:
    """How one object looks (see Project.scene)."""
    end = o['offset'] + o['length']
    d = {'index': o['index'], 'kind': o['kind'], 'bbox': o['bbox'],
         'tags': [a.get('tag') or a['address'] for a in o['addresses']]}
    a = _attrs_at(pay, o, w)
    if a is not None and a + 45 <= end:
        if o['kind'] == 'Window':
            d['fill'] = _rgb(pay, a + 1)
        elif o['kind'] in ('Rect', 'Line', 'Text') or o['kind'].startswith('draw_'):
            d['fill'] = _rgb(pay, a + 1) if pay[a] else None
            d['frame'], d['frame_width'] = _rgb(pay, a + 5), pay[a + 44]
    d['labels'] = [{'text': t, 'font': f, 'size': size, 'x': x, 'y': y,
                    'color': _rgb(pay, at + 8) if at + 12 < end else '#000000',
                    'align': _ALIGN.get(pay[at + 12], 'left') if at + 12 < end else 'left'}
                   for (at, x, y), (t, f, size) in zip(o['_label_pos'], o['_lab'])]
    slot = o['_label_pos'][-1][0] + 15 if o['_label_pos'] else None
    g = bytes(pay[slot:slot + 16]) if slot is not None and slot + 16 <= end else b''
    if g not in groups:   # not at its place: the first picture object named anywhere in the record
        g = next((bytes(pay[k:k + 16]) for k in range(o['offset'], end - 15) if bytes(pay[k:k + 16]) in groups), g)
    if g in groups:
        d['picture'] = [{'image': i.hex(), 'transparent': t} for i, t in groups[g]]
    elif len(g) == 16 and any(g) and g[6] >> 4 == 5 and g[8] >> 6 == 2:
        d['shape'] = g.hex()      # a shape of EasyBuilder's own library: drawn by EasyBuilder, not kept here
    return d


# Inserting records. CArchive numbers every class (at its FF FF definition) and every object it
# writes, in stream order from 1; a later use of a class is 0x8000 | its number. K inserted objects
# add K to every class defined after them, so their references are rewritten: classes defined
# between the windows (CCharDouble, CTrendLimit...) -- number computed, references found by value;
# classes of the settings after the windows (CTagObject, CDataLog, CMail*...) -- the 16-bit values
# of their number range among plain data (exactly the 1464 references EasyBuilder 6.10 rewrote when
# copying a window). The computed number of the first settings class must be the most used value.
def _nested_classes(pay: bytes, wins: list, defs: list, wend: int) -> list:
    """Classes defined between the windows (CCharDouble, CTrendLimit...): [{name, def, tag, refs}]."""
    imgs = _image_spans(pay)
    istarts = [s for s, _ in imgs]

    def in_image(k):
        j = bisect.bisect_right(istarts, k) - 1
        return j >= 0 and k < imgs[j][1]

    def objects_before(pos):
        n = 0
        for w in wins:
            if w['_rec_end'] <= pos:
                n += w['declared']
                continue
            if w['_first'] <= pos:
                n += sum(1 for o in w['objects'] if o['offset'] < pos)
            break
        return n

    nested = []
    for d, name, e in defs:
        if d >= wend:
            break
        if name in ('CPartObject', 'CDrawObject'):
            continue
        idx = 1 + sum(1 for x in defs if x[0] < d) + objects_before(d) + \
            sum(1 + sum(1 for k in c['refs'] if k < d) for c in nested)
        tb, refs = struct.pack('<H', 0x8000 | idx), []
        k = pay.find(tb, e)
        while k != -1:
            if _plain_ref(pay, k) and not in_image(k):
                refs.append(k)
            k = pay.find(tb, k + 1)
        nested.append({'name': name, 'def': d, 'tag': 0x8000 | idx, 'refs': refs})
    return nested


def _class_tags(pay: bytes) -> tuple:
    """(CPartObject reference, CDrawObject reference) as 2 bytes each (None — not used)."""
    heads = _heads(pay)
    return (next((pay[h[0]:h[0] + 2] for h in heads if h[1] == 'P' and h[3] == 3), None),
            next((pay[h[0]:h[0] + 2] for h in heads if h[1] == 'D' and h[3] == 2), None))


def _map_info(pay: bytes, wins: list) -> dict:
    if not wins:
        raise ValueError('no windows in the project')
    defs, wend = class_defs(pay), wins[-1]['_rec_end']
    tail = [d for d in defs if d[0] >= wend]
    if not tail:
        raise ValueError('the settings after the windows were not found')
    nested = _nested_classes(pay, wins, defs, wend)
    first = 0x8000 | (1 + sum(1 for x in defs if x[0] < tail[0][0]) + sum(w['declared'] for w in wins)
                      + sum(1 + len(c['refs']) for c in nested))

    def most_used(start, span, lo, hi, prefer=None):
        c = collections.Counter()
        for k in range(start, min(len(pay) - 1, start + span)):
            if pay[k + 1] & 0x80:
                v = pay[k] | pay[k + 1] << 8
                if lo <= v < hi and _plain_ref(pay, k):
                    c[v] += 1
        if not c:
            return None
        # a reference LL HH after a zero byte also reads as HH00 one byte earlier, as often as the real one
        best = max(c.values())
        tops = [v for v, n in c.items() if n == best]
        return prefer if prefer in tops else min(tops, key=lambda v: (v & 0xff == 0, v))

    lo = most_used(tail[0][0], 1 << 16, first, 0xffff, first)
    if lo != first:
        raise ValueError(f'class numbering does not add up (expected {first:#x}, found {lo and hex(lo)})')
    hi = most_used(tail[-1][0], 1 << 20, lo, lo + 0x1000) or lo

    def ref(k):
        return pay[k + 1] & 0x80 and lo <= (pay[k] | pay[k + 1] << 8) <= hi

    # a class reference of the settings stands after 00 / FF / 01-03 or another reference and before a small
    # number (a length, 00) or another reference; the OPC UA tag table (big-endian lengths) holds none
    tab = opcua_table(pay)
    skip = (tab['entries'][0]['start'], tab['end']) if tab else (0, 0)
    trefs = [k for k in range(tail[0][0], len(pay) - 2)
             if ref(k) and not skip[0] <= k < skip[1] and _plain_ref(pay, k)
             and (pay[k - 1] in (0, 1, 2, 3, 0xff) or ref(k - 2)) and (pay[k + 2] <= 0x40 or ref(k + 2))]
    ptag, dtag = _class_tags(pay)
    return {'defs': defs, 'nested': nested, 'tail_refs': trefs, 'range': (lo, hi), 'ptag': ptag, 'dtag': dtag,
            'first_defs': max(pay.find(_PART_DEF), pay.find(_DRAW_DEF)),
            'counts': (len(trefs), tuple(len(c['refs']) for c in nested))}


def _check_range(pay: bytes, s: int, e: int, info: dict, what: str = 'copying') -> None:
    """Refuse records holding sub-objects or class definitions other than the widget / shape ones."""
    for c in info['nested']:
        if s <= c['def'] < e or any(s <= k < e for k in c['refs']):
            raise ValueError(f'it holds sub-objects ({c["name"]}); {what} those is not supported yet')
    for d, name, _ in info['defs']:
        if s <= d < e and name not in ('CPartObject', 'CDrawObject'):
            raise ValueError(f'it defines class {name}; {what} it is not supported')


def _clone_bytes(pay: bytes, s: int, e: int, info: dict) -> bytes:
    """Bytes of a window / object record to insert again (its class definitions -> references)."""
    _check_range(pay, s, e, info)
    rec = pay[s:e]
    for cdef, tag in ((_PART_DEF, info['ptag']), (_DRAW_DEF, info['dtag'])):
        if cdef in rec:
            if tag is None:
                raise ValueError('no class reference to copy the definition from')
            rec = rec.replace(cdef, tag)
    return rec


def _renumber_edits(pay: bytes, info: dict, inserts) -> list:
    """2-byte edits renumbering the class references for [(offset, entries)] inserted."""
    if any(at <= info['first_defs'] for at, _ in inserts):
        raise ValueError('inserting before the first widget / shape definition is not supported')
    edits = []
    for c in info['nested']:
        add = sum(k for at, k in inserts if at <= c['def'])
        edits += [(k, k + 2, struct.pack('<H', c['tag'] + add)) for k in c['refs']] if add else []
    add = sum(k for _, k in inserts)
    if info['range'][1] + add > 0xffff:
        raise ValueError('too many objects for 16-bit class references')
    if info['range'][0] + add <= 0x8000:
        raise ValueError('class references would leave their range')
    return edits + [(k, k + 2, struct.pack('<H', (pay[k] | pay[k + 1] << 8) + add)) for k in info['tail_refs']]
