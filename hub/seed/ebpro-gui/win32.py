"""The Windows side of gui.py: windows, pictures of windows, the mouse and the keyboard — ctypes only, nothing to install.

Coordinates are physical screen pixels (the process is made DPI-aware), the same ones GetWindowRect gives, so a point on
a picture of a window plus the window's corner is the point on the screen."""
import ctypes
import os
import subprocess
import time
from ctypes import wintypes as wt

from gui import GuiError

user32 = ctypes.WinDLL('user32', use_last_error=True)
gdi32 = ctypes.WinDLL('gdi32', use_last_error=True)
kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
ULONG_PTR = ctypes.c_size_t
LRESULT = ctypes.c_ssize_t
WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [('dx', wt.LONG), ('dy', wt.LONG), ('mouseData', wt.DWORD), ('dwFlags', wt.DWORD),
                ('time', wt.DWORD), ('dwExtraInfo', ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [('wVk', wt.WORD), ('wScan', wt.WORD), ('dwFlags', wt.DWORD), ('time', wt.DWORD),
                ('dwExtraInfo', ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [('uMsg', wt.DWORD), ('wParamL', wt.WORD), ('wParamH', wt.WORD)]


class _INPUTU(ctypes.Union):
    _fields_ = [('mi', MOUSEINPUT), ('ki', KEYBDINPUT), ('hi', HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [('type', wt.DWORD), ('u', _INPUTU)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [('biSize', wt.DWORD), ('biWidth', wt.LONG), ('biHeight', wt.LONG), ('biPlanes', wt.WORD),
                ('biBitCount', wt.WORD), ('biCompression', wt.DWORD), ('biSizeImage', wt.DWORD),
                ('biXPelsPerMeter', wt.LONG), ('biYPelsPerMeter', wt.LONG), ('biClrUsed', wt.DWORD),
                ('biClrImportant', wt.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [('bmiHeader', BITMAPINFOHEADER), ('bmiColors', wt.DWORD * 3)]


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [('cbSize', wt.DWORD), ('flags', wt.DWORD), ('hwndActive', wt.HWND), ('hwndFocus', wt.HWND),
                ('hwndCapture', wt.HWND), ('hwndMenuOwner', wt.HWND), ('hwndMoveSize', wt.HWND),
                ('hwndCaret', wt.HWND), ('rcCaret', wt.RECT)]


def _sig(dll, name, res, *args):
    f = getattr(dll, name)
    f.restype, f.argtypes = res, list(args)
    return f


H, B, I, U, D = wt.HWND, wt.BOOL, ctypes.c_int, wt.UINT, wt.DWORD
EnumWindows = _sig(user32, 'EnumWindows', B, WNDENUMPROC, wt.LPARAM)
EnumChildWindows = _sig(user32, 'EnumChildWindows', B, H, WNDENUMPROC, wt.LPARAM)
GetWindowTextW = _sig(user32, 'GetWindowTextW', I, H, wt.LPWSTR, I)
GetWindowTextLengthW = _sig(user32, 'GetWindowTextLengthW', I, H)
GetClassNameW = _sig(user32, 'GetClassNameW', I, H, wt.LPWSTR, I)
IsWindowVisible = _sig(user32, 'IsWindowVisible', B, H)
IsWindowEnabled = _sig(user32, 'IsWindowEnabled', B, H)
IsIconic = _sig(user32, 'IsIconic', B, H)
IsWindow = _sig(user32, 'IsWindow', B, H)
GetWindowRect = _sig(user32, 'GetWindowRect', B, H, ctypes.POINTER(wt.RECT))
GetWindowThreadProcessId = _sig(user32, 'GetWindowThreadProcessId', D, H, ctypes.POINTER(D))
GetForegroundWindow = _sig(user32, 'GetForegroundWindow', H)
GetDlgCtrlID = _sig(user32, 'GetDlgCtrlID', I, H)
ShowWindow = _sig(user32, 'ShowWindow', B, H, I)
BringWindowToTop = _sig(user32, 'BringWindowToTop', B, H)
SetForegroundWindow = _sig(user32, 'SetForegroundWindow', B, H)
AttachThreadInput = _sig(user32, 'AttachThreadInput', B, D, D, B)
SetCursorPos = _sig(user32, 'SetCursorPos', B, I, I)
SendInput = _sig(user32, 'SendInput', U, U, ctypes.POINTER(INPUT), I)
MapVirtualKeyW = _sig(user32, 'MapVirtualKeyW', U, U, U)
PostMessageW = _sig(user32, 'PostMessageW', B, H, U, wt.WPARAM, wt.LPARAM)
SendMessageTimeoutW = _sig(user32, 'SendMessageTimeoutW', LRESULT, H, U, wt.WPARAM, ctypes.c_void_p, U, U,
                           ctypes.POINTER(ULONG_PTR))
ScreenToClient = _sig(user32, 'ScreenToClient', B, H, ctypes.POINTER(wt.POINT))
ChildWindowFromPointEx = _sig(user32, 'ChildWindowFromPointEx', H, H, wt.POINT, U)
GetGUIThreadInfo = _sig(user32, 'GetGUIThreadInfo', B, D, ctypes.POINTER(GUITHREADINFO))
GetDC = _sig(user32, 'GetDC', wt.HDC, H)
ReleaseDC = _sig(user32, 'ReleaseDC', I, H, wt.HDC)
PrintWindow = _sig(user32, 'PrintWindow', B, H, wt.HDC, U)
CreateCompatibleDC = _sig(gdi32, 'CreateCompatibleDC', wt.HDC, wt.HDC)
CreateDIBSection = _sig(gdi32, 'CreateDIBSection', wt.HBITMAP, wt.HDC, ctypes.POINTER(BITMAPINFO), U,
                        ctypes.POINTER(ctypes.c_void_p), wt.HANDLE, D)
SelectObject = _sig(gdi32, 'SelectObject', wt.HGDIOBJ, wt.HDC, wt.HGDIOBJ)
DeleteObject = _sig(gdi32, 'DeleteObject', B, wt.HGDIOBJ)
DeleteDC = _sig(gdi32, 'DeleteDC', B, wt.HDC)
BitBlt = _sig(gdi32, 'BitBlt', B, wt.HDC, I, I, I, I, wt.HDC, I, I, D)
GdiFlush = _sig(gdi32, 'GdiFlush', B)
OpenProcess = _sig(kernel32, 'OpenProcess', wt.HANDLE, D, B, D)
CloseHandle = _sig(kernel32, 'CloseHandle', B, wt.HANDLE)
QueryFullProcessImageNameW = _sig(kernel32, 'QueryFullProcessImageNameW', B, wt.HANDLE, D, wt.LPWSTR,
                                  ctypes.POINTER(D))
GetCurrentThreadId = _sig(kernel32, 'GetCurrentThreadId', D)

WM_SETTEXT, WM_GETTEXT, WM_GETTEXTLENGTH, WM_CLOSE = 0x000C, 0x000D, 0x000E, 0x0010
WM_KEYDOWN, WM_KEYUP, WM_CHAR, WM_MOUSEMOVE = 0x0100, 0x0101, 0x0102, 0x0200
BUTTONS = {'left': (0x0002, 0x0004, 0x0201, 0x0202, 0x0203, 0x0001),
           'right': (0x0008, 0x0010, 0x0204, 0x0205, 0x0206, 0x0002),
           'middle': (0x0020, 0x0040, 0x0207, 0x0208, 0x0209, 0x0010)}
KEYUP, UNICODE, EXTENDED = 0x0002, 0x0004, 0x0001
SMTO_ABORTIFHUNG = 0x0002


def dpi_aware():
    try:
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):     # per monitor v2
            return
    except (AttributeError, OSError):
        pass
    try:
        ctypes.WinDLL('shcore').SetProcessDpiAwareness(2)
        return
    except (AttributeError, OSError):
        pass
    try:
        user32.SetProcessDPIAware()
    except (AttributeError, OSError):
        pass


def _text(hwnd) -> str:
    n = GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ''
    buf = ctypes.create_unicode_buffer(n + 2)
    GetWindowTextW(hwnd, buf, n + 2)
    return buf.value


def _class(hwnd) -> str:
    buf = ctypes.create_unicode_buffer(256)
    GetClassNameW(hwnd, buf, 256)
    return buf.value


def _rect(hwnd) -> tuple:
    r = wt.RECT()
    if not GetWindowRect(hwnd, ctypes.byref(r)):
        raise GuiError('окно закрылось')
    return r.left, r.top, r.right, r.bottom


def _pid(hwnd) -> int:
    pid = D()
    GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def _exe(pid: int, cache: dict) -> str:
    if pid in cache:
        return cache[pid]
    path = ''
    h = OpenProcess(0x1000, False, pid)             # PROCESS_QUERY_LIMITED_INFORMATION
    if h:
        try:
            buf, n = ctypes.create_unicode_buffer(1024), D(1024)
            if QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
                path = buf.value
        finally:
            CloseHandle(h)
    cache[pid] = path
    return path


def _ask(hwnd, msg, wparam=0, lparam=None, timeout=1000):
    res = ULONG_PTR()
    if not SendMessageTimeoutW(hwnd, msg, wparam, lparam, SMTO_ABORTIFHUNG, timeout, ctypes.byref(res)):
        return None
    return res.value


class Win32:
    def __init__(self):
        dpi_aware()
        self._exes = {}

    # windows
    def windows(self) -> list:
        out = []

        def each(hwnd, _):
            try:
                pid = _pid(hwnd)
                out.append({'hwnd': int(hwnd), 'title': _text(hwnd), 'class': _class(hwnd), 'pid': pid,
                            'exe': _exe(pid, self._exes), 'rect': _rect(hwnd), 'visible': bool(IsWindowVisible(hwnd)),
                            'minimized': bool(IsIconic(hwnd))})
            except GuiError:
                pass
            return True
        EnumWindows(WNDENUMPROC(each), 0)
        return out

    def foreground(self) -> int:
        return int(GetForegroundWindow() or 0)

    def restore(self, hwnd):
        ShowWindow(hwnd, 9)                          # SW_RESTORE
        time.sleep(0.3)

    def bring(self, hwnd) -> bool:
        if IsIconic(hwnd):
            self.restore(hwnd)
        pid = _pid(hwnd)
        for attempt in range(3):
            fg = GetForegroundWindow()
            if fg and (int(fg) == hwnd or (attempt and _pid(fg) == pid)):
                return True
            me = GetCurrentThreadId()
            tid = GetWindowThreadProcessId(fg, None) if fg else 0
            joined = bool(tid and tid != me and AttachThreadInput(me, tid, True))
            try:
                BringWindowToTop(hwnd)
                SetForegroundWindow(hwnd)
            finally:
                if joined:
                    AttachThreadInput(me, tid, False)
            time.sleep(0.2)
        fg = GetForegroundWindow()
        return bool(fg and (int(fg) == hwnd or _pid(fg) == pid))

    def capture(self, hwnd) -> tuple:
        l, t, r, b = _rect(hwnd)
        w, h = r - l, b - t
        if w <= 0 or h <= 0:
            return 0, 0, b''
        screen = GetDC(None)
        mem = CreateCompatibleDC(screen)
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize, bmi.bmiHeader.biWidth, bmi.bmiHeader.biHeight = ctypes.sizeof(BITMAPINFOHEADER), w, -h
        bmi.bmiHeader.biPlanes, bmi.bmiHeader.biBitCount = 1, 32
        bits = ctypes.c_void_p()
        bmp = CreateDIBSection(screen, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
        if not bmp:
            DeleteDC(mem)
            ReleaseDC(None, screen)
            raise GuiError('не хватило памяти на картинку окна')
        old = SelectObject(mem, bmp)
        try:
            ok = PrintWindow(hwnd, mem, 2)           # PW_RENDERFULLCONTENT: ribbons and WPF too
            GdiFlush()
            data = ctypes.string_at(bits, w * h * 4)
            if not ok or data.count(0) == len(data):     # some windows draw nothing into PrintWindow: take the screen
                BitBlt(mem, 0, 0, w, h, screen, l, t, 0x00CC0020 | 0x40000000)    # SRCCOPY | CAPTUREBLT
                GdiFlush()
                data = ctypes.string_at(bits, w * h * 4)
        finally:
            SelectObject(mem, old)
            DeleteObject(bmp)
            DeleteDC(mem)
            ReleaseDC(None, screen)
        return w, h, data

    def children(self, hwnd) -> list:
        out = []

        def each(c, _):
            try:
                n = _ask(c, WM_GETTEXTLENGTH, 0, None, 300) or 0
                text = ''
                if 0 < n < 20000:
                    buf = ctypes.create_unicode_buffer(n + 2)
                    _ask(c, WM_GETTEXT, n + 1, ctypes.cast(buf, ctypes.c_void_p), 300)
                    text = buf.value
                out.append({'hwnd': int(c), 'class': _class(c), 'text': text, 'id': GetDlgCtrlID(c),
                            'rect': _rect(c), 'visible': bool(IsWindowVisible(c)), 'enabled': bool(IsWindowEnabled(c))})
            except GuiError:
                pass
            return len(out) < 2000
        EnumChildWindows(hwnd, WNDENUMPROC(each), 0)
        return out

    # the mouse
    def _send(self, events):
        arr = (INPUT * len(events))(*events)
        if SendInput(len(events), arr, ctypes.sizeof(INPUT)) != len(events):
            raise GuiError('Windows не принял нажатия (экран заблокирован или EasyBuilder запущен от администратора, '
                           'а агент — нет)')

    @staticmethod
    def _mouse(flags):
        return INPUT(type=0, u=_INPUTU(mi=MOUSEINPUT(0, 0, 0, flags, 0, 0)))

    def click(self, x, y, button='left', double=False):
        down, up = BUTTONS[button][:2]
        SetCursorPos(x, y)
        time.sleep(0.05)
        self._send([self._mouse(down), self._mouse(up)])
        if double:
            time.sleep(0.05)
            self._send([self._mouse(down), self._mouse(up)])
        time.sleep(0.1)

    def drag(self, x1, y1, x2, y2):
        SetCursorPos(x1, y1)
        time.sleep(0.05)
        self._send([self._mouse(0x0002)])
        try:
            time.sleep(0.15)
            steps = max(4, min(40, int(max(abs(x2 - x1), abs(y2 - y1)) / 15)))
            for i in range(1, steps + 1):
                SetCursorPos(x1 + (x2 - x1) * i // steps, y1 + (y2 - y1) * i // steps)
                time.sleep(0.02)
            time.sleep(0.1)
        finally:
            self._send([self._mouse(0x0004)])
        time.sleep(0.1)

    @staticmethod
    def _deepest(hwnd, sx, sy):
        cur = hwnd
        for _ in range(30):
            p = wt.POINT(sx, sy)
            ScreenToClient(cur, ctypes.byref(p))
            c = ChildWindowFromPointEx(cur, p, 0x0001 | 0x0004)      # skip invisible and transparent
            if not c or int(c) == int(cur):
                break
            cur = c
        p = wt.POINT(sx, sy)
        ScreenToClient(cur, ctypes.byref(p))
        return cur, ((p.y & 0xFFFF) << 16) | (p.x & 0xFFFF)

    def post_click(self, hwnd, x, y, button='left', double=False):
        target, lp = self._deepest(hwnd, x, y)
        _, _, down, up, dbl, mk = BUTTONS[button]
        PostMessageW(target, WM_MOUSEMOVE, 0, lp)
        PostMessageW(target, down, mk, lp)
        PostMessageW(target, up, 0, lp)
        if double:
            PostMessageW(target, dbl, mk, lp)
            PostMessageW(target, up, 0, lp)
        time.sleep(0.1)

    # the keyboard
    @staticmethod
    def _key(vk, ext, up):
        flags = (EXTENDED if ext else 0) | (KEYUP if up else 0)
        return INPUT(type=1, u=_INPUTU(ki=KEYBDINPUT(vk, MapVirtualKeyW(vk, 0), flags, 0, 0)))

    def keys(self, combos):
        for combo in combos:
            self._send([self._key(vk, ext, False) for vk, ext in combo]
                       + [self._key(vk, ext, True) for vk, ext in reversed(combo)])
            time.sleep(0.08)

    def text(self, s):
        events = []
        for ch in str(s).replace('\r\n', '\n'):
            if ch in '\n\t':
                vk = 0x0D if ch == '\n' else 0x09
                events += [self._key(vk, False, False), self._key(vk, False, True)]
                continue
            raw = ch.encode('utf-16-le')
            for i in range(0, len(raw), 2):
                unit = int.from_bytes(raw[i:i + 2], 'little')
                events += [INPUT(type=1, u=_INPUTU(ki=KEYBDINPUT(0, unit, UNICODE, 0, 0))),
                           INPUT(type=1, u=_INPUTU(ki=KEYBDINPUT(0, unit, UNICODE | KEYUP, 0, 0)))]
        for i in range(0, len(events), 60):
            self._send(events[i:i + 60])
            time.sleep(0.02)

    @staticmethod
    def _focus(hwnd):
        info = GUITHREADINFO(cbSize=ctypes.sizeof(GUITHREADINFO))
        tid = GetWindowThreadProcessId(hwnd, None)
        if tid and GetGUIThreadInfo(tid, ctypes.byref(info)) and info.hwndFocus:
            return info.hwndFocus
        return hwnd

    def post_keys(self, hwnd, combos):
        target = self._focus(hwnd)
        for combo in combos:
            vk, ext = combo[0]
            lp = 1 | (MapVirtualKeyW(vk, 0) << 16) | ((1 << 24) if ext else 0)
            PostMessageW(target, WM_KEYDOWN, vk, lp)
            PostMessageW(target, WM_KEYUP, vk, lp | (3 << 30))
            time.sleep(0.03)

    def post_text(self, hwnd, s):
        target = self._focus(hwnd)
        raw = str(s).replace('\r\n', '\n').replace('\n', '\r').encode('utf-16-le')
        for i in range(0, len(raw), 2):
            PostMessageW(target, WM_CHAR, int.from_bytes(raw[i:i + 2], 'little'), 1)

    def set_text(self, hwnd, s):
        buf = ctypes.create_unicode_buffer(str(s))
        if _ask(hwnd, WM_SETTEXT, 0, ctypes.cast(buf, ctypes.c_void_p), 2000) is None:
            raise GuiError('поле не приняло текст (EasyBuilder занят?) — попробуйте ещё раз или mode «real»')

    def close(self, hwnd):
        PostMessageW(hwnd, WM_CLOSE, 0, 0)
        time.sleep(0.2)

    def launch(self, exe, args):
        subprocess.Popen([exe] + list(args), cwd=os.path.dirname(exe) or None, close_fds=True,
                         creationflags=0x00000008 | 0x00000200)    # detached: EasyBuilder outlives this server
