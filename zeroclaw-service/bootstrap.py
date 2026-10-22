#!/usr/bin/env python3
"""Build a restrictive ZeroClaw config from environment. Secrets stay in env."""
from __future__ import annotations

import json
import os
import pathlib
import re
import sys

APPROVED_TOOLS = (
    "list_approved_tasks",
    "list_runs",
    "enroll_task",
    "get_task_context",
    "prepare_task_action",
    "dispatch_task_action",
    "get_correlated_replies",
    "claim_planning_turn",
    "recover_execution",
)
FORBIDDEN_TOOLS = (
    "exec",
    "shell",
    "browser",
    "web_fetch",
    "http_request",
    "read",
    "write",
    "process",
)


def _need(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"missing required environment variable {name}")
    return value


def _origin(url: str) -> str:
    if not re.match(r"^https://[a-z0-9.-]+(?:/.*)?$", url, re.I):
        raise SystemExit("SYSAI_BASE_URL must be https://host")
    return url.rstrip("/")


def build_config(env: dict[str, str]) -> str:
    base = _origin(env["SYSAI_BASE_URL"])
    mcp_url = f"{base}/execution/mcp"
    model_uri = f"{base}/execution/v1"
    model_id = env.get("SYSAI_MODEL_ID", "sysai-agent").strip() or "sysai-agent"
    port = int(env.get("PORT", "10000"))
    if port < 1 or port > 65535:
        raise SystemExit("invalid PORT")
    tools_json = env.get("SYSAI_MCP_TOOLS_JSON", "").strip()
    if tools_json:
        names = [t["name"] if isinstance(t, dict) else t for t in json.loads(tools_json)]
    else:
        names = list(APPROVED_TOOLS)
    for name in names:
        if not re.match(r"^[a-z][a-z0-9_]*$", name):
            raise SystemExit(f"invalid MCP tool name {name!r}")
        if name not in APPROVED_TOOLS:
            raise SystemExit(f"tool {name} is not on the SYSAI allow-list")
    allowed = [f"sysai__{n}" for n in names]
    deny = list(FORBIDDEN_TOOLS)
    # Authorization header is injected at runtime via env substitution in start —
    # config.toml stores a placeholder that bootstrap replaces after env check.
    mcp_token = env["SYSAI_MCP_TOKEN"]
    proxy_token = env.get("SYSAI_MODEL_PROXY_TOKEN", mcp_token)
    gw_token = env["ZEROCLAW_GATEWAY_TOKEN"]
    if any(len(t) < 16 for t in (mcp_token, proxy_token, gw_token)):
        raise SystemExit("tokens must be at least 16 characters")

    # Do not write raw secrets into a committed template; this generated file
    # lives only on the instance filesystem.
    allowed_toml = ", ".join(f'"{n}"' for n in allowed)
    include_toml = ", ".join(f'"{n}"' for n in names)
    deny_toml = ", ".join(f'"{n}"' for n in deny)
    return f"""# Generated at boot. Do not commit.
[gateway]
bind = "lan"
port = {port}
auth_token = {json.dumps(gw_token)}

[providers.models.custom.sysai]
uri = {json.dumps(model_uri)}
model = {json.dumps(model_id)}
api_key = {json.dumps(proxy_token)}
wire_api = "chat_completions"

[agents.sysai]
model_provider = "custom.sysai"
mcp_bundles = ["sysai"]

[mcp]
enabled = true

[[mcp.servers]]
name = "sysai"
transport = "http"
url = {json.dumps(mcp_url)}
headers = {{ Authorization = {json.dumps("Bearer " + mcp_token)} }}
allowed_tools = [{include_toml}]

[mcp_bundles.sysai]
servers = ["sysai"]

[tools]
http_request = false

[tools.policy]
allowed_tools = [{allowed_toml}]
excluded_tools = [{deny_toml}]
"""


def write_config(text: str, dest: pathlib.Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text, encoding="utf-8")
    dest.chmod(0o600)


def main() -> None:
    env = dict(os.environ)
    for key in ("SYSAI_BASE_URL", "SYSAI_MCP_TOKEN", "ZEROCLAW_GATEWAY_TOKEN"):
        _need(key)
    if not env.get("SYSAI_MODEL_PROXY_TOKEN"):
        env["SYSAI_MODEL_PROXY_TOKEN"] = env["SYSAI_MCP_TOKEN"]
    text = build_config(env)
    home = pathlib.Path(os.environ.get("XDG_CONFIG_HOME", "/tmp/zeroclaw-config"))
    dest = home / "zeroclaw" / "config.toml"
    write_config(text, dest)
    print(f"ZeroClaw config written to {dest} (secrets not logged)", file=sys.stderr)


if __name__ == "__main__":
    main()
