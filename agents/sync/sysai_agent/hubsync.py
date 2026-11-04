"""SYSAI_Hub on the computer. Skills — in the read-only folder Hermes reads (skills.external_dirs), tools — each in
its own folder under tools/ (a Python tool with requirements.txt gets its own environment). Checked by sha256;
what left the manifest is removed. The skills the agent wrote (skills.create_dir) go to the hub as proposals when
the rules allow; a person in SYSAI reviews them."""
import hashlib
import io
import os
import shutil
import subprocess
import zipfile
from pathlib import Path, PurePosixPath

from . import fsutil, hermes, paths
from .client import ApiError

MAX_UNPACKED = 300 * 1024 * 1024
MAX_PROPOSAL = 20 * 1024 * 1024
JUNK = ("__pycache__/", ".git/", ".venv/", "node_modules/", ".DS_Store", "Thumbs.db", ".pyc")


class HubError(Exception):
    pass


def _safe_name(name: str) -> str:
    name = name.replace("\\", "/")
    parts = PurePosixPath(name).parts
    if not parts or name.startswith("/") or ":" in parts[0] or any(p in ("", ".", "..") for p in parts):
        raise HubError(f"недопустимый путь в архиве: {name}")
    return "/".join(parts)


def unpack(blob: bytes, dest: Path) -> int:
    """Files of the archive into a new folder: safe relative paths only, no links, within the size limit."""
    total = 0
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise HubError(f"ссылка в архиве: {info.filename}")
            rel = _safe_name(info.filename)
            total += info.file_size
            if total > MAX_UNPACKED:
                raise HubError("архив больше 300 МБ в распакованном виде")
            target = dest.joinpath(*rel.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
    return total


def install(target: Path, blob: bytes, readonly: bool) -> None:
    """The new version next to the old one, then swapped: a broken download never leaves half a folder."""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    new, old = target.with_name(target.name + ".new"), target.with_name(target.name + ".old")
    fsutil.rmtree(new)
    fsutil.rmtree(old)
    unpack(blob, new)
    if readonly:
        fsutil.make_readonly(new)
    if target.exists():
        try:
            os.replace(target, old)
        except OSError as exc:
            fsutil.rmtree(new)
            raise HubError(f"папка занята (открыта программой?): {exc}") from exc
    os.replace(new, target)
    fsutil.rmtree(old)


def _download(client, item: dict) -> bytes:
    blob, headers = client.get_bytes(item["url"])
    sha = hashlib.sha256(blob).hexdigest()
    if sha != item.get("sha256") or (headers.get("x-sha256") and headers["x-sha256"] != sha):
        raise HubError("архив пришёл повреждённым (sha256 не сходится)")
    return blob


def _tool_env(home: Path, name: str, folder: Path) -> str:
    """A Python tool with requirements.txt: its own environment by uv; the python to start it with ("" — the
    synchronizer's own)."""
    req = folder / "requirements.txt"
    uv = shutil.which("uv")
    if not req.exists() or not uv:
        return ""
    venv = home / "venvs" / name
    fsutil.rmtree(venv)
    run = dict(capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900,
               creationflags=hermes.NO_WINDOW)
    r = subprocess.run([uv, "venv", "--quiet", str(venv)], **run)
    if r.returncode == 0:
        python = venv / ("Scripts/python.exe" if paths.WINDOWS else "bin/python")
        r = subprocess.run([uv, "pip", "install", "--quiet", "--python", str(python), "-r", str(req)], **run)
        if r.returncode == 0:
            return str(python)
    raise HubError("не поставились зависимости (requirements.txt): " + (r.stderr or r.stdout).strip()[-300:])


def sync(home: Path, client, manifest: dict, state: dict, profile_dir: Path | None = None) -> dict:
    """Brings hub/ and tools/ to the manifest. State: {"skills": {name: {version, sha256}}, "tools": {...}}."""
    home = Path(home)
    report = {"changed": [], "removed": [], "errors": [], "skipped": [], "conflicts": []}
    for part, folder, readonly in (("skills", home / "hub", True), ("tools", home / "tools", True)):
        have = state.setdefault(part, {})
        want = {}
        for item in manifest.get(part) or []:
            spec = item.get("spec") or {}
            if part == "tools" and spec.get("platform", "any") not in ("any", paths.PLATFORM):
                report["skipped"].append(f"{item['name']} (для {spec.get('platform')})")
                continue
            want[item["name"]] = item
        for name, item in want.items():
            mine = have.get(name) or {}
            if mine.get("sha256") == item.get("sha256") and (folder / name).is_dir():
                if part == "tools":
                    mine["spec"] = item.get("spec") or {}
                continue
            try:
                install(folder / name, _download(client, item), readonly)
                entry = {"version": item.get("version", ""), "sha256": item.get("sha256", "")}
                if part == "tools":
                    entry["spec"] = item.get("spec") or {}
                    entry["python"] = _tool_env(home, name, folder / name)
                have[name] = entry
                report["changed"].append(f"{name} {item.get('version', '')}")
            except (HubError, ApiError, OSError, zipfile.BadZipFile, subprocess.SubprocessError) as exc:
                report["errors"].append(f"{name}: {exc}")
        for name in [n for n in have if n not in want]:
            try:
                fsutil.rmtree(folder / name)
                fsutil.rmtree(home / "venvs" / name)
                del have[name]
                report["removed"].append(name)
            except OSError as exc:
                report["errors"].append(f"{name}: не удалить ({exc})")
    if profile_dir is not None:
        report["conflicts"] = sorted(set(state.get("skills") or {}) & hermes.own_skills(profile_dir))
    return report


def _collect(folder: Path) -> dict:
    files, total = {}, 0
    for path in sorted(folder.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(folder).as_posix()
        if any(j in rel + ("/" if path.is_dir() else "") or rel.endswith(j.rstrip("/")) for j in JUNK):
            continue
        data = path.read_bytes()
        total += len(data)
        if total > MAX_PROPOSAL:
            raise HubError("папка больше 20 МБ")
        files[rel] = data
    return files


def _zip(files: dict) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for rel in sorted(files):
            info = zipfile.ZipInfo(rel, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, files[rel])
    return out.getvalue()


def proposals(home: Path) -> list:
    """The folders of skills (SKILL.md) and tools (TOOL.md) the agent wrote, the topmost ones."""
    root = Path(home) / "proposals"
    if not root.is_dir():
        return []
    found = sorted({d.parent for name in ("SKILL.md", "TOOL.md") for d in root.rglob(name)}, key=lambda p: len(p.parts))
    top = []
    for folder in found:
        if not any(folder != t and t in folder.parents for t in top):
            top.append(folder)
    return sorted(top)


def propose(home: Path, client, state: dict, rules: dict) -> list:
    """Sends new and changed proposals; what was sent. Unchanged ones (by content) are not sent again."""
    if not (rules.get("skills") or {}).get("propose"):
        return []
    sent = state.setdefault("proposals", {})
    done = []
    root = Path(home) / "proposals"
    for folder in proposals(home):
        rel = folder.relative_to(root).as_posix()
        try:
            files = _collect(folder)
        except (HubError, OSError) as exc:
            sent[rel] = {"sha": "", "status": "rejected", "message": str(exc)}
            continue
        sha = hashlib.sha256(b"".join(k.encode() + b"\0" + hashlib.sha256(v).digest() for k, v in sorted(files.items()))).hexdigest()
        if (sent.get(rel) or {}).get("sha") == sha:
            continue
        try:
            answer = client.multipart("/agents/api/hub/proposals",
                                      {"changelog": "Агент написал или изменил сам", "why": f"Папка proposals/{rel} на ПК"},
                                      {"file": ("proposal.zip", _zip(files), "application/zip")})
            sent[rel] = {"sha": sha, "status": "review", "name": answer.get("skill", ""), "version": answer.get("version", "")}
            done.append(f"{answer.get('skill', rel)} {answer.get('version', '')}".strip())
        except ApiError as exc:
            if not 400 <= exc.status < 500 or exc.status in (401, 429):
                raise
            sent[rel] = {"sha": sha, "status": "rejected", "message": str(exc)[:300]}
    return done


def summary(state: dict) -> dict:
    """For the pulse: what is on the computer."""
    return {"skills": {k: v.get("version", "") for k, v in (state.get("skills") or {}).items()},
            "tools": {k: v.get("version", "") for k, v in (state.get("tools") or {}).items()},
            "conflicts": list(state.get("conflicts") or [])}
