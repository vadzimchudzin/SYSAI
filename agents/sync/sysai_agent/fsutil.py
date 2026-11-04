"""Files written whole or not at all; folders made read-only and removed even when read-only."""
import json
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path


def atomic_write(path: Path, data, private: bool = False) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = data.encode("utf-8") if isinstance(data, str) else data
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix="." + path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        if private and os.name != "nt":
            os.chmod(tmp, 0o600)
        if path.exists():
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD)  # a read-only file cannot be replaced on Windows
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_json(path: Path, default=None):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def write_json(path: Path, obj, private: bool = False) -> None:
    atomic_write(path, json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True), private=private)


def read_text(path: Path) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def make_readonly(folder: Path) -> None:
    for root, _, names in os.walk(folder):
        for name in names:
            p = os.path.join(root, name)
            os.chmod(p, stat.S_IMODE(os.stat(p).st_mode) & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))


def rmtree(folder: Path) -> None:
    def again(func, p, _exc):
        os.chmod(p, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
        func(p)
    if Path(folder).exists():
        if sys.version_info >= (3, 12):
            shutil.rmtree(folder, onexc=again)
        else:
            shutil.rmtree(folder, onerror=again)
