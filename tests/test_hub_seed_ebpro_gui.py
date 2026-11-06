"""ebpro-gui: the seed passes the hub check; the driver on a fake Windows — only EasyBuilder's windows, one driver at
a time, no download/password windows, points inside the window, pictures, keys, fields, waiting, opening a project."""
import base64
import importlib.util
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.net import hub

GUI = Path(__file__).resolve().parent.parent / "hub" / "seed" / "ebpro-gui"
_saved = list(sys.path)
_spec = importlib.util.spec_from_file_location("ebpro_gui_server", GUI / "server.py")
srv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(srv)          # it puts its folder on sys.path to find gui.py; ebpro-cmtp has a server.py too
sys.path[:] = _saved
gui = sys.modules["gui"]

EXE = r"C:\EBpro\EasyBuilderPro.exe"
TOOLS = ["eb_edit", "eb_windows", "eb_shot", "eb_controls", "eb_click", "eb_drag", "eb_keys", "eb_write", "eb_wait",
         "eb_close", "eb_free"]


def _files() -> dict:
    return {str(p.relative_to(GUI)).replace(os.sep, "/"): p.read_bytes()
            for p in GUI.rglob("*") if p.is_file() and "__pycache__" not in p.parts}


class Fake:
    """Windows as the driver sees it: EasyBuilder, its download dialog, its diagnoser, Notepad and an IME helper."""

    def __init__(self):
        self.log, self.fg, self.launched = [], 100, []
        self.wins = [
            {"hwnd": 100, "title": "EasyBuilder Pro - [Нория]", "class": "Afx:1", "pid": 1, "exe": EXE,
             "rect": (10, 20, 810, 620), "visible": True},
            {"hwnd": 101, "title": "Download", "class": "#32770", "pid": 1, "exe": EXE, "rect": (100, 100, 400, 300),
             "visible": True},
            {"hwnd": 102, "title": "cMT Diagnoser", "class": "Qt", "pid": 2, "exe": r"C:\EBpro\cMTDiagnoser.exe",
             "rect": (0, 0, 300, 200), "visible": True},
            {"hwnd": 103, "title": "", "class": "IME", "pid": 1, "exe": EXE, "rect": (0, 0, 1, 1), "visible": True},
            {"hwnd": 200, "title": "Блокнот", "class": "Notepad", "pid": 3, "exe": r"C:\Windows\notepad.exe",
             "rect": (0, 0, 500, 500), "visible": True},
            {"hwnd": 104, "title": "Hidden", "class": "X", "pid": 1, "exe": EXE, "rect": (0, 0, 50, 50),
             "visible": False},
        ]

    def windows(self):
        return [dict(w) for w in self.wins]

    def foreground(self):
        return self.fg

    def bring(self, hwnd):
        self.log.append(("bring", hwnd))
        self.fg = hwnd
        return True

    def restore(self, hwnd):
        self.log.append(("restore", hwnd))

    def capture(self, hwnd):
        return 4, 2, bytes([0, 0, 255, 0] * 4 + [255, 0, 0, 0] * 4)        # red row, blue row (BGRA)

    def children(self, hwnd):
        return [{"hwnd": 555, "class": "Edit", "text": "Имя файла", "id": 1001, "rect": (110, 120, 210, 140),
                 "visible": True, "enabled": True},
                {"hwnd": 556, "class": "Button", "text": "OK", "id": 1, "rect": (0, 0, 9, 9), "visible": False}]

    def click(self, x, y, button, double):
        self.log.append(("click", x, y, button, double))

    def post_click(self, hwnd, x, y, button, double):
        self.log.append(("post_click", hwnd, x, y))

    def drag(self, *xy):
        self.log.append(("drag",) + xy)

    def keys(self, combos):
        self.log.append(("keys", combos))

    def post_keys(self, hwnd, combos):
        self.log.append(("post_keys", hwnd, combos))

    def text(self, s):
        self.log.append(("text", s))

    def post_text(self, hwnd, s):
        self.log.append(("post_text", hwnd, s))

    def set_text(self, hwnd, s):
        self.log.append(("set_text", hwnd, s))

    def close(self, hwnd):
        self.log.append(("close", hwnd))

    def launch(self, exe, args):
        self.launched.append((exe, args))
        stem = os.path.splitext(os.path.basename(args[0]))[0]
        self.wins.append({"hwnd": 300, "title": f"EasyBuilder Pro - [{stem}]", "class": "Afx:1", "pid": 9,
                          "exe": EXE, "rect": (0, 0, 900, 700), "visible": True})


@pytest.fixture
def fake(tmp_path, monkeypatch):
    f = Fake()
    monkeypatch.setattr(srv, "DRIVER", gui.Driver(f, gui.Lock(str(tmp_path / "gui.lock"))))
    return f


def _call(name, **args):
    return srv._call(name, args)


def _say(res):
    return res["content"][0]["text"]


def test_gui_seed_passes_the_hub_check():
    meta, report = hub.check(_files())
    assert report == {"errors": [], "warnings": []}
    spec = hub.tool_spec(meta)
    assert meta["name"] == "ebpro-gui" and spec["kind"] == "mcp" and spec["run"] == "python server.py"
    assert spec["platform"] == "windows" and spec["tools"] == TOOLS == [t["name"] for t in srv.TOOLS]


def test_gui_sees_only_easybuilder_windows(fake):
    data = json.loads(_say(_call("eb_windows")))
    assert [w["window"] for w in data["windows"]] == [100, 101, 102]        # not Notepad, the IME helper, hidden
    assert data["windows"][0]["foreground"] and data["windows"][2]["program"] == "cMTDiagnoser.exe"
    assert data["driver"] is None
    assert _call("eb_shot", window=200)["isError"]                          # Notepad is not EasyBuilder's


def test_gui_one_driver_at_a_time(fake):
    assert not _call("eb_click", who="Агент", x=5, y=6).get("isError")
    assert fake.log[-2:] == [("bring", 100), ("click", 15, 26, "left", False)]    # window corner + the point
    busy = _call("eb_keys", who="Помощник", keys="ctrl+s")
    assert busy["isError"] and "ведёт «Агент»" in _say(busy)
    assert json.loads(_say(_call("eb_windows")))["driver"]["who"] == "Агент"
    assert _call("eb_shot")["content"][1]["type"] == "image"               # looking is open to everyone
    assert _call("eb_free", who="Помощник")["isError"]
    assert not _call("eb_free", who="Агент").get("isError")
    assert not _call("eb_keys", who="Помощник", keys="ctrl+s").get("isError")
    assert _call("eb_click", x=1, y=1)["isError"]                          # who is required


def test_gui_leaves_download_and_passwords_to_the_person(fake):
    res = _call("eb_click", who="Агент", window="Download", x=5, y=5)
    assert res["isError"] and "человек" in _say(res)
    assert not any(e[0] == "click" for e in fake.log)


def test_gui_points_stay_inside_the_window(fake):
    res = _call("eb_click", who="Агент", x=800, y=10)                      # the window is 800 wide: 0..799
    assert res["isError"] and "вне окна" in _say(res)
    res = _call("eb_drag", who="Агент", window=100, x=1, y=2, to_x=30, to_y=40)
    assert not res.get("isError") and fake.log[-1] == ("drag", 11, 22, 40, 60)
    _call("eb_click", who="Агент", window="диагност", x=1, y=1)            # no such title
    _call("eb_click", who="Агент", window="Diagnoser", x=3, y=4, mode="post", button="right")
    assert fake.log[-1] == ("post_click", 102, 3, 4)


def test_gui_shot_is_a_png_of_the_window(fake, tmp_path):
    from PIL import Image
    res = _call("eb_shot", out_file=str(tmp_path / "s" / "shot.png"))
    img = Image.open(io.BytesIO(base64.b64decode(res["content"][1]["data"]))).convert("RGB")
    assert img.size == (4, 2) and img.getpixel((0, 0)) == (255, 0, 0) and img.getpixel((3, 1)) == (0, 0, 255)
    assert (tmp_path / "s" / "shot.png").read_bytes() == base64.b64decode(res["content"][1]["data"])
    half = _call("eb_shot", scale=0.5, show=False)
    assert len(half["content"]) == 1 and "2×1" in _say(half) and "×2" in _say(half)
    after = _call("eb_keys", who="Агент", keys="esc", shot=True)
    assert after["content"][1]["type"] == "image"


def test_gui_keys():
    assert gui.parse_keys("ctrl+s") == [[(0x11, False), (0x53, False)]]
    assert gui.parse_keys("alt, ф, F4") == [[(0x12, False)], [(0x41, False)], [(0x73, False)]]
    assert gui.parse_keys("delete, ctrl+shift+left") == [[(0x2E, True)], [(0x11, False), (0x10, False), (0x25, True)]]
    for bad in ("", "ctrl+", "hyper+x", ", ,"):
        with pytest.raises(gui.GuiError):
            gui.parse_keys(bad)


def test_gui_post_mode_takes_single_keys_only(fake):
    assert _call("eb_keys", who="Агент", keys="ctrl+s", mode="post")["isError"]
    assert not _call("eb_keys", who="Агент", keys="enter, tab", mode="post").get("isError")
    assert fake.log[-1] == ("post_keys", 100, [[(0x0D, False)], [(0x09, False)]])
    assert _call("eb_keys", who="Агент", keys="enter", mode="robot")["isError"]


def test_gui_fields_and_text(fake):
    data = json.loads(_say(_call("eb_controls")))
    assert data["controls"] == [{"control": 555, "class": "Edit", "text": "Имя файла", "id": 1001, "x": 100, "y": 100,
                                 "width": 100, "height": 20, "enabled": True}]
    assert not _call("eb_write", who="Агент", text="Нория 3", control=555).get("isError")
    assert fake.log[-1] == ("set_text", 555, "Нория 3")
    assert _call("eb_write", who="Агент", text="x", control=777)["isError"]
    _call("eb_write", who="Агент", text="Привет")
    assert fake.log[-2:] == [("bring", 100), ("text", "Привет")]
    assert _call("eb_write", who="Агент", text="x" * 4001)["isError"]


def test_gui_wait_close_and_edit(fake, tmp_path):
    assert "Есть окно 101" in _say(_call("eb_wait", title="download"))
    assert "больше нет" in _say(_call("eb_wait", title="Сохранить как", gone=True))
    res = _call("eb_wait", title="Сохранить как", timeout=0.01)
    assert res["isError"] and "не появилось" in _say(res)
    assert not _call("eb_close", who="Агент", window="Diagnoser").get("isError") and fake.log[-1] == ("close", 102)
    project, exe = tmp_path / "Нория 3.cmtp", tmp_path / "EasyBuilderPro.exe"
    project.write_bytes(b"x")
    exe.write_bytes(b"x")
    res = json.loads(_say(_call("eb_edit", path=str(project), exe=str(exe), wait=2)))
    assert res["window"]["window"] == 300 and fake.launched == [(str(exe), [str(project)])]
    assert _call("eb_edit", path=str(tmp_path / "nope.cmtp"))["isError"]
    (tmp_path / "a.txt").write_text("x")
    assert _call("eb_edit", path=str(tmp_path / "a.txt"))["isError"]


def test_gui_lock_runs_out(tmp_path):
    lock = gui.Lock(str(tmp_path / "l.lock"), hold=0.01)
    lock.take("A")
    import time
    time.sleep(0.05)
    lock.take("B")                       # A went quiet: B may drive
    assert lock.holder()[0] == "B"


@pytest.mark.skipif(os.name == "nt", reason="on Windows the real backend starts")
def test_gui_stdio_server_lists_tools_and_says_windows_only():
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "eb_windows", "arguments": {}}}]
    out = subprocess.run([sys.executable, str(GUI / "server.py")], input="\n".join(json.dumps(m) for m in msgs),
                         capture_output=True, text=True, timeout=30, encoding="utf-8").stdout.splitlines()
    replies = [json.loads(line) for line in out]
    assert replies[0]["result"]["serverInfo"]["name"] == "ebpro-gui"
    assert [t["name"] for t in replies[1]["result"]["tools"]] == TOOLS
    assert replies[2]["result"]["isError"] and "Windows" in replies[2]["result"]["content"][0]["text"]


@pytest.mark.parametrize("name", ["ebpro-project", "ebpro-donor", "ebpro-team", "ebpro-easybuilder", "ebpro-mnemoscheme"])
def test_ebpro_skills_pass_the_hub_check(name):
    root = GUI.parent / name
    files = {str(p.relative_to(root)).replace(os.sep, "/"): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    meta, report = hub.check(files)
    assert report == {"errors": [], "warnings": []} and meta["name"] == name and hub.doc_of(files) == "SKILL.md"
    body = files["SKILL.md"].decode("utf-8")
    for tool in ("ebpro-cmtp", "ebpro-gui"):            # the skills point at the tools they need
        assert tool in body or name == "ebpro-donor"


def test_gui_finds_easybuilder_as_the_window_keeps_it(tmp_path, monkeypatch):
    exe = tmp_path / "EB" / "EasyBuilderPro.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"x")
    (tmp_path / "desktop").mkdir()
    (tmp_path / "desktop" / "easybuilder.json").write_text(json.dumps({"exe": str(exe)}), encoding="utf-8")
    monkeypatch.setenv("SYSAI_HOME", str(tmp_path))
    monkeypatch.setenv("EBPRO_EXE", str(tmp_path / "nope.exe"))          # a wrong one is passed over
    assert gui.find_exe() == str(exe) and gui.lock_path() == str(tmp_path / "ebpro-gui.lock")
    with pytest.raises(gui.GuiError):
        gui.find_exe(str(tmp_path / "missing.exe"))
