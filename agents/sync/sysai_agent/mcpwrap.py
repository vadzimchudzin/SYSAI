"""«sysai-agent mcp <name>»: a hub MCP server started for Hermes inside a guard. The guard checks the signed rules
(access.json, the key pinned at connection) itself — whatever the harness is told: the server must be allowed,
tools/list shows only the allowed tools, tools/call of others is refused, paths must be in the allowed folders,
commands must not match the forbidden patterns, sites must be allowed. The rules are reloaded when the file changes."""
import json
import os
import re
import shlex
import subprocess
import sys
import threading
from pathlib import Path

from . import fsutil, paths, rules as R, signing

PATH_KEYS = ("path", "file", "dir", "folder", "cwd", "directory")
COMMAND_KEYS = ("command", "cmd", "commandline", "command_line", "shell", "powershell")
WRITE_TOKENS = {"write", "save", "delete", "remove", "move", "rename", "create", "put", "edit", "update", "export",
                "out", "output", "dest", "destination", "target"}


def _tokens(name: str) -> set:
    """«saveProject», «out_dir», «files-write» → {"save", "project"}, {"out", "dir"}, {"files", "write"}."""
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name or "")
    return {t for t in re.split(r"[^A-Za-z0-9]+", spaced.lower()) if t}


def _path_key(key: str) -> bool:
    low = (key or "").lower()
    return any(low.startswith(w) or low.endswith(w) for w in PATH_KEYS)


def _looks_like_path(value: str, key: str) -> bool:
    v = value.strip()
    if len(v) < 2:
        return False
    if v[1:3] in (":\\", ":/") or v.startswith(("\\\\", "//", "/", "~")) or "\\" in v:
        return True
    return ("/" in v or v.startswith(".")) and _path_key(key)


class Guard:
    def __init__(self, home: Path, name: str, access_file: Path):
        self.home, self.name, self.file = Path(home), name, Path(access_file)
        self.mtime, self.rules = None, None
        self.agent = fsutil.read_json(self.home / "agent.json", {}) or {}

    def load(self) -> dict | None:
        """The rules, reloaded when access.json changed; None — not trusted (no file, broken, another key)."""
        try:
            mtime = self.file.stat().st_mtime
        except OSError:
            self.rules = None
            return None
        if mtime != self.mtime:
            self.mtime = mtime
            signed = fsutil.read_json(self.file, None)
            ok = self.agent.get("public_key") and signing.verify(signed, self.agent["public_key"], self.agent.get("key_id", ""))
            self.rules = signed["doc"]["rules"] if ok else None
        return self.rules

    def server_allowed(self) -> bool:
        rules = self.load()
        return bool(rules) and self.name in (rules.get("mcp") or {})

    def check_call(self, tool: str, args) -> str:
        """Why the call is refused ("" — allowed)."""
        rules = self.load()
        if not rules:
            return "нет подписанных доступов SYSAI (access.json) — синхронизатор ещё не получил их или подпись не сходится"
        if self.name not in (rules.get("mcp") or {}):
            return f"MCP-сервер {self.name} агенту не разрешён"
        if not R.tool_allowed(rules, self.name, tool):
            return f"инструмент {tool} сервера {self.name} агенту не разрешён"
        writes = bool(_tokens(tool) & WRITE_TOKENS)
        for key, value in _strings(args):
            if value.startswith(("http://", "https://")):
                if not R.site_allowed(rules, value):
                    return f"сайт {value[:80]} не в разрешённых"
                continue
            low = key.lower()
            if low in COMMAND_KEYS or low.endswith("command"):
                pattern = R.command_denied(rules, value)
                if pattern:
                    return "команды агенту запрещены" if pattern == "*" else f"команда попадает под запрет «{pattern}»"
                continue
            if _looks_like_path(value, key):
                write = writes or bool(_tokens(key) & WRITE_TOKENS)
                if not R.path_allowed(rules, value, write=write):
                    return f"папка {value[:120]} не разрешена агенту" + (" на запись" if write else "")
        return ""


def _strings(obj, key: str = ""):
    """(key, text) of every string inside the arguments; the key is the nearest name above."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _strings(v, str(k))
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings(v, key)
    elif isinstance(obj, str):
        yield key, obj


def command(spec: dict, folder: Path, python: str = "") -> list:
    """The tool's run line as arguments: «python …» starts with its own environment or the synchronizer's Python."""
    run = (spec or {}).get("run") or ""
    args = shlex.split(run, posix=not paths.WINDOWS)
    args = [a[1:-1] if len(a) > 1 and a[0] == a[-1] == '"' else a for a in args]
    if not args:
        raise ValueError("у инструмента нет команды запуска (run)")
    if args[0].lower() in ("python", "python3", "py", "python.exe", "pythonw"):
        args[0] = python or sys.executable
    elif not os.path.isabs(args[0]) and (folder / args[0]).exists():
        args[0] = str(folder / args[0])
    return args


def _error(msg_id, text: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32001, "message": f"SYSAI: {text}"}}


def serve(name: str, home: Path | None = None, stdin=None, stdout=None) -> int:
    home = Path(home or paths.home())
    stdin = stdin or sys.stdin.buffer
    stdout = stdout or sys.stdout.buffer
    access_file = Path(os.environ.get("SYSAI_ACCESS_FILE") or home / "access.json")
    guard = Guard(home, name, access_file)
    if not guard.server_allowed():
        sys.stderr.write(f"SYSAI: MCP-сервер {name} агенту не разрешён (или нет подписанных доступов)\n")
        return 2
    state = fsutil.read_json(home / "state.json", {}) or {}
    tool = (state.get("tools") or {}).get(name) or {}
    folder = Path(os.environ.get("SYSAI_TOOL_DIR") or home / "tools" / name)
    if not tool or not folder.is_dir():
        sys.stderr.write(f"SYSAI: инструмента {name} нет на компьютере — синхронизатор поставит его из SYSAI_Hub\n")
        return 2
    try:
        args = command(tool.get("spec") or {}, folder, tool.get("python") or "")
    except ValueError as exc:
        sys.stderr.write(f"SYSAI: {exc}\n")
        return 2
    env = {**os.environ, "SYSAI_HOME": str(home), "SYSAI_TOOL_DIR": str(folder)}
    proc = subprocess.Popen(args, cwd=str(folder), stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env,
                            creationflags=0x08000000 if paths.WINDOWS else 0)
    lock = threading.Lock()
    lists = set()  # ids of tools/list requests: their answers are filtered

    def say(obj) -> None:
        with lock:
            stdout.write(json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n")
            stdout.flush()

    def upstream() -> None:
        for line in proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                with lock:
                    stdout.write(line)
                    stdout.flush()
                continue
            if isinstance(msg, dict) and msg.get("id") in lists and isinstance(msg.get("result"), dict):
                lists.discard(msg.get("id"))
                rules = guard.load() or {}
                tools = msg["result"].get("tools")
                if isinstance(tools, list):
                    msg["result"]["tools"] = [t for t in tools if isinstance(t, dict) and
                                              R.tool_allowed(rules, name, str(t.get("name") or ""))]
            say(msg)

    reader = threading.Thread(target=upstream, daemon=True)
    reader.start()
    try:
        for line in stdin:
            if not line.strip():
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            forward = []
            for item in msg if isinstance(msg, list) else [msg]:
                if not isinstance(item, dict):
                    continue
                method = item.get("method")
                if method == "tools/list" and "id" in item:
                    lists.add(item["id"])
                if method == "tools/call":
                    params = item.get("params") if isinstance(item.get("params"), dict) else {}
                    why = guard.check_call(str(params.get("name") or ""), params.get("arguments") or {})
                    if why:
                        if "id" in item:
                            say(_error(item["id"], why))
                        continue
                forward.append(item)
            if forward and proc.poll() is None:
                out = forward if isinstance(msg, list) else forward[0]
                proc.stdin.write(json.dumps(out, ensure_ascii=False).encode("utf-8") + b"\n")
                proc.stdin.flush()
    except (BrokenPipeError, OSError):
        pass
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        reader.join(timeout=5)
    return proc.returncode or 0
