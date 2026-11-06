"""The window «Агент SYSAI» (agents/desktop): what SYSAI gives out (app/net/desktop.py — the app's zip, its copy of
Electron, the offer in the signed document), how the synchronizer installs, updates and opens it
(sysai_agent/desktop.py), and the window's own checks (lib/rules.js) — the same answers as the synchronizer's on the
same cases; the window's JavaScript tests run here too when Node.js is on the machine."""
import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import zipfile
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")
ROOT = Path(__file__).resolve().parents[1]
DESK = ROOT / "agents" / "desktop"
sys.path.insert(0, str(ROOT / "agents" / "sync"))

from sysai_agent import cli, core, desktop as D, hermes, hook, paths, rules as R  # noqa: E402

from app import settings_store  # noqa: E402
from app.db import Department, SessionLocal, User  # noqa: E402
from app.net import access, agents, desktop  # noqa: E402

NODE = shutil.which("node")


def _node_ok() -> bool:
    if not NODE:
        return False
    try:
        out = subprocess.run([NODE, "--version"], capture_output=True, text=True, timeout=30).stdout
        return int(out.strip().lstrip("v").split(".")[0]) >= 18
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


needs_node = pytest.mark.skipif(not _node_ok(), reason="нужен Node.js 18+")


def _zip(files: dict) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return out.getvalue()


def _transport(tc):
    def send(method, url, headers, body, timeout):
        path = url.split("://", 1)[-1]
        path = path[path.index("/"):] if "/" in path else "/"
        r = tc.request(method, path, headers=headers, content=body)
        return r.status_code, {k.lower(): v for k, v in r.headers.items()}, r.content
    return send


# ------------------------------------------------------------------------------------------------- what SYSAI gives
def test_the_window_is_given_out_as_a_zip_of_agents_desktop(client, net_on):
    name, blob, sha = desktop.build()
    assert name == f"sysai-desktop-{desktop.version()}.zip" and desktop.build()[2] == sha
    archive = zipfile.ZipFile(io.BytesIO(blob))
    names = set(archive.namelist())
    assert {"package.json", "main.js", "preload.js", "lib/uimcp.js", "lib/rules.js", "lib/bridge.js", "lib/acp.js",
            "renderer/index.html", "renderer/fonts/fonts.css", "assets/icon.ico", "LICENSES.md"} <= names
    assert not any(n.startswith("test/") for n in names) and "README.md" not in names
    assert any(n.startswith("renderer/fonts/") and n.endswith(".woff2") for n in names)
    assert "url(/static/fonts/" not in archive.read("renderer/fonts/fonts.css").decode("utf-8")
    assert json.loads(archive.read("package.json"))["version"] == desktop.version()
    r = client.get(f"/agents/api/desktop/{name}")
    assert r.status_code == 200 and r.content == blob and r.headers["x-sha256"] == sha
    missing = client.get("/agents/api/desktop/sysai-desktop-0.0.1.zip")
    assert missing.status_code == 404 and name in missing.json()["error"]["message"]
    offer = desktop.offer(True)
    assert offer["sha256"] == sha and offer["file"] == name and offer["auto_update"] is True
    assert offer["electron"]["sha256"] == desktop.ELECTRON["sha256"] and all(u.startswith("https://") for u in offer["electron"]["urls"])
    assert D.offer({"desktop": offer})["sha256"] == sha, "the synchronizer takes what the server offers"
    settings_store.set_many({"agents_on": False})
    assert client.get(f"/agents/api/desktop/{name}").status_code == 404


def test_the_zip_follows_the_sources(tmp_path, monkeypatch):
    src = tmp_path / "desktop"
    shutil.copytree(DESK, src, ignore=shutil.ignore_patterns("node_modules"))
    monkeypatch.setattr(desktop, "DESKTOP", src)
    monkeypatch.setattr(desktop, "_cache", {})
    first = desktop.build()[2]
    assert desktop.build()[2] == first
    (src / "main.js").write_text((src / "main.js").read_text(encoding="utf-8") + "\n// новое\n", encoding="utf-8")
    assert desktop.build()[2] != first


def test_electron_comes_from_sysai_s_checked_copy(client, net_on, tmp_path, monkeypatch):
    import httpx
    monkeypatch.setattr(desktop.config, "DATA_DIR", tmp_path)
    file = desktop.ELECTRON["file"]
    r = client.get(f"/agents/api/desktop/electron/{file}")
    assert r.status_code == 503 and r.json()["error"]["code"] == "not_ready"  # the computer takes the release then
    assert client.get("/agents/api/desktop/electron/electron-v1.0.0-win32-x64.zip").status_code == 404
    blob = b"PK electron for windows" * 100
    monkeypatch.setitem(desktop.ELECTRON, "sha256", hashlib.sha256(blob).hexdigest())
    monkeypatch.setitem(desktop.ELECTRON, "urls", ["https://down.example/e.zip", "https://mirror.example/e.zip"])
    asked = []

    @contextlib.contextmanager
    def stream(method, url, **kw):
        asked.append(url)
        if "down.example" in url:
            raise httpx.ConnectError("нет связи")

        class Resp:
            def raise_for_status(self):
                return None

            def iter_bytes(self, size):
                yield blob[:7]
                yield blob[7:]
        yield Resp()
    folder = desktop.electron_cache()
    folder.mkdir(parents=True)
    (folder / "electron-v1.0.0-win32-x64.zip").write_bytes(b"old")
    assert desktop.fetch_electron(stream) == folder / file and asked == desktop.ELECTRON["urls"]
    assert not (folder / "electron-v1.0.0-win32-x64.zip").exists(), "the older one goes"
    r = client.get(f"/agents/api/desktop/electron/{file}")
    assert r.status_code == 200 and r.content == blob and r.headers["x-sha256"] == desktop.ELECTRON["sha256"]
    (folder / file).unlink()
    monkeypatch.setitem(desktop.ELECTRON, "sha256", "0" * 64)  # not the release SYSAI names: never kept
    with pytest.raises(RuntimeError, match="sha256"):
        desktop.fetch_electron(stream)
    assert list(folder.iterdir()) == []


def test_the_signed_document_names_the_window_and_the_person(net_agent, monkeypatch):
    got = access.document(net_agent["id"])
    doc = got["doc"]
    assert doc["desktop"]["sha256"] == desktop.build()[2] and doc["desktop"]["version"] == desktop.version()
    assert doc["desktop"]["electron"]["sha256"] == desktop.ELECTRON["sha256"]
    assert doc["desktop"]["auto_update"] == doc["synchronizer"]["auto_update"]
    with SessionLocal() as s:
        user = s.get(User, net_agent["user_id"])
        assert doc["person"] == (user.display_name or user.username)
        user.display_name = "Иван Петров"
        s.commit()
    again = access.document(net_agent["id"])
    assert again["doc"]["person"] == "Иван Петров" and again["rev"] != got["rev"], "the window shows who it serves"
    real = desktop.offer
    monkeypatch.setattr(desktop, "offer", lambda auto: {**real(auto), "sha256": "f" * 64})
    assert access.document(net_agent["id"])["rev"] != again["rev"], "a new build of the window is a new revision"


# ------------------------------------------------------------------------------------------------- the synchronizer
GOOD = {"version": "0.1.0", "file": "sysai-desktop-0.1.0.zip", "sha256": "a" * 64, "auto_update": True,
        "electron": {"version": "44.10.2", "file": "electron-v44.10.2-win32-x64.zip", "sha256": "b" * 64,
                     "urls": ["https://github.com/e.zip", "http://plain.example/e.zip", 5]}}


def test_only_a_well_formed_offer_is_taken():
    got = D.offer({"desktop": GOOD})
    assert got["electron"]["urls"] == ["https://github.com/e.zip"] and got["auto_update"] is True
    for bad in ({"file": "../../evil.zip"}, {"sha256": "x"}, {"version": "latest"}, {"electron": None}):
        assert D.offer({"desktop": {**GOOD, **bad}}) == {}
    linux = {**GOOD["electron"], "file": "electron-v44.10.2-linux-x64.zip"}
    assert D.offer({"desktop": {**GOOD, "electron": linux}}) == {}
    assert D.offer({}) == {} and D.offer(None) == {}


def test_the_window_is_due_on_windows_with_auto_update_once_per_retry(tmp_path, monkeypatch):
    got = D.offer({"desktop": GOOD})
    state = {"desktop_offer": got}
    monkeypatch.setattr(paths, "WINDOWS", False)
    assert D.due(tmp_path, state) == {}
    monkeypatch.setattr(paths, "WINDOWS", True)
    assert D.due(tmp_path, state) == got
    assert D.due(tmp_path, {"desktop_offer": {**got, "auto_update": False}}) == {}
    tried = {**state, "desktop_try": {"sha256": got["sha256"], "at": 1000.0}}
    assert D.due(tmp_path, tried, now=1060.0) == {} and D.due(tmp_path, tried, now=1001.0 + D.RETRY) == got


def test_an_archive_cannot_write_outside_its_folder(tmp_path):
    bad = tmp_path / "bad.zip"
    bad.write_bytes(_zip({"ok.txt": "x", "../evil.txt": "x"}))
    with pytest.raises(D.DesktopError, match="за пределы"):
        D._extract(bad, tmp_path / "out", "0" * 64)
    assert not (tmp_path / "evil.txt").exists() and not (tmp_path / "out").exists()


@pytest.fixture
def pc(client, net_on, tmp_path, monkeypatch):
    """A computer connected by the code, «Windows» for the window: {"home", "profile", "send", "client", "ran"}."""
    from fastapi.testclient import TestClient
    from app.main import app
    monkeypatch.setenv("SYSAI_KEYRING", "0")
    monkeypatch.delenv("SYSAI_AGENT_TOKEN", raising=False)
    monkeypatch.setattr(hermes, "version", lambda: "0.9.1")
    monkeypatch.setattr(hermes, "gateway_running", lambda profile_dir: False)
    monkeypatch.setattr(hermes, "_alive", lambda pid: pid == os.getpid())
    monkeypatch.setitem(core._hermes_version, "at", 0.0)
    with SessionLocal() as s:
        admin = s.query(User).filter(User.role == "superuser").first()
        dept = Department(name="ОПО")
        s.add(dept)
        s.commit()
        admin_id, dept_id = admin.id, dept.id
    agent = agents.create(admin_id, "opo-pc", dept_id, None, "hermes", "", by="test")
    home, profile = tmp_path / "SYSAI", tmp_path / "hermes" / "profiles" / "opo-pc"
    send = _transport(TestClient(app))
    core.connect("http://testserver", agents.issue_code(agent.id), home, transport=send, hermes_home=str(profile))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for exe in ("sysai-agent.exe", "sysai-agentw.exe"):
        (bin_dir / exe).write_bytes(b"MZ")
    monkeypatch.setattr(core, "launcher", lambda: str(bin_dir / "sysai-agent.exe"))
    ran = []

    def run(args, **kw):  # the window writing its shortcuts (`electron.exe <app> --sysai-shortcuts`)
        ran.append((args, kw))
        (home / "desktop" / "shortcuts.json").write_text(json.dumps({"done": ["desktop", "start"]}), encoding="utf-8")
        return subprocess.CompletedProcess(args, 0)
    monkeypatch.setattr(D.subprocess, "run", run)
    monkeypatch.setattr(paths, "WINDOWS", True)
    return {"home": home, "profile": profile, "send": send, "ran": ran, "agent_id": agent.id, "user_id": admin_id,
            "client": core.Client("http://testserver", (home / "token").read_text(encoding="utf-8"), send)}


@pytest.fixture
def electron(monkeypatch):
    """The Electron release, as the document names it and the addresses give it (SYSAI has no copy yet: 503)."""
    blob = _zip({"electron.exe": b"MZ electron", "resources/default_app.asar": b"asar", "LICENSE": b"MIT"})
    monkeypatch.setitem(desktop.ELECTRON, "sha256", hashlib.sha256(blob).hexdigest())
    monkeypatch.setitem(desktop.ELECTRON, "urls", ["https://releases.example/electron.zip"])
    asked = []

    def urlopen(request, timeout=60):
        asked.append(request.full_url)
        if "/agents/api/desktop/electron/" in request.full_url:
            raise urllib.error.HTTPError(request.full_url, 503, "Service Unavailable", {}, io.BytesIO(b""))
        return io.BytesIO(blob)
    monkeypatch.setattr(D.urllib.request, "urlopen", urlopen)
    return {"blob": blob, "asked": asked}


def _state(home) -> dict:
    return json.loads((Path(home) / "state.json").read_text(encoding="utf-8"))


def test_the_synchronizer_installs_the_window_sysai_signed(pc, electron, monkeypatch):
    home, client = pc["home"], pc["client"]
    core.sync_once(home, transport=pc["send"], restart=False)
    state = _state(home)
    got = state["desktop_offer"]
    assert got["sha256"] == desktop.build()[2] and got["electron"]["sha256"] == desktop.ELECTRON["sha256"]
    assert state["person"] and D.installed(home) == {} and D.due(home, state) == got

    def broken(method, url, headers, body, timeout):
        status, h, data = pc["send"](method, url, headers, body, timeout)
        return status, h, (data + b"x" if "/agents/api/desktop/sysai-desktop" in url else data)
    with pytest.raises(D.DesktopError, match="sha256"):  # not the build SYSAI signed: never installed
        D.install(home, core.Client(client.server, client.token, broken), got, say=lambda t: None)
    assert D.installed(home) == {} and not (home / "desktop" / "install.lock").exists()

    electron["asked"].clear()
    info = D.install(home, client, got, say=lambda t: None)
    exe, app = Path(info["exe"]), Path(info["app"])
    assert exe.read_bytes() == b"MZ electron" and exe.parent.name == f"electron-{desktop.ELECTRON_VERSION}"
    assert electron["asked"] == [], "Electron was kept from the first try"
    assert app.name == f"app-{got['version']}-{got['sha256'][:8]}" and (app / "lib" / "uimcp.js").is_file()
    saved = json.loads((home / "desktop" / "desktop.json").read_text(encoding="utf-8"))
    assert saved == info and saved["shortcuts"] == ["desktop", "start"] and saved["person"] == state["person"]
    assert saved["launcher"].endswith("sysai-agentw.exe") and saved["profile_dir"] == str(pc["profile"])
    assert saved["agent"] == "opo-pc" and saved["synchronizer"] == core.__version__ and saved["electron_sha256"] == got["electron"]["sha256"]
    [(args, kw)] = pc["ran"]
    assert args == [str(exe), str(app), "--sysai-shortcuts"] and kw["env"]["SYSAI_HOME"] == str(home)
    assert (home / "desktop" / "sysai.ico").is_file() and not (home / "desktop" / "downloads").exists()
    assert D.current(home, got) and D.due(home, state) == {}
    assert core.status(home)["desktop"]["version"] == got["version"] and core.versions(home)["desktop"] == got["version"]

    # once more: nothing downloaded, the shortcuts are left as the person keeps them
    pc["ran"].clear()
    assert D.install(home, client, got, say=lambda t: None) == {**info, "installed_at": D.installed(home)["installed_at"]}
    assert electron["asked"] == [] and pc["ran"] == []

    # a new build: a folder of its own; the running window keeps the one it uses
    name, blob, _ = desktop.build()
    old = zipfile.ZipFile(io.BytesIO(blob))
    blob2 = _zip({**{n: old.read(n) for n in old.namelist()}, "NEW.txt": b"new"})
    sha2 = hashlib.sha256(blob2).hexdigest()
    monkeypatch.setattr(desktop, "build", lambda: (name, blob2, sha2))
    core.sync_once(home, transport=pc["send"], restart=False)
    got2 = _state(home)["desktop_offer"]
    assert got2["sha256"] == sha2 and not D.current(home, got2)
    bridge = {"port": 9, "token": "t", "pid": os.getpid(), "app": str(app), "exe": str(exe)}
    (home / "desktop" / "bridge.json").write_text(json.dumps(bridge), encoding="utf-8")
    info2 = D.install(home, client, got2, say=lambda t: None)
    assert Path(info2["app"]) != app and (Path(info2["app"]) / "NEW.txt").is_file() and app.is_dir()
    (home / "desktop" / "bridge.json").unlink()  # the window was closed
    D.install(home, client, got2, say=lambda t: None)
    assert not app.exists() and exe.is_file(), "the old build goes, Electron stays"


def test_the_loop_installs_the_window_in_the_background_once(pc, electron):
    home = pc["home"]
    settings_store.set_many({"agents_auto_update": False})
    assert core.run(home, every=1, transport=pc["send"], cycles=1, sleep=lambda _: None) == 0
    assert D.installed(home) == {}, "«обновлять сами» off: only the installer or sysai-agent desktop install it"
    settings_store.set_many({"agents_auto_update": True})
    assert core.run(home, every=1, transport=pc["send"], cycles=2, sleep=lambda _: None) == 0
    info = D.installed(home)
    assert info["sha256"] == desktop.build()[2] and _state(home)["desktop_try"]["sha256"] == info["sha256"]
    assert len(pc["ran"]) == 1, "the shortcuts come with the first installation"


def test_a_failed_installation_is_not_tried_again_before_retry(pc, electron, monkeypatch):
    home = pc["home"]
    monkeypatch.setitem(desktop.ELECTRON, "sha256", "0" * 64)  # what the addresses give is not what SYSAI names
    assert core.run(home, every=1, transport=pc["send"], cycles=2, sleep=lambda _: None) == 0
    assert D.installed(home) == {} and len(electron["asked"]) == 2, "tried once, not again before RETRY"
    assert not list((home / "desktop").glob("**/*.part"))


def test_desktop_json_follows_the_synchronizer(pc, electron):
    home = pc["home"]
    core.sync_once(home, transport=pc["send"], restart=False)
    D.install(home, pc["client"], _state(home)["desktop_offer"], say=lambda t: None)
    with SessionLocal() as s:
        s.get(User, pc["user_id"]).display_name = "Мария Сидорова"
        s.commit()
    core.sync_once(home, transport=pc["send"], restart=False)
    assert D.installed(home)["person"] == "Мария Сидорова"
    assert D.installed(home)["python"] == sys.executable, "the window runs the hub tools of «Холст» with it"


def test_sysai_agent_desktop_installs_and_opens_the_window(pc, electron, monkeypatch, tmp_path, capsys):
    home = pc["home"]
    real = core.client_for
    monkeypatch.setattr(core, "client_for", lambda h, cfg, transport=None: real(h, cfg, pc["send"]))
    monkeypatch.setenv("ELECTRON_RUN_AS_NODE", "1")
    opened = []
    monkeypatch.setattr(D.subprocess, "Popen", lambda args, **kw: opened.append((args, kw)))
    assert cli.main(["--home", str(home), "desktop", "--install-only"]) == 0
    info = D.installed(home)
    assert info and opened == [] and len(pc["ran"]) == 1
    assert cli.main(["--home", str(home), "desktop", "--install-only"]) == 0 and len(pc["ran"]) == 2, \
        "the installer writes the shortcuts again"
    doc = tmp_path / "отчёт.md"
    doc.write_text("# Отчёт", encoding="utf-8")
    assert cli.main(["--home", str(home), "desktop", str(doc)]) == 0
    [(args, kw)] = opened
    assert args == [info["exe"], info["app"], str(doc.resolve())]
    assert "ELECTRON_RUN_AS_NODE" not in kw["env"] and kw["env"]["SYSAI_HOME"] == str(home) and kw["cwd"] == info["app"]
    assert cli.main(["--home", str(home), "status"]) == 0 and f"Окно агента «Агент SYSAI»: {info['version']}" in capsys.readouterr().out
    monkeypatch.setattr(paths, "WINDOWS", False)
    assert cli.main(["--home", str(home), "desktop"]) == 1


def test_a_running_window_hears_of_the_new_build(pc):
    import http.server
    import threading
    home = pc["home"]
    heard = []

    class Window(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            heard.append((self.path, self.headers["Authorization"], json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"ok": true}')

        def log_message(self, *a):
            return None
    server = http.server.HTTPServer(("127.0.0.1", 0), Window)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        (home / "desktop").mkdir(parents=True, exist_ok=True)
        (home / "desktop" / "bridge.json").write_text(json.dumps({"port": server.server_port, "token": "tok", "pid": os.getpid()}),
                                                     encoding="utf-8")
        assert D.notify(home, D.UPDATED) is True
        assert heard == [("/notice", "Bearer tok", {"text": D.UPDATED, "kind": "info"})]
        (home / "desktop" / "bridge.json").write_text(json.dumps({"port": server.server_port, "token": "tok", "pid": 999999}),
                                                     encoding="utf-8")
        assert D.notify(home, "x") is False, "a window that is gone is not asked"
    finally:
        server.shutdown()


def test_the_installer_installs_the_window():
    script = (ROOT / "agents" / "installer" / "install.ps1").read_text(encoding="utf-8")
    assert "& sysai-agent desktop --install-only" in script
    assert script.index("sysai-agent desktop --install-only") < script.index("CurrentVersion\\Run")
    assert "-ExecutionPolicy Bypass" not in script.split("# 7.")[1], "the window step bypasses nothing"


# ------------------------------------------------------------------------------------------------- the window itself
@needs_node
def test_the_window_s_own_tests():
    r = subprocess.run([NODE, "--test", "test/unit.test.js"], cwd=DESK, capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, (r.stdout[-6000:] + r.stderr[-3000:])


PARITY = r"""
const rules = require('./lib/rules');
let data = '';
process.stdin.on('data', (d) => { data += d; });
process.stdin.on('end', () => {
  const out = JSON.parse(data).map((c) => {
    if (c.kind === 'norm') return rules.norm(c.path);
    if (c.kind === 'path') return rules.pathAllowed(c.rules, c.path, c.write);
    if (c.kind === 'command') return rules.commandDenied(c.rules, c.command);
    if (c.kind === 'guarded') return rules.guardedPath(c.path, c.home, c.profile);
    return rules.touchesGuard(c.text, c.home, c.profile);
  });
  process.stdout.write(JSON.stringify(out));
});
"""


def _python(case):
    if case["kind"] == "norm":
        return R._norm(case["path"])
    if case["kind"] == "path":
        return R.path_allowed(case["rules"], case["path"], case["write"])
    if case["kind"] == "command":
        return R.command_denied(case["rules"], case["command"])
    if case["kind"] == "guarded":
        return hook.guarded_path(case["path"], Path(case["home"]), Path(case["profile"]) if case["profile"] else None)
    return hook.touches_guard(case["text"], Path(case["home"]), Path(case["profile"]) if case["profile"] else None)


@needs_node
def test_the_window_checks_as_the_synchronizer_does():
    cases = [{"kind": "norm", "path": p} for p in (
        "C:", "C:\\", "c:/Users//a/./b/../c/", "\\\\srv\\share\\x\\..\\y", "..\\a", "a\\..\\..\\b", "C:\\a\\..\\..\\b",
        "  C:\\Temp  ", "", "C:a\\..\\b", "/home/u/x/", "D:\\Проекты\\Отчёт.XLSX", "C:\\a\\.\\.\\b\\\\\\c", "~\\x", ".")]
    folders = [
        {"read": ["C:\\Users\\a"], "write": ["C:\\Users\\a\\Отчёты"], "deny": ["C:\\Users\\a\\Личное"]},
        {"read": ["*"], "write": [], "deny": ["C:\\Windows", "D:\\"]},
        {"read": [], "write": ["*"], "deny": []},
        {"read": ["c:/users/A/"], "write": [], "deny": []},
    ]
    for f in folders:
        for p in ("C:\\Users\\a\\doc.txt", "C:\\USERS\\A\\Отчёты\\x.xlsx", "C:\\Users\\a\\Личное\\x", "C:\\Users\\ab\\x",
                  "C:\\Users\\a\\..\\b\\x", "C:\\Windows\\win.ini", "D:\\x", "E:\\x", "C:\\Users\\a", ""):
            for write in (False, True):
                cases.append({"kind": "path", "rules": {"folders": f}, "path": p, "write": write})
    deny = ["*format*", "rm -rf*", "del /s*", "shutdown*", "reg  delete *", "[!a]bc*", "a?c", "*[0-9]*", "x[]y", "q[!]z",
            "*[*", "[a-]z", "*.BAT"]
    for run in (True, False):
        for c in ("format c:", "FORMAT   D: /q", "rm -rf /", "rm  -rf /tmp", "del /s /q c:\\x", "shutdown /s /t 0",
                  "reg delete HKCU\\x", "xbc", "abc", "a-c", "v2", "x[]y", "q!z", "a[b", "-z", "run.bat", "dir",
                  "echo\u00a0format", "echo\x1fformat", ""):
            cases.append({"kind": "command", "rules": {"commands": {"run": run, "deny": deny}}, "command": c})
    home = "C:\\Users\\a\\AppData\\Local\\SYSAI"
    for profile in ("C:/Users/a/AppData/Local/hermes/profiles/opo", "C:/Users/a/AppData/Local/hermes", ""):
        for p in (f"{home}\\access.json", f"{home}\\proposals\\skill.md", f"{home}\\PROPOSALS", f"{home}x\\a",
                  "C:/Users/a/AppData/Local/hermes/profiles/opo/config.yaml", "C:/Users/a/AppData/Local/hermes/.env",
                  "C:/Users/a/AppData/Local/hermes/profiles/opo/notes.md", "C:\\Temp\\a"):
            cases.append({"kind": "guarded", "path": p, "home": home, "profile": profile})
        for t in ("sysai-agent status", "sysai-agent stop", "C:\\x\\sysai-agentw.exe run", "hermes config set x",
                  "hermes -p opo chat", "type %LOCALAPPDATA%\\SYSAI\\state.json", "dir C:/Users/a/AppData/Local/sysai",
                  "notepad profiles\\opo\\SOUL.md", "notepad C:\\Users\\a\\AppData\\Local\\hermes\\config.yaml", "dir C:\\"):
            cases.append({"kind": "touches", "text": t, "home": home, "profile": profile})
    r = subprocess.run([NODE, "-e", PARITY], cwd=DESK, input=json.dumps(cases), capture_output=True, text=True,
                       encoding="utf-8", timeout=120)
    assert r.returncode == 0, r.stderr
    js = json.loads(r.stdout)
    differ = [(c, _python(c), j) for c, j in zip(cases, js) if _python(c) != j]
    assert not differ, differ[:5]


def test_render_docker_contains_all_desktop_sources():
    docker = (ROOT / 'Dockerfile').read_text(encoding='utf-8')
    assert 'COPY agents/desktop agents/desktop' in docker
    assert 'COPY agents/sync agents/sync' in docker
    assert 'COPY agents/installer agents/installer' in docker
    name, blob, _ = desktop.build()
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        assert name != 'sysai-desktop-0.0.0.zip'
        assert {'package.json', 'main.js', 'renderer/index.html', 'lib/canvas.js'} <= set(z.namelist())
