# A. Token-spend audit — where do ~273k input tokens per main-model request come from?

Date of study: 2026-11-12. Repo: `/workspace/SYSAI` @ 7ad99a7 (read-only). Language: English, compact. FACT = seen in code/doc/measurement/URL;
HYPOTHESIS = inferred, not verified. Token counts: `tiktoken` `o200k_base` (works here), unless "Hermes est." (= chars/4, Hermes' own estimator).
Measured ratios: Russian prose 3.3 chars/token, tool-definition JSON 3.8 chars/token (the brief's 2.2 chars/token for Russian would overstate by ~45%
for an o200k-class tokenizer; other tokenizers - DeepSeek, Gemini, Claude - may be 20-40% higher; the main model's tokenizer is unknown).

## 0. TL;DR (verdict first)

1. **The owner's hypothesis (MCP tool definitions dominate the context) is false for this code base.** All 82 tools of all 8 MCP servers an agent can have
   (sysai 14, plc-kit 17, machine-expert 9, pic-index 5, doc-kit 4, ebpro-cmtp 14, ebpro-gui 11, window 8) are **16.9k tokens** in the form a model sees
   (`name+description+inputSchema`; raw `tools/list` JSON 17.6k). In the configuration the code delivers by default (no ebpro servers) it is **10.9k**.
   That is 4-6% of the 273k average. And on Hermes >= 0.20 (the sync code targets >= 0.22) MCP tools are *not even sent*: a `tool_search` bridge
   replaces them (3 bridge tools + a catalog listing = **2.4k tokens**).
2. **The whole fixed prefix** (system prompt + SOUL + skills index + memory + Hermes built-in tools + MCP) is **~16k tokens (Hermes >= 0.22 default),
   ~25k (older Hermes / eager MCP), ~31k (eager, all 8 servers)** = 6-11% of 273k. Even the theoretical "everything loaded" number
   (all MCP + all 7 SKILL.md bodies + all 6 TOOL.md) is ~64k = 23%, and TOOL.md are never auto-loaded.
3. **So 89-94% of an average request (~242-257k tokens) is conversation history**: tool results, tool-call arguments (files the agent writes), skill texts,
   attachments, images, assistant text. Nothing in the SYSAI config makes that history shrink: SYSAI writes no `compression`, no `model.context_length`,
   no tool-result budget; the gateway's `/v1/models` has no `context_length`; the spend page shows **no "сжатие" role row** (3 roles only: главная, исполнитель,
   зрение) => HYPOTHESIS: Hermes never compacted these sessions.
4. A transparent what-if model calibrated on the screenshot (1 uncompacted session, 142 requests, growth ~3.5k tokens/request, ~1.2k output tokens/request)
   reproduces **all four observed numbers at once** (avg 272k, 38.8M tokens, 98.7% cache, $5.13 vs observed 273k / 38.79M / 98% / $5.05).
   In that model removing **all** MCP tool definitions saves **4-6%**; compaction at 150-200k saves **~55%**; halving tool results saves ~48%; splitting
   into stage sessions saves ~60%; five cache expiries (human pauses) cost +39%.
5. gpt-6.1-terra has a **272k-token price break** (OpenRouter catalog fetched today: prompt $1 -> $2 /M, cached read $0.05 -> $0.10 /M, output $5 -> $7.5 /M for
   requests with >= 272k prompt tokens). An average of 273k very likely puts a large share of requests (half in the model) at or above the break (HYPOTHESIS: the distribution is not known;
   the $5.05 is consistent with it). Keeping prompts under 272k is the sharpest cost lever.
6. Do not build a code-mode / search-and-execute wrapper for the tool *definitions*: the ceiling of that saving is 2-6% of dollars. A code-mode idea that
   keeps big *results* out of the context is worth something, but the cheap fixes (compaction trigger, result caps, stage sessions) get 10x more.

## 1. What ONE request of the engineer's agent consists of

Chain (FACT): desktop window "Агент SYSAI" -> `hermes -p <profile> acp` (`agents/desktop/main.js:335-339`) -> Hermes -> `POST <server>/v1/chat/completions`
(SYSAI gateway, `app/net/routes.py:1067`) -> OpenRouter -> model. Request body = OpenAI chat format:
`[system] + tools[] + messages[user, assistant(tool_calls), tool, ...]`. The gateway forwards it unchanged except `DROP=("models","route","user","sysai")`,
adds `user=<agent>` and `usage.include` (`app/net/gateway.py:289-297`).

| # | Part | Where it comes from (file:line) | Size | Always / on request |
|---|---|---|---|---|
| 1 | Hermes system prompt (identity slot, guidance, skills index, memory, date line, runtime hints) | Hermes `agent/system_prompt.py:751-815` (3 tiers stable/context/volatile) | 2.4k stable + ~0.65k volatile (GPT variant) | always |
| 2 | SOUL.md = COMMON + dept "ОПО" (replaces Hermes default identity) | `app/net/soul.py:12-59,81-95`; written by `agents/sync/sysai_agent/hermes.py:125-132` | 3,189 chars = **975 tok** | always |
| 3 | Skills index (`<available_skills>`: name + description clipped to 60 chars) | skills in `${SYSAI_HOME}/hub` via `skills.external_dirs` (`access.py:561`); `hubsync.py:105`; profile created `--no-skills` (`agents/installer/install.ps1:92`) | 7 hub skills ~0.15k + header ~0.25k = **~0.4k** | always (index only) |
| 4 | Skill bodies (SKILL.md) | loaded by the model with `skill_view`; Hermes' index text says "you MUST load it ... err on the side of loading" (`agent/prompt_builder.py:1349-1353`) | plc-project 3.6k, ebpro-project 1.8k, ebpro-easybuilder 1.4k, project-docs 1.4k, ebpro-donor 1.3k, ebpro-team 1.3k, ebpro-mnemoscheme 1.2k = **12.1k if all 7** | on request, but nudged to load; once loaded it stays in history |
| 5 | Memory blocks (MEMORY.md, USER.md) | `memory_enabled/user_profile_enabled: true` (`access.py:558`); Hermes caps 2,200 + 1,375 chars (`config_defaults.py:1302-1303`) | 0-1.3k | always |
| 6 | Hermes built-in tools (`hermes-acp` set, ~21 tools) | `toolsets.py:186`, ACP session `acp_adapter/session.py:103-106` | **9.6-10.1k** (Hermes fixture `tests/fixtures/prompt_surface/surfaces.json`; my stub-extraction of 20 tools: 7.9k) | always |
| 7 | MCP tools: `sysai` (HTTP `/mcp`), hub servers via `sysai-agent mcp <name>`, + `window` server given by the desktop to every session (`main.js:299,420-422`, 8 tools) | `access.py:504-572`; `mcpwrap.py:164-170` (only filters tools by rules, changes nothing in size) | eager **10.9k** (57 tools, default) .. **16.9k** (82); deferred **1.9k..2.8k** | always (see 1.2) |
| 8 | Conversation history: user messages (+ inlined attachments), assistant text/reasoning, tool-call arguments, tool results, skill bodies, images | Hermes keeps everything until compaction | **the rest: ~242-257k avg** | grows every turn |

### 1.1 Which hub items are "always" vs "on request" (FACT from code; production DB state NOT verified)
- Every new hub item is created `delivery="always"` (`app/net/hub.py:464`); `on_demand` is a per-item switch in the hub UI (`hub.py:48-49`, `routes.py:594`). Only
  `delivery == "always"` or "taken" items go into the manifest (`hub.py:845-862`) and onto the PC. Tools additionally need the server name in the profile's
  `mcp` rules (`hub._allowed`, `hub.py:802-811`).
- Code-default profile "Инженер ОПО" (`access.py:81`): `mcp = {sysai, plc-kit, machine-expert, pic-index, doc-kit, cad}` - **`ebpro-cmtp` and `ebpro-gui` are NOT
  allowed** (cross-area finding: EasyBuilder tools are not delivered unless the production profile was edited; `cad` has no hub item). The 7 skills (incl. five
  `ebpro-*`) are delivered to everyone ("always", audience all) and mention tools that may not exist.
- `TOOL.md` folders live in `${SYSAI_HOME}/tools`, not in `skills.external_dirs`, so TOOL.md texts are never in the prompt (only if the agent reads them:
  `hub_get` returns `text[:20000]`, `mcp.py:152`).

### 1.2 Hermes version matters a lot for MCP tool definitions (FACT: Hermes source at tags + release notes via GitHub API)
- The sync code targets Hermes **>= 0.22** ("Hermes 0.22 and later - the one multiplexing gateway", `hermes.py:211`; `agents/desktop/lib/acp.js:3`). 0.22.0 = 2026-10-14;
  latest 0.23.1 = 2026-11-10. The installer installs whatever the official script gives today (`install.ps1:78`), no pin.
- Hermes `tool_search` ("progressive tool disclosure"): in v0.16.0-v0.19.x (2026-06-06 .. 07-30) `auto` activated only if deferrable MCP schemas were >= **10%** of the context window
  (>= 20k tokens when the window is unknown); for SYSAI the window is the 256k fallback => 25.6k threshold > our 11-17k of definitions => **eager** in those versions.
  **Since v0.20.0 (tag v2026.8.3, "tiered disclosure, July 2026") and in 0.21/0.22/0.23: ANY MCP tool activates the bridge** (`tools/tool_search.py`, `should_activate`,
  `is_deferrable_tool_name`: "an MCP tool ... is eligible"; verified in the files at tags v2026.6.5, v2026.8.3, v2026.8.31, v0.22.0); default `tools.tool_search.enabled: "auto"` (== on)
  (`hermes_cli/config_defaults.py:1968-2004`).
  In the model's tool array MCP tools are replaced by `tool_search`/`tool_describe`/`tool_call`; the description of `tool_search` embeds a listing
  `- name: first sentence (<=80 chars)` per tool, budget `min(4000 tokens, 5% of context)`.
- Measured with Hermes' own catalog code (`tools/tool_search_catalog.py`, run on a scratch copy with stubs, `/tmp/research/tok/listing.py`):
  default-engineer set (57 tools) listing 6,008 chars = 1.9k tok, bridge total 2.4k; all 8 servers (82 tools): 2.8k / 3.4k.
  Tool schemas then reach the context only through `tool_describe` results (~0.2-0.4k per tool actually used).
- Hermes' own A/B (288 live runs incl. gpt-5.6-terra, `evals/core_tool_deferral/results/SUMMARY.md`): shrinking the tool schema surface by 56%
  (47.4 KB -> 21.0 KB) cut tokens by only **7-23%** in 6-9-turn tasks (more turns +1-2 for discovery). In 100+ turn sessions the share is far smaller.
- Search quirk: BM25 indexes only `[A-Za-z0-9]+` tokens (`tool_search_catalog.py:_TOKEN_RE`), so Russian description text is not searchable; names are English, so it works.
- HYPOTHESIS: the engineers' PCs run >= 0.22 (>= 0.20 is enough) and therefore already defer MCP tools. Verification in section 11 (V1/V2) takes 5 minutes.
  Version matrix for the fixed prefix: Hermes < 0.20 -> MCP eager (11-17k); >= 0.20 -> deferred (2.4-3.4k).

## 2. Measurements

### 2.1 How measured
- Hub servers (6): imported `TOOLS` from a **scratch copy** of `hub/seed` (`python -I -B`, nothing written to the repo; `ebpro-cmtp` needed `cryptography`, `uvx`):
  `/tmp/research/tok/dump_tools.py`. `sysai` MCP: AST-extracted the `TOOLS` dict of `app/net/mcp.py` without importing the app
  (`dump_sysai_mcp.py`; same output as `tools_list()` for a rule set with all tools open). `window` MCP (desktop): `node` required `lib/uimcp.js` from a scratch copy
  (`dump_window_tools.js`). All numbers in `/tmp/research/tok/measure_out.json`.
- "raw" = compact JSON of the tools as `tools/list` returns them (incl. `title`, `annotations`). "model-visible" = what Hermes sends:
  `{"type":"function","function":{"name":"mcp__<server>__<tool>","description":..., "parameters": inputSchema}}` (Hermes `tools/mcp_tool_schema.py:172,_convert_mcp_schema`).
  `title`, `annotations` and server `instructions` are not part of tool definitions. Provider-side rendering of tools can add/remove ~10-15%.

### 2.2 MCP servers (tokens, o200k)
| server | tools | raw chars | raw tok | model-visible chars | **model-visible tok** | of which descriptions | schemas | `instructions` tok (not tools) |
|---|---|---|---|---|---|---|---|---|
| sysai (`app/net/mcp.py`) | 14 (10 if no storage section opened: `files_*` hidden, `mcp.py:412-424`) | 4,871 | 1,209 | 4,949 | **1,265** | 458 | 497 | 162 |
| plc-kit | 17 | 14,800 | 3,878 | 13,893 | **3,695** | 1,517 | 1,741 | 315 |
| machine-expert (Windows) | 9 | 8,059 | 2,026 | 7,583 | **1,909** | 633 | 1,067 | 204 |
| pic-index | 5 | 4,469 | 1,163 | 4,198 | **1,098** | 414 | 571 | 153 |
| doc-kit | 4 | 4,007 | 1,144 | 3,814 | **1,104** | 528 | 483 | 173 |
| window (desktop, every session) | 8 | 7,420 | 1,958 | 6,992 | **1,845** | 716 | 956 | 0 |
| **subtotal = code-default engineer set** | **57** | 43,626 | 11,378 | 41,429 | **10,916** | | | |
| ebpro-cmtp (not in default `mcp` rules) | 14 | 15,564 | 4,030 | 14,858 | **3,882** | 1,583 | 1,944 | 133 |
| ebpro-gui (Windows, not in default rules) | 11 | 8,180 | 2,226 | 7,728 | **2,125** | 464 | 1,405 | 104 |
| **ALL** | **82** | 67,370 | 17,634 | 64,015 | **16,923** | 6,313 | 8,664 | |

Biggest tools (tokens, model-visible): `cmtp_edit` 1,487; `canvas_edit` 815; `doc_export` 494; `me_online` 463; `plc_project_build` 412; `me_create` 366; `eb_click` 331;
`cmtp_render` 331; `pics_label_update` 315; `plc_hmi_tags` 299. Average 206 tok/tool (sysai 90). Per-tool numbers for all 82:
sysai: hub_propose 142, hub_search 125, files_write 123, files_find 97, files_read 97, hub_take 93, people 89, log_event 88, notify_me 82, files_list 81, hub_get 75, hub_drop 69, whoami 60, departments 44.
plc-kit: plc_project_build 412, plc_hmi_tags 299, plc_stage_update 288, plc_link 279, plc_tag_check 249, plc_io_map 236, plc_variables 210, plc_info 197, plc_style 192, plc_project_template 192, plc_check 184, plc_export_st 184, plc_build 177, plc_io 163, plc_addresses 159, plc_pou 148, plc_pous 126.
ebpro-cmtp: cmtp_edit 1487, cmtp_render 331, cmtp_scene 261, cmtp_objects 224, cmtp_check 209, cmtp_pack 206, cmtp_relabel 183, cmtp_links 167, cmtp_extract_images 153, cmtp_tags 143, cmtp_labels 142, cmtp_unpack 131, cmtp_windows 123, cmtp_info 122.
(Others in `measure_out.json`.) The mcpwrap wrapper (`mcpwrap.py:164-170`) only removes tools the rules forbid; the profile gives `["*"]`, so nothing shrinks.

### 2.3 Skills, tool docs, SOUL, Hermes baseline (tokens)
| item | chars | bytes (Hermes `prompt-size` shows bytes) | tokens |
|---|---|---|---|
| SKILL.md plc-project | 11,663 | 18,276 | 3,633 |
| ebpro-project / ebpro-easybuilder / project-docs | 5,626 / 4,704 / 4,704 | | 1,756 / 1,445 / 1,411 |
| ebpro-donor / ebpro-team / ebpro-mnemoscheme | 4,363 / 4,171 / 4,535 | | 1,336 / 1,257 / 1,217 |
| **all 7 SKILL.md** | 39,766 | | **12,055** (descriptions alone: 688 full, ~150 clipped to 60 chars) |
| TOOL.md ebpro-cmtp / plc-kit / doc-kit / machine-expert / pic-index / ebpro-gui | 22,265 / 15,121 / 7,949 / 7,786 / 7,006 / 5,550 | | 7,011 / 4,630 / 2,504 / 2,295 / 2,236 / 1,679 = **20,355** (never auto-loaded) |
| SOUL.md (COMMON 2,184 chars + ОПО 895 chars + 2 header lines) | 3,189 | | **975** (limit `MAX_TEXT=8000` chars per text, `soul.py:10`) |
| `whoami` answer (rules JSON, engineer) | 1,690 | | 588 (SOUL says call it first) |
| Hermes system prompt (recorded fixtures, chars/4): cli.gpt stable 2,420 + volatile 1,264; desktop.claude 1,495 + 1,269 | | | 2.5-3.7k |
| Hermes built-in tool schemas (fixture `surfaces.json`): 21 tools (9,464-9,620), desktop 22 tools (10,120) | | | 9.5-10.1k |
| Hermes built-ins, my extraction (20 of the `hermes-acp` tools, `/tmp/research/tok/hermes_schemas.py`) | 35,016 | | 7,894 |

### 2.4 Fixed-prefix sums
| scenario | components | tokens | share of 273k |
|---|---|---|---|
| **(b1) default config, Hermes >= 0.22 (MCP deferred)** | system 3.1k + SOUL net +0.8k + memory ~0.8k + built-ins 9.6k + MCP bridge listing 1.9k | **~16k** | 6% |
| (b2) default config, older Hermes / `tool_search: off` (MCP eager, 57 tools) | same but MCP defs 10.9k | **~25k** | 9% |
| (b3) eager, EasyBuilder servers also allowed (82 tools) | MCP defs 16.9k | **~31k** | 11% |
| **(a) "everything loaded"** (theoretical max) | b3 + all 7 SKILL.md bodies 12.1k + all 6 TOOL.md 20.4k | **~64k** | 23% |
Add ~1k for the first `whoami` and ~12k if the agent loads all skills (it is told to). Worker sub-agents carry the same fixed prefix (MCP/toolsets inherited:
`delegation.inherit_mcp_toolsets: true`, Hermes `config_defaults.py:1341`).

## 3. Tools that can return LARGE outputs, and the caps

Row cost (measured on rows shaped as the code builds them, o200k): plc_variables row 94 tok; plc_hmi_tags (modbus) 99; OPC UA tag row 93 + add_tags row 18; plc_check issue 43;
plc_io row ~54; cmtp_tags row 32; cmtp_objects row ~110 (guess); files_list item 98.

| tool (file:line) | cap in SYSAI code | typical / worst answer (tokens) |
|---|---|---|
| sysai `files_read` (`mcp.py:233`, `files.py:30,358-373`) | text file inline up to **200,000 bytes**, whole text, no paging | 30-55k (a file of 100-200 KB) |
| sysai `files_list` / `files_find` (`mcp.py:213,225`) | 200 / 100 items | up to 19.6k / 9.8k |
| sysai `hub_get` (`mcp.py:152`) | `text[:20000]` chars + up to 300 file entries | ~6k |
| plc-kit `plc_variables` (`server.py:323`) | **3,000 rows** | 600 vars = 56k; 1,500 = 141k; cap 282k |
| plc-kit `plc_hmi_tags` (`server.py:356-359`) | **3,000 rows**, OPC UA returns `tags` AND `add_tags` | 588 tags (M241 example in ebpro TOOL.md) = 65k; cap 333k; `out_file` CSV exists |
| plc-kit `plc_io` (`:284`), `plc_check` (`:376`) | 3,000 channels / 2,000 issues | 162k / 86k worst |
| plc-kit `plc_pou` (`:307-317`) | none (full ST of a POU) | 1-6k typical |
| ebpro-cmtp `cmtp_tags` / `cmtp_labels` / `cmtp_check` (`server.py:328,374,391`) | 5,000 / 2,000 / 3,000 rows | 160k / ~60k / ~130k worst |
| ebpro-cmtp `cmtp_objects`, `cmtp_scene`, `cmtp_links` (`:363-386`) | **no cap** (whole window / all links) | HYPOTHESIS 5-35k per window (30-100 objects x 110-350 tok) |
| ebpro-cmtp `cmtp_render show:true` (`:403`), ebpro-gui `eb_shot` + every action with `shot` (`server.py:128-172`) | PNG <= 3 MB base64 image per call | image tokens ~1.5k each (Hermes' default image cost estimate) |
| doc-kit `doc_text` (`server.py:51,95`, `dockit.py:769`) | default 60,000 chars, caller may raise | ~18-20k |
| pic-index `pics_find/list/check` (`server.py:52`, `picindex.py:519,544,626`) | 100 sets / 1,000 rows / 2,000 issues; update returns 50 unrecognized | up to ~15-40k |
| machine-expert `me_build` etc. (`server.py:130,221-254`) | errors 40, warnings 15, lists 60 | <1k (the one well-behaved server) |

Hermes-side caps (upstream main, `tools/budget_config.py:11-20`, `tools/tool_output_limits.py:19-21`, `tools/mcp_tool_content.py:129`): MCP result > **50,000 chars** is spilled to disk,
the model sees a 1,500-char preview + path (`<persisted-output>`); other tools 100,000 chars; **200,000 chars per turn** across results; `terminal` output 50,000;
`read_file` is pinned "unlimited" (2,000 lines x 2,000 chars per read, no spill); `execute_code` stdout 50 KB; `delegate_task` summary <= 24,000 chars
(`config_defaults.py:1348`); images from MCP results: up to 4 attached natively per result; a native image embedded in history "is re-sent on every later API call"
(`tools/vision_tools_history_budget.py:1-10`; default 256 KB per embed, main agent unlimited repeats). Consequence: a single MCP result is bounded to ~12-25k tokens,
but 5-10 such calls per stage are the 60-250k that fill the history; old results are never pruned unless compaction runs (`proactive_prune_tokens: 0`, `micro_compact: false`).

Attachments (FACT, cross-area): the desktop sends the paperclip files as `resource_link` with `file://` URIs (`agents/desktop/renderer/js/app.js:send`, `main.js:660`);
Hermes' ACP adapter **inlines text files up to 512 KB into the user message** and images <= 512 KB as image parts, binaries as "[Binary file omitted]"
(`acp_adapter/content.py:22,147-190`). A 400 KB CSV export attached once = ~130k+ tokens in every later request. Not verified whether the engineers attach such files.

## 4. Compaction ("сжатие") and prompt caching

### 4.1 What SYSAI writes into the Hermes profile (FACT)
`hermes.py:17-19` OWNED = model, fallback_providers, delegation, approvals, memory, mcp_servers; parts: providers.sysai, auxiliary.vision/compression, skills.external_dirs/create_dir,
security.redact_secrets, hooks.pre_tool_call. Rendered in `access.py:539-571`:
- `model`: provider custom, base_url `<server>/v1`, default `sysai/<main>` (profile `main`, default `auto`), api_key `${SYSAI_AGENT_TOKEN}`. **No `context_length`.**
- `delegation`: `sysai/<workers>` (default cheap), max_concurrent_children 3, max_spawn_depth 1, `request_overrides.extra_body.sysai.role=workers`.
- `auxiliary.compression`: custom, base_url, model `sysai/<_plain(cheap,mid,strong)>`, role "compression"; `auxiliary.vision`: `sysai/<vision|mid|strong|cheap>`, role "vision".
  **No `api_key`** in the auxiliary blocks: works only through Hermes' same-origin key inheritance (upstream main `agent/auxiliary_client.py:4939-4943`); if the PC's Hermes lacks it
  the aux calls get 401, which the gateway does not record anywhere (auth fails before `record()`, `routes.py:122`).
- **NOT written (so Hermes defaults apply)**: `compression.*` (enabled true, threshold 0.50, threshold_tokens null, tail lean, protect_last_n 20; `config_defaults.py:563-601`),
  `tools.tool_search`, `tool_budget`, `tool_output`, `vision.*`, `agent.disabled_toolsets`, `model.context_length`.
  `grep` of `agents/` and `app/net/` for threshold/context_length/tool_search/tool_budget finds nothing.

### 4.2 When would Hermes compact? (FACT from upstream main; PC version unverified)
- Trigger = `threshold x context_length`; for windows **< 512k the floor is 0.75** (`agent/context_compressor.py:1150-1151,2751-2755`). The window is learned from `GET <base>/models`
  `context_length`; otherwise fallback **256,000** (`agent/model_metadata.py:261-262,1086-1095,2052-2081`; warning "Could not determine context length ... falling back to 256,000").
  The SYSAI gateway `models_list()` returns `{id, object, created, owned_by, name}` only (`gateway.py:515-518`) => Hermes believes **256k** for every `sysai/*` tier => trigger **192k**.
- Real windows (OpenRouter catalog, fetched 2026-11-12): gpt-6.1-terra 1,050,000; deepseek-v4.1-flash-1017 1,048,576; gemini-2.5-flash(-lite) 1,048,576; claude-haiku-5.5 1,000,000.
  If Hermes knew the true window the trigger would be 0.50 x 1.05M = **525k** - above the 272k price break, so useless for cost.
- Observed: average 273k with 1.05M-window model and **no** "сжатие" row in the "Кто спросил" card (screenshot `acead0c5...png`, 3 rows). A 192k trigger and a working summariser
  would keep the average near 120-130k (model: 118k), so either compaction is not firing, or fails (auth/401 on the aux call; cooldown 60-900 s after a failure; "abort, transcript kept" on auth/network/overload failures,
  `context-compression-and-caching.md`), or the PC's Hermes behaves differently. HYPOTHESIS; checks V3/V4 in section 11.
- Compaction when it fires (lean mode): one aux LLM call; tail 10-25k tokens + summary + verbatim user messages; defaults `micro_compact false`, `proactive_prune_tokens 0`.
  The team's own test (TASKS.md ОПО-5.8, Sept 2026): Hermes compression keeps 78.9% of facts at 55k tokens (vs Jev 75.5% at 115k).

### 4.3 Caching (ОПО-5.5) - what the gateway does, what breaks prefixes
- Gateway: passes `messages`/`tools`/cache marks unchanged => prefix stability is Hermes' job; for Claude/Kimi tiers declares `providers.sysai.models[...].prompt_caching: true`
  (`access.py:567-571`, `gateway.cache_marks` `:133-137`); OpenAI/DeepSeek/Gemini cache automatically (observed 98% / 96% / 75%).
- Hermes keeps the prompt byte-stable on purpose (3 tiers, date-only line "Conversation started: <weekday, date>", frozen memory snapshot, deterministic tool order, listing "byte-stable";
  `system_prompt.py:751-815,508-532`). Nothing random found in the SYSAI-owned parts (SOUL has person+department names only; hub skill list changes only on hub revision, which restarts Hermes).
- What really breaks or wastes cache here (ranked): (1) **cache expiry during human pauses** - approval/"may I create this file?" prompts every few minutes; an expired 300k-token prefix is re-read at the
  uncached price ($1-2/M instead of $0.05-0.10/M); (2) **`sysai/auto` (Jev) as main model**: model switches between requests = no cache (default `main: auto` in `access.py:84,102`; the lead profile copies the engineer's, `:91`; Jev currently failing);
  (3) compaction rewrite (one miss per compaction, by design); (4) fallback to the spare model (`models:[a,b]`, `gateway.py:92-98`); (5) HYPOTHESIS: OpenRouter provider rotation for DeepSeek/others
  (gateway sends no `provider` preference). OpenRouter lists `input_cache_write` $1.25/M for gpt-6.1-terra: new tokens that get cached may be billed at the write price.

## 5. Gateway / spend / trace

- Per request recorded (`NetSpend`, `app/net/models.py:193-216`, written by `gateway.record():391-419`): day, at, agent, user, **tier**, **model that answered**, project, skill, prompt_tokens,
  completion_tokens, **cached_tokens**, cost (OpenRouter `usage.cost`), ms, ok, error, **role**, task, route. NOT recorded: number of messages/tools, system/tools/history/last-result size, images,
  reasoning tokens, request index in the conversation, cache-write tokens. Debug mode (24 h, kept 7 days) stores `trace.request_excerpt()` (`trace.py:119-147`): `messages` count, `system_chars`,
  tool **names**, tail of last <= 8 messages (<= 8,000 chars each) - the only way today to see the shape of a request.
- Spend page (`app/net/spend.py:15-18,53-113`, `templates/net/spend.html`): groups agent/project/skill/tier/model/role; "Токенов" = prompt + completion; "кэш %" = cached/prompt.
  Trace page groups steps into runs by task or 15-min gaps (`trace.py:17,225-267`).
- Roles (`trace.py:19-20`, tag `sysai.role` set by Hermes config): **главная** = no tag = Hermes `model.default` (planning, writing ST, tool calls; the background memory/skill review forks inherit the main route
  - FACT, `agent/background_review.py:1-8`; other aux calls such as titles probably too - HYPOTHESIS); **исполнитель** = `delegation.model` (sub-agents from `delegate_task`, <= 3 parallel, depth 1, up to 250 iterations each); **сжатие** = `auxiliary.compression`; **зрение** =
  `auxiliary.vision` (`vision_analyze`, returns text).
  Reading the screenshot (HYPOTHESIS for model->role, mapping is in the production `agents_models` setting): gpt-6.1-terra 142 req / $5.05 / 96% = главная; deepseek-v4.1-flash-1017 80 req, 154k avg, $0.14 = исполнитель
  (workers run big contexts but on a $0.15/M model, so cheap); gemini-2.5-flash-lite 14 req, 45k avg = зрение; singles (gpt-4.1-nano = `DEFAULT_FALLBACK["cheap"]`, claude-haiku-5.5, gpt-6-luna, gemini-2.5-flash) = spare/Jev picks/tests.
- Tiers: `DEFAULT_MODELS` cheap gemini-2.5-flash-lite, mid gemini-2.5-flash, strong gemini-2.5-pro, vision "" -> `vision_model` setting or gemini-2.5-flash (`gateway.py:35-39,115-120`); real models come from setting
  `agents_models` (not in repo). Built-in profiles: main `auto`, workers `cheap` (`access.py:84-85`).
- `sysai/auto`: Jev Router plugin with `models` = the agent's tier models (incl. the **vision** tier model), `excluded_models` = every catalog model dearer than the dearest of them, `cost_tier` from profile;
  Jev unavailable -> `mid` (`gateway.py:191-205,220-245`). Errors: "—" rows = refused before a model was chosen (budget_exceeded / model_not_allowed / no_key, `prepare()` raises before `plan.model` is set, `spend.py:20-24`;
  HYPOTHESIS: the $3/day default budget or the company daily limit); "typesafe/jev-router" rows = Jev failures with a status not in `FALL_BACK_ON` (402/403/429...) - Jev is another agent's area.
- Budgets: per agent per day (engineer $3, lead $5, TO $2, default $1; `access.py:63-107`), per project ($30/$60/0), company daily (`llm.check_budget`). **Per-project budget and the Projects/Skills/Task columns are inert for Hermes traffic**:
  they come only from headers `X-SYSAI-Project/-Skill/-Task` (`routes.py:125,1076-1077`) and nothing in `render_hermes` or the desktop sets them (grep: only tests do) => "без проекта" for everything.
- 401 (bad token) and requests that never reach the gateway are invisible to spend/trace.

## 6. Bottom-up vs observed (what is explained, what is not)

Observed (screenshot, FACT): gpt-6.1-terra 142 req, 38.79M tokens (prompt+completion) = 273k/req, cache 98%, $5.05 ($0.0356/req); deepseek 80 req, 154k/req, 96%, $0.14.

| layer | tokens/request | share of 273k | status |
|---|---|---|---|
| fixed prefix (system, SOUL, skills index, memory, built-ins, MCP) | 16k (deferred) / 25k (eager) / 31k (eager, all 8) | 6% / 9% / 11% | **explained, measured** |
| skill bodies loaded (1-7 skills) | 1.3-12k (plc-project alone 3.6k) | <= 4% | explained, upper bound |
| `whoami`, `hub_*`, `plc_info`... small results | ~2-5k | ~1-2% | explained |
| **history growth: tool results + tool-call arguments (files written) + assistant text/reasoning + images + attachments** | **~225-255k average** (the rows above are part of the 242-257k history) | **~82-93%** | **NOT explained by code; composition unknown** |

What the code tells about history (FACT/estimates): single MCP results up to 12-25k tokens inline (Hermes cap) but frequently 20-60k before the cap (variables/tags tables, section 3); 5-10 such calls per stage;
the agent writes whole files through tool calls (screenshot of the project folder: ~160 KB of .st/.py/.md/.csv = 50-60k tokens as arguments; GVL_IO.st 42 KB alone ~14k) and reads them back; terminal outputs up to 50k chars;
`project.md` is read at every stage start (the skill says so); images.
Cross-check from the cache rate (FACT arithmetic): 2% uncached of ~38.6M prompt tokens = ~0.77M uncached = ~5.4k new tokens per request on average. If the history grows by g tokens per request,
a single linear session of 142 requests needs g ~3.5k to give avg 273k (it ends at ~520k, uncached share 1.3%); two sessions of 71 need g ~7k (uncached 2.7%). The observed 2% sits between:
**1-2 long uncompacted sessions** is consistent with all four numbers (HYPOTHESIS - not unique).

### 6.1 What-if model (NOT a measurement; `/tmp/research/tok/sim.py`)
Per request i: P_i = P_(i-1) + g; cost_i = P_(i-1) x c_cache + g x c_in + out x c_out; prices from the OpenRouter catalog (gpt-6.1-terra): <272k: in $1.00, out $5.00, cached $0.05; >=272k: $2.00 / $7.50 / $0.10.
Base: N=142, F=25k, g=3.5k, out=1.2k -> avg 272k, last 518k, 38.8M tokens, cache 98.7%, **$5.13** (observed $5.05); 50% of requests >= 272k.
Dollar split in the base: cached reads **$3.30 (64%)**, new/uncached input $0.77 (15%), output $1.06 (21%). So "cost ~ cached tokens x cached price" is ~2/3 of the money, and
cached tokens = sum over requests of the history length = N x average context; cost grows ~quadratically with session length. Not resolvable from the screenshot alone (output tokens unknown):
with all requests in the cheap tier the same $5.05 needs ~0.48M output tokens (47% of dollars); with half the tokens in the >272k tier ~0.2M. The CSV (`/agents/spend.csv`, columns "токенов ответа", "из кэша") settles it.

| lever (on the calibrated base) | cost | vs base |
|---|---|---|
| remove ALL MCP tool defs, default set (-10.9k) | $4.94 | **-4%** |
| remove ALL MCP defs, all 8 servers (-16.8k) | $4.84 | **-6%** |
| remove MCP defs AND Hermes built-ins (-27k) | $4.66 | -9% |
| compaction at 200k -> 45k (3 compactions) | $2.41 | **-53%** |
| compaction at 150k -> 45k (4) | $2.26 | **-56%** |
| compaction at 100k -> 40k (7) | $2.21 | -57% |
| tool results/turn -30% (g 3.5k -> 2.45k) | $3.49 | -32% |
| tool results/turn -50% (g -> 1.75k) | $2.67 | -48% |
| 5 cache expiries (human pauses) | $7.15 | **+39%** |
| compaction 150k + results -30% | $2.06 | -60% |
| same 142 requests as 3 / 5 / 7 stage sessions (47 / 28 / 20 requests) | $2.12 / $1.91 / $1.86 | -59% / -63% / -64% |
Compaction costs included (cheap summariser, ~$0.03 each). Quality loss of summaries is NOT modelled. DeepSeek workers: $0.14 for 12.3M tokens - context size is not a cost problem there.

## 7. Logging proposal: per-request anatomy on spend/trace pages

Goal: see, per request and per run, tools-definition tokens, system tokens, history tokens, last-tool-result tokens, image tokens, request index.
1. `app/net/trace.py` - new `anatomy(body) -> dict` next to `request_excerpt()` (reuse `text_of`): `n_messages`, `n_tools`, `tools_chars` (len of `json.dumps(tools)`), `system_chars`,
   `history_chars` (all non-system), `last_result_chars` + `last_result_tool` (the trailing run of `role=="tool"` messages after the last assistant), `n_images` + image data-url chars,
   `turn` = count of `assistant` messages (request index in the conversation, no state needed), `conv` = short sha1 of system[:200]+first user message[:2000] (groups requests of one conversation),
   `tool_mode` = "bridge" if tool names include `tool_search` else "eager", and `project` parsed from the first user message ("Папка проекта: ...", `agents/desktop/lib/projects.js:211`) when no header.
2. `app/net/gateway.py::prepare()` (after line 304) `plan.anatomy = trace.anatomy(body)` (always; O(size), tens of ms at 500k tokens); `record()` converts chars to tokens by scaling the provider's `prompt_tokens`
   proportionally (`tok_part = prompt_tokens * part_chars / total_chars`; images counted separately, ~1.5k each; flag as estimates) and stores them; also store `completion_tokens_details.reasoning_tokens`
   and cache-write tokens from `usage`.
3. `app/net/models.py::NetSpend` + `app/db.py:1209-1212` (the `_add_column_if_missing("net_spend", ...)` list): new nullable columns `n_msgs, n_tools, tok_tools, tok_system, tok_history, tok_last_result,
   last_result_tool, n_images, turn, conv, reasoning_tokens`.
4. `app/net/spend.py`: new group "Размер запроса" (buckets of prompt_tokens: <50k, 50-100k, 100-200k, 200-272k, >=272k) with requests, cost share and cache%; a cost split cached/new/output from tokens x catalog prices
   (`gateway.prices()` currently keeps only prompt/completion; extend with `input_cache_read` and `overrides`); CSV columns. `trace._model_step()` + `templates/net/trace.html:34` "Подробности": "запрос №N беседы abc123 ·
   инструменты 1.9k · система 3.9k · история 212k · последний результат plc_variables 19k · 2 картинки"; run header: peak prompt, number of compactions (a drop in prompt_tokens between consecutive steps), share of requests >= 272k.
5. Zero-code today: Trace -> "Режим отладки" (24 h) -> run one task -> open "текст" of a model step: `messages`, `system_chars`, `tools` names (tells eager vs bridge) - see V1.

## 8. Honest verdict on the hypothesis and on code-mode wrappers

- Share of an average 273k request (FACT where measured, else estimate): tool definitions 4% eager (10.9k) .. 6% (16.9k) .. ~1% deferred (2.4k) [+ ~0.2-0.4k per described tool in history]; system+SOUL+memory ~2%;
  Hermes built-in tools ~3.5%; skills (index 0.15%, bodies <= 4%); **history (tool results, written files, assistant text, images, attachments, loaded skill bodies) ~89-94%**.
- Dollar value of the *maximum possible* saving from any definition-trimming scheme (Cloudflare-style Code Mode on the definitions, per-stage tool filtering, deferred loading): 17k tokens x 142 requests x $0.05-0.10/M
  = $0.12-0.24 of $5.05 (2-5%), model: 4-6%. Already ~0 if the PCs run Hermes >= 0.22. The claimed "99.3%" refers to cutting *definition* tokens (Cloudflare: thousands of API endpoints -> two tools);
  here there are 82 tools of 206 tokens.
- What does move the bill: (1) **history length** (compaction / stage sessions / pruning): -50..-65%; (2) **big tool results and written files** (caps, paging, "summary first"): -30..-48%;
  (3) the **272k price break** (disappears with (1)); (4) **cache expiries** from human pauses (+39% for five); (5) too many turns.
  Per-stage tool filtering is not needed for cost; it may still help the model's accuracy (not studied).
- A "code mode" that keeps big results out of the context is the one useful kernel of the idea: Hermes' `execute_code` cannot call MCP tools (sandbox only `web_search, web_extract, read_file, write_file,
  search_files, patch, terminal`, `tools/code_execution_tool.py:43-45`), but the hub tools are plain Python on the PC (`%SYSAI_HOME%\tools\...`): a small "plc/cmtp query" CLI/`jq`-like filter returning
  counts + the matching rows would remove the need to read 600-row tables. This is the indexer direction (owner point 8) and should follow the cheap fixes, not replace them.
- Hermes ships the search-and-execute mechanism natively for definitions (`tool_search`); do not duplicate it.

## 9. Ranked cheap fixes in THIS code base (estimates from the what-if model; risk = my judgement)

| # | Fix (file / knob) | Saving | Risk |
|---|---|---|---|
| 1 | **Make compaction fire at a sane size.** `agents/sync/sysai_agent/hermes.py:17-19`: add `compression` to `OWNED_PARTS` (`enabled`, `threshold_tokens`); `app/net/access.py:539-566`: emit `"compression": {"enabled": True, "threshold_tokens": 150000}` (documented key: "fires at the lower of the ratio trigger and this count"). Also `gateway.models_list()` (`:515-518`) add `context_length` per tier (from the OpenRouter catalog the gateway already loads in `prices()`), so Hermes sizes thresholds/tool budgets to the real window instead of the 256k fallback. | **-50..-55%** of main-model $ in long sessions (also kills the >272k tier) | low-medium: summaries lose detail (lean mode keeps user messages verbatim, anchors; project.md/pipeline.json hold the state) |
| 2 | **One session per stage.** Desktop: `agents/desktop/renderer/js/app.js:385` (`ask: composer.fill`) + `projects.js:187` - `newDraft()` before filling the stage prompt; the skill already says "do not rely on the conversation, state is in project.md / plc_stage_update" (`plc-project/SKILL.md`). | **-59..-64%** | medium: UX, continuity (related to "dialogs not saved" point; coordinate with UX agent) |
| 3 | **Cap and page the big results at the source.** `hub/seed/plc-kit/server.py:284,323,356-359,376` (default 200 rows + `offset` + `count`, point to `filter`/`out_file`); `ebpro-cmtp/server.py:328,374,363-391` (same; `cmtp_scene/objects/links` summary-first); `app/net/files.py:30` `MAX_INLINE_READ` 200,000 -> 40,000; `doc-kit/server.py:51` default 60,000 -> 20,000 chars. Add one sentence to the tool descriptions ("use filter, never dump"). | -30% of history growth => **~-32% $** | low-medium (agent must page; plc-kit/ebpro-cmtp need a new hub version) |
| 4 | **Hermes-side result budgets in the synced config**: `tool_budget.mcp_result_size_chars: 20000` (read by Hermes `tools/budget_config.py:_configured_mcp_result_size`), `tool_output.max_bytes: 20000` (terminal); add `tool_budget`/`tool_output` to `OWNED_PARTS`. Spilled results stay readable via `read_file`. | part of #3 for all tools immediately, no hub release | low |
| 5 | **Proactive prune** of old big tool results (no LLM): `compression.proactive_prune_tokens: 48000` (opt-in; breaks cache prefix episodically; `config_defaults.py:596-603`). | 15-30% of growth | low-medium |
| 6 | **Do not use `sysai/auto` as main** (`access.py:84,102` -> `"main": "mid"`; existing DB profiles need the UI) until Jev works and routing is sticky. | avoids full-price re-reads on model switches | none |
| 7 | **Auto-approve toggle** in the composer (owner point 5): fewer human pauses => fewer cache expiries (+39% for five in the model) and less wall time. | up to -28% (removes the +39%) | low |
| 8 | **Attachments**: in `app.js send()` do not send text files > ~30 KB as `resource_link` (Hermes inlines up to 512 KB); send the path + size and let the agent `read_file` ranges. | avoids 30-130k+ tokens per big attachment (if used) | low |
| 9 | **Attribution**: parse `project` from the first user message in the gateway (`trace.anatomy`, section 7) so Projects/Skills columns and the $30 project budget work; or inject `X-SYSAI-Project` if Hermes config can carry headers (not verified). | no direct saving; makes the bill visible and enforceable | low |
| 10 | Pin `tools.tool_search.enabled: "on"` (add `tools` to `OWNED_PARTS`) so older Hermes behaves like >= 0.22; run `hermes update` on the PCs. | up to -9k tokens/request (~4%) in the eager case | low-medium (+1-2 discovery turns) |
| 11 | Workers: `delegation.max_iterations` 250 -> 60, `max_summary_chars` 24,000 -> 8,000 (`access.py:544-547`). | small ($0.14 total today), quality | low |
| 12 | Trim big tool descriptions (`cmtp_edit` 1.5k, `canvas_edit` 0.8k), SOUL, skills. | <= 1% | none; not worth the time |

## 10. Cross-area findings (brief)
- `ebpro-cmtp`/`ebpro-gui` are not in the code-default engineer `mcp` rules (`access.py:43,81`) while the five `ebpro-*` skills are delivered "always": check the production profile (EasyBuilder agents).
- Hermes `tool_search` BM25 ignores Cyrillic; tool descriptions are Russian (first 80 chars go into the catalog listing) - names carry the discovery. Fine, but keep names descriptive.
- Hermes background review (forks the agent with the conversation snapshot on the main route, same prefix cache; `agent/background_review.py:1-8`; cadence presumably `memory.nudge_interval: 10`, my reading, not verified): a small extra, counted as "главная".
- ACP sessions get `cwd = home` (`main.js:419`), so Hermes' runtime block shows the home folder, not the project folder.
- Jev: statuses outside `FALL_BACK_ON` leave the request failed with model `typesafe/jev-router` (`gateway.py:45,355-364`).

## 11. NOT verified, and the cheapest ways to verify (no code changes)
- V1 (5 min): SYSAI -> agent page -> Трасса -> "Режим отладки" (24 h) -> one agent task -> open "текст" of the first model step: `tools` list. `tool_search, tool_describe, tool_call` = MCP deferred;
  `mcp__plc_kit__plc_info ...` = eager. Also `messages` and `system_chars`.
- V2 (PC): `hermes --version`; `hermes -p <agent> prompt-size --json` (exists since 0.16; offline) = exact bytes of system prompt, skills index, memory, tool schemas, per-toolset and per-skill table.
- V3 (PC): `...\hermes\profiles\<agent>\logs\agent.log`: grep "Could not determine context length" (256k fallback) and "context compression attempt telemetry" / "compress" (did it ever run, did it fail, 401?).
  `hermes -p <agent> insights --days 7` = Hermes' own token/cost analytics.
- V4 (SYSAI): Расходы -> 30 days -> "Кто спросил": is there a "сжатие" row? Download `/agents/spend.csv?days=30`: gpt-6.1-terra rows give input/cached/completion tokens => exact dollar split
  (resolves the output-vs-cache question) and, per day, requests-per-day (session length).
- V5 (SYSAI): Хаб: how many items are "по запросу"; Доступы of the agent: `mcp` list (are ebpro servers there?), `main`/`workers`/tiers, budgets; Настройки -> real models per tier (`agents_models`).
- Not verified at all: the PC's Hermes version and its behaviour on the points above (I read upstream main + tags v2026.6.5/v2026.8.31/v0.22.0); the production DB (hub delivery flags, profiles, budgets);
  tokenizer of gpt-6.1-terra; how OpenRouter bills cache writes for it; real typical sizes of `plc_variables/hmi_tags/cmtp_scene` answers on the real ЛПЗС projects (I only have the 2-POU fixture
  and structural row sizes); whether engineers attach large text files; the exact number of requests per session.

## 12. Sources and files
- Repo files cited inline. Scripts and data: `/tmp/research/tok/` (`dump_tools.py`, `dump_sysai_mcp.py`, `dump_window_tools.js`, `measure.py` -> `measure_out.json`, `listing.py`, `rows.py`, `sim.py`, `hermes_schemas.py`, `whoami_size.py`);
  scratch copies `/tmp/research/seedcopy`, `/tmp/research/deskcopy`; fixture project copy `/tmp/research/fix`.
- Hermes (read only, untrusted, never executed; `/tmp/research/hermes-src/`): raw.githubusercontent.com/NousResearch/hermes-agent/main/{agent/system_prompt.py, agent/prompt_builder.py, agent/skill_utils.py,
  agent/model_metadata.py, agent/context_compressor.py, agent/auxiliary_client.py, agent/background_review.py, tools/tool_search.py, tools/tool_search_catalog.py, tools/budget_config.py, tools/mcp_tool_schema.py,
  tools/mcp_tool_content.py, tools/vision_tools_history_budget.py, tools/code_execution_tool.py, acp_adapter/content.py, acp_adapter/session.py, toolsets.py, hermes_cli/config_defaults.py,
  hermes_cli/prompt_size.py, tests/fixtures/prompt_surface/surfaces.json, evals/core_tool_deferral/results/SUMMARY.md, website/docs/developer-guide/context-compression-and-caching.md};
  tags v2026.6.5 / v2026.8.3 / v2026.8.31 / v0.22.0 for `tools/tool_search.py`; releases via `api.github.com/repos/NousResearch/hermes-agent/releases`.
- Prices/windows: `https://openrouter.ai/api/v1/models` fetched 2026-11-12 (`/tmp/research/or/models.json`): openai/gpt-6.1-terra prompt 1e-6, completion 5e-6, input_cache_read 5e-8, input_cache_write 1.25e-6,
  override from 272,000 prompt tokens: 2e-6 / 7.5e-6 / 1e-7 / 2.5e-6, context 1,050,000; deepseek/deepseek-v4.1-flash-1017 1.5e-7 / 6e-7 / cache 3e-9 (time-of-day overrides); typesafe/jev-router priced -1.
