"""The checks of the signed rules, the same as on the server (app/net/access.py): MCP tools, commands, folders,
sites. What is not allowed is forbidden."""
import fnmatch
import ntpath
import re
from urllib.parse import urlparse

WHOLE_PC = "*"  # in «Папки»: the whole computer


def tool_allowed(rules: dict, server: str, tool: str) -> bool:
    tools = (rules.get("mcp") or {}).get(server) or []
    return "*" in tools or tool in tools


def command_denied(rules: dict, command: str) -> str:
    """The first forbidden pattern the command matches ("" = allowed); case-insensitive, «*» is any text."""
    commands = rules.get("commands") or {}
    if not commands.get("run"):
        return "*"
    text = " ".join((command or "").split()).lower()
    for pattern in commands.get("deny") or []:
        if fnmatch.fnmatchcase(text, " ".join(str(pattern).split()).lower()):
            return pattern
    return ""


def _norm(path) -> str:
    text = str(path or "").strip()
    if not text:
        return ""
    text = ntpath.normpath(text.replace("/", "\\"))  # «..» collapsed: no way out of a folder
    return re.sub(r"\\+", r"\\", text).rstrip("\\").lower()


def path_allowed(rules: dict, path, write: bool = False) -> bool:
    """Inside a «read» (or «write») folder and outside every forbidden one; Windows paths, any case; «*» — the whole
    computer."""
    target = _norm(path)
    if not target:
        return False
    folders = rules.get("folders") or {}

    def inside(root):
        if str(root or "").strip() == WHOLE_PC:
            return True
        root = _norm(root)
        return bool(root) and (target == root or target.startswith(root + "\\"))
    if any(inside(p) for p in folders.get("deny") or []):
        return False
    roots = (folders.get("write") or []) if write else (folders.get("read") or []) + (folders.get("write") or [])
    return any(inside(p) for p in roots)


def site_allowed(rules: dict, url: str) -> bool:
    internet = rules.get("internet") or {}
    mode = internet.get("mode") or "off"
    if mode == "all":
        return True
    if mode != "list":
        return False
    host = (urlparse(url).hostname or "").lower().rstrip(".")

    def site(s):
        return re.sub(r"^[a-z]+://", "", str(s).strip().lower()).split("/")[0].lstrip("*.").rstrip(".")
    sites = [site(s) for s in internet.get("sites") or []]
    return bool(host) and any(s and (host == s or host.endswith("." + s)) for s in sites)
