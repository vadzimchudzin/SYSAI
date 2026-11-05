"""The window «Агент SYSAI» SYSAI gives out: the Electron app from agents/desktop as a zip, built in memory like the
synchronizer's wheel (app/net/installer.py) — the code in the repository is the code on the PCs. The fonts come from
app/static/fonts (the window loads nothing from the network). Electron itself is not built here: the signed document
names the release (version, sha256 of the Windows zip, where to download it) and the synchronizer downloads it,
checks it and keeps it next to the app (sysai_agent/desktop.py) — from SYSAI first: the server keeps a checked copy
(DATA_DIR/net/desktop), downloaded once, for the computers that reach SYSAI but not the internet."""
import hashlib
import io
import json
import logging
import os
import threading
import zipfile
from pathlib import Path

from .. import config

log = logging.getLogger("sysai.net.desktop")

ROOT = Path(__file__).resolve().parents[2]
DESKTOP = ROOT / "agents" / "desktop"
STATIC = ROOT / "app" / "static"
SKIP_DIRS = {"test", "tests", "node_modules", "__pycache__", ".git", "out", "dist"}
SKIP_FILES = {".DS_Store", "Thumbs.db", "README.md"}
ELECTRON_VERSION = "44.10.2"
ELECTRON = {
    "version": ELECTRON_VERSION,
    "file": f"electron-v{ELECTRON_VERSION}-win32-x64.zip",
    "sha256": "7af18e85a1537b832924ae3b0e5146602f2f4f4b0c77c74e4ae0dc251ca338f9",
    "urls": [
        f"https://github.com/electron/electron/releases/download/v{ELECTRON_VERSION}/electron-v{ELECTRON_VERSION}-win32-x64.zip",
        f"https://npmmirror.com/mirrors/electron/{ELECTRON_VERSION}/electron-v{ELECTRON_VERSION}-win32-x64.zip",
    ],
}
ELECTRON_LIMIT = 400 * 1024 * 1024
_cache: dict = {}
_fetching = threading.Lock()


def version() -> str:
    try:
        return str(json.loads((DESKTOP / "package.json").read_text(encoding="utf-8"))["version"])
    except (OSError, ValueError, KeyError):
        return "0.0.0"


def file_name() -> str:
    return f"sysai-desktop-{version()}.zip"


def _fonts() -> list:
    """The fonts of SYSAI's pages for the window: the same files, the stylesheet with addresses next to it."""
    out = []
    css = (STATIC / "fonts.css").read_text(encoding="utf-8").replace("url(/static/fonts/", "url(./")
    for p in sorted((STATIC / "fonts").glob("*.woff2")):
        out.append((f"renderer/fonts/{p.name}", p.read_bytes()))
    out.append(("renderer/fonts/fonts.css", css.encode("utf-8")))
    return out


def _paths() -> list:
    out = []
    for p in sorted(DESKTOP.rglob("*")):
        rel = p.relative_to(DESKTOP)
        if not p.is_file() or any(part in SKIP_DIRS for part in rel.parts) or p.name in SKIP_FILES:
            continue
        if rel.parts[:2] == ("renderer", "fonts"):
            continue
        out.append((rel.as_posix(), p))
    return out


def _sources() -> list:
    return [(name, p.read_bytes()) for name, p in _paths()] + _fonts()


def _stamp() -> tuple:
    """What the files are now, by their names, sizes and times: the zip is rebuilt only when this changes."""
    found = [p for _, p in _paths()] + sorted((STATIC / "fonts").glob("*.woff2")) + [STATIC / "fonts.css"]
    out = []
    for p in found:
        try:
            st = p.stat()
            out.append((str(p), st.st_size, st.st_mtime_ns))
        except OSError:
            out.append((str(p), -1, 0))
    return tuple(out)


def _build(sources: list) -> tuple:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(sources):
            item = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            item.compress_type = zipfile.ZIP_DEFLATED
            item.external_attr = 0o644 << 16
            archive.writestr(item, data)
    blob = out.getvalue()
    return file_name(), blob, hashlib.sha256(blob).hexdigest()


def build() -> tuple:
    """(file name, bytes, sha256), rebuilt only when the sources change."""
    stamp = _stamp()
    if _cache.get("stamp") != stamp:
        _cache.update(stamp=stamp, value=_build(_sources()))
    return _cache["value"]


def offer(auto_update: bool) -> dict:
    """What the signed document offers the computer: the app (its zip by sha256) and the Electron to run it."""
    name, _, sha = build()
    prefetch()  # the computers take Electron from SYSAI first
    return {"version": version(), "file": name, "sha256": sha, "electron": dict(ELECTRON, urls=list(ELECTRON["urls"])),
            "auto_update": bool(auto_update)}


# ------------------------------------------------------------------------------------------------- Electron at SYSAI
def electron_cache() -> Path:
    return config.DATA_DIR / "net" / "desktop"


def electron_path() -> Path | None:
    """SYSAI's checked copy of the Electron release the document names, if it has one yet."""
    path = electron_cache() / ELECTRON["file"]
    return path if path.is_file() else None


def fetch_electron(stream=None) -> Path:
    """Downloads the release from its addresses in order, checks the sha256, keeps it (the older ones go)."""
    import httpx
    stream = stream or httpx.stream
    folder = electron_cache()
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / ELECTRON["file"]
    part = folder / (ELECTRON["file"] + ".part")
    problems = []
    for url in ELECTRON["urls"]:
        digest, size = hashlib.sha256(), 0
        try:
            with stream("GET", url, follow_redirects=True, timeout=60) as resp:
                resp.raise_for_status()
                with open(part, "wb") as handle:
                    for chunk in resp.iter_bytes(1 << 20):
                        size += len(chunk)
                        if size > ELECTRON_LIMIT:
                            raise ValueError("слишком большой файл")
                        digest.update(chunk)
                        handle.write(chunk)
            if digest.hexdigest() != ELECTRON["sha256"]:
                raise ValueError("sha256 не совпал")
            os.replace(part, target)
        except (httpx.HTTPError, OSError, ValueError) as exc:
            problems.append(f"{url.split('/')[2]}: {exc}")
            part.unlink(missing_ok=True)
            continue
        for old in folder.glob("electron-*.zip"):
            if old.name != target.name:
                old.unlink(missing_ok=True)
        log.info("Electron %s для окна агента скачан (%s МБ)", ELECTRON_VERSION, size // (1 << 20))
        return target
    raise RuntimeError("Electron для окна агента не скачан: " + "; ".join(problems))


def _fetch_in_background() -> None:
    try:
        fetch_electron()
    except Exception as exc:  # noqa: BLE001  the computers download it from the release addresses then
        log.warning("%s", exc)
    finally:
        _fetching.release()


def prefetch() -> bool:
    """Starts downloading SYSAI's copy in the background, once at a time; False — there is one or it is coming."""
    if electron_path() or config.TESTING or not _fetching.acquire(blocking=False):
        return False
    threading.Thread(target=_fetch_in_background, name="sysai-electron", daemon=True).start()
    return True
