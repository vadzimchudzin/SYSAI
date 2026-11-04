"""The shared storage of the agent network: the files of people and agents in one place.

Sections — «Общее» and one per department, any folders inside. A file keeps every version (a new one only when the
content changed); what is deleted goes to the trash and keeps its bytes. The bytes are in the S3 storage of SYSAI
(storage.py) or, while it is not set up, on the disk of the server (DATA_DIR/net/files). Who may what:
  people — a superuser everything; anyone else reads and writes «Общее» and the sections of the departments of
  his agents;
  agents — only what their access opens (the section «Хранилище» of the profile: what to read, where to write;
  «свой отдел» is the agent's department). Nothing by default, and never more than their person
  (access.cap_by_person: write at most to «Общее» and the own department)."""
import base64
import hashlib
import mimetypes
import pathlib
import re
import shutil
from dataclasses import dataclass, field

from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError

from .. import config, storage
from ..db import Department, SessionLocal, now
from . import access, journal
from .models import NetAgent, NetFile, NetFileVersion

MB = 1024 * 1024
MAX_UPLOAD = 200 * MB          # one file from the browser or through the agent API
MAX_INLINE_WRITE = 5 * MB      # files_write over MCP (text or base64)
MAX_INLINE_READ = 200_000      # files_read gives the text itself up to this size; bigger — a download link
MAX_LIST = 500
COMMON = access.FILES_COMMON
BAD_CHARS = re.compile(r'[\x00-\x1f<>:"|?*]')
EXT_KIND = {}
for _kind, _exts in {"doc": "pdf doc docx odt rtf txt md", "sheet": "xlsx xlsm xls csv ods",
                     "drawing": "dwg dxf dwf dgn", "image": "png jpg jpeg gif webp bmp tif tiff svg heic",
                     "archive": "zip 7z rar tar gz tgz", "code": "py js ts json xml yaml yml ini cfg toml st il project "
                     "projectarchive ps1 bat cmd sh html css sql"}.items():
    EXT_KIND.update({e: _kind for e in _exts.split()})
KIND_RU = {"section": "раздел", "folder": "папка", "doc": "документ", "sheet": "таблица", "drawing": "чертёж",
           "image": "изображение", "archive": "архив", "code": "код", "file": "файл"}
KIND_ICON = {"section": "building", "folder": "folder", "doc": "doc", "sheet": "sheet", "drawing": "drafting",
             "image": "image", "archive": "archive", "code": "code", "file": "file"}
TEXT_MIME = ("text/", "application/json", "application/xml", "application/x-yaml", "application/javascript")


class FilesError(Exception):
    pass


@dataclass
class Rights:
    """What one person or agent may do in the storage: section names in lower case, «*» — every section."""
    read: set = field(default_factory=set)
    write: set = field(default_factory=set)
    who: str = ""
    agent_id: int | None = None
    superuser: bool = False

    @property
    def by_agent(self) -> bool:
        return self.agent_id is not None

    def can_write(self, section: str) -> bool:
        return "*" in self.write or (section or "").lower() in self.write

    def can_read(self, section: str) -> bool:
        return "*" in self.read or (section or "").lower() in self.read or self.can_write(section)

    @property
    def nothing(self) -> bool:
        return not self.read and not self.write


def for_person(user: dict) -> Rights:
    user = user or {}
    if user.get("role") == "superuser":
        return Rights({"*"}, {"*"}, who=user.get("username", ""), superuser=True)
    with SessionLocal() as s:
        names = {name.lower() for (name,) in s.query(Department.name).join(NetAgent, NetAgent.department_id == Department.id)
                 .filter(NetAgent.user_id == user.get("id"), NetAgent.status != "disabled")}
    mine = {COMMON.lower()} | names
    return Rights(set(mine), set(mine), who=user.get("username", ""))


def for_agent(ctx, rules: dict | None = None) -> Rights:
    rules = rules if rules is not None else access.rules_for(ctx.agent_id)
    with SessionLocal() as s:
        dept = s.get(Department, ctx.department_id) if getattr(ctx, "department_id", None) else None
        own = dept.name.lower() if dept else ""

    def expand(items) -> set:
        out = set()
        for item in items or []:
            low = str(item).strip().lower()
            if low == access.FILES_OWN.lower():
                if own:
                    out.add(own)
            elif low:
                out.add(low)
        return out
    f = rules.get("files") or {}
    return Rights(expand(f.get("read")), expand(f.get("write")), who=ctx.name, agent_id=ctx.agent_id)


# ----------------------------------------------------------------------------- paths
def norm_path(path) -> str:
    """«Общее/Проекты/IO.xlsx»: parts without «..», control and Windows-forbidden characters; «\\» is «/»."""
    parts = [p.strip() for p in re.split(r"[\\/]+", str(path or "")) if p.strip()]
    for part in parts:
        if part in (".", "..") or BAD_CHARS.search(part) or part.endswith(".") or len(part) > 255:
            raise FilesError(f"Недопустимое имя: «{part[:80]}»")
    out = "/".join(parts)
    if len(out) > 600:
        raise FilesError("Слишком длинный путь (больше 600 знаков)")
    return out


def sections(s) -> list:
    """«Общее», the active departments and any other top level that already has files (a renamed department)."""
    out = [COMMON] + [d.name for d in s.query(Department).filter(Department.active.is_(True)).order_by(Department.name)]
    seen = {n.lower() for n in out}
    for (folder,) in s.query(NetFile.folder).filter(~NetFile.folder.contains("/")).distinct():
        if folder and folder.lower() not in seen:
            out.append(folder)
            seen.add(folder.lower())
    return out


def _resolve(s, path: str) -> str:
    """The path as stored: the real section name and the existing spelling of every folder (Windows does not tell
    «Проекты» from «проекты», so neither does the storage)."""
    parts = norm_path(path).split("/") if norm_path(path) else []
    if not parts:
        return ""
    known = {n.lower(): n for n in sections(s)}
    head = known.get(parts[0].lower())
    if head is None:
        raise FilesError(f"Нет раздела «{parts[0]}». Разделы: " + ", ".join(known.values()))
    out = [head]
    for part in parts[1:]:
        names = {name.casefold(): name for (name,) in s.query(NetFile.name).filter(NetFile.folder == "/".join(out))}
        out.append(names.get(part.casefold(), part))
    return "/".join(out)


def _section(path: str) -> str:
    return path.split("/")[0] if path else ""


def _need_read(rights: Rights, path: str) -> None:
    if not rights.can_read(_section(path)):
        raise FilesError(f"Нет доступа к разделу «{_section(path)}»" +
                         (" — его открывают в «Доступах» агента (раздел «Хранилище»)" if rights.by_agent else ""))


def _need_write(rights: Rights, path: str) -> None:
    if not rights.can_write(_section(path)):
        raise FilesError(f"Нет права писать в раздел «{_section(path)}»" +
                         (" — его открывают в «Доступах» агента (раздел «Хранилище»)" if rights.by_agent else ""))


def human(size) -> str:
    size = int(size or 0)
    for unit, step in (("ГБ", 1024 ** 3), ("МБ", MB), ("КБ", 1024)):
        if size >= step:
            return f"{size / step:.1f}".replace(".", ",").replace(",0", "") + " " + unit
    return f"{size} Б"


def kind_of(name: str, is_folder: bool = False) -> str:
    if is_folder:
        return "folder"
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return EXT_KIND.get(ext, "file")


def _item(r: NetFile, rights: Rights | None = None) -> dict:
    kind = kind_of(r.name, r.is_folder)
    section = _section(r.path)
    return {"path": r.path, "name": r.name, "folder": r.folder, "section": section, "is_folder": bool(r.is_folder),
            "kind": kind, "kind_ru": KIND_RU[kind], "icon": KIND_ICON[kind], "size": r.size or 0, "size_ru": human(r.size),
            "mime": r.mime or "", "version": r.version or 0, "sha256": r.sha256 or "", "note": r.note or "",
            "created_by": r.created_by or "", "created_at": r.created_at, "updated_at": r.updated_at,
            "updated_by": r.updated_by or "", "by_agent": bool(r.updated_by_agent), "deleted_at": r.deleted_at,
            "deleted_by": r.deleted_by or "", "can_write": rights.can_write(section) if rights else False}


def crumbs(path: str) -> list:
    parts = path.split("/") if path else []
    return [{"name": p, "path": "/".join(parts[:i + 1])} for i, p in enumerate(parts)]


# ----------------------------------------------------------------------------- bytes
def _disk(key: str) -> pathlib.Path:
    return pathlib.Path(config.DATA_DIR) / key


def _key(sha: str) -> str:
    """By the content: the same bytes are kept once, whatever file or version they belong to."""
    return f"net/files/{sha[:2]}/{sha}"


def _store(sha: str, data: bytes | None = None, source=None, mime: str = "") -> str:
    """Puts the bytes away before any database transaction (an upload of 200 MB must not hold the database)."""
    key = _key(sha)
    try:
        if storage.configured():
            if storage.head(key) is None:
                if data is not None:
                    storage.put_bytes(key, data, mime or "application/octet-stream")
                else:
                    storage.put_file(key, source, mime or "application/octet-stream")
            return "s3:" + key
    except storage.StorageError as exc:
        raise FilesError(f"Хранилище S3: {exc}") from None
    target = _disk(key)
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        part = target.with_name(target.name + ".part")
        if data is not None:
            part.write_bytes(data)
        else:
            shutil.copyfile(source, part)
        part.replace(target)
    return "disk:" + key


def _discard(stored: str) -> None:
    where, _, key = (stored or "").partition(":")
    try:
        if where == "s3" and storage.configured():
            storage.delete(key)
        elif where == "disk":
            _disk(key).unlink(missing_ok=True)
    except Exception:  # noqa: BLE001  a leftover copy is not worth an error
        pass


def read_bytes(stored: str, limit: int = 0) -> bytes:
    where, _, key = (stored or "").partition(":")
    try:
        if where == "s3":
            return storage.get_bytes(key, limit)
        with open(_disk(key), "rb") as stream:
            return stream.read(limit) if limit else stream.read()
    except (storage.StorageError, OSError) as exc:
        raise FilesError(f"Файл не прочитать: {exc}") from None


def hash_file(path) -> tuple[int, str]:
    digest, size = hashlib.sha256(), 0
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(256 * 1024), b""):
            digest.update(block)
            size += len(block)
    return size, digest.hexdigest()


# ----------------------------------------------------------------------------- reading
def listing(rights: Rights, folder: str = "", q: str = "", trash: bool = False, limit: int = MAX_LIST) -> dict:
    """A folder (the sections at the top), a search over every readable section, or the trash."""
    q = (q or "").strip()
    with SessionLocal() as s:
        folder = _resolve(s, folder) if folder else ""
        if folder:
            _need_read(rights, folder)
            if "/" in folder and not trash and not q:
                row = s.query(NetFile).filter(NetFile.path == folder).first()
                if row is None or not row.is_folder or row.deleted_at is not None:
                    raise FilesError(f"Нет папки «{folder}»")
        if q or trash:
            query = s.query(NetFile).filter(NetFile.deleted_at.isnot(None) if trash else NetFile.deleted_at.is_(None))
            if folder:
                prefix = folder.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "/%"
                query = query.filter(or_(NetFile.folder == folder, NetFile.folder.like(prefix, escape="\\")))
            needle = q.casefold()
            items = []
            for r in query.order_by(NetFile.deleted_at.desc() if trash else NetFile.updated_at.desc()).limit(20000):
                if not rights.can_read(_section(r.path)):
                    continue
                if needle and needle not in f"{r.path} {r.note or ''}".casefold():
                    continue
                items.append(_item(r, rights))
                if len(items) >= limit:
                    break
            if not trash:
                items.sort(key=lambda i: (not i["is_folder"], i["name"].casefold()))
        elif not folder:
            totals = {}
            for path, size, is_folder in s.query(NetFile.path, NetFile.size, NetFile.is_folder).filter(NetFile.deleted_at.is_(None)):
                if not is_folder:
                    t = totals.setdefault(_section(path).lower(), [0, 0])
                    t[0] += 1
                    t[1] += size or 0
            items = []
            for name in sections(s):
                if not rights.can_read(name):
                    continue
                count, size = totals.get(name.lower(), [0, 0])
                items.append({"path": name, "name": name, "folder": "", "section": name, "is_folder": True, "kind": "section",
                              "kind_ru": KIND_RU["section"], "icon": "folder" if name == COMMON else "building",
                              "files": count, "size": size, "size_ru": human(size), "note": "", "version": 0,
                              "updated_at": None, "updated_by": "", "by_agent": False, "deleted_at": None,
                              "can_write": rights.can_write(name), "common": name == COMMON})
        else:
            rows = (s.query(NetFile).filter(NetFile.folder == folder, NetFile.deleted_at.is_(None))
                    .order_by(NetFile.is_folder.desc(), NetFile.name).limit(limit).all())
            items = sorted((_item(r, rights) for r in rows), key=lambda i: (not i["is_folder"], i["name"].casefold()))
        return {"path": folder, "crumbs": crumbs(folder), "items": items, "q": q, "trash": trash,
                "section": _section(folder), "can_write": bool(folder) and rights.can_write(_section(folder)),
                "can_read_any": rights.superuser or bool(rights.read or rights.write)}


def info(rights: Rights, path: str) -> dict:
    """A file or a folder with its versions (files) or contents count (folders)."""
    with SessionLocal() as s:
        path = _resolve(s, path)
        _need_read(rights, path)
        row = s.query(NetFile).filter(NetFile.path == path).first()
        if row is None:
            raise FilesError(f"Нет «{path}»")
        out = _item(row, rights)
        if row.is_folder:
            prefix = path + "/"
            kids = [r for r in s.query(NetFile.path, NetFile.size, NetFile.is_folder)
                    .filter(NetFile.path.startswith(prefix, autoescape=True), NetFile.deleted_at.is_(None))]
            out["files"] = sum(1 for r in kids if not r.is_folder)
            out["folders"] = sum(1 for r in kids if r.is_folder)
            out["size"] = sum(r.size or 0 for r in kids if not r.is_folder)
            out["size_ru"] = human(out["size"])
            out["versions"] = []
        else:
            out["versions"] = [{"version": v.version, "size": v.size, "size_ru": human(v.size), "sha256": v.sha256,
                                "mime": v.mime, "note": v.note, "at": v.at, "by": v.by, "by_agent": bool(v.by_agent),
                                "current": v.version == row.version}
                               for v in s.query(NetFileVersion).filter(NetFileVersion.file_id == row.id)
                               .order_by(NetFileVersion.version.desc())]
        return out


def blob(rights: Rights, path: str, version: int | None = None) -> dict:
    """Where the bytes of a file (its current or a given version) are: {"stored", "name", "mime", "size", "version"}."""
    with SessionLocal() as s:
        path = _resolve(s, path)
        _need_read(rights, path)
        row = s.query(NetFile).filter(NetFile.path == path, NetFile.is_folder.is_(False)).first()
        if row is None or (row.deleted_at is not None and not rights.can_write(_section(path))):
            raise FilesError(f"Нет файла «{path}»")
        wanted = version or row.version
        v = s.query(NetFileVersion).filter(NetFileVersion.file_id == row.id, NetFileVersion.version == wanted).first()
        if v is None:
            raise FilesError(f"Нет версии {wanted}")
        return {"stored": v.key, "name": row.name, "mime": v.mime or "application/octet-stream", "size": v.size,
                "version": v.version, "sha256": v.sha256, "path": path}


def read_text(rights: Rights, path: str, version: int | None = None) -> dict:
    """For agents: the text itself when the file is small and textual; otherwise only what it is."""
    found = blob(rights, path, version)
    out = {k: found[k] for k in ("path", "name", "mime", "size", "version", "sha256")}
    textual = found["mime"].startswith(TEXT_MIME) or kind_of(found["name"]) in ("code",) or \
        found["name"].lower().endswith((".md", ".txt", ".csv"))
    if textual and found["size"] <= MAX_INLINE_READ:
        data = read_bytes(found["stored"])
        try:
            out["text"] = data.decode("utf-8")
        except UnicodeDecodeError:
            out["text"] = data.decode("cp1251", errors="replace")
    else:
        out["note"] = ("Файл большой или не текстовый: скачайте его по ссылке download (токен агента)"
                       if found["size"] > MAX_INLINE_READ or not textual else "")
    return out


def summary(rights: Rights) -> dict:
    with SessionLocal() as s:
        files = size = trash = 0
        for path, fsize, is_folder, deleted in s.query(NetFile.path, NetFile.size, NetFile.is_folder, NetFile.deleted_at):
            if not rights.can_read(_section(path)):
                continue
            if deleted is not None:
                trash += 0 if is_folder else 1
            elif not is_folder:
                files += 1
                size += fsize or 0
        visible = [n for n in sections(s) if rights.can_read(n)]
    return {"files": files, "size": size, "size_ru": human(size), "sections": len(visible), "trash": trash,
            "s3": storage.configured()}


# ----------------------------------------------------------------------------- writing
def _journal(rights: Rights, action: str, path: str, details=None, session=None) -> None:
    journal.log(action, ("file:" + path)[:160], details, actor=rights.who, actor_kind="agent" if rights.by_agent else "user",
                agent_id=rights.agent_id, session=session)


def _ensure_folders(s, folder: str, rights: Rights, note: str = "") -> None:
    parts = folder.split("/")
    for i in range(2, len(parts) + 1):
        path = "/".join(parts[:i])
        row = s.query(NetFile).filter(NetFile.path == path).first()
        if row is None:
            s.add(NetFile(path=path, folder="/".join(parts[:i - 1]), name=parts[i - 1], is_folder=True,
                          note=(note or "")[:500] if path == folder else "", created_by=rights.who, updated_by=rights.who,
                          updated_by_agent=rights.by_agent))
            s.flush()
        elif not row.is_folder:
            raise FilesError(f"«{path}» — файл, а не папка")
        elif row.deleted_at is not None:
            row.deleted_at, row.deleted_by = None, ""


def mkdir(rights: Rights, path: str, note: str = "") -> dict:
    with SessionLocal() as s:
        path = _resolve(s, path)
        if "/" not in path:
            raise FilesError("Папка создаётся внутри раздела, например «Общее/Проекты»")
        _need_write(rights, path)
        row = s.query(NetFile).filter(NetFile.path == path).first()
        if row is not None and not row.is_folder:
            raise FilesError("Здесь уже есть файл с таким именем")
        if row is not None and row.deleted_at is None:
            return _item(row, rights)
        _ensure_folders(s, path, rights, note)
        _journal(rights, "files.mkdir", path, {"что": (note or "")[:200]} if note else None, session=s)
        s.commit()
        return _item(s.query(NetFile).filter(NetFile.path == path).one(), rights)


def put(rights: Rights, path: str, data: bytes | None = None, source=None, note: str = "", mime: str = "",
        limit: int = MAX_UPLOAD) -> dict:
    """A new version of a file (or the first one). The same bytes again change nothing: {"changed": False}."""
    if (data is None) == (source is None):
        raise ValueError("data or source")
    size, sha = (len(data), hashlib.sha256(data).hexdigest()) if data is not None else hash_file(source)
    if size > limit:
        raise FilesError(f"Файл больше {limit // MB} МБ")
    note = (note or "").strip()[:500]
    with SessionLocal() as s:  # the checks first, in a short read
        path = _resolve(s, path)
        parts = path.split("/")
        if len(parts) < 2:
            raise FilesError("Файл кладётся в раздел или папку, например «Общее/план.xlsx»")
        _need_write(rights, path)
        row = s.query(NetFile).filter(NetFile.path == path).first()
        if row is not None and row.is_folder:
            raise FilesError("Здесь папка с таким именем")
        if row is not None and row.deleted_at is None and row.sha256 == sha:
            return {**_item(row, rights), "changed": False}
    name, folder = parts[-1], "/".join(parts[:-1])
    guessed = (mime or mimetypes.guess_type(name)[0] or "application/octet-stream")[:120]
    stored = _store(sha, data=data, source=source, mime=guessed)
    with SessionLocal() as s:
        row = s.query(NetFile).filter(NetFile.path == path).first()
        if row is not None and row.is_folder:
            raise FilesError("Здесь папка с таким именем")
        if len(parts) > 2:
            _ensure_folders(s, folder, rights)
        if row is None:
            row = NetFile(path=path, folder=folder, name=name, is_folder=False, version=0, note=note, created_by=rights.who)
            s.add(row)
            s.flush()
        restored = row.deleted_at is not None
        version = (row.version or 0) + 1
        s.add(NetFileVersion(file_id=row.id, version=version, size=size, sha256=sha, mime=guessed, key=stored, note=note,
                             by=rights.who, by_agent=rights.by_agent))
        row.version, row.size, row.sha256, row.mime = version, size, sha, guessed
        row.updated_at, row.updated_by, row.updated_by_agent = now(), rights.who, rights.by_agent
        row.deleted_at, row.deleted_by = None, ""
        _journal(rights, "files.put", path, {"версия": version, "размер": human(size), **({"что изменилось": note} if note else {}),
                                             **({"из корзины": True} if restored else {})}, session=s)
        try:
            s.commit()
        except IntegrityError:
            s.rollback()
            raise FilesError("Этот файл как раз меняют — повторите") from None
        return {**_item(row, rights), "changed": True}


def put_inline(rights: Rights, path: str, text=None, b64=None, note: str = "") -> dict:
    """files_write of MCP: the text itself or base64, up to MAX_INLINE_WRITE."""
    if (text is None) == (b64 is None):
        raise FilesError("Нужно одно из двух: text (текст файла) или base64 (двоичный файл)")
    if text is not None:
        data = str(text).encode("utf-8")
    else:
        try:
            data = base64.b64decode(str(b64), validate=True)
        except (ValueError, TypeError):
            raise FilesError("base64 не читается") from None
    if len(data) > MAX_INLINE_WRITE:
        raise FilesError(f"Через MCP — до {MAX_INLINE_WRITE // MB} МБ; больше — PUT /agents/api/files/raw")
    return put(rights, path, data=data, note=note, limit=MAX_INLINE_WRITE)


def remove(rights: Rights, path: str) -> dict:
    """To the trash, with everything inside a folder. The bytes stay: restore() brings it back."""
    with SessionLocal() as s:
        path = _resolve(s, path)
        if "/" not in path:
            raise FilesError("Раздел не удаляется")
        _need_write(rights, path)
        row = s.query(NetFile).filter(NetFile.path == path, NetFile.deleted_at.is_(None)).first()
        if row is None:
            raise FilesError(f"Нет «{path}»")
        moment, count = now(), 0
        rows = [row] + (s.query(NetFile).filter(NetFile.path.startswith(path + "/", autoescape=True),
                                                NetFile.deleted_at.is_(None)).all() if row.is_folder else [])
        for r in rows:
            r.deleted_at, r.deleted_by = moment, rights.who
            count += 0 if r.is_folder else 1
        _journal(rights, "files.delete", path, {"файлов": count}, session=s)
        s.commit()
        return {"path": path, "deleted": True, "files": count}


def restore(rights: Rights, path: str) -> dict:
    with SessionLocal() as s:
        path = _resolve(s, path)
        _need_write(rights, path)
        row = s.query(NetFile).filter(NetFile.path == path).first()
        if row is None or row.deleted_at is None:
            raise FilesError(f"«{path}» нет в корзине")
        moment = row.deleted_at
        rows = [row] + (s.query(NetFile).filter(NetFile.path.startswith(path + "/", autoescape=True),
                                                NetFile.deleted_at == moment).all() if row.is_folder else [])
        for r in rows:
            r.deleted_at, r.deleted_by = None, ""
        if "/" in row.folder:
            _ensure_folders(s, row.folder, rights)
        _journal(rights, "files.restore", path, None, session=s)
        s.commit()
        return {"path": path, "restored": True}


def purge(rights: Rights, path: str) -> dict:
    """Deleting for good, from the trash only and only by a superuser: every version and its bytes."""
    if not rights.superuser:
        raise FilesError("Удаляет навсегда только руководитель (суперпользователь)")
    with SessionLocal() as s:
        path = _resolve(s, path)
        row = s.query(NetFile).filter(NetFile.path == path).first()
        if row is None or row.deleted_at is None:
            raise FilesError("Навсегда удаляется только то, что в корзине")
        rows = [row] + (s.query(NetFile).filter(NetFile.path.startswith(path + "/", autoescape=True)).all()
                        if row.is_folder else [])
        stored = []
        for r in rows:
            for v in s.query(NetFileVersion).filter(NetFileVersion.file_id == r.id):
                stored.append(v.key)
                s.delete(v)
            s.delete(r)
        _journal(rights, "files.purge", path, {"версий": len(stored)}, session=s)
        s.commit()
        kept = {k for (k,) in s.query(NetFileVersion.key).filter(NetFileVersion.key.in_(set(stored)))} if stored else set()
    for key in set(stored) - kept:  # the same bytes may still belong to another file
        _discard(key)
    return {"path": path, "purged": True}
