# B — JEV ROUTER FAILURES (study only; nothing inside /workspace/SYSAI was modified or created)

Date 2026-11-12. Labels: **FACT** = seen in code / live docs / catalog / an experiment I executed; **DERIVED** = arithmetic on facts;
**HYPOTHESIS** = reasoning that is not verified. Paths are absolute under `/workspace/SYSAI` unless stated.
Everything I ran lives in `/tmp/research/` (`b_jev_wire.py`, `b_jev_sim.py`, `b_jev_probe_test.py`, `b-patch/`, downloaded docs in `b-docs/`, `b-jev/`, `b-ep/`, `b-gh/`).
No authenticated call to OpenRouter was made (no key available, none used).

---------------------------------------------------------------------------------------------------------------------------------
## 0. Summary

1. **FACT** `typesafe/jev-router` is a real, public OpenRouter router (catalog row created 2026-09-25, context 1,000,000, max completion 64,000, price `-1`
   = "no own price", tools/streaming/reasoning supported). The request we build (`model`, plugin `jev-router` with `cost_tier`, `models`, `excluded_models`)
   **matches the documented contract**; list sizes are far below the limits (150 excluded slugs / 3.4 KB vs 1,024 / 65,536). I found no schema error in it (section 3).
2. **FACT (executed against the real `gateway.py`, fake upstream, real 2026-11-12 catalog)** a Jev attempt that fails with 400/404/500/502/503/504 is re-sent to
   "mid" and is booked as a **success row under the served model** (`route = "jev→mid: HTTP …"`). It can **never** create a row named `typesafe/jev-router`.
   Rows named `typesafe/jev-router` with `ok=False` and 0 tokens come **only** from failure classes the gateway does not recover and that never open the breaker:
   HTTP 401/402/403/408/413/422/429/529, transport errors/timeouts, "200 + error body", and errors that arrive inside a stream (section 2).
3. So "5 of 5 failed" is a property of the **Jev row** (successful Jev requests are booked under the model that answered), not proof that every `sysai/auto` request fails.
   The real Jev success rate is visible only in the **tier table** ("Уровни": `sysai/auto` N requests / K errors) and in the route strings on the Trace page.
4. **FACT (git, read-only)** `sysai/auto` is the default main model only for profiles created from commit `fcf3355` (2026-11-04) on. The first commit `e0b6342` (2026-11-03) created
   the built-in profiles **without** `auto`/`main`, and `ensure_builtin()` never migrates `models` (access.py:296-321; docs/opo/TASKS.md:293). A production DB that was
   initialised before 2026-11-04 12:37 UTC still runs `sysai/mid` as the default (unless a person ticked `auto`); if so the 5 Jev rows would be a short experiment, not a systematic outage (HYPOTHESIS, check section 8 step 1).
5. **I cannot name the exact cause of the 5 + 6 errors from the screenshot** (it does not contain the failure card, the tier table or any time). The decisive evidence is
   2 clicks away (section 8: "Owner's 5-minute checklist"). Ranked causes and exact fixes are in sections 4-5.
6. The empty-model row (6 errors) is **most probably `budget_exceeded`** (section 5): default company limit `$5.00/UTC-day` (settings_store.py:36) and engineer-profile agent limit `$3/day`
   (access.py:84) sit right at the screenshot total ($5.26, terra alone $5.05). Hermes retries a failed call 3 times, so 6 rows is about 2 blocked turns.
7. The circuit breaker works **as coded** but is incomplete: it opens only on 404/503 (gateway.py:360), the fallback list misses 401/402/403/408/413/422/429/529, transport
   errors and 200-with-error bodies. Fix = 37 changed lines (prototype diff in `/tmp/research/b-patch/jev-minimal-fix.diff`, re-ran the whole matrix: all classes recover except 401 and
   in-stream errors).
8. Jev as a cost tool is a poor fit for this agent (section 7c): the model price gap is ~11x but the router (a) has no silent degradation (the docs list 503 "Jev Router is unavailable" as an error; third-party notes call it fail-closed), (b) can change model
   per turn and lose the 98% prompt cache (terra cache read $0.05/M vs $1.00/M), (c) has a decision model with a 262,144-token prompt limit while the main model averages ~273k input tokens/request.

---------------------------------------------------------------------------------------------------------------------------------
## 1. What we send to OpenRouter for `sysai/auto` (FACT: code + executed capture)

Code: `/workspace/SYSAI/app/net/gateway.py`. Flow: `complete()`/`stream()` -> `draft()` (249) -> `prepare()` (261) -> `_route_auto()` (225) -> `jev_plugin()` (191) -> `_post()/_open()`.

**Endpoint and headers** (gateway.py:312-314, 322, 326; `config.OPENROUTER_BASE_URL` = `https://openrouter.ai/api/v1`, config.py:40):
`POST {base}/chat/completions`, `Authorization: Bearer <server key>`, `X-Title: SYSAI agents`, `HTTP-Referer: {PUBLIC_URL or https://github.com/VadimChudin/SYSAI}`,
`Content-Type: application/json` (httpx). No `X-OpenRouter-Metadata`, no `session_id`/`x-session-id`. Timeouts: read 600 s, connect 30 s (catalog GET: 30/10 s, line 318).

**Body** (captured by running the real `gateway.complete()` with a fake upstream; agent body with tools/reasoning/stream_options/sysai tag/`models`):
```
{ "model": "typesafe/jev-router",
  "messages": [...unchanged...], "tools": [...unchanged...], "temperature": 0.2,   <- every other key of the agent body passes through
  "reasoning": {"enabled": true, "effort": "medium"},        <- "max_tokens" and "mode" are stripped (gateway.py:238-244); key removed if it becomes empty
  "user": "<agent name>", "usage": {"include": true},        <- gateway.py:292-293
  "plugins": [ ...agent's other plugins (kept only if internet mode == "all")..., 
     {"id": "jev-router", "cost_tier": "low",                <- profile field auto_cost: low|medium|high, default low (access.py:188)
      "models": ["deepseek/deepseek-v4.1-flash-1017","google/gemini-2.5-flash","openai/gpt-6.1-terra"],   <- model_for(t) of every allowed tier incl. vision, sorted, deduplicated
      "excluded_models": [150 slugs, 3,397 chars]} ] }       <- every catalog model dearer than the dearest included one, per prompt OR per completion price
```
Removed from the agent body: `models`, `route`, `user`, `sysai` (DROP, line 41); `stream_options` is removed for non-stream requests; `stream:true` is forced for streams.
The total body of a 2-message request is ~4.6 KB. With the owner's models (terra $1/$5 per M is the cap) the exclusion list holds 150 slugs, of which **9 are `~author/family-latest` aliases**
(claude-fable/opus/sonnet, deepseek-pro, gemini-pro, kimi, gpt-astra/sol, grok). With the shipped defaults (gemini 2.5 flash-lite/flash/pro) it holds 122 slugs, 8 aliases. The pool left under the cap: 310 priced catalog models (defaults: 338), 137 (defaults: 161) of them with ≥300k context.
The catalog is read once per 12 h from `GET {base}/models` (`prices()`, 162-188; negative prices = routers are skipped, line 178 — correct: 8 of 468 rows are priced -1).

**Rules around it**: `JEV_EXCLUDE_MAX=(1024, 65_536)` (line 43, equals the documented list limits) -> otherwise no plugin, request goes to the fallback tier with route
`auto→mid: слишком много дорогих моделей…`; no catalog/prices -> `auto→mid: нет цен моделей в каталоге OpenRouter` (and the catalog is retried only after 10 min, line 46);
fallback tier = first of `mid, cheap, strong` the profile allows (220-222). The `models` array of OpenRouter (spare model) is **not** sent with Jev (only on the fallback tier, `Plan.spare`).

**Success booking** (`record()`, 391-419): one `net_spend` row per request: `model` = served model (`data["model"]`, stream: first chunk), `tier="auto"`, `route="jev:low"`; plus
` · не из уровней агента` when the served model is not among the tier models (398-399).

**Failure handling** (355-378, 422-465):
| step | code |
|---|---|
| Jev attempt answers 400/404/500/502/503/504 | `_jev_failed`: same request to the fallback tier, `plan.fall_back()` rewrites model/plugins/`models`, route `jev→mid: HTTP 404 <message>` (≤120 chars) |
| only 404/503 | also `_jev_rest(why)`: for `JEV_REST=600` s every `sysai/auto` skips Jev, route `auto→mid: Jev недоступен (<why[:60]>)` (state is per process, in memory, reset at every restart/deploy) |
| fallback tier fails with 404/408/429/5xx | `_backup_failed`: once more to the tier's spare model (`mid→запасная`/`auto→запасная`) |
| anything else | `GatewayError` -> row with the model that was current (**`typesafe/jev-router` if Jev was never replaced**), `ok=False`, `error="<code>: <message>"[:300]`, 0 tokens |

Tests (tests/test_net_gateway.py:164-286, test_net_access.py:24-31,151-168, conftest.py:61-62) cover: plugin shape, reasoning strip, 404 -> mid + rest, no-catalog path, 503 before a stream and an error chunk
after the fallback. They use a hand-made catalog and **never** the real router; they do **not** cover 401/402/403/408/429/529, transport errors, 200+error bodies, an error as first stream event, a re-opened breaker,
or the list-size guard.

---------------------------------------------------------------------------------------------------------------------------------
## 2. Executed experiment: what each Jev failure does today (FACT)

`/tmp/research/b_jev_wire.py` imports the real `app.net.gateway`, throw-away sqlite in `/tmp/research/b-run`, real catalog (`b-jev/models.json`), fake `_post/_open`. Tiers = owner-like
(cheap deepseek-v4.1-flash, mid/strong gpt-6.1-terra). Fallback tier answers 200. "agent" = HTTP status the agent gets.

| Jev attempt fails with | agent sees | upstream calls | breaker rests | row(s) written (model / ok / error code / route) |
|---|---|---|---|---|
| 400, 500, 502, 504 | **200** | 2 | no | terra / ok / – / `jev→mid: HTTP 400 …` |
| 404, 503 | **200** | 2 | **yes** | terra / ok / – / `jev→mid: HTTP 404 …` |
| 401 | 502 | 1 | no | **typesafe/jev-router / FAIL / upstream_auth** / `jev:low` |
| 402 | 402 | 1 | no | **jev-router / FAIL / upstream_credits** |
| 403 | 502 | 1 | no | **jev-router / FAIL / upstream_auth** ("OpenRouter не принял ключ SYSAI: …" even for budget/guardrail/moderation 403) |
| 408, 413, 422 | same | 1 | no | **jev-router / FAIL / upstream_rejected** |
| 429 | 429 | 1 | no | **jev-router / FAIL / upstream_rate_limit** |
| 529 | 502 | 1 | no | **jev-router / FAIL / upstream_error** |
| 200 + `{"error":…}` body | 502 | 1 | no | **jev-router / FAIL / upstream_error** (status hard-coded 502, real code lost; gateway.py:440-441) |
| httpx timeout/connect error | 502 | 1 | no | **jev-router / FAIL / upstream_error** ("OpenRouter недоступен: …"; after a 600 s read timeout) |
| stream: 404 before 200 | 200 | 2 | yes | terra / ok / `jev→mid: HTTP 404 …` |
| stream: 429 or 403 before 200 | 429 / 502 | 1 | no | **jev-router / FAIL** |
| stream: 200 then an `error` chunk | 200 (error passed on) | 1 | no | model of the chunk (here jev-router) / FAIL / upstream_error, route `jev:low · не из уровней агента` |
| Jev 404, then mid also 503 | 502 | 3 | yes | spare model (gpt-4.1-mini) / FAIL / upstream_error, route `jev→mid: … · auto→запасная: HTTP 503 …` |

Consequences (FACT):
* A failed Jev attempt that is recovered leaves **no error anywhere**: not in the failure card, not in the agent's error counter, only in the `route` text of an otherwise green row. Jev's real failure rate is invisible.
* The row `typesafe/jev-router` + errors is exactly the set of **unrecovered** failures. Every one of them also costs the agent a failed model call (section 6).
* Where the OpenRouter text survives: `error` column `"<code>: <OpenRouter error.message>"` (≤300 chars). Lost: HTTP status (except for 5xx messages and fallback routes), `error.code`, `error.metadata`
  (`limit_source`, `provider_name`, `error_type`) — `_message()` keeps only `error.message` (gateway.py:334-339).

---------------------------------------------------------------------------------------------------------------------------------
## 3. What `typesafe/jev-router` really is, and our request vs its contract

Sources (all fetched 2026-11-12): live catalog `https://openrouter.ai/api/v1/models` (468 rows, 845 KB), `https://openrouter.ai/api/v1/models/typesafe/jev-router/endpoints`,
`https://openrouter.ai/api/v1/models/typesafe/jev-1.14/endpoints`, model page `https://openrouter.ai/typesafe/jev-router`, docs `https://openrouter.ai/docs/llms-full.txt`
(pages "Jev Router" /docs/guides/routing/routers/jev-router, "Errors and Debugging" /docs/api_reference/errors-and-debugging, "Router metadata", "Credit limits / In-flight spending budget"
/docs/api_reference/limits, "Auto Router", "Switchyard", "ZDR", "Jev" community hub), third-party notes read with `gh api` (untrusted, labelled below).

**What it is (FACT):** "TypeSafe: Jev Router" — a *router model* (tokenizer "Router", no endpoints of its own: `endpoints: []`). A decision model (TypeSafe Jev, a "System One" model that returns typed
probabilities, not text) assesses the conversation; the router picks a **model and a reasoning effort**; the picked model generates the answer. Docs: "A cost tier is a routing preference, not a fixed model, guaranteed
score, or per-request price cap." Catalog: context 1,000,000; `top_provider.max_completion_tokens` 64,000; input modalities text/image/file/audio/video; output text; pricing `-1/-1` (docs: "billed at the price of the
model it is routed to… no routing fee… the context length is that model's"). Router endpoint record: provider "OpenRouter" (adapter "Stealth"), `data_policy.training=false`, no rpm limit listed.
Supported parameters in the catalog: frequency_penalty, include_reasoning, logit_bias, logprobs, max_completion_tokens, max_tokens, min_p, parallel_tool_calls, presence_penalty, reasoning, reasoning_effort,
repetition_penalty, response_format, seed, stop, structured_outputs, temperature, tool_choice, tools, top_a, top_k, top_logprobs, top_p, verbosity. (The "supports_tool_parameters:false" flags on the model page are the same
all-false stub that `openrouter/auto` has — not meaningful.) Works on Chat Completions, Responses, Messages, **streaming and non-streaming**. Decision model `typesafe/jev-1.14`: provider TypeSafe,
**context and `max_prompt_tokens` 262,144**, $0.042/M input, output free.

**Contract vs our request**
| item | docs say | we send | verdict |
|---|---|---|---|
| model slug / plugin id | `typesafe/jev-router`, plugin `"jev-router"` required `id` | same | OK |
| `cost_tier` | `low|medium|high`; omitted = default policy; unknown -> 400 | `low` (profile `auto_cost`, values validated in access.py:188) | OK |
| `models` / `allowed_models` | include **preferences with fallback** (pool retried without them if empty); no match with any catalog text model -> 404 naming the field | tier models, ≥1 priced catalog slug guaranteed (gateway.py:197-199) | OK |
| `excluded_models` | hard exclusions; slug (matches every dated revision), dated slug, `*` wildcard, `~family-latest` alias (matches every revision of the family); ≤1,024 patterns, ≤1,024 chars each, ≤65,536 chars per list | 122-150 exact slugs incl. 8-9 `~…-latest` aliases | OK on limits; **alias semantics unverified** (an excluded alias may also hide an older included revision) |
| unknown plugin fields / values | 400 | none | OK |
| `reasoning` | `effort` is a hint; **`reasoning.max_tokens` and `reasoning.mode:"pro"` -> 400**; legacy `reasoning_effort` accepted | strips both, keeps the rest | OK |
| other router in the same request | forbidden (400) | `models` array removed (DROP) | OK |
| region | global data region only (model page: "Not currently available through EU in-region routing"; regional -> 400) | `https://openrouter.ai/api/v1` unless `OPENROUTER_BASE_URL` is overridden | OK by default; **prod env not verified** |
| access | "only in workspaces that allow it. Access to the router **and the selected models** is still required"; no access -> **404** | – | cannot check; how a workspace "allows" it is **not documented** |
| auth | Bearer key | same | OK |
| errors | 400 (unknown tier/field, reasoning, region, two routers), 404 (no eligible model **or no access**), 503 (Jev Router unavailable) | recovered | – |
| generic errors | 401, 402 (credits / key limit / in-flight budget), 403 (guardrail, moderation, **budget limits**), 408, 429, 502, 503; after OpenRouter has sent `200 OK` every failure arrives **inside** the body/SSE event | **not recovered** | **GAP** (section 2) |
| streaming | `200 OK` + headers are committed as soon as the provider accepts the request, "before the model produces a single token… includes provider errors that arrive before any token" | status-based fallback only | **GAP** for router/provider failures after commit |
| diagnostics | `X-OpenRouter-Metadata: enabled` -> `openrouter_metadata.pipeline[jev-router]` (`resolved_models`, `candidates`, `reason`, `pipeline`, `list_fallback`, `list_tier_cap`), also on error envelopes (top level) | not sent | **missing** |
| session | `session_id` body field / `x-session-id` header = sticky key; Auto Router docs: "Switching models costs extra while the new model rebuilds the prompt cache" | not forwarded | missing (cost/caching) |
| usage | `usage.cost` | `usage:{include:true}` (legacy, harmless) | OK |

**What the docs do NOT say (not verified):** how a workspace is "allowed" to use the router (no toggle documented); what the router does with conversations longer than the decision model's 262,144-token prompt limit;
router-specific rate limits; whether a non-router model may follow the router in a `models` fallback array; whether `reasoning.enabled:false` / `effort:"none"` are accepted; how Jev-stage failures are reported
(documented only: 503 "unavailable"); whether the conversation text reaches TypeSafe under ZDR (third-party notes claim "ZDR supported, attachments never sent to Jev" — unverified; the same notes say Jev is
strongest in English and that a Jev failure makes the request fail instead of degrading — consistent with the 503 row, unverified otherwise). Third-party agent harnesses wrap it with fail-open logic for the same reason.

---------------------------------------------------------------------------------------------------------------------------------
## 4. Ranked causes of the 5 failed Jev rows (each with how to confirm and the exact fix)

Starting logic (FACT): the failures are in the **unrecovered** class (section 2). Causes the owner listed that are therefore *excluded as the cause of these rows* (they may still happen invisibly and be recovered):
404 "no endpoints / no access" (recovered, rests the breaker), 400 invalid plugin / reasoning / region (recovered), 503 unavailable (recovered), "tier models not accepted" (router accepts any catalog text model; ≥1 priced
include is guaranteed), "tools / streaming unsupported" (catalog + docs say both are supported), account data policy (router endpoint `training:false`; a ZDR/guardrail that shrinks the pool should give 404 = recovered — inferred from the docs, not tested).

Where to look (all pages exist; see section 8): failure card `/agents/spend?days=7` (bottom card "Отказы и сбои": code, count, example text ≤200 chars of OpenRouter's message), Trace `/agents/<id>/trace?days=7&errors=1`
(column "Итог" = `<code>: <message>`; "Подробности" = model + `route`; "Время" = duration; header "Первая ошибка: …"), debug mode (button on the trace page, 24 h) adds a "текст" link per step.
Duration is the best discriminator: a few hundred ms to seconds = an HTTP-level rejection, ~10 min = our 600 s read timeout, tens of seconds = a router/provider timeout.

| # | cause | likelihood (HYPOTHESIS ranking) | evidence | confirm on spend/trace page | exact fix |
|---|---|---|---|---|---|
| 1 | **Failure after OpenRouter committed `200`**: SSE error event / `200 + {"error":…}` body from the router or the model it picked; or a transport timeout | high among unrecovered classes | Hermes streams by default (hermes `agent/turn_api_call.py:_should_stream`); OpenRouter docs (Streaming error formats) say all failures after the provider accepts arrive in the body; two hops with Jev; gateway has no status to inspect (gateway.py:440-441, 496-497) | code `upstream_error`; text "OpenRouter HTTP 502: …"/chunk message, or "OpenRouter недоступен: …Timeout"; 0 tokens; "Время" tiny vs ≈10 мин | minimal patch (fallback on transport error and `200+error`); phase 2: peek the first SSE event (≤10-20 s) before committing the stream; add `X-OpenRouter-Metadata` |
| 2 | **HTTP 429** rate limit (new router; docs give `Retry-After`) | medium | listed generic error; not recovered (gateway.py:357) | code `upstream_rate_limit` "Модель перегружена, повтори позже: …" | fall back + rest for `Retry-After`; no OpenRouter-side fix |
| 3 | **HTTP 402 / 403**: credits, in-flight spending budget (402 `weight_exceeds_budget` / `in_flight_budget_exhausted`), key limit, guardrail or workspace budget, guardrail model allow-list, moderation | medium-low (would normally hit all models, not only Jev; a guardrail allow-list or a router-specific credit estimate would hit only Jev) | docs: 403 "budget limit exceeded (… limit_source=openrouter_key_limit/guardrail/workspace)"; our 403->`upstream_auth` text blames the key | `upstream_auth` + message containing "Budget limit", "guardrail", "blocked", or `upstream_credits` "На счёте OpenRouter нет средств: …" | OpenRouter side: `GET https://openrouter.ai/api/v1/key` (`limit_remaining`, `budgets`), settings/credits, settings/guardrails, settings/privacy; gateway: fall back on 402/403 and keep `error.metadata.limit_source` in the text |
| 4 | **Conversation longer than the decision model can read** (Jev 1.14 `max_prompt_tokens` 262,144; main-model input averages **273k tokens/request** = 38.79M/142; worker model 154k) | medium-low, **unverified** (docs silent) | numbers above; docs: Jev long-context mode >32 KB text "slower and less accurate" | failing/fallback rows cluster in the biggest conversations: success rows with route `jev→mid: HTTP 4xx` and `prompt` > 262k; or run the 1 MB probe (section 8) | gateway guard: skip Jev above ~200k estimated tokens (route `auto→mid: длинный диалог`); ask OpenRouter support how the router feeds Jev |
| 5 | **Not a Jev fault: `sysai/auto` barely used** (existing profiles keep `main=mid`, section 6) -> only an experiment produced 5 rows | high as an *explanation of the small number*, not of the failure class | git `e0b6342` vs `fcf3355`; access.py:296-321; docs TASKS.md:293 | agent page line "думает и планирует: sysai/mid" and tier table: `sysai/auto` row small | none needed; decide whether to keep Jev (section 7) |
| 6 | Production runs an older gateway without the fallback | low | Jev code (with fallback) is in the repo since 2026-11-04 12:37; first commit 2026-11-03 | successful `auto` rows have routes `jev:…` / `jev→mid:…` / `auto→mid:…` only in the new code | deploy current main |

Observed-pattern cross-check: in the failure matrix, a status in {402, 403, 408, 429, 529, transport} makes Hermes retry (up to 3 attempts, error_classifier: 402 billing is non-retryable, others retry), so 5 rows ≈ 2 failed
turns (3 + 2) — **HYPOTHESIS**, based on `agent.api_max_retries` default 3 in Hermes (`tests/agent/test_api_max_retries_config.py` docstring) and `_run_api_retry_loop`.

Message -> cause table for the owner (what to read in the example column):
* `upstream_error` + "OpenRouter недоступен: ReadTimeout" and duration ~10 min -> router/model hung (cause 1); fix = fallback + rest.
* `upstream_error` + "OpenRouter HTTP 502: …" with the model's own text -> failure after commit (cause 1).
* `upstream_rate_limit` -> 429 (cause 2).
* `upstream_credits` or `upstream_auth` with "Budget"/"guardrail"/"blocked"/"credits" -> cause 3; plain "No auth credentials / Invalid API key" -> the key (then *all* models would fail).
* `upstream_rejected` + message mentioning context/tokens/"too large" -> cause 4.

---------------------------------------------------------------------------------------------------------------------------------
## 5. The row with an empty model ("—", 6 requests, 6 errors, 0 tokens)

The spend page groups by `NetSpend.model` (spend.py:17-19,66; "—" = `EMPTY_RU` for ''). Only `gateway.record()` writes `net_spend` (grep: one `NetSpend(` constructor, gateway.py:404; callers only `routes.py:1081,1084`).
`model = served or plan.model` (line 394). `plan.model` is still '' when `prepare()` raises before the routing step (line 300-304), so every refusal in `prepare()` with a **valid tier** or a **missing `model`** key lands in "—".

Executed (b_jev_wire.py section 4), exact rows:
| path | code line | HTTP | row (tier, model, error) |
|---|---|---|---|
| body has no `model` | 266-268 | 400 | ('', '', `model_not_allowed: model: один из уровней sysai/cheap…`) -> shown "—" in both model and tier tables |
| unknown model name (e.g. `gpt-4o`) | 266-268 | 400 | ('', **'gpt-4o'**, …) — has a name, so NOT "—" |
| valid tier but no `messages` | 263-264 | 400 | ('mid', '', `invalid_request`) |
| tier not allowed for this agent (e.g. `sysai/strong`, or `sysai/auto` when `auto` is not ticked) | 270-271 | 403 | ('strong', '', `model_not_allowed: Доступы агента не разрешают уровень sysai/strong`) |
| no OpenRouter key | 272-273 | 503 | ('mid', '', `no_key`) |
| company daily limit (`daily_budget_usd`, default **$5.00**, `llm.check_budget`) | 285-288 | 429 | ('mid', '', `budget_exceeded: Дневной лимит расходов на ИИ исчерпан: $5.07 из $5.00`) |
| agent daily budget (engineer profile **$3.00**) | 274-279 | 429 | ('mid', '', `budget_exceeded: Дневной бюджет агента исчерпан: $3.01 из $3.00. Скажи своему человеку…`) |
| project budget (engineer **$30**, only with `X-SYSAI-Project`) | 280-284 | 429 | ('mid', '', `budget_exceeded: Бюджет проекта … исчерпан`) |
Never recorded at all (answered before the gateway): bad/expired token 401, network switched off 404, unparsable JSON 400 (routes.py:118-129, 1074-1075).

**Most probable (HYPOTHESIS, strong prior):** `budget_exceeded`. Reasons: (a) total on screen $5.26, terra alone $5.05 — right at the $5.00 company limit and above the $3.00 agent limit; the page default period is 7 days
(spend.py:31-36) and git shows the whole network is ~9 days old, so these numbers may be one or two days of use; (b) a configuration error (missing model, forbidden tier, no key) would fail *every* call, not 6;
(c) Hermes does not recognise `budget_exceeded`/our Russian text as billing (its patterns/codes are English: `insufficient_quota`, `budget limit exceeded`, …), so HTTP 429 is retried 3 times -> 6 rows ≈ 2 blocked turns.
**How to tell:** spend page bottom card "Отказы и сбои": the code of the 6 rows and the example text ("Дневной лимит…" = company, "Дневной бюджет агента…" = agent, "Бюджет проекта…" = project); the tier table shows the tier of the
refused requests (`—` = no model key). Fix if confirmed: raise `daily_budget_usd` (settings page) / profile budget; better UX: answer budget refusals with HTTP 402 + code `insufficient_quota` so Hermes stops at once instead of retrying 3 times.

---------------------------------------------------------------------------------------------------------------------------------
## 6. Circuit breaker, failed turns, is `sysai/auto` the default?

**Breaker (FACT):** `JEV_REST=600` s, state `_state["jev_rest_until"/"jev_why"]` (gateway.py:47, 208-217), process-global, in memory (single uvicorn process per Dockerfile; resets on deploy). It is opened only in `_jev_failed`
for **404/503** (360-361). Concurrent in-flight requests each try Jev once more. After the rest, the next *user* request is the probe (no background canary). Nothing in the UI shows the breaker state or `jev_why`.
Designed behaviour = "1 failed Jev attempt per 10 min + fallbacks" **for 404/503 only**, and those are recovered and invisible as errors. The 5 visible failures therefore cannot come from the breaker's designed path;
they come from classes it does not know (section 2). The only other silent switch-off is `prices()` failing: Jev is skipped for ≥10 min (`auto→mid: нет цен…`).

**Failed turn or not (FACT + HYPOTHESIS):** recovered statuses -> the agent gets HTTP 200 (one extra round trip), no failed turn. Unrecovered -> the agent gets `{"error":{"type":"upstream_…"}}` with HTTP 402/429/502/408…; Hermes
(`fallback_providers: []`, set by SYSAI, access.py:543) retries up to 3 attempts with jittered backoff (defaults of `agent/retry_utils.py:121`: base 5 s, max 120 s) and then ends the turn with an error (402 billing is non-retryable). So each unrecovered Jev
failure costs the person a failed model call, ≈15-60 s of waiting and a visible error. Because the gateway never rests Jev for these statuses, the next turn tries Jev again and fails again.

**Default model (FACT):** `sysai/auto` is `model.default` of Hermes for the built-in profiles "Инженер ОПО", "Руководитель ОПО" (deepcopy of engineer) and "ТО": access.py:84-85,102 (`main:"auto"`, `workers:"cheap"`, `auto_cost:"low"`),
`render_hermes` access.py:540 (`"default": f"sysai/{main}"`), test_net_access.py:24. Helpers use explicit tiers: workers `sysai/cheap`, compression `sysai/cheap`, vision `sysai/vision` (access.py:544-553; `sysai.role` tag -> "исполнитель/сжатие/зрение").
The sync agent overwrites `model` in the PC's Hermes config on every apply (agents/sync/sysai_agent/hermes.py:17, 50-70). **But** profiles already in the DB are not migrated (`ensure_builtin`, access.py:296-321; `normalize` derives `main` = first of
mid/strong/cheap when absent, access.py:205-216): git `e0b6342:app/net/access.py` shows the first engineer profile as `tiers [cheap, mid, strong, vision]` without `auto`. Check on the agent page (`/agents/<id>`, template line 65: "думает и планирует: sysai/…").
The desktop composer has a per-conversation model picker (agents/desktop/renderer/js/composer.js:15, app.js:591-596), so a person can also pick another allowed tier (HYPOTHESIS: the list = the allowed tiers from `/v1/models`).

---------------------------------------------------------------------------------------------------------------------------------
## 7. Recommendations

### 7a. Minimal fix
**Today, no code (zero risk):** in "Доступы" -> profile "Инженер ОПО" (and "ТО"): set "Главная модель" to `sysai/mid` (or `strong`) and untick `sysai/auto`, or set `main` via the profile form (templates/net/access.html:45). The gateway then never calls Jev.
Verify on the agent page that it says `sysai/mid`. Raise the budgets if section 5 is confirmed.

**Code (≈37 changed lines, `/tmp/research/b-patch/jev-minimal-fix.diff`; applied to a *copy*, not the repo):**
1. `_jev_failed`: fall back on **any** non-200 of the Jev attempt except 401, and always `_jev_rest` (new helper `_jev_down`).
2. `complete()` / `_open_stream()`: a transport error or a `200 + error` body of the Jev attempt also goes to the fallback tier and rests Jev (`_error_body`).
3. Result of re-running the whole matrix on the patched copy: 400/402/403/404/408/413/422/429/500/502/503/504/529, 200+error, timeout -> **agent sees 200**, one success row with `route jev→mid: <reason>`, breaker rests; 401 still fails (right: the key is bad);
   an error as the *first stream event* still reaches the agent (needs phase 2: peek the first `data:` line for ≤10-20 s before committing; keep the 600 s timeout for the rest).
4. Also (small): keep the HTTP status and `error.metadata.limit_source/error_type` in the stored error (`_message`/`_upstream_error`), stop calling every 403 "OpenRouter не принял ключ SYSAI"; send `X-OpenRouter-Metadata: enabled` on Jev requests and store `pipeline[jev-router].reason/resolved_models`
   in `route`; show the **outbound** plugin summary (cost_tier, includes, `len(excluded_models)`, reasoning) in the debug excerpt (`trace.request_excerpt(body)` shows only the incoming body, gateway.py:307-308).
5. Tests to add (tests/test_net_gateway.py): one parametrised test over {401,402,403,408,429,529, timeout, 200+error, error as first stream event}; breaker re-opens after `jev_rest_until`; list-size guard; `prepare()` refusals ("—" rows).
Effort: S (0.5-1 day incl. tests).

### 7b. Health probe with a visible status and reason
Prototype (runs on fakes, `/tmp/research/b-patch/jev-probe-prototype.diff`, `b_jev_probe_test.py`): `gateway.probe_jev(big_chars=0)` sends **one tiny request through the real route** — the real plugin of the built-in engineer profile via `jev_plugin()` (so list sizes, aliases and
reasoning handling are tested, not just access), `max_tokens:16`, header `X-OpenRouter-Metadata: enabled`, 45 s timeout — and stores `{at, ok, status, served, ms, message, reason}` in `_state["jev_probe"]`; success closes the breaker, failure opens it.
Cost ≈ $0.00001. "Deep" variant (`big_chars=1_000_000`, ~270k tokens, ≤ $0.3 at the $1/M cap) answers the long-conversation hypothesis (cause 4) directly; owner-triggered only.
Wiring (files/functions):
* Startup: `app/main.py:startup()` (125-142, after `notion.start_worker()`, skipped under `config.TESTING`): `threading.Timer(config.STARTUP_DELAY + 5, gateway.probe_jev)` with `daemon=True` (same pattern as the userbot timer, main.py:144-146), only when `net.enabled()` and a key exists; re-probe when the rest ends
  (replaces "the first user request is the canary") and every ~30 min while any profile allows `auto`.
* Route: `@router.post("/agents/access/jev-probe")` in `app/net/routes.py` next to `access_settings` (362), `_person(request, admin=True)`, `await _in_thread(gateway.probe_jev, big)`, `_back("/agents/access#models", msg=…)`.
* UI: `app/templates/net/access.html` `<section id="models">` (line ~78) and the `auto_cost` field (line 47): pill green "Jev работает · проверен 14:03 · выбрал google/gemini-2.5-flash-lite за 0,8 с", amber "Jev отдыхает до 14:13: HTTP 404 …, sysai/auto идёт на «mid»", red with the Russian reason by code
  (404 = нет доступа у ключа/рабочего пространства или не осталось подходящих моделей; 402 = нет средств/лимит ключа; 403 = бюджет/guardrail/модерация; 429 = лимит запросов; 400 = роутер отклонил параметры; timeout) + buttons "Проверить сейчас" / "Проверить на длинном запросе".
  Same one-line banner on `spend.html` and next to "думает и планирует: sysai/auto" in `agent.html:65` when Jev is not OK. The existing "Проверить сохранённый ключ" (`/settings/test-openrouter`, `llm.check_key`, main.py:3214) only calls `GET /key`; extend it to also print `limit_remaining`/`is_free_tier`.
* Counters without schema change: `gateway.jev_report(days)` = count of `net_spend` rows by `route` prefix (`jev:` ok, `jev→` fell back, `auto→` skipped, errors with model jev-router) -> "Jev за сутки: 41 ответил, 3 ушли на mid (HTTP 404), 2 ошибки".
* Persist the last probe in `settings_store` so the status survives restarts. Tests: probe with fake `_post` for 200/404/429/timeout (as in b_jev_probe_test.py) and the access page contains the status. Effort: S-M (1-1.5 days).

### 7c. Alternative: our own deterministic router (by role / stage / context size / vision need)
**Facts that frame it (DERIVED from the screenshot + live catalog prices):** terra $1.00/M input, **$0.05/M cache read**, $1.25/M cache write, $5/M output; deepseek-v4.1-flash $0.15 / $0.003 / $0.60; blended spend per token on the screenshot: terra $0.130/M tokens vs deepseek $0.0114/M = **11x**.
With 98% cache and list prices, terra's $5.05 splits into roughly $2.6-2.8 input and **$2.2-2.4 output** (≈0.48M completion tokens = ~3.4k per request) — output is ~45% of the main-model bill. A switch *back to terra* in a 270k-token conversation costs ≈ $0.26-0.32 more than a cached request (≈ 7-9 average terra requests of $0.036), a switch to deepseek ≈ $0.04: **switch rarely, pin per session/stage.**
Moving ~60% of the terra tokens to a deepseek-class model would cut the screenshot total from $5.26 to ≈ $2.5 (DERIVED upper estimate, ignores quality and cache rebuilds).

Design (where): replace the body of `_route_auto` (gateway.py:225) by `_route_policy(plan, rules, body)` choosing a *tier* (never a raw model), keep Jev as an optional engine (`auto_engine: policy|jev` in the profile; access.py `normalize`, profile form). Features available at the gateway today:
role tag (`plan.role`), image parts (`trace.text_of` already walks parts), estimated prompt size (chars/3.8, or the previous `usage.prompt_tokens` of the same conversation), last message role (`user` new instruction vs `tool` loop), tool names of the last assistant message,
error markers in the last tool result (traceback, compiler error, non-zero exit), loop depth, plus `X-SYSAI-Skill/Project/Task` (**not set by `render_hermes` today** — no `extra_headers`; skill usage is only reported by the agent via `log_event(skill_used)`, mcp.py:351-365; no hub skill declares a tier).
Starting rules (HYPOTHESES to calibrate): vision/compression/workers keep their explicit tiers (already); main: first user turn of a session = `strong` (plan), tool loops of reading/extraction/format conversion = `cheap`, code-writing/compile loops = `mid`, two consecutive tool errors or review/verify = `strong`;
image-bearing turns never to a non-vision tier; estimated prompt > X -> do not downgrade mid-session; pin the decision per (agent, conversation fingerprint) with TTL; budget-aware downgrade at 70% of the daily limit. Route text `policy:stage=read→cheap` goes into the existing `route` column (no migration).
Files: `app/net/gateway.py` (policy + pin cache), `app/net/access.py` (`COST_TIERS`/new `auto_engine`, `render_hermes` unchanged), `app/templates/net/access.html` (rules table), `tests/test_net_gateway.py`, later hub skill front matter `model_tier:` + the planned task level of the Conductor (docs/opo/TASKS.md:283-296, 621-624, ОПО-15.3).

| | own deterministic router | Jev Router |
|---|---|---|
| failure modes | none external; testable offline | extra hop, documented 503 "unavailable", **fail-closed**; today unrecovered statuses break turns |
| predictability / audit | every decision explained in `route`; same input -> same tier | opaque; "cost tier is a preference, not a cap"; model may change per turn; our `models` list is only a preference (relaxed when the pool is empty), so an answer can come from any model under the price cap (flagged `· не из уровней агента`) |
| prompt cache | pinned by design (the 98% cache is the main saving) | claimed cache-aware by the vendor/third parties, unverified; Auto-Router docs warn that switching rebuilds the cache |
| long contexts | independent | decision model prompt limit 262,144 tokens (avg main input 273k) — behaviour undocumented |
| language / quality | tuned on our traffic (Russian, ST code, tool loops) | decision model reported strongest in English (third-party), generic benchmarks (237/423 tasks in a vendor benchmark, third-party summary) |
| privacy | nothing leaves | conversation text is assessed by TypeSafe's model (policy not documented on OpenRouter) |
| adaptivity | only what we encode; brittle on unseen tasks | adapts per turn without our work |
| effort | M (3-5 days: v0 role/vision/size rules 1 day, stickiness + stage detection 2 days, UI+tests 1-2 days, plus data work) | S for the fix/probe, but benefit unproven |

**Data to collect first ("study, then plan")**, in this order:
1. Per-request export of `net_spend` for 7-14 days (id, at, tier, model, role, skill, task, prompt/completion/cached tokens, cost, ms, ok, error, route). The CSV in the UI is aggregated by day/agent/project/skill/tier/model/role (spend.py:116-136) and cannot give per-request size distributions -> add `?raw=1` or run SQL on the production DB.
2. Shadow mode: compute the policy decision for every request and store it in `route` (`policy?:cheap`) without acting on it; add nullable columns for `turn_kind`, `tool_names`, `tool_error`, `est_ctx_tokens` (models.py:193-216) — instrumentation first, behaviour second.
3. Quality signal per stage: Machine Expert build result / compile pass-rate, number of retries and loops per task, human accept/reject, by model — to decide which stages tolerate the cheap model (A/B the same task on cheap vs strong).
4. Current tier->model mapping and the "Уровни" table (which tier the 142 terra and 80 deepseek requests belong to) and the failure card (section 8).
5. A short Jev experiment *only if wanted*: probe + the metadata header for a day, compare `resolved_models`/`reason` with what a rule would choose.

Sibling option to mention: OpenRouter's own `nvidia/switchyard` router (`stage` algorithm scores the tool results already in the conversation, no judge call when decisive; `composite` pins the tier for a conversation; documented fail-open `caller_order` when its routing service is unavailable and automatic fallback to the next model) with `models:[cheap, strong]` — a documented fail-open alternative if an external router is still wanted; quality for ST/EasyBuilder work unmeasured.

---------------------------------------------------------------------------------------------------------------------------------
## 8. Owner's 5-minute checklist (read-only), and server probes

1. `/agents/<id>` — line "Нейронки … думает и планирует: sysai/…": is the main model `auto`?
2. `/agents/spend?days=7&agent=<id>`: tier table row `sysai/auto — авто…` (N requests, K errors, cost) = the real Jev denominator; bottom card **"Отказы и сбои"**: code + count + example for the 5 Jev errors and the 6 empty-model errors (`budget_exceeded`? `upstream_*`?).
3. `/agents/<id>/trace?days=7&errors=1` -> open each run: for failed `sysai/auto` steps read "Итог" (text), "Время" (ms), "Подробности" (model + `route`); look at neighbouring green `auto` steps: `jev:low` = Jev works, `jev→mid: HTTP …` = silently failing and recovered, `auto→mid: Jev недоступен (…)` = resting.
4. Optional: "Режим отладки" on the trace page for 24 h, repeat the failing action, open "текст" of the step (shows the incoming request tail and the error; not the outbound plugin).
5. On the server (read-only; `$OPENROUTER_API_KEY` = the key stored in SYSAI; do not paste it anywhere):
   * `curl -s https://openrouter.ai/api/v1/key -H "Authorization: Bearer $OPENROUTER_API_KEY"` — `limit_remaining`, `usage_daily`, `is_free_tier`, `budgets`/`effective_budget`.
   * `curl -sS -i -m 60 https://openrouter.ai/api/v1/chat/completions -H "Authorization: Bearer $OPENROUTER_API_KEY" -H "Content-Type: application/json" -H "X-OpenRouter-Metadata: enabled" -d '{"model":"typesafe/jev-router","messages":[{"role":"user","content":"Reply with: ok"}],"max_tokens":16,"plugins":[{"id":"jev-router","cost_tier":"low"}]}'`
     — status + the *real* error body + `openrouter_metadata.pipeline` (jev-router stage). Repeat with `"stream":true` and `curl -N` to see whether errors arrive as HTTP status or as an SSE event.
   * Interpretation: 200 -> router works for this key (then compare with the real plugin shape/long prompts); 404 -> no access / no eligible model (check workspace + guardrails + ZDR at openrouter.ai/settings/privacy, /settings/guardrails); 400 -> read the field named in the message; 402/403 -> credits/limits/guardrail; 429 -> wait `Retry-After`.

---------------------------------------------------------------------------------------------------------------------------------
## 9. Cross-area notes
* Token audit: main model avg 273k input tokens/request, worker model 154k; derived output share of the terra bill ≈ 45% (assumes list prices, 98% cache); SYSAI's page total ($5.26 for the default 7-day window, network age ~9 days by git) vs the owner's "$25 per project" -> either another period/filter,
  or spend that does not pass the gateway (Hermes auxiliary tasks other than vision/compression are not managed by SYSAI: hermes.py:17-18), or per-project accounting differs. Worth reconciling with the OpenRouter activity page before any optimisation.
* Hermes streams by default and retries each failed model call up to 3 times: every gateway refusal is multiplied by 3 in the spend/trace counts.
* `X-SYSAI-Skill/Project/Task` headers are never set by the generated Hermes config, so skill/project columns are empty in practice; any stage-aware routing needs that signal first.

## 10. Not verified / open
* The cause of the 5 Jev + 6 empty-model errors (no production data, no OpenRouter key, no screenshot of the failure card/tier table/time window).
* How a workspace "allows" the router; router rate limits; behaviour on conversations > 262,144 tokens; exact error body of "no access"; whether a router may be the first element of a `models` fallback array (would give a native single-request fallback — test with the probe).
* `~family-latest` alias semantics inside `excluded_models`; third-party claims on ZDR, fail-closed behaviour, English bias and cache-aware switching.
* That production runs main @ 7ad99a7 (git shows Jev + fallback in the repo since 2026-11-04 12:37 UTC).
* The Hermes version installed on the PC vs GitHub main (I read main of NousResearch/hermes-agent today: streaming default, `api_max_retries` = 3, error classifier).

## Appendix — files
`/workspace/SYSAI`: app/net/gateway.py (1-545), app/net/spend.py, app/net/trace.py, app/net/access.py (84-85, 102, 185-216, 296-321, 538-553), app/net/routes.py (118-129, 248-271, 362-378, 1059-1086), app/llm.py (93-112, 362-372), app/settings_store.py:36, app/main.py:125-142, 3214,
app/templates/net/{spend,trace,trace_text,agent,access}.html, agents/sync/sysai_agent/hermes.py:17-70, tests/test_net_gateway.py:164-286, tests/test_net_access.py:24,151-168, tests/conftest.py:61-62, docs/opo/NET.md:111-114, docs/opo/TASKS.md:283-296,621-624.
`/tmp/research`: b_jev_wire.py (experiment), b_jev_sim.py / b_jev_alias.py (catalog arithmetic), b_jev_probe_test.py, b-patch/{jev-minimal-fix.diff, jev-probe-prototype.diff, gateway_orig.py, gateway_minfix.py, gateway_patched.py}, b-jev/models.json (live catalog), b-docs/llms-full.txt (live docs), b-ep/*.json|html (endpoint records), b-gh/ (third-party notes, Hermes sources).
