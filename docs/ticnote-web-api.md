# TicNote web/API reconnaissance (experimental)

The official TicNote API Skill is gated by Professional/Business. Before building an importer around undocumented app endpoints, map the requests made by the user's own web session.

## Run locally

From the repository root, in a local Python environment, run:

    python -m pip install playwright
    python -m playwright install chromium
    python tools/ticnote_web_probe.py

Chromium opens. Sign in manually, open one test recording, open the transcript if available, and click the normal UI export/download action for the original audio. Return to the terminal and press Enter.

The probe is read-only: it doesn't call APIs itself, submit forms, or save response bodies. It records HTTP method, URL patterns with query values/long IDs redacted, response status/content type, and JSON field names/types. The report is written to ~/.sysai-ticnote-probe/network-map.txt.

The browser session is stored separately at ~/.sysai-ticnote-probe/browser-profile. **Never commit or share that profile directory**: it contains authenticated cookies. Don't send screenshots or logs that show API keys, cookies, session tokens, signed URLs, personal transcripts, or audio.

## What to identify

1. How the web app lists recordings and fetches recording metadata.
2. Whether transcript and raw audio are separate resources.
3. How export/download obtains the audio: direct browser download or a short-lived signed file URL.
4. Whether pagination and an updated/created timestamp exist for polling.
5. Whether the session uses a CSRF token or refreshable bearer token.

Only after the observed request/response shapes are available should we implement the connector. Prefer normal authenticated web endpoints only for the user's own account; they are unofficial, can change without notice, and may be restricted by TicNote's terms. Avoid actions that transcribe or mutate content in TicNote, because SYSAI should process the original audio itself.

## Target architecture

TicNote web session/API adapter -> durable inbox in SYSAI -> download original audio -> checksum/idempotency check -> existing SYSAI audio pipeline.

A user-visible connection should not ask for the TicNote password in SYSAI. First prototype a local authenticated browser/session. For production, prefer a dedicated importer worker and secure session storage rather than launching a browser inside the main web process. The lack of an official webhook may require periodic polling; use a backfill/sync button to recover missed events.
