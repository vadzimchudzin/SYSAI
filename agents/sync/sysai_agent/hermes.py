"""The signed configuration applied to the agent's Hermes profile: in config.yaml only the keys SYSAI owns (the rest
is the person's), the token in the profile's .env, SOUL.md — the originals kept aside once. The gateway of the
profile is restarted when something changed and it runs (Hermes itself refuses while a task is live: then later)."""
import copy
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import yaml

from . import fsutil, paths

# whole keys SYSAI owns; and parts of shared sections
OWNED = ("model", "fallback_providers", "delegation", "approvals", "memory", "mcp_servers")
OWNED_PARTS = {"providers": ("sysai",), "auxiliary": ("vision", "compression"), "skills": ("external_dirs", "create_dir"),
               "security": ("redact_secrets",), "hooks": ("pre_tool_call",),
               # the size of a conversation is its cost: when it is squeezed and how big a result of a tool may be
               "compression": ("enabled", "threshold_tokens"), "tool_budget": ("mcp_result_size_chars",),
               "tool_output": ("max_bytes",),
               # the schemas of MCP tools go to the model when one is needed, on every version of Hermes alike
               "tools": ("tool_search",)}
ENV_MARK = "# SYSAI: строки ниже ведёт синхронизатор"
CONFIG_HEAD = ("# model, delegation, fallback_providers, approvals, memory, mcp_servers, providers.sysai, auxiliary.vision,\n"
               "# auxiliary.compression, skills.external_dirs, skills.create_dir, security.redact_secrets,\n"
               "# hooks.pre_tool_call, compression.enabled, compression.threshold_tokens,\n"
               "# tool_budget.mcp_result_size_chars, tool_output.max_bytes, tools.tool_search ведёт SYSAI (синхронизатор перезапишет их\n"
               "# правки); остальное — ваше.\n")
CONFIG_BACKUP = "config.before-sysai.yaml"
# Hermes runs a shell hook only after consent per (event, command); the synchronizer gives it for its own hook
ALLOWLIST = "shell-hooks-allowlist.json"
HOOK_EVENT = "pre_tool_call"
SOUL_BACKUP = "SOUL.before-sysai.md"
NO_WINDOW = 0x08000000 if paths.WINDOWS else 0  # CREATE_NO_WINDOW: no console flashing from the background


def expand(obj, home: Path, launcher: str = ""):
    """${SYSAI_HOME} becomes the real folder (Hermes need not know it); «sysai-agent» — the full path of the launcher,
    so the MCP servers start whatever PATH Hermes passes. ${SYSAI_AGENT_TOKEN} stays: Hermes takes it from .env."""
    if isinstance(obj, dict):
        out = {k: expand(v, home, launcher) for k, v in obj.items()}
        command = out.get("command")
        if launcher and command == "sysai-agent":
            out["command"] = launcher
        elif launcher and isinstance(command, str) and command.startswith("sysai-agent "):  # a hook: one command line
            out["command"] = f'"{launcher}"' + command[len("sysai-agent"):]
        return out
    if isinstance(obj, list):
        return [expand(v, home, launcher) for v in obj]
    if isinstance(obj, str):
        return obj.replace("${SYSAI_HOME}", str(home))
    return obj


def merge(current, ours: dict) -> dict:
    out = copy.deepcopy(current) if isinstance(current, dict) else {}
    out.pop("sysai", None)  # SYSAI's own part lives in sysai.json, Hermes has no such key
    for key in OWNED:
        if key in ours:
            out[key] = copy.deepcopy(ours[key])
        else:
            out.pop(key, None)
    for key, parts in OWNED_PARTS.items():
        section = dict(out[key]) if isinstance(out.get(key), dict) else {}
        theirs = ours.get(key) if isinstance(ours.get(key), dict) else {}
        for part in parts:
            if part in theirs:
                section[part] = copy.deepcopy(theirs[part])
            else:
                section.pop(part, None)
        if section:
            out[key] = section
        else:
            out.pop(key, None)
    return out


def _env_value(value: str) -> str:
    if re.fullmatch(r"[\w\-.:/\\@+]+", value):
        return value
    if "'" not in value:
        return f"'{value}'"
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def env_text(current: str, values: dict) -> str:
    keep = []
    for line in current.splitlines():
        key = line.split("=", 1)[0].strip()
        key = key[len("export "):].strip() if key.startswith("export ") else key
        if line.strip() == ENV_MARK or ("=" in line and key in values):
            continue
        keep.append(line)
    while keep and not keep[-1].strip():
        keep.pop()
    lines = keep + ([""] if keep else []) + [ENV_MARK] + [f"{k}={_env_value(str(v))}" for k, v in values.items()]
    return "\n".join(lines) + "\n"


def apply(profile_dir: Path, harness_config: dict, soul: str, home: Path, token: str, server: str,
          launcher: str = "") -> list:
    """What changed: «config.yaml», «.env», «SOUL.md»."""
    profile_dir = Path(profile_dir)
    profile_dir.mkdir(parents=True, exist_ok=True)
    changed = []
    ours = expand({k: v for k, v in harness_config.items() if k != "sysai"}, home, launcher)
    path = profile_dir / "config.yaml"
    text = fsutil.read_text(path)
    try:
        current = yaml.safe_load(text) if text.strip() else {}
    except yaml.YAMLError:
        current = None  # broken by hand: kept aside, written anew
    if not isinstance(current, dict):
        current = {}
    merged = merge(current, ours)
    if merged != current or not path.exists():
        if text.strip() and not (profile_dir / CONFIG_BACKUP).exists():
            fsutil.atomic_write(profile_dir / CONFIG_BACKUP, text)
        fsutil.atomic_write(path, CONFIG_HEAD + yaml.safe_dump(merged, allow_unicode=True, sort_keys=False,
                                                                default_flow_style=False, width=1000))
        changed.append("config.yaml")
    if allow_hooks(profile_dir, merged):
        changed.append(ALLOWLIST)
    env_path = profile_dir / ".env"
    env_now = fsutil.read_text(env_path)
    env_new = env_text(env_now, {"SYSAI_AGENT_TOKEN": token, "SYSAI_HOME": str(home), "SYSAI_URL": server})
    if env_new != env_now:
        fsutil.atomic_write(env_path, env_new, private=True)
        changed.append(".env")
    if soul.strip():
        soul_path = profile_dir / "SOUL.md"
        now = fsutil.read_text(soul_path)
        if now != soul:
            if now.strip() and not (profile_dir / SOUL_BACKUP).exists():
                fsutil.atomic_write(profile_dir / SOUL_BACKUP, now)
            fsutil.atomic_write(soul_path, soul)
            changed.append("SOUL.md")
    return changed


def our_hooks(config: dict) -> list:
    """The command lines of the synchronizer's hooks in a Hermes configuration."""
    hooks = config.get("hooks") if isinstance(config, dict) else None
    entries = hooks.get(HOOK_EVENT) if isinstance(hooks, dict) else None
    return [e["command"] for e in entries or [] if isinstance(e, dict) and isinstance(e.get("command"), str)
            and "sysai-agent" in e["command"]]


def allow_hooks(profile_dir: Path, config: dict) -> bool:
    """The consent Hermes asks for at the first run of a shell hook — given for the synchronizer's own hook only (the
    person's consents stay; ours that are no longer in the configuration go). True — the file changed."""
    path = Path(profile_dir) / ALLOWLIST
    data = fsutil.read_json(path, None)
    data = data if isinstance(data, dict) else {}
    approvals = data.get("approvals") if isinstance(data.get("approvals"), list) else []
    ours = our_hooks(config)

    def stale(e):
        return (isinstance(e, dict) and e.get("event") == HOOK_EVENT and "sysai-agent" in str(e.get("command") or "")
                and e.get("command") not in ours)
    keep = [e for e in approvals if not stale(e)]
    have = {e.get("command") for e in keep if isinstance(e, dict) and e.get("event") == HOOK_EVENT}
    import datetime as dt
    stamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    keep += [{"event": HOOK_EVENT, "command": c, "approved_at": stamp, "script_mtime_at_approval": None,
              "approved_by": "SYSAI"} for c in ours if c not in have]
    if keep == approvals:
        return False
    fsutil.atomic_write(path, json.dumps({**data, "approvals": keep}, ensure_ascii=False, indent=2))
    return True


def own_skills(profile_dir: Path) -> set:
    """Names of the skills the profile has of its own (the folders with SKILL.md under skills/)."""
    root = Path(profile_dir) / "skills"
    out = set()
    if root.is_dir():
        for doc in root.rglob("SKILL.md"):
            if len(doc.relative_to(root).parts) <= 4:
                out.add(doc.parent.name)
    return out


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if paths.WINDOWS:
        import ctypes
        kernel = ctypes.windll.kernel32
        handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def _pid_of(record) -> int:
    pid = record.get("pid") if isinstance(record, dict) else record
    try:
        return int(pid)
    except (TypeError, ValueError):
        return 0


def _vbs_quote(value: str) -> str:
    return '"' + str(value).replace('"', '""') + '"'


def _replace_managed_block(text: str, begin: str, end: str, block: str) -> tuple[str, bool]:
    if begin in text:
        start = text.index(begin)
        stop = text.find(end, start)
        if stop < 0:
            raise ValueError("incomplete SYSAI ACP gateway launcher block")
        stop += len(end)
        updated = text[:start] + block + text[stop:]
        return updated, updated != text
    return "", False


def _patch_gateway_vbs(text: str, shim_dir: Path, sysai_home: Path) -> tuple[str, bool]:
    begin = "' SYSAI ACP MCP PATCH BEGIN"
    end = "' SYSAI ACP MCP PATCH END"
    q = _vbs_quote
    block = "\r\n".join((
        begin,
        f"env.Item({q('SYSAI_HERMES_ACP_PATCH')}) = {q('1')}",
        f"env.Item({q('SYSAI_HOME')}) = {q(str(sysai_home))}",
        end,
    ))
    updated, changed = _replace_managed_block(text, begin, end, block)
    if begin not in text:
        anchor = 'Set env = sh.Environment("PROCESS")'
        if text.count(anchor) != 1:
            raise ValueError("Hermes gateway VBS launcher has no supported environment insertion point")
        updated = text.replace(anchor, anchor + "\r\n" + block, 1)
        changed = True

    priority_begin = "' SYSAI ACP MCP PATH PRIORITY BEGIN"
    priority_end = "' SYSAI ACP MCP PATH PRIORITY END"
    priority = "\r\n".join((
        priority_begin,
        f"existing_pp = env.Item({q('PYTHONPATH')})",
        "If Len(existing_pp) > 0 Then",
        f"  env.Item({q('PYTHONPATH')}) = {q(str(shim_dir))} & {q(';')} & existing_pp",
        "Else",
        f"  env.Item({q('PYTHONPATH')}) = {q(str(shim_dir))}",
        "End If",
        priority_end,
    ))
    prioritized, priority_changed = _replace_managed_block(updated, priority_begin, priority_end, priority)
    if priority_begin not in updated:
        anchor = "sh.CurrentDirectory = "
        if updated.count(anchor) != 1:
            raise ValueError("Hermes gateway VBS launcher has no supported final PYTHONPATH insertion point")
        prioritized = updated.replace(anchor, priority + "\r\n" + anchor, 1)
        priority_changed = True
    return prioritized, changed or priority_changed


def _patch_gateway_cmd(text: str, shim_dir: Path, sysai_home: Path) -> tuple[str, bool]:
    begin = "rem SYSAI ACP MCP PATCH BEGIN"
    end = "rem SYSAI ACP MCP PATCH END"
    block = "\r\n".join((
        begin,
        'set "SYSAI_HERMES_ACP_PATCH=1"',
        f'set "SYSAI_HOME={sysai_home}"',
        end,
    ))
    updated, changed = _replace_managed_block(text, begin, end, block)
    if begin not in text:
        lines = text.splitlines(keepends=True)
        indexes = [i for i, line in enumerate(lines) if line.strip().lower().startswith('set "hermes_home=')]
        if len(indexes) != 1:
            raise ValueError("Hermes gateway CMD launcher has no supported HERMES_HOME line")
        insert_at = indexes[0] + 1
        lines[insert_at:insert_at] = [line + "\r\n" for line in block.split("\r\n")]
        updated = "".join(lines)
        changed = True

    priority_begin = "rem SYSAI ACP MCP PATH PRIORITY BEGIN"
    priority_end = "rem SYSAI ACP MCP PATH PRIORITY END"
    priority = "\r\n".join((
        priority_begin,
        f'set "PYTHONPATH={shim_dir};%PYTHONPATH%"',
        priority_end,
    ))
    prioritized, priority_changed = _replace_managed_block(updated, priority_begin, priority_end, priority)
    if priority_begin not in updated:
        lines = prioritized.splitlines(keepends=True) if prioritized else updated.splitlines(keepends=True)
        indexes = [i for i, line in enumerate(lines)
                   if "gateway run" in line.casefold() and not line.lstrip().lower().startswith("rem ")]
        if len(indexes) != 1:
            raise ValueError("Hermes gateway CMD launcher has no supported gateway command insertion point")
        insert_at = indexes[0]
        lines[insert_at:insert_at] = [line + "\r\n" for line in priority.split("\r\n")]
        prioritized = "".join(lines)
        priority_changed = True
    return prioritized, changed or priority_changed


def _same_path_in_text(text: str, path: Path) -> bool:
    expected = os.path.normcase(os.path.normpath(str(path))).replace("/", "\\").casefold()
    actual = text.replace("/", "\\").casefold()
    return expected in actual


def _patch_gateway_launchers(home: Path, profile_dir: Path | None = None) -> bool:
    """Wire the shim into this Hermes root's generated Windows Gateway launchers.

    ACP is only a viewer when a Gateway is already running. Its session MCP
    connections and tool calls live in that Gateway process, so patching only
    the Desktop child would miss the actual runtime. Only generated launchers
    under this Hermes root are touched; originals get one-time backups.
    """
    if not paths.WINDOWS:
        return False
    if profile_dir is None:
        hermes_root = paths.hermes_root()
    else:
        profile_dir = Path(profile_dir)
        hermes_root = profile_dir.parent.parent if profile_dir.parent.name == "profiles" else profile_dir
    folder = Path(hermes_root) / "gateway-service"
    if not folder.is_dir():
        return False
    shim_dir = Path(home) / "hermes-acp-patch"
    changed = False
    for launcher in [*folder.glob("*.vbs"), *folder.glob("*.cmd")]:
        try:
            raw = launcher.read_bytes()
            is_vbs = launcher.suffix.lower() == ".vbs"
            text = raw.decode("utf-16" if is_vbs else "utf-8-sig")
        except (OSError, UnicodeError):
            continue
        if not _same_path_in_text(text, Path(hermes_root)) or "gateway run" not in text.casefold():
            continue
        try:
            updated, did_change = (_patch_gateway_vbs(text, shim_dir, Path(home)) if is_vbs
                                   else _patch_gateway_cmd(text, shim_dir, Path(home)))
        except ValueError:
            continue
        if not did_change:
            continue
        backup = launcher.with_name(launcher.name + ".before-sysai")
        if not backup.exists():
            fsutil.atomic_write(backup, raw)
        fsutil.atomic_write(launcher, updated.encode("utf-16") if is_vbs else updated.encode("utf-8"))
        changed = True
    return changed


def stage_acp_patch(home: Path, profile_dir: Path | None = None) -> bool:
    """Stage the shim and wire it into Hermes' Windows Gateway launcher.

    Desktop ACP also receives the shim environment as a cold-start fallback.
    For an already-running Gateway, the generated VBS/CMD launcher must carry
    PYTHONPATH into the Gateway process before Python imports sitecustomize.
    True means the staged file or a supported launcher changed.
    """
    source = Path(__file__).with_name("acp_sitecustomize.py")
    target = Path(home) / "hermes-acp-patch" / "sitecustomize.py"
    content = source.read_text(encoding="utf-8")
    changed = False
    if not target.is_file() or target.read_text(encoding="utf-8") != content:
        fsutil.atomic_write(target, content)
        changed = True
    return _patch_gateway_launchers(Path(home), profile_dir) or changed


def gateway_running(profile_dir: Path) -> bool:
    """A gateway serves this profile: its own (gateway.pid in the profile) or — Hermes 0.22 and later — the one
    multiplexing gateway of the computer, started from the default profile: it keeps gateway.pid and
    gateway_state.json (with «served_profiles») in the Hermes root, the profiles have no gateway.pid of their own."""
    profile_dir = Path(profile_dir)
    if _alive(_pid_of(fsutil.read_json(profile_dir / "gateway.pid", None))):
        return True
    root = profile_dir.parent.parent if profile_dir.parent.name == "profiles" else profile_dir
    state = fsutil.read_json(root / "gateway_state.json", None)
    if not isinstance(state, dict) or str(state.get("gateway_state") or "") == "stopped":
        return False
    if root != profile_dir:
        served = state.get("served_profiles")
        if not isinstance(served, list) or profile_dir.name not in served:
            return False  # the gateway of the default profile alone (an older Hermes, or this profile not served)
    return _alive(_pid_of(fsutil.read_json(root / "gateway.pid", None))) or _alive(_pid_of(state))


def _run(args: list, timeout: int = 180):
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                          creationflags=NO_WINDOW)


def restart(profile: str, profile_dir: Path) -> tuple[bool, str]:
    """(done, what happened). Not running — nothing to restart: the new settings are read at the start."""
    if not gateway_running(profile_dir):
        return True, "шлюз Hermes не запущен: новые настройки он возьмёт при запуске"
    exe = shutil.which("hermes")
    if not exe:
        return False, "не найдена команда hermes"
    try:
        r = _run([exe, "-p", profile, "gateway", "restart"])
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"перезапуск Hermes не удался: {exc}"
    said = (r.stdout + "\n" + r.stderr).strip()[-400:]
    return r.returncode == 0, said or ("перезапущен" if r.returncode == 0 else f"код {r.returncode}")


def version() -> str:
    exe = shutil.which("hermes")
    if not exe:
        return ""
    try:
        r = _run([exe, "--version"], timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    m = re.search(r"\d+\.\d+(\.\d+)?", r.stdout or "")
    return m.group(0) if m else (r.stdout or "").strip()[:40]


def profile_info(profile_dir: Path) -> dict:
    """For «sysai-agent status»: what SYSAI put into the profile."""
    cfg = fsutil.read_text(Path(profile_dir) / "config.yaml")
    try:
        data = yaml.safe_load(cfg) if cfg.strip() else {}
    except yaml.YAMLError:
        data = {}
    data = data if isinstance(data, dict) else {}
    return {"config": bool(cfg), "model": (data.get("model") or {}).get("default", "") if isinstance(data.get("model"), dict) else "",
            "mcp": sorted((data.get("mcp_servers") or {}).keys()) if isinstance(data.get("mcp_servers"), dict) else [],
            "soul": (Path(profile_dir) / "SOUL.md").exists(), "gateway": gateway_running(profile_dir),
            "hook": bool(our_hooks(data)), "dir": str(profile_dir)}


def dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)
