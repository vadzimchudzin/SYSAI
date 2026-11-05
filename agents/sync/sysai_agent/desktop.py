"""The window «Агент SYSAI» on this computer (agents/desktop): what SYSAI signed — the document's «desktop», the app's
zip by its sha256 and the Electron release that runs it (app/net/desktop.py) — downloaded, checked and kept in the
SYSAI folder: desktop\\electron-<version> (from SYSAI's copy first, else from the release addresses),
desktop\\app-<version>-<sha> and desktop\\desktop.json, what the window reads (Hermes, the launcher, the person, the
profile). The shortcuts «Агент SYSAI» on the desktop and in Start are written by the window itself
(`electron.exe <app> --sysai-shortcuts`) and start «sysai-agentw desktop»: the version installed now, whichever it is.
Updated as the synchronizer is: with «обновлять сами» a new build is installed in the background; a running window
keeps its folders, the next start takes the new one. Windows only."""
import datetime as dt
import hashlib
import http.client
import json
import logging
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from . import __version__, fsutil, paths
from .client import ApiError

log = logging.getLogger("sysai_agent")
RETRY = 6 * 3600
LIMIT = 400 * 1024 * 1024
CHUNK = 1 << 20
FOLDER = "desktop"
AT_SYSAI = "/agents/api/desktop/electron/{file}"
DETACHED = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
NETWORK = (urllib.error.URLError, http.client.HTTPException, OSError, TimeoutError)
UPDATED = "Вышла новая версия окна агента: она откроется при следующем запуске окна"


class DesktopError(Exception):
    pass


def offer(doc) -> dict:
    """What the signed document offers, checked for its shape ({} — nothing)."""
    got = doc.get("desktop") if isinstance(doc, dict) else None
    if not isinstance(got, dict):
        return {}
    el = got.get("electron") if isinstance(got.get("electron"), dict) else {}
    version, file, sha = (str(got.get(k) or "") for k in ("version", "file", "sha256"))
    ev, ef, es = (str(el.get(k) or "") for k in ("version", "file", "sha256"))
    if not (re.fullmatch(r"\d+(?:\.\d+){1,3}", version) and re.fullmatch(r"sysai-desktop-[0-9A-Za-z.\-]+\.zip", file)
            and re.fullmatch(r"[0-9a-f]{64}", sha) and re.fullmatch(r"\d+\.\d+\.\d+", ev)
            and re.fullmatch(rf"electron-v{re.escape(ev)}-win32-(?:x64|arm64|ia32)\.zip", ef)
            and re.fullmatch(r"[0-9a-f]{64}", es)):
        return {}
    urls = [u for u in el.get("urls") or [] if isinstance(u, str) and u.startswith("https://")][:5]
    return {"version": version, "file": file, "sha256": sha, "auto_update": bool(got.get("auto_update")),
            "electron": {"version": ev, "file": ef, "sha256": es, "urls": urls}}


def folder(home) -> Path:
    return Path(home) / FOLDER


def installed(home) -> dict:
    """desktop.json of the installed window, when its files are in place ({} — not installed)."""
    info = fsutil.read_json(folder(home) / "desktop.json", {})
    if not isinstance(info, dict) or not info.get("exe") or not info.get("app"):
        return {}
    if not Path(info["exe"]).is_file() or not (Path(info["app"]) / "package.json").is_file():
        return {}
    return info


def current(home, got: dict) -> bool:
    info = installed(home)
    return bool(info and got) and info.get("sha256") == got.get("sha256") and \
        info.get("electron_sha256") == (got.get("electron") or {}).get("sha256")


def due(home, state: dict, now: float | None = None) -> dict:
    """The offer to install in the background now ({} — nothing): Windows, «обновлять сами», not what is installed,
    not tried within RETRY."""
    got = state.get("desktop_offer") if isinstance(state.get("desktop_offer"), dict) else {}
    if not paths.WINDOWS or not got.get("auto_update") or current(home, got):
        return {}
    tried = state.get("desktop_try") if isinstance(state.get("desktop_try"), dict) else {}
    now = time.time() if now is None else now
    if tried.get("sha256") == got.get("sha256") and now - float(tried.get("at") or 0) < RETRY:
        return {}
    return got


def _alive(pid: int) -> bool:
    from .hermes import _alive as alive
    return alive(pid)


def _lock(home) -> Path | None:
    """One installation at a time (the installer, the background loop, «sysai-agent desktop»)."""
    path = folder(home) / "install.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return path
        except FileExistsError:
            try:
                pid = int(fsutil.read_text(path).strip() or 0)
            except ValueError:
                pid = 0
            if pid == os.getpid() or (pid and _alive(pid)):
                return None
            path.unlink(missing_ok=True)
    return None


def _host(url: str) -> str:
    return url.split("/")[2] if "://" in url else url


def download(url: str, target: Path, sha: str, opener=None) -> int:
    """Streams the file to `target`, checked against the sha256 on the way; the size."""
    opener = opener or urllib.request.urlopen
    request = urllib.request.Request(url, headers={"User-Agent": f"sysai-agent/{__version__}"})
    part = target.with_name(target.name + ".part")
    digest, size = hashlib.sha256(), 0
    try:
        with opener(request, timeout=60) as resp, open(part, "wb") as handle:
            while True:
                chunk = resp.read(CHUNK)
                if not chunk:
                    break
                size += len(chunk)
                if size > LIMIT:
                    raise DesktopError("слишком большой файл")
                digest.update(chunk)
                handle.write(chunk)
        if digest.hexdigest() != sha:
            raise DesktopError("файл не совпал с подписанным SYSAI (sha256)")
        os.replace(part, target)
        return size
    except BaseException:
        part.unlink(missing_ok=True)
        raise


def _extract(archive_path: Path, target: Path, sha: str) -> None:
    """The zip to `target` whole (through a .tmp folder), nothing outside it; «.sha256» marks what it is."""
    tmp = target.with_name(target.name + ".tmp")
    fsutil.rmtree(tmp)
    root = tmp.resolve()
    with zipfile.ZipFile(archive_path) as archive:
        for item in archive.infolist():
            dest = (tmp / item.filename).resolve()
            if dest != root and root not in dest.parents:
                raise DesktopError(f"в архиве путь за пределы папки: {item.filename}")
        archive.extractall(tmp)
    (tmp / ".sha256").write_text(sha, encoding="utf-8")
    if target.exists():
        fsutil.rmtree(target)
    os.replace(tmp, target)


def _ready(target: Path, probe: str, sha: str) -> bool:
    return (target / probe).is_file() and fsutil.read_text(target / ".sha256").strip() == sha


def _electron(home, client, el: dict, say, opener=None) -> Path:
    base = folder(home)
    target = base / f"electron-{el['version']}"
    if _ready(target, "electron.exe", el["sha256"]):
        return target
    downloads = base / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    archive = downloads / el["file"]
    urls = ([client.server + AT_SYSAI.format(file=el["file"])] if client is not None else []) + list(el["urls"])
    problems = []
    for url in urls:
        try:
            say(f"скачиваю Electron {el['version']} ({_host(url)})…")
            download(url, archive, el["sha256"], opener)
            break
        except (DesktopError, *NETWORK) as exc:
            problems.append(f"{_host(url)}: {exc}")
    else:
        raise DesktopError("Electron для окна агента не скачан — " + "; ".join(problems))
    try:
        _extract(archive, target, el["sha256"])
    finally:
        archive.unlink(missing_ok=True)
    return target


def _app(home, client, got: dict, say) -> Path:
    base = folder(home)
    target = base / f"app-{got['version']}-{got['sha256'][:8]}"
    if _ready(target, "package.json", got["sha256"]):
        return target
    say(f"скачиваю окно агента {got['version']}…")
    data, _ = client.get_bytes(f"/agents/api/desktop/{got['file']}")
    if hashlib.sha256(data).hexdigest() != got["sha256"]:
        raise DesktopError(f"окно агента {got['version']} не совпало с подписанным SYSAI (sha256): не установлено")
    archive = base / "downloads" / got["file"]
    fsutil.atomic_write(archive, data)
    try:
        _extract(archive, target, got["sha256"])
    finally:
        archive.unlink(missing_ok=True)
    return target


def _launcher(sysai: str) -> str:
    """«sysai-agentw» next to «sysai-agent»: it starts the window without a console."""
    if not sysai:
        return ""
    quiet = Path(sysai).with_name(Path(sysai).stem + "w" + Path(sysai).suffix)
    return str(quiet) if quiet.exists() else sysai


def fields(home, state: dict | None = None) -> dict:
    """What the window takes from the synchronizer: the agent, the profile, the programs, the person."""
    from . import core
    cfg = fsutil.read_json(Path(home) / "agent.json", {}) or {}
    state = state if state is not None else (fsutil.read_json(Path(home) / "state.json", {}) or {})
    sysai = core.launcher()
    return {"agent": cfg.get("agent", ""), "server": cfg.get("server", ""),
            "profile": cfg.get("profile") or cfg.get("agent", ""),
            "profile_dir": str(paths.hermes_profile_dir(cfg)) if cfg.get("agent") else "",
            "hermes": shutil.which("hermes") or "", "sysai_agent": sysai, "launcher": _launcher(sysai),
            "person": str(state.get("person") or ""), "synchronizer": __version__}


def refresh(home, state: dict) -> bool:
    """desktop.json follows the synchronizer (the person, the programs, its version); True — rewritten."""
    info = installed(home)
    if not info or (folder(home) / "install.lock").exists():  # an installation writes it now
        return False
    fresh = {**info, **{k: v for k, v in fields(home, state).items() if v}}  # what is not found now is kept
    if fresh == info:
        return False
    fsutil.write_json(folder(home) / "desktop.json", fresh)
    return True


def _clean_env(home) -> dict:
    env = {k: v for k, v in os.environ.items() if k.upper() != "ELECTRON_RUN_AS_NODE"}
    env["SYSAI_HOME"] = str(home)
    return env


def _shortcuts(home, info: dict, run=None) -> dict:
    """The window writes its shortcuts itself (Electron sets the AppUserModelID of the taskbar on them)."""
    run = run or subprocess.run
    report = folder(home) / "shortcuts.json"
    report.unlink(missing_ok=True)
    try:
        run([info["exe"], info["app"], "--sysai-shortcuts"], env=_clean_env(home), cwd=info["app"], timeout=120,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"done": [], "error": str(exc)}
    got = fsutil.read_json(report, {}) or {}
    return got if isinstance(got, dict) else {"done": []}


def _same(a, b) -> bool:
    return bool(a) and bool(b) and os.path.normcase(os.path.abspath(str(a))) == os.path.normcase(os.path.abspath(str(b)))


def bridge(home) -> dict:
    """The running window's bridge.json, when that window is alive ({} — none runs)."""
    info = fsutil.read_json(folder(home) / "bridge.json", {}) or {}
    if not isinstance(info, dict) or not info.get("port") or not info.get("token"):
        return {}
    try:
        return info if _alive(int(info.get("pid") or 0)) else {}
    except (TypeError, ValueError):
        return {}


def _cleanup(home, keep: list) -> None:
    """Older versions go, but not what a running window uses: a folder that cannot be renamed is held — it stays."""
    base = folder(home)
    live = bridge(home)
    held = [Path(live["exe"]).parent if live.get("exe") else None, live.get("app")]
    for p in base.iterdir() if base.is_dir() else []:
        if not p.is_dir() or not re.match(r"(?:electron|app)-", p.name):
            continue
        if any(_same(p, k) for k in keep) or any(_same(p, h) for h in held if h):
            continue
        trash = p if ".old-" in p.name else p.with_name(f"{p.name}.old-{os.getpid()}-{int(time.time())}")
        try:
            if trash != p:
                os.rename(p, trash)
            fsutil.rmtree(trash)
        except OSError:
            continue
    shutil.rmtree(base / "downloads", ignore_errors=True)


def notify(home, text: str, kind: str = "info", timeout: float = 5) -> bool:
    """A notice in the running window (its bridge on 127.0.0.1, no proxy)."""
    info = bridge(home)
    if not info:
        return False
    request = urllib.request.Request(f"http://127.0.0.1:{int(info['port'])}/notice", method="POST",
                                     data=json.dumps({"text": text, "kind": kind}, ensure_ascii=False).encode("utf-8"),
                                     headers={"Authorization": f"Bearer {info['token']}", "Content-Type": "application/json"})
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=timeout) as resp:
            return resp.status == 200
    except NETWORK:
        return False


def install(home, client, got: dict, say=None, shortcuts: bool | None = None, opener=None, run=None) -> dict:
    """Installs what the offer names (what is already in place stays); desktop.json as written. `shortcuts`: write
    them (True), not (False), or only the first time and when the launcher moved (None)."""
    if not paths.WINDOWS:
        raise DesktopError("окно агента «Агент SYSAI» есть только для Windows")
    say = say or log.info
    home = Path(home)
    lock = _lock(home)
    if lock is None:
        raise DesktopError("окно агента уже ставится (установщик или синхронизатор в фоне): подождите несколько минут")
    try:
        before = installed(home)
        electron = _electron(home, client, got["electron"], say, opener)
        app = _app(home, client, got, say)
        icon = app / "assets" / "icon.ico"
        if icon.is_file():
            shutil.copyfile(icon, folder(home) / "sysai.ico")
        info = {**fields(home), "version": got["version"], "file": got["file"], "sha256": got["sha256"],
                "electron": got["electron"]["version"], "electron_sha256": got["electron"]["sha256"],
                "exe": str(electron / "electron.exe"), "app": str(app),
                "installed_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
        fsutil.write_json(folder(home) / "desktop.json", info)
        if shortcuts is None:
            shortcuts = not before or not info["launcher"] or before.get("launcher") != info["launcher"]
        if shortcuts:
            made = _shortcuts(home, info, run)
            info["shortcuts"] = made.get("done") or []
            if "desktop" not in info["shortcuts"]:
                say("ярлык «Агент SYSAI» не создался" + (f": {made['error']}" if made.get("error") else "") +
                    " — окно открывает команда sysai-agent desktop")
            fsutil.write_json(folder(home) / "desktop.json", info)
        elif before.get("shortcuts") is not None:
            info["shortcuts"] = before["shortcuts"]
            fsutil.write_json(folder(home) / "desktop.json", info)
        _cleanup(home, [electron, app])
        if before and before.get("sha256") != info["sha256"]:
            notify(home, UPDATED)
        return info
    finally:
        lock.unlink(missing_ok=True)


def install_quietly(home, client, got: dict) -> None:
    """The background installation of the loop: the outcome goes to the log."""
    try:
        info = install(home, client, got, say=log.info)
        log.info("Окно агента «Агент SYSAI» %s установлено (Electron %s)", info["version"], info["electron"])
    except (DesktopError, ApiError, *NETWORK, zipfile.BadZipFile) as exc:
        log.warning("Окно агента: %s", exc)
    except Exception as exc:  # noqa: BLE001  a thread of the loop must not die silently
        log.exception("Окно агента не установилось: %s", exc)


def signed_offer(home, client, cfg: dict) -> dict:
    """The offer from the signed document, asked now (the signature checked as in sync_once)."""
    from . import signing
    signed = client.json("GET", "/agents/api/config")
    if not signing.verify(signed, cfg["public_key"], cfg.get("key_id", "")):
        raise DesktopError("подпись настроек не сходится с ключом SYSAI, полученным при подключении")
    if signed["doc"].get("agent") != cfg["agent"]:
        raise DesktopError(f"настройки для другого агента ({signed['doc'].get('agent')})")
    return offer(signed["doc"])


def launch(home, files=(), popen=None) -> int:
    """Opens the installed window (the files — shown in it at once); a running window takes them itself."""
    info = installed(home)
    if not info:
        raise DesktopError("окно агента не установлено: sysai-agent desktop --install-only")
    popen = popen or subprocess.Popen
    args = [info["exe"], info["app"], *[str(Path(f).resolve()) for f in files]]
    popen(args, env=_clean_env(home), cwd=info["app"], close_fds=True, stdin=subprocess.DEVNULL,
          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=DETACHED if paths.WINDOWS else 0)
    return 0


def cli(home, files=(), install_only: bool = False) -> int:
    """«sysai-agent desktop [--install-only] [файлы]»: installs the window when it is missing (or asked), opens it."""
    from . import core
    home = Path(home)
    if not paths.WINDOWS:
        raise DesktopError("окно агента «Агент SYSAI» есть только для Windows")
    if install_only or not installed(home):
        cfg = core.load_agent(home)
        client, _ = core.client_for(home, cfg)
        try:
            got = signed_offer(home, client, cfg)
        except ApiError as exc:
            raise DesktopError(f"SYSAI не ответил: {exc}") from exc
        if not got:
            raise DesktopError("SYSAI не предлагает окно агента: обновите SYSAI на сервере")
        try:
            info = install(home, client, got, say=log.info, shortcuts=True if install_only else None)
        except ApiError as exc:
            raise DesktopError(f"окно агента не скачано: {exc}") from exc
        except zipfile.BadZipFile as exc:
            raise DesktopError(f"архив окна агента повреждён: {exc}") from exc
        log.info("Окно агента «Агент SYSAI» %s установлено%s", info["version"],
                 ": ярлык на рабочем столе" if "desktop" in (info.get("shortcuts") or []) else "")
        if install_only:
            return 0
    return launch(home, files)


def summary(home) -> dict:
    info = installed(home)
    return {k: info.get(k, "") for k in ("version", "electron", "installed_at", "app")} if info else {}
