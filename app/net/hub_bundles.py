"""Downloadable starter packages (EasyBuilder Pro, PLC) shipped with the server.

These are separate from the Hub database: downloading never publishes or changes a version.
"""
from pathlib import Path
import hashlib

from . import hub

ROOT = Path(__file__).resolve().parents[2] / "hub" / "seed"
BUNDLES = {
    "ebpro-cmtp": "EasyBuilder Pro — инструмент MCP",
    "ebpro-mnemoscheme": "EasyBuilder Pro — умение сборки мнемосхем",
    "ebpro-gui": "EasyBuilder Pro — работа в программе (Windows)",
    "plc-kit": "ПЛК Machine Expert (ST) — инструмент MCP",
    "plc-project": "Проект под ключ: ПЛК и экраны — умение",
}


def package(name: str):
    if name not in BUNDLES:
        return None
    root = ROOT / name
    if not root.is_dir() or root.is_symlink():
        return None
    files = {}
    for path in root.rglob("*"):
        rel = path.relative_to(root)
        if any(part.startswith(".") or part == "__pycache__" for part in rel.parts) or path.suffix == ".pyc":
            continue
        if path.is_symlink():
            raise hub.HubError("Ссылки в пакете не допускаются")
        if path.is_file():
            files[rel.as_posix()] = path.read_bytes()
    files = hub.safe_files(files)
    doc = hub.doc_of(files)
    meta, checks = hub.check(files)
    if meta.get("name") != name or checks["errors"] or not hub.VERSION_RE.fullmatch(str(meta.get("version") or "")):
        raise hub.HubError("Некорректный встроенный пакет")
    blob = hub.build_zip(files)
    # Apply exactly the same archive limits as a manual Hub upload.
    hub.read_zip(blob)
    return {"name": name, "title": BUNDLES[name], "version": meta["version"], "blob": blob,
            "sha256": hashlib.sha256(blob).hexdigest(), "doc": doc}


def listing():
    result = []
    for name in BUNDLES:
        try:
            item = package(name)
        except (OSError, hub.HubError):
            continue
        if item:
            result.append({key: item[key] for key in ("name", "title", "version")})
    return result
