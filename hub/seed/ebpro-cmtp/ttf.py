"""A small TrueType reader and rasteriser (glyf outlines, cmap 4/12, composite glyphs) for drawing labels without
extra packages. Fonts are taken from the system folders (Windows Fonts, /usr/share/fonts...)."""
import math
import os
import struct

_DIRS = [os.path.join(os.environ.get('WINDIR', r'C:\Windows'), 'Fonts'),
         os.path.join(os.environ.get('LOCALAPPDATA', os.path.expanduser('~')), 'Microsoft', 'Windows', 'Fonts'),
         '/usr/share/fonts', '/usr/local/share/fonts', os.path.expanduser('~/.fonts'),
         os.path.expanduser('~/.local/share/fonts'), '/Library/Fonts', '/System/Library/Fonts']
FALLBACK = ('droid sans fallback', 'droid sans', 'arial', 'liberation sans', 'dejavu sans', 'noto sans',
            'microsoft sans serif', 'tahoma', 'verdana')


class Font:
    def __init__(self, path: str):
        self.path = path
        with open(path, 'rb') as f:
            d = self.d = f.read()
        n = struct.unpack_from('>H', d, 4)[0]
        self.t = {}
        for i in range(n):
            tag, _, off, ln = struct.unpack_from('>4sIII', d, 12 + 16 * i)
            self.t[tag.decode('latin-1')] = (off, ln)
        if 'glyf' not in self.t or 'loca' not in self.t:
            raise ValueError('not a TrueType outline font')
        h = self.t['head'][0]
        self.upm = struct.unpack_from('>H', d, h + 18)[0]
        self.loc_long = struct.unpack_from('>h', d, h + 50)[0] == 1
        hh = self.t['hhea'][0]
        self.ascent, self.descent = struct.unpack_from('>hh', d, hh + 4)
        self.nhm = struct.unpack_from('>H', d, hh + 34)[0]
        self.nglyphs = struct.unpack_from('>H', d, self.t['maxp'][0] + 4)[0]
        self.cmap = self._cmap()
        self.cache = {}

    def _cmap(self) -> dict:
        d, base = self.d, self.t['cmap'][0]
        n = struct.unpack_from('>H', d, base + 2)[0]
        best = None
        for i in range(n):
            pid, eid, off = struct.unpack_from('>HHI', d, base + 4 + 8 * i)
            fmt = struct.unpack_from('>H', d, base + off)[0]
            rank = {(3, 10, 12): 0, (0, 4, 12): 0, (3, 1, 4): 1, (0, 3, 4): 1, (0, 1, 4): 2}.get((pid, eid, fmt))
            if rank is not None and (best is None or rank < best[0]):
                best = (rank, base + off, fmt)
        out = {}
        if best is None:
            return out
        _, p, fmt = best
        if fmt == 4:
            segx2 = struct.unpack_from('>H', d, p + 6)[0]
            ends, starts = p + 14, p + 16 + segx2
            deltas, ranges = starts + segx2, starts + 2 * segx2
            for s in range(segx2 // 2):
                end = struct.unpack_from('>H', d, ends + 2 * s)[0]
                start = struct.unpack_from('>H', d, starts + 2 * s)[0]
                delta = struct.unpack_from('>h', d, deltas + 2 * s)[0]
                ro = struct.unpack_from('>H', d, ranges + 2 * s)[0]
                for c in range(start, min(end, 0xfffe) + 1):
                    if ro == 0:
                        g = (c + delta) & 0xffff
                    else:
                        q = ranges + 2 * s + ro + 2 * (c - start)
                        g = struct.unpack_from('>H', d, q)[0]
                        g = (g + delta) & 0xffff if g else 0
                    if g:
                        out[c] = g
        elif fmt == 12:
            ng = struct.unpack_from('>I', d, p + 12)[0]
            for i in range(ng):
                a, b, g = struct.unpack_from('>III', d, p + 16 + 12 * i)
                for c in range(a, min(b, a + 70000) + 1):
                    out[c] = g + c - a
        return out

    def advance(self, g: int) -> int:
        p = self.t['hmtx'][0]
        return struct.unpack_from('>H', self.d, p + 4 * min(g, self.nhm - 1))[0]

    def _glyph_span(self, g: int):
        lo = self.t['loca'][0]
        if g >= self.nglyphs:
            return 0, 0
        if self.loc_long:
            a, b = struct.unpack_from('>II', self.d, lo + 4 * g)
        else:
            a, b = (2 * v for v in struct.unpack_from('>HH', self.d, lo + 2 * g))
        return self.t['glyf'][0] + a, b - a

    def contours(self, g: int, depth: int = 0) -> list:
        """Contours of glyph g in font units: [[(x, y, on_curve), ...], ...]."""
        if g in self.cache:
            return self.cache[g]
        p, ln = self._glyph_span(g)
        out = []
        if ln > 0:
            d = self.d
            nc = struct.unpack_from('>h', d, p)[0]
            if nc >= 0:
                ends = struct.unpack_from(f'>{nc}H', d, p + 10)
                npts = ends[-1] + 1 if nc else 0
                q = p + 10 + 2 * nc
                q += 2 + struct.unpack_from('>H', d, q)[0]
                flags = []
                while len(flags) < npts:
                    f = d[q]
                    q += 1
                    flags.append(f)
                    if f & 8:
                        flags += [f] * d[q]
                        q += 1
                xs, ys = [], []
                for coords, short, same in ((xs, 2, 16), (ys, 4, 32)):
                    v = 0
                    for f in flags[:npts]:
                        if f & short:
                            dv = d[q]
                            q += 1
                            v += dv if f & same else -dv
                        elif not f & same:
                            v += struct.unpack_from('>h', d, q)[0]
                            q += 2
                        coords.append(v)
                s = 0
                for e in ends:
                    out.append([(xs[i], ys[i], flags[i] & 1) for i in range(s, e + 1)])
                    s = e + 1
            elif depth < 8:
                q = p + 10
                while True:
                    fl, cg = struct.unpack_from('>HH', d, q)
                    q += 4
                    if fl & 1:
                        a1, a2 = struct.unpack_from('>hh', d, q)
                        q += 4
                    else:
                        a1, a2 = struct.unpack_from('>bb', d, q)
                        q += 2
                    m = (1.0, 0.0, 0.0, 1.0)
                    if fl & 8:
                        s1 = struct.unpack_from('>h', d, q)[0] / 16384
                        q += 2
                        m = (s1, 0.0, 0.0, s1)
                    elif fl & 0x40:
                        s1, s2 = (v / 16384 for v in struct.unpack_from('>hh', d, q))
                        q += 4
                        m = (s1, 0.0, 0.0, s2)
                    elif fl & 0x80:
                        m = tuple(v / 16384 for v in struct.unpack_from('>hhhh', d, q))
                        q += 8
                    dx, dy = (a1, a2) if fl & 2 else (0, 0)
                    for c in self.contours(cg, depth + 1):
                        out.append([(m[0] * x + m[2] * y + dx, m[1] * x + m[3] * y + dy, on) for x, y, on in c])
                    if not fl & 0x20:
                        break
        self.cache[g] = out
        return out

    def glyph(self, ch: str) -> int:
        return self.cmap.get(ord(ch), 0)

    def width(self, text: str, px: float) -> float:
        return sum(self.advance(self.glyph(c)) for c in text) * px / self.upm

    def render(self, text: str, px: float):
        return Chain([self]).render(text, px)


class Chain:
    """Fonts tried in turn for every character (the first one that has it), metrics of the first."""

    def __init__(self, fonts: list):
        self.fonts = fonts
        f = fonts[0]
        self.upm, self.ascent, self.descent = f.upm, f.ascent, f.descent
        self.fake_bold = getattr(f, 'fake_bold', False)

    def _pick(self, ch: str):
        for f in self.fonts:
            g = f.glyph(ch)
            if g:
                return f, g
        return self.fonts[0], 0

    def width(self, text: str, px: float) -> float:
        w = 0.0
        for ch in text:
            f, g = self._pick(ch)
            w += f.advance(g) * px / f.upm
        return w

    def render(self, text: str, px: float):
        """(width, height, baseline, alpha bytes) of one line of text at px pixels per em."""
        s0 = px / self.upm
        asc, desc = self.ascent * s0, -self.descent * s0
        h = max(1, int(asc + desc + 2))
        base = asc + 1
        edges, pen = [], 1.0
        for ch in text:
            f, g = self._pick(ch)
            s = px / f.upm
            for c in f.contours(g):
                pts = _flatten(c, s, pen, base)
                for i in range(len(pts)):
                    x0, y0 = pts[i - 1]
                    x1, y1 = pts[i]
                    if y0 != y1:
                        edges.append((x0, y0, x1, y1))
            pen += f.advance(g) * s
        w = max(1, int(pen + 2))
        return w, h, base, _raster(edges, w, h)


def _flatten(c, s, ox, base) -> list:
    """A quadratic contour as points in pixels (y down)."""
    n = len(c)
    if not n:
        return []
    pts = [(x * s + ox, base - y * s, on) for x, y, on in c]
    start = next((i for i in range(n) if pts[i][2]), None)
    if start is None:   # only off-curve points: start in the middle of the first two
        a, b = pts[0], pts[1 % n]
        pts.insert(0, ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2, 1))
        start, n = 0, n + 1
    seq = pts[start:] + pts[:start]
    out = [(seq[0][0], seq[0][1])]
    prev = seq[0]
    i = 1
    while i <= n:
        p = seq[i % n]
        if p[2]:
            out.append((p[0], p[1]))
            prev = p
            i += 1
            continue
        nxt = seq[(i + 1) % n]
        end = nxt if nxt[2] else ((p[0] + nxt[0]) / 2, (p[1] + nxt[1]) / 2, 1)
        steps = max(2, min(12, int((abs(end[0] - prev[0]) + abs(end[1] - prev[1])) / 2)))
        for k in range(1, steps + 1):
            t = k / steps
            a = (1 - t) * (1 - t)
            b = 2 * (1 - t) * t
            cc = t * t
            out.append((a * prev[0] + b * p[0] + cc * end[0], a * prev[1] + b * p[1] + cc * end[1]))
        prev = end
        i += 2 if nxt[2] else 1
    return out


def _raster(edges, w, h, ss=4) -> bytes:
    """Non-zero fill of the edges with ss sub-scanlines per pixel row -> alpha per pixel."""
    rows = [[] for _ in range(h * ss)]
    for x0, y0, x1, y1 in edges:
        d = 1 if y1 > y0 else -1
        if y0 > y1:
            x0, y0, x1, y1 = x1, y1, x0, y0
        a = max(0, math.ceil(y0 * ss - 0.5))
        b = min(h * ss - 1, math.ceil(y1 * ss - 0.5) - 1)
        k = (x1 - x0) / (y1 - y0)
        for r in range(a, b + 1):
            rows[r].append((x0 + ((r + 0.5) / ss - y0) * k, d))
    cov = [0.0] * (w * h)
    unit = 1.0 / ss
    for r in range(h * ss):
        xs = rows[r]
        if len(xs) < 2:
            continue
        xs.sort()
        base = (r // ss) * w
        wind = 0
        for i in range(len(xs) - 1):
            wind += xs[i][1]
            if not wind:
                continue
            xa, xb = max(0.0, xs[i][0]), min(w - 0.001, xs[i + 1][0])
            if xb <= xa:
                continue
            ia, ib = int(xa), int(xb)
            if ia == ib:
                cov[base + ia] += (xb - xa) * unit
            else:
                cov[base + ia] += (ia + 1 - xa) * unit
                for j in range(base + ia + 1, base + ib):
                    cov[j] += unit
                cov[base + ib] += (xb - ib) * unit
    return bytes(min(255, int(c * 255 + 0.5)) for c in cov)


# -- finding fonts ---------------------------------------------------------------------------------------------------
_INDEX = None
_FONTS = {}


def _names(path: str):
    """(family, subfamily) from the name table, lower case."""
    with open(path, 'rb') as f:
        head = f.read(12)
        if len(head) < 12 or head[:4] not in (b'\x00\x01\x00\x00', b'true'):
            return None
        n = struct.unpack_from('>H', head, 4)[0]
        dirs = f.read(16 * n)
        for i in range(n):
            tag, _, off, ln = struct.unpack_from('>4sIII', dirs, 16 * i)
            if tag == b'name':
                f.seek(off)
                d = f.read(ln)
                break
        else:
            return None
    cnt, so = struct.unpack_from('>HH', d, 2)
    got = {}
    for i in range(cnt):
        pid, eid, lid, nid, ln, off = struct.unpack_from('>HHHHHH', d, 6 + 12 * i)
        if nid not in (1, 2):
            continue
        raw = d[so + off:so + off + ln]
        try:
            s = raw.decode('utf-16-be') if pid in (0, 3) else raw.decode('latin-1')
        except UnicodeDecodeError:
            continue
        if nid not in got or (pid == 3 and lid == 0x409):
            got[nid] = s
    return (got.get(1, '').strip().lower(), got.get(2, '').strip().lower()) if 1 in got else None


def index() -> dict:
    """{family: {subfamily: path}} of the TrueType fonts in the system folders."""
    global _INDEX
    if _INDEX is None:
        _INDEX = {}
        for top in _DIRS:
            if not os.path.isdir(top):
                continue
            for root, _, files in os.walk(top):
                for fn in files:
                    if not fn.lower().endswith('.ttf'):
                        continue
                    path = os.path.join(root, fn)
                    try:
                        nm = _names(path)
                    except (OSError, struct.error):
                        nm = None
                    if nm:
                        _INDEX.setdefault(nm[0], {}).setdefault(nm[1], path)
    return _INDEX


def find(name: str, bold: bool = False):
    """A Font for an EasyBuilder font name ('Arial', 'Droid Sans Fallback', 'Tahoma Bold'...), falling back to
    common sans fonts; None if the system has no TrueType font at all."""
    fam = (name or '').strip().lower()
    if fam.endswith(' bold'):
        fam, bold = fam[:-5].strip(), True
    key = (fam, bold)
    if key in _FONTS:
        return _FONTS[key]
    idx = index()
    fonts = []
    for f in [fam] + [x for x in FALLBACK if x != fam]:
        faces = idx.get(f)
        if not faces:
            continue
        path = (faces.get('bold') if bold else None) or faces.get('regular') or faces.get('normal') or \
            next(iter(faces.values()))
        try:
            font = _FONTS.get(path) or Font(path)
        except (OSError, ValueError, struct.error):
            continue
        _FONTS[path] = font
        if not fonts:
            font.fake_bold = bold and 'bold' not in [k for k, v in faces.items() if v == path]
        fonts.append(font)
        if len(fonts) >= 4:
            break
    if not fonts:
        for f in sorted(idx):
            try:
                fonts.append(Font(next(iter(idx[f].values()))))
                break
            except (OSError, ValueError, struct.error):
                continue
    chain = Chain(fonts) if fonts else None
    _FONTS[key] = chain
    return chain
