#!/bin/sh
set -eu
umask 077
export OPENCLAW_STATE_DIR="${OPENCLAW_STATE_DIR:-/home/node/.openclaw}"
export OPENCLAW_CONFIG_PATH="$OPENCLAW_STATE_DIR/openclaw.json"
export OPENCLAW_GATEWAY_PORT="${PORT:-10000}"
export NODE_OPTIONS="${NODE_OPTIONS:---max-old-space-size=256}"
export OPENCLAW_DISABLE_BONJOUR=1
export OPENCLAW_NO_AUTO_UPDATE=1
: "${SYSAI_MCP_TOKEN:?SYSAI_MCP_TOKEN is required}"
export SYSAI_MCP_AUTHORIZATION="Bearer $SYSAI_MCP_TOKEN"
node /opt/openclaw-service/bootstrap.mjs
# Fresh ephemeral configuration needs no retained-state migration. Start the official
# gateway directly to avoid the memory-heavy doctor preflight on Render Free.
exec node /app/openclaw.mjs gateway --bind lan --port "$OPENCLAW_GATEWAY_PORT" --allow-unconfigured
