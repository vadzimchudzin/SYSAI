"""EasyBuilder Pro driven the way a person drives it: its windows, a picture of a window, the mouse and the keyboard.

This part does not depend on Windows: which windows are EasyBuilder's, who of the agents drives it now, the keys, the
PNG of a window; the Windows calls are in win32.py (ctypes, nothing to install). Only EasyBuilder's own windows are
touched: their processes are EasyBuilderPro.exe or programs from its folder (the simulator and the like); windows of
downloading to a panel, of passwords and firmware are left to the person."""
import json
import ntpath
import os
import struct
import tempfile
import time
import zlib

EB_NAMES = ('easybuilderpro.exe',)
DENY = ('download', 'upload', 'загрузк', 'выгрузк', 'password', 'пароль', 'firmware', 'прошивк')
HOLD = 90               # seconds an agent keeps EasyBuilder after its last action
MAX_SIDE = 8000
SKIP = {'IME', 'MSCTFIME UI', 'tooltips_class32', 'SysShadow', 'ForegroundStaging'}


class GuiError(Exception):
    pass


# -- a picture: BGRA rows (top-down) -> PNG ----------------------------------------------------------------------------
def png(w: int, h: int, bgra: bytes, step: int = 1) -> tuple:
    """(PNG bytes, width, height) of a top-down BGRA picture, every `step`-th pixel (step 2 — half the size)."""
    if w <= 0 or h <= 0 or len(bgra) < w * h * 4:
        raise GuiError('пустая картинка окна')
    step = max(1, int(step))
    px = memoryview(bgra).cast('B')[:w * h * 4].cast('I')
    rows = []
    for y in range(0, h, step):
        row = px[y * w:(y + 1) * w][::step].tobytes()
        rgb = bytearray(len(row) // 4 * 3)
        rgb[0::3], rgb[1::3], rgb[2::3] = row[2::4], row[1::4], row[0::4]
        rows.append(b'\x00' + bytes(rgb))
    nw, nh = (w + step - 1) // step, (h + step - 1) // step

    def chunk(kind, body):
        return struct.pack('>I', len(body)) + kind + body + struct.pack('>I', zlib.crc32(kind + body) & 0xffffffff)
    data = (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', nw, nh, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(b''.join(rows), 6)) + chunk(b'IEND', b''))
    return data, nw, nh


# -- keys --------------------------------------------------------------------------------------------------------------
VK = {'ctrl': 0x11, 'control': 0x11, 'shift': 0x10, 'alt': 0x12, 'win': 0x5B, 'enter': 0x0D, 'return': 0x0D,
      'tab': 0x09, 'esc': 0x1B, 'escape': 0x1B, 'space': 0x20, 'backspace': 0x08, 'delete': 0x2E, 'del': 0x2E,
      'insert': 0x2D, 'ins': 0x2D, 'home': 0x24, 'end': 0x23, 'pageup': 0x21, 'pgup': 0x21, 'pagedown': 0x22,
      'pgdn': 0x22, 'left': 0x25, 'up': 0x26, 'right': 0x27, 'down': 0x28, 'menu': 0x5D, 'apps': 0x5D,
      'plus': 0xBB, 'minus': 0xBD, 'comma': 0xBC, 'period': 0xBE}
VK.update({f'f{i}': 0x6F + i for i in range(1, 25)})
VK.update({chr(c).lower(): c for c in range(0x41, 0x5B)})
VK.update({str(d): 0x30 + d for d in range(10)})
EXTENDED = {0x2E, 0x2D, 0x24, 0x23, 0x21, 0x22, 0x25, 0x26, 0x27, 0x28, 0x5B, 0x5D}
RU = dict(zip('йцукенгшщзхъфывапролджэячсмитьбю', 'qwertyuiop[]asdfghjkl;\'zxcvbnm,.'))


def parse_keys(text: str) -> list:
    """«ctrl+s», «alt, f, s», «enter», «ctrl+shift+f7» -> combos [[(vk, extended), ...], ...] pressed one after another."""
    combos = []
    for part in str(text or '').split(','):
        part = part.strip().lower()
        if not part:
            continue
        keys = []
        for name in (k.strip() for k in part.split('+')):
            name = RU.get(name, name) if len(name) == 1 else name
            if name in VK:
                vk = VK[name]
            elif len(name) == 1 and name in '[];\',./`-=\\':
                vk = {'[': 0xDB, ']': 0xDD, ';': 0xBA, "'": 0xDE, ',': 0xBC, '.': 0xBE, '/': 0xBF, '`': 0xC0,
                      '-': 0xBD, '=': 0xBB, '\\': 0xDC}[name]
            else:
                raise GuiError(f'клавиша «{name}» не знакома: ctrl, shift, alt, enter, tab, esc, f1…f24, буквы, цифры, '
                               f'стрелки (left, up…), delete, home, end, pageup')
            keys.append((vk, vk in EXTENDED))
        if not keys:
            raise GuiError(f'пустая комбинация в «{text}»')
        combos.append(keys)
    if not combos:
        raise GuiError('какие клавиши? например «ctrl+s» или «alt, f»')
    if len(combos) > 30:
        raise GuiError('за раз — не больше 30 комбинаций')
    return combos


# -- who drives EasyBuilder now ------------------------------------------------------------------------------------------
def sysai_home() -> str:
    """The SYSAI folder of this computer (as the synchronizer and the window «Агент SYSAI» keep it)."""
    if os.environ.get('SYSAI_HOME'):
        return os.environ['SYSAI_HOME']
    base = os.environ.get('LOCALAPPDATA')
    return os.path.join(base, 'SYSAI') if base else ''


def lock_path() -> str:
    return os.path.join(sysai_home() or tempfile.gettempdir(), 'ebpro-gui.lock')


class Lock:
    """One mouse, one keyboard: one agent (or helper) at a time drives EasyBuilder; the others wait. The holder keeps
    it for HOLD seconds after its last action or until eb_free."""

    def __init__(self, path: str = None, hold: float = HOLD):
        self.path, self.hold = path or lock_path(), hold

    def holder(self):
        try:
            with open(self.path, encoding='utf-8') as f:
                d = json.load(f)
            left = float(d.get('until', 0)) - time.time()
            return (str(d.get('who') or '?'), left) if left > 0 else None
        except (OSError, ValueError, AttributeError):
            return None

    def take(self, who: str) -> None:
        who = str(who or '').strip()
        if not who:
            raise GuiError('кто ведёт EasyBuilder? who — «Агент» или имя помощника')
        h = self.holder()
        if h and h[0] != who:
            raise GuiError(f'EasyBuilder сейчас ведёт «{h[0]}» (ещё {int(h[1]) + 1} с или до eb_free) — подождите')
        os.makedirs(os.path.dirname(self.path) or '.', exist_ok=True)
        tmp = f'{self.path}.{os.getpid()}.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump({'who': who, 'until': time.time() + self.hold, 'pid': os.getpid()}, f, ensure_ascii=False)
        os.replace(tmp, self.path)

    def free(self, who: str) -> bool:
        h = self.holder()
        if h and h[0] != str(who or '').strip():
            raise GuiError(f'EasyBuilder ведёт «{h[0]}», не вы')
        try:
            os.remove(self.path)
        except OSError:
            pass
        return bool(h)


# -- where EasyBuilder is ----------------------------------------------------------------------------------------------
def exe_candidates() -> list:
    out = []
    if os.environ.get('EBPRO_EXE'):
        out.append(os.environ['EBPRO_EXE'])
    home = sysai_home()
    if home:      # the one the person chose in the window «Агент SYSAI»
        try:
            with open(os.path.join(home, 'desktop', 'easybuilder.json'), encoding='utf-8') as f:
                exe = json.load(f).get('exe')
            if exe:
                out.append(exe)
        except (OSError, ValueError, AttributeError):
            pass
    out.append(r'C:\EBpro\EasyBuilderPro.exe')
    for env in ('ProgramFiles(x86)', 'ProgramFiles'):
        if os.environ.get(env):
            out += [os.path.join(os.environ[env], 'EBpro', 'EasyBuilderPro.exe'),
                    os.path.join(os.environ[env], 'Weintek', 'EasyBuilder Pro', 'EasyBuilderPro.exe')]
    return out


def find_exe(given: str = None) -> str:
    if given:
        if not os.path.isfile(given):
            raise GuiError(f'нет программы {given}')
        return given
    for exe in exe_candidates():
        if exe and os.path.isfile(exe):
            return exe
    raise GuiError('EasyBuilder Pro не найден: укажите exe (путь к EasyBuilderPro.exe) или переменную EBPRO_EXE')


# -- the driver ----------------------------------------------------------------------------------------------------------
def _public(w: dict) -> dict:
    l, t, r, b = w['rect']
    return {'window': w['hwnd'], 'title': w['title'], 'class': w['class'], 'x': l, 'y': t, 'width': r - l,
            'height': b - t, 'minimized': w.get('minimized', False), 'foreground': w.get('foreground', False),
            'program': ntpath.basename(w.get('exe') or '')}


class Driver:
    def __init__(self, backend, lock: Lock = None):
        self.b, self.lock = backend, lock or Lock()

    # windows
    def windows(self) -> list:
        ws = [w for w in self.b.windows() if w.get('visible')]
        fg = self.b.foreground()
        dirs = {ntpath.dirname(w['exe']).lower() for w in ws
                if w.get('exe') and ntpath.basename(w['exe']).lower() in EB_NAMES}
        out = []
        for w in ws:
            exe = (w.get('exe') or '').lower()
            l, t, r, b = w['rect']
            if r - l < 2 or b - t < 2 or w.get('class') in SKIP:      # helpers: IME, tooltips, empty frames
                continue
            if ntpath.basename(exe) in EB_NAMES or (dirs and ntpath.dirname(exe) in dirs):
                out.append(dict(w, foreground=w['hwnd'] == fg))
        return out

    def window(self, ref=None) -> dict:
        ws = self.windows()
        if not ws:
            raise GuiError('EasyBuilder Pro не запущен (или свёрнут в трей): откройте проект — eb_edit')
        if ref in (None, ''):
            fg = [w for w in ws if w['foreground']]
            if fg:
                return fg[0]
            main = [w for w in ws if 'easybuilder' in w['title'].lower()] or ws
            return max(main, key=lambda w: (w['rect'][2] - w['rect'][0]) * (w['rect'][3] - w['rect'][1]))
        s = str(ref).strip()
        if s.isdigit():
            for w in ws:
                if w['hwnd'] == int(s):
                    return w
            raise GuiError(f'окна {s} у EasyBuilder нет (закрылось?) — посмотрите eb_windows')
        hit = [w for w in ws if s.lower() in w['title'].lower()]
        if not hit:
            raise GuiError(f'у EasyBuilder нет окна «{s}»; есть: ' + '; '.join(f'«{w["title"]}»' for w in ws[:12]))
        return hit[0]

    def _input(self, who, ref):
        w = self.window(ref)
        if any(d in w['title'].lower() for d in DENY):
            raise GuiError(f'окно «{w["title"]}» — загрузка в панель, пароль или прошивка: это делает человек')
        self.lock.take(who)
        return w

    def _point(self, w, x, y):
        l, t, r, b = w['rect']
        x, y = int(x), int(y)
        if not (0 <= x < r - l and 0 <= y < b - t):
            raise GuiError(f'точка ({x}, {y}) вне окна «{w["title"]}» {r - l}×{b - t} (координаты — от левого '
                           f'верхнего угла окна, как на картинке eb_shot)')
        return l + x, t + y

    def _front(self, w):
        if not self.b.bring(w['hwnd']):
            raise GuiError('EasyBuilder не вышел на передний план (экран заблокирован или занят другим окном) — '
                           'попросите человека разблокировать ПК или используйте mode «post»')

    # what the tools do
    def shot(self, ref=None, scale=1.0):
        w = self.window(ref)
        if w.get('minimized'):
            self.b.restore(w['hwnd'])
            w = self.window(w['hwnd'])
        width, height, bgra = self.b.capture(w['hwnd'])
        if not (0 < width <= MAX_SIDE and 0 < height <= MAX_SIDE):
            raise GuiError('окно не снять (свёрнуто или за краем экрана)')
        step = max(1, round(1 / max(0.1, min(1.0, float(scale or 1)))))
        data, nw, nh = png(width, height, bgra, step)
        return w, data, nw, nh, step

    def controls(self, ref=None, flt=''):
        w = self.window(ref)
        l, t = w['rect'][:2]
        out = []
        for c in self.b.children(w['hwnd']):
            if not c.get('visible'):
                continue
            text = str(c.get('text') or '')
            if flt and flt.lower() not in f"{text} {c.get('class', '')}".lower():
                continue
            cl, ct, cr, cb = c['rect']
            out.append({'control': c['hwnd'], 'class': c.get('class', ''), 'text': text[:200], 'id': c.get('id'),
                        'x': cl - l, 'y': ct - t, 'width': cr - cl, 'height': cb - ct, 'enabled': c.get('enabled', True)})
            if len(out) >= 400:
                break
        return w, out

    def click(self, who, ref, x, y, button='left', double=False, mode='real'):
        w = self._input(who, ref)
        sx, sy = self._point(w, x, y)
        if button not in ('left', 'right', 'middle'):
            raise GuiError('button: left, right или middle')
        if mode == 'post':
            self.b.post_click(w['hwnd'], sx, sy, button, bool(double))
        else:
            self._front(w)
            self.b.click(sx, sy, button, bool(double))
        return w

    def drag(self, who, ref, x, y, to_x, to_y):
        w = self._input(who, ref)
        a, b = self._point(w, x, y), self._point(w, to_x, to_y)
        self._front(w)
        self.b.drag(a[0], a[1], b[0], b[1])
        return w

    def keys(self, who, ref, keys, mode='real'):
        combos = parse_keys(keys)
        w = self._input(who, ref)
        if mode == 'post':
            if any(len(c) > 1 or c[0][0] in (0x10, 0x11, 0x12, 0x5B) for c in combos):
                raise GuiError('в режиме post — только одиночные клавиши; комбинации (ctrl+…) — в режиме real')
            self.b.post_keys(w['hwnd'], combos)
        else:
            self._front(w)
            self.b.keys(combos)
        return w

    def write(self, who, ref, text, control=None, mode='real'):
        text = str(text)
        if len(text) > 4000:
            raise GuiError('за раз — до 4000 знаков')
        w = self._input(who, ref)
        if control not in (None, ''):
            ids = {c['hwnd'] for c in self.b.children(w['hwnd'])}
            if not str(control).strip().isdigit() or int(control) not in ids:
                raise GuiError(f'у окна «{w["title"]}» нет поля {control} — посмотрите eb_controls')
            self.b.set_text(int(control), text)
        elif mode == 'post':
            self.b.post_text(w['hwnd'], text)
        else:
            self._front(w)
            self.b.text(text)
        return w

    def close(self, who, ref):
        w = self._input(who, ref)
        self.b.close(w['hwnd'])
        return w

    def wait(self, title, gone=False, timeout=30.0):
        limit = max(0.0, min(300.0, float(timeout or 30)))
        end = time.monotonic() + limit
        s = str(title or '').strip().lower()
        if not s:
            raise GuiError('какое окно ждать? title — часть заголовка')
        while True:
            hit = [w for w in self.windows() if s in w['title'].lower()]
            if (not hit) if gone else hit:
                return hit[0] if hit else None
            if time.monotonic() > end:
                raise GuiError(f'окно «{title}» {"не закрылось" if gone else "не появилось"} за {int(limit)} с')
            time.sleep(0.25)

    def edit(self, path, exe=None, wait=60.0):
        path = os.path.abspath(str(path))
        if not os.path.isfile(path):
            raise GuiError(f'нет файла {path}')
        if not path.lower().endswith(('.cmtp', '.emtp', '.ecmp', '.mtp')):
            raise GuiError('это не проект EasyBuilder (.cmtp / .emtp)')
        before = {w['hwnd'] for w in self.windows()}
        self.b.launch(find_exe(exe), [path])
        stem = os.path.splitext(os.path.basename(path))[0].lower()
        end = time.monotonic() + max(1.0, min(300.0, float(wait or 60)))
        while time.monotonic() < end:
            ws = self.windows()
            mine = [w for w in ws if stem in w['title'].lower()] or [w for w in ws if w['hwnd'] not in before]
            if mine:
                return mine[0], ws
            time.sleep(0.5)
        raise GuiError('EasyBuilder запущен, но окно с проектом не появилось — посмотрите eb_windows через минуту')
