#!/bin/sh
set -eu
export HOME="${HOME:-/tmp}"
export XDG_CONFIG_HOME="${XDG_CONFIG_HOME:-/tmp/zeroclaw-config}"
export XDG_DATA_HOME="${XDG_DATA_HOME:-/tmp/zeroclaw-data}"
mkdir -p "$XDG_CONFIG_HOME/zeroclaw" "$XDG_DATA_HOME/zeroclaw"
python3 /app/bootstrap.py
PORT="${PORT:-10000}"
exec zeroclaw gateway --port "$PORT"
