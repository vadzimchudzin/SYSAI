"""AES-256 in CBC mode in plain Python — for the container of a `.project` file when the `cryptography` package is
not on the computer. Table-driven (four T-tables for each direction); a project of a few megabytes takes seconds."""
import struct

_SBOX = [0] * 256
_INV = [0] * 256


def _init():
    p = q = 1
    while True:
        p ^= ((p << 1) ^ (0x1B if p & 0x80 else 0)) & 0xFF          # p *= 3
        q ^= q << 1
        q ^= q << 2
        q ^= q << 4
        q &= 0xFF
        if q & 0x80:
            q ^= 0x09                                                # q /= 3
        x = q ^ (q << 1 | q >> 7) ^ (q << 2 | q >> 6) ^ (q << 3 | q >> 5) ^ (q << 4 | q >> 4)
        _SBOX[p] = (x ^ 0x63) & 0xFF
        if p == 1:
            break
    _SBOX[0] = 0x63
    for i, s in enumerate(_SBOX):
        _INV[s] = i


_init()


def _mul(a, b):
    r = 0
    while b:
        if b & 1:
            r ^= a
        a = ((a << 1) ^ (0x11B if a & 0x80 else 0)) & 0xFF
        b >>= 1
    return r


def _rot(w, n):
    return ((w >> n) | (w << (32 - n))) & 0xFFFFFFFF


_TE0 = [(_mul(s, 2) << 24) | (s << 16) | (s << 8) | _mul(s, 3) for s in _SBOX]
_TE1, _TE2, _TE3 = [_rot(w, 8) for w in _TE0], [_rot(w, 16) for w in _TE0], [_rot(w, 24) for w in _TE0]
_TD0 = [(_mul(s, 14) << 24) | (_mul(s, 9) << 16) | (_mul(s, 13) << 8) | _mul(s, 11) for s in _INV]
_TD1, _TD2, _TD3 = [_rot(w, 8) for w in _TD0], [_rot(w, 16) for w in _TD0], [_rot(w, 24) for w in _TD0]


def _expand(key: bytes):
    if len(key) != 32:
        raise ValueError('AES-256 needs a key of 32 bytes')
    w = list(struct.unpack('>8I', key))
    rcon = 1
    for i in range(8, 60):
        t = w[i - 1]
        if i % 8 == 0:
            t = ((t << 8) | (t >> 24)) & 0xFFFFFFFF
            t = (_SBOX[t >> 24] << 24) | (_SBOX[(t >> 16) & 255] << 16) | (_SBOX[(t >> 8) & 255] << 8) | _SBOX[t & 255]
            t ^= rcon << 24
            rcon = _mul(rcon, 2)
        elif i % 8 == 4:
            t = (_SBOX[t >> 24] << 24) | (_SBOX[(t >> 16) & 255] << 16) | (_SBOX[(t >> 8) & 255] << 8) | _SBOX[t & 255]
        w.append(w[i - 8] ^ t)
    return w


def _expand_dec(key: bytes):
    w = _expand(key)
    d = []
    for r in range(14, -1, -1):
        d += w[4 * r:4 * r + 4]
    for i in range(4, 56):          # the middle round keys go through InvMixColumns
        t = d[i]
        d[i] = (_TD0[_SBOX[t >> 24]] ^ _TD1[_SBOX[(t >> 16) & 255]] ^ _TD2[_SBOX[(t >> 8) & 255]] ^ _TD3[_SBOX[t & 255]])
    return d


def encrypt_cbc(key: bytes, iv: bytes, data: bytes) -> bytes:
    """`data` must be a whole number of 16-byte blocks (pad it first)."""
    if len(data) % 16:
        raise ValueError('data is not a whole number of blocks')
    w = _expand(key)
    te0, te1, te2, te3, sb = _TE0, _TE1, _TE2, _TE3, _SBOX
    p0, p1, p2, p3 = struct.unpack('>4I', iv)
    n = len(data) // 4
    words = struct.unpack(f'>{n}I', data)
    out = []
    for b in range(0, n, 4):
        s0 = words[b] ^ p0 ^ w[0]
        s1 = words[b + 1] ^ p1 ^ w[1]
        s2 = words[b + 2] ^ p2 ^ w[2]
        s3 = words[b + 3] ^ p3 ^ w[3]
        k = 4
        for _ in range(13):
            t0 = te0[s0 >> 24] ^ te1[(s1 >> 16) & 255] ^ te2[(s2 >> 8) & 255] ^ te3[s3 & 255] ^ w[k]
            t1 = te0[s1 >> 24] ^ te1[(s2 >> 16) & 255] ^ te2[(s3 >> 8) & 255] ^ te3[s0 & 255] ^ w[k + 1]
            t2 = te0[s2 >> 24] ^ te1[(s3 >> 16) & 255] ^ te2[(s0 >> 8) & 255] ^ te3[s1 & 255] ^ w[k + 2]
            t3 = te0[s3 >> 24] ^ te1[(s0 >> 16) & 255] ^ te2[(s1 >> 8) & 255] ^ te3[s2 & 255] ^ w[k + 3]
            s0, s1, s2, s3 = t0, t1, t2, t3
            k += 4
        p0 = ((sb[s0 >> 24] << 24) | (sb[(s1 >> 16) & 255] << 16) | (sb[(s2 >> 8) & 255] << 8) | sb[s3 & 255]) ^ w[56]
        p1 = ((sb[s1 >> 24] << 24) | (sb[(s2 >> 16) & 255] << 16) | (sb[(s3 >> 8) & 255] << 8) | sb[s0 & 255]) ^ w[57]
        p2 = ((sb[s2 >> 24] << 24) | (sb[(s3 >> 16) & 255] << 16) | (sb[(s0 >> 8) & 255] << 8) | sb[s1 & 255]) ^ w[58]
        p3 = ((sb[s3 >> 24] << 24) | (sb[(s0 >> 16) & 255] << 16) | (sb[(s1 >> 8) & 255] << 8) | sb[s2 & 255]) ^ w[59]
        out += (p0, p1, p2, p3)
    return struct.pack(f'>{n}I', *out)


def decrypt_cbc(key: bytes, iv: bytes, data: bytes) -> bytes:
    if len(data) % 16:
        raise ValueError('data is not a whole number of blocks')
    w = _expand_dec(key)
    td0, td1, td2, td3, inv = _TD0, _TD1, _TD2, _TD3, _INV
    p0, p1, p2, p3 = struct.unpack('>4I', iv)
    n = len(data) // 4
    words = struct.unpack(f'>{n}I', data)
    out = []
    for b in range(0, n, 4):
        c0, c1, c2, c3 = words[b], words[b + 1], words[b + 2], words[b + 3]
        s0, s1, s2, s3 = c0 ^ w[0], c1 ^ w[1], c2 ^ w[2], c3 ^ w[3]
        k = 4
        for _ in range(13):
            t0 = td0[s0 >> 24] ^ td1[(s3 >> 16) & 255] ^ td2[(s2 >> 8) & 255] ^ td3[s1 & 255] ^ w[k]
            t1 = td0[s1 >> 24] ^ td1[(s0 >> 16) & 255] ^ td2[(s3 >> 8) & 255] ^ td3[s2 & 255] ^ w[k + 1]
            t2 = td0[s2 >> 24] ^ td1[(s1 >> 16) & 255] ^ td2[(s0 >> 8) & 255] ^ td3[s3 & 255] ^ w[k + 2]
            t3 = td0[s3 >> 24] ^ td1[(s2 >> 16) & 255] ^ td2[(s1 >> 8) & 255] ^ td3[s0 & 255] ^ w[k + 3]
            s0, s1, s2, s3 = t0, t1, t2, t3
            k += 4
        o0 = ((inv[s0 >> 24] << 24) | (inv[(s3 >> 16) & 255] << 16) | (inv[(s2 >> 8) & 255] << 8) | inv[s1 & 255]) ^ w[56] ^ p0
        o1 = ((inv[s1 >> 24] << 24) | (inv[(s0 >> 16) & 255] << 16) | (inv[(s3 >> 8) & 255] << 8) | inv[s2 & 255]) ^ w[57] ^ p1
        o2 = ((inv[s2 >> 24] << 24) | (inv[(s1 >> 16) & 255] << 16) | (inv[(s0 >> 8) & 255] << 8) | inv[s3 & 255]) ^ w[58] ^ p2
        o3 = ((inv[s3 >> 24] << 24) | (inv[(s2 >> 16) & 255] << 16) | (inv[(s1 >> 8) & 255] << 8) | inv[s0 & 255]) ^ w[59] ^ p3
        out += (o0, o1, o2, o3)
        p0, p1, p2, p3 = c0, c1, c2, c3
    return struct.pack(f'>{n}I', *out)
