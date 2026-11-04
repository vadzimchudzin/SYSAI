"""What an employee's computer downloads from SYSAI (ОПО-6.2): the installer script and the synchronizer as a wheel,
built in memory from agents/sync — no build tools on the server, the code in the repository is the code on the PCs."""
import base64
import hashlib
import io
import re
import zipfile
from pathlib import Path

AGENTS = Path(__file__).resolve().parents[2] / "agents"
SYNC = AGENTS / "sync"
SCRIPT = AGENTS / "installer" / "install.ps1"
PACKAGE = "sysai_agent"
REQUIRES = ("cryptography>=41", "PyYAML>=6", 'keyring>=24; sys_platform == "win32"')
_cache: dict = {}


def version() -> str:
    text = (SYNC / PACKAGE / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.M)
    return m.group(1) if m else "0.0.0"


def wheel_name() -> str:
    return f"{PACKAGE}-{version()}-py3-none-any.whl"


def _sources() -> list:
    return [(p.relative_to(SYNC).as_posix(), p.read_bytes()) for p in sorted((SYNC / PACKAGE).rglob("*.py"))
            if "__pycache__" not in p.parts]


def _digest(data: bytes) -> str:
    return "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()


def _build(sources: list) -> tuple:
    ver = version()
    info = f"{PACKAGE}-{ver}.dist-info"
    meta = (f"Metadata-Version: 2.1\nName: sysai-agent\nVersion: {ver}\n"
            "Summary: SYSAI agent synchronizer: rules, SOUL.md and SYSAI_Hub on the employee's computer\n"
            "Requires-Python: >=3.11\n" + "".join(f"Requires-Dist: {r}\n" for r in REQUIRES))
    files = list(sources) + [
        (f"{info}/METADATA", meta.encode()),
        (f"{info}/WHEEL", b"Wheel-Version: 1.0\nGenerator: sysai\nRoot-Is-Purelib: true\nTag: py3-none-any\n"),
        (f"{info}/entry_points.txt", b"[console_scripts]\nsysai-agent = sysai_agent.cli:main\n\n"
                                     b"[gui_scripts]\nsysai-agentw = sysai_agent.cli:main_windowless\n"),
        (f"{info}/top_level.txt", b"sysai_agent\n"),
    ]
    record = "".join(f"{name},{_digest(data)},{len(data)}\n" for name, data in files) + f"{info}/RECORD,,\n"
    files.append((f"{info}/RECORD", record.encode()))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in files:
            item = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            item.compress_type = zipfile.ZIP_DEFLATED
            item.external_attr = 0o644 << 16
            archive.writestr(item, data)
    blob = out.getvalue()
    return wheel_name(), blob, hashlib.sha256(blob).hexdigest()


def wheel() -> tuple:
    """(file name, bytes, sha256), rebuilt only when the sources change."""
    sources = _sources()
    key = hashlib.sha256(b"".join(n.encode() + b"\0" + d for n, d in sources)).hexdigest()
    if _cache.get("key") != key:
        _cache.update(key=key, value=_build(sources))
    return _cache["value"]


def script(server: str) -> str:
    server = server.rstrip("/")
    name = wheel()[0]
    return (SCRIPT.read_text(encoding="utf-8").replace("__SYSAI_SERVER__", server)
            .replace("__SYSAI_WHEEL__", f"{server}/agents/api/sync/{name}").replace("__SYSAI_VERSION__", version()))


def one_liner(server: str, code: str) -> str:
    return f"$env:SYSAI_CODE='{code}'; irm {server.rstrip('/')}/agents/api/install.ps1 | iex"
