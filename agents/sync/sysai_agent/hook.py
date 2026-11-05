"""«sysai-agent hook»: Hermes asks it before each of its own tools that touch the computer — a pre_tool_call shell
hook, fail closed (no answer is a refusal). The call is checked by the same signed rules as the hub MCP servers
(access.json, the key pinned at connection): commands (on or off, the forbidden patterns), folders (read, write,
forbidden; «*» — the whole computer), the internet (off, a list of sites, all). The SYSAI folder, the synchronizer and
SYSAI's files in the Hermes profile belong to the synchronizer: the agent neither writes there nor stops it, so the
guard stays as SYSAI set it.

Commands and code run with the rights of the person: they are checked by their text (the forbidden patterns, the
guarded places), not by what they will touch — whoever must not reach a folder by a command gets no commands.

Wire (Hermes agent/shell_hooks.py): stdin — {"hook_event_name", "tool_name", "tool_input", "session_id", "cwd",
"extra"} in UTF-8; stdout — {"action": "block", "message": …} refuses, nothing — allowed. Which tools come here is
the matcher in the configuration SYSAI made (app/net/access.py, hook_tools)."""
import datetime as dt
import hashlib
import json
import ntpath
import os
import posixpath
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

from . import fsutil, paths, rules as R, signing

ALLOWLIST = "shell-hooks-allowlist.json"
OURS_IN_PROFILE = ("config.yaml", ".env", "SOUL.md", ALLOWLIST)  # what SYSAI keeps in the Hermes profile
LOG_LIMIT = 1_000_000
CODE = ("terminal", "execute_code")
NEEDS_COMMANDS = ("process_manage", "computer_use")
FILE_READ = ("read_file", "search_files")
FILE_WRITE = ("write_file", "patch")
# the synchronizer itself: only asking it is free; Hermes's settings and hooks — by SYSAI only
_SELF = re.compile(r"\bsysai-agentw?(?:\.exe)?\b(?!\s+(?:status|version)\b)", re.I)
_HERMES_SETTINGS = re.compile(r"\bhermes(?:\.exe)?\b[^\n|;&]*\b(?:config|hooks)\b", re.I)
SETTINGS_NOTE = "папку SYSAI, синхронизатор и настройки агента меняет только SYSAI"


def load_rules(home: Path):
    """The signed rules: trusted when access.json is the one the synchronizer verified (its sha256 in state.json) or
    its signature checks with the key pinned at connection; otherwise None."""
    home = Path(home)
    try:
        raw = (home / "access.json").read_bytes()
        signed = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(signed, dict) or not isinstance(signed.get("doc"), dict):
        return None
    state = fsutil.read_json(home / "state.json", {}) or {}
    if state.get("access_sha") != hashlib.sha256(raw).hexdigest():
        agent = fsutil.read_json(home / "agent.json", {}) or {}
        if not agent.get("public_key") or not signing.verify(signed, agent["public_key"], agent.get("key_id", "")):
            return None
    rules = signed["doc"].get("rules")
    return rules if isinstance(rules, dict) else None


def _values(obj, keys: tuple):
    """Every string under one of the keys, at any depth (a tool may take a batch: «items»)."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in keys and isinstance(v, str):
                yield v
            elif k in keys and isinstance(v, list):
                yield from (x for x in v if isinstance(x, str))
            else:
                yield from _values(v, keys)
    elif isinstance(obj, list):
        for v in obj:
            yield from _values(v, keys)


def full_path(path: str, cwd: str = "") -> str:
    """As the tool will take it: «~» and variables expanded, a relative path from the folder Hermes works in."""
    text = os.path.expandvars(os.path.expanduser(str(path or "").strip()))
    if not text:
        return ""
    if text.startswith("file://"):
        text = text[len("file://"):].lstrip("/") if paths.WINDOWS else text[len("file://"):]
    flavour = ntpath if paths.WINDOWS else posixpath
    if flavour.isabs(text) or (paths.WINDOWS and re.match(r"^[A-Za-z]:", text)):
        return text
    return flavour.join(cwd or os.getcwd(), text)


def _norm(path) -> str:
    return R._norm(path)


def _inside(target: str, root) -> bool:
    root = _norm(root)
    return bool(root) and (target == root or target.startswith(root + "\\"))


def guarded_path(path: str, home: Path, profile_dir) -> bool:
    """A path only the synchronizer writes: the SYSAI folder (but the skills the agent proposes) and SYSAI's files
    in the Hermes profile."""
    target = _norm(path)
    if not target:
        return False
    home = Path(home)
    if _inside(target, home / "proposals"):
        return False
    if _inside(target, home):
        return True
    return bool(profile_dir) and any(_inside(target, Path(profile_dir) / name) for name in OURS_IN_PROFILE)


def touches_guard(text: str, home: Path, profile_dir) -> bool:
    """A command or code that names the guarded places or manages the synchronizer or Hermes's settings."""
    low = re.sub(r"\\+", r"\\", (text or "").replace("/", "\\")).lower()
    marks = [_norm(home), "appdata\\local\\sysai", "localappdata%\\sysai", "localappdata\\sysai"]
    if profile_dir:
        profile_dir = Path(profile_dir)
        if profile_dir.parent.name == "profiles":  # a named profile is SYSAI's whole: «hermes\profiles\<agent>»
            marks += [_norm(profile_dir), _norm(Path("profiles") / profile_dir.name)]
        else:
            marks += [_norm(profile_dir / name) for name in OURS_IN_PROFILE]
    if any(m and m in low for m in marks):
        return True
    return bool(_SELF.search(text or "") or _HERMES_SETTINGS.search(text or ""))


def _host(url: str) -> str:
    return (urlparse(str(url)).hostname or str(url))[:80]


def check(rules: dict, tool: str, args, cwd: str = "", home: Path | None = None, profile_dir=None) -> str:
    """Why the call is refused ("" — allowed)."""
    args = args if isinstance(args, dict) else {}
    home = Path(home or paths.home())
    commands = rules.get("commands") or {}
    mode = (rules.get("internet") or {}).get("mode") or "off"
    if tool in CODE:
        if not commands.get("run"):
            return "команды и код на ПК агенту не разрешены (SYSAI → Доступы → Команды на ПК)"
        for text in _values(args, ("command", "code")):
            if tool == "terminal":
                pattern = R.command_denied(rules, text)
                if pattern:
                    return f"команда попадает под запрет «{pattern}» (SYSAI → Доступы → Команды на ПК)"
            if touches_guard(text, home, profile_dir):
                return SETTINGS_NOTE
        return ""
    if tool in NEEDS_COMMANDS:
        return "" if commands.get("run") else "без «Команд на ПК» агенту это не разрешено (SYSAI → Доступы)"
    if tool in FILE_WRITE:
        for path in _values(args, ("path",)):
            target = full_path(path, cwd)
            if guarded_path(target, home, profile_dir):
                return SETTINGS_NOTE
            if not R.path_allowed(rules, target, write=True):
                return f"запись в {target[:160]} агенту не разрешена (SYSAI → Доступы → Папки)"
        return ""
    if tool in FILE_READ:
        for path in list(_values(args, ("path",))) or ["."]:
            target = full_path(path, cwd)
            if not R.path_allowed(rules, target):
                return f"папка {target[:160]} агенту не разрешена (SYSAI → Доступы → Папки)"
        return ""
    if tool == "vision_analyze":
        for image in _values(args, ("image_url",)):
            if image.startswith(("http://", "https://")):
                if not R.site_allowed(rules, image):
                    return f"сайт {_host(image)} не в разрешённых (SYSAI → Доступы → Интернет)"
            elif not image.startswith("data:") and not R.path_allowed(rules, full_path(image, cwd)):
                return f"папка {full_path(image, cwd)[:160]} агенту не разрешена (SYSAI → Доступы → Папки)"
        return ""
    if tool == "web_search":
        if mode == "all":
            return ""
        if mode == "list":
            return ("поиск в интернете закрыт: агенту открыты только сайты из списка (SYSAI → Доступы → Интернет) — "
                    "откройте нужный адрес")
        return "интернет агенту закрыт (SYSAI → Доступы → Интернет)"
    if tool == "web_extract":
        urls = [u for u in _values(args, ("urls", "url")) if u.strip()]
        bad = [u for u in urls if not R.site_allowed(rules, u)]
        if bad:
            return "сайты не в разрешённых: " + ", ".join(dict.fromkeys(_host(u) for u in bad[:5])) + \
                " (SYSAI → Доступы → Интернет)"
        return ""
    if tool.startswith("browser_"):
        return "" if mode == "all" else "браузер агенту открыт только при «Интернет: весь» (SYSAI → Доступы → Интернет)"
    return ""


def _log(home: Path, tool: str, reason: str, args) -> None:
    try:
        path = Path(home) / "logs" / "hook.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > LOG_LIMIT:
            os.replace(path, path.with_name("hook.log.1"))
        what = json.dumps(args, ensure_ascii=False, default=str)[:300]
        stamp = dt.datetime.now().isoformat(timespec="seconds")
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"{stamp} запрещено {tool}: {reason} · {what}\n")
    except OSError:
        pass


def main(home: Path | None = None, stdin=None, stdout=None) -> int:
    """One question of Hermes: the verdict on stdout, always exit code 0 (a crash is a refusal: fail_closed)."""
    home = Path(home or paths.home())
    raw = (stdin if stdin is not None else sys.stdin.buffer).read()
    try:
        payload = json.loads(raw.decode("utf-8-sig") if isinstance(raw, bytes) else (raw or "{}"))
    except ValueError:
        payload = {}
    payload = payload if isinstance(payload, dict) else {}
    tool = str(payload.get("tool_name") or "")
    args = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    try:
        rules = load_rules(home)
        if rules is None:
            reason = ("на этом компьютере нет подписанных доступов SYSAI: синхронизатор их ещё не получил "
                      "(sysai-agent sync) или файл повреждён")
        else:
            agent = fsutil.read_json(home / "agent.json", {}) or {}
            profile_dir = paths.hermes_profile_dir(agent) if agent.get("agent") else None
            reason = check(rules, tool, args, str(payload.get("cwd") or ""), home, profile_dir)
    except Exception as exc:  # noqa: BLE001  a check that broke must still answer: refused
        reason = f"проверка SYSAI не удалась: {exc}"
    if reason:
        out = json.dumps({"action": "block", "message": "SYSAI: " + reason}, ensure_ascii=False).encode("utf-8")
        target = stdout if stdout is not None else sys.stdout.buffer
        target.write(out)
        target.flush()
        _log(home, tool, reason, args)
    return 0
