# F — Web research: MCP / agent token efficiency (verified facts with URLs)

Date of research: 2026-11-12. Language: English. Scope: WEB research only; nothing inside `/workspace/SYSAI` was modified
(one read-only measurement of the repo's MCP tool definitions was run from `/tmp`, see §7.2).

How to read this file
- Every claim carries its source URL and the source's date. "docs 2026-11-12" = the page has no publication date, I fetched it on 2026-11-12.
- Tags: **[FACT]** = seen on the cited page/in the cited file; **[INFERENCE]** = my own reasoning/arithmetic from facts (assumptions stated);
  **[NOT VERIFIED]** = could not be confirmed. Nothing here is invented; where a number comes from a vendor's own marketing it is labelled so.
- Numbers were taken from raw page text (curl + html-to-text, saved under `/tmp/research/raw/<name>/page.txt`), not from model summaries,
  except where stated. Helper scripts: `/tmp/research/scripts/` (`h2t.py`, `csearch.py`, `readme_scan.py`, `measure_tools.py`, `listing_estimate.py`, `calc.py`).
- Token counts of my own measurements: tiktoken `o200k_base`.

---------------------------------------------------------------------------------------------------------------------------

## 0. Executive summary (what the evidence says, and what it does NOT say)

1. **"99.3%" is traceable, but only to one small project's own benchmark.** It is the 5-tool row (1,519 -> 11 tokens, "compact_save": 99.3) of
   `activeing123/mcptoon`'s self-measured benchmark; that project's headline is 99.2% (255 synthetic tools, 71,929 -> 581 tokens, name-only index
   vs full JSON schemas). "mcpton" is most probably **mcptoon** (MCP + TOON). It is not from Cloudflare (99.9%) or Anthropic (98.7%). §3.
2. **Vendor numbers measure different things.** Cloudflare 99.9% = 1.17M tokens -> ~1,000 for a 2,500-endpoint API (server-side Code Mode);
   Anthropic 98.7% = an illustrative 150,000 -> 2,000 example (not a benchmark); Anthropic's *internal-test* numbers are tool search "85% reduction"
   (~77K -> ~8.7K tokens before any work, 50+ tools) and programmatic tool calling -37% on research tasks (43,588 -> 27,297). §1, §2.
3. **SYSAI's own hub tool definitions are small.** I measured the 6 hub MCP servers: **60 tools = 15,183 tokens** (avg 253, median 208 per tool; OpenAI
   function form). The brief's average request is ~273k tokens, so hub definitions are ~5.6% of an average request and, if the prefix stays cached,
   ~2% of the terra bill (§7.3). **[INFERENCE]** "MCP definitions eat the context" is not supported by these numbers for the hub tools;
   Hermes' own built-in tool schemas and the `sysai` HTTP MCP server were NOT measured.
4. **Hermes already ships a built-in "Tool Search"** (BM25, 3 bridge tools, default `tools.tool_search.enabled: auto` = on) that replaces MCP tool schemas by
   a ≤4,000-token name listing. On a current Hermes (v0.23.x) MCP schemas are therefore probably already deferred. Whether the engineers' PCs run such a
   version, and whether it is active in ACP sessions, is **NOT VERIFIED** (the repo pins no Hermes version). §4.
5. **Hermes explicitly does not do "code mode"**; its `execute_code` tool cannot call MCP tools; `delegate_task` has no per-call toolsets argument. §4.4.
6. **The levers that Hermes' own docs name for exactly this symptom are not set by SYSAI**: compaction threshold (default 50% of the window; "rarely fires on
   large-window models"), `proactive_prune_tokens` (default off), background-review forks that replay the whole conversation on the main model,
   `reasoning_effort` (default medium). SYSAI's `/v1/models` does not report `context_length`, so Hermes may not know the real window (§4.3, §8).
7. **A price cliff sits right at the owner's average request size**: OpenRouter's catalogue prices `openai/gpt-6.1-terra` at 2x input / 1.5x output for prompts
   >= 272,000 tokens; the brief's average is 273,169 tokens per request. §6.6.
8. **Caching facts (late 2026)**: OpenAI GPT-5.6+ (incl. gpt-6.1-terra) now charges cache *writes* at 1.25x and reads at 0.05x (terra); Anthropic 5m/1h TTL at
   1.25x/2x, reads 0.1x (0.05x on Sonnet/Opus 5.5); DeepSeek automatic (hit = 0.02x on V4.1 Flash); Gemini implicit; Kimi automatic (docs contradict NET.md:131). §5.
9. **MCP spec 2026-07-28 is the latest**; it asks servers to return `tools/list` in a deterministic order "to improve LLM prompt cache hit rates", adds
   `ttlMs/cacheScope` on list results, is stateless, moves tasks to an extension; Server Cards and Skills-over-MCP are Final extensions. §6.1-6.2.
10. **Browser**: Hermes has built-in browser tools (local Chromium by default) and web search/extract backends with keyless tiers; Microsoft's own Playwright MCP README
    recommends CLI+SKILLS over MCP for token reasons. A separate browser MCP is probably unnecessary. §4.6.

---------------------------------------------------------------------------------------------------------------------------

## 1. Cloudflare "Code Mode"

Sources:
- CF1 = https://blog.cloudflare.com/code-mode/ — "Code Mode: the better way to use MCP", Kenton Varda & Sunil Pai, **2025-09-26** (raw text read).
- CF2 = https://blog.cloudflare.com/code-mode-mcp/ — "Code Mode: give agents an entire API in 1,000 tokens", Matt Carey, **2026-02-20**.
- CF3 = https://developers.cloudflare.com/dynamic-workers/ — Dynamic Workers docs, "Last updated Oct 31, 2026".
- CF4 = https://github.com/cloudflare/agents/tree/main/packages/codemode (README, fetched 2026-11-12).
- HN1 = https://news.ycombinator.com/item?id=45399204 and item?id=45386248 (2025-09-26/27), HN comments on CF1.

**What it is [FACT, CF1]:** "Convert the MCP tools into a TypeScript API, and then ask an LLM to write code that calls that API." The agent gets ONE tool that
executes TypeScript; the code runs in a sandbox "totally isolated from the Internet"; its only access to the outside world is through TypeScript APIs for the
connected MCP servers, backed by RPC to the agent loop; results come back via `console.log()`.

**Original claim (CF1, 2025-09):** LLMs handle many/complex tools better as a TS API because of training data; chained calls skip copying intermediate results
through the model. **CF1 gives NO token numbers and NO benchmark** ("The results are striking" is not quantified). **Important detail [FACT, CF1]:** in that
first version "Currently, the entire API is loaded" into the agent's context ("future improvements could allow an agent to search and browse the API more
dynamically"). I.e. the first Code Mode saves intermediate-result tokens and round trips, NOT definition tokens.

**Sandbox/bindings [FACT, CF1]:** V8 isolates ("start in a handful of milliseconds using only a few megabytes of memory", no containers, new isolate per snippet, no
pooling); Worker Loader API (`env.LOADER.get(...)`), sandbox `env` holds *bindings* = live RPC objects to the MCP servers, `globalOutbound: null` blocks internet,
API keys stay in the supervisor ("the AI cannot possibly write code that leaks any keys"). Status in 2025-09: "The Dynamic Worker Loader API is in closed beta";
locally available with Wrangler/workerd.

**Server-side Code Mode for the whole Cloudflare API [FACT, CF2, 2026-02-20]:**
- Two tools, `search()` and `execute()`, each taking one `code` string (JS async arrow function). "While consuming only around 1,000 tokens. The footprint stays fixed".
- "For a large API like the Cloudflare API, Code Mode reduces the number of input tokens used by 99.9%. An equivalent MCP server without Code Mode would consume
  **1.17 million tokens**." The chart caption says "measured with tiktoken". The spec has "over 2,500 endpoints" and never enters the model context.
- Sandbox: "Dynamic Worker isolate — a lightweight V8 sandbox with no file system, no environment variables ... and external fetches disabled by default."
- Worked example: protect an origin from DDoS = "four tool calls".
- CF2 compares approaches itself: client-side Code Mode (needs a sandbox in the agent; "implemented in Goose and Anthropics Claude SDK as Programmatic Tool Calling"),
  CLIs via MCPorter ("OpenClaw and Moltworker convert MCP servers into CLIs using MCPorter"; limitation: needs a shell, broader attack surface), dynamic tool search
  (needs a maintained search function; "each matched tool still uses tokens"). Planned: "first-class Code Mode integration for all your MCP servers" behind MCP Server Portals.
- CF3 (docs, 2026-10-31) markets Code Mode as "Save up to 80% in inference tokens and cost by allowing the agent to programmatically process data instead of sending it all
  through the LLM" — a different, softer number; its methodology is **[NOT VERIFIED]**.
- Hermes' docs mention the same server with the opt-out flag `https://mcp.cloudflare.com/mcp?codemode=false` (~3,300 tools if code mode is off) (docs 2026-11-12, see §4).

**Agents SDK package [FACT, CF4]:** `@cloudflare/codemode` — "Experimental — may have breaking changes"; `createCodeTool({tools, executor})` + `DynamicWorkerExecutor`;
needs the Workers runtime (or local workerd). Not a drop-in for a Python/Windows desktop agent.

**Limitations / caveats**
- [FACT] Both Cloudflare and Anthropic say code execution needs "a secure execution environment with appropriate sandboxing, resource limits, and monitoring" (§2).
- [FACT, HN1] Independent commenters note the idea predates MCP (Apple's CodeAct, ICML 2024: "up to 20% higher success rate" across 17 LLMs, https://machinelearning.apple.com/research/codeact;
  Google CaMeL; Hugging Face smolagents) and that the initial release was a Cloudflare-hosted service. Elicitation/approval flow through code was an open question there.
- [INFERENCE] The 99.9% figure is for a 2,500-endpoint API; it scales with API size. For ~50-60 tools (our case) the starting point is ~15k tokens, not 1.17M, so the
  absolute saving is bounded by that (§7).
- [FACT, Atlassian blog, 2026-03-29, https://www.atlassian.com/blog/developer/mcp-compression-preventing-tool-bloat-in-ai-agents] code mode "introduces a new dependency on
  successful code generation and execution, which can become its own failure mode".

---------------------------------------------------------------------------------------------------------------------------

## 2. Anthropic engineering posts (numbers, patterns)

Sources (all raw text read; the engineering URLs now redirect to `anthropic.com/research/...`):
- A1 = https://www.anthropic.com/engineering/code-execution-with-mcp — "Code execution with MCP", **2025-11-04**, Adam Jones & Conor Kelly.
- A2 = https://www.anthropic.com/engineering/advanced-tool-use — "Introducing advanced tool use", **2025-11-24**, Bin Wu et al.
- A3 = https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents — **2025-09-29**.
- A4 = https://www.anthropic.com/news/context-management (now https://claude.com/resources/articles/context-management) — context editing + memory tool, **2025-09-29**.
- A5 = https://www.anthropic.com/engineering/equipping-agents-for-the-real-world-with-agent-skills — Agent Skills, **2025-10-16**, update **2025-12-18** ("published Agent Skills as an open standard").
- A6 = https://www.anthropic.com/engineering/writing-tools-for-agents — **2025-09-11**.
- A7 = https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-search-tool (docs 2026-11-12).

**A1 Code execution with MCP [FACT]**
- Two problems: tool definitions overload context; intermediate results pass through the model ("a 2-hour sales meeting ... an additional 50,000 tokens").
- Pattern: generate a file tree of tools (`./servers/google-drive/getDocument.ts` ...); the agent lists the directory and reads only the needed files:
  "This reduces the token usage from 150,000 tokens to 2,000 tokens—a time and cost saving of 98.7%." **This is an illustrative scenario in the text, not a benchmark.**
- Progressive disclosure: filesystem navigation or a `search_tools` tool "with a detail level parameter (name only, name and description, or full definition with schemas)".
- Data stays in the sandbox: "The agent sees five rows instead of 10,000" (filter in code, log only 5).
- Privacy: the MCP client tokenizes PII (`[EMAIL_1]`...) and untokenizes in later tool calls, "real ... never through the model".
- State/skills: write intermediate files; save reusable code in `./skills/` with a `SKILL.md`.
- Caveat: "Running agent-generated code requires a secure execution environment with appropriate sandboxing, resource limits, and monitoring ... operational overhead and security considerations that direct tool calls avoid."
- Names Cloudflare's "Code Mode" as "similar findings".

**A2 Advanced tool use [FACT]** (beta header `advanced-tool-use-2025-11-20`)
- Definition cost data point: GitHub 35 tools ~26K, Slack 11 tools ~21K, Sentry 5 ~3K, Grafana 5 ~3K, Splunk 2 ~2K = "58 tools ... approximately 55K tokens"; Jira alone ~17K; "At Anthropic, we've seen tool definitions consume 134K tokens before optimization."
- Tool Search Tool: traditional "~72K tokens for 50+ MCP tools" (~77K total before work) vs search tool ~500 tokens + 3-5 discovered tools ~3K = ~8.7K: "an **85% reduction** in token usage". Accuracy: "Opus 4 improved from 49% to 74%, and Opus 4.5 from 79.5% to 88.1%". Deferred tools via `defer_loading: true`; "doesn't break prompt caching because deferred tools are excluded from the initial prompt". Use when >10K tokens of definitions, 10+ tools; less beneficial for <10 tools.
- Programmatic Tool Calling: Python orchestration in a code-execution sandbox; "Average usage dropped from 43,588 to 27,297 tokens, a **37% reduction** on complex research tasks"; example 200KB raw -> 1KB result; accuracy 25.6%->28.5% (knowledge retrieval), 46.5%->51.2% (GIA). Best for 3+ dependent calls, large datasets, parallel fan-out; not for single quick calls.
- Tool Use Examples: "improved accuracy from 72% to 90% on complex parameter handling"; 1-5 examples per tool; they ADD definition tokens.
- A7 (docs): "A typical multiserver setup ... ~55k tokens ... Tool search typically reduces this by over 85 percent, loading only the 3–5 tools"; "Claude’s ability to pick the right tool degrades once you exceed **30–50 available tools**".

**A3/A4 Context engineering [FACT]**
- "Context rot"; "smallest possible set of high-signal tokens"; "bloated tool sets ... one of the most common failure modes"; just-in-time retrieval; compaction; structured note-taking; sub-agents return "condensed ... (often 1,000-2,000 tokens)"; "tool result clearing" = "one of the safest lightest touch forms of compaction".
- A4 numbers: memory tool + context editing = "improved performance by 39% over baseline", context editing alone 29%; "In a 100-turn web search evaluation, context editing ... reducing token consumption by 84%".

**A5 Agent Skills [FACT]** — metadata (name+description) preloaded in the system prompt = level 1; SKILL.md body = level 2 (loaded when relevant); linked files = level 3. The open spec (https://agentskills.io/specification, docs 2026-11-12) states ~100 tokens/skill metadata, <5,000 tokens recommended for instructions, SKILL.md under 500 lines, name max 64 chars, description max 1,024 chars.

**A6 Writing tools [FACT]** — `response_format` enum: concise 72 tokens vs detailed 206 ("~1/3 of the tokens"); Claude Code caps tool responses at 25,000 tokens by default; namespacing tools; pagination/filtering/truncation with steering messages.

---------------------------------------------------------------------------------------------------------------------------

## 3. "mcpton" and the "99.3%" figure

Sources: GitHub API/READMEs fetched 2026-11-12 (`gh api`), HN Algolia API, PyPI JSON. Raw copies: `/tmp/research/raw/readmes/*`, `/tmp/research/raw/mcptoon-files/*`.

### 3.1 Most likely meaning
- GitHub has **no repo named "mcpton"** (`gh search repos mcpton` = empty). Closest names: **mcptoon** (MCP + TOON, a compact JSON notation) and **MCPorter** (`mcporter`).
- **mcptoon** — "Show HN: Mcptoon – Token-efficient MCP CLI client", https://news.ycombinator.com/item?id=49253721 (**2026-08-11**, 73 points, poster `mcptokensaver`, repo https://github.com/activeing123/mcptoon).
- **99.3% trace [FACT]**: in that repo `assets/benchmark_tiktoken.json` has `{"tools": 5, "json": 1519, "compact": 11, "compact_save": 99.3}`; `docs/comparison.md` line 31 and `docs/README-details.md` line 28 print
  `| 5 | 1,519 | ... | 11 | −99.3% |`. The same files give 50 tools: 14,113 -> 114 (−99.2%); 255 tools: 71,929 -> 581 (−99.2%). The README/homepage headline (2026-11-12) is **99.2%**; at the last commit of the Show HN day
  (2026-08-11, commit 09fc937) the README said **97%** (and "98%" for discovery, "93%" for `--slim`); `assets/benchmark_data.json` marks the 5-tool row "Superseded legacy snapshot replaced 2026-09-06".
- So: **99.3% = mcptoon's best-case row** (5 tools; name-only manifest vs full JSON schemas, tiktoken cl100k_base, measured by the project itself). I cannot prove this is the page the owner read — **[NOT VERIFIED]**.
  No other repo/page I scanned (mcporter, mcp2cli, mcptools, mcp-compressor, mcp-compress-router, cmcp, codemode-mcp variants, UTCP code-mode, squeez, entroly, Cloudflare, Anthropic, HN, GitHub issues) contains 99.3.
- Other "~99%" figures in circulation: Cloudflare 99.9% (CF2), Anthropic 98.7% (A1), mcp2cli "Save 96–99%", mcp-compress-router "up to 99%", cmcp "99% fewer tool definitions (2 vs hundreds)" (a count of tools, not tokens).

### 3.2 Critical reading of the mcptoon claim
- [FACT, README docs 2026-11-12] The percentage is **definition-side only**: "The "99%" in this README is the definition-side saving — from not loading every schema ... Result compression is a separate, much smaller bill" (`--toon` saves ~6% on a mixed result sample, 1.8–48.6% by shape). Their real-machine table: 7 public servers, 209 tools, 640,606 -> 905 tokens (−99.86%) — dominated by one 170-tool server.
- [FACT] The agent must then call `mcptoon inspect <server> <tool>` to get a schema and `mcptoon call ...`; those extra round trips and schema fetches are not in the 99.2%.
- [FACT, HN thread 2026-08-11] Independent criticism: "TOON variant clearly doesn't have the same information", `null`/`\n` replaced by non-ASCII symbols may *increase* tokens (debazel), "isn't this just compression? Lookups burn tokens just on the other end?" (bythreads), a commenter who built a similar proxy (`ameshkov/mcp-compress-router`) found "it's often better to return tool names with argument names ... Otherwise models often tend to hallucinate argument names and an extra turn is required".
- [FACT] Project is young: created 2026-07-27, single maintainer (534 of 543 commits), 74 GitHub releases in 3.5 months (latest v0.8.39 on 2026-11-12), 429 stars, owner account created 2026-07-16.

### 3.3 Candidate table (all numbers as of 2026-11-12)

| Project | What it does | Lang / licence | Stars; created; last commit | How it wraps OUR Python stdio servers (not tested by me) | Claimed saving and where it comes from |
|---|---|---|---|---|---|
| **activeing123/mcptoon** (PyPI `mcptoon` 0.8.39, py>=3.10, pure wheel) | CLI/gateway: name-only tool index, on-demand schema, optional TOON result encoding, skills pointer | Python stdlib only, Apache-2.0 | 429★; 2026-07-27; 2026-11-12 | `mcptoon add plc-kit --stdio <cmd...>` (could be the existing `sysai-agent mcp plc-kit` wrapper so signed access rules stay in the chain); agent uses `mcptoon manifest/inspect/call` via a shell tool, or `mcptoon serve` as ONE MCP server | 99.2% (255 synthetic tools), 99.86% (209 real tools), 99.3% (5-tool row); self-measured with tiktoken cl100k_base |
| **openclaw/mcporter** (ex steipete/mcporter) | TypeScript runtime/CLI: `list` (TS-style signatures), `call`, `generate-cli`, `emit-ts`, record/replay; imports configs from Cursor/Claude/Codex; tool filters in config | TypeScript, MIT | 5,112★; 2025-11-05; 2026-11-05 (v0.17.0) | `mcporter call --stdio "<cmd>"` or `config/mcporter.json` `mcpServers.<n>.command/args`; **needs Node >= 24** for npm install | No % claim in README. Cloudflare CF2 names it as the CLI route to progressive disclosure |
| **knowsuchagency/mcp2cli** (PyPI 3.9.0, py>=3.10) | Any MCP/OpenAPI/GraphQL -> CLI at runtime; `--list`, `--search`, session daemon, "bake" configs | Python, MIT | 2,440★; 2026-03-09; 2026-11-09 | `mcp2cli --mcp-stdio "python server.py" --list` / `<tool> --arg v` | "Save 96–99% of the tokens wasted on tool schemas" (README). Write-up page is JS-only: **[NOT VERIFIED]** |
| **atlassian-labs/mcp-compressor** (PyPI 0.40.0, py>=3.11, Windows wheel) | MCP proxy: replaces N tools by `get_tool_schema` + `invoke_tool` (+ `list_tools` at max); levels low/medium/high/max; tool filters; TOON; Python/TS/Rust SDK; CLI-mode/Code-mode generated clients | Rust core + Python/TS bindings, Apache-2.0 | 142★; 2026-03-04; 2026-10-31 | `mcp-compressor -c medium -- python server.py` (documented for stdio); in Hermes the entry would be `command: mcp-compressor`, `args: [-c, medium, --, sysai-agent, mcp, plc-kit]` | Atlassian blog **2026-03-29**: "70–97%"; 94-tool GitHub scenario 17,600 tokens -> 3,900 / 3,300 / 2,200 / 500 by level; "tool compression had almost no impact on end-to-end quality in the evaluation setup we used" (vendor test) |
| **ameshkov/mcp-compress-router** | Router MCP with `get_tool_schema`/`invoke_tool` | TypeScript (Node >= 24), MIT | 21★; 2026-06-04; 2026-11-10 | stdio downstream servers via `add <name> -- <cmd>` | "up to 99%" / 97.7% for 3 servers (26K -> ~600). Own table (Sept 2026): savings depend on the host: Claude Code with Tool Search on 1.1%, Codex CLI 1.9%, OpenCode V2 −8.6%; Tool Search off 31.7%; Copilot CLI 49.7% |
| **assimelha/cmcp** | Code-Mode gateway: `search()` + `execute()` (TypeScript in QuickJS, 64 MB limit) over many MCP servers | Rust, MIT | 32★; 2026-02-21; **2026-02-26 (stale)** | `cmcp add --transport stdio <name> -- <cmd>` | "99% fewer tool definitions (2 vs hundreds)" = tool count, no token measurement |
| **avencera/codemode-mcp** | Gateway: `search_tools`, `describe_tools` (typed Python stubs), `execute` (Python in pydantic **Monty** sandbox after `ty` type check), `wait`; per-tool allow/ask/deny | Rust, Apache-2.0 | 1★; 2026-10-12; 2026-11-12 | stdio upstream servers supported; approvals use Mac Touch ID/passkeys (macOS-centric) | none quantified |
| **jx-codes/codemode-mcp** | Early local Code-Mode for MCP (Deno sandbox, one `execute_code` tool) | JS/Bun, **no licence** | 131★; 2025-09-28; 2025-10-09 | author says he moved on to `jx-codes/lootbox` | none |
| **universal-tool-calling-protocol/code-mode** | UTCP code-mode library | TypeScript, MPL-2.0 | 1,666★; 2025-11-11; 2026-08-25 | not read in depth | not read |
| **claudioemmanuel/squeez** | Hook-based *output* compressor for 7 CLI hosts incl. **Hermes** (bash output "up to 95%") | Rust, Apache-2.0 | 231★; 2026-03-23; 2026-10-30 | hooks, not an MCP wrapper | tool-result side, not schemas |

[INFERENCE] If Hermes' built-in Tool Search is active (§4.2), a schema wrapper adds little (compare ameshkov's measured "Tool Search on: 1.1%") and moves the per-tool access check one layer
inside the wrapper (the SYSAI hook would see the generic `invoke_tool`/`tool_call` name instead of the real tool; with Hermes' native Tool Search the hook still sees real tool names, per
https://hermes-agent.nousresearch.com/docs/user-guide/features/tool-search).

---------------------------------------------------------------------------------------------------------------------------

## 4. Hermes Agent (Nous Research) — what we can set

Sources (docs fetched 2026-11-12; raw text in `/tmp/research/raw/hermes-*/page.txt`):
- H0 = https://github.com/NousResearch/hermes-agent (MIT, Python, 262,873★, created 2025-07-22; **latest release v0.23.1, 2026-11-10**; v0.23.0 2026-11-05; v0.22.0 2026-10-14).
- HM = https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp ; HC = .../user-guide/configuration ; HS = .../user-guide/security ; HK = .../user-guide/features/skills ;
  HSE = .../user-guide/sessions ; HB = .../user-guide/features/browser ; HX = .../user-guide/features/code-execution ; HT = .../user-guide/features/tool-search ;
  HA = .../user-guide/features/acp ; HD = .../user-guide/features/delegation ; HMem = .../user-guide/features/memory ; HP = .../integrations/providers.
- HSRC = repo files at https://github.com/NousResearch/hermes-agent (main, fetched 2026-11-12): `website/docs/developer-guide/context-compression-and-caching.md`,
  `website/docs/user-guide/configuring-models.md`, `hermes_cli/config_defaults.py`, `cli-config.yaml.example`; release notes https://github.com/NousResearch/hermes-agent/releases.

### 4.1 What SYSAI assumes and writes today
- [FACT] Installer takes the latest Hermes from `https://hermes-agent.nousresearch.com/install.ps1` (`agents/installer/install.ps1:78`). **No Hermes version pin exists in the repo** (grep: only fake versions "0.9.1"/"0.12.0" in tests);
  `docs/opo/TASKS.md:357-359` says "проверенная версия Hermes в настройках" is still to be done. So the PCs' Hermes version is **[NOT VERIFIED]**.
- [FACT] Keys SYSAI owns/writes (`agents/sync/sysai_agent/hermes.py:17-19`, `app/net/access.py:539-572`): `model` (custom provider -> SYSAI gateway `/v1`, `default: sysai/<tier>`), `fallback_providers: []`, `delegation`
  (model, base_url, `max_concurrent_children: 3`, `max_spawn_depth: 1`, `request_overrides.extra_body.sysai.role`), `auxiliary.vision`, `auxiliary.compression` (model only), `approvals: {mode: off, deny}`,
  `hooks.pre_tool_call`, `memory` (enabled), `security.redact_secrets`, `mcp_servers` (`sysai` HTTP + hub tools via `sysai-agent mcp <name>`), `skills.external_dirs/create_dir`, `providers.sysai.models[...].prompt_caching` (Claude/Kimi tiers only).
- [FACT] **Not set by SYSAI (Hermes defaults apply)**: `compression.*`, `model.context_length`, `tools.tool_search`, `tool_budget.*`, `auxiliary.background_review.*`, `memory.nudge_interval`, `skills.creation_nudge_interval`, `agent.reasoning_effort`, `sessions.*`.
  The sync agent only rewrites the keys in `OWNED`/`OWNED_PARTS` (`hermes.py:17-19`), everything else "is the person's" — so new keys need a small change in that list plus the server document (the delivery path is code even though the *settings* are not).
- [FACT, `app/net/gateway.py:515-518`] SYSAI's `/v1/models` returns only `{id, object, created: 0, owned_by, name}` — **no `context_length`**.

### 4.2 MCP exposure and Tool Search (the most relevant finding)
- [FACT, HM] Per server: `enabled: false` (skip entirely), `tools.include` / `tools.exclude` (exact names or fnmatch globs; "include wins"), `tools.resources: false`, `tools.prompts: false`, `lazy: true` (register tools from schema cache,
  start the process on first call), `idle_timeout_seconds`, `max_lifetime_seconds`, `timeout`, `connect_timeout`, `supports_parallel_tool_calls`. `/reload-mcp` reloads; "a session's tool set is otherwise frozen". Tool names are `mcp_<server>_<tool>`; toolset `mcp-<server>`.
- [FACT, HM+HC] MCP results over 50,000 chars are spilled to disk (`tool_budget.mcp_result_size_chars: 50000`), generic spillover 100,000 chars, `tool_output.max_bytes: 50000` (terminal), `file_read_max_chars: 100000` (~25-35K tokens).
- [FACT, HT + `hermes_cli/config_defaults.py`] **Tool Search is built in**: MCP and plugin tools are replaced by three bridge tools `tool_search(queries)`, `tool_describe(names)`, `tool_call(calls)`; BM25 + stemming; hooks/guardrails/approvals "run against the real tool name — not against tool_call".
  Default `tools.tool_search.enabled: "auto"` ("an alias of on today"); `threshold_pct: 5`; `listing: auto`; `listing_max_tokens: 4000` (name + first sentence <=60 chars, grouped by server); tier 2 for huge catalogs (Cloudflare ~3,300 tools ≈ 32K tokens of names alone).
  Caveats stated by Hermes itself: one or two extra model calls on cold tools; deferred schemas get no system-prompt-cache benefit; "Toolset edits invalidate cache"; smaller models search worse; "No JS sandbox ... the JS-sandbox 'code mode' some other implementations offer is a large surface area; we skip it."
  Release notes: `tool_search` already referenced in v2026.8.3 (2026-08-03), multi-query/stemming v2026.8.27, rarely-used built-ins deferred by default in v0.22.0 (2026-10-14: "desktop schemas 13,367→6,883 tokens"). The release that made it default for MCP is **[NOT VERIFIED]**.
- [INFERENCE] With Tool Search active, the 60 hub tools (15,183 tokens eager) would cost roughly a 1.7k-token listing (my estimate, §7.2) plus the three bridge schemas (not measured; assumed 0.5-1k) ≈ 2.2-2.7k tokens.
- [FACT, HA] ACP sessions: curated `hermes-acp` toolset (file, terminal/process, web/browser, memory, todo, session_search, skills, execute_code, delegate_task, vision). `platform_toolsets.acp: [file, web, skills, <mcp server names>]` limits which MCP servers ACP sessions get; `agent.disabled_toolsets` removes toolsets everywhere; `no_mcp` drops all.
  Whether Tool Search applies inside ACP sessions is **[NOT VERIFIED]** (the page does not say).

### 4.3 Context compression, caching, cost knobs (doc defaults)
| Key | Default | Effect (source) |
|---|---|---|
| `compression.enabled` / `threshold` | true / 0.50 | compress at 50% of context window; "floored at 0.75 below 512K windows" (HSRC dev guide) |
| `compression.threshold_tokens` | null | absolute cap, lower of ratio vs absolute; doc example: "`threshold_tokens: 256000` to compact a 1M-window model at 256K instead of 500K"; "a fixed cost ceiling per call" (HC) |
| `compression.proactive_prune_tokens` | 0 (off) | deterministic no-LLM prune/dedupe of old oversized tool results; "On large-window models the threshold compaction (≈50% of the window) rarely fires, so bulky tool outputs ... ride along in history and get re-sent on every subsequent turn ... try 48000" (HC) |
| `compression.tail_mode` / `protect_last_n` / `target_ratio` | lean / 20 / 0.20 | lean = 2.5% tail (10K-25K), one summary call; `in_place: true`; `idle_compact_after_seconds` (0=off); `prepare_ahead` (false); `model_thresholds` per model substring (HC, HSRC) |
| `auxiliary.compression.{provider,model,reasoning_effort}` | auto = main model | summary model must have a window >= the main model's (HC) — SYSAI already routes it to a cheap tier |
| `model.context_length` / `providers.<n>.models.<id>.context_length` | detection chain | order: config pin -> per-model -> persistent cache -> endpoint `/models` -> Anthropic/OpenRouter/Nous metadata -> models.dev -> **128K fallback** (HP "Context Length Detection"). SYSAI's endpoint reports none (§4.1) => **[INFERENCE]** Hermes may resolve `sysai/*` via the 128K fallback or a cached value; check `/usage` |
| `auxiliary.background_review.{enabled,max_input_tokens,provider,model}`; `memory.nudge_interval`(10); `skills.creation_nudge_interval`(15) | enabled; unset => 75% of window capped 600,000 | post-turn "self-improvement" forks "replay the conversation on every provider request"; "can burn a meaningful share of total tokens on busy hosts"; same-model fork inherits reasoning effort and keeps the cache prefix byte-identical (HMem; defaults from `cli-config.yaml.example`) |
| `agent.reasoning_effort` | medium (none/minimal/low/medium/high/xhigh/max/ultra) | per-task overrides under `auxiliary.<task>.reasoning_effort`; `none` is sent explicitly on GPT-5.x (HC) |
| `agent.max_turns` | unlimited | `loop_caps.max_subagents: 50`, `max_web_searches: 50` per turn (HC) |
| `prompt_caching.cache_ttl` | "5m" | Anthropic-style breakpoints only ("5m"/"1h"/"auto"); caching is "always-on" for Claude via Anthropic/OpenRouter/Nous; subagents clamped to 5m (HC) |
| `providers.<id>.models.<m>.prompt_caching: true/false` | detection | "For a gateway that resolves a bare model alias only after receiving the request, opt the alias into prompt-cache markers" — exactly SYSAI's case (configuring-models.md, HSRC) |
| `code_execution.{mode,timeout,max_tool_calls}` | project / 300 s / 50 | `execute_code` limits; stdout cap 50 KB (HX) |
| `delegation.{model,max_concurrent_children,max_spawn_depth,compression_threshold_tokens,backend}` | inherit / 3 / 1 / 0 / default | children "isolated context, inherited tool access"; `backend: profile_routes` (official `advanced-delegation` plugin) routes tasks to other Hermes *profiles*, each with its own MCP/toolsets/SOUL (HD) |
| `sessions.auto_prune` / `retention_days` | false / 90 | state.db (SQLite+FTS5) keeps every session (HSE) |

- [FACT, HSRC dev guide] Cache hygiene rules inside Hermes: "Avoid mutating [the system prompt] mid-conversation"; after compaction the cache is invalidated for the compressed region; **"Model identity is part of the cache key: ... Any mid-conversation model change — an explicit /model switch, primary-model fallback, or a credential-pool rotation ... means the next request gets zero cache hits ... Don't add features that silently swap the model or credentials mid-session."**
  I found no mention of Hermes sending `session_id`/`prompt_cache_key` to the provider in the configuration/providers docs I searched (**[NOT VERIFIED]** in source) — see OpenRouter sticky routing in §5.
- [FACT, HC "Skills"] Skill index lives in the system prompt "which stays fixed so the provider's prompt cache keeps working"; Level-0 `skills_list()` is "~3k tokens" for the bundled catalog; SYSAI creates the profile with `--no-skills`, so only hub skills count (the Agent Skills spec says ~100 tokens/skill).

### 4.4 What Hermes does NOT do (so a wrapper would have to)
- `execute_code` scripts can call only `web_search, web_extract, read_file, write_file, search_files, patch, terminal`; "tool whitelist (scripts cannot call execute_code recursively, delegate_task, or MCP tools)"; limits 300 s, 50 tool calls, 50 KB stdout (HX).
- `delegate_task` "does not accept a model-facing toolsets parameter. Each subagent inherits the parent's enabled toolsets" (HD) — per-stage tool sets need separate profiles (`profile_routes`) or separate sessions.
- No JS/Python "code mode" for MCP (HT).
- Per-session tool list is frozen; changing toolsets/model mid-session costs a cache miss (HM, HT, HSRC).

### 4.5 Approvals, sessions, skills (owner items 5, 6)
- [FACT, HS] `approvals.mode: smart|manual|off` (default smart); `off` = `--yolo`: all commands run, and write calls on `trust: untrusted` MCP servers skip the trust prompt ("An MCP server's declared spend limit still asks"). `approvals.deny` globs apply before yolo/off (SYSAI uses this). `command_allowlist` = permanent allow-list ("always"); `hermes approvals suggest` mines history into proposals; `/yolo` toggles per session and is saved with the session; `cron_mode`/`single_query_mode`/`unattended_mode: deny|approve`; file-write denylist is a hard block with no prompt (HERMES_WRITE_SAFE_ROOT optional). `memory.write_approval` and `skills.write_approval` (both default false = "write freely") can gate agent-created memories/skills (HMem, HK).
- [FACT, HA] ACP: "Dangerous terminal commands can be routed back to the editor as approval prompts"; options allow once / allow for session / allow always / deny; "Whether you actually see a prompt is up to the host"; `approvals.timeout` default 300 s. [INFERENCE] With `approvals.mode: off` (what SYSAI writes) Hermes itself should not ask for file creation; a "may I create this file?" question therefore comes from the model/SOUL text or from the desktop host's own ACP handling — the desktop agent area should check (not verified here).
- [FACT, HA+HSE] Sessions are persisted in `~/.hermes/state.db`; "Conversations are persisted to Hermes' session database and can be listed, loaded, resumed, or forked after the ACP server restarts"; a new session without a prompt stays in memory only; "the editor must supply the same mcpServers on load/resume". CLI: `hermes -c`, `hermes --resume <id|title|latest> [--in <dir>]`, titles auto-generated. So persistence exists upstream; the desktop must call list/load/resume instead of always `session/new` (not verified in our desktop code).

### 4.6 Browser and web (owner item 7)
- [FACT, HB] Built-in `browser` toolset: `browser_exec` drives Chromium over CDP (Python), `browser_cdp`, `browser_dialog`; default **local packaged Chromium, headless, one per session, no key**; `browser.cloud_provider: local | browserbase | browser-use | firecrawl | nous`; `browser.user_data_dir` keeps logins; `/browser connect` attaches to the user's Chrome/Edge. ACP's curated toolset includes "web/browser tools" (HA).
- [FACT, HC] `web.backend: firecrawl | searxng | parallel | tavily | perplexity | keenable | exa` (Parallel/Tavily/Exa "optional — keyless free tier"; SearXNG search-only).
- [FACT, https://github.com/microsoft/playwright-mcp README, fetched 2026-11-12] "coding agents increasingly favor CLI–based workflows exposed as SKILLs over MCP because CLI invocations are more token-efficient: they avoid loading large tool schemas and verbose accessibility trees". Related tools: ChromeDevTools/chrome-devtools-mcp 55,149★ (Apache-2.0), vercel-labs/agent-browser 50,179★ (Apache-2.0, Rust CLI), browser-use/browser-use 120,813★ (MIT).
- [INFERENCE] No browser MCP is needed if the `browser`/`web` toolsets stay enabled by SYSAI's profile (the hook already lists browser among checked tools, NET.md:35-36); what must be checked is that a web backend is configured.

### 4.7 Quick checks on a PC (settings/inspection only)
`hermes --version`; read `config.yaml` for `tools.tool_search`, `compression`, `model.context_length`; `/usage` and the status bar (`context_detail`, `cache_hit`); `~/.hermes/logs/agent.log` lines "Background review complete: ... calls= in= out=" and `session_model_usage` rows with `task='background_review'` (HMem); `hermes mcp test <name>`.

---------------------------------------------------------------------------------------------------------------------------

## 5. Prompt caching per provider (late 2026) and how OpenRouter passes it through

Sources: OAI = https://developers.openai.com/api/docs/guides/prompt-caching and https://developers.openai.com/api/docs/pricing (docs 2026-11-12);
ANT = https://platform.claude.com/docs/en/build-with-claude/prompt-caching (docs 2026-11-12); DS = https://api-docs.deepseek.com/guides/kv_cache and .../quick_start/pricing (© 2026);
GG = https://ai.google.dev/gemini-api/docs/caching (-> aistudio.google.com/docs/caching; read via Context7 mirror `/websites/ai_google_dev_gemini-api`, docs 2026-11-12);
KIMI = https://platform.kimi.ai/docs/guide/context-caching (docs 2026-11-12); OR = https://openrouter.ai/docs/guides/best-practices/prompt-caching (docs 2026-11-12); ORM = https://openrouter.ai/api/v1/models (fetched 2026-11-12).

| Provider | Activation | Read price | Write price | TTL / retention | Min prefix | Usage fields |
|---|---|---|---|---|---|---|
| **OpenAI** (GPT-5.6+, incl. gpt-6.1-terra, gpt-6-luna) | automatic (implicit) or explicit breakpoints (`prompt_cache_options.mode`, `prompt_cache_breakpoint`, <=4 writes/request) (OAI) | 0.05x on GPT-6.1 Sol/Terra, 0.1x on most others (OAI); terra $0.05 vs $1.00 | **1.25x** for 30 min default; **2.5x** for 12 h (Responses API) (OAI); "a prompt ... only sent once costs more with caching on than off" (OR) | min lifetime 30 min / 12 h (earlier models: `in_memory` ~5-10 min up to 1 h, `24h`) | 1,024 tokens (GPT-5.6+) | `cached_tokens`, `cache_write_tokens` (OR) |
| **Anthropic** (Claude 5.x) | explicit `cache_control` on blocks (max 4) or top-level automatic `cache_control` (not on Bedrock) (ANT) | 0.1x; **0.05x Opus 5.5 / Sonnet 5.5**; 0.025x Fable/Mythos (ANT) | 5 min TTL 1.25x; 1 h TTL 2x (ANT) | 5 min default (refreshed on hit), 1 h optional | 512 (Haiku 5.5, Sonnet 5.5, Opus 5/5.5), 1,024 (Sonnet 5, Opus 4.8), 2,048 (Opus 4.7), 4,096 (Opus 4.5/4.6, Haiku 4.5) (ANT) | `cache_creation_input_tokens`, `cache_read_input_tokens`, `input_tokens` (after last breakpoint) |
| **DeepSeek** (V4.1 Flash 1017) | automatic disk cache, "enabled by default"; best-effort (DS) | hit $0.003 vs miss $0.15 per M (0.02x), peak $0.006/$0.30 (DS pricing) | none (cache miss price = input) | "cleared ... usually within a few hours to a few days" | none stated | `prompt_cache_hit_tokens`, `prompt_cache_miss_tokens` |
| **Google Gemini** (2.5+) | implicit by default, no write/storage cost; explicit caching with TTL default 1 h + storage fee (not supported on Gemini 4 Argon+) (GG) | catalogue shows 0.1x for 2.5 Flash/Flash-Lite ($0.03 vs $0.30; $0.01 vs $0.10) (ORM) — OR's page still says 0.25x (stale/inconsistent) | none (implicit) | implicit "on average 3-5 minutes" (OR) | Google docs: 2,048 (2.5 Flash/Pro), 4,096 (3.x/4); OR page says 1,024 (Flash)/4,096 (Pro) — **conflict, [NOT VERIFIED]** | `usage_metadata` / `usage.total_cached_tokens` |
| **Moonshot Kimi** | automatic prefix cache; optional `prompt_cache_options {mode: implicit, ttl: 5m\|1h}` (KIMI) | 0.1x (kimi-k3 $0.30 vs $3.00) | billed separately: 5m = 1x input, 1h = 2x | 5 m / 1 h, locked at first write; org-isolated | n/a | via OpenAI-style usage |

- [FACT, KIMI] "Keep the order and exact text of fixed content unchanged within the same session. Do not put timestamps, random IDs, or other dynamic fields in the prefix." [FACT, OR] "Prompt caching with Moonshot AI is automated and does not require any additional configuration."
  => `docs/opo/NET.md:131` ("Claude и Kimi — по меткам cache_control") and `app/net/gateway.py:133-137` (`cache_marks` returns True for kimi/moonshot) are not supported by these two sources for Kimi K2/K3 (marks are harmless but unnecessary). **[NOT VERIFIED]** for older K2 endpoints.
- **OpenRouter pass-through [FACT, OR]:** `usage.prompt_tokens_details.cached_tokens` (read) and `cache_write_tokens` (written); `cache_discount` in the generation record (negative for writes); **provider sticky routing**: after a cached request later requests go to the same provider; "only activates when the provider's cache read pricing is cheaper"; expires after 60 minutes idle;
  keyed per account+model+conversation, where a conversation = hash of the first system/developer message + first non-system message, or an explicit `session_id` / `x-session-id` (<=256 chars) or `prompt_cache_key`; "not used when you specify a manual provider order via provider.order"; with `session_id` it activates on any successful request. Anthropic: top-level `cache_control` = automatic breakpoint that "advances as conversations grow"; `cache_control` <-> `prompt_cache_breakpoint` are converted (TTLs are not).
  For GPT-5.6+ you can disable caching per request with `prompt_cache_options.mode: "explicit"` and no breakpoints (avoids the 1.25x write on one-shot prompts).
  [FACT, repo] The SYSAI gateway sets no `session_id`/`prompt_cache_key` (`rg` over `app/net/gateway.py`), so stickiness relies on the message hash; and it sends OpenRouter a `models: [tier model, fallback]` list (NET.md:125-129) — a fallback turn is a cache miss (Hermes dev guide: model identity is part of the cache key).

**Rules that keep a prefix stable (each from a primary source)**
1. Order is tools -> system -> messages and "Modifying tool definitions (names, descriptions, parameters) invalidates the entire cache" (ANT). OpenAI lists "tools: Changes tool names, descriptions, schemas, ordering" and `context_management`/compaction as prefix-changing (OAI).
2. Put large/common content first, variable content last; no timestamps or random IDs in the prefix (KIMI; GG "Try putting large and common contents at the beginning ... send requests with similar prefix in a short amount of time"; ANT shows a timestamp-in-prefix example that never hits).
3. Append-only history: "Adding or removing messages in the middle invalidates the cache for everything after" (Hermes dev guide); DeepSeek matches only whole persisted prefix units (DS).
4. Deterministic tool order: **MCP spec 2026-07-28: "Servers SHOULD return tools from tools/list in a deterministic order to enable client-side caching and improve LLM prompt cache hit rates."**
5. Do not switch model/credentials/provider mid-session (Hermes dev guide; OpenRouter sticky routing is per model).
6. Add tools late rather than editing the prefix: Anthropic beta `inline-tools-2026-09-15` ("tool_addition" block in a mid-conversation system message keeps the cached prefix), OpenAI tool search injects discovered tools "at the end of the context window" (OpenAI tool-search doc), Anthropic tool search "doesn't break prompt caching" (A2).
7. Anthropic: a growing conversation needs a breakpoint within the 20-block lookback of the previous write (ANT); add a second breakpoint if a turn adds >=20 blocks.

---------------------------------------------------------------------------------------------------------------------------

## 6. 2026 developments worth knowing

### 6.1 MCP specification revision **2026-07-28** (latest) — https://modelcontextprotocol.io/specification/2026-07-28/changelog (fetched 2026-11-12; changes since 2025-11-25)
[FACT] Stateless protocol (no `initialize` handshake, no `Mcp-Session-Id`; each request carries version/capabilities in `_meta`), mandatory `server/discover`, `subscriptions/listen` replaces GET stream and resource subscribe, tasks moved from core into the official extension `io.modelcontextprotocol/tasks` (polling `tasks/get`), Multi Round-Trip Requests (`resultType: "input_required"`) replace server-initiated sampling/elicitation/roots requests,
**CacheableResult: `ttlMs` and `cacheScope` required on `tools/list`, `prompts/list`, `resources/list`, `resources/read`**, deterministic `tools/list` order SHOULD (cache hits), `inputSchema`/`outputSchema` may use any JSON Schema 2020-12 keywords, `structuredContent` any JSON value, Roots/Sampling/Logging **deprecated** (12-month deprecation window policy), HTTP+SSE and RFC 7591 DCR deprecated (Client ID Metadata Documents preferred), `x-mcp-header`, OpenTelemetry `_meta` conventions.
2025-11-25 (https://modelcontextprotocol.io/specification/2025-11-25/changelog): icons, URL-mode elicitation, sampling with tools, experimental tasks, tool-name guidance, JSON Schema 2020-12 default, Client ID Metadata Documents.
[INFERENCE for us] Our hub servers hand-roll protocol `2025-06-18` (`hub/seed/plc-kit/server.py` `PROTOCOL`); nothing in 2026-07-28 forces an upgrade today, but (a) return `tools/list` in a fixed order, (b) `annotations` (readOnlyHint etc.) are already used.

### 6.2 MCP extensions
- [FACT] **SEP-2127 MCP Server Cards** (Extensions Track, **Final**, created 2026-01-21): pre-connection discovery of transports/versions (`io.modelcontextprotocol/server-card`), https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/seps/2127-mcp-server-cards.md .
- [FACT] **SEP-2640 Skills Extension** (**Final**, created 2026-04-23): serve Agent Skills over MCP resources (`skill://` URIs, `skills/list`, `skills/get`), https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/seps/2640-skills-extension.md .

### 6.3 "Tool search" in clients (the mainstream answer to definition bloat)
- Anthropic API: `tool_search_tool_regex_20251119` / `..._bm25_20251119` + `defer_loading` (A2, A7); **Claude Code**: "Tool search is enabled by default: MCP tools are deferred ... Only tool names and server instructions load at session start" (https://code.claude.com/docs/en/mcp, docs 2026-11-12); MCP output warning at 10,000 tokens, default cap 25,000 (`MAX_MCP_OUTPUT_TOKENS`).
- OpenAI Responses API: `tool_search` tool + `defer_loading: true` on functions/namespaces/MCP servers; "only gpt-5.4 and later models support tool_search"; "aim to keep each namespace to fewer than 10 functions"; "preserve the model's cache" (https://developers.openai.com/api/docs/guides/tools-tool-search, docs 2026-11-12). [INFERENCE] Not reachable from Hermes' chat-completions path through OpenRouter; Hermes has its own (§4.2).
- Hermes built-in Tool Search (§4.2). [FACT, code search] The `openai/codex` repo contains `codex-rs/code-mode-runtime` (V8) and `core/src/tools/code_mode` — Codex CLI implements code mode (docs not read); mcp-compress-router's README names "Claude Code's Tool Search, Codex CLI's code mode, and OpenCode V2's Code Mode" as host-side deferral (README, fetched 2026-11-12).

### 6.4 Agent Skills as an open standard
[FACT] Spec and reference library at https://agentskills.io/specification and https://github.com/agentskills/agentskills (27,212★, Apache-2.0, created 2025-12-16); Anthropic announced the open standard on 2025-12-18 (A5); Hermes is "compatible with the agentskills.io open standard" (README/HK); MCP has a Final Skills extension (§6.2). SYSAI's `SKILL.md` format (NET.md:55) is already this standard. `anthropics/skills` has 193,480★ (no licence field).

### 6.5 Sandbox runtimes for code execution (if code mode is ever wanted)
| Runtime | Facts (GitHub/doc, fetched 2026-11-12) | Fit |
|---|---|---|
| **pydantic/monty** | "A secure Python sandbox, written in Rust, for code written by AI"; MIT; 8,894★; PyPI `pydantic-monty` 1.6.0 (2026-11-11, py>=3.10); "under 1ms" start vs "around 1500ms for a sandbox service"; filesystem/env/network "do not exist inside the sandbox"; host functions injected (`external_lookup`) | Natural for a Python host (only a Python *subset*); used by `avencera/codemode-mcp` |
| Deno | MIT, 108,868★, last push 2026-10-29; "A modern runtime for JavaScript and TypeScript" | used as the code sandbox by jx-codes/codemode-mcp (its README) |
| Pyodide | MPL-2.0, 14,936★, "a Python distribution for the browser and Node.js based on WebAssembly" | Node/browser host needed |
| e2b | Apache-2.0, 14,823★, "Open-source, secure environment with real-world tools for enterprise-grade agents" | deployment model (hosted vs self-hosted) not checked |
| Cloudflare Dynamic Workers / workerd | V8 isolates; Dynamic Workers docs "Last updated Oct 31, 2026"; workerd Apache-2.0 9,175★; `@cloudflare/codemode` "Experimental" | Workers platform only |
| Vercel Sandbox, Docker, Daytona, Modal, SSH | built-in Hermes *terminal backends* (HC/H0), not code-mode sandboxes | n/a |

### 6.6 Models the owner uses (OpenRouter catalogue, https://openrouter.ai/api/v1/models, fetched 2026-11-12; $/M tokens)
| Model id | Released (catalogue `created`) | Context | Input | Output | Cache read | Cache write | Tier / notes |
|---|---|---|---|---|---|---|---|
| `openai/gpt-6.1-terra` | 2026-10-26 | 1,050,000 | 1.00 | 5.00 | 0.05 (0.05x) | 1.25 | **>=272,000 prompt tokens: 2.00 / 7.50 / read 0.10 / write 2.50** (`pricing.overrides[min_prompt_tokens=272000]`); matches OpenAI's page for the short tier (https://developers.openai.com/api/docs/pricing; the page has a Short/Long context toggle, long values not in static HTML) |
| `openai/gpt-6-luna` | 2026-09-22 | 1,050,000 | 0.10 | 0.50 | 0.01 | 0.125 | >=272,000: 0.20 / 0.75 / 0.02 / 0.25 |
| `deepseek/deepseek-v4.1-flash-1017` | 2026-10-17 | 1,048,576 (max out 393,216) | 0.15 | 0.60 | 0.003 (0.02x) | none | peak (Mon-Fri 01-04 and 06-10 UTC, excl. Chinese holidays) = 2x: 0.30 / 1.20 / 0.006 (DeepSeek pricing page; same schedule in `pricing.overrides`) |
| `anthropic/claude-haiku-5.5` | 2026-10-07 | 1,000,000 | 0.10 | 0.50 | 0.01 | 0.125 (5m) / 0.20 (1h) | **>=100,000 prompt tokens: 0.50 / 2.50 / 0.05 / 0.625 / 1.00** |
| `google/gemini-2.5-flash` | 2025-06-17 | 1,048,576 | 0.30 | 2.50 | 0.03 | (storage-based) | `pricing.internal_reasoning` 2.50 = output price |
| `google/gemini-2.5-flash-lite` | 2025-07-22 | 1,048,576 | 0.10 | 0.40 | 0.01 | (storage-based) | `internal_reasoning` 0.40 = output price |
| `openai/gpt-4.1-nano` | 2025-04-14 | 1,047,576 | 0.10 | 0.40 | 0.03 | none | |
Reference: `anthropic/claude-sonnet-5.5` 2.00 / 10.00 / read 0.10 / write 2.50; `moonshotai/kimi-k3.1-pro` listed (prices not compared).
- [FACT] Predecessor tiers in the same catalogue: `gpt-5.6-terra` 2.00 / 12.00 / read 0.20 -> `gpt-6.1-terra` 1.00 / 5.00 / 0.05; `gpt-5.6-luna` 0.20 / 1.20 / 0.02 -> `gpt-6-luna` 0.10 / 0.50 / 0.01; `deepseek-v4-flash-0731` 0.0434 / 0.30 / read 0.035 -> `v4.1-flash-1017` 0.15 / 0.60 / read 0.003 (input up, cached-input price down 12x).
- [INFERENCE, assumptions: the brief's 142 requests / 38.79M tokens / $5.05 / cache 98%, all tokens counted as input] If the *average* request were in the long tier, input alone would cost $0.0404/request > the observed $0.0356 average, so most requests were under 272k; the tier matters for the tail. Under the base tier: cached reads ≈ $1.90 (38%), fresh/written tokens ≈ $0.97 (19%), remainder ≈ $2.18 (43%) would be output/reasoning (~0.44M output tokens). This split is a **hypothesis** — the real split is in `net_spend`/trace.

---------------------------------------------------------------------------------------------------------------------------

## 7. Practical numbers: size of tool definitions and realistic reductions

### 7.1 Literature (what others measured)
| Source (date) | Measured set | Tokens | Tokens/tool |
|---|---|---|---|
| Anthropic A2 (2025-11-24) | GitHub 35 tools / Slack 11 / Sentry 5 / Grafana 5 / Splunk 2 | ~26K / ~21K / ~3K / ~3K / ~2K (58 tools ≈ 55K) | 743 / 1,909 / 600 / 600 / 1,000 (avg 948) |
| Anthropic A2 | "50+ MCP tools" | ~72K | ≤1,440 |
| Scalekit (2026-03-11) https://www.scalekit.com/blog/mcp-vs-cli-use | GitHub Copilot MCP server, 43 tools, Claude Sonnet 4, 75 runs | 44,026 for the simplest task (CLI: 1,365) | ~1,024 (includes task text) |
| Atlassian (2026-03-29) | 94-tool GitHub scenario | 17,600 (uncompressed) | 187 |
| mcp2cli README (fetched 2026-11-12; "measured directly against the Claude API") | zereight/gitlab-mcp, 122 tools | 27,969 | 229 |
| mcptoon docs (self-measured, synthetic) | 255 tools / 50 / 5 | 71,929 / 14,113 / 1,519 | 282 / 282 / 304 |
| mcptoon real-machine (self-measured) | 209 tools, 7 servers (170 from one) | 640,606 | 3,065 |
| Cloudflare CF2 | >2,500 endpoints | 1.17M | ~468 |
| Droid Mode README (third-party, unverified) | 22-tool server ≈ 2,400 | | ~110 |
So published per-tool costs span roughly **190 to 1,900 tokens** (median of the independent sets ≈ 500-750); hand-written schemas with long docstrings sit at the high end, terse ones at the low end.

### 7.2 Measured here (read-only) on SYSAI's own hub MCP servers
Method: started each `hub/seed/*/server.py` over stdio from `/tmp` with `python -I -B` (no bytecode written), sent `initialize` + `tools/list`, counted tokens with tiktoken `o200k_base` (`/tmp/research/scripts/measure_tools.py`; `ebpro-cmtp` needed `cryptography`, supplied via `uvx`).
| Server | Tools | Tokens (OpenAI function form) | Avg/tool | Instructions field |
|---|---|---|---|---|
| plc-kit | 17 | 3,997 | 235 | 315 |
| ebpro-cmtp | 14 | 4,304 | 307 | 133 |
| ebpro-gui | 11 | 2,365 | 215 | 104 |
| machine-expert | 9 | 2,129 | 237 | 204 |
| pic-index | 5 | 1,199 | 240 | 153 |
| doc-kit | 4 | 1,189 | 297 | 173 |
| **Total** | **60** | **15,183** (raw MCP JSON 16,423) | **253** (median 208) | 1,082 |
Largest tools: `ebpro-cmtp.cmtp_edit` 1,684; `doc-kit.doc_export` 537; `machine-expert.me_online` 523; `plc-kit.plc_project_build` 442; smallest: 83-98. Descriptions are Russian (≈2.2 chars/token), which makes tokens/tool higher than for English schemas of equal length.
NOT measured: Hermes' built-in tool schemas (README: "40+ tools"), the `sysai` HTTP MCP server (`/mcp`), SOUL.md, memory, skill index.

### 7.3 What this implies (arithmetic from §7.2 and the brief; assumptions explicit)
- Hub definitions = 15,183 / 273,169 = **5.6%** of the average terra request. At cached-read price ($0.05/M) over 142 requests: 2.156M token-requests = **$0.11 (2.1% of $5.05)**; if they were re-sent uncached every time: $2.16 (43%). [INFERENCE] Since the brief reports 98% cache hits, the first number is the realistic one.
- **Plain per-stage filtering** (e.g. PLC stage = plc-kit + machine-expert + doc-kit = 7,315 tokens; HMI stage = ebpro-cmtp + ebpro-gui + pic-index = 7,868): −48% to −52% of definitions (≈7.3-7.9k tokens/request), deterministic, no new component. But it must happen at session/subagent boundaries: toolset edits mid-session invalidate the cache (HT, ANT, OAI). Hermes cannot pass toolsets to `delegate_task`; use separate profiles/sessions.
- **Hermes Tool Search** (already built in): ≈ 1.7k listing (my estimate, `listing_estimate.py`) + bridge schemas (assumed 0.5-1k) = 2.2-2.7k, i.e. **≈ −84%** of definitions (≈12.7k tokens/request, 4.6% of an average request, ≈$0.09 per 142 cached requests). Consistent with Anthropic's measured −85% for a 50+ tool set (A2). Costs: 1-2 extra model calls per cold tool, search-quality dependence.
- **Code Mode / search+execute** (Cloudflare-style, ~1k fixed tokens): at most another ~1.5-2k tokens below Tool Search for our 60 tools; its real advantage is keeping *intermediate results* out of context (A1, A2: PTC −37% on research tasks) — relevant only if our tool outputs are large; Hermes already spills big results to disk and has `execute_code` for native tools. It adds a sandbox, code-generation failure modes (output tokens cost 5x input here), and moves access checks inside the gateway.
- **Take-away [INFERENCE]:** for 50-60 Python stdio tools the *maximum* gain from any definition-side technique is ≈ 5% of an average request (≈ 2% of terra spend when the prefix is cached). Definition bloat can matter for accuracy (Anthropic: selection degrades beyond 30-50 tools; Hermes core 40+ plus our 60 plus `sysai` tools exceeds that when everything is eager), but it cannot by itself explain $25 -> $3-5. The levers with the largest documented effect are history size (compaction threshold/window, proactive prune, tool-output spill), output/reasoning tokens, price tiers (272k), background-review forks, and cache stability (no model swaps/fallbacks). Which of them dominates needs the per-request trace (tokens by role, `cached_tokens`, `cache_write_tokens`, request size histogram, tool-array length).

---------------------------------------------------------------------------------------------------------------------------

## 8. Cross-area notes for the other research agents (brief)
- **Jev / auto routing:** Hermes dev guide says any mid-conversation model change gives zero cache hits and re-reads the conversation at undiscounted price; OpenRouter sticky routing is per model; the gateway's `models: [x, fallback]` can switch mid-conversation. Relevant to `sysai/auto` and to fallbacks (NET.md:111-129).
- **Context window knowledge:** gateway `/v1/models` has no `context_length` (`app/net/gateway.py:515-518`); Hermes falls back to detection chain/128K (HP). Consider `providers.sysai.models["sysai/<t>"].context_length` in the signed config (same place where `prompt_caching` is set, `app/net/access.py:569-571`) — **requires verifying what Hermes currently resolves**.
- **Trace attribution:** background-review forks use the main route and carry no `extra_body.sysai.role` in the current config (`access.py:544-553` sets roles only for workers/compression/vision) -> they would appear as "главная" in «Кто спросил» (`app/net/trace.py:19-20`). **[INFERENCE]**, unverified.
- **Price tier at 272k** for `gpt-6.1-terra` (and 100k for haiku-5.5): count requests above the thresholds in the trace.
- **DeepSeek peak window**: 01-04 and 06-10 UTC Mon-Fri = 04-07 and 09-13 Moscow time (UTC+3) — the engineers' working hours pay 2x on deepseek-v4.1-flash.
- **Approvals (owner item 5):** `approvals.mode: off` is already written; see §4.5 for other sources of "may I create this file?".
- **Sessions (item 6):** Hermes persists/lists/loads/resumes ACP sessions; check whether `agents/desktop` ever calls list/load/resume.

---------------------------------------------------------------------------------------------------------------------------

## 9. NOT VERIFIED / limits of this research
- Which page the owner read for "99.3%" (only a plausible trace to mcptoon's own benchmark).
- mcp2cli's "96–99%" write-up (JS-only page); Cloudflare's "up to 80%" methodology (CF3); Droid Mode per-tool numbers; Scalekit's "~90% with schema filtering" (a product estimate).
- Hermes version on the engineers' PCs; whether Tool Search is on there and applies to ACP sessions; which window Hermes resolves for `sysai/*`; when Tool Search became default.
- Hermes built-in tool schema size; `sysai` MCP server definitions; the composition of the ~273k average request (needs the gateway trace).
- OpenAI long-context tier values come from OpenRouter's catalogue only; Gemini minimum-token conflict (Google 2,048 vs OpenRouter 1,024/4,096); OpenRouter docs' Gemini/DeepSeek read multipliers differ from the catalogue (catalogue used).
- Kimi: NET.md says marks are required; Moonshot and OpenRouter docs say automatic — not tested on the real endpoint.
- Reddit was unreachable (403) and DuckDuckGo blocks bots; sources were found via GitHub (`gh`), HN Algolia API, vendor sites. No web search tool was available, so coverage of blogs outside these channels is incomplete.
- The brief's "$25 per project" vs the screenshot total (~$5.3 over the displayed period) was not reconciled (not my area).

## 10. Artifacts
- Report: `/tmp/research/F-token-saving-research.md`; raw pages `/tmp/research/raw/*/page.txt`; READMEs `/tmp/research/raw/readmes/*/README.md`; mcptoon files `/tmp/research/raw/mcptoon-files/`; OpenRouter catalogue `/tmp/research/raw/openrouter-models/models.json`; Hermes releases JSON `/tmp/research/raw/hermes-releases/`.
- Scripts: `/tmp/research/scripts/{h2t,csearch,readme_scan,measure_tools,listing_estimate,calc,tk_test}.py`.
