# C - Machine Expert (PLC/ST) pipeline, compile, controller, project folder

Date 2026-11-12. Repo `/workspace/SYSAI` @ main 7ad99a7 (read-only study; nothing in the repo was changed - checked with `find -newer`).
Tags: **FACT** = seen in code / docs / test / my measurement; **PUBLIC** = public source (URL named); **HYP** = my hypothesis;
**UNVERIFIED** = could not check. Token numbers: `tiktoken o200k_base` (works in this sandbox), Russian text measures ~3.2 chars/token
here (not 2.2).

Evidence scripts and raw outputs are in `/tmp/research/` (list at the end, section 11).

---------------------------------------------------------------------------------------------------------------------------------

## 0. Summary (what matters most)

1. **Nobody has ever compiled our output.** Machine Expert (ME) was never run on a generated `.project`; the compile loop was tested only
   against a fake ME whose "compiler" knows two rules (`@@` = error, `TODO` = warning) (`tests/fake_machine_expert.py:1-10`).
   We also do not have the text of the owner's compile errors, so **every cause below is a ranked hypothesis**. The cheapest way to turn it
   into facts is the 30-minute "bisect protocol" in 2.5.
2. **`plc_check` is a heuristic lint, not a compiler, and its gate is weak.** On 30 injected realistic faults (the compiler would reject all
   of them; harness `c_mutate.py`) it **blocks the build 3 times (10 %)**, only warns 11 times (some by accident), and is **silent 16 times
   (53 %)**: every type error, member typo, FB-call parameter error, `&&`/`!=`, unclosed comment, bad time literal, reserved word, recursion,
   task->FB (section 2.2). `plc_project_build` writes the `.project` when there are only warnings.
3. **Biggest structural hole (most likely compile breaker, HYP):** a project built from a template **keeps the template's channel->variable
   bindings after the program is stripped**, so channels point at variables that no longer exist. The repo's own test asserts this
   (`tests/test_hub_seed_plc_project.py:265-277`: after `create`, channel I0 still holds `Application.old_0`) and the build report still says
   "сходится". Nothing checks that every mapped variable is declared, has the right type, or is not declared twice (R4).
4. **A real parser closes the whole syntax class; the semantic class needs symbols + types + library signatures.** With an adapter, `blark`
   (GPL-2.0) rejected 12/12 syntactic faults, `iec-checker` v0.4 (LGPL-3.0, 10 MB static binary) ~10-12/12; union with today's `plc_check`
   flags 18-19/30. The remaining 11-12 are all semantic (types, members, call parameters, recursion, task kind, address bits).
5. **Compile on the PC exists only as an untested design** (`hub/seed/machine-expert`). Static cross-check: of ~80 scripting names used by
   `me_job.py`, all but 6 exist in the public CODESYS ScriptEngine docs (the 6 are guarded fallbacks). The shaky part is Schneider's
   shell: whether `LogicBuilder.exe`/`LogicBuilderShell.exe` accept `--runscript/--noUI` is **not documented publicly**; the author of a
   public CODESYS-MCP toolkit writes that it "has never been run against" Machine Expert and asks users for exactly this
   (PUBLIC: github.com/johannesPettersson80/codesys-mcp-toolkit `docs/machine-expert-testing.md`).
6. **Connecting to controllers:** no live-connection code exists (no pymodbus/asyncua anywhere; `me_scan`/`me_online` go through ME only).
   Best first step: **read-only OPC UA** to M241 (the verified customer panel project already uses it, node ids are already computable:
   `plcme.m241_node_id`), as a new hub tool with its own `uv` venv (the sync agent already supports `requirements.txt` per tool,
   `agents/sync/sysai_agent/hubsync.py:80-98`).
7. **The folder mess has three causes, all in our own text/code:** (a) `plc/` is the only place the skill tells the agent to write, so
   generators/notes/CSV land there (`plc-project/SKILL.md:28,67`); (b) the window lists every file of `plc/ hmi/ docs/` flat under
   "Сделано агентом" (`agents/desktop/renderer/js/projects.js:158-166`); (c) the deterministic generators that TASKS.md plans
   (ОПО-10.1/10.2/9.4) were never built, so the LLM writes its own Python generators each time (`gen_io.py`, `gen_mech.py`, ...
   in the screenshot: ~46 KB of scripts + ~22 KB of notes + ~29 KB csv/json next to ~61 KB of ST).
8. **Cost lever inside this area (cross-area, for the token audit):** PLC tool outputs are context bombs. 336 synthetic variables ->
   `plc_hmi_tags` (opcua) = 36.7k tokens in ONE result, `plc_variables` = 22.7k, `plc_pou GVL` = 6.6k. The static PLC prompt
   (plc-kit tools/list 4.4k + machine-expert 2.3k + skill 3.6k = ~10.3k tokens) is only ~4 % of the 273k average request, so tool
   definitions are not the PLC-side driver; big tool results and number of turns are (HYP).
9. **Tests (task 8):** `tests/test_hub_seed_plc_project.py` + `tests/test_hub_seed_plc.py`: **24 passed in 14.96 s** (needed an ephemeral
   `uvx` env, see section 8).

---------------------------------------------------------------------------------------------------------------------------------

## 1. The current pipeline, end to end

### 1.1 Components

| Piece | Where | What it is |
|---|---|---|
| Skill `plc-project` 2.2.0 | `hub/seed/plc-project/SKILL.md` (154 lines, 11.7k chars = 3.6k tokens) | the stage script the LLM follows |
| Skill `project-docs` 1.0.0 | `hub/seed/project-docs/SKILL.md` | docs stage |
| MCP `plc-kit` 1.3.0 | `hub/seed/plc-kit/` (server.py 447 lines + plcst/plcme/plcbuild/plcproj/meproject/mearchive/meaes/plcstage/plclink) | 17 tools, pure stdlib, any OS: read/check ST, write `.project` from ST folder + template, IO channels, OPC UA list, stage file |
| MCP `machine-expert` 0.1.1 | `hub/seed/machine-expert/` (server.py, mehost.py, me_job.py, mesrc.py) | 9 tools, Windows only, needs licensed ME: compile, devices, scan, online |
| MCP `ebpro-cmtp` (14 tools), `ebpro-gui`, `pic-index` (5), `doc-kit` (4), MCP `sysai` (files_*, hub_*) | `hub/seed/*` | HMI / pictures / Word / storage (other agents' areas) |
| Window | `agents/desktop/lib/projects.js`, `renderer/js/projects.js`, `main.js:691` | project folder `Документы\SYSAI Проекты\<name>`, stage strip, "Создать проект" prompts |
| Wine bench | `scripts/me_engine_wine.sh`, `scripts/me_engine/` | dev-side only: real CODESYS 3.5.19.40 object manager opens the `.project` (no compile) |

### 1.2 Stages: tools, LLM vs code, artifacts, where

Stage keys are defined twice and pinned equal by a test: `plcstage.py:12-19` and `projects.js:15-22`
(`tests/test_hub_seed_plc.py:169-176`).

| # | Stage (key) | Tools used | LLM work | Deterministic code | Artifacts and where |
|---|---|---|---|---|---|
| 1 | Материалы (`materials`) | `sysai` files_*, Hermes read/vision, `plc_info`/`plc_io` on donors | read ТЗ/E3/PDF, list every mechanism and signal, count them, write `project.md` | **none** (TASKS ОПО-7/8 "разбор чертежей / теги и оборудование" are planned, not built) | `project.md`; observed: agent-written `extract_e3.py`, `extract_equipment.py`, `equipment_source.csv`, 4 `.md` notes, `техсхема_просмотр.png` (697 KB) |
| 2 | Решения (`decisions`) | `plc_link`, `plc_io`, `hub_search`, `plc_project_template` | choose template, style; ask the engineer | `plc_link` is table-driven (`plclink.py`: model regexes -> OPC UA/Modbus, resolution) | facts into `pipeline.json`; `project.md` |
| 3 | Программа ПЛК (`plc`) | `plc_export_st` (donor example), `plc_check`, `plc_addresses`, `plc_hmi_tags` | **writes all ST**: GVLs, DUT, FB per mechanism type, instances, IO binding, Main, alarms, `project.json` | `plc_check` (regex lint), address map | `plc/*.st`, `plc/project.json` (+ stray scripts) |
| 4 | Компиляция (`build`) | `plc_project_build`, `plc_io`, `plc_io_map`, then `me_find`/`me_build`/`me_update`/`me_online` (PC only) | decide what to fix | **file writer**: strip template program, create objects, tasks, OPC UA list, seal, re-read and compare (`plcproj.py:196-209`, `meproject.py:708-799`) | `plc/<name>.project`; ME compile also drops `<name>.project.OpcUASymbolConf.map` and `.settings.conf` next to it (`MACHINE_EXPERT.md:203-204`, claim not verified by me) |
| 5 | Экраны (`hmi`) | `plc_hmi_tags`, `pics_*`, `cmtp_*`, `plc_tag_check` | assemble screens | tag derivation, picture index, cmtp writer/checker | `hmi/tags.csv`, `hmi/*.cmtp`, `hmi/pictures/` |
| 6 | Документы (`docs`) | `doc_*`, `cmtp_render` | write Markdown | docx builder + checker | `docs/*.md`, `docs/img/*.png`, `docs/*.docx` |

Count of agent-facing tools in this pipeline: plc-kit 17 + machine-expert 9 + ebpro-cmtp 14 + pic-index 5 + doc-kit 4 = 49 (+ sysai + Hermes
native tools incl. `terminal`/`execute_code`).

### 1.3 Folder layout as it is defined today

`SKILL.md:24-34` and `Projects.create()` (`lib/projects.js:147-153`, `PARTS` at :13) create/describe:
`materials/ plc/ hmi/ docs/ project.md style.md pipeline.json`. `plc/` = "программа: *.st и project.json; Line.project; plc.xml".
Nothing in any skill or tool defines a place for scratch files. Tool write targets are always explicit arguments
(`out_file`, `out_dir`, `src_dir`); the only implicit write is `pipeline.json` in the project root (`plcstage.py:79-83`, atomic tmp+replace).
Agent cwd is the user's home (`main.js:339`: `new AcpClient({..., cwd: os.homedir()})`), and Hermes `terminal`/`execute_code` are allowed when
"Команды на ПК" is on (`agents/sync/sysai_agent/hook.py:30,136-146`) - that is how the `.py` generators appear.

### 1.4 How stage progress is tracked

- Writer: tool `plc_stage_update` -> `<project>/pipeline.json` `{version, stages{key:{state,note,updated}}, facts{...}}`
  (`plcstage.py`). Rules: states `todo/work/done/problem`; `done` is refused until all earlier stages are `done`, except `hmi`/`docs` may skip
  `build` (`plcstage.py:60-66`).
- Reader: `projects.js:68-87` `pipeline()`; renderer polls `projects:info` every 4 s (`renderer/js/projects.js:30`), recomputes a stamp from
  stage states + facts + the list of files of `materials/plc/hmi/docs` and re-renders on change. Strip: 6 numbered buttons (tick/pulse/alert), facts
  line (controller, panel, resolution, link, style), note of the current stage; a click sends a prompt to the agent (`:184-195`).
- **Gap (FACT):** a stage is whatever the LLM says. `build: done` can be set with no compile ever run; `facts.machine_expert` is free text.
  The strip has no counters (mechanisms/signals), no link to the produced file, no validator results.
- Cost note (HYP): `projects.info()` walks the whole folder (<=3000 files, depth <=6) every 4 s while a project is open.

### 1.5 Size of what the LLM carries (measured)

| Item | chars | tokens (o200k) |
|---|---|---|
| plc-kit `tools/list` (17 tools) | 15,429 | 4,360 |
| machine-expert `tools/list` (9 tools) | 8,465 | 2,335 |
| `plc-project/SKILL.md` | 11,663 | 3,633 |
| `plc-kit/TOOL.md` / `machine-expert/TOOL.md` (docs, not necessarily in context) | 15,121 / 7,786 | 4,630 / 2,295 |
| result of `plc_hmi_tags` (opcua), 336 synthetic vars with Russian comments | 90,675 | **36,667** |
| result of `plc_variables`, same project | 62,641 | 22,738 |
| result of `plc_pou GVL` | 15,948 | 6,628 |

Why `plc_hmi_tags` is so big: in OPC UA mode it returns the same rows twice (`add_tags` **and** `tags`) plus `node_m241`
(`server.py:349-357`), and the skill tells the LLM to pass `add_tags` "как есть" into `cmtp_edit` (`SKILL.md:97`), i.e. the LLM is a
copy machine between two tools (36k tokens in, similar out). Caps: 3000 rows (`plc_variables`, `plc_io`, `plc_hmi_tags`), 2000 issues
(`plc_check`). Fix idea: write to file, return summary + path; give `cmtp_edit` a `tags_file` (as `plc_tag_check` already has, `server.py:362-365`).

---------------------------------------------------------------------------------------------------------------------------------

## 2. Why the generated project is not verified to compile

### 2.1 What is verified and what is not (FACT)

Verified (`MACHINE_EXPERT.md:158-173`, handoff section 5, tests): container read/write byte-for-byte on 3 customer projects; program from `.project`
== their `.export` (49 POU, 11 lists); objects we create == objects the CODESYS engine creates (POU, FB, function, method, action, GVL, 4 DUT kinds,
folder - **tasks are not in that list**, `MACHINE_EXPERT.md:165`); the CODESYS 3.5.19.40 object manager under Wine opens/re-saves our output; 13 messages
at `projects.open` equal the original project's (all "library not installed").
Not verified: opening in the ME window, any compile, Schneider checks at open (version/compat), download. Reasons: no licence for the shell
(`EME_Startup_Feature`), no libraries, no device repository on the bench (handoff section 6/7).

### 2.2 What `plc_check` / `plc_build` check today (FACT, code reading + fault injection)

Reading (`plcst.py`, `plcme.py:420-500`, `plcbuild.py`):

| Capability | Status |
|---|---|
| Parser | **none.** `blank_comments` + keyword stack (IF/CASE/FOR/WHILE/REPEAT) + bracket balance + 2 line heuristics (`=` vs `:=`, missing `;`) (`plcst.py:266-324`) |
| Declarations | statement split by `;`, `AT %addr`, init, comments, group titles (`plcst.py:145-228`); Latin-only names (Cyrillic names silently dropped from the var list, `_IDENT`) |
| Symbol resolution | **first identifier only**, against locals/globals/POU/GVL/types/`STANDARD`+keywords; **member after `.` skipped; named parameter of a call skipped** (`plcst.py:244-263`, `plcme.py:466-475`); result is a **warning** |
| Types | **no type checker.** Unknown *declared* type -> warning (`plcme.py:446-449`) |
| Library awareness | a flat whitelist `STANDARD` (TON, R_TRIG, BLINK, PID...) independent of the template's Library Manager; **every `X_TO_Y` conversion is "unknown"**: of 81 common IEC/CODESYS names 43 are flagged (INT_TO_REAL, REAL_TO_INT, TO_INT, ...) |
| Addresses | syntax and overlaps only; if any `%MX a.b` has b in 8..15 the whole project is silently switched to "word bit numbering" - so `%MX0.9` on an M241 (byte numbering) is accepted (`plcme.py:260-261`) |
| Tasks | task->POU must exist, **any POU kind** (FB/function pass, `plcme.py:478-481`); no priority/interval validation |
| Link | OPC UA lists exist; `{attribute 'symbol'...}` present (warning); no pragma-typo check |
| Gate | `plc_project_build`/`plc_build` refuse **only on level `error`**; warnings pass; `force` bypasses (`server.py:243-248`) |
| After write | `_verify` re-reads and compares POU/GVL/DUT **text only** (`plcproj.py:174-193`) - no IO, no tasks, no template compare |

Fault-injection (`/tmp/research/c_mutate.py`, output `c_mutate.out`): valid mini project (GVL with symbol pragma, FB with TON+R_TRIG, Main, 1 task),
34 single mutations; 30 are things the CODESYS compiler must reject (expected verdict = IEC/CODESYS knowledge, **not run in ME**).
Verdict = new issues relative to the valid baseline.

| Class | n | plc_check blocks (error) | warns | silent |
|---|---|---|---|---|
| S syntax (missing END_IF/`;`, `=` for `:=`, `&&`/`!=`, ELSEIF, END_FUNCTION_BLOCK/END_PROGRAM terminators, METHOD inside FB text, unclosed comment/string, `T#5sec`) | 11 | 1 (S01) | 7 | 3 (S04 `&&`/`!=`, S08 comment, S10 time literal) |
| D declarations (undeclared name, unknown FB type, duplicate var, reserved word `Not`, FB recursion, `%MX0.9`, same `%MW` twice) | 7 | 1 (D03) | 3 (D01, D02, D07) | 3 (D04, D05, D06) |
| T types/members/calls (member typo `tonStart.QQ`, wrong named param, positional garbage call, BOOL:=INT, INT as IF condition, const array index out of range, STRING:=5, wrong function `TO_INTEGER`, REAL:=BOOL) | 9 | 0 | 1 (T08) | 8 |
| K tasks (task calls an FB, task calls missing POU, priority 99 + interval "fast") | 3 | 1 (K02) | 0 | 2 (K01, K03) |
| **Total** | **30** | **3** | **11** | **16** |

Of the 11 "warnings", five are accidents of the unknown-name heuristic (`ELSEIF`, `END_*`, `METHOD` reported as "undeclared name"). Controls: the valid
project gives 1 warning (`INT_TO_REAL` false positive); CASE/FOR/member-access controls are clean.

Also FACT: the SKILL tells the agent "каждое предупреждение разбери" (`SKILL.md:76`) - the agent is told to chew through the false positives.

### 2.3 Risk list: causes of compile failure in OUR output

"Offline?" = can it be checked without Machine Expert. P = partly / needs data harvested from the PC.

| # | Risk | Evidence | Today | Offline? | What it takes |
|---|---|---|---|---|---|
| R1 | **ST syntax errors** in LLM-written ST (~61 KB visible in the screenshot) | table 2.2: 10 of 11 S-faults reach the file (only S01 blocks; 7 warn, 3 silent) | weak | **Y** | real parser (own ~1 week or vendor LGPL); or ~10 regexes (hours) |
| R2 | **ST semantics**: undeclared names, member/param typos, type mismatches, conversions, call signatures | T-class 9/9 missed or warning-only | none | Y for project-defined types; **P** for library FBs (needs signatures) | symbol table + type checker for the subset we generate; library catalog |
| R3 | **Library references/placeholders/versions**: program may use an FB/function that the template's Library Manager does not reference (BLINK/PID are in `Util`; M241 system libs) | `STANDARD` whitelist is template-independent; Library Manager object is kept by `strip_program` but never read | silent | **P** (list of refs from template: Y, ~1 day; signatures: need `.library` files) | `plc_libraries` tool + PC harvest |
| R4 | **IO mapping / device tree vs generated GVLs**: stale `Application.old_0`-type bindings after strip; `plc_io_map` does not check the variable exists (`meproject.py:583-610`), its type vs channel type, direction, duplicates; variables that the mapping itself creates (`CreateVariable`, `IoConfig_Globals_Mapping`) vs a same-named GVL variable; `AT %IX/%QX` declared in GVL while the channel is also mapped; module set vs signal list | test `:265-277`; `plcproj.create` returns no IO info; build text prints "сходится" (`server.py:269`) | **silent** | **Y** (both sides are in our hands) | IO-binding report in `plc_project_build`: unbound-and-undeclared, declared-twice, type/direction mismatch, signals without channel, channels without signal; option to clear all bindings in a template |
| R5 | **Name clashes**: object-level duplicates are caught at load (`plcbuild.py:99-101`, case-insensitive); **reserved words as identifiers** (D04), variable vs POU/type/GVL name, `STANDARD` contains TO/BY/AT/OF so they pass as names | D04 silent | partial | Y | keyword table + clash rules |
| R6 | **GVL pragmas/attributes**: symbol pragma presence is a warning; typo/`qualified_only` vs IO mapping unchecked; `set_opcua` publishes only plain-type vars of the named lists and **requires** an existing OPC UA Symbol Configuration object in the template (`meproject.py:622-644`; not created by our code - only `me_job.ensure_symbols` tries, with an API name that is not in the public docs) | K06 silent | partial | Y | static rules; template check |
| R7 | **Task configuration**: a *new* task object is built from a hand-made field layout (`meproject.py:489-512`), never diffed against an engine-made task; `apply(clean=True)` deletes template tasks not named in `project.json` (`:780-782`); no `tasks` key => template tasks are emptied (`strip_program` sets `pous=[]`, `apply` skips) so nothing runs (`plc_check` only warns "нет ни одной задачи", `plcme.py:482-483`); task->FB and priority/interval unchecked (K01/K03) | code + table | weak | Y (static) / P (diff vs template task layout) | rules + keep template tasks by default |
| R8 | **Removed precompile caches** (`*.precompileinfo.auxiliary`: `applicationcontext...`, `poolcontext.pool...` in the engine fixture) on every change (`meproject.py:252-255`) - docs claim ME rebuilds them | engine-made fixture lists both entries | n/a | **N** | only ME |
| R9 | **Profile/version mismatch**: `profile.auxiliary`, `profile7.auxiliary`, `savecontext_profile.auxiliary` (29 KB text), `missingtypeinformation.auxiliary`, `logging_project_categories_*`, and in real projects `redundantproject*`, `devicesmetainformation`, `engineering*` are carried unchanged from the template; we never compare the template's compiler/profile version with the installed ME (`me_find` knows it) | `mearchive list` of the fixture; no code touches them (grep) | none | P (read + compare: Y; effect: N) | version guard (public headless-build script does this, see 4.2) |
| R10 | **Schneider-specific files untouched / unknown object kinds left in the template** (Trace, Recipe, Alarm, Visu...: `strip_program` removes only 10 kinds, `meproject.py:672`; folders are removed together with everything shown in them, `:442-461`) - could reference removed variables | code | none | P (`plc_info.counts['other']` lists the kinds) | print removed names + remaining unknown kinds |
| R11 | **Template dependence**: needs same controller + same module set; controller identity (device id/version, "101a 0710", 5.2.11.24 in the 3 projects) is **never read** - only the tree name; `project.json.controller` is not compared with the template; a template whose device description version is not installed on the PC gives the "Device not installed to the system" error class (PUBLIC: zbest1000 README, known limitations) | `plcproj.create` prints tree name only | none | P (read DeviceIdentification; compare with `me_devices` on PC) | new reader + PC check |
| R12 | **Unsupported constructs**: methods/actions/properties/interfaces are not laid out in the ST folder (`TOOL.md:181-182`); an LLM that writes `METHOD ... END_METHOD` or `END_FUNCTION_BLOCK` inside the file lands them in the body | S06/S07/S11 are warnings | weak | Y | hard errors |
| R13 | `force: true` exists on both build tools and the skill does not forbid it | `server.py:243` | soft | Y | remove for agent or log |
| R14 | Windows file lock: `os.replace` onto an `.project` that ME has open fails (`plcproj.py:162-171`) | code | - | - | message only |

R4, R1, R2 are my top three (HYP, ranked by "how much generated content is exposed" and "nothing checks it"). R8-R11 decide whether even a
trivial program opens in ME - unknowable without a run.

### 2.4 Is the verification gap checkable without ME? (short answer)

About 70 % of the risk surface can be attacked offline: R1, R2 (project types), R4, R5, R6, R7, R12, R13 fully; R3, R9, R10, R11 partly (with data from the PC);
R8 and the real effect of R9/R10 only with ME.

### 2.5 The 30-minute "bisect protocol" (recommended before any code)

1. Engineer's PC, ME 2.2: `plc_project_build` with a donor template and a **trivial program** (empty `Main`, one GVL variable, the donor's own
   mapped variable names declared or all bindings cleared). Open, copy the ME "Messages" list verbatim, compile, copy messages again, save, send the saved file
   (handoff section 8.1, `MACHINE_EXPERT.md:180-191`).
2. If step 1 fails -> template/file/device/IO/profile problem (R4, R8-R11) - **not** the ST. If it passes -> add the agent's GVLs, then DUT/FBs, then Main, compiling
   each time: the first failing step names the class.
3. Paste the message list for the failing `PLC test` project. A 40-line parser mapping `C####` lines to `.st` files (`mesrc.place` logic) gives the first real
   "assisted" loop with zero scripting risk.

---------------------------------------------------------------------------------------------------------------------------------

## 3. Proposed layered OFFLINE validation chain

Principle: every layer returns a short structured report (<= ~2 KB to the LLM, full JSON to `_work/reports/`), blocks on `error`, and is calibrated
on known-good code (the 3 customer projects compile in ME, so any error our checker raises on their exported ST is a false positive; plus fault-injection
like 2.2). Customer files must not enter the repo - calibrate in the sandbox, commit only synthetic fixtures.

| L | Layer | Catches (from my table) | Data needed | Cost | Today |
|---|---|---|---|---|---|
| L0 | **Parse** (real grammar for the CODESYS ST subset: pragmas, `AT`, `VAR_GLOBAL` lists, no END_* terminators, no METHOD in text) | all 12 S/D04 faults | none | own parser ~5-8 days (stdlib), or `blark`/`iec-checker`/tree-sitter + adapter | regex lint |
| L1 | **Symbols/scopes**: locals, FB members (project types), DUT fields, enum values, GVL qualification, named call parameters, recursion, task kind | D01, D02, D05, T01-T03, K01 | project declarations (have) | 2-4 days after L0 | first identifier only |
| L2 | **IEC types**: assignment/condition/operator types, `X_TO_Y` matrix, literals (T#, 16#), const array bounds, REAL/INT/BOOL rules | T04-T07, T09, S10 | built-in function table (static) + L1 | 1-2 weeks for the subset we generate | none |
| L3 | **Library API signatures**: FB/function inputs/outputs/in-outs/return, methods, structs/enums of Standard, Util, M241 system libs | T01-T03 for library FBs; R3 | **library files from the PC** (see 3.3) + the template's reference list | 1 day tooling + 1 h engineer | whitelist |
| L4 | **IO map vs tags/spec**: channels of the template (`plc_io`) vs signal list/spec; every mapped variable declared once, type/direction ok, no orphan channel/signal, `CreateVariable` clashes, `%IX/%QX` clashes, publish list vs HMI tags (`plc_tag_check` exists) | R4, R14-class | template (have), spec | **hours-1 day** | none |
| L5 | **Project rules**: tasks (kind, priority 0-31, interval, program-only), pragmas, OPC UA object present, names/keywords, M241 byte-bit rule, `tasks` key present | R5-R7, R12, D06, K01, K03 | static | hours | partial |
| L6 | **Engine load** (Wine bench, `me_engine_wine.sh open`): real object manager reads every object; ~18 s/run; with the library repository in the bench profile the Library-Manager messages (category `{56a60174-...}`) would validate placeholder resolution (HYP) | broken objects, bad container, (libs) | ME folder (have), library repo (not yet) | exists | exists, dev-side only |
| L7 | **Real compile on the PC** (`me_build`) | everything incl. R8-R11 | licensed ME | exists, unrun | - |

(Effort numbers in this section and in 7.2 are my own estimates, not measurements.)

### 3.1 Cheap first (value per hour)

1. L4 + R4 report inside `plc_project_build` (+ `plc_project_template` option "unbind all channels", + print `removed` names): hours; hits the most likely structural failure.
2. L5 and the regex quick wins for L0 (C-style operators, `END_*`/`METHOD`/`ACTION`/`PROPERTY` in bodies, unclosed `(*`/string, time literals, keyword names,
   `X_TO_Y` into the whitelist, promote "unknown type"/"unknown name" to errors when the name is not a known library symbol): hours.
3. `plc_libraries` (read the template's Library Manager object): ~1 day; gives the real whitelist per template and kills the false positives.
4. L0 real parser; then L1.
5. L3 once the library data is in hand; L2 last (largest).

### 3.2 Open-source options (measured on my 30 faults; **toy set, indicative only**)

Adapters were needed for every external tool (add `END_*`, strip `{attribute}`, wrap GVLs, tasks) - the adapter itself is a component (`c_iec_adapter.py`).

| Tool | License / form | Result here | Notes |
|---|---|---|---|
| `blark` 0.x (klauer/blark, PyPI) | GPL-2.0, Python+lark+lxml (5 MB) | **rejected 12/12 syntax faults**, accepted all 18 syntactically valid mutants and the control (no semantics) | TwinCAT-oriented grammar (CODESYS-like); GPL = legal question if bundled in a distributed tool |
| `iec-checker` v0.4 (jubnzv/iec-checker, 2022 release) | LGPL-3.0, OCaml, 10.3 MB static ELF/EXE | rejected 12 of 30 (K03 interval counted; S06/S11 confounded by my adapter), flags PLCopen-style lint (uninitialised vars, unused vars - noisy), **no** undeclared/type/call checks | README says newer docs list 26 checks; latest prebuilt on GitHub is v0.4 |
| RuSTy (PLC-lang/rusty v1.0.8, 2026-11-06) | LGPL-3.0, Rust/LLVM, `plc check` = "run the compiler up to validation, write nothing", `-i` = "read declarations without compiling bodies" (= library stubs) (PUBLIC: book/user/reference/command-line.md) | **not run**: release assets `plc.exe` 116 MB, `plc-linux-x86_64` 145 MB (> 50 MB rule), `.deb` 27 MB (Debian-only, not tried) | the only candidate with real resolution/validation; ships a std library; needs an adapter for CODESYS GVL/pragmas; deployment weight is high; evaluate on donor ST first |
| matiec (GPL-3.0) / tree-sitter ST grammar (MIT) | - | not evaluated | matiec is named in TASKS ОПО-12.2 |

Union coverage on the 30 faults: `plc_check` alone flags 14 (3 blocking, 11 warn); + blark -> 18; + iec-checker -> 19. Remaining 11-12 are semantic:
D05 recursion, D06 address bit, K01 task->FB, T01-T07, T09 (and K03 priority).

### 3.3 Data to harvest from the customer's PC (one-time, engineer ~1 hour)

- **Library repository** (the `.library`/`.compiled-library` files ME uses: Standard, Util, system/M241 libs) and **device repository** (device descriptions for TM241 + TM3 modules).
  The handoff names "C:\ProgramData, часть Machine Expert"; I could not verify subfolder names (UNVERIFIED). Safe path: in ME open the Library Repository / Device Repository
  dialogs (they exist in CODESYS V3.5), read "Location", send a directory listing first, then the needed files.
- Purpose: (a) library signature catalog (L3); (b) put the repository into the Wine bench so `projects.open` stops reporting "library not installed" and starts validating
  placeholders (L6); (c) later: parse device descriptions to build module trees without a template (handoff 7: "Проект без шаблона").
- Alternative that needs no format work: a PC-side script (`me_job.py` job) that walks the Library Manager of the template via ScriptEngine and dumps declarations to JSON; the public
  codesys-mcp-toolkit does `list_libraries`/`get_library_api` the same way (PUBLIC, README). UNVERIFIED on ME.
- **Template factory (HYP):** ScriptEngine can add child devices (`ScriptDeviceObject.add(...)` appears in the public docs and the public toolkit builds device chains with it), so on
  the PC ME itself could assemble "controller + modules" templates from a hardware list; offline code then does the rest. Needs a live ME run to confirm.

---------------------------------------------------------------------------------------------------------------------------------

## 4. Compile on the engineer's PC

### 4.1 How it works now (FACT, code reading)

- `mehost.py`: finds `LogicBuilderShell.exe` / `LogicBuilder.exe` / `CODESYS.exe` under `C:\Program Files\Schneider Electric\EcoStruxure Machine Expert\*` (+ SoMachine, CODESYS paths, `:30-39`);
  tries in order (`:93-108`): `LogicBuilderShell.exe <script>`, then `LogicBuilder.exe [--profile=X] --noUI --runscript=<script>`, then CODESYS the same way; `SYSAI_ME_EXE` / `SYSAI_ME_CMD` override.
  Job = JSON file; script `me_job.py` (IronPython 2.7 inside ME) reads `SYSAI_ME_JOB`, writes `result.json`; host polls, kills the process after the result appears (`:207-248`), one job at a time (lock file),
  default timeout 900 s, logs in `%LOCALAPPDATA%\SYSAI\machine-expert\jobs` (last 40).
- Actions (`me_job.py:692`): `devices` (device_repository.get_all_devices), `create`/`update` (open template or `projects.create` + add controller, create/replace POU/GVL/DUT text through
  `textual_declaration/implementation`, tasks, symbol config, `proj.save()`, build), `build` (open -> `app.build()` -> messages; no save), `export`, `scan` (`gateway.perform_network_scan()`),
  `online` (simulation or real: `set_simulation_mode`, `create_online_application`, `login`, start/stop, `set_prepared_value`/`write_prepared_values`, `read_value`).
- Messages: `clear_messages` -> `build()` -> iterate `system.get_message_categories(True)` -> `get_message_objects(cat)`; row = severity, text, object, `position_text`, `prefix+number` (code),
  line = first number of `position_text`; `mesrc.place` maps (object, part, line) to `.st` file:line (`body_line + n - 1`) (`me_job.py:358-397`, `mesrc.py:140-150`). Output to the LLM: <=40 errors/15 warnings (`server.py:121-134`).
- Safety: write/download/start/stop on a real controller require `confirm: true` (`server.py:223-232`); password not left in job files.
- Never run on live ME (`TOOL.md:23-25`); fake ME has two rules (`tests/fake_machine_expert.py`).

### 4.2 What is publicly known (labelled)

PUBLIC, official CODESYS ScriptEngine documentation (copy dumped in github.com/johannesPettersson80/codesys-api `scriptDoc.txt`; the CODESYS help site itself returned 403/429 to my fetches):
- `ScriptApplication.build()/rebuild()/clean()/generate_code()` - "only works for applications in the primary project"; messages via `System.get_message_objects(category=None, severities=...)` (default category = ScriptMessage!) and `get_message_categories(bActive=True)`.
- `ScriptMessage` has `object`, `position`, `position_text`, `severity`, `text`, `prefix` ("compile, LINT, IO-Config..."), `number`.
- `--runscript` with `--noUI`: script exit terminates CODESYS; `System.prompt_handling` is obsolete in favour of `script_prompt_handling`/`prompt_answers`; `--textPrompts` for text prompts in noUI.
- Online: `create_online_application`, `login(option, ...)`, `read_value(s)`, `set_prepared_value` + `write_prepared_values`; gateway: `perform_network_scan()` (3.5.8.0), device: `set_gateway_and_address`, `set_gateway_and_device_name`, `set_gateway_and_ip_address(gateway, ip, port=11740)`.
- Public working headless-build script (zhouyongh/codesys-sync `build_project.py`; author verified on CODESYS 3.5.17 and 3.5.22): categories `{194b48a9-...}` script output (excluded), `{56a60174-...}` library manager, `{97f48d64-...}` compiler,
  `{220493a1-...}` other code checks; clears messages before `app.build()`; **version guard** - compares the project's last-saved compiler version with the running IDE to avoid silent upgrades; builds a **copy** of the project; never reports `errors=0` after a fatal abort.
- Public MCP toolkit (zbest1000/mcptoolkit-for-codesys): CODESYS takes **30-90 s** to open; keeps one long-lived instance with a file-IPC watcher, heartbeat and a modal-dialog guard (modal dialogs freeze scripting); build messages carry severity/prefix/number/source/position ("Line 1, Column 1 (Impl)");
  "Device not installed to the system" + "Unknown type 'IoConfigTaskMap'" cascades when the device descriptor/IoStandard is missing; claims that the comm-path (gateway/target binding) **cannot be scripted** in SP22 (contradicts the docs - treat our `set_target` as risky).
- Static check I did: 80 distinct attribute names used by `me_job.py`; 6 are **not** in the public docs: `create_function`, `create_function_block`, `create_program` (fallbacks after `create_pou`), `create_symbol_config`/`is_symbol_config`, `set_communication_address` (guarded by `hasattr`). Everything else (`build`, `get_message_*`, `create_task`, `textual_declaration/implementation`, `set_simulation_mode`, `login`, `perform_network_scan`...) is documented. (`c_apicheck.py`.)
  Minor: code is built as `prefix + str(number)` = `C32`, ME shows `C0032` (the public script does the same); `part_of()` keys on English/Russian words only.

UNVERIFIED: that Machine Expert's own executables accept `--runscript/--noUI/--profile` (not documented by Schneider in anything I could reach; the public toolkit author lists it as an open question); that ME's licence allows `--noUI`; that message `prefix/number` behave as in CODESYS;
that a second ME process can open a project the engineer has open; startup time on ME (ours says "минуты").

### 4.3 What is missing for a closed loop build -> compile -> read messages -> fix -> repeat

1. **Any evidence the shell takes a script at all** (4.2). Step 0 = the bisect protocol; if `--runscript` is refused, fall back to the assisted loop (engineer pastes messages) and/or our own host like `lbh.cs` - but that bypasses the Schneider shell/licence and cannot load Schneider device modules (handoff 6), so it is not a compile path.
2. **Compile a copy, not the deliverable:** ME writes `.OpcUASymbolConf.map`/`.settings.conf` beside the project (docs claim) - compile `_work/compile/<name>.project` so `plc/` stays clean (public script does the same).
3. **Version guard** (project profile vs installed ME) and a recorded environment line (ME version, device description versions, project hash) in every report.
4. **Structured report file** `_work/reports/compile-<ts>.json` + a stage gate: `plc_stage_update(build, done)` accepted only if the newest report has 0 errors and is newer than the `.project`. Today `build: done` is a sentence.
5. **Error routing:** (a) message with object in the ST folder -> agent/generator fixes (`file:line` exists); (b) IO config / device / library / profile messages (no `.st` object) -> deterministic fixes (`plc_io_map`, template swap) or ask the engineer; (c) project/version -> human. Today `_build_text` prints all alike.
6. **Loop control in code**, not prose: max iterations, "same message twice" detector (SKILL has it only as advice, `SKILL.md:86-88`), cost cap.
7. **Persistent session** (watcher + heartbeat + prompt handling) so each iteration is seconds, not 30-90 s+ of cold start (`mehost.run` starts ME per job and exits it).
8. Prompt handling: our `system.prompt_handling = PromptHandling.None` (obsolete enum) vs public `ForwardSimplePrompts|LogSimplePrompts|LogMessageKeys`; check on first run.
9. Pre-validators (section 3) so that ME is called only on code that passed L0-L5.
10. `me_online` simulation read of all HMI tags (SKILL stage 4) is the only "does it run" check; needs a run to know it works for M241 simulation.

---------------------------------------------------------------------------------------------------------------------------------

## 5. Connecting to controllers (what Machine Expert's "connect" does and what we can do)

What ME does when you connect (CODESYS model, PUBLIC docs above): local gateway -> network scan -> pick node -> login (user/password where set) with project comparison -> optional download/online change -> monitor/write/force
any variable, start/stop/reset, diagnostics. It uses the proprietary CODESYS online protocol; no open client exists (HYP: PLCHandler SDK is a CODESYS product, licence UNVERIFIED).

### 5.1 Options

| Option | Reads what | Prerequisites on the controller | Security | Pros | Cons |
|---|---|---|---|---|---|
| **A. OPC UA client** (`asyncua`, LGPL) to the M241/M251/M262 server | variables **published in the OPC UA Symbol Configuration** (our `plc_project_build` fills `SelectedVariables` for plain-type vars of the lists in `project.json`, `meproject.py:622-644`); read, write (rights = 8 default), subscribe | Ethernet + OPC UA server enabled in the project's device (Ethernet_1 -> OPC UA Server) and a firmware that has it (`plclink.py:22-27` says "нужна прошивка с сервером OPC UA"); at least one customer line ("Родина", cMT, 588 OPC UA tags per `NEW_PROJECT.md:59-60`) runs its panel over it: **node ids `ns=2;i=N`, N = CRC-32 of "List.Var" without final inversion, checked on 582 tags** (`plcme.py:576-579`, `TOOL.md:105-108`) | OPC UA security policies/users: configuration depends on the project/firmware (UNVERIFIED which M241 firmware offers which policy); prefer signed+encrypted + user auth for anything but read-only on an isolated LAN | no ME, no licence, no download; node ids computable offline; works through the Python of the tool venv | only published symbols; FB instances/structs not in tags (`TOOL.md:103-104`); needs the server enabled |
| **B. Modbus TCP** (stdlib client ~100 lines, or `pymodbus`) | the `%M` area as holding registers (`%MW n` = register n; `%MX` per the byte/word rule), per `TOOL.md:81-91`; mapping from `AT %M...` is already computed by `plcst.modbus()` | Modbus TCP server (port 502) per `plclink.py:22-27`; unit id and which function codes - UNVERIFIED | none in protocol (no auth/encryption) -> read-only, isolated network only | trivial, dependency-free, needs no project symbols beyond addresses | only what the program placed at `%M`; no names/types from the controller; writes are dangerous |
| **C. ME scripting online** (`me_online`/`me_scan`) | any variable of the opened project, force, download, start/stop | licensed ME on the PC, gateway, valid project file, credentials | ME's own (user mgmt), our `confirm` gate | everything ME can do | heavy (30-90 s+ per call), licence seat, UI/prompt fragility, never run |
| **D. Network discovery** | find controllers/firmware | A/B ports; ME gateway for `me_scan` | scanning an industrial LAN needs consent | `me_scan` exists (via ME); a stdlib probe could ask TCP 502 / OPC UA `GetEndpoints` / Modbus FC 43 device-id (UNVERIFIED that M241 answers FC 43) | no code yet |

### 5.2 What the codebase has today (FACT)

`plc_io` (channels of the **file**, not live), `plc_io_map`, `plc_tag_check` (panel tags vs program, node-id check), `plc_hmi_tags` (names/types/node_m241), `plc_addresses`/`plcst.modbus()` (address->register), OPC UA list writer `set_opcua`
(fills an object that must already be in the template), `me_scan`/`me_online` (ME only, untested). **No** pymodbus/asyncua/socket client anywhere in the repo (grep). TASKS ОПО-11.3 allows reads freely and requires `confirm` for changes.
Per-tool Python venv from `requirements.txt` via `uv` is supported by the synchronizer (`hubsync.py:80-98`); the access guard has path rules (`mcpwrap.py` PATH_KEYS) but **no rule for network targets (IP/host:port)** - a PLC-network allow-list must be added next to "Папки/Команды/Интернет" (cross-area, SYSAI access doc).

### 5.3 Recommendation

Minimal first step: **new hub tool `plc-live`, read-only, OPC UA first**:
`plc_scan {hosts|cidr}` -> which hosts answer 4840/502 + endpoint/security info (no writes), `plc_read {host, names|from_project}` -> compact table + CSV snapshot into `_work/live/`, `plc_watch {names, seconds, period}` -> change summary (edges, min/max) for logic debugging.
Names come from the built `.project` (`plc_hmi_tags`/`p.opcua()`), node ids from `plcme.m241_node_id` (no browsing). No write tool in v1.
Staged path: (1) read-only A, then B for Modbus-only lines; (2) log-to-file monitoring + diff against expected behaviour of the generated types; (3) writes only to a whitelist of test variables, only on a simulator/bench, `confirm` + audit log, never outputs/forcing; (4) download/start/stop stay in `me_online`, engineer-approved.
Design consequence for generators: publish a flat "observability" block (states, alarm words) in the GVL because FB internals are invisible to OPC UA/Modbus.

---------------------------------------------------------------------------------------------------------------------------------

## 6. The "Project folder" mess

### 6.1 What the screenshot shows (`91f83b4b...png`, 24 visible rows, `...\SYSAI Проекты\PLC test\plc\`)

9 `.st` (GVL_IO 42 KB, FB_Mech 5.7, GVL_Main 4.4, FB_Treater 2.3, FB_Diverter 2.0, DUT 1.8, FB_Bin 1.1, FB_External 1.1, GVL_Routes 0.9, + FB_Aspiration), 7 `.py` generators/extractors (gen_mech 16 KB, gen_io 9.2, analysis_routes 8.6, gen_routes 5.9, extract_e3 5.0, create_offline_skeleton 1.6, extract_equipment), 4 `.md` notes (22 KB, incl. `00_НЕ_ДЛЯ_ЗАГРУЗКИ.md`),
`equipment_source.csv` 26 KB, `cleaning_route_matrix.json` 2.8 KB, `техсхема_просмотр.png` 697 KB. **No `.project` in the visible part** (list is cut at `GVL_Routes.st`; `Line.project`/`Main.st`/`project.json` would sort below).
The sorting is `localeCompare('ru')` (Cyrillic first), i.e. this is the window's own list.

### 6.2 What decides the layout (FACT)

| Decider | Where | Effect |
|---|---|---|
| Folder creation | `lib/projects.js:13` (`PARTS`), `:147-153` | only `materials plc hmi docs` + `project.md`; no scratch area |
| Skill text | `plc-project/SKILL.md:28` ("plc/ program: *.st, project.json, Line.project, plc.xml"), `:67` ("Пиши в plc/"), `:81`, `:90` (`out_file: plc/<имя>.project`), `:97`/`:100` (`hmi/tags.csv`, `hmi/pictures/`); `project-docs/SKILL.md:32,34` (`docs/img`, Markdown in `docs/`); `plc-kit/TOOL.md:148-150`; `machine-expert/TOOL.md:65-66`; `pic-index/TOOL.md:87` | `plc/` is both source folder and deliverable folder; the LLM has no rule for scratch |
| Prompts | `lib/projects.js:218-238` ("напиши программу ПЛК на ST в plc ... собери plc.xml" - stale text, `prompts.build` is not used by the renderer) | same |
| Listing | `renderer/js/projects.js:158-166`: `for (f of [...info.plc, ...info.hmi, ...info.docs]) list.append(row)` - every file of those 3 folders, flat, with relative names; `_walk` skips only names starting with `.` or `~$` (`lib/projects.js:110`) | this is the screenshot |
| Why scripts exist | cwd = home (`main.js:339`) + `terminal`/`execute_code` allowed + no generator tools | the LLM writes its own generators/extractors and puts them where the skill points |
| ST-per-object format | `plcbuild.load_dir`/`split_source` (`plcbuild.py:36-115`): one file per POU/GVL/DUT is the **input format** of `plc_project_build` | the 9+ `.st` fragments are legitimate intermediates, but they belong in a work folder |
| Tool outputs | all explicit params; only `pipeline.json` implicit | tools are not the source of clutter except via ME's `.map/.conf` beside the compiled file (docs claim) |

Test that pins the current layout: `agents/desktop/test/unit.test.js:708` (`readdirSync(...) == ['docs','hmi','materials','plc','project.md']`) and `:746` (prompt text).

### 6.3 Proposed canonical layout

```
<проект>/
  materials/      INPUT. Engineer's files (UI copies them). Agent reads only.
  plc/            DELIVERABLE. Only <Имя>.project  (1..N)            <- what the engineer opens
  hmi/            DELIVERABLE. Only <Имя>.cmtp / .emtp  (1..N)
  docs/           DELIVERABLE. Only *.docx
  _work/          SCRATCH. Collapsed in the window, safe to delete (everything is reproducible)
    st/           project.json + *.st  (input of plc_project_build)
    spec/         project.spec.json, signals.csv, routes.json  (machine-readable spec, see 7)
    gen/          generators (only if a project needs its own), generation logs
    reports/      check / io-map / build / compile reports (json + 10-line md); stage gates read these
    compile/      copy of the .project that ME compiles + ME side files (.map/.conf)
    hmi/          tags.csv, pictures/, renders/
    docs/         md sources, img/
    notes/        the agent's own notes (instead of loose .md files)
    live/         controller read snapshots, audit.log
  project.md  style.md  pipeline.json     (memory/state; stay in the root, the window shows them on separate rows)
```

### 6.4 Exact changes

1. `agents/desktop/lib/projects.js`: `PARTS` += `_work` (create `_work/st|spec|reports|compile|hmi|docs|notes|live`); `info()` returns `work` (count/size only - do not list thousands of rows, do not walk it on the 4 s poll), `stray` (files in `plc/hmi/docs` that are not deliverable extensions: `.project .projectarchive .cmtp .emtp .docx`); `prompts()` text: "рабочие файлы - только в _work/", drop the stale `plc.xml`/"в plc" wording.
2. `renderer/js/projects.js:158-166`: replace the flat list by (a) **Результат** group: deliverables only (1..N `.project`, `.cmtp`, `.docx`), (b) a badge "лишнее в plc/: N" with a button "Убрать в _work" (move, never delete - matches the file's own header comment), (c) a collapsed "_work (N файлов, X МБ)" row that opens the folder.
3. `hub/seed/plc-project/SKILL.md`: folder block (`:24-34`) and stages 3-6 (`:67,79-83,90,96-100,113`): ST source -> `_work/st` (`src_dir`), `out_file: plc/<имя>.project`, `plc_hmi_tags out_file: _work/hmi/tags.csv`, pictures -> `_work/hmi/pictures`, notes -> `_work/notes`, new rule in "Ошибки": "никаких скриптов, CSV/JSON/MD и промежуточных `.st` в plc/ hmi/ docs/"; scripts only in `_work/gen`.
4. `hub/seed/project-docs/SKILL.md:10,32,34,45`: md sources and `docs/img` -> `_work/docs`; `.docx` stays in `docs/`.
5. `hub/seed/plc-kit/TOOL.md:125-173` and the examples in `server.py` tool descriptions; `hub/seed/machine-expert/TOOL.md:62-70`; `hub/seed/pic-index/TOOL.md:87`: same paths.
6. Deterministic nudges: `plc_stage_update` on `done` returns a warning listing stray files in deliverable folders; ME compile uses `_work/compile` copy; `plc_project_build` writes its report to `_work/reports/` (derive from `out_file`'s project root).
7. Tests/docs to update with it: `unit.test.js:708,746`, `docs/opo/NEW_PROJECT.md:34`, `tests/test_hub_seed_plc.py` only if stage list changes (it should not).
8. Best fix of the root cause: ship generators as tools (section 7) so the LLM stops authoring scratch scripts.

---------------------------------------------------------------------------------------------------------------------------------

## 7. Target loop and what is missing (build order)

### 7.1 The loop

```
documents (ТЗ, E3, tables)
   |  LLM (once, with citations: file/page) + engineer answers
   v
project.spec.json  (compact, machine-readable)  <-- the only thing the LLM authors at scale
   |  V0 spec schema + completeness (signals on scheme == signals in spec; each mechanism has all signals its type requires)
   |  V1 IO reconciliation vs template channels (plc_io): type/direction/count/duplicates/orphans
   v
deterministic generators (tested, versioned in the hub, no LLM):
   gen_io (typed IO GVL + mapping plan) | gen_mech (instances of *donor FB classes*) | gen_routes (route matrix -> ST) | gen_alarms | gen_tasks (project.json) | gen_hmi_tags (from FB interfaces)
   |  L0-L5 validators on the ST folder (parse, symbols, types, library signatures, project rules)
   v
plc_project_build (template + ST folder -> .project) + IO binding report + removed/unknown-kind report
   |  Wine bench open (dev CI) ; copy to _work/compile
   v
PC: me_build (persistent session) -> structured report -> classify (ST | IO/device/library | project/version)
   |  ST: regenerate / patch spec / patch custom ST fragment ; IO: plc_io_map or template ; else: ask engineer
   v  loop <= N, "same message twice" stops, report archived, stage gate reads the report
delivered: plc/<Имя>.project, panel, docs ; _work/ collapsed
```

Spec sketch (illustrative, ~10-20 KB for a whole line):

```json
{"project": {"name": "...", "controller": "TM241CE40R", "panel": "cMT3072X", "link": "opcua", "template": "materials/Родина.project"},
 "mechanisms": [{"id": "N1", "type": "noria", "title": "Нория 1",
    "in":  {"speed": "DI1.I0", "belt": "DI1.I1", "local_start": "DI1.I2", "vfd_ready": "DI1.I3"},
    "out": {"run": "DO1.Q0", "siren": "DO1.Q1"},
    "params": {"start_delay_s": 5, "speed_timeout_s": 10},
    "interlocks": {"requires_running": ["C1"], "stops": ["S2"]}, "src": "Э3 лист 7; ТЗ п.4.2"}],
 "routes": [{"id": "R1", "title": "...", "chain": ["N1", "C1", "S1"]}],
 "alarms": [{"id": 12, "text": "...", "cond": "N1.speed_fault", "prio": 2}]}
```

Why this shape (FACT/HYP): TASKS already plans it - ОПО-10.1 `project_model`, ОПО-10.2 `st_generate` "по шаблонам, без нейронок", ОПО-9.4 типовые объекты, ОПО-10.3 logic by strong model only where needed, ОПО-12.1-12.3 `st_lint`/`st_compile`/`logic_test`
(`docs/opo/TASKS.md:448-468,521-533`) - none exists in plc-kit's 17 tools. **Donor FB classes** (the customers' own production FBs, exported with `plc_export_st` from projects that compile in ME) are known-good ST: instantiate them instead of inventing
`FB_Mech/FB_Bin/...` per project (HYP: removes most R1/R2 and a large part of the output tokens). LLM-authored one-off generators (the screenshot) re-pay cost and risk on every project; moving them into tested tools is the cost lever and the clutter fix.

### 7.2 Missing pieces in build order (effort: S <= 1 day, M 2-4 days, L 1-2 weeks; estimates)

| # | Piece | Effort | Why now |
|---|---|---|---|
| 0 | **First real ME run** (bisect protocol 2.5; collect messages of `PLC test`; library/device repository listing) | S + engineer 1 h | turns hypotheses into facts; decides everything below |
| 1 | `_work/` layout + window grouping + skill/prompt text + stage gate by report file (section 6.4) | S-M | fixes owner complaint 4 immediately; no risk |
| 2 | plc-kit quick wins: IO-binding report in `plc_project_build`, `unbind_io` for templates, print removed/unknown kinds, `X_TO_Y` whitelist, regex hardening (C-style ops, `END_*`/`METHOD`, unclosed comment/string, time literal, keywords), task kind/priority/interval, M241 byte-bit rule, errors instead of warnings where certain | S-M | hits R4/R1/R5/R7/R12; measurable with `c_mutate.py` (target: <= 3 of 30 silent for syntax-class) |
| 3 | `plc_libraries` (template reference list) + PC harvest job for library declarations -> catalog JSON | M | R3, kills false positives, feeds L3 |
| 4 | Spec schema + `plc_spec_check` + IO reconciliation | M | the contract between LLM and code |
| 5 | Generators `gen_io`, `gen_mech` (donor FB library), `gen_routes`, `gen_tasks`, `gen_hmi_tags`; type library from donors (ОПО-9.4/10.2) | L | cost and quality lever |
| 6 | L0 real parser (own, or vendored LGPL after legal check) + L1 symbols/params/recursion | M | closes syntax class and half of the semantic class |
| 7 | ME compile loop v1: structured report, copy-compile, version guard, error classes, loop control; **assisted mode first** (parse pasted messages) | M | needs step 0 evidence |
| 8 | ME persistent session (watcher/heartbeat/prompt handling) | M | iteration time |
| 9 | `plc-live` read-only (OPC UA, Modbus) + PLC-network access rule | M | owner request 3b, low risk |
| 10 | L2 type checker for the generated subset, or RuSTy evaluation with `-i` stubs | L | last 11-12 of 30 faults |
| 11 | Logic tests on simulation (`logic_test`, ОПО-12.3), template factory via ME scripting, writes with whitelist | L | later |

Acceptance gates (suggested): `compile.json` errors=0 newer than the `.project`; IO report clean; spec completeness 100 %; each generator has golden-file tests on synthetic fixtures.

---------------------------------------------------------------------------------------------------------------------------------

## 8. Test run (task 8)

Command requested: `cd /workspace/SYSAI && python -m pytest -q tests/test_hub_seed_plc_project.py tests/test_hub_seed_plc.py`.
The system Python has **no pytest and none of the app dependencies** (and `conftest.py` imports `app`: fastapi/sqlalchemy/httpx/telethon...), so the literal command cannot run. I ran the same two files in an ephemeral `uvx` environment with a minimal subset of
`requirements.txt` (pytest 8.3.5, sqlalchemy 2.0.40, httpx 0.28.1, fastapi 0.115.12, jinja2, python-multipart, itsdangerous, telethon 1.45.0, cryptography, PyYAML; 31 packages, small download; no numpy/onnxruntime/weasyprint/psycopg/pypdf/segno),
with `-p no:cacheprovider` and `PYTHONDONTWRITEBYTECODE=1`:

`24 passed in 14.96s` (11 in `test_hub_seed_plc_project.py`, 13 in `test_hub_seed_plc.py`); log `/tmp/research/c_pytest.log`. Nothing was written into the repo (`find -newer` check; only `.git` dir mtime changed, not by me).

---------------------------------------------------------------------------------------------------------------------------------

## 9. Cross-area notes

- Token audit: PLC tool results are the large items (36.7k tokens for 336 tags in one result; the LLM re-emits `add_tags`). `plc_check` can emit 2000 issues, `plc_io` 3000 channels. Static PLC prompt ~10k tokens only.
- Desktop UX: `projects:info` walks the folder every 4 s; flat file list; stale `prompts.build` text; layout pinned by `unit.test.js:708`.
- Access/guard: PLC network access is not a rule category; `force: true` on build tools is available to the agent.
- Web/browser MCP and indexers: not touched here. Donor FB library and type library are the "indexer" of the PLC side (names, inputs/outputs, states) - same idea as `pic-index` for pictures.
- Handoff text mentions a CI link/commit `0a8bc79`; repo is now at 7ad99a7 - not checked.

## 10. What I could NOT verify

- Anything about ME itself: opening, compile messages, whether `LogicBuilder*.exe` take `--runscript/--noUI/--profile`, licence behaviour, prompt handling, error codes (`C0032`, `C0077`...). "Expected CODESYS verdicts" in 2.2 are from IEC/CODESYS knowledge, not run.
- The owner's actual error text; the real contents of `PLC test` (I only have the screenshot); real library/device repository paths and formats; M241 firmware/OPC UA security specifics; Modbus unit id/function codes; CODESYS online-protocol details (ports 1740-1743 UDP / 11740 TCP are from memory - only 11740 appears in the public docs as the default port).
- CODESYS help site (403/429): official docs were read through a public GitHub dump, so version may be older than 3.5.19.
- RuSTy and matiec were not run (size / time). iec-checker v0.4 and blark results are on 30 synthetic faults with adapters; not a statistical claim.
- `plc_check` behaviour on real customer ST (no customer files in the sandbox: `/workspace/medata` is gone).

## 11. Evidence files (`/tmp/research/`)

`c_mutate.py` + `c_mutate.out` + `c_mutate.json` (fault injection on `plc_check`); `c_iec_adapter.py`, `c_iec_run.py`, `c_iec_run.out`, `c_iec/` (iec-checker); `c_blark_run.py`, `c_blark_run.out`, `c_blark/` (blark);
`c_tokens.py`, `c_outsize.py` (token sizes); `c_apicheck.py` (me_job.py vs public docs dump); `c_pytest.log` (tests); `gh_search.py`, `gh_readme.py`, `dl_gh/` (public READMEs/docs read: zbest1000, codesys-wine, codesys-sync, rusty, iec-checker, blark, scriptDoc dump), `dl_iecchk/` (10 MB binary, run with `env -i`).
