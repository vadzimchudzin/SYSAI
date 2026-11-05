"""The synchronizer updates itself, but only to what SYSAI signed: the signed document carries the version SYSAI
gives out and the sha256 of its wheel (`synchronizer`, app/net/access.py), and the setting «обновлять сами» of the
network. A newer version is downloaded, checked against that sha256 and installed from the checked file by a helper
that outlives the loop: on Windows a running program holds its files, so the loop leaves, the helper waits for it,
installs the wheel with uv (as the installer did) and starts the loop again. Nothing is stopped by force: files held
by a running hub MCP server or a hook make the install fail — it is tried again after RETRY, the old version keeps
working. Hermes need not restart: it calls the same launcher, and what the new version writes to the profile restarts
it as any change of the settings does."""
import hashlib
import os
import re
import subprocess
import time
from pathlib import Path

from . import __version__, fsutil, paths

RETRY = 6 * 3600
SCRIPT = "update.ps1"
FOLDER = "updates"
DETACHED = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW


class UpdateError(Exception):
    pass


def _parts(version: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", str(version or ""))[:4])


def newer(candidate: str, current: str = __version__) -> bool:
    return bool(_parts(candidate)) and _parts(candidate) > _parts(current)


def wheel_name(version: str) -> str:
    return f"sysai_agent-{version}-py3-none-any.whl"


def offer(doc: dict) -> dict:
    """What the signed document offers: {"version", "sha256", "auto_update"} or {}."""
    got = doc.get("synchronizer") if isinstance(doc, dict) else None
    if not isinstance(got, dict) or not re.fullmatch(r"[0-9a-f]{64}", str(got.get("sha256") or "")):
        return {}
    return {"version": str(got.get("version") or ""), "sha256": str(got["sha256"]),
            "auto_update": bool(got.get("auto_update", True))}


def due(state: dict, now: float | None = None) -> dict:
    """The offer to install now ({} — nothing): automatic updates on, newer than this one, not tried within RETRY."""
    got = state.get("offer") if isinstance(state.get("offer"), dict) else {}
    if not got.get("auto_update") or not newer(got.get("version", "")):
        return {}
    now = time.time() if now is None else now
    if state.get("update_to") == got["version"] and now - float(state.get("update_at") or 0) < RETRY:
        return {}
    return got


def fetch(home: Path, client, got: dict) -> Path:
    """The wheel of the offer, downloaded and checked against the signed sha256; kept in «updates»."""
    name = wheel_name(got["version"])
    data, _ = client.get_bytes(f"/agents/api/sync/{name}")
    if hashlib.sha256(data).hexdigest() != got["sha256"]:
        raise UpdateError(f"обновление {got['version']} не совпало с подписанным SYSAI (sha256): не установлено")
    folder = Path(home) / FOLDER
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.glob("*.whl"):
        if old.name != name:
            old.unlink(missing_ok=True)
    path = folder / name
    fsutil.atomic_write(path, data)
    return path


def _ps(text) -> str:
    return "'" + str(text).replace("'", "''") + "'"


def script(home: Path, wheel: Path, version: str, pid: int) -> str:
    """The helper in PowerShell 5.1: wait for the loop, install the checked wheel, start the loop again."""
    home = Path(home)
    return f"""$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'
$log = {_ps(home / 'logs' / 'update.log')}
function Log([string]$t) {{ Add-Content -Path $log -Value ((Get-Date -Format 's') + ' ' + $t) -Encoding UTF8 }}
Log {_ps(f'обновление синхронизатора {__version__} -> {version}')}
try {{ Wait-Process -Id {int(pid)} -Timeout 120 -ErrorAction SilentlyContinue }} catch {{}}
$env:Path = @([Environment]::GetEnvironmentVariable('Path', 'Machine'), [Environment]::GetEnvironmentVariable('Path', 'User'),
              (Join-Path $env:USERPROFILE '.local\\bin')) -join ';'
$uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
if (-not $uv) {{ Log 'нет uv: обновление отложено' }}
else {{
  $ok = $false
  for ($i = 0; ($i -lt 3) -and (-not $ok); $i++) {{
    & $uv tool install --force --python 3.12 {_ps(wheel)} *>> $log
    $ok = ($LASTEXITCODE -eq 0)
    if (-not $ok) {{ Start-Sleep -Seconds 20 }}
  }}
  if ($ok) {{ Log {_ps('установлен ' + version)} }}
  else {{ Log 'не установился (файлы заняты: идёт задача Hermes с инструментом хаба?) - повторю позже, работает прежняя версия' }}
}}
$runner = Get-Command 'sysai-agentw' -ErrorAction SilentlyContinue
if (-not $runner) {{ $runner = Get-Command 'sysai-agent' -ErrorAction SilentlyContinue }}
if ($runner) {{ Start-Process -FilePath $runner.Source -ArgumentList 'run' -WindowStyle Hidden; Log 'синхронизатор запущен' }}
"""


def start(home: Path, wheel: Path, version: str, state: dict, popen=None) -> bool:
    """Hands the checked wheel to the helper; True — the loop must leave now (the helper waits for it)."""
    popen = popen or subprocess.Popen
    if not paths.WINDOWS:
        return False  # elsewhere the synchronizer is updated by whoever installed it
    home = Path(home)
    (home / "logs").mkdir(parents=True, exist_ok=True)
    path = home / SCRIPT
    fsutil.atomic_write(path, "\ufeff" + script(home, wheel, version, os.getpid()))  # BOM: PowerShell 5.1 reads UTF-8
    state.update(update_to=version, update_at=time.time())  # not again before RETRY, whatever the helper does
    fsutil.write_json(home / "state.json", state)
    try:
        popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File", str(path)],
              creationflags=DETACHED, close_fds=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
              stderr=subprocess.DEVNULL, cwd=str(home))
    except OSError:
        return False
    return True
