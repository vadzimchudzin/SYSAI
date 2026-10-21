# Actual OpenClaw companion on Render Free

This is a thin configuration/bootstrap layer around the official OpenClaw 2026.10.5-slim published image. It is NOT a replacement agent runtime. No custom MCP plugin is necessary: this pinned release implements native `mcp.servers` Streamable HTTP, header authentication and tool filtering.

## Build / Render settings

Use this directory as Docker build context, or set Docker context/Dockerfile paths appropriately when integrating into the parent repository. Build only COPY layers; no npm install/source compilation. Plan Free; no disk. Health check `/startupz`. Render sets PORT; start.sh forwards it to the official gateway. Container remains the non-root node user. Control UI is disabled; `/` is not an application dashboard. `/v1/responses` is enabled; `/v1/chat/completions` on this OpenClaw gateway is disabled. The separate model provider uses the SYSAI backend chat-completions proxy.

The official image manifest was verified against GHCR: `2026.10.5` and `2026.10.5-slim` both resolved to `sha256:f40d1e66fd14d8c5b49c832f4100f83b53fdc01f5108f7db11de38fd0ee6dbe5`. Dockerfile pins that digest. Linux amd64 child manifest: `sha256:0be6e07c5c99cdd4466dc9186dc89ebbf68ca8e045c44b4cc496c441a9af1a9e`.

## Required environment (inject secrets privately)

- `OPENCLAW_GATEWAY_TOKEN`: independent strong gateway operator secret, >=32 chars.
- `SYSAI_BASE_URL`: `https://sysai.onrender.com` (HTTPS origin only).
- `SYSAI_MCP_TOKEN`: backend scoped bearer token for `/execution/mcp`.
- `SYSAI_MODEL_PROXY_TOKEN`: backend scoped model proxy bearer token; may equal MCP token only if backend explicitly supports that scope.
- `SYSAI_MODEL_ID`: exact backend accepted model id (not guessed).
- `SYSAI_MCP_TOOLS_JSON`: exact approved tool manifest from backend implementation. JSON array of objects with `name` and `category`. Categories: meetings, tasks, staff, conversations, message, files, memory. At least one memory tool required. No wildcards. Example shape ONLY: `[{"name":"memory_read","category":"memory"},{"name":"task_read","category":"tasks"}]`; replace with actual backend tool names. These example names are not a claim about its final API.

Optional:
- `SYSAI_MODEL_BASE_PATH`: default `/v1`; configure actual scoped backend base path ending `/v1`, e.g. `/execution/v1`, once confirmed. This is the base path, NOT the final `/chat/completions` route. The OpenAI-compatible adapter appends that route.
- `SYSAI_MODEL_CONTEXT_WINDOW`: default 32768; must match backend model metadata.
- `SYSAI_MODEL_MAX_TOKENS`: default 4096.
- `NODE_OPTIONS`: defaults `--max-old-space-size=384`.
- `PORT`: Render-provided, fallback 10000.

No real credentials are present in this package. Generated config uses environment substitution for MCP Authorization and a provider API-key SecretRef. The gateway token is consumed by OpenClaw from its environment. Do not print runtime environments/configs or package generated state.

## Security boundary

Native MCP connects only to configured `${SYSAI_BASE_URL}/execution/mcp`. Exact `toolFilter.include` plus minimal OpenClaw tool profile and exact `sysai__<name>` additions restrict model-facing tools. Built-in session_status is denied, as are shell/process/filesystem/browser/web/direct messaging/spawn paths. Code Mode/elevated execution/ACP are disabled. No Telegram channels are configured. Existing SYSAI main Telethon remains sole consumer/sender.

The backend must enforce caller scope, tool authorization, parameter schemas and business permissions. Do not approve generic HTTP, URL-fetch, shell, arbitrary code, SQL, or filesystem tools under an innocent category. Approved files tools must use backend-issued file IDs, not arbitrary paths/URLs. This package does not pretend tool-name validation validates all backend argument schemas. Backend tool schemas should use additionalProperties:false where applicable. MCP initialize/list/call protocol is owned by OpenClaw's native client, not a hand-written transport.

The Gateway token is full operator authority; never give it to public clients. Render ingress is public HTTPS: use parent server-to-server relay/access controls where feasible. Tool restrictions apply to agent turns, not every operator administration API. Root shell/browser tool denial is not a network firewall or a substitute for keeping operator credentials private.

## Memory and lifecycle

Render Free has no durable disk and sleeps after idle time. Local sessions/transcripts/config files are ephemeral. Bootstrap regenerates config and AGENTS.md. AGENTS.md explicitly requires restoring identity/tasks/evidence via main SYSAI's Neon-backed memory tools at new sessions and persisting actual completion evidence there. This is durable business memory, NOT full OpenClaw session persistence or a full SQLite backup. Never claim previous_response_id survives loss of state. Never copy live SQLite/WAL files as backups.

## Local checks

`node --max-old-space-size=384 --test bootstrap.test.mjs`

These are structural/bootstrap tests, not an OpenClaw end-to-end run. This sandbox has Node 20 but no Docker; the official image supplies its own Node 24. No real model or MCP credentials are used in tests. No memory-fit claim is made. OpenClaw docs state 1 GB absolute minimum / 2 GB recommended; 512 MB is below that. Stop and report OOM if the minimal test cannot run. A 384 MB V8 heap cap does not cap total RSS or all worker heaps.

## Runtime acceptance checks (parent)

1. Verify bootstrap and `/startupz` succeed without OOM/restart loop.
2. POST `/v1/responses` with gateway Bearer auth and JSON `{ "model":"openclaw", "user":"sysai-test", "input":"Read durable task memory through the approved SYSAI tools. Do not modify anything." }`.
3. Check actual native MCP tool discovery and one read-only call. If no tools appear, inspect `node /app/openclaw.mjs mcp doctor sysai --probe` in a separate process, avoiding extra peak memory during normal run.
4. Verify an unapproved built-in tool and unapproved MCP tool cannot run.
5. Only then perform an explicitly authorized externally visible test, recording backend evidence in durable memory.
6. Measure memory and verify state loss behavior across a restart/redeploy.

## Official evidence pinned to this release

- https://github.com/openclaw/openclaw/blob/v2026.10.5/docs/tools/mcp.md
- https://github.com/openclaw/openclaw/blob/v2026.10.5/src/config/zod-schema.mcp-server.ts
- https://github.com/openclaw/openclaw/blob/v2026.10.5/docs/gateway/config-tools/tool-policy.md
- https://github.com/openclaw/openclaw/blob/v2026.10.5/docs/gateway/config-tools/custom-providers.md
- https://github.com/openclaw/openclaw/blob/v2026.10.5/docs/gateway/openresponses-http-api.md
- https://github.com/openclaw/openclaw/blob/v2026.10.5/docker-entrypoint.mjs
- https://docs.openclaw.ai/install/render
- https://docs.openclaw.ai/install/docker
- https://docs.openclaw.ai/help/faq-first-run/providers-and-hosting
- https://render.com/docs/free
