# SYSAI execution MCP pilot — separate runtime, existing single sender

This branch supplies **main SYSAI's tools**, not an installed OpenClaw runtime.
The main application stays the only Telegram USER-account consumer/sender and the
only owner of Neon/model/session credentials. An actual OpenClaw deployment is
managed separately under `openclaw-service/`; no custom Python client is branded OpenClaw.
`examples/sysai_mcp_client.py` is only a maintained-SDK interoperability example.

## Preserved deployed baseline

EXperemental-AI started at experimental `125904fc408d42ba41cefe7f19575924c9ef469e`.
All 20 changed files from deployed PR18 snapshot
`93cb58697ffefbd633afdc5179e23937a08a57a7` are included before the small execution hooks.
Audio/transcription/diarization/report generation are not redesigned here. PR18 voice,
department ordering and task routing fixes are preserved, not replaced by obsolete main.

## Main service settings (no values belong in git/chat)

- `SYSAI_MCP_TOKEN`: independently generated bearer secret, at least 32 characters.
- `SYSAI_MCP_USER_ID`: existing approved superuser ID; checked fresh on every tool.
- `SYSAI_MCP_SCOPES`: comma-separated `read,plan,send,model`; default is read-only.
- `SYSAI_COORDINATOR_LIVE`: leave unset for preparation; `1` permits live sends only
  when the authenticated UI also enables the pilot and disables dry-run.
- `SYSAI_MCP_MODEL_ALLOWLIST`: optional additional explicit model IDs. The existing
  `agent_model` setting is allowed; `sysai-agent` is an alias to it.
- Existing `PUBLIC_URL`, `DATABASE_URL`, `SECRET_KEY`, encrypted settings and existing
  userbot credentials stay on main. Do not create a second Telegram session.

The independent runtime receives only the scoped bearer and main URLs, not a
DATABASE_URL or OpenRouter key. TLS is required remotely. Token rotation is an
operator change; there is no public token registration endpoint.

## Explicit additive schema installation

No pilot table is created by main startup or a web/API request. Review
`migrations/20261022_coordinator_pilot.sql`, preferably on a Neon development branch.
An operator can apply that additive SQL or run `python -m app.coordinator.migrate --apply`
against the **deliberately selected** database. Without `--apply`, the command only
prints SQL. No DROP/TRUNCATE/reset/data migration is part of this feature.

Tables: coordinator_runs, coordinator_actions, coordinator_events, coordinator_leases.
They use SQLAlchemy/Postgres-compatible JSON and timezone-aware timestamps. Tasks,
events, reply evidence, send receipts, budgets and leases persist in main's Neon DB;
no critical state belongs on Render's ephemeral filesystem.

## Human opt-in / tomorrow's self-test

1. Deploy reviewed main code over the verified deployed baseline; do not merge or
   deploy automatically. Apply the reviewed additive schema explicitly.
2. Sign in as an approved superuser and open `/coordinator`.
3. Enable pilot, keep **dry-run ON**, enter explicit employee IDs. The task target
   must be active and have a verified numeric Telegram USER identity. A model cannot
   select a new recipient or infer an owner.
4. Enroll an unsent assistant task from an explicitly approved meeting. This atomically
   changes its legacy status to `coordinator_owned`, excluding it from the old sender.
5. Actual OpenClaw can discover the MCP tools below. Prepare one plan, inspect the
   questions/events in `/coordinator`. No message is sent in dry-run.
6. For a reviewed real test: existing userbot account must be ready, set the separate
   live environment gate and turn off UI dry-run. Existing quiet/rate guards apply.
7. Reply to the exact Telegram message or include `[SYSAI:<run_id>]`. An unreferenced
   reply is never guessed between multiple open tasks. The next runtime tick sees
   `queued` plus the linked employee reply and persistent task notes.
8. A quoted evidence event is required for a result. A send receipt is never fulfillment.
   Summaries explicitly attribute employee-reported evidence, not independent verification.
   Semantic result quality is model-assisted; user review remains necessary in this pilot.

Complex tasks produce `needs_review` and a suggested employee. Only human acceptance
establishes the assignee. Acceptance means a handoff, NOT completion of the employee's
work. Unknown recipients, revoked approval/role/allowlist, unclear results and uncertain
sends hold for review. No automatic resend control is exposed for uncertain sends.

## True MCP protocol

Endpoint: `/execution/mcp`, StreamableHTTP via pinned `mcp==1.26.0`, stateless JSON POST.
Supports MCP initialize, tools/list and tools/call using the SDK, not a JSON facade.
All requests need `Authorization: Bearer <scoped secret>`. No mutating GET or SSE wait.

Tools:

| Tool | Arguments | Scope |
|---|---|---|
| list_approved_tasks | {} | read |
| list_runs | {} | read |
| enroll_task | task_id | plan |
| get_task_context | run_id | read |
| claim_planning_turn | run_id | plan |
| prepare_task_action | run_id, revision, plan | plan |
| dispatch_task_action | run_id | send |
| get_correlated_replies | run_id | read |
| recover_execution | {} | plan |

Context includes bounded staff profiles/departments/duties, task notes, replies and
this task's stored artifacts, plus a revision. Plan kinds:
`ask`, `clarify`, `file`, `result`, `delegate`, `hold`. Fields are allowlisted. A
result needs `evidence_event_id` and an exact `evidence_quote` from that run's reply.
A file action needs an `artifact_id` that belongs to this task AND meeting, is a
stored PDF, and is <=2 MiB. No filesystem path, raw upload, arbitrary URL/SQL/shell.

Suggested runtime turn: recover_execution -> list_runs -> claim_planning_turn for
one queued run -> model -> prepare_task_action using returned revision -> (only if
live is approved) dispatch_task_action. Return while awaiting replies. Replayed/stale
plans are rejected; actions are durably claimed before send and never automatically
resent after an ambiguous outcome. Fresh account/task/approval/recipient checks occur
again immediately before external side effects. Single main process recommended.

## Model proxy, existing main key

Base URL `/execution/v1`; POST `/chat/completions`, bearer token and `model` scope.
Model `sysai-agent`, text/tool-call messages, maximum output 2048 tokens (default900),
20 second provider timeout, bounded context and response sizes. Streaming requests are supported by buffering the same bounded nonstream upstream
completion, then emitting valid OpenAI SSE role/content/tool-call/finish/usage deltas
and `[DONE]`. This is compatibility streaming, not upstream token-by-token streaming.
The fixed upstream is OpenRouter; its key is read through existing config access,
never returned. Existing spend guard and cost recording remain active. Durable daily
model call ceiling is `daily_actions * 3`; task planning calls are also bounded (6/run).
No retries silently spend another provider call. Configure provider credit limits too.

## Free Render / portability

The main MCP adapter requires no second consumer or scheduler. Actual OpenClaw's
Docker/manifest is a separate deliverable, not the Python example. Free Render web
services sleep after inactivity, have ephemeral disk and shared instance-hour limits;
there are no free background workers. While asleep, no runtime ticks/reminders run.
Manual waking/testing is valid; do not self-ping to circumvent quotas. Persist memory
through main tools, not local runtime disk. An always-on VPS can call the same MCP
contract without changing tools, credentials or task state design.

## Verification scope

Tests run offline in isolated SQLite with synthetic provider responses/Telegram
receipts. They exercise the real SDK protocol and production policy/state code;
no real person/provider/production DB is contacted. CI gates >=80% LINE coverage
only for NEW `app.coordinator` plus the SDK client example, and reports BRANCH
coverage separately. Neither number is whole-repository coverage. Full existing
regressions also run. PostgreSQL/live Telegram/actual OpenClaw need deployment acceptance
checks; local passing tests alone do not prove that a runtime was provisioned.

## Automatic real OpenClaw wake

Main-only environment: `SYSAI_OPENCLAW_URL` (fixed HTTPS origin),
`SYSAI_OPENCLAW_TOKEN` (gateway token, never returned/stored in task context),
`SYSAI_OPENCLAW_ENABLED=1` (default0). Existing main scheduler submits one
nonblocking wake job for eligible enrolled tasks. It calls `/v1/responses`,
model `openclaw:main`, stable per-run `user`, maximum2048 output tokens and25s timeout.
Per-run lease claims/backoff and wake diagnostic events use ONLY the four existing
pilot tables; no additional migration is needed. Gateway timeout/503 holds retry
for5minutes, at most3 wakes without task progress. Linked employee replies requeue
the same run; the next eligible scheduler step wakes its stable task session.
Dry-run prepared tasks do not repeatedly wake. `/coordinator` shows wake status/errors.
Wake events do not invalidate a planner revision while OpenClaw is working.
No custom Python planner or duplicate Telegram consumer is started.

## Approval handoff (global pilot opt-in)

With the pilot enabled, assistant.run_task atomically enrolls each newly scheduled
approved assistant task BEFORE the legacy planner/sender can claim it. Existing
approval/recovery scheduling is reused; there is no unrelated historical bulk scan.
The configured current approved SYSAI_MCP_USER_ID owns the run. Missing schema or
invalid/revoked principal/approval holds visibly and does not fall back to sending.
Already coordinator-owned tasks stay excluded from legacy even if pilot is disabled.
When pilot is disabled, existing legacy behavior is unchanged for non-owned tasks.
Default dry-run and the independent live gate still protect every external send.
The diagnostics page reports only sanitized origin/configured flags, never tokens.
