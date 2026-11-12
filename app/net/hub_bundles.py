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
    "machine-expert": "Machine Expert: проект .project, компиляция, контроллер (Windows) — инструмент MCP",
    "pic-index": "Библиотека картинок оборудования: индекс и поиск — инструмент MCP",
    "doc-kit": "Документация в Word по образцу — инструмент MCP",
    "drawing-kit": "Схемы и чертежи в PDF: устройства, механизмы, каналы — инструмент MCP",
    "plc-project": "Проект под ключ с нуля: ПЛК, Machine Expert, экраны — умение",
    "project-docs": "Документация проекта: руководство и паспорт — умение",
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


# What a package means for this Hub, most pressing first: (state, label, colour of the badge)
STATES = {
    "update": ("Новая версия", "amber"),
    "new": ("Нет в Hub", "blue"),
    "pending": ("Ждёт публикации", "amber"),
    "current": ("Установлена", "green"),
    "older": ("В Hub новее", "gray"),
}
ORDER = ("update", "new", "pending", "current", "older")


def state_of(version: str, known: dict | None) -> dict:
    """The package against the Hub: nothing there yet, a newer version than the Hub has, this very version waiting for
    publication, already published, or the Hub is ahead."""
    if not known or not known["versions"]:
        return {"state": "new", "hub": ""}
    current, status = known["current"], known["versions"].get(version)
    if status in ("draft", "review"):
        return {"state": "pending", "hub": current, "status": status}
    if current == version:
        return {"state": "current", "hub": current}
    if current and hub.vkey(current) > hub.vkey(version):
        return {"state": "older", "hub": current}
    return {"state": "update" if current else "new", "hub": current}


def install(name: str, user: dict) -> dict:
    """Put the package into the Hub in one step: upload it for review and, when this person may publish it, publish.
    -> {'name', 'version', 'published', 'note'}."""
    item = package(name)
    if item is None:
        raise hub.HubError("Нет такого пакета")
    state = state_of(item["version"], hub.version_states().get(name))
    version = item["version"]
    if state["state"] == "current":
        raise hub.HubError(f"{item['title']} {version} уже установлена")
    if state["state"] == "older":
        raise hub.HubError(f"В Hub версия новее ({state['hub']}): пакет {version} не ставится поверх неё")
    if state["state"] != "pending":
        _, version = hub.submit(hub.read_zip(item["blob"]), user=user, review=True,
                                changelog=f"Готовый пакет SYSAI {item['version']}")
    try:
        hub.publish(name, version, user, note="Готовый пакет SYSAI")
    except hub.HubError as exc:
        return {"name": name, "version": version, "published": False, "note": str(exc)}
    return {"name": name, "version": version, "published": True, "note": ""}


def listing():
    result = []
    try:
        known = hub.version_states()
    except Exception:  # noqa: BLE001 — the list must still show when the Hub tables cannot be read
        known = {}
    for name in BUNDLES:
        try:
            item = package(name)
        except (OSError, hub.HubError):
            continue
        if item:
            row = {key: item[key] for key in ("name", "title", "version")}
            row.update(state_of(item["version"], known.get(name)))
            row["label"], row["colour"] = STATES[row["state"]]
            row["kind"] = "умение" if item["doc"] == hub.SKILL_DOC else "инструмент"
            result.append(row)
    result.sort(key=lambda r: ORDER.index(r["state"]))        # stable: the order of BUNDLES inside a state
    return result
