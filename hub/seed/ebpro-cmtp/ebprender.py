"""A window of an EasyBuilder Pro project drawn as a PNG, the way the panel shows it as far as the project tells:
background, shapes, the pictures of the first state, labels in the project's fonts (TrueType fonts of the
system), EasyBuilder's own library shapes as plain buttons, fields and lamps. Pure Python, no packages."""
import struct
import zlib

import ttf

FONT = 1.35     # px of a font per EasyBuilder point (as the canvas of the window app)
LINE = 1.36
FIELD = {'NumericInput', 'NumericDisplay', 'ASCII', 'SetWord?', 'ASCII?'}
LAMP = {'BitLamp', 'WordLamp?', 'MultiState?'}


def _hex(c, default=(0, 0, 0)):
    try:
        c = str(c)
        return int(c[1:3], 16), int(c[3:5], 16), int(c[5:7], 16)
    except (TypeError, ValueError, IndexError):
        return default


class Canvas:
    def __init__(self, w: int, h: int, bg=(0, 0, 0)):
        self.w, self.h = w, h
        self.px = bytearray(bytes(bg) * (w * h))

    def fill(self, l, t, r, b, rgb, alpha: float = 1.0) -> None:
        """Fill pixels l..r, t..b (inclusive)."""
        l, t, r, b = max(0, int(l)), max(0, int(t)), min(self.w - 1, int(r)), min(self.h - 1, int(b))
        if l > r or t > b:
            return
        n = r - l + 1
        if alpha >= 1:
            row = bytes(rgb) * n
            for y in range(t, b + 1):
                i = 3 * (y * self.w + l)
                self.px[i:i + 3 * n] = row
            return
        a, ia = alpha, 1 - alpha
        for y in range(t, b + 1):
            i = 3 * (y * self.w + l)
            seg = self.px[i:i + 3 * n]
            self.px[i:i + 3 * n] = bytes(int(seg[k] * ia + rgb[k % 3] * a) for k in range(3 * n))

    def frame(self, l, t, r, b, rgb, width=1) -> None:
        width = max(1, int(width))
        self.fill(l, t, r, t + width - 1, rgb)
        self.fill(l, b - width + 1, r, b, rgb)
        self.fill(l, t, l + width - 1, b, rgb)
        self.fill(r - width + 1, t, r, b, rgb)

    def gradient(self, l, t, r, b, top, bottom) -> None:
        h = max(1, int(b) - int(t))
        for y in range(int(t), int(b) + 1):
            k = (y - int(t)) / h
            self.fill(l, y, r, y, tuple(int(top[i] + (bottom[i] - top[i]) * k) for i in range(3)))

    def line(self, x0, y0, x1, y1, rgb, width=1) -> None:
        width = max(1, int(width))
        n = int(max(abs(x1 - x0), abs(y1 - y0))) + 1
        h = width // 2
        if x0 == x1 or y0 == y1:
            self.fill(min(x0, x1) - h, min(y0, y1) - h, max(x0, x1) - h + width - 1, max(y0, y1) - h + width - 1, rgb)
            return
        for k in range(n):
            x = x0 + (x1 - x0) * k / max(1, n - 1)
            y = y0 + (y1 - y0) * k / max(1, n - 1)
            self.fill(x - h, y - h, x - h + width - 1, y - h + width - 1, rgb)

    def ellipse(self, l, t, r, b, rgb, edge=None) -> None:
        cx, cy = (l + r) / 2, (t + b) / 2
        rx, ry = max(0.5, (r - l) / 2), max(0.5, (b - t) / 2)
        for y in range(max(0, int(t)), min(self.h - 1, int(b)) + 1):
            dy = (y - cy) / ry
            if abs(dy) > 1:
                continue
            dx = rx * (1 - dy * dy) ** 0.5
            self.fill(cx - dx, y, cx + dx, y, rgb)
            if edge:
                self.fill(cx - dx, y, cx - dx, y, edge)
                self.fill(cx + dx, y, cx + dx, y, edge)
        if edge:
            self.fill(cx - rx * 0.4, t, cx + rx * 0.4, t, edge)
            self.fill(cx - rx * 0.4, b, cx + rx * 0.4, b, edge)

    def image(self, img, l, t, r, b) -> None:
        """Draw an RGBA image (w, h, bytes) stretched over l..r, t..b."""
        iw, ih, data = img
        tw, th = int(r) - int(l) + 1, int(b) - int(t) + 1
        if tw <= 0 or th <= 0 or iw <= 0 or ih <= 0:
            return
        xs = [min(iw - 1, int((x + 0.5) * iw / tw)) for x in range(tw)]
        for y in range(th):
            py = int(t) + y
            if not 0 <= py < self.h:
                continue
            sy = min(ih - 1, int((y + 0.5) * ih / th))
            src = memoryview(data)[4 * iw * sy:4 * iw * (sy + 1)]
            base = 3 * (py * self.w)
            for x in range(tw):
                px = int(l) + x
                if not 0 <= px < self.w:
                    continue
                q = 4 * xs[x]
                a = src[q + 3]
                if a == 0:
                    continue
                i = base + 3 * px
                if a == 255:
                    self.px[i:i + 3] = src[q:q + 3]
                else:
                    for c in range(3):
                        self.px[i + c] = (src[q + c] * a + self.px[i + c] * (255 - a)) // 255

    def mask(self, m, x0, y0, rgb, clip=None) -> None:
        """Paint colour rgb through an alpha mask (w, h, bytes) with its top-left at x0, y0."""
        w, h, a = m
        cl, ct, cr, cb = clip or (0, 0, self.w - 1, self.h - 1)
        cl, ct, cr, cb = max(0, cl), max(0, ct), min(self.w - 1, cr), min(self.h - 1, cb)
        x0, y0 = int(round(x0)), int(round(y0))
        for y in range(h):
            py = y0 + y
            if not ct <= py <= cb:
                continue
            row = a[y * w:(y + 1) * w]
            if not any(row):
                continue
            base = 3 * py * self.w
            for x, v in enumerate(row):
                if not v:
                    continue
                px = x0 + x
                if not cl <= px <= cr:
                    continue
                i = base + 3 * px
                for c in range(3):
                    self.px[i + c] = (rgb[c] * v + self.px[i + c] * (255 - v)) // 255

    def png(self) -> bytes:
        raw = b''.join(b'\x00' + bytes(self.px[3 * y * self.w:3 * (y + 1) * self.w]) for y in range(self.h))
        return png_bytes(self.w, self.h, raw, 2)


def png_bytes(w, h, raw, ctype) -> bytes:
    def chunk(kind, body):
        return struct.pack('>I', len(body)) + kind + body + struct.pack('>I', zlib.crc32(kind + body) & 0xffffffff)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, ctype, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(raw, 6)) + chunk(b'IEND', b''))


# -- pictures --------------------------------------------------------------------------------------------------------
def decode_png(data: bytes):
    """(w, h, RGBA bytes) of a PNG (8-bit and lower, not interlaced); ValueError otherwise."""
    if data[:8] != b'\x89PNG\r\n\x1a\n':
        raise ValueError('not a PNG')
    p, idat, pal, trns = 8, [], None, None
    w = h = depth = ctype = inter = None
    while p + 8 <= len(data):
        n, kind = struct.unpack_from('>I4s', data, p)
        body = data[p + 8:p + 8 + n]
        if kind == b'IHDR':
            w, h, depth, ctype, _, _, inter = struct.unpack('>IIBBBBB', body)
        elif kind == b'PLTE':
            pal = body
        elif kind == b'tRNS':
            trns = body
        elif kind == b'IDAT':
            idat.append(body)
        elif kind == b'IEND':
            break
        p += 12 + n
    if not w or inter or depth == 16 or w * h > 16_000_000:
        raise ValueError('this PNG is not supported')
    ch = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[ctype]
    bpp = max(1, ch * depth // 8)
    stride = (w * ch * depth + 7) // 8
    raw = zlib.decompress(b''.join(idat))
    out = bytearray()
    prev = bytearray(stride)
    for y in range(h):
        f = raw[y * (stride + 1)]
        line = bytearray(raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        if f == 1:
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 255
        elif f == 2:
            line = bytearray((a + b) & 255 for a, b in zip(line, prev))
        elif f == 3:
            for i in range(stride):
                line[i] = (line[i] + ((line[i - bpp] if i >= bpp else 0) + prev[i]) // 2) & 255
        elif f == 4:
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        prev = line
        if depth < 8:
            per = 8 // depth
            vals = [(line[x // per] >> (8 - depth * (x % per + 1))) & ((1 << depth) - 1) for x in range(w)]
            if ctype == 0:
                k = 255 // ((1 << depth) - 1)
                vals = [v * k for v in vals]
        else:
            vals = line
        if ctype == 6:
            out += vals
        elif ctype == 2:
            key = struct.unpack('>HHH', trns) if trns and len(trns) == 6 else None
            for x in range(w):
                rgb = vals[3 * x:3 * x + 3]
                out += bytes(rgb) + (b'\x00' if key and tuple(rgb) == key else b'\xff')
        elif ctype == 3:
            for v in vals:
                out += (pal[3 * v:3 * v + 3] if pal and 3 * v + 3 <= len(pal) else b'\x00\x00\x00') + \
                    bytes([trns[v] if trns and v < len(trns) else 255])
        elif ctype == 0:
            for v in vals:
                out += bytes([v, v, v, 255])
        else:
            for x in range(w):
                v = vals[2 * x]
                out += bytes([v, v, v, vals[2 * x + 1]])
    return w, h, bytes(out)


def decode_bmp(data: bytes, transparent=None):
    """(w, h, RGBA bytes) of an uncompressed Windows bitmap; the transparent colour gets alpha 0."""
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
        pal = [bytes((data[p + 4 * i + 2], data[p + 4 * i + 1], data[p + 4 * i])) for i in range(n)]
    key = bytes(_hex(transparent)) if transparent else None
    stride = (bits * w + 31) // 32 * 4
    out = bytearray()
    for y in range(abs(h)):
        src = off + (abs(h) - 1 - y if h > 0 else y) * stride
        for x in range(w):
            if bits >= 24:
                q = src + x * (bits // 8)
                rgb = bytes((data[q + 2], data[q + 1], data[q]))
            else:
                v = (data[src + x * bits // 8] >> (8 - bits - x * bits % 8)) & ((1 << bits) - 1)
                rgb = pal[v] if v < len(pal) else b'\x00\x00\x00'
            out += rgb + (b'\x00' if rgb == key else b'\xff')
    return w, abs(h), bytes(out)


def picture(p, guid: str, transparent=None):
    """(w, h, RGBA) of a library picture, None if it cannot be decoded here (JPEG, GIF)."""
    lib, _ = p._library()
    try:
        o, n = lib[bytes.fromhex(guid)]
    except (KeyError, ValueError):
        return None
    data = p.payload[o:o + n]
    try:
        if data[:2] == b'BM':
            return decode_bmp(data, transparent)
        if data[:8] == b'\x89PNG\r\n\x1a\n':
            return decode_png(data)
    except (ValueError, IndexError, KeyError, struct.error, zlib.error):
        return None
    return None


# -- labels --------------------------------------------------------------------------------------------------------
def _font(name):
    bold = 'bold' in str(name or '').lower()
    return ttf.find(str(name or '').replace('Bold', '').replace('bold', '').strip() or 'Droid Sans Fallback', bold)


def _draw_text(cv, lab, box, whole, scale, clip):
    text = str(lab.get('text') or '')
    if not text.strip():
        return
    f = _font(lab.get('font'))
    px = max(1.0, float(lab.get('size') or 12) * FONT * scale)
    rgb = _hex(lab.get('color'))
    lines = text.replace('\r\n', '\n').replace('\r', '\n').split('\n')
    lh = px * LINE
    l, t, r, _ = box
    if f is None:       # no fonts at all: grey bars where the text is
        for k, line in enumerate(lines):
            w = len(line) * px * 0.55
            x = l if whole else lab['x'] * scale
            cv.fill(x, t + k * lh + px * 0.3, x + w, t + k * lh + px * 0.9, rgb, 0.5)
        return
    widths = [f.width(line, px) for line in lines]
    block = (r - l + 1) if whole else max(widths or [0])
    x0 = l if whole else lab['x'] * scale
    y0 = t if whole else lab['y'] * scale
    asc, desc = f.ascent * px / f.upm, -f.descent * px / f.upm
    align = lab.get('align') or 'left'
    for k, line in enumerate(lines):
        if not line.strip():
            continue
        w, h, base, a = f.render(line, px)
        shift = {'center': (block - widths[k]) / 2, 'right': block - widths[k]}.get(align, 0)
        top = y0 + k * lh + (lh - (asc + desc)) / 2 + asc - base
        cv.mask((w, h, a), x0 + shift - 1, top, rgb, clip)
        if getattr(f, 'fake_bold', False):
            cv.mask((w, h, a), x0 + shift, top, rgb, clip)


def _light(c) -> bool:
    r, g, b = _hex(c)
    return (0.299 * r + 0.587 * g + 0.114 * b) / 255 > 0.62


def render(p, window, scale: float = 1.0, marks: bool = False, states=None) -> bytes:
    """PNG bytes of the window (scale 0.1-2). marks — the index of every object in a corner, to name them."""
    sc = p.scene(window)
    if states is None:
        states = {}
    if not isinstance(states, dict):
        raise ValueError('states must map object indices to state indices')
    chosen = {}
    for key, state in states.items():
        if not isinstance(key, str) or not key.isascii() or not key.isdigit() or str(int(key)) != key:
            raise ValueError('state object index must be a canonical nonnegative integer string')
        index = int(key)
        if index >= len(sc['objects']) or not isinstance(state, int) or isinstance(state, bool) or state < 0:
            raise ValueError('invalid object or state index')
        pics = sc['objects'][index].get('picture') or []
        if state >= len(pics):
            raise ValueError(f'object #{index} has no picture state {state}')
        chosen[index] = state
    W, H = sc['window']['width'], sc['window']['height']
    scale = max(0.1, min(2.0, float(scale or 1)))
    cw, ch = max(1, int(W * scale)), max(1, int(H * scale))
    cv = Canvas(cw, ch, _hex(sc['window']['background'], (0, 0, 0)))
    pics = {}
    for o in sc['objects']:
        if o['kind'] == 'Window' or not o.get('bbox'):
            continue
        l, t, r, b = (v * scale for v in o['bbox'])
        if r < 0 or b < 0 or l >= cw or t >= ch:
            continue
        kind = o['kind']
        if kind == 'Line':
            cv.line(int(l), int(t), int(r), int(b), _hex(o.get('frame') or o.get('fill')),
                    max(1, int((o.get('frame_width') or 1) * scale)))
        elif kind in ('Rect', 'Text') or kind.startswith('draw_'):
            if o.get('fill'):
                cv.fill(l, t, r, b, _hex(o['fill']))
            if kind != 'Text' and o.get('frame') and (o.get('frame_width') or 0) > 0:
                cv.frame(l, t, r, b, _hex(o['frame']), max(1, int(o['frame_width'] * scale)))
        state = chosen.get(o['index'], 0)
        pic = (o.get('picture') or [None])[state]
        labels = o.get('labels') or []
        if o['index'] in chosen and len(labels) > 1:
            lab = labels[state] if state < len(labels) and str(labels[state].get('text') or '') else None
        else:
            lab = next((x for x in labels if str(x.get('text') or '') != ''), None)
        if pic:
            key = pic['image'] + (pic['transparent'] or '')
            if key not in pics:
                pics[key] = picture(p, pic['image'], pic['transparent'])
            if pics[key]:
                cv.image(pics[key], l, t, r, b)
            else:   # a picture this renderer does not decode (JPEG...): a light box
                cv.fill(l, t, r, b, (128, 128, 128), 0.35)
                cv.frame(l, t, r, b, (200, 200, 200), 1)
        elif o.get('shape'):
            if kind in FIELD:
                cv.fill(l, t, r, b, (255, 255, 255))
                cv.frame(l, t, r, b, (112, 112, 112), 1)
            elif kind in LAMP:
                cv.ellipse(l, t, r, b, (190, 190, 190), (85, 85, 85))
            elif lab and _light(lab.get('color')):
                cv.gradient(l, t, r, b, (93, 110, 128), (46, 58, 71))
                cv.frame(l, t, r, b, (28, 36, 44), 1)
            else:
                cv.gradient(l, t, r, b, (251, 251, 251), (210, 210, 210))
                cv.frame(l, t, r, b, (111, 111, 111), 1)
        if lab:
            whole = kind == 'Text'
            _draw_text(cv, lab, (l, t, r, b), whole, scale, (0, 0, cw - 1, ch - 1))
    if marks:
        f = ttf.find('Arial') or ttf.find('')
        for o in sc['objects']:
            if o['kind'] == 'Window' or not o.get('bbox'):
                continue
            l, t = o['bbox'][0] * scale, o['bbox'][1] * scale
            s = str(o['index'])
            if f:
                w, h, base, a = f.render(s, 11)
                cv.fill(l, t, l + w, t + 12, (200, 30, 30))
                cv.mask((w, h, a), l, t + 9 - base, (255, 255, 255))
    return cv.png()
