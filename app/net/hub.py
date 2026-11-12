"""SYSAI_Hub — one hub of skills and tools for every agent of the network (ОПО-3.x).

A skill is an open SKILL.md folder (name, description, version; «Когда применять», «Как делать», «Ошибки»,
«Проверка») with references/, scripts/, assets/ — Hermes, OpenClaw, Claude Code and Codex read it as is. A tool is a
TOOL.md folder: an MCP server (run on the computer or a url) or a program the agent runs from its skills; the frontmatter
says how to start it. Versions go draft → review → published; a published version never changes (a fix is a new
version, a rollback picks an older one).

Agents take from here what they need. «Всегда с собой» items reach every agent they are allowed to; «по запросу» ones
the agent finds itself (hub_search) and takes when a task needs them (hub_take). What an agent learned itself comes back
as a proposal and becomes everyone's after the owner approves it."""
import difflib
import hashlib
import io
import json
import re
import zipfile
from urllib.parse import urlparse

from sqlalchemy import func

from .. import config, settings_store
from ..db import Department, SessionLocal, User, now
from . import journal
from .models import NetAgent, NetHubPick, NetSkill, NetSkillUsage, NetSkillVersion

NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
MB = 1024 * 1024
MAX_ZIP = 2 * MB                     # a skill: instructions and small scripts
MAX_UNPACKED = 8 * MB
MAX_FILES = 200
MAX_TOOL_ZIP = 10 * MB               # a tool: the code of an MCP server or a program (dependencies are installed on the PC)
MAX_TOOL_UNPACKED = 40 * MB
MAX_TOOL_FILES = 2000
MAX_DESCRIPTION = 1024
SKILL_DOC, TOOL_DOC = "SKILL.md", "TOOL.md"
SECTIONS = ("Когда применять", "Как делать", "Ошибки", "Проверка")
TOOL_SECTIONS = ("Когда применять", "Как запускать", "Ошибки")
TOP_LEVEL = ("SKILL.md", "references", "scripts", "assets", "README.md", "LICENSE", "LICENSE.txt")
JUNK = ("__MACOSX/", ".DS_Store", "Thumbs.db", ".git/", "desktop.ini", "__pycache__/", ".pyc")
STATUS_RU = {"draft": ("черновик", "gray"), "review": ("на проверке", "amber"), "published": ("опубликован", "green"),
             "retired": ("снят", "red"), "rejected": ("отклонён", "red")}
KINDS = ("skill", "mcp", "program")
KIND_RU = {"skill": "навык", "mcp": "MCP-сервер", "program": "программа"}
KIND_GROUP = {"skill": "Навыки", "mcp": "MCP-серверы", "program": "Программы"}
KIND_ICON = {"skill": "spark", "mcp": "plug", "program": "terminal"}
DELIVERY = ("always", "on_demand")
DELIVERY_RU = {"always": "всегда с собой", "on_demand": "по запросу"}
PLATFORMS = ("any", "windows", "linux")
PLATFORM_RU = {"any": "любой ПК", "windows": "Windows", "linux": "Linux"}
RESERVED = ("sysai",)                # the built-in MCP server of SYSAI
EXECUTABLE = (".exe", ".dll", ".msi", ".sys", ".scr", ".com", ".so", ".dylib")
REQUEST_RE = re.compile(r"requests\.|httpx\.|urllib|urlopen|fetch\(|axios|WebClient|Invoke-WebRequest|Invoke-RestMethod|"
                        r"\b(curl|wget|iwr|irm)\b|http\.client|aiohttp|socket\.", re.I)
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


def tool_template(name: str = "my-tool", kind: str = "mcp") -> str:
    run = "python server.py" if kind == "mcp" else "python main.py"
    what = "MCP-сервер: какие инструменты даёт агенту" if kind == "mcp" else "Программа: что делает и что выдаёт"
    return (f"---\nname: {name}\nkind: {kind}\ndescription: {what} — одна-две фразы.\nversion: 1.0.0\n"
            f"run: {run}\nplatform: windows\n" + ("tools: tool_one, tool_two\n" if kind == "mcp" else "") +
            "requires: python>=3.11\n---\n# Название инструмента\n\n## Когда применять\n\n## Как запускать\n\n"
            "Синхронизатор ставит папку инструмента в %SYSAI_HOME%\\tools и запускает команду run из неё.\n\n## Ошибки\n")


def _items(value) -> list:
    """A list of the frontmatter: «a, b», a block («- a» on lines) or a YAML flow list «[a, "b"]»."""
    text = str(value or "").strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    out = []
    for part in re.split(r"[,\n]", text):
        part = _unquote(part.strip().strip("-").strip()).strip()
        if part and part not in out:
            out.append(part[:120])
    return out


def tool_spec(meta: dict) -> dict:
    """How a tool runs, from the TOOL.md frontmatter: kind, run (a command started in the tool's folder) or url
    (a remote MCP server), platform, the MCP tools it gives, what it needs on the computer."""
    platform = str(meta.get("platform") or "any").strip().lower()
    return {"kind": str(meta.get("kind") or "").strip().lower(), "run": str(meta.get("run") or "").strip()[:500],
            "url": str(meta.get("url") or "").strip()[:500], "platform": platform,
            "tools": _items(meta.get("tools"))[:100], "requires": _items(meta.get("requires"))[:50]}


# ----------------------------------------------------------------------------- archives
def vkey(version: str) -> tuple:
    try:
        return tuple(int(p) for p in str(version).split("."))
    except ValueError:
        return (0, 0, 0)


def bump(version: str) -> str:
    a, b, c = (list(vkey(version)) + [0, 0, 0])[:3]
    return f"{a}.{b}.{c + 1}"


def doc_of(files: dict) -> str:
    """SKILL.md (a skill) or TOOL.md (a tool): exactly one of them in the root of the folder."""
    skill, tool = SKILL_DOC in files, TOOL_DOC in files
    if skill and tool:
        raise HubError("В одной папке либо SKILL.md (навык), либо TOOL.md (инструмент)")
    if not skill and not tool:
        raise HubError("В архиве нет SKILL.md или TOOL.md (в корне или в единственной папке)")
    return SKILL_DOC if skill else TOOL_DOC


def _skill_limits(files: dict) -> None:
    if len(files) > MAX_FILES or sum(len(v) for v in files.values()) > MAX_UNPACKED:
        raise HubError(f"Слишком большой навык: не больше {MAX_FILES} файлов и {MAX_UNPACKED // MB} МБ "
                       "(код и программы — отдельным инструментом с TOOL.md)")


def read_zip(data: bytes) -> dict:
    """Files of an uploaded skill or tool folder: safe relative paths only, no links, within the limits. A single top
    folder (the item's own name) is taken off."""
    if len(data) > MAX_TOOL_ZIP:
        raise HubError(f"Архив больше {MAX_TOOL_ZIP // MB} МБ")
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
        if total > MAX_TOOL_UNPACKED or len(files) >= MAX_TOOL_FILES:
            raise HubError(f"Слишком большой архив: не больше {MAX_TOOL_FILES} файлов и {MAX_TOOL_UNPACKED // MB} МБ")
        files[name] = archive.read(info)
    if SKILL_DOC not in files and TOOL_DOC not in files:
        tops = {p.split("/")[0] for p in files}
        if len(tops) == 1 and all("/" in p for p in files):
            top = tops.pop() + "/"
            files = {p[len(top):]: v for p, v in files.items()}
    if doc_of(files) == SKILL_DOC:
        if len(data) > MAX_ZIP:
            raise HubError(f"Навык больше {MAX_ZIP // MB} МБ (код и программы — отдельным инструментом с TOOL.md)")
        _skill_limits(files)
    return files


def safe_files(files: dict) -> dict:
    """Files given as a mapping (the editor, MCP): the same path rules and limits as an archive."""
    if not isinstance(files, dict):
        raise HubError("Файлы: объект {путь: текст}")
    out, total = {}, 0
    for path, content in files.items():
        path = str(path).replace("\\", "/").strip()
        parts = path.split("/")
        if not path or path.startswith("/") or ":" in parts[0] or any(p in ("", ".", "..") for p in parts):
            raise HubError(f"Недопустимый путь: {path}")
        data = content.encode("utf-8") if isinstance(content, str) else bytes(content)
        total += len(data)
        if total > MAX_TOOL_UNPACKED or len(out) >= MAX_TOOL_FILES:
            raise HubError(f"Слишком много файлов: не больше {MAX_TOOL_FILES} файлов и {MAX_TOOL_UNPACKED // MB} МБ")
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


def _run_file(run: str) -> str:
    """The file the run command starts, when it names one inside the folder («python server.py» → server.py)."""
    for token in run.replace('"', " ").split()[:3]:
        token = token.replace("\\", "/")
        if token.startswith("-"):
            continue
        if "/" in token or re.search(r"\.(py|cmd|bat|ps1|exe|js|mjs|sh)$", token, re.I):
            return token[2:] if token.startswith("./") else token
    return ""


def _check_tool(meta: dict, files: dict, errors: list, warnings: list) -> None:
    spec = tool_spec(meta)
    if spec["kind"] not in ("mcp", "program"):
        errors.append({"path": TOOL_DOC, "what": "kind: mcp (MCP-сервер) или program (программа)"})
        return
    if spec["run"] and spec["url"]:
        errors.append({"path": TOOL_DOC, "what": "либо run (запуск на ПК), либо url (сервер в сети) — не оба"})
    if spec["kind"] == "mcp" and not (spec["run"] or spec["url"]):
        errors.append({"path": TOOL_DOC, "what": "MCP-серверу нужен run (команда запуска на ПК) или url (адрес сервера)"})
    if spec["kind"] == "program" and not spec["run"]:
        errors.append({"path": TOOL_DOC, "what": "программе нужен run — команда запуска из папки инструмента"})
    if spec["kind"] == "program" and spec["url"]:
        errors.append({"path": TOOL_DOC, "what": "url бывает только у MCP-сервера"})
    if spec["platform"] not in PLATFORMS:
        errors.append({"path": TOOL_DOC, "what": "platform: windows, linux или any"})
    if spec["url"]:
        parsed = urlparse(spec["url"])
        host = (parsed.hostname or "").lower()
        if parsed.scheme not in ("https", "http") or not host:
            errors.append({"path": TOOL_DOC, "what": "url: адрес вида https://сервер/mcp"})
        elif parsed.scheme == "http" and host not in ("localhost", "127.0.0.1"):
            errors.append({"path": TOOL_DOC, "what": "url: только https (http — лишь для localhost)"})
        elif not _host_ok(host, allowed_hosts()):
            errors.append({"path": TOOL_DOC, "what": f"url: сервер {host} не в списке разрешённых сайтов хаба"})
    if spec["run"]:
        target = _run_file(spec["run"])
        if target and target not in files:
            warnings.append({"path": TOOL_DOC, "what": f"run запускает {target}, а такого файла в папке нет"})
    if spec["kind"] == "mcp" and not spec["tools"]:
        warnings.append({"path": TOOL_DOC, "what": "не перечислены tools: в доступах их не выбрать по одному"})


def check(files: dict) -> tuple[dict, dict]:
    """(frontmatter, report). Errors block publishing (ОПО-3.8): a bad name or description, dangerous commands,
    secrets, unknown sites in scripts, a tool that cannot be started. Warnings are shown to the reviewer."""
    errors, warnings = [], []
    doc = TOOL_DOC if TOOL_DOC in files and SKILL_DOC not in files else SKILL_DOC
    text = _text(files.get(doc, b"")) or ""
    meta, body = parse_skill_md(text)
    name = str(meta.get("name") or "")
    what = "навык" if doc == SKILL_DOC else "инструмент"
    if not meta:
        errors.append({"path": doc, "what": f"нет заголовка --- name / description --- в начале {doc}"})
    if not NAME_RE.fullmatch(name) or len(name) > 64:
        errors.append({"path": doc, "what": "name: латиница в нижнем регистре, цифры и дефисы, до 64 знаков"})
    description = str(meta.get("description") or "").strip()
    if not description:
        errors.append({"path": doc, "what": f"нет description: агент не поймёт, когда применять {what}"})
    elif len(description) > MAX_DESCRIPTION:
        errors.append({"path": doc, "what": f"description длиннее {MAX_DESCRIPTION} знаков"})
    if meta.get("version") and not VERSION_RE.fullmatch(str(meta["version"])):
        errors.append({"path": doc, "what": "version: три числа через точку, например 1.0.0"})
    if doc == TOOL_DOC:
        _check_tool(meta, files, errors, warnings)
    for section in (SECTIONS if doc == SKILL_DOC else TOOL_SECTIONS):
        if not re.search(r"^#{1,4}\s*" + re.escape(section), body, re.M | re.I):
            warnings.append({"path": doc, "what": f"нет раздела «{section}»"})
    hosts = allowed_hosts()
    for path in sorted(files):
        top = path.split("/")[0]
        if doc == SKILL_DOC and top not in TOP_LEVEL:
            warnings.append({"path": path, "what": "вне SKILL.md, references/, scripts/, assets/"})
        if path.lower().endswith(EXECUTABLE):
            warnings.append({"path": path, "what": "исполняемый файл: SYSAI не видит, что внутри — только из надёжного источника"})
            continue
        content = _text(files[path])
        if content is None:
            if doc == SKILL_DOC and top == "scripts":
                warnings.append({"path": path, "what": "двоичный файл в scripts/ — не проверить"})
            continue
        code = top == "scripts" if doc == SKILL_DOC else path != TOOL_DOC
        for number, line in enumerate(content.splitlines(), 1):
            for pattern, danger in _DANGER:
                if pattern.search(line):
                    errors.append({"path": path, "line": number, "what": "опасная команда: " + danger})
            for pattern, secret in _SECRETS:
                if pattern.search(line):
                    errors.append({"path": path, "line": number, "what": f"секрет в {'навыке' if doc == SKILL_DOC else 'инструменте'}: " + secret})
            for host in URL_RE.findall(line):
                if not _host_ok(host, hosts):
                    item = {"path": path, "line": number, "what": f"внешний адрес {host}"}
                    # in a skill every script line counts; in a tool's code — the lines that really go to the network
                    blocking = code and (doc == SKILL_DOC or REQUEST_RE.search(line))
                    (errors if blocking else warnings).append(item)
    return meta, {"errors": errors[:100], "warnings": warnings[:100]}


# ----------------------------------------------------------------------------- lifecycle
def can_approve(user: dict | None, skill: NetSkill) -> bool:
    """The owner of the skill or a superuser (for now the head of the owning department is a superuser)."""
    return bool(user) and (user.get("role") == "superuser" or (skill.owner_user_id is not None and skill.owner_user_id == user.get("id")))


def _target(skill: NetSkill) -> str:
    return f"{skill.kind or 'skill'}:{skill.name}"


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
    """A new version of a skill (SKILL.md) or a tool (TOOL.md) from a person (draft, or «на проверку») or from an agent
    (always a proposal for review). Returns (name, version)."""
    files = safe_files(files)
    doc = doc_of(files)
    meta, _ = parse_skill_md(_text(files[doc]) or "")
    name = str(meta.get("name") or "")
    if not NAME_RE.fullmatch(name) or len(name) > 64:
        raise HubError(f"name в {doc}: латиница в нижнем регистре, цифры и дефисы, например plc-io-from-excel")
    if name in RESERVED:
        raise HubError(f"Имя {name} занято встроенным MCP-сервером SYSAI")
    if not str(meta.get("description") or "").strip():
        raise HubError(f"Нужен description в {doc}")
    kind = "skill" if doc == SKILL_DOC else str(meta.get("kind") or "").strip().lower()
    if kind not in ("mcp", "program") and doc == TOOL_DOC:
        raise HubError("kind в TOOL.md: mcp (MCP-сервер) или program (программа)")
    if kind == "skill":
        _skill_limits(files)
    by = agent.name if agent is not None else (user or {}).get("username", "")
    with SessionLocal() as s:
        skill = s.query(NetSkill).filter(NetSkill.name == name).first()
        if skill is None:
            dept = department_id if department_id is not None else (agent.department_id if agent is not None else None)
            owner = agent.user_id if agent is not None else (user or {}).get("id")
            skill = NetSkill(name=name, kind=kind, delivery="always", department_id=dept, owner_user_id=owner,
                             audience={"all": True}, status="draft")
            s.add(skill)
            s.flush()
        elif (skill.kind or "skill") != kind:
            raise HubError(f"«{name}» в хабе уже есть, и это {KIND_RU.get(skill.kind or 'skill')}: выберите другое имя")
        elif skill.status == "retired" and agent is not None:
            raise HubError("Снято с хаба: предложения не принимаются")
        versions = [v.version for v in s.query(NetSkillVersion.version).filter(NetSkillVersion.skill_id == skill.id)]
        latest = max(versions, key=vkey) if versions else ""
        wanted = str(meta.get("version") or "")
        wanted = wanted if VERSION_RE.fullmatch(wanted) else ""
        if wanted and wanted in versions and agent is None:
            raise HubError(f"Версия {wanted} уже есть: поднимите version в {doc} (например {bump(latest)})")
        if not wanted or wanted in versions or (latest and vkey(wanted) <= vkey(latest)):
            wanted = bump(latest) if latest else (wanted or "1.0.0")
        text = set_version(_text(files[doc]) or "", wanted)
        files[doc] = text.encode("utf-8")
        meta, report = check(files)
        blob = build_zip(files)
        limit = MAX_ZIP if kind == "skill" else MAX_TOOL_ZIP
        if len(blob) > limit:
            raise HubError(f"{'Навык' if kind == 'skill' else 'Инструмент'} больше {limit // MB} МБ")
        status = "review" if (agent is not None or review) else "draft"
        row = NetSkillVersion(skill_id=skill.id, version=wanted, blob=blob, sha256=hashlib.sha256(blob).hexdigest(),
                              size=len(blob), files=[{"path": p, "size": len(files[p])} for p in sorted(files)],
                              skill_md=text[:200000], spec=tool_spec(meta) if kind != "skill" else None,
                              changelog=(changelog or "").strip()[:4000],
                              why=(why or "").strip()[:4000], checks=report, status=status,
                              author_user_id=None if agent is not None else (user or {}).get("id"),
                              author_agent_id=agent.agent_id if agent is not None else None)
        s.add(row)
        _, body = parse_skill_md(text)
        if not skill.current_version:  # what the card shows until something is published
            skill.title = title_of(meta, body, name)
            skill.description = str(meta.get("description") or "")[:MAX_DESCRIPTION]
        skill.updated_at = now()
        _refresh_status(s, skill)
        journal.log("hub.propose" if agent is not None else "hub.version", f"{kind}:{name}",
                    {"версия": wanted, "статус": status, "что изменилось": row.changelog[:300], "ошибок проверки": len(report["errors"])},
                    actor=by, actor_kind="agent" if agent is not None else "user",
                    agent_id=agent.agent_id if agent is not None else None, session=s)
        s.commit()
        return name, wanted


def _version(s, name: str, version: str):
    skill = s.query(NetSkill).filter(NetSkill.name == name).first()
    if skill is None:
        raise HubError("В хабе этого нет")
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
            raise HubError("Отправить на проверку может автор или владелец")
        row.status = "review"
        _refresh_status(s, skill)
        journal.log("hub.review", _target(skill), {"версия": version}, actor=user.get("username", ""), session=s)
        s.commit()


def publish(name: str, version: str, user: dict, note: str = "") -> None:
    with SessionLocal() as s:
        skill, row = _version(s, name, version)
        if not can_approve(user, skill):
            raise HubError("Публикует владелец или руководитель (суперпользователь)")
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
        journal.log("hub.publish", _target(skill), {"версия": {"было": before, "стало": version}},
                    actor=user.get("username", ""), session=s)
        s.commit()


def reject(name: str, version: str, user: dict, note: str = "") -> None:
    with SessionLocal() as s:
        skill, row = _version(s, name, version)
        if not can_approve(user, skill):
            raise HubError("Отклоняет владелец или руководитель (суперпользователь)")
        if row.status not in ("draft", "review"):
            raise HubError("Опубликованная версия не отклоняется — можно откатиться на другую")
        row.status, row.decided_by, row.decided_at, row.decision_note = "rejected", user.get("username", ""), now(), note[:1000]
        _refresh_status(s, skill)
        journal.log("hub.reject", _target(skill), {"версия": version, "почему": note[:300]}, actor=user.get("username", ""),
                    session=s)
        s.commit()


def rollback(name: str, version: str, user: dict) -> None:
    with SessionLocal() as s:
        skill, row = _version(s, name, version)
        if not can_approve(user, skill):
            raise HubError("Откатывает владелец или руководитель (суперпользователь)")
        if row.status != "published":
            raise HubError("Откатиться можно только на опубликованную версию")
        before = skill.current_version
        skill.current_version = version
        meta, body = parse_skill_md(row.skill_md or "")
        skill.title, skill.description = title_of(meta, body, name), str(meta.get("description") or "")[:MAX_DESCRIPTION]
        skill.updated_at = now()
        journal.log("hub.rollback", _target(skill), {"версия": {"было": before, "стало": version}},
                    actor=user.get("username", ""), session=s)
        s.commit()


def retire(name: str, user: dict, back: bool = False) -> None:
    with SessionLocal() as s:
        skill = s.query(NetSkill).filter(NetSkill.name == name).first()
        if skill is None:
            raise HubError("В хабе этого нет")
        if not can_approve(user, skill):
            raise HubError("Снимает владелец или руководитель (суперпользователь)")
        skill.status = "draft" if back else "retired"
        if back:
            _refresh_status(s, skill)
        skill.updated_at = now()
        journal.log("hub.restore" if back else "hub.retire", _target(skill), None, actor=user.get("username", ""), session=s)
        s.commit()


def configure(name: str, user: dict, department_id=None, owner_user_id=None, audience=None, delivery=None) -> None:
    with SessionLocal() as s:
        skill = s.query(NetSkill).filter(NetSkill.name == name).first()
        if skill is None:
            raise HubError("В хабе этого нет")
        if not can_approve(user, skill):
            raise HubError("Настраивает владелец или руководитель (суперпользователь)")
        before = {"отдел": skill.department_id, "владелец": skill.owner_user_id, "кому": skill.audience,
                  "как берут": skill.delivery or "always"}
        skill.department_id = department_id or None
        if owner_user_id and s.get(User, owner_user_id) is not None:
            skill.owner_user_id = owner_user_id
        if isinstance(audience, dict):
            skill.audience = audience
        if delivery in DELIVERY:
            skill.delivery = delivery
        journal.log("hub.settings", f"{skill.kind or 'skill'}:{name}", journal.changes(before, {"отдел": skill.department_id,
                    "владелец": skill.owner_user_id, "кому": skill.audience, "как берут": skill.delivery or "always"}),
                    actor=user.get("username", ""), session=s)
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


def _holders(s) -> dict:
    """name → {agent: version} of what the agents reported they have (heartbeat: hub_state)."""
    out = {}
    for a in s.query(NetAgent).all():
        state = a.hub_state or {}
        for part in ("skills", "tools"):
            for item, version in (state.get(part) or {}).items():
                out.setdefault(item, {})[a.name] = version
    return out


def _uses(s) -> dict:
    return dict(s.query(NetSkillUsage.skill, func.count(NetSkillUsage.id)).group_by(NetSkillUsage.skill).all())


def builtin() -> dict:
    """The MCP server of SYSAI itself: always there, nothing to upload — what every agent connects to first."""
    from . import mcp
    with SessionLocal() as s:
        holders = sum(1 for a in s.query(NetAgent).filter(NetAgent.status == "active"))
    return {"name": "sysai", "kind": "mcp", "kind_ru": KIND_RU["mcp"], "icon": KIND_ICON["mcp"], "title": "SYSAI",
            "description": "Встроенный MCP-сервер SYSAI: кто я и что мне можно, отделы и люди, хаб, хранилище файлов, "
                           "сообщение своему человеку и журнал. Подключён у каждого агента.",
            "builtin": True, "status": "published", "status_ru": ("встроенный", "blue"), "version": mcp.SERVER_INFO["version"],
            "current": mcp.SERVER_INFO["version"], "delivery": "always", "delivery_ru": DELIVERY_RU["always"],
            "department": "", "department_id": None, "owner": "SYSAI", "review": 0, "uses": 0, "agents": holders,
            "spec": {"kind": "mcp", "run": "", "url": "/mcp", "platform": "any", "tools": list(mcp.TOOLS), "requires": []},
            "tools": [{"name": n, "description": t[0], "read_only": t[3]} for n, t in mcp.TOOLS.items()],
            "updated_at": None, "versions": [], "using": [], "picks": [], "audience": {"all": True}}


def listing(q: str = "", department_id=None, kind: str = "") -> list:
    q = (q or "").strip().lower()
    with SessionLocal() as s:
        depts = _dept_names(s)
        owners = {u.id: u.username for u in s.query(User).all()}
        rows = s.query(NetSkill).order_by(NetSkill.name).all()
        review = {}
        for v in s.query(NetSkillVersion.skill_id).filter(NetSkillVersion.status == "review"):
            review[v.skill_id] = review.get(v.skill_id, 0) + 1
        specs = {}
        for skill_id, version, spec in s.query(NetSkillVersion.skill_id, NetSkillVersion.version, NetSkillVersion.spec):
            specs[(skill_id, version)] = spec
        latest = {}
        for k in rows:
            vs = [ver for (sid, ver) in specs if sid == k.id]
            latest[k.id] = max(vs, key=vkey) if vs else ""
        holders, uses = _holders(s), _uses(s)
        out = []
        for k in rows:
            item_kind = k.kind or "skill"
            if department_id and k.department_id != department_id:
                continue
            if kind and item_kind != kind:
                continue
            if q and q not in f"{k.name} {k.title} {k.description}".lower():
                continue
            spec = specs.get((k.id, k.current_version or latest[k.id])) or {}
            out.append({"name": k.name, "kind": item_kind, "kind_ru": KIND_RU.get(item_kind, item_kind),
                        "icon": KIND_ICON.get(item_kind, "box"), "title": k.title or k.name, "description": k.description,
                        "status": k.status, "status_ru": STATUS_RU.get(k.status, (k.status, "gray")), "version": k.current_version,
                        "latest": latest[k.id], "department": depts.get(k.department_id, ""), "department_id": k.department_id,
                        "owner": owners.get(k.owner_user_id, ""), "review": review.get(k.id, 0), "updated_at": k.updated_at,
                        "delivery": k.delivery or "always", "delivery_ru": DELIVERY_RU.get(k.delivery or "always"),
                        "spec": spec, "agents": len(holders.get(k.name, {})), "uses": uses.get(k.name, 0), "builtin": False})
        return out


def version_states() -> dict:
    """{name: {'current': the published version or '', 'versions': {version: status}}} — to tell what a package brings."""
    with SessionLocal() as s:
        out = {k.name: {"current": k.current_version or "", "retired": k.status == "retired", "versions": {}, "id": k.id}
               for k in s.query(NetSkill).all()}
        by_id = {v["id"]: v for v in out.values()}
        for skill_id, version, status in s.query(NetSkillVersion.skill_id, NetSkillVersion.version, NetSkillVersion.status):
            if skill_id in by_id:
                by_id[skill_id]["versions"][version] = status
        for v in out.values():
            v.pop("id")
        return out


def review_queue() -> list:
    with SessionLocal() as s:
        rows = (s.query(NetSkillVersion, NetSkill).join(NetSkill, NetSkill.id == NetSkillVersion.skill_id)
                .filter(NetSkillVersion.status == "review").order_by(NetSkillVersion.created_at).all())
        agents = {a.id: a.name for a in s.query(NetAgent).all()}
        users = {u.id: u.username for u in s.query(User).all()}
        return [{"name": k.name, "kind": k.kind or "skill", "kind_ru": KIND_RU.get(k.kind or "skill"), "title": k.title or k.name,
                 "icon": KIND_ICON.get(k.kind or "skill", "box"), "version": v.version, "changelog": v.changelog, "why": v.why,
                 "author": agents.get(v.author_agent_id) or users.get(v.author_user_id, ""), "by_agent": bool(v.author_agent_id),
                 "errors": len((v.checks or {}).get("errors") or []), "created_at": v.created_at} for v, k in rows]


def card(name: str) -> dict | None:
    if name in RESERVED:
        return builtin()
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
        using = [{"agent": agent, "version": version} for agent, version in sorted(_holders(s).get(k.name, {}).items())]
        picks = [{"agent": names.get(p.agent_id, "?"), "why": p.why, "at": p.at}
                 for p in s.query(NetHubPick).filter(NetHubPick.name == k.name).order_by(NetHubPick.at)]
        uses = s.query(NetSkillUsage).filter(NetSkillUsage.skill == k.name).count()
        item_kind = k.kind or "skill"
        current = next((v for v in versions if v.version == k.current_version), None) or (versions[0] if versions else None)
        return {"name": k.name, "kind": item_kind, "kind_ru": KIND_RU.get(item_kind, item_kind), "icon": KIND_ICON.get(item_kind, "box"),
                "title": k.title or k.name, "description": k.description, "status": k.status, "builtin": False,
                "status_ru": STATUS_RU.get(k.status, (k.status, "gray")), "current": k.current_version,
                "department_id": k.department_id, "department": depts.get(k.department_id, ""),
                "owner_user_id": k.owner_user_id, "owner": users.get(k.owner_user_id, ""), "audience": k.audience or {"all": True},
                "delivery": k.delivery or "always", "delivery_ru": DELIVERY_RU.get(k.delivery or "always"),
                "spec": (current.spec if current is not None else None) or {}, "doc": TOOL_DOC if item_kind != "skill" else SKILL_DOC,
                "using": using, "picks": picks, "uses": uses,
                "versions": [{"version": v.version, "status": v.status, "status_ru": STATUS_RU.get(v.status, (v.status, "gray")),
                              "sha256": v.sha256, "size": v.size, "files": v.files or [], "changelog": v.changelog, "why": v.why,
                              "checks": v.checks or {"errors": [], "warnings": []}, "skill_md": v.skill_md, "spec": v.spec or {},
                              "author": names.get(v.author_agent_id) or users.get(v.author_user_id, ""),
                              "by_agent": bool(v.author_agent_id), "created_at": v.created_at, "decided_by": v.decided_by,
                              "decided_at": v.decided_at, "decision_note": v.decision_note,
                              "current": v.version == k.current_version} for v in versions]}


def summary() -> dict:
    """The line under the title of the hub page."""
    items = listing()
    count = {kind: sum(1 for i in items if i["kind"] == kind) for kind in KINDS}
    return {"skills": count["skill"], "mcp": count["mcp"] + 1, "programs": count["program"],
            "review": len(review_queue()), "on_demand": sum(1 for i in items if i["delivery"] == "on_demand")}


# ----------------------------------------------------------------------------- for agents
def _allowed(skill: NetSkill, rules: dict) -> bool:
    """A skill is advice; a tool runs on the computer: an MCP server must be in «MCP-инструменты» of the access,
    a program — in «Программы» (or «*»)."""
    kind = skill.kind or "skill"
    if kind == "mcp":
        return skill.name in (rules.get("mcp") or {})
    if kind == "program":
        programs = [p.lower() for p in rules.get("programs") or []]
        return "*" in programs or skill.name.lower() in programs
    return True


def _visible(skill: NetSkill, ctx, rules: dict, depts: dict) -> bool:
    if skill.status == "retired" or not skill.current_version:
        return False
    audience = skill.audience or {"all": True}
    if not audience.get("all") and ctx.department_id not in (audience.get("departments") or []):
        return False
    allowed = rules["skills"]["departments"]
    if not ("*" in allowed or skill.department_id is None or depts.get(skill.department_id) in allowed
            or str(skill.department_id) in allowed):
        return False
    return _allowed(skill, rules)


def catalog(ctx, rules=None) -> list:
    """Everything published the agent may take: «всегда с собой» and «по запросу», with what it already took."""
    from . import access
    rules = rules if rules is not None else access.rules_for(ctx.agent_id)
    with SessionLocal() as s:
        depts = _dept_names(s)
        picked = {p.name for p in s.query(NetHubPick).filter(NetHubPick.agent_id == ctx.agent_id)}
        out = []
        for k in s.query(NetSkill).order_by(NetSkill.name).all():
            if not _visible(k, ctx, rules, depts):
                continue
            v = s.query(NetSkillVersion).filter(NetSkillVersion.skill_id == k.id, NetSkillVersion.version == k.current_version).first()
            if v is None:
                continue
            kind = k.kind or "skill"
            delivery = k.delivery or "always"
            out.append({"name": k.name, "kind": kind, "title": k.title or k.name, "description": k.description,
                        "version": v.version, "sha256": v.sha256, "size": v.size, "department": depts.get(k.department_id, ""),
                        "delivery": delivery, "taken": k.name in picked, "spec": (v.spec or {}) if kind != "skill" else None,
                        "with_agent": delivery == "always" or k.name in picked})
        return out


def visible_skills(ctx) -> list:
    return [i for i in catalog(ctx) if i["kind"] == "skill"]


def delivered_tools(ctx, rules=None) -> list:
    """The tools the synchronizer installs for this agent (and the harness configuration starts)."""
    return [i for i in catalog(ctx, rules) if i["kind"] != "skill" and i["with_agent"]]


def manifest(ctx) -> dict:
    """What the synchronizer keeps on the computer: skills in the read-only hub folder, tools in their own folders —
    name, version, sha256, where to download and (tools) how to start. «По запросу» items are here once taken."""
    items = [i for i in catalog(ctx) if i["with_agent"]]
    skills = [{k: i[k] for k in ("name", "title", "description", "version", "sha256", "size", "department")} |
              {"url": f"/agents/api/hub/skills/{i['name']}/{i['version']}.zip"} for i in items if i["kind"] == "skill"]
    tools = [{k: i[k] for k in ("name", "kind", "title", "description", "version", "sha256", "size", "department", "spec")} |
             {"url": f"/agents/api/hub/tools/{i['name']}/{i['version']}.zip"} for i in items if i["kind"] != "skill"]
    rev = hashlib.sha256(json.dumps([(k["name"], k["version"], k["sha256"]) for k in skills + tools]).encode()).hexdigest()[:12]
    return {"rev": rev, "skills": skills, "tools": tools, "generated_at": now().isoformat(timespec="seconds")}


def agent_blob(ctx, name: str, version: str):
    """The archive of a published version the agent may have («всегда с собой» or «по запросу»), or None."""
    from . import access
    rules = access.rules_for(ctx.agent_id)
    with SessionLocal() as s:
        skill = s.query(NetSkill).filter(NetSkill.name == name).first()
        if skill is None or not _visible(skill, ctx, rules, _dept_names(s)):
            return None
        row = s.query(NetSkillVersion).filter(NetSkillVersion.skill_id == skill.id, NetSkillVersion.version == version,
                                              NetSkillVersion.status == "published").first()
        return (row.blob, row.sha256) if row is not None else None


def take(ctx, name: str, why: str = "") -> dict:
    """The agent takes an item «по запросу» for a task: the synchronizer installs it with the next sync."""
    item = next((i for i in catalog(ctx) if i["name"] == name), None)
    if item is None:
        raise HubError("Этого нет среди доступного агенту в хабе")
    if item["delivery"] == "always":
        return {"name": name, "kind": item["kind"], "version": item["version"], "status": "always",
                "message": "Это и так всегда с агентом"}
    with SessionLocal() as s:
        if not s.query(NetHubPick).filter(NetHubPick.agent_id == ctx.agent_id, NetHubPick.name == name).first():
            s.add(NetHubPick(agent_id=ctx.agent_id, name=name, why=(why or "").strip()[:300]))
            journal.log("hub.take", f"{item['kind']}:{name}", {"версия": item["version"], "зачем": (why or "")[:200]},
                        actor=ctx.name, actor_kind="agent", agent_id=ctx.agent_id, session=s)
            s.commit()
    return {"name": name, "kind": item["kind"], "version": item["version"], "status": "taken",
            "message": "Взято: синхронизатор установит при следующей синхронизации" +
                       ("; текст навыка уже можно взять через hub_get" if item["kind"] == "skill" else "")}


def drop(ctx, name: str) -> dict:
    with SessionLocal() as s:
        row = s.query(NetHubPick).filter(NetHubPick.agent_id == ctx.agent_id, NetHubPick.name == name).first()
        if row is not None:
            s.delete(row)
            journal.log("hub.drop", name, None, actor=ctx.name, actor_kind="agent", agent_id=ctx.agent_id, session=s)
            s.commit()
    return {"name": name, "status": "dropped"}


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
