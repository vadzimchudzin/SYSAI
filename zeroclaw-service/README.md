# ZeroClaw on Render Free

Lightweight SYSAI execution agent. Replaces OpenClaw on the 512 MiB Free instance.
Telegram stays on main SYSAI. Live sends stay off until `SYSAI_COORDINATOR_LIVE=1`.

Pinned binary: ZeroClaw **v0.8.6** (`zeroclaw-x86_64-unknown-linux-musl`).

## Required env

- `SYSAI_BASE_URL` — `https://sysai.onrender.com`
- `SYSAI_MCP_TOKEN` — same token as main MCP
- `ZEROCLAW_GATEWAY_TOKEN` — gateway auth
- `SYSAI_MODEL_PROXY_TOKEN` — optional, defaults to MCP token
- `SYSAI_MODEL_ID` — default `sysai-agent`
- `PORT` — Render sets this

## Local tests

```bash
python3 bootstrap_test.py
```
