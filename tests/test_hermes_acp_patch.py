"""Regression tests for the Hermes ACP MCP session-scope compatibility shim."""
from __future__ import annotations

import builtins
import contextvars
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

SYNC = Path(__file__).resolve().parents[1] / "agents" / "sync"
sys.path.insert(0, str(SYNC))

from sysai_agent import acp_sitecustomize, hermes  # noqa: E402


SCOPE_SOURCE = '''from tools.mcp_tool_common import _core

def _server_key(name, scope=None, *, current=True):
    if scope is None and current:
        scope = _core._mcp_registry_scope()
    return name if scope is None else (scope, name)

def _key_name(key):
    return key[1] if isinstance(key, tuple) else key

def _resolve_server_key(name, scope=None, *, current=True):
    if scope is None and current:
        scope = _core._mcp_registry_scope()
    own = _server_key(name, scope, current=False)
    if scope is None or own in _core._servers or own in _core._lazy_server_configs:
        return own
    for key, scopes in _core._server_tool_scopes.items():
        if scope in scopes and _key_name(key) == name and key in _core._servers:
            return key
    return own
'''


HANDLERS_SOURCE = '''from tools.mcp_tool_common import _core

def _trust_gate_check(server_name: str, tool_name: str):
    from tools.mcp_tool_scope import _server_key
    from tools import mcp_tool_costs as _costs
    # Trust and costs are the calling profile's own policy.
    key = _server_key(server_name)
    return key

def _untrusted(server_name: str) -> bool:
    from tools.mcp_tool_scope import _server_key
    key = _server_key(server_name)
    return key
'''


def test_scope_source_patch_is_valid_and_idempotent():
    patched, changed = acp_sitecustomize._patch_scope_source(SCOPE_SOURCE)
    assert changed
    assert acp_sitecustomize._SCOPE_MARKER in patched
    compile(patched, "mcp_tool_scope.py", "exec")
    again, changed_again = acp_sitecustomize._patch_scope_source(patched)
    assert again == patched and not changed_again


def test_session_scope_resolves_only_the_serving_profiles_live_connection():
    patched, _ = acp_sitecustomize._patch_scope_source(SCOPE_SOURCE)
    session_scope = contextvars.ContextVar("test_session_scope", default=None)
    profile_key = ("profile-a", "plc-kit")
    other_key = ("profile-b", "plc-kit")
    core = SimpleNamespace(
        _mcp_registry_scope=lambda: session_scope.get() or "profile-a",
        _servers={profile_key: object(), other_key: object()},
        _lazy_server_configs={},
        _server_tool_scopes={},
    )

    @contextmanager
    def scope(value):
        token = session_scope.set(value)
        try:
            yield
        finally:
            session_scope.reset(token)

    registry_module = SimpleNamespace(
        current_session_tool_scope=lambda: session_scope.get(),
        session_tool_scope=scope,
    )
    original_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "tools.mcp_tool_common":
            return SimpleNamespace(_core=core)
        if name == "tools.registry":
            return registry_module
        return original_import(name, globals, locals, fromlist, level)

    namespace = {"__builtins__": dict(vars(builtins), __import__=fake_import)}
    exec(compile(patched, "mcp_tool_scope.py", "exec"), namespace)
    with scope("editor-session:abc"):
        assert namespace["_resolve_server_key"]("plc-kit") == profile_key
    assert namespace["_resolve_server_key"]("plc-kit", scope="profile-b", current=False) == other_key


def test_trust_source_patch_inherits_profile_policy_but_keeps_session_override():
    patched, changed = acp_sitecustomize._patch_handlers_source(HANDLERS_SOURCE)
    assert changed
    assert acp_sitecustomize._TRUST_MARKER in patched
    assert "key = _caller_trust_key(server_name)" in patched
    compile(patched, "mcp_tool_handlers.py", "exec")
    again, changed_again = acp_sitecustomize._patch_handlers_source(patched)
    assert again == patched and not changed_again

    session_scope = contextvars.ContextVar("test_session_scope", default=None)
    profile_key = ("profile-a", "plc-kit")
    session_key = ("editor-session:abc", "plc-kit")
    core = SimpleNamespace(_server_trust_levels={profile_key: "trusted"})

    def server_key(name):
        scope_value = session_scope.get()
        return (scope_value, name) if scope_value is not None else ("profile-a", name)

    @contextmanager
    def scope(value):
        token = session_scope.set(value)
        try:
            yield
        finally:
            session_scope.reset(token)

    registry_module = SimpleNamespace(
        current_session_tool_scope=lambda: session_scope.get(),
        session_tool_scope=scope,
    )
    scope_module = SimpleNamespace(_server_key=server_key)
    original_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "tools.mcp_tool_common":
            return SimpleNamespace(_core=core)
        if name == "tools.mcp_tool_scope":
            return scope_module
        if name == "tools.registry":
            return registry_module
        if name == "tools":
            return SimpleNamespace(mcp_tool_costs=SimpleNamespace())
        return original_import(name, globals, locals, fromlist, level)

    namespace = {"__builtins__": dict(vars(builtins), __import__=fake_import)}
    exec(compile(patched, "mcp_tool_handlers.py", "exec"), namespace)
    with scope("editor-session:abc"):
        assert namespace["_caller_trust_key"]("plc-kit") == profile_key
        core._server_trust_levels[session_key] = "untrusted"
        assert namespace["_caller_trust_key"]("plc-kit") == session_key
    assert namespace["_caller_trust_key"]("unknown") == ("profile-a", "unknown")


def test_sync_stages_the_sitecustomize_shim_in_the_sysai_home(tmp_path):
    changed = hermes.stage_acp_patch(tmp_path)
    staged = tmp_path / "hermes-acp-patch" / "sitecustomize.py"
    assert changed and staged.is_file()
    assert staged.read_text(encoding="utf-8") == Path(acp_sitecustomize.__file__).read_text(encoding="utf-8")
    assert not hermes.stage_acp_patch(tmp_path)
