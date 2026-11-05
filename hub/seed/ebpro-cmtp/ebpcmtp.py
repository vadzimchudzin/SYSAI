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


# Convenience module-level API --------------------------------------------------
def unwrap(data) -> Project:
    return Project.load(data)
