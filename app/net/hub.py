"""SYSAI_Hub — one hub of skills for every agent of the network (ОПО-3.x).

A skill is an open SKILL.md folder (name, description, version; «Когда применять», «Как делать», «Ошибки»,
«Проверка») with references/, scripts/, assets/ — Hermes, OpenClaw, Claude Code and Codex read it as is. Versions go
draft → review → published; a published version never changes (a fix is a new version, a rollback picks an older
one). Agents get the published skills allowed to them (name, version, sha256) and put them into a read-only folder;
what an agent learned itself comes back as a proposal and becomes everyone's after the owner approves it."""
import difflib
import hashlib
import io
import json
import re
import zipfile
from urllib.parse import urlparse

from .. import config, settings_store
from ..db import Department, SessionLocal, User, now
from . import journal
from .models import NetAgent, NetSkill, NetSkillUsage, NetSkillVersion

NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
MAX_ZIP = 2 * 1024 * 1024
MAX_UNPACKED = 8 * 1024 * 1024
MAX_FILES = 200
MAX_DESCRIPTION = 1024
SECTIONS = ("Когда применять", "Как делать", "Ошибки", "Проверка")
TOP_LEVEL = ("SKILL.md", "references", "scripts", "assets", "README.md", "LICENSE", "LICENSE.txt")
JUNK = ("__MACOSX/", ".DS_Store", "Thumbs.db", ".git/", "desktop.ini")
STATUS_RU = {"draft": ("черновик", "gray"), "review": ("на проверке", "amber"), "published": ("опубликован", "green"),
             "retired": ("снят", "red"), "rejected": ("отклонён", "red")}
DEFAULT_HOSTS = ["sysat.by", "se.com", "schneider-electric.com", "localhost", "127.0.0.1"]

DANGER = [
    (r"\brm\s+-[a-z]*r[a-z]*f?[a-z]*\s+(/|~|\$HOME|\*)(\s|$)", "rm -rf по корню, домашней папке или всему"),
    (r"\bformat(\.com)?\s+[a-z]:", "форматирование диска"),
    (r"\b(del|erase)\s+(/[a-z]\s+)*/[sq]\b", "удаление с /s или /q"),
    (r"\b(rd|rmdir)\s+(/[a-z]\s+)*/s\b", "удаление папок с /s"),
    (r"Remove-Item\b[^\n]*-Recurse[^\n]*-Force|Remove-Item\b[^\n]*-Force[^\n]*-Recurse", "Remove-Item -Recurse -Force"),
    (r"\breg(\.exe)?\s+delete\b", "удаление из реестра"),
    (r"\b(bcdedit|diskpart|mkfs(\.\w+)?)\b|\bvssadmin\s+delete\b|\bcipher\s+/w\b|\bdd\s+if=", "команды диска и загрузки"),
    (r"\bshutdown(\.exe)?\s+[/-][srfp]\b|\b(Stop|Restart)-Computer\b", "выключение компьютера"),
    (r"\b(curl|wget)\b[^\n|]*\|\s*(sudo\s+)?(ba|z|da)?sh\b", "скачать и сразу выполнить"),
    (r"\b(iwr|irm|Invoke-WebRequest|Invoke-RestMethod)\b[^\n|]*\|\s*(iex|Invoke-Expression)\b", "скачать и сразу выполнить"),
    (r"Set-MpPreference\b[^\n]*-Disable", "отключение защитника Windows"),
    (r"\bnet\s+user\s+\S+\s+\S+\s+/add\b", "создание пользователя Windows"),
    (r":\(\)\s*\{\s*:\|:&\s*\};:", "fork-бомба"),
]
SECRETS = [
    (r"\bsk-or-v1-[0-9a-f]{20,}", "ключ OpenRouter"),
    (r"\bsk-(proj-|ant-)?[A-Za-z0-9_-]{24,}", "ключ API"),
    (r"\bAKIA[0-9A-Z]{16}\b", "ключ AWS"),
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "закрытый ключ"),
    (r"\bgh[pousr]_[A-Za-z0-9]{30,}", "токен GitHub"),
    (r"\bxox[abprs]-[A-Za-z0-9-]{10,}", "токен Slack"),
    (r"\bsya_[A-Za-z0-9_-]{20,}", "токен агента SYSAI"),
    (r"\b\d{8,10}:AA[A-Za-z0-9_-]{30,}", "токен Telegram-бота"),
    (r"(?i)\b(password|passwd|пароль)\s*[:=]\s*['\"][^'\"\s]{6,}['\"]", "пароль в тексте"),
]
_DANGER = [(re.compile(p, re.I), what) for p, what in DANGER]
_SECRETS = [(re.compile(p), what) for p, what in SECRETS]
URL_RE = re.compile(r"\bhttps?://([A-Za-z0-9.-]+)")


class HubError(Exception):
    pass


# ----------------------------------------------------------------------------- SKILL.md
def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        try:
            return json.loads(value)
        except ValueError:
            return value[1:-1]
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1].replace("''", "'")
    return value


def parse_skill_md(text: str) -> tuple[dict, str]:
    """The frontmatter (top-level keys; «|» and «>» blocks; nested maps kept as text) and the body."""
    text = (text or "").lstrip("\ufeff")
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return {}, text
    meta, key, block, style = {}, None, [], ""

    def close():
        if key is not None and style:
            meta[key] = ("\n" if style == "|" else " ").join(line.strip() for line in block).strip()
    for line in lines[1:end]:
        if line[:1] in (" ", "\t") or not line.strip():
            if key is not None:
                block.append(line)
            continue
        close()
        key, block, style = None, [], ""
        m = re.match(r"^([A-Za-z0-9_-]+)\s*:\s*(.*)$", line)
        if not m:
            continue
        key, value = m.group(1), m.group(2).strip()
        if value in ("|", ">", "|-", ">-", "|+", ">+"):
            style = value[0]
        elif value == "":
            style = "|"
        else:
            meta[key] = _unquote(value)
            key = None
    close()
    return meta, "\n".join(lines[end + 1:]).lstrip("\n")


def set_version(text: str, version: str) -> str:
    """Write the version the hub gave into the frontmatter."""
    meta, _ = parse_skill_md(text)
    lines = (text or "").lstrip("\ufeff").splitlines()
    if not meta or not lines or lines[0].strip() != "---":
        return text
    end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    for i in range(1, end):
        if re.match(r"^version\s*:", lines[i]):
            lines[i] = f"version: {version}"
            break
    else:
        lines.insert(end, f"version: {version}")
    return "\n".join(lines) + ("\n" if text.endswith("\n") else "")


def title_of(meta: dict, body: str, name: str) -> str:
    if meta.get("title"):
        return str(meta["title"])[:200]
    m = re.search(r"^#\s+(.+)$", body or "", re.M)
    return (m.group(1).strip() if m else name)[:200]


def template(name: str = "my-skill") -> str:
    return (f"---\nname: {name}\ndescription: Что делает навык и когда его применять — одна-две фразы.\nversion: 1.0.0\n---\n"
            "# Название навыка\n\n## Когда применять\n\n## Как делать\n\n## Ошибки\n\n## Проверка\n")


# ----------------------------------------------------------------------------- archives
def vkey(version: str) -> tuple:
    try:
        return tuple(int(p) for p in str(version).split("."))
    except ValueError:
        return (0, 0, 0)


def bump(version: str) -> str:
    a, b, c = (list(vkey(version)) + [0, 0, 0])[:3]
    return f"{a}.{b}.{c + 1}"


def read_zip(data: bytes) -> dict:
    """Files of an uploaded skill folder: safe relative paths only, no links, within the limits. A single top folder
    (the skill's own name) is taken off."""
    if len(data) > MAX_ZIP:
        raise HubError(f"Архив больше {MAX_ZIP // (1024 * 1024)} МБ")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise HubError("Это не zip-архив") from exc
    files, total = {}, 0
    for info in archive.infolist():
        name = info.filename.replace("\\", "/")
        if info.is_dir() or any(j in name or name.endswith(j.rstrip("/")) for j in JUNK):
            continue
        if (info.external_attr >> 16) & 0o170000 == 0o120000:
            raise HubError(f"Ссылки в архиве не допускаются: {name}")
        parts = name.split("/")
        if name.startswith("/") or ":" in parts[0] or any(p in ("", ".", "..") for p in parts):
            raise HubError(f"Недопустимый путь в архиве: {name}")
        total += info.file_size
        if total > MAX_UNPACKED or len(files) >= MAX_FILES:
            raise HubError(f"Слишком большой навык: не больше {MAX_FILES} файлов и {MAX_UNPACKED // (1024 * 1024)} МБ")
        files[name] = archive.read(info)
    if "SKILL.md" not in files:
        tops = {p.split("/")[0] for p in files}
        if len(tops) == 1 and all("/" in p for p in files):
            top = tops.pop() + "/"
            files = {p[len(top):]: v for p, v in files.items()}
    if "SKILL.md" not in files:
        raise HubError("В архиве нет SKILL.md (в корне или в единственной папке навыка)")
    return files


def safe_files(files: dict) -> dict:
    """Files given as a mapping (the editor, MCP): the same path rules and limits as an archive."""
    if not isinstance(files, dict):
        raise HubError("Файлы навыка: объект {путь: текст}")
    out, total = {}, 0
    for path, content in files.items():
        path = str(path).replace("\\", "/").strip()
        parts = path.split("/")
        if not path or path.startswith("/") or ":" in parts[0] or any(p in ("", ".", "..") for p in parts):
            raise HubError(f"Недопустимый путь: {path}")
        data = content.encode("utf-8") if isinstance(content, str) else bytes(content)
        total += len(data)
        if total > MAX_UNPACKED or len(out) >= MAX_FILES:
            raise HubError(f"Слишком большой навык: не больше {MAX_FILES} файлов и {MAX_UNPACKED // (1024 * 1024)} МБ")
        out[path] = data
    return out


def build_zip(files: dict) -> bytes:
    """The same files always give the same bytes (and sha256): sorted, fixed dates."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(files):
            info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, files[path])
    return out.getvalue()


def unpack(blob: bytes) -> dict:
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        return {i.filename: archive.read(i) for i in archive.infolist() if not i.is_dir()}


def _text(data: bytes):
    if b"\x00" in data[:4096]:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1251", errors="replace")


def allowed_hosts() -> list:
    hosts = list(settings_store.get("agents_hub_hosts") or DEFAULT_HOSTS)
    own = urlparse(config.PUBLIC_URL or "").hostname
    return hosts + ([own] if own else [])


def _host_ok(host: str, hosts: list) -> bool:
    host = host.lower().rstrip(".")
    return any(host == h.lower() or host.endswith("." + h.lower()) for h in hosts)


def check(files: dict) -> tuple[dict, dict]:
    """(frontmatter, report). Errors block publishing (ОПО-3.8): a bad name or description, dangerous commands,
    secrets, unknown sites in scripts. Warnings are shown to the reviewer."""
    errors, warnings = [], []
    text = _text(files.get("SKILL.md", b"")) or ""
    meta, body = parse_skill_md(text)
    name = str(meta.get("name") or "")
    if not meta:
        errors.append({"path": "SKILL.md", "what": "нет заголовка --- name / description --- в начале SKILL.md"})
    if not NAME_RE.fullmatch(name) or len(name) > 64:
        errors.append({"path": "SKILL.md", "what": "name: латиница в нижнем регистре, цифры и дефисы, до 64 знаков"})
    description = str(meta.get("description") or "").strip()
    if not description:
        errors.append({"path": "SKILL.md", "what": "нет description: агент не поймёт, когда применять навык"})
    elif len(description) > MAX_DESCRIPTION:
        errors.append({"path": "SKILL.md", "what": f"description длиннее {MAX_DESCRIPTION} знаков"})
    if meta.get("version") and not VERSION_RE.fullmatch(str(meta["version"])):
        errors.append({"path": "SKILL.md", "what": "version: три числа через точку, например 1.0.0"})
    for section in SECTIONS:
        if not re.search(r"^#{1,4}\s*" + re.escape(section), body, re.M | re.I):
            warnings.append({"path": "SKILL.md", "what": f"нет раздела «{section}»"})
    hosts = allowed_hosts()
    for path in sorted(files):
        top = path.split("/")[0]
        if top not in TOP_LEVEL:
            warnings.append({"path": path, "what": "вне SKILL.md, references/, scripts/, assets/"})
        content = _text(files[path])
        if content is None:
            if top == "scripts":
                warnings.append({"path": path, "what": "двоичный файл в scripts/ — не проверить"})
            continue
        for number, line in enumerate(content.splitlines(), 1):
            for pattern, what in _DANGER:
                if pattern.search(line):
                    errors.append({"path": path, "line": number, "what": "опасная команда: " + what})
            for pattern, what in _SECRETS:
                if pattern.search(line):
                    errors.append({"path": path, "line": number, "what": "секрет в навыке: " + what})
            for host in URL_RE.findall(line):
                if not _host_ok(host, hosts):
                    item = {"path": path, "line": number, "what": f"внешний адрес {host}"}
                    (errors if top == "scripts" else warnings).append(item)
    return meta, {"errors": errors[:100], "warnings": warnings[:100]}


# ----------------------------------------------------------------------------- lifecycle
def can_approve(user: dict | None, skill: NetSkill) -> bool:
    """The owner of the skill or a superuser (for now the head of the owning department is a superuser)."""
    return bool(user) and (user.get("role") == "superuser" or (skill.owner_user_id is not None and skill.owner_user_id == user.get("id")))


def _refresh_status(s, skill: NetSkill) -> None:
    if skill.status == "retired":
        return
    if skill.current_version:
        skill.status = "published"
    elif s.query(NetSkillVersion).filter(NetSkillVersion.skill_id == skill.id, NetSkillVersion.status == "review").count():
        skill.status = "review"
    else:
        skill.status = "draft"


def submit(files: dict, user: dict | None = None, agent=None, changelog: str = "", why: str = "",
           review: bool = False, department_id=None) -> tuple[str, str]:
    """A new version from a person (draft, or «на проверку») or from an agent (always a proposal for review).
    Returns (name, version)."""
    files = safe_files(files)
    if "SKILL.md" not in files:
        raise HubError("Нет SKILL.md")
    meta, _ = parse_skill_md(_text(files["SKILL.md"]) or "")
    name = str(meta.get("name") or "")
    if not NAME_RE.fullmatch(name) or len(name) > 64:
        raise HubError("name в SKILL.md: латиница в нижнем регистре, цифры и дефисы, например plc-io-from-excel")
    if not str(meta.get("description") or "").strip():
        raise HubError("Нужен description в SKILL.md")
    by = agent.name if agent is not None else (user or {}).get("username", "")
    with SessionLocal() as s:
        skill = s.query(NetSkill).filter(NetSkill.name == name).first()
        if skill is None:
            dept = department_id if department_id is not None else (agent.department_id if agent is not None else None)
            owner = agent.user_id if agent is not None else (user or {}).get("id")
            skill = NetSkill(name=name, department_id=dept, owner_user_id=owner, audience={"all": True}, status="draft")
            s.add(skill)
            s.flush()
        elif skill.status == "retired" and agent is not None:
            raise HubError("Навык снят с хаба: предложения к нему не принимаются")
        versions = [v.version for v in s.query(NetSkillVersion.version).filter(NetSkillVersion.skill_id == skill.id)]
        latest = max(versions, key=vkey) if versions else ""
        wanted = str(meta.get("version") or "")
        wanted = wanted if VERSION_RE.fullmatch(wanted) else ""
        if wanted and wanted in versions and agent is None:
            raise HubError(f"Версия {wanted} уже есть: поднимите version в SKILL.md (например {bump(latest)})")
        if not wanted or wanted in versions or (latest and vkey(wanted) <= vkey(latest)):
            wanted = bump(latest) if latest else (wanted or "1.0.0")
        skill_md = set_version(_text(files["SKILL.md"]) or "", wanted)
        files["SKILL.md"] = skill_md.encode("utf-8")
        meta, report = check(files)
        blob = build_zip(files)
        if len(blob) > MAX_ZIP:
            raise HubError("Навык больше 2 МБ")
        status = "review" if (agent is not None or review) else "draft"
        row = NetSkillVersion(skill_id=skill.id, version=wanted, blob=blob, sha256=hashlib.sha256(blob).hexdigest(),
                              size=len(blob), files=[{"path": p, "size": len(files[p])} for p in sorted(files)],
                              skill_md=skill_md[:200000], changelog=(changelog or "").strip()[:4000],
                              why=(why or "").strip()[:4000], checks=report, status=status,
                              author_user_id=None if agent is not None else (user or {}).get("id"),
                              author_agent_id=agent.agent_id if agent is not None else None)
        s.add(row)
        _, body = parse_skill_md(skill_md)
        if not skill.current_version:  # what the card shows until something is published
            skill.title = title_of(meta, body, name)
            skill.description = str(meta.get("description") or "")[:MAX_DESCRIPTION]
        skill.updated_at = now()
        _refresh_status(s, skill)
        journal.log("hub.propose" if agent is not None else "hub.version", f"skill:{name}",
                    {"версия": wanted, "статус": status, "что изменилось": row.changelog[:300], "ошибок проверки": len(report["errors"])},
                    actor=by, actor_kind="agent" if agent is not None else "user",
                    agent_id=agent.agent_id if agent is not None else None, session=s)
        s.commit()
        return name, wanted


def _version(s, name: str, version: str):
    skill = s.query(NetSkill).filter(NetSkill.name == name).first()
    if skill is None:
        raise HubError("Нет такого навыка")
    row = s.query(NetSkillVersion).filter(NetSkillVersion.skill_id == skill.id, NetSkillVersion.version == version).first()
    if row is None:
        raise HubError("Нет такой версии")
    return skill, row


def to_review(name: str, version: str, user: dict) -> None:
    with SessionLocal() as s:
        skill, row = _version(s, name, version)
        if row.status != "draft":
            raise HubError("На проверку отправляется только черновик")
        if not can_approve(user, skill) and row.author_user_id != user.get("id"):
            raise HubError("Отправить на проверку может автор или владелец навыка")
        row.status = "review"
        _refresh_status(s, skill)
        journal.log("hub.review", f"skill:{name}", {"версия": version}, actor=user.get("username", ""), session=s)
        s.commit()


def publish(name: str, version: str, user: dict, note: str = "") -> None:
    with SessionLocal() as s:
        skill, row = _version(s, name, version)
        if not can_approve(user, skill):
            raise HubError("Публикует владелец навыка или руководитель (суперпользователь)")
        if row.status not in ("draft", "review"):
            raise HubError("Эта версия уже опубликована или отклонена")
        if (row.checks or {}).get("errors"):
            raise HubError("Проверка не пройдена: исправьте ошибки и загрузите новую версию")
        before = skill.current_version
        row.status, row.decided_by, row.decided_at, row.decision_note = "published", user.get("username", ""), now(), note[:1000]
        meta, body = parse_skill_md(row.skill_md or "")
        skill.current_version = version
        skill.title = title_of(meta, body, name)
        skill.description = str(meta.get("description") or "")[:MAX_DESCRIPTION]
        if skill.status == "retired":
            skill.status = "published"
        skill.updated_at = now()
        _refresh_status(s, skill)
        journal.log("hub.publish", f"skill:{name}", {"версия": {"было": before, "стало": version}},
                    actor=user.get("username", ""), session=s)
        s.commit()


def reject(name: str, version: str, user: dict, note: str = "") -> None:
    with SessionLocal() as s:
        skill, row = _version(s, name, version)
        if not can_approve(user, skill):
            raise HubError("Отклоняет владелец навыка или руководитель (суперпользователь)")
        if row.status not in ("draft", "review"):
            raise HubError("Опубликованная версия не отклоняется — можно откатиться на другую")
        row.status, row.decided_by, row.decided_at, row.decision_note = "rejected", user.get("username", ""), now(), note[:1000]
        _refresh_status(s, skill)
        journal.log("hub.reject", f"skill:{name}", {"версия": version, "почему": note[:300]}, actor=user.get("username", ""),
                    session=s)
        s.commit()


def rollback(name: str, version: str, user: dict) -> None:
    with SessionLocal() as s:
        skill, row = _version(s, name, version)
        if not can_approve(user, skill):
            raise HubError("Откатывает владелец навыка или руководитель (суперпользователь)")
        if row.status != "published":
            raise HubError("Откатиться можно только на опубликованную версию")
        before = skill.current_version
        skill.current_version = version
        meta, body = parse_skill_md(row.skill_md or "")
        skill.title, skill.description = title_of(meta, body, name), str(meta.get("description") or "")[:MAX_DESCRIPTION]
        skill.updated_at = now()
        journal.log("hub.rollback", f"skill:{name}", {"версия": {"было": before, "стало": version}},
                    actor=user.get("username", ""), session=s)
        s.commit()


def retire(name: str, user: dict, back: bool = False) -> None:
    with SessionLocal() as s:
        skill = s.query(NetSkill).filter(NetSkill.name == name).first()
        if skill is None:
            raise HubError("Нет такого навыка")
        if not can_approve(user, skill):
            raise HubError("Снимает владелец навыка или руководитель (суперпользователь)")
        skill.status = "draft" if back else "retired"
        if back:
            _refresh_status(s, skill)
        skill.updated_at = now()
        journal.log("hub.restore" if back else "hub.retire", f"skill:{name}", None, actor=user.get("username", ""), session=s)
        s.commit()


def configure(name: str, user: dict, department_id=None, owner_user_id=None, audience=None) -> None:
    with SessionLocal() as s:
        skill = s.query(NetSkill).filter(NetSkill.name == name).first()
        if skill is None:
            raise HubError("Нет такого навыка")
        if not can_approve(user, skill):
            raise HubError("Настраивает владелец навыка или руководитель (суперпользователь)")
        before = {"отдел": skill.department_id, "владелец": skill.owner_user_id, "кому": skill.audience}
        skill.department_id = department_id or None
        if owner_user_id and s.get(User, owner_user_id) is not None:
            skill.owner_user_id = owner_user_id
        if isinstance(audience, dict):
            skill.audience = audience
        journal.log("hub.settings", f"skill:{name}", journal.changes(before, {"отдел": skill.department_id,
                    "владелец": skill.owner_user_id, "кому": skill.audience}), actor=user.get("username", ""), session=s)
        s.commit()


# ----------------------------------------------------------------------------- reading
def files_of(name: str, version: str) -> dict:
    with SessionLocal() as s:
        _, row = _version(s, name, version)
        return unpack(row.blob)


def blob_of(name: str, version: str) -> tuple[bytes, str]:
    with SessionLocal() as s:
        _, row = _version(s, name, version)
        return row.blob, row.sha256


def diff(name: str, old: str, new: str) -> list:
    a, b = files_of(name, old), files_of(name, new)
    out = []
    for path in sorted(set(a) | set(b)):
        if path not in a:
            kind = "added"
        elif path not in b:
            kind = "removed"
        elif a[path] == b[path]:
            continue
        else:
            kind = "changed"
        ta, tb = _text(a.get(path, b"")), _text(b.get(path, b""))
        if ta is None or tb is None:
            text = "двоичный файл " + {"added": "добавлен", "removed": "удалён", "changed": "изменён"}[kind]
        else:
            text = "".join(difflib.unified_diff(ta.splitlines(True), tb.splitlines(True), f"{old}/{path}", f"{new}/{path}", n=2))
        out.append({"path": path, "kind": kind, "diff": text[:50000]})
    return out


def _dept_names(s) -> dict:
    return {d.id: d.name for d in s.query(Department).all()}


def listing(q: str = "", department_id=None) -> list:
    q = (q or "").strip().lower()
    with SessionLocal() as s:
        depts = _dept_names(s)
        owners = {u.id: u.username for u in s.query(User).all()}
        rows = s.query(NetSkill).order_by(NetSkill.name).all()
        review = {}
        for v in s.query(NetSkillVersion.skill_id).filter(NetSkillVersion.status == "review"):
            review[v.skill_id] = review.get(v.skill_id, 0) + 1
        out = []
        for k in rows:
            if department_id and k.department_id != department_id:
                continue
            if q and q not in f"{k.name} {k.title} {k.description}".lower():
                continue
            out.append({"name": k.name, "title": k.title or k.name, "description": k.description, "status": k.status,
                        "status_ru": STATUS_RU.get(k.status, (k.status, "gray")), "version": k.current_version,
                        "department": depts.get(k.department_id, ""), "owner": owners.get(k.owner_user_id, ""),
                        "review": review.get(k.id, 0), "updated_at": k.updated_at})
        return out


def review_queue() -> list:
    with SessionLocal() as s:
        rows = (s.query(NetSkillVersion, NetSkill).join(NetSkill, NetSkill.id == NetSkillVersion.skill_id)
                .filter(NetSkillVersion.status == "review").order_by(NetSkillVersion.created_at).all())
        agents = {a.id: a.name for a in s.query(NetAgent).all()}
        users = {u.id: u.username for u in s.query(User).all()}
        return [{"name": k.name, "version": v.version, "changelog": v.changelog, "why": v.why,
                 "author": agents.get(v.author_agent_id) or users.get(v.author_user_id, ""), "by_agent": bool(v.author_agent_id),
                 "errors": len((v.checks or {}).get("errors") or []), "created_at": v.created_at} for v, k in rows]


def card(name: str) -> dict | None:
    with SessionLocal() as s:
        k = s.query(NetSkill).filter(NetSkill.name == name).first()
        if k is None:
            return None
        depts = _dept_names(s)
        users = {u.id: u.username for u in s.query(User).all()}
        agents = s.query(NetAgent).all()
        names = {a.id: a.name for a in agents}
        versions = (s.query(NetSkillVersion).filter(NetSkillVersion.skill_id == k.id).all())
        versions.sort(key=lambda v: vkey(v.version), reverse=True)
        using = [{"agent": a.name, "version": ((a.hub_state or {}).get("skills") or {}).get(k.name)} for a in agents
                 if k.name in ((a.hub_state or {}).get("skills") or {})]
        uses = s.query(NetSkillUsage).filter(NetSkillUsage.skill == k.name).count()
        return {"name": k.name, "title": k.title or k.name, "description": k.description, "status": k.status,
                "status_ru": STATUS_RU.get(k.status, (k.status, "gray")), "current": k.current_version,
                "department_id": k.department_id, "department": depts.get(k.department_id, ""),
                "owner_user_id": k.owner_user_id, "owner": users.get(k.owner_user_id, ""), "audience": k.audience or {"all": True},
                "using": using, "uses": uses,
                "versions": [{"version": v.version, "status": v.status, "status_ru": STATUS_RU.get(v.status, (v.status, "gray")),
                              "sha256": v.sha256, "size": v.size, "files": v.files or [], "changelog": v.changelog, "why": v.why,
                              "checks": v.checks or {"errors": [], "warnings": []}, "skill_md": v.skill_md,
                              "author": names.get(v.author_agent_id) or users.get(v.author_user_id, ""),
                              "by_agent": bool(v.author_agent_id), "created_at": v.created_at, "decided_by": v.decided_by,
                              "decided_at": v.decided_at, "decision_note": v.decision_note,
                              "current": v.version == k.current_version} for v in versions]}


# ----------------------------------------------------------------------------- for agents
def _visible(skill: NetSkill, ctx, rules: dict, depts: dict) -> bool:
    if skill.status == "retired" or not skill.current_version:
        return False
    audience = skill.audience or {"all": True}
    if not audience.get("all") and ctx.department_id not in (audience.get("departments") or []):
        return False
    allowed = rules["skills"]["departments"]
    return "*" in allowed or skill.department_id is None or depts.get(skill.department_id) in allowed \
        or str(skill.department_id) in allowed


def visible_skills(ctx) -> list:
    from . import access
    rules = access.rules_for(ctx.agent_id)
    with SessionLocal() as s:
        depts = _dept_names(s)
        out = []
        for k in s.query(NetSkill).order_by(NetSkill.name).all():
            if _visible(k, ctx, rules, depts):
                v = s.query(NetSkillVersion).filter(NetSkillVersion.skill_id == k.id, NetSkillVersion.version == k.current_version).first()
                if v is not None:
                    out.append({"name": k.name, "title": k.title or k.name, "description": k.description,
                                "version": v.version, "sha256": v.sha256, "size": v.size,
                                "department": depts.get(k.department_id, "")})
        return out


def manifest(ctx) -> dict:
    """What the synchronizer keeps in the read-only hub folder: name, version, sha256 and where to download."""
    skills = [{**item, "url": f"/agents/api/hub/skills/{item['name']}/{item['version']}.zip"} for item in visible_skills(ctx)]
    rev = hashlib.sha256(json.dumps([(k["name"], k["version"], k["sha256"]) for k in skills]).encode()).hexdigest()[:12]
    return {"rev": rev, "skills": skills, "generated_at": now().isoformat(timespec="seconds")}


def agent_blob(ctx, name: str, version: str):
    """The archive of a published version the agent may have, or None."""
    from . import access
    rules = access.rules_for(ctx.agent_id)
    with SessionLocal() as s:
        skill = s.query(NetSkill).filter(NetSkill.name == name).first()
        if skill is None or not _visible(skill, ctx, rules, _dept_names(s)):
            return None
        row = s.query(NetSkillVersion).filter(NetSkillVersion.skill_id == skill.id, NetSkillVersion.version == version,
                                              NetSkillVersion.status == "published").first()
        return (row.blob, row.sha256) if row is not None else None


def propose(ctx, files: dict, changelog: str = "", why: str = "") -> tuple[str, str]:
    """A skill the agent made or improved itself: a proposal for the owner (ОПО-3.6)."""
    from . import access
    if not access.rules_for(ctx.agent_id)["skills"]["propose"]:
        raise HubError("Доступы агента не разрешают предлагать навыки")
    return submit(files, agent=ctx, changelog=changelog, why=why)


def record_use(ctx, skill: str, version: str = "", tokens: int = 0, ok=None) -> None:
    with SessionLocal() as s:
        s.add(NetSkillUsage(skill=str(skill)[:64], version=str(version or "")[:20], agent_id=ctx.agent_id,
                            tokens=int(tokens or 0), ok=ok))
        s.commit()
