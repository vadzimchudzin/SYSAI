"""Runtime compatibility for Hermes ACP editor sessions.

Loaded only by the SYSAI Desktop ACP child through PYTHONPATH. The two source
transformations mirror the session/profile MCP fix and its trust-key follow-up:
profile connection lookup is visible in a session overlay, and profile trust
metadata remains the caller's policy when the session has no trust record.

No Hermes files are modified on disk. Unsupported upstream source layouts fail
open to the original module and are recorded in the SYSAI log.
"""
from __future__ import annotations

import importlib.machinery
import os
import re
import sys
import threading
from pathlib import Path

_SCOPE_MARKER = "SYSAI ACP session/profile MCP compatibility"
_TRUST_MARKER = "SYSAI ACP session/profile MCP trust compatibility"
_TARGETS = {"tools.mcp_tool_scope", "tools.mcp_tool_handlers"}
_LOG_LOCK = threading.Lock()
_INSTALLED = False


def _patch_scope_source(source: str) -> tuple[str, bool]:
    if _SCOPE_MARKER in source:
        return source, False
    # The upstream fix may already be present without the SYSAI marker.
    if ("if scope == current_session_tool_scope():" in source
            and "profile_key in _core._servers" in source):
        return source, False
    old = (
        "    for key, scopes in _core._server_tool_scopes.items():\n"
        "        if scope in scopes and _key_name(key) == name and key in _core._servers:\n"
        "            return key\n"
        "    return own\n"
    )
    if source.count(old) != 1:
        raise ValueError("Hermes MCP resolver does not match a supported source layout")
    new = (
        "    for key, scopes in _core._server_tool_scopes.items():\n"
        "        if scope in scopes and _key_name(key) == name and key in _core._servers:\n"
        "            return key\n"
        "    from tools.registry import current_session_tool_scope, session_tool_scope\n"
        "    if scope == current_session_tool_scope():\n"
        f"        # {_SCOPE_MARKER}.\n"
        "        with session_tool_scope(None):\n"
        "            profile_key = _resolve_server_key(name)\n"
        "        if profile_key in _core._servers:\n"
        "            return profile_key\n"
        "    return own\n"
    )
    return source.replace(old, new, 1), True


def _function_span(source: str, name: str) -> tuple[int, int]:
    match = re.search(r"^def " + re.escape(name) + r"\(", source, re.MULTILINE)
    if not match:
        raise ValueError(f"Hermes MCP trust function {name} is missing")
    next_function = re.search(r"^(?:async )?def [A-Za-z_]\w*\(", source[match.end():], re.MULTILINE)
    end = match.end() + next_function.start() if next_function else len(source)
    return match.start(), end


def _patch_trust_function(source: str, name: str) -> tuple[str, bool]:
    start, end = _function_span(source, name)
    body = source[start:end]
    if "key = _caller_trust_key(server_name)" in body:
        return source, False
    import_line = "    from tools.mcp_tool_scope import _server_key\n"
    key_line = "    key = _server_key(server_name)\n"
    if body.count(import_line) != 1 or body.count(key_line) != 1:
        raise ValueError(f"Hermes MCP trust function {name} does not match a supported source layout")
    body = body.replace(import_line, "", 1).replace(
        key_line, "    key = _caller_trust_key(server_name)\n", 1)
    return source[:start] + body + source[end:], True


def _patch_handlers_source(source: str) -> tuple[str, bool]:
    if _TRUST_MARKER not in source and "def _caller_trust_key(" not in source:
        anchor = "def _trust_gate_check("
        if source.count(anchor) != 1:
            raise ValueError("Hermes MCP trust gate does not match a supported source layout")
        index = source.index(anchor)
        line_start = source.rfind("\n", 0, index) + 1
        helper = (
            "def _caller_trust_key(server_name: str):\n"
            f"    \"\"\"{_TRUST_MARKER}: ACP sessions inherit the serving profile's trust policy.\"\"\"\n"
            "    from tools.mcp_tool_scope import _server_key\n"
            "    from tools.registry import current_session_tool_scope, session_tool_scope\n"
            "    key = _server_key(server_name)\n"
            "    if key not in _core._server_trust_levels and current_session_tool_scope() is not None:\n"
            "        with session_tool_scope(None):\n"
            "            key = _server_key(server_name)\n"
            "    return key\n\n\n"
        )
        source = source[:line_start] + helper + source[line_start:]
    changed = False
    for name in ("_trust_gate_check", "_untrusted"):
        source, did_change = _patch_trust_function(source, name)
        changed = changed or did_change
    return source, changed


def _patch_source(fullname: str, source: str) -> tuple[str, bool]:
    if fullname == "tools.mcp_tool_scope":
        return _patch_scope_source(source)
    if fullname == "tools.mcp_tool_handlers":
        return _patch_handlers_source(source)
    raise ValueError(f"unexpected module for Hermes ACP patch: {fullname}")


def _log(message: str) -> None:
    home = os.environ.get("SYSAI_HOME", "").strip()
    if not home:
        return
    try:
        path = Path(home) / "logs" / "hermes-acp-patch.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with _LOG_LOCK:
            with path.open("a", encoding="utf-8") as stream:
                stream.write(message.replace("\r", " ").replace("\n", " ")[:800] + "\n")
    except OSError:
        pass


class _PatchLoader:
    def __init__(self, fullname, wrapped):
        self.fullname = fullname
        self.wrapped = wrapped

    def __getattr__(self, name):
        return getattr(self.wrapped, name)

    def create_module(self, spec):
        create = getattr(self.wrapped, "create_module", None)
        return create(spec) if create else None

    def get_source(self, fullname):
        source = self.wrapped.get_source(fullname)
        patched, _ = _patch_source(fullname, source)
        return patched

    def exec_module(self, module):
        try:
            source = self.wrapped.get_source(self.fullname)
            if source is None:
                raise ValueError("Hermes source loader returned no Python source")
            patched, changed = _patch_source(self.fullname, source)
            filename = self.wrapped.get_filename(self.fullname)
            code = compile(patched, filename, "exec", dont_inherit=True)
        except Exception as exc:
            _log(f"{self.fullname}: compatibility patch not applied ({type(exc).__name__}: {exc})")
            self.wrapped.exec_module(module)
            return
        exec(code, module.__dict__)
        _log(f"{self.fullname}: {'SYSAI ACP compatibility applied' if changed else 'compatible upstream implementation detected'}")


class _PatchFinder:
    def find_spec(self, fullname, path=None, target=None):
        if fullname not in _TARGETS:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path, target)
        if spec is not None and spec.loader is not None and not isinstance(spec.loader, _PatchLoader):
            if callable(getattr(spec.loader, "get_source", None)):
                spec.loader = _PatchLoader(fullname, spec.loader)
        return spec


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    if not any(isinstance(item, _PatchFinder) for item in sys.meta_path):
        sys.meta_path.insert(0, _PatchFinder())
    _INSTALLED = True
    _log("SYSAI Hermes ACP MCP compatibility loader active")


if os.environ.get("SYSAI_HERMES_ACP_PATCH") == "1":
    install()
