#!/usr/bin/env python3
"""mearchive -- read and write CODESYS V3.5 / EcoStruxure Machine Expert ``.project`` files.

Layers (the byte-level description: docs/opo/MACHINE_EXPERT.md):

* container  : ``open_container`` / ``seal_container``   (AES-256-CBC wrapper around a ZIP)
* zip        : ``Project``                                (entry order + zip metadata preserved)
* storage    : ``SharedStorage``                          (string table + schema table)
* archive    : ``load`` / ``dump``                        ("new binary archive", magic 0x28092002)
               ``load_legacy`` / ``dump_legacy``          ("old binary archive", magic 0x85301D21)

Pure Python 3.11+, stdlib only.  The container uses ``cryptography`` when it is installed and
the plain-Python AES of ``meaes`` otherwise.

CLI:  python mearchive.py dump <file.project|file.zip> [--entry NAME] [--json] [--max N]
      python mearchive.py list <file.project|file.zip>
"""
from __future__ import annotations

import hashlib
import io
import struct
import sys
import zlib

# --------------------------------------------------------------------------------------
# ObjectFormat tags
# --------------------------------------------------------------------------------------
F_NULL, F_BOOL, F_BYTE, F_SBYTE, F_CHAR, F_DECIMAL, F_DOUBLE, F_FLOAT = range(8)
F_INT, F_UINT, F_LONG, F_ULONG, F_SHORT, F_USHORT, F_STRING, F_ARCHIVABLE = range(8, 16)
F_TYPECONVERTER = 0x80
F_HOM_ARRAY, F_HET_ARRAY, F_HOM_LIST, F_HET_LIST = 0x81, 0x82, 0x83, 0x84
F_HH_DICT, F_HOMHET_DICT, F_HETHOM_DICT, F_HETHET_DICT = 0x85, 0x86, 0x87, 0x88
F_HOM_LIST_TI, F_HET_LIST_TI = 0x40, 0x41
F_HH_DICT_TI, F_HOMHET_DICT_TI, F_HETHOM_DICT_TI, F_HETHET_DICT_TI = 0x42, 0x43, 0x44, 0x45

FORMAT_NAMES = {
    0: 'Null', 1: 'Bool', 2: 'Byte', 3: 'Sbyte', 4: 'Char', 5: 'Decimal', 6: 'Double', 7: 'Float',
    8: 'Int', 9: 'Uint', 10: 'Long', 11: 'Ulong', 12: 'Short', 13: 'Ushort', 14: 'String',
    15: 'Archivable', 0x80: 'TypeConverter', 0x81: 'HomArray', 0x82: 'HetArray', 0x83: 'HomList',
    0x84: 'HetList', 0x85: 'HomHomDict', 0x86: 'HomHetDict', 0x87: 'HetHomDict', 0x88: 'HetHetDict',
    0x40: 'HomListTI', 0x41: 'HetListTI', 0x42: 'HomHomDictTI', 0x43: 'HomHetDictTI',
    0x44: 'HetHomDictTI', 0x45: 'HetHetDictTI',
}

_SCALAR = {  # fmt -> struct code
    F_BYTE: '<B', F_SBYTE: '<b', F_DOUBLE: '<d', F_FLOAT: '<f', F_INT: '<i', F_UINT: '<I',
    F_LONG: '<q', F_ULONG: '<Q', F_SHORT: '<h', F_USHORT: '<H',
}
_LIST_FORMATS = (F_HOM_ARRAY, F_HET_ARRAY, F_HOM_LIST, F_HET_LIST, F_HOM_LIST_TI, F_HET_LIST_TI)
_HOM_SEQ = (F_HOM_ARRAY, F_HOM_LIST, F_HOM_LIST_TI)
_DICT_FORMATS = (F_HH_DICT, F_HOMHET_DICT, F_HETHOM_DICT, F_HETHET_DICT,
                 F_HH_DICT_TI, F_HOMHET_DICT_TI, F_HETHOM_DICT_TI, F_HETHET_DICT_TI)
_KEY_HOM = (F_HH_DICT, F_HOMHET_DICT, F_HH_DICT_TI, F_HOMHET_DICT_TI)
_VAL_HOM = (F_HH_DICT, F_HETHOM_DICT, F_HH_DICT_TI, F_HETHOM_DICT_TI)

# Arrays whose construction type (= element type) is a .NET primitive are stored as one raw block
# of count * Marshal.SizeOf(type) bytes.  (Marshal.SizeOf(bool) == 4 and SizeOf(char) == 1; the
# .NET code would fail on those, so they do not occur in practice.)
PRIMITIVE_ARRAY_TYPES = {
    'System.Byte': 'B', 'System.SByte': 'b', 'System.Int16': 'h', 'System.UInt16': 'H',
    'System.Int32': 'i', 'System.UInt32': 'I', 'System.Int64': 'q', 'System.UInt64': 'Q',
    'System.Single': 'f', 'System.Double': 'd',
}

NEW_MAGIC = 0x28092002
LEGACY_MAGIC1 = 0x85301D21
LEGACY_MAGIC2 = 0x076FFC6A
CONTAINER_MAGIC = bytes.fromhex('2389ED3314BD934B8DA95D9108C53235')
STRING_TABLE_ENTRY = '__shared_data_storage_string_table__.auxiliary'
SCHEMA_TABLE_ENTRY = '__shared_data_storage_schema_table__.auxiliary'


class FormatError(ValueError):
    pass


# --------------------------------------------------------------------------------------
# low-level reader / writer
# --------------------------------------------------------------------------------------
class Reader:
    __slots__ = ('b', 'i')

    def __init__(self, b, i=0):
        self.b = b
        self.i = i

    def u8(self):
        v = self.b[self.i]
        self.i += 1
        return v

    def take(self, n):
        if self.i + n > len(self.b):
            raise FormatError(f'unexpected end of data at {self.i} (+{n})')
        v = self.b[self.i:self.i + n]
        self.i += n
        return bytes(v)

    def unpack(self, code):
        n = struct.calcsize(code)
        v = struct.unpack_from(code, self.b, self.i)[0]
        self.i += n
        return v

    def mbui(self):
        """Multi-byte unsigned int: 7 bits per byte, least significant group first, 0x80 = more."""
        v = s = 0
        b = self.b
        while True:
            c = b[self.i]
            self.i += 1
            v |= (c & 0x7F) << s
            if not c & 0x80:
                return v
            s += 7

    def eof(self):
        return self.i >= len(self.b)


def mbui(v: int) -> bytes:
    if v < 0x80:
        return bytes((v,))
    out = bytearray()
    while True:
        c = v & 0x7F
        v >>= 7
        if v:
            out.append(c | 0x80)
        else:
            out.append(c)
            return bytes(out)


# --------------------------------------------------------------------------------------
# node classes
# --------------------------------------------------------------------------------------
class RawFloat(float):
    """A float whose exact bytes are kept (NaN payloads)."""
    __slots__ = ('raw',)


class Prim:
    """A primitive value that carries its own type tag (needed in heterogeneous collections and
    wherever the format cannot be derived from the Python type).  ``ctype`` is only used for
    F_TYPECONVERTER (string form of a value converted with a .NET TypeConverter)."""
    __slots__ = ('fmt', 'value', 'ctype')

    def __init__(self, fmt, value, ctype=None):
        self.fmt = fmt
        self.value = value
        self.ctype = ctype

    def __repr__(self):
        c = f', {self.ctype!r}' if self.ctype else ''
        return f'Prim({FORMAT_NAMES.get(self.fmt, self.fmt)}, {self.value!r}{c})'

    def __eq__(self, o):
        return isinstance(o, Prim) and (o.fmt, o.value, o.ctype) == (self.fmt, self.value, self.ctype)

    def __hash__(self):
        return hash((self.fmt, self.value, self.ctype))


class Obj:
    """An IArchivable object: type GUID + ordered named fields.

    ``names[i]`` / ``tags[i]`` / ``values[i]`` describe field i; ``tags[i]`` is ``(fmt, ctype)`` --
    exactly what the schema entry stores for the member.  ``schema_id`` is the id the object had
    when it was read (None for new objects); on dump it is reused if the schema entry still matches
    the object's (type_guid, names, tags), otherwise a matching entry is looked up / created.

    Dict-like access: ``obj['Name']``, ``obj['Name'] = value`` (keeps the member format),
    ``obj.set('Name', value, fmt, ctype)`` (changes it), ``'Name' in obj``, ``obj.keys()``...
    A null archivable reference (schema id 0) is an Obj with ``type_guid is None``.
    """
    __slots__ = ('type_guid', 'schema_id', 'names', 'tags', 'values')

    def __init__(self, type_guid, fields=(), schema_id=None):
        self.type_guid = type_guid
        self.schema_id = schema_id
        self.names, self.tags, self.values = [], [], []
        for name, fmt, ctype, value in fields:
            self.names.append(name)
            self.tags.append((fmt, ctype))
            self.values.append(value)

    @property
    def is_null(self):
        return self.type_guid is None

    def _idx(self, name):
        try:
            return self.names.index(name)
        except ValueError:
            raise KeyError(name) from None

    def __getitem__(self, name):
        return self.values[self._idx(name)]

    def __setitem__(self, name, value):
        i = self._idx(name)
        fmt, ctype = self.tags[i]
        nfmt, nctype = tag_of(value, (fmt, ctype))
        self.tags[i] = (nfmt, nctype)
        self.values[i] = value.value if isinstance(value, Prim) else value

    def set(self, name, value, fmt=None, ctype=None):
        """Set (or append) a field.  ``fmt``/``ctype`` default to what the value implies."""
        if isinstance(value, Prim):
            fmt, ctype, value = value.fmt, value.ctype, value.value
        elif fmt is None:
            fmt, ctype = tag_of(value, None)
        if name in self.names:
            i = self.names.index(name)
            self.tags[i] = (fmt, ctype)
            self.values[i] = value
        else:
            self.names.append(name)
            self.tags.append((fmt, ctype))
            self.values.append(value)

    def remove(self, name):
        i = self._idx(name)
        del self.names[i], self.tags[i], self.values[i]

    def __contains__(self, name):
        return name in self.names

    def __len__(self):
        return len(self.names)

    def __iter__(self):
        return iter(self.names)

    def keys(self):
        return list(self.names)

    def items(self):
        return list(zip(self.names, self.values))

    def get(self, name, default=None):
        return self.values[self.names.index(name)] if name in self.names else default

    def fmt_of(self, name):
        return self.tags[self._idx(name)]

    def members(self):
        return tuple((n, f, c) for n, (f, c) in zip(self.names, self.tags))

    def __repr__(self):
        if self.is_null:
            return 'Obj(null)'
        return f'Obj({self.type_guid}, {len(self.names)} fields: {", ".join(self.names[:6])}{"..." if len(self.names) > 6 else ""})'


class Coll:
    """Array / list.  ``fmt`` is one of the six sequence formats, ``ctype`` the construction type
    string (None for the *TI "constructed by target instance" formats), ``elem`` the shared element
    tag ``(fmt, ctype)`` for homogeneous formats.  ``items`` is a list; for homogeneous formats the
    primitives are plain Python values, for heterogeneous ones every item is self-describing
    (None, str, bool, Prim, Obj, Coll, Dict).  Primitive arrays (``raw`` is a struct code) keep
    ``items`` as ``bytes`` (System.Byte) or a list of numbers."""
    __slots__ = ('fmt', 'ctype', 'elem', 'items', 'raw')

    def __init__(self, fmt, ctype=None, elem=None, items=None, raw=None):
        self.fmt = fmt
        self.ctype = ctype
        self.elem = elem
        self.items = [] if items is None else items
        self.raw = raw

    @property
    def homogeneous(self):
        return self.fmt in _HOM_SEQ

    def __len__(self):
        return len(self.items)

    def __iter__(self):
        return iter(self.items)

    def __getitem__(self, i):
        return self.items[i]

    def __setitem__(self, i, v):
        self.items[i] = v

    def append(self, v):
        self.items.append(v)

    def __repr__(self):
        return f'Coll({FORMAT_NAMES.get(self.fmt)}, {self.ctype!r}, n={len(self.items)})'


class Dict:
    """Dictionary.  ``items`` is a list of ``[key, value]`` pairs in file order; ``ktag`` / ``vtag``
    are the shared ``(fmt, ctype)`` tags when keys / values are homogeneous (else None)."""
    __slots__ = ('fmt', 'ctype', 'ktag', 'vtag', 'items')

    def __init__(self, fmt, ctype=None, ktag=None, vtag=None, items=None):
        self.fmt = fmt
        self.ctype = ctype
        self.ktag = ktag
        self.vtag = vtag
        self.items = [] if items is None else items

    def __len__(self):
        return len(self.items)

    def __iter__(self):
        return (k for k, _ in self.items)

    def keys(self):
        return [k for k, _ in self.items]

    def values(self):
        return [v for _, v in self.items]

    def __getitem__(self, key):
        for k, v in self.items:
            if k == key:
                return v
        raise KeyError(key)

    def __setitem__(self, key, value):
        for kv in self.items:
            if kv[0] == key:
                kv[1] = value
                return
        self.items.append([key, value])

    def get(self, key, default=None):
        for k, v in self.items:
            if k == key:
                return v
        return default

    def __contains__(self, key):
        return any(k == key for k, _ in self.items)

    def __repr__(self):
        return f'Dict({FORMAT_NAMES.get(self.fmt)}, {self.ctype!r}, n={len(self.items)})'


def tag_of(value, default=None):
    """(fmt, ctype) a value is written with.  ``default`` is the tag of the slot the value goes in
    (kept if compatible with the Python type)."""
    if isinstance(value, Prim):
        return value.fmt, value.ctype
    if isinstance(value, Obj):
        return F_ARCHIVABLE, None
    if isinstance(value, (Coll, Dict)):
        return value.fmt, value.ctype
    if value is None:
        return F_NULL, None
    if default is not None:
        f = default[0]
        if isinstance(value, bool):
            if f == F_BOOL:
                return default
        elif isinstance(value, str):
            if f in (F_STRING, F_TYPECONVERTER, F_CHAR):
                return default
        elif isinstance(value, int):
            if f in (F_BYTE, F_SBYTE, F_INT, F_UINT, F_LONG, F_ULONG, F_SHORT, F_USHORT):
                return default
        elif isinstance(value, float):
            if f in (F_DOUBLE, F_FLOAT):
                return default
        elif isinstance(value, (bytes, bytearray)):
            if f == F_DECIMAL:
                return default
    if isinstance(value, bool):
        return F_BOOL, None
    if isinstance(value, str):
        return F_STRING, None
    if isinstance(value, float):
        return F_DOUBLE, None
    if isinstance(value, int):
        raise FormatError(f'integer {value!r} needs an explicit format: use Prim(F_INT, v) etc.')
    raise FormatError(f'cannot derive a format for {value!r}')


# --------------------------------------------------------------------------------------
# shared data storage (string table + schema table)
# --------------------------------------------------------------------------------------
class SchemaEntry:
    """One schema: ``id``, ``type_guid`` (lower-case 'D' format string) and the member list
    ``((name, fmt, ctype), ...)``.  A schema is the *shape of one serialized instance*: member
    formats are the formats the values actually had (a null member has format Null), so one .NET
    type usually has several schema entries."""
    __slots__ = ('id', 'type_guid', 'members')

    def __init__(self, id, type_guid, members):
        self.id = id
        self.type_guid = type_guid
        self.members = tuple(tuple(m) for m in members)

    def key(self):
        return (self.type_guid, self.members)

    def __repr__(self):
        return f'SchemaEntry({self.id}, {self.type_guid}, {len(self.members)} members)'


class SharedStorage:
    """String table (id <-> string) and schema table (id -> SchemaEntry).

    File order of both tables is preserved; new strings / schemas are appended with fresh ids
    (max id + 1), which is what the original implementation does for a non-compacted storage.
    """

    def __init__(self):
        self.strings: dict[int, str] = {}       # id -> string, in file order
        self.string_ids: dict[str, int] = {}    # string -> id
        self.schemas: dict[int, SchemaEntry] = {}   # id -> entry, in file order
        self._schema_keys: dict[tuple, int] = {}
        self._next_string = 0
        self._dup_strings = False

    # ---- parsing -------------------------------------------------------------------------
    @classmethod
    def parse(cls, string_table: bytes, schema_table: bytes) -> 'SharedStorage':
        self = cls()
        r = Reader(string_table)
        n = r.mbui() if string_table else 0
        strings, string_ids = self.strings, self.string_ids
        mx = -1
        for _ in range(n):
            sid = r.mbui()
            ln = r.mbui()
            s = r.take(ln).decode('utf-8', 'surrogateescape')
            if sid in strings or s in string_ids:
                self._dup_strings = True
            strings[sid] = s
            string_ids.setdefault(s, sid)
            if sid > mx:
                mx = sid
        if not r.eof():
            raise FormatError(f'string table: {len(string_table) - r.i} trailing bytes')
        self._next_string = mx + 1
        r = Reader(schema_table)
        n = r.mbui() if schema_table else 0
        for _ in range(n):
            eid = r.mbui()
            guid = self.string(r.mbui())
            members = []
            for _ in range(r.mbui()):
                name = self.string(r.mbui())
                fmt = r.u8()
                ctype = self.string(r.mbui()) if fmt & 0x80 else None
                members.append((name, fmt, ctype))
            e = SchemaEntry(eid, guid, members)
            self.schemas[eid] = e
            self._schema_keys.setdefault(e.key(), eid)
        if not r.eof():
            raise FormatError(f'schema table: {len(schema_table) - r.i} trailing bytes')
        return self

    # ---- serialization ------------------------------------------------------------------
    def string_table_bytes(self) -> bytes:
        out = [mbui(len(self.strings))]
        for sid, s in self.strings.items():
            b = s.encode('utf-8', 'surrogateescape')
            out.append(mbui(sid))
            out.append(mbui(len(b)))
            out.append(b)
        return b''.join(out)

    def schema_table_bytes(self) -> bytes:
        """NOTE: may add strings (type guids / member names of new schemas) -- serialize the schema
        table *before* the string table."""
        out = [mbui(len(self.schemas))]
        for e in self.schemas.values():
            out.append(mbui(e.id))
            out.append(mbui(self.sid(e.type_guid)))
            out.append(mbui(len(e.members)))
            for name, fmt, ctype in e.members:
                out.append(mbui(self.sid(name)))
                out.append(bytes((fmt,)))
                if fmt & 0x80:
                    out.append(mbui(self.sid(ctype)))
        return b''.join(out)

    def serialize(self) -> tuple[bytes, bytes]:
        """-> (string_table_bytes, schema_table_bytes)"""
        schema = self.schema_table_bytes()
        return self.string_table_bytes(), schema

    # ---- strings ------------------------------------------------------------------------
    def string(self, sid: int) -> str:
        try:
            return self.strings[sid]
        except KeyError:
            raise FormatError(f'string id {sid} is not in the string table') from None

    def sid(self, s: str) -> int:
        """id of a string, adding it to the table if necessary."""
        i = self.string_ids.get(s)
        if i is None:
            if not isinstance(s, str):
                raise FormatError(f'not a string: {s!r}')
            i = self._next_string
            while i in self.strings:
                i += 1
            self._next_string = i + 1
            self.strings[i] = s
            self.string_ids[s] = i
        return i

    add_string = sid

    # ---- schemas ------------------------------------------------------------------------
    def schema_for(self, type_guid: str, members, prefer: int | None = None) -> int:
        """id of the schema (type_guid, members); a new entry is appended if none exists."""
        members = tuple(members)
        if prefer is not None:
            e = self.schemas.get(prefer)
            if e is not None and e.type_guid == type_guid and e.members == members:
                return prefer
        key = (type_guid, members)
        i = self._schema_keys.get(key)
        if i is None:
            i = len(self.schemas) + 1          # original: Count + 1
            while i in self.schemas or i == 0:
                i += 1
            self.schemas[i] = SchemaEntry(i, type_guid, members)
            self._schema_keys[key] = i
        return i


# --------------------------------------------------------------------------------------
# value codec ("new binary archive")
# --------------------------------------------------------------------------------------
class _Decoder:
    def __init__(self, data, storage: SharedStorage):
        self.r = Reader(data)
        self.st = storage

    def tag(self):
        r = self.r
        fmt = r.u8()
        return fmt, (self.st.string(r.mbui()) if fmt & 0x80 else None)

    def tagged(self):
        """Value preceded by its tag; primitives come back wrapped so the tag is not lost."""
        fmt, ctype = self.tag()
        v = self.value(fmt, ctype)
        if fmt == F_NULL or fmt == F_STRING or fmt == F_BOOL or fmt == F_ARCHIVABLE or fmt in _LIST_FORMATS \
                or fmt in _DICT_FORMATS:
            return v
        return Prim(fmt, v, ctype)

    def value(self, fmt, ctype):
        r = self.r
        if fmt == F_STRING or fmt == F_TYPECONVERTER:
            return self.st.string(r.mbui())
        if fmt == F_ARCHIVABLE:
            return self.obj()
        if fmt == F_NULL:
            if r.u8() != 0:
                raise FormatError(f'null marker is not 0 at {r.i - 1}')
            return None
        if fmt == F_BOOL:
            v = r.u8()
            if v > 1:
                return Prim(F_BYTE, v)     # never seen; keeps the byte
            return bool(v)
        code = _SCALAR.get(fmt)
        if code is not None:
            if fmt == F_FLOAT or fmt == F_DOUBLE:
                raw = r.take(4 if fmt == F_FLOAT else 8)
                v = struct.unpack(code, raw)[0]
                if struct.pack(code, v) != raw:
                    v = RawFloat(v)
                    v.raw = raw
                return v
            return r.unpack(code)
        if fmt == F_CHAR:
            return chr(r.unpack('<H'))
        if fmt == F_DECIMAL:
            return r.take(16)
        if fmt in _LIST_FORMATS:
            return self.seq(fmt, ctype)
        if fmt in _DICT_FORMATS:
            return self.dict(fmt, ctype)
        raise FormatError(f'unknown object format 0x{fmt:02x} at {r.i}')

    def obj(self):
        r = self.r
        sid = r.mbui()
        e = self.st.schemas.get(sid)
        if e is None:
            if sid == 0:
                return Obj(None, schema_id=0)
            raise FormatError(f'schema id {sid} is not in the schema table (at {r.i})')
        o = Obj(e.type_guid, schema_id=sid)
        value = self.value
        names, tags, values = o.names, o.tags, o.values
        for name, fmt, ctype in e.members:
            names.append(name)
            tags.append((fmt, ctype))
            values.append(value(fmt, ctype))
        return o

    def seq(self, fmt, ctype):
        r = self.r
        n = r.mbui()
        c = Coll(fmt, ctype)
        if fmt in _HOM_SEQ:
            efmt, ectype = c.elem = self.tag()
            if fmt == F_HOM_ARRAY and ctype in PRIMITIVE_ARRAY_TYPES:
                code = PRIMITIVE_ARRAY_TYPES[ctype]
                c.raw = code
                size = struct.calcsize(code)
                raw = r.take(n * size)
                if code == 'B':
                    c.items = raw
                else:
                    items = list(struct.unpack(f'<{n}{code}', raw))
                    if code in 'fd' and struct.pack(f'<{n}{code}', *items) != raw:
                        c.items = raw           # NaN payloads: keep the block as bytes
                        c.raw = 'raw:' + code
                    else:
                        c.items = items
                return c
            value = self.value
            c.items = [value(efmt, ectype) for _ in range(n)]
        else:
            tagged = self.tagged
            c.items = [tagged() for _ in range(n)]
        return c

    def dict(self, fmt, ctype):
        r = self.r
        n = r.mbui()
        d = Dict(fmt, ctype)
        kh, vh = fmt in _KEY_HOM, fmt in _VAL_HOM
        if kh:
            d.ktag = self.tag()
        if vh:
            d.vtag = self.tag()
        items = d.items
        for _ in range(n):
            k = self.value(*d.ktag) if kh else self.tagged()
            v = self.value(*d.vtag) if vh else self.tagged()
            items.append([k, v])
        return d


class _Encoder:
    def __init__(self, storage: SharedStorage):
        self.st = storage
        self.out = []

    def tag(self, fmt, ctype):
        self.out.append(bytes((fmt,)))
        if fmt & 0x80:
            if ctype is None:
                raise FormatError(f'format 0x{fmt:02x} needs a construction type')
            self.out.append(mbui(self.st.sid(ctype)))

    def tagged(self, v):
        if isinstance(v, Prim):
            self.tag(v.fmt, v.ctype)
            self.value(v.fmt, v.ctype, v.value)
            return
        fmt, ctype = tag_of(v)
        self.tag(fmt, ctype)
        self.value(fmt, ctype, v)

    def value(self, fmt, ctype, v):
        out = self.out
        if isinstance(v, Prim):
            if v.fmt != fmt and not (fmt == F_BOOL and v.fmt == F_BYTE):
                raise FormatError(f'{v!r} in a slot of format {FORMAT_NAMES.get(fmt, fmt)}')
            v = v.value
        if fmt == F_STRING or fmt == F_TYPECONVERTER:
            out.append(mbui(self.st.sid(v)))
        elif fmt == F_ARCHIVABLE:
            self.obj(v)
        elif fmt == F_NULL:
            if v is not None:
                raise FormatError(f'format Null but value {v!r}')
            out.append(b'\0')
        elif fmt == F_BOOL:
            out.append(bytes((int(v),)))
        elif fmt in _SCALAR:
            if isinstance(v, RawFloat):
                out.append(v.raw)
            else:
                out.append(struct.pack(_SCALAR[fmt], v))
        elif fmt == F_CHAR:
            out.append(struct.pack('<H', ord(v)))
        elif fmt == F_DECIMAL:
            if len(v) != 16:
                raise FormatError('decimal must be 16 raw bytes')
            out.append(bytes(v))
        elif fmt in _LIST_FORMATS:
            self.seq(fmt, ctype, v)
        elif fmt in _DICT_FORMATS:
            self.dict(fmt, ctype, v)
        else:
            raise FormatError(f'unknown object format 0x{fmt:02x}')

    def obj(self, o):
        if not isinstance(o, Obj):
            raise FormatError(f'format Archivable but value {o!r}')
        if o.is_null:
            self.out.append(b'\0')
            return
        sid = self.st.schema_for(o.type_guid, o.members(), o.schema_id)
        self.out.append(mbui(sid))
        value = self.value
        for (fmt, ctype), v in zip(o.tags, o.values):
            value(fmt, ctype, v)

    def seq(self, fmt, ctype, c):
        if not isinstance(c, Coll) or c.fmt != fmt:
            raise FormatError(f'format {FORMAT_NAMES.get(fmt)} but value {c!r}')
        out = self.out
        out.append(mbui(len(c.items) if not (c.raw and c.raw.startswith('raw:'))
                        else len(c.items) // struct.calcsize(c.raw[4:])))
        if fmt in _HOM_SEQ:
            if c.elem is None:
                raise FormatError('homogeneous collection without element tag')
            efmt, ectype = c.elem
            self.tag(efmt, ectype)
            if fmt == F_HOM_ARRAY and ctype in PRIMITIVE_ARRAY_TYPES:
                code = PRIMITIVE_ARRAY_TYPES[ctype]
                if isinstance(c.items, (bytes, bytearray)):
                    out.append(bytes(c.items))
                else:
                    out.append(struct.pack(f'<{len(c.items)}{code}', *c.items))
                return
            value = self.value
            for v in c.items:
                value(efmt, ectype, v)
        else:
            for v in c.items:
                self.tagged(v)

    def dict(self, fmt, ctype, d):
        if not isinstance(d, Dict) or d.fmt != fmt:
            raise FormatError(f'format {FORMAT_NAMES.get(fmt)} but value {d!r}')
        self.out.append(mbui(len(d.items)))
        kh, vh = fmt in _KEY_HOM, fmt in _VAL_HOM
        if kh:
            self.tag(*d.ktag)
        if vh:
            self.tag(*d.vtag)
        for k, v in d.items:
            if kh:
                self.value(d.ktag[0], d.ktag[1], k)
            else:
                self.tagged(k)
            if vh:
                self.value(d.vtag[0], d.vtag[1], v)
            else:
                self.tagged(v)


def retag(c):
    """Recompute homogeneous / heterogeneous format of a Coll or Dict the way the original writer
    does (all items share format, schema id and construction type -> homogeneous; an empty
    collection is homogeneous with element tag Null).  Use after editing ``items`` by hand; needs
    items in *tagged* form for primitives when the result is heterogeneous.  Returns ``c``.
    Schema-id equality of Obj items is approximated by (type_guid, members)."""
    def sig(v, tag):
        if tag is not None and not isinstance(v, (Obj, Coll, Dict, Prim)) and v is not None:
            return tag
        f, ct = tag_of(v, tag)
        if isinstance(v, Obj):
            return (f, ct, v.type_guid, v.members())
        return (f, ct)

    def analyse(values, tag):
        sigs = [sig(v, tag) for v in values]
        if not sigs:
            return (F_NULL, None)
        if all(s == sigs[0] for s in sigs):
            return sigs[0][:2]
        return None

    def strip(values):
        return [v.value if isinstance(v, Prim) else v for v in values]

    def wrap(values, tag):
        out = []
        for v in values:
            if isinstance(v, (Obj, Coll, Dict, Prim)) or v is None or isinstance(v, (str, bool)) and tag is None:
                out.append(v)
            else:
                f, ct = tag_of(v, tag)
                out.append(v if f in (F_STRING, F_BOOL) else Prim(f, v, ct))
        return out

    if isinstance(c, Coll):
        if c.raw:
            return c
        hom = analyse(c.items, c.elem)
        base = {F_HOM_ARRAY: 0, F_HET_ARRAY: 0, F_HOM_LIST: 1, F_HET_LIST: 1, F_HOM_LIST_TI: 2, F_HET_LIST_TI: 2}[c.fmt]
        if hom is not None:
            c.items = strip(c.items)
            c.fmt, c.elem = (F_HOM_ARRAY, F_HOM_LIST, F_HOM_LIST_TI)[base], hom
        else:
            c.items = wrap(c.items, c.elem)
            c.fmt, c.elem = (F_HET_ARRAY, F_HET_LIST, F_HET_LIST_TI)[base], None
    elif isinstance(c, Dict):
        ti = c.fmt < 0x80
        kh = analyse([k for k, _ in c.items], c.ktag)
        vh = analyse([v for _, v in c.items], c.vtag)
        ks = strip(c.keys()) if kh is not None else wrap(c.keys(), c.ktag)
        vs = strip(c.values()) if vh is not None else wrap(c.values(), c.vtag)
        c.items = [[k, v] for k, v in zip(ks, vs)]
        c.ktag, c.vtag = kh, vh
        idx = (0 if kh is not None else 2) + (0 if vh is not None else 1)
        c.fmt = ((F_HH_DICT_TI, F_HOMHET_DICT_TI, F_HETHOM_DICT_TI, F_HETHET_DICT_TI) if ti else
                 (F_HH_DICT, F_HOMHET_DICT, F_HETHOM_DICT, F_HETHET_DICT))[idx]
    return c


# --------------------------------------------------------------------------------------
# archive level
# --------------------------------------------------------------------------------------
class Archive:
    """One "new binary archive" stream.  ``embedded`` is True when the stream carries its own
    string and schema table (stand-alone archives such as *.precompileinfo.auxiliary); then
    ``storage`` is that private storage, otherwise it is the project's shared storage."""
    __slots__ = ('root', 'storage', 'embedded')

    def __init__(self, root, storage, embedded=False):
        self.root = root
        self.storage = storage
        self.embedded = embedded

    @classmethod
    def parse(cls, data: bytes, storage: SharedStorage | None = None) -> 'Archive':
        if len(data) < 20:
            raise FormatError('too short for a binary archive')
        magic, zero, n_str, n_schema, n_data = struct.unpack_from('<5I', data, 0)
        if magic != NEW_MAGIC:
            raise FormatError(f'bad magic 0x{magic:08x}')
        if zero != 0:
            raise FormatError(f'second header word is {zero}, expected 0')
        if 20 + n_str + n_schema + n_data != len(data):
            raise FormatError(f'section lengths {n_str}+{n_schema}+{n_data} do not match size {len(data)}')
        embedded = bool(n_str or n_schema)
        if embedded:
            storage = SharedStorage.parse(data[20:20 + n_str], data[20 + n_str:20 + n_str + n_schema])
        elif storage is None:
            raise FormatError('archive uses the shared data storage; pass storage=')
        body = data[20 + n_str + n_schema:]
        dec = _Decoder(body, storage)
        root = dec.tagged()
        if dec.r.i != len(body):
            raise FormatError(f'{len(body) - dec.r.i} bytes left after the root object')
        return cls(root, storage, embedded)

    def to_bytes(self) -> bytes:
        enc = _Encoder(self.storage)
        enc.tagged(self.root)
        body = b''.join(enc.out)
        if self.embedded:
            strings, schema = self.storage.serialize()
        else:
            strings = schema = b''
        return struct.pack('<5I', NEW_MAGIC, 0, len(strings), len(schema), len(body)) + strings + schema + body


_EMBEDDED: 'dict[int, tuple]' = {}


def load(entry_bytes: bytes, storage: SharedStorage | None = None):
    """Parse a .meta / .object / binary .auxiliary entry -> root node (normally an ``Obj``).
    For stand-alone archives (embedded tables) use ``Archive.parse`` to get at the private storage."""
    return Archive.parse(entry_bytes, storage).root


def dump(node, storage: SharedStorage) -> bytes:
    """Serialize a node tree to entry bytes using (and, for new strings / schemas, extending) the
    shared storage."""
    return Archive(node, storage, False).to_bytes()


def is_new_archive(data: bytes) -> bool:
    return len(data) >= 20 and data[:8] == struct.pack('<II', NEW_MAGIC, 0)


def is_legacy_archive(data: bytes) -> bool:
    return len(data) >= 40 and data[:8] == struct.pack('<II', LEGACY_MAGIC1, LEGACY_MAGIC2)


# --------------------------------------------------------------------------------------
# legacy binary archive (magic 0x85301D21 0x076FFC6A) -- self-contained, named values
# --------------------------------------------------------------------------------------
# marker bytes of the compact variant (header flag != 0) / marker dwords of the wide variant (flag 0)
_LEG_COMPACT = {'null': 218, 'array': 69, 'list': 6, 'dict': 154, 'single': 208, 'prim': 173,
                'conv': 45, 'obj': 26, 'list_ti': 7, 'dict_ti': 155}
_LEG_WIDE = {'null': 835255258, 'array': 329718853, 'list': 1459388422, 'dict': -790136934 & 0xFFFFFFFF,
             'single': -800519611 & 0xFFFFFFFF, 'prim': 2918262748, 'conv': 756610662, 'obj': 442951059,
             'list_ti': 1728942359, 'dict_ti': -503805941 & 0xFFFFFFFF}
LEGACY_PRIMITIVES = {  # type string -> struct code  (C# aliases are what the writer emits)
    'bool': '?', 'byte': 'B', 'sbyte': 'b', 'double': 'd', 'float': 'f', 'int': 'i', 'uint': 'I',
    'long': 'q', 'ulong': 'Q', 'short': 'h', 'ushort': 'H', 'char': 'char', 'decimal': '16s',
}
for _k, _v in (('System.Boolean', 'bool'), ('System.Byte', 'byte'), ('System.SByte', 'sbyte'),
               ('System.Double', 'double'), ('System.Single', 'float'), ('System.Int32', 'int'),
               ('System.UInt32', 'uint'), ('System.Int64', 'long'), ('System.UInt64', 'ulong'),
               ('System.Int16', 'short'), ('System.UInt16', 'ushort'), ('System.Char', 'char'),
               ('System.Decimal', 'decimal')):
    LEGACY_PRIMITIVES[_k] = LEGACY_PRIMITIVES[_v]
_LEG_ARRAY_RAW = {k: v for k, v in LEGACY_PRIMITIVES.items() if v in tuple('BbhHiIqQfd')}


class LVal:
    """One named value of a legacy archive.

    kind    value
    ------- ---------------------------------------------------------------
    null    None
    prim    Python value (type = C# alias such as 'bool', 'uint', 'long')
    conv    str  -- TypeConverter string form (type 'System.String', 'System.Guid', ...)
    obj     list[LVal]   (type = '{type-guid}'); dict-like access by child name
    array   list[LVal] | bytes (raw primitive block; type = element type)
    list    list[LVal]            (type = collection type name)
    list_ti list[LVal]            (no type: constructed by the owning object)
    dict    list[[LVal, LVal]]    (type = collection type name)
    dict_ti list[[LVal, LVal]]
    """
    __slots__ = ('kind', 'name', 'type', 'value')

    def __init__(self, kind, name=None, type=None, value=None):
        self.kind, self.name, self.type, self.value = kind, name, type, value

    def __getitem__(self, name):
        if self.kind != 'obj':
            raise TypeError(f'{self.kind} value has no named children')
        for c in self.value:
            if c.name == name:
                return c
        raise KeyError(name)

    def get(self, name, default=None):
        try:
            return self[name]
        except KeyError:
            return default

    def keys(self):
        return [c.name for c in self.value] if self.kind == 'obj' else []

    def __repr__(self):
        v = self.value
        if isinstance(v, (list, bytes)):
            v = f'<{len(v)}>'
        return f'LVal({self.kind}, {self.name!r}, {self.type!r}, {v!r})'


class LegacyArchive:
    """Old-style binary archive: 40 byte header, private UTF-16 string table, named value tree.
    ``strings`` maps id -> str in file order (id 0 means null and is never stored)."""

    def __init__(self, root=None, strings=None, compact=True, flag=1):
        self.root = root
        self.strings = strings if strings is not None else {}
        self.compact = compact
        self.flag = flag

    @classmethod
    def parse(cls, data: bytes) -> 'LegacyArchive':
        m1, m2, n_str, crc_str, n_data, crc_data, flag = struct.unpack_from('<IIIqIqQ', data, 0)
        if (m1, m2) != (LEGACY_MAGIC1, LEGACY_MAGIC2):
            raise FormatError('bad legacy magic')
        if 40 + n_str + n_data != len(data):
            raise FormatError('legacy archive: section lengths do not match size')
        sec_s, sec_d = data[40:40 + n_str], data[40 + n_str:]
        if zlib.crc32(sec_s) != crc_str or zlib.crc32(sec_d) != crc_data:
            raise FormatError('legacy archive: CRC mismatch')
        self = cls(compact=flag != 0, flag=flag)
        r = Reader(sec_s)
        rd = r.mbui if self.compact else (lambda: r.unpack('<I'))
        while not r.eof():
            sid = rd()
            self.strings[sid] = r.take(rd()).decode('utf-16-le', 'surrogatepass')
        r = Reader(sec_d)
        self.root = self._read(r)
        if not r.eof():
            raise FormatError(f'legacy archive: {len(sec_d) - r.i} bytes left after root')
        return self

    def _read(self, r):
        M = _LEG_COMPACT if self.compact else _LEG_WIDE
        num = r.mbui if self.compact else (lambda: r.unpack('<I'))
        mark = r.u8 if self.compact else (lambda: r.unpack('<I'))
        strings = self.strings

        def s():
            i = num()
            if i == 0:
                return None
            try:
                return strings[i]
            except KeyError:
                raise FormatError(f'legacy string id {i} unknown') from None

        def value():
            m = mark()
            if m == M['null']:
                return LVal('null', s())
            if m == M['single']:
                name, typ, sub = s(), s(), mark()
                if sub == M['prim']:
                    code = LEGACY_PRIMITIVES.get(typ)
                    if code is None:
                        raise FormatError(f'legacy primitive of unknown type {typ!r}')
                    if code == 'char':      # BinaryReader.ReadChar, UTF-8
                        b0 = r.b[r.i]
                        n = 1 if b0 < 0x80 else 2 if b0 < 0xE0 else 3
                        v = r.take(n).decode('utf-8', 'surrogatepass')
                    elif code == '16s':
                        v = r.take(16)
                    elif code == '?':
                        v = r.u8()
                        v = bool(v) if v < 2 else v
                    elif code in 'fd':
                        raw = r.take(struct.calcsize(code))
                        v = struct.unpack('<' + code, raw)[0]
                        if struct.pack('<' + code, v) != raw:
                            v = RawFloat(v)
                            v.raw = raw
                    else:
                        v = r.unpack('<' + code)
                    return LVal('prim', name, typ, v)
                if sub == M['conv']:
                    return LVal('conv', name, typ, s())
                if sub == M['obj']:
                    return LVal('obj', name, typ, [value() for _ in range(num())])
                raise FormatError(f'legacy: unknown sub-marker {sub} at {r.i}')
            if m == M['array']:
                name, typ, n = s(), s(), num()
                code = _LEG_ARRAY_RAW.get(typ)
                if code:
                    return LVal('array', name, typ, r.take(n * struct.calcsize(code)))
                return LVal('array', name, typ, [value() for _ in range(n)])
            if m == M['list']:
                name, typ, n = s(), s(), num()
                return LVal('list', name, typ, [value() for _ in range(n)])
            if m == M['list_ti']:
                name, n = s(), num()
                return LVal('list_ti', name, None, [value() for _ in range(n)])
            if m == M['dict']:
                name, typ, n = s(), s(), num()
                return LVal('dict', name, typ, [[value(), value()] for _ in range(n)])
            if m == M['dict_ti']:
                name, n = s(), num()
                return LVal('dict_ti', name, None, [[value(), value()] for _ in range(n)])
            raise FormatError(f'legacy: unknown marker {m} at {r.i}')

        return value()

    def to_bytes(self) -> bytes:
        M = _LEG_COMPACT if self.compact else _LEG_WIDE
        compact = self.compact
        num = mbui if compact else (lambda v: struct.pack('<I', v))
        mark = (lambda v: bytes((v,))) if compact else (lambda v: struct.pack('<I', v))
        ids = {v: k for k, v in reversed(list(self.strings.items()))}
        strings = self.strings
        out = []

        def s(v):
            if v is None:
                out.append(num(0))
                return
            i = ids.get(v)
            if i is None:
                i = max(strings, default=0) + 1
                strings[i] = v
                ids[v] = i
            out.append(num(i))

        def value(x):
            k = x.kind
            if k == 'null':
                out.append(mark(M['null']))
                s(x.name)
            elif k in ('prim', 'conv', 'obj'):
                out.append(mark(M['single']))
                s(x.name)
                s(x.type)
                out.append(mark(M[k]))
                if k == 'prim':
                    code = LEGACY_PRIMITIVES[x.type]
                    v = x.value
                    if code == 'char':
                        out.append(v.encode('utf-8', 'surrogatepass'))
                    elif code == '16s':
                        out.append(bytes(v))
                    elif code == '?':
                        out.append(bytes((int(v),)))
                    elif isinstance(v, RawFloat):
                        out.append(v.raw)
                    else:
                        out.append(struct.pack('<' + code, v))
                elif k == 'conv':
                    s(x.value)
                else:
                    out.append(num(len(x.value)))
                    for c in x.value:
                        value(c)
            elif k == 'array':
                out.append(mark(M['array']))
                s(x.name)
                s(x.type)
                code = _LEG_ARRAY_RAW.get(x.type)
                if code:
                    out.append(num(len(x.value) // struct.calcsize(code)))
                    out.append(bytes(x.value))
                else:
                    out.append(num(len(x.value)))
                    for c in x.value:
                        value(c)
            elif k in ('list', 'list_ti'):
                out.append(mark(M[k]))
                s(x.name)
                if k == 'list':
                    s(x.type)
                out.append(num(len(x.value)))
                for c in x.value:
                    value(c)
            elif k in ('dict', 'dict_ti'):
                out.append(mark(M[k]))
                s(x.name)
                if k == 'dict':
                    s(x.type)
                out.append(num(len(x.value)))
                for kk, vv in x.value:
                    value(kk)
                    value(vv)
            else:
                raise FormatError(f'legacy: unknown kind {k}')

        value(self.root)
        sec_d = b''.join(out)
        st = []
        for i, v in strings.items():
            b = v.encode('utf-16-le', 'surrogatepass')
            st += [num(i), num(len(b)), b]
        sec_s = b''.join(st)
        return struct.pack('<IIIqIqQ', LEGACY_MAGIC1, LEGACY_MAGIC2, len(sec_s), zlib.crc32(sec_s),
                           len(sec_d), zlib.crc32(sec_d), self.flag) + sec_s + sec_d


def load_legacy(data: bytes) -> LegacyArchive:
    return LegacyArchive.parse(data)


def dump_legacy(archive: LegacyArchive) -> bytes:
    return archive.to_bytes()


# --------------------------------------------------------------------------------------
# auxiliary entry classification
# --------------------------------------------------------------------------------------
def raw_deflate(data: bytes) -> bytes:
    """Raw deflate exactly as .NET DeflateStream (zlib level 6, windowBits -15, memLevel 8)."""
    c = zlib.compressobj(6, zlib.DEFLATED, -15, 8)
    return c.compress(data) + c.flush()


def raw_inflate(data: bytes) -> bytes:
    d = zlib.decompressobj(-15)
    out = d.decompress(data)
    if not d.eof or d.unused_data:
        raise FormatError('not a complete raw deflate stream')
    return out


def classify(name: str, data: bytes) -> str:
    """Kind of a zip entry:
    'archive'          new binary archive using the shared storage (.meta, .object)
    'archive-embedded' new binary archive with its own string/schema table
    'legacy'           legacy binary archive
    'deflate-legacy'   raw-deflate stream containing a legacy binary archive
    'string-table' / 'schema-table'
    'json' / 'xml' / 'text' / 'dotnet-binaryformatter' / 'deflate' / 'binary'
    """
    low = name.lower()
    if low == STRING_TABLE_ENTRY:
        return 'string-table'
    if low == SCHEMA_TABLE_ENTRY:
        return 'schema-table'
    if is_new_archive(data):
        n_str, n_schema = struct.unpack_from('<II', data, 8)
        return 'archive-embedded' if n_str or n_schema else 'archive'
    if is_legacy_archive(data):
        return 'legacy'
    if data[:1] == b'{' and data.rstrip()[-1:] == b'}':
        return 'json'
    if data[:17] == b'\x00\x01\x00\x00\x00\xff\xff\xff\xff\x01\x00\x00\x00\x00\x00\x00\x00':
        return 'dotnet-binaryformatter'
    head = data[:200].lstrip(b'\xef\xbb\xbf')
    if head.lstrip()[:1] == b'<':
        return 'xml'
    try:
        inner = raw_inflate(data)
    except Exception:
        inner = None
    if inner is not None:
        return 'deflate-legacy' if is_legacy_archive(inner) else 'deflate'
    try:
        data.decode('utf-8')
        return 'text'
    except UnicodeDecodeError:
        return 'binary'


# --------------------------------------------------------------------------------------
# container (AES) layer
# --------------------------------------------------------------------------------------
def _derive_key(digest: bytes, salt: bytes, rounds: int = 1000) -> bytes:
    """.NET PasswordDeriveBytes(digest, salt, "SHA256", 1000).GetBytes(32)."""
    h = hashlib.sha256(digest + salt).digest()
    for _ in range(rounds - 1):
        h = hashlib.sha256(h).digest()
    return h


def is_container(data: bytes) -> bool:
    return data[:16] == CONTAINER_MAGIC


def container_params(data: bytes) -> tuple[bytes, bytes]:
    """-> (iv, salt) of a sealed container."""
    return data[16:32], data[32:48]


def _aes_cbc(key: bytes, iv: bytes, data: bytes, encrypt: bool) -> bytes:
    """AES-256-CBC: the `cryptography` package when the computer has it, else plain Python (meaes)."""
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError:
        import meaes
        return meaes.encrypt_cbc(key, iv, data) if encrypt else meaes.decrypt_cbc(key, iv, data)
    c = Cipher(algorithms.AES(key), modes.CBC(iv))
    x = c.encryptor() if encrypt else c.decryptor()
    return x.update(data) + x.finalize()


def open_container(data: bytes) -> bytes:
    """Sealed .project bytes -> plain zip bytes (verifies the embedded SHA-256)."""
    if not is_container(data):
        raise FormatError('not an encrypted project container')
    iv, salt, digest = data[16:32], data[32:48], data[48:80]
    body = data[80:]
    if not body or len(body) % 16:
        raise FormatError('container body is not a multiple of the AES block size')
    out = _aes_cbc(_derive_key(digest, salt), iv, body, False)
    pad = out[-1]
    if not 1 <= pad <= 16 or out[-pad:] != bytes((pad,)) * pad:
        raise FormatError('bad PKCS7 padding (wrong key?)')
    out = out[:-pad]
    if hashlib.sha256(out).digest() != digest:
        raise FormatError('SHA-256 of the decrypted data does not match the header')
    return out


def seal_container(zip_bytes: bytes, iv: bytes | None = None, salt: bytes | None = None) -> bytes:
    """Plain zip bytes -> sealed .project bytes.  Random iv / salt unless given."""
    import os
    iv = os.urandom(16) if iv is None else iv
    salt = os.urandom(16) if salt is None else salt
    digest = hashlib.sha256(zip_bytes).digest()
    pad = 16 - len(zip_bytes) % 16
    body = _aes_cbc(_derive_key(digest, salt), iv, zip_bytes + bytes((pad,)) * pad, True)
    return CONTAINER_MAGIC + iv + salt + digest + body


# --------------------------------------------------------------------------------------
# zip layer -- byte-compatible with what Machine Expert writes
# --------------------------------------------------------------------------------------
_STORED_BLOCK = 32763


def _deflate_stored(data: bytes) -> bytes:
    """Machine Expert saves with CompressionLevel.NoCompression: zip method 8 (deflate) whose
    stream consists of *stored* blocks of 32763 bytes, the last one flagged final."""
    out = []
    n = len(data)
    i = 0
    while True:
        chunk = data[i:i + _STORED_BLOCK]
        i += len(chunk)
        final = i >= n
        out.append(struct.pack('<BHH', 1 if final else 0, len(chunk), len(chunk) ^ 0xFFFF))
        out.append(chunk)
        if final:
            return b''.join(out)


def _dos_datetime(dt) -> tuple[int, int]:
    y, mo, d, h, mi, s = dt
    return (h << 11) | (mi << 5) | (s // 2), ((y - 1980) << 9) | (mo << 5) | d


def write_zip(entries, date_time=None) -> bytes:
    """entries: iterable of (name, data[, date_time]).  Layout as written by .NET ZipArchive:
    version 20, flags 0, method 8, no extra fields, no data descriptors, no comments."""
    import time
    if date_time is None:
        date_time = time.localtime()[:6]
    out = io.BytesIO()
    central = []
    count = 0
    for e in entries:
        name, data = e[0], e[1]
        dt = e[2] if len(e) > 2 and e[2] else date_time
        t, d = _dos_datetime(dt)
        try:
            bname = name.encode('ascii')
            flags = 0
        except UnicodeEncodeError:
            bname = name.encode('utf-8')
            flags = 0x800
        comp = _deflate_stored(data)
        crc = zlib.crc32(data)
        off = out.tell()
        out.write(struct.pack('<IHHHHHIIIHH', 0x04034B50, 20, flags, 8, t, d, crc, len(comp), len(data),
                              len(bname), 0))
        out.write(bname)
        out.write(comp)
        central.append(struct.pack('<IHHHHHHIIIHHHHHII', 0x02014B50, 20, 20, flags, 8, t, d, crc, len(comp),
                                   len(data), len(bname), 0, 0, 0, 0, 0, off) + bname)
        count += 1
    cd_off = out.tell()
    cd = b''.join(central)
    out.write(cd)
    out.write(struct.pack('<IHHHHIIH', 0x06054B50, 0, 0, count, count, len(cd), cd_off, 0))
    return out.getvalue()


# --------------------------------------------------------------------------------------
# Project
# --------------------------------------------------------------------------------------
NULL_GUID = '00000000-0000-0000-0000-000000000000'


class Project:
    """All entries of a project file, in zip order.

        p = Project.open('X.project')        # sealed container or plain zip (path or bytes)
        meta = p.load('<guid>.meta')         # -> Obj
        obj  = p.load('<guid>.object')
        obj['Name'] = ...; p.store('<guid>.object', obj)
        p.save('Y.project')                  # sealed again if the source was (new iv/salt unless kept)

    ``entries``    name -> bytes (insertion order = zip order)
    ``date_times`` name -> (y, m, d, h, mi, s) zip timestamp
    ``storage``    the shared string/schema tables (parsed lazily; written back by save() only if
                   something was stored through ``store``/``storage`` was touched via ``mark_dirty``)
    """

    def __init__(self):
        self.entries: dict[str, bytes] = {}
        self.date_times: dict[str, tuple] = {}
        self.sealed = False
        self.iv = None
        self.salt = None
        self._storage = None
        self._storage_dirty = False

    # ---- reading --------------------------------------------------------------------------
    @classmethod
    def open(cls, src) -> 'Project':
        import zipfile
        if isinstance(src, (bytes, bytearray)):
            data = bytes(src)
        else:
            with open(src, 'rb') as f:
                data = f.read()
        self = cls()
        if is_container(data):
            self.sealed = True
            self.iv, self.salt = container_params(data)
            data = open_container(data)
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for info in z.infolist():
                self.entries[info.filename] = z.read(info)
                self.date_times[info.filename] = info.date_time
        return self

    def _find(self, name):
        if name in self.entries:
            return name
        low = name.lower()
        for n in self.entries:
            if n.lower() == low:
                return n
        raise KeyError(name)

    @property
    def storage(self) -> SharedStorage:
        if self._storage is None:
            self._storage = SharedStorage.parse(self.entries.get(self._find(STRING_TABLE_ENTRY), b''),
                                                self.entries.get(self._find(SCHEMA_TABLE_ENTRY), b''))
        return self._storage

    def mark_dirty(self):
        self._storage_dirty = True

    def kind(self, name) -> str:
        name = self._find(name)
        return classify(name, self.entries[name])

    def load(self, name):
        """Decode an entry: Obj (new archive), LegacyArchive (legacy / deflate-legacy), SharedStorage
        (the two tables), or the raw bytes for everything else."""
        name = self._find(name)
        data = self.entries[name]
        k = classify(name, data)
        if k == 'archive':
            return load(data, self.storage)
        if k == 'archive-embedded':
            return Archive.parse(data)
        if k == 'legacy':
            return LegacyArchive.parse(data)
        if k == 'deflate-legacy':
            return LegacyArchive.parse(raw_inflate(data))
        if k in ('string-table', 'schema-table'):
            return self.storage
        return data

    def store(self, name, value, date_time=None, deflate=None):
        """Encode ``value`` into entry ``name`` (created if new).  Obj -> archive on the shared
        storage; Archive / LegacyArchive -> their own format; bytes -> as is.  ``deflate=True``
        wraps a LegacyArchive in raw deflate (default: keep what the existing entry does)."""
        try:
            name = self._find(name)
        except KeyError:
            pass
        if isinstance(value, (bytes, bytearray)):
            data = bytes(value)
        elif isinstance(value, Archive):
            data = value.to_bytes()
            if not value.embedded:
                self._storage_dirty = True
        elif isinstance(value, LegacyArchive):
            data = value.to_bytes()
            if deflate is None:
                deflate = name in self.entries and classify(name, self.entries[name]) == 'deflate-legacy'
            if deflate:
                data = raw_deflate(data)
        else:
            data = dump(value, self.storage)
            self._storage_dirty = True
        self.entries[name] = data
        if date_time:
            self.date_times[name] = date_time

    def remove(self, name):
        name = self._find(name)
        del self.entries[name]
        self.date_times.pop(name, None)

    # ---- object tree ----------------------------------------------------------------------
    def object_guids(self):
        return [n[:-5] for n in self.entries if n.lower().endswith('.meta')]

    def metas(self):
        """-> {guid: meta Obj}"""
        return {g: self.load(g + '.meta') for g in self.object_guids()}

    # ---- writing --------------------------------------------------------------------------
    def flush_storage(self):
        if self._storage is not None and self._storage_dirty:
            strings, schema = self._storage.serialize()
            self.entries[self._find_or(STRING_TABLE_ENTRY)] = strings
            self.entries[self._find_or(SCHEMA_TABLE_ENTRY)] = schema
            self._storage_dirty = False

    def _find_or(self, name):
        try:
            return self._find(name)
        except KeyError:
            return name

    def to_zip_bytes(self, date_time=None) -> bytes:
        """``date_time``: stamp for *all* entries (Machine Expert stamps every entry with the save
        time); default keeps the stored per-entry stamps (now for new entries)."""
        self.flush_storage()
        if date_time:
            return write_zip(((n, d) for n, d in self.entries.items()), date_time)
        return write_zip((n, d, self.date_times.get(n)) for n, d in self.entries.items())

    def to_bytes(self, sealed=None, iv=None, salt=None, date_time=None, keep_iv=False) -> bytes:
        z = self.to_zip_bytes(date_time)
        if self.sealed if sealed is None else sealed:
            if keep_iv:
                iv, salt = iv or self.iv, salt or self.salt
            return seal_container(z, iv, salt)
        return z

    def save(self, path, **kw):
        data = self.to_bytes(**kw)
        with open(path, 'wb') as f:
            f.write(data)
        return len(data)


# --------------------------------------------------------------------------------------
# pretty printing
# --------------------------------------------------------------------------------------
KNOWN_TYPES = {
    '6f9dac99-8de1-4efc-8465-68ac443b7d08': 'POU', 'f8a58466-d7f6-439f-bbb8-d4600e41d099': 'Method',
    '5a3b8626-d3e9-4f37-98b5-66420063d91e': 'Property', '792f2eb6-721e-4e64-ba20-bc98351056db': 'Accessor',
    '8ac092e5-3128-4e26-9e7e-11016c6684f2': 'Action', '6654496c-404d-479a-aad2-8551054e5f1e': 'Interface',
    'f89f7675-27f1-46b3-8abb-b7da8e774ffd': 'InterfaceMethod', 'ffbfa93a-b94d-45fc-a329-229860183b1d': 'GVL',
    '261bd6e6-249c-4232-bb6f-84c2fbeef430': 'PersistentVars', '2db5746d-d284-4425-9f7f-2663a34b0ebc': 'DUT',
    '738bea1e-99bb-4f04-90bb-a7a567e74e3a': 'Folder', '225bfe47-7336-4dbc-9419-4105a7c831fa': 'Device',
    '639b491f-5557-464c-af91-1471bac9f549': 'Application', 'ae1de277-a207-4a28-9efb-456c06bd52f3': 'TaskConfiguration',
    '98a2708a-9b18-4f31-82ed-a1465b24fa2d': 'Task', 'adb5cb65-8e1d-4a00-b70a-375ea27582f3': 'LibraryManager',
    'f18bec89-9fef-401d-9953-2f11739a6808': 'Visualization', '8753fe6f-4a22-4320-8103-e553c4fc8e04': 'ProjectSettings',
    '085afe48-c5d8-4ea5-ab0d-b35701fa6009': 'ProjectInformation',
}


def _short(v, maxlen):
    s = repr(v)
    if maxlen and len(s) > maxlen:
        s = s[:maxlen] + f'...<{len(s)} chars>'
    return s


def _fmt_tag(tag):
    if tag is None:
        return '?'
    f, c = tag
    n = FORMAT_NAMES.get(f, hex(f))
    return f'{n}<{c}>' if c else n


def _head(x, maxlen=120):
    if isinstance(x, Obj):
        if x.is_null:
            return 'Obj null'
        kn = KNOWN_TYPES.get(x.type_guid)
        return f'Obj {x.type_guid}{" (" + kn + ")" if kn else ""} schema={x.schema_id}'
    if isinstance(x, Coll):
        e = f' of {_fmt_tag(x.elem)}' if x.elem else ''
        return f'{_fmt_tag((x.fmt, x.ctype))}{e} [{len(x.items)}]'
    if isinstance(x, Dict):
        e = f' {_fmt_tag(x.ktag) if x.ktag else "*"} -> {_fmt_tag(x.vtag) if x.vtag else "*"}'
        return f'{_fmt_tag((x.fmt, x.ctype))}{e} {{{len(x.items)}}}'
    if isinstance(x, Prim):
        return f'{_fmt_tag((x.fmt, x.ctype))} = {_short(x.value, maxlen)}'
    return _short(x, maxlen)


def format_node(v, indent=0, maxlen=120, max_items=0, out=None):
    """Indented text rendering of a node tree -> list of lines."""
    if out is None:
        out = ['  ' * indent + _head(v, maxlen)]
    pad = '  ' * (indent + 1)

    def child(label, x, tag=None):
        if isinstance(x, (Obj, Coll, Dict)):
            out.append(f'{pad}{label}: {_head(x, maxlen)}')
            format_node(x, indent + 1, maxlen, max_items, out)
        elif isinstance(x, Prim) or tag is None:
            out.append(f'{pad}{label}: {_head(x, maxlen)}')
        else:
            out.append(f'{pad}{label}: {_fmt_tag(tag)} = {_short(x, maxlen)}')

    if isinstance(v, Obj):
        for name, t, x in zip(v.names, v.tags, v.values):
            child(name, x, t)
    elif isinstance(v, Coll):
        if isinstance(v.items, (bytes, bytearray)):
            out.append(f'{pad}raw: {_short(bytes(v.items), maxlen)}')
        else:
            for i, x in enumerate(v.items):
                if max_items and i >= max_items:
                    out.append(f'{pad}... {len(v.items) - i} more')
                    break
                child(f'[{i}]', x)
    elif isinstance(v, Dict):
        for i, (k, x) in enumerate(v.items):
            if max_items and i >= max_items:
                out.append(f'{pad}... {len(v.items) - i} more')
                break
            if isinstance(k, (Obj, Coll, Dict)):
                child(f'key#{i}', k)
                child(f'val#{i}', x)
            else:
                child(f'{{{_short(k.value if isinstance(k, Prim) else k, 60)}}}', x)
    return out


def format_legacy(x, indent=0, maxlen=120, out=None, max_items=None):
    if out is None:
        out = []
    pad = '  ' * indent
    label = f'{x.name}: ' if x.name is not None else ''
    if x.kind in ('null', 'prim', 'conv'):
        t = f'{x.type} = ' if x.type else ''
        out.append(f'{pad}{label}{x.kind} {t}{_short(x.value, maxlen)}')
    elif x.kind == 'array' and isinstance(x.value, (bytes, bytearray)):
        out.append(f'{pad}{label}array<{x.type}> raw {_short(bytes(x.value), maxlen)}')
    elif x.kind in ('obj', 'array', 'list', 'list_ti'):
        out.append(f'{pad}{label}{x.kind}<{x.type}> [{len(x.value)}]')
        for i, c in enumerate(x.value):
            if max_items and i >= max_items:
                out.append(f'{pad}  ... {len(x.value) - i} more')
                break
            format_legacy(c, indent + 1, maxlen, out, max_items)
    else:
        out.append(f'{pad}{label}{x.kind}<{x.type}> {{{len(x.value)}}}')
        for i, (k, v) in enumerate(x.value):
            if max_items and i >= max_items:
                out.append(f'{pad}  ... {len(x.value) - i} more')
                break
            out.append(f'{pad}  key:')
            format_legacy(k, indent + 2, maxlen, out, max_items)
            out.append(f'{pad}  value:')
            format_legacy(v, indent + 2, maxlen, out, max_items)
    return out


def to_jsonable(v, maxlen=0):
    """Lossy JSON-friendly view of a node tree (for inspection, not for round trips)."""
    def cut(s):
        return s if not maxlen or len(s) <= maxlen else s[:maxlen] + f'...<{len(s)}>'
    if isinstance(v, Obj):
        if v.is_null:
            return None
        d = {'$type': v.type_guid, '$schema': v.schema_id}
        for n, x in zip(v.names, v.values):
            d[n] = to_jsonable(x, maxlen)
        return d
    if isinstance(v, Coll):
        if isinstance(v.items, (bytes, bytearray)):
            return {'$bytes': cut(bytes(v.items).hex())}
        return [to_jsonable(x, maxlen) for x in v.items]
    if isinstance(v, Dict):
        return {'$dict': [[to_jsonable(k, maxlen), to_jsonable(x, maxlen)] for k, x in v.items]}
    if isinstance(v, Prim):
        return to_jsonable(v.value, maxlen)
    if isinstance(v, (bytes, bytearray)):
        return {'$bytes': cut(bytes(v).hex())}
    if isinstance(v, str):
        return cut(v)
    if isinstance(v, float) and v != v:
        return 'NaN'
    if isinstance(v, LVal):
        if v.kind in ('null', 'prim', 'conv'):
            return to_jsonable(v.value, maxlen)
        if v.kind == 'obj':
            d = {'$type': v.type}
            for c in v.value:
                d[c.name] = to_jsonable(c, maxlen)
            return d
        if v.kind in ('dict', 'dict_ti'):
            return {'$dict': [[to_jsonable(k, maxlen), to_jsonable(x, maxlen)] for k, x in v.value]}
        if isinstance(v.value, (bytes, bytearray)):
            return {'$bytes': cut(bytes(v.value).hex())}
        return [to_jsonable(c, maxlen) for c in v.value]
    return v


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------
def _cli(argv):
    import argparse
    import json
    ap = argparse.ArgumentParser(prog='mearchive.py', description=__doc__.split('\n')[0])
    sub = ap.add_subparsers(dest='cmd', required=True)
    d = sub.add_parser('dump', help='print object trees')
    d.add_argument('file')
    d.add_argument('--entry', action='append', help='entry name, guid or substring (repeatable); default: all archives')
    d.add_argument('--json', action='store_true')
    d.add_argument('--max', type=int, default=120, help='truncate values to N chars (0 = no limit)')
    d.add_argument('--max-items', type=int, default=0, help='show at most N items per collection')
    d.add_argument('--raw', action='store_true', help='also print text/xml/json entries')
    l = sub.add_parser('list', help='list entries: kind, size, name (and object name/type for metas)')
    l.add_argument('file')
    s = sub.add_parser('schemas', help='print the schema table')
    s.add_argument('file')
    u = sub.add_parser('unseal', help='decrypt a .project container to a plain zip')
    u.add_argument('file')
    u.add_argument('out')
    a = ap.parse_args(argv)
    if a.cmd == 'unseal':
        with open(a.file, 'rb') as f:
            z = open_container(f.read())
        with open(a.out, 'wb') as f:
            f.write(z)
        return 0
    p = Project.open(a.file)
    if a.cmd == 'list':
        names = {}
        for g in p.object_guids():
            try:
                m = p.load(g + '.meta')
                names[g] = f"{m.get('Name')!r} type={KNOWN_TYPES.get(m.get('TypeGuid'), m.get('TypeGuid'))} parent={m.get('ParentGuid')}"
            except Exception as e:      # noqa
                names[g] = f'<{e}>'
        for n, data in p.entries.items():
            extra = names.get(n.rsplit('.', 1)[0], '') if n.endswith(('.meta', '.object')) else ''
            print(f'{classify(n, data):22} {len(data):9}  {n}  {extra}')
        return 0
    if a.cmd == 'schemas':
        st = p.storage
        for e in st.schemas.values():
            print(f'{e.id}: {e.type_guid} {KNOWN_TYPES.get(e.type_guid, "")}')
            for n, f, c in e.members:
                print(f'    {n}: {_fmt_tag((f, c))}')
        return 0
    sel = a.entry
    for n, data in p.entries.items():
        if sel and not any(x.lower() in n.lower() for x in sel):
            continue
        k = classify(n, data)
        if k in ('archive', 'archive-embedded'):
            root = Archive.parse(data, p.storage).root
            print(f'=== {n} [{k}, {len(data)} bytes]')
            if a.json:
                print(json.dumps(to_jsonable(root, a.max), ensure_ascii=False, indent=1))
            else:
                print('\n'.join(format_node(root, 0, a.max, a.max_items)))
        elif k in ('legacy', 'deflate-legacy'):
            arc = LegacyArchive.parse(raw_inflate(data) if k == 'deflate-legacy' else data)
            print(f'=== {n} [{k}, {len(data)} bytes]')
            if a.json:
                print(json.dumps(to_jsonable(arc.root, a.max), ensure_ascii=False, indent=1))
            else:
                print('\n'.join(format_legacy(arc.root, 0, a.max, max_items=a.max_items)))
        elif sel or a.raw:
            print(f'=== {n} [{k}, {len(data)} bytes]')
            if k in ('json', 'xml', 'text'):
                t = data.decode('utf-8', 'replace')
                print(t if not a.max or len(t) < a.max * 40 else t[:a.max * 40] + '...')
            elif k == 'string-table':
                for i, s_ in p.storage.strings.items():
                    print(i, _short(s_, a.max))
            elif k == 'schema-table':
                print(f'{len(p.storage.schemas)} schemas; use the "schemas" command')
            else:
                print(_short(data, a.max * 4 if a.max else 0))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(_cli(sys.argv[1:]))
    except BrokenPipeError:
        pass
