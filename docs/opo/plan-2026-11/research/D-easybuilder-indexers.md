# D. EasyBuilder Pro screen pipeline, indexers, REA

Date 2026-11-12. Repo `/workspace/SYSAI` @ 7ad99a7 (read-only; nothing in it was changed). Language: English, compact.
Labels used everywhere: **FACT** (seen in code/doc/URL), **MEASURED** (my own measurement, method in Appendix A),
**ESTIMATE** (arithmetic on measured numbers with stated assumptions), **HYPOTHESIS** (not verified).
Token counts: `tiktoken` o200k_base (it installed and worked). Russian prose with Latin terms came out at 3.2 chars/token,
English at 4.1, JSON with hex ids much worse (see 2.1). The real models (gpt-6.1-terra etc.) will differ by tens of percent.

---------------------------------------------------------------------------------------------------------------------------

## 0. Summary (what matters for the plan)

1. **There is no element catalog and no image index that understands the project files today.** The agent learns "what objects
   exist, what states they have, which pictures fit" only by dumping a donor window as JSON (`cmtp_objects`, `cmtp_scene`) and
   by name-matching a folder of picture files (`pic-index`). Creation of any object = clone/import from a donor saved by the
   same EasyBuilder revision, then typed edits. There is no object factory (EASYBUILDER_FORMAT.md:151-168, NEW_PROJECT.md:119-126).
2. **pic-index is name-only** (file/folder tokens + a 27-kind equipment dictionary + state words + manual labels keyed by sha1;
   `picindex.py:1-5`, `pic-index/TOOL.md:90-97`). It never looks at pixels, and it cannot see the pictures that live inside
   `.cmtp` files: those have **no file names** (library record = `FF + GUID + u32 len + data`), `cmtp_extract_images` names them
   by byte offset and **skips BMP** (`ebpcmtp.py:171-188`, `server.py:329-338`; MEASURED: a library with one PNG and one BMP gives `images()` = [PNG] only, `d_verify_facts.py`), the picture-object *names* that EasyBuilder
   stores are parsed (`ebpcmtp.py:1204-1253`) but never shown to the agent.
3. **Token hot spots in this chain are answers, not definitions.** Tool definitions of ebpro-cmtp+pic-index = 5.9k tokens per
   request (2.2% of the 273k average; with ebpro-gui 8.5k = 3.1%). One full-window `cmtp_objects` costs 143 tokens/object on
   my synthetic mix (30-object card 4.3k; an assumed 250-object main window about 36k); unfiltered `cmtp_tags` 19.5k (588 tags);
   dumping all 88 windows of the real project (kind-weighted) would be about 0.25M tokens, ~80% of it system windows.
   A compact line format for the same window is **4.8x smaller** (measured).
   The skills tell the agent to re-read five dumps after every stage (about 42k per pass). Model of the screen stage for a
   12-mechanism project: about 0.30-0.40M tool-output tokens and about 138-150 round trips (175-220 with retries) today;
   about 32-48k tokens and about 63-87 round trips with the proposed index (ESTIMATE, section 3.7; script `d_flow_model.py`).
4. **Design (section 3):** one neutral `hmi-index` server with 5 compact tools (`hmi_element_find`, `hmi_image_find`,
   `hmi_recipe`, `hmi_bind_check`, `hmi_index`), SQLite + FTS5, three layers: (a) element catalog built from donors/EasyBuilder
   kit projects (empirical) + a curated capability table generated from `ebpedit.py` + later the binary-derived class table;
   (b) image library keyed by content sha256 with labels from names, then from **usage inside projects**, then **one-time cached
   vision labelling in 24-thumbnail contact sheets** (about 100 tokens/image, 1,000 images about 0.1M tokens, cents); (c) tag/IO
   bindings joining `.cmtp` addresses, PLC signals and I/O. Embeddings are not needed in v1.
5. **REA** (MIT, Rust, 109 stars, one maintainer, 5 releases in 6 days in Oct 2026) is a PE/ELF triage framework with a custom
   pseudo-C decompiler and a 31-tool MCP server. It is cheap to try for a first pass on `EasyBuilder Pro.exe` (imports, strings,
   packer/entropy, function list) but it has no PE-resource parsing, no MFC/C++ class recovery and no published accuracy
   evidence; **Ghidra headless is the safer main tool** for the one job that matters (the `Serialize` of `CPartObject`, one
   class for all widget kinds). Details and workflow in section 4. I could not run REA or touch the EasyBuilder binary.
6. **From-scratch screens without a donor are blocked by four unknowns**: per-kind property blocks (typed parse), the
   device/system settings tail (PLC endpoint, Modbus, passwords, fonts), Modbus/REAL address encodings, and EasyBuilder's own
   shape library (GUID only). Pragmatic route: a **seed pack of tiny EasyBuilder-saved mini-donors** (one per element kind,
   panel, protocol), indexed as templates; binary work removes the dependency later.
7. **Cross-area finding (verify with the owner):** the default engineer access profile does not list `ebpro-cmtp` or
   `ebpro-gui` (`app/net/access.py:81`, `:43`); a hub MCP server is started for Hermes only if it is in `rules["mcp"]`
   (`access.py:532`). If the owner's profile lacks `ebpro-cmtp: *`, the agent has no screen tools at all.

---------------------------------------------------------------------------------------------------------------------------

## 1. The screen pipeline as it is today (FACT unless marked)

### 1.1 Components

| Component | Size on disk | Tools | Role |
|---|---|---|---|
| `hub/seed/ebpro-cmtp` 1.6.0 | TOOL.md 34,138 B (22.3k chars, 7,011 tok); server.py 49 KB; ebpcmtp.py 73 KB; ebpedit.py 44 KB; ebprender.py 18 KB | 14: cmtp_info, labels, extract_images, unpack, pack, relabel, windows, objects, tags, scene, links, check, render, edit | parse/render/check/edit `.cmtp/.emtp`, pure Python + `cryptography` |
| `hub/seed/pic-index` 1.0.0 | TOOL.md 10.9 KB; picindex.py 31.6 KB | 5: pics_index_update, find, list, label_update, check | name-based index of a picture folder |
| `hub/seed/ebpro-gui` 1.0.0 (Windows only) | TOOL.md 8.7 KB | 11: eb_edit, windows, shot, controls, click, drag, keys, write, wait, close, free | drives the real EasyBuilder; "not yet verified on live EasyBuilder" (`ebpro-gui/TOOL.md:17`) |
| `hub/seed/doc-kit` 1.0.0 | TOOL.md 12.8 KB | 4 | consumer of `cmtp_render` PNGs in the manual |
| Skills `ebpro-project`, `-donor`, `-mnemoscheme`, `-team`, `-easybuilder`, `plc-project` (stage 5 `hmi`) | 8.8 / 6.7 / 7.9 / 6.6 / 7.6 / 18.3 KB | - | the procedure the agent follows |
| `scripts/ebpro_wine.sh` | - | setup/start/open/shot/stop | opens a built project in the real EasyBuilder under Wine |

The brief's "about 19 tools" = 14 (ebpro-cmtp) + 5 (pic-index). With ebpro-gui 30, with doc-kit 34.

### 1.2 Flow (plc-project SKILL.md:96-111, ebpro-project, ebpro-mnemoscheme, ebpro-donor)

```
ТЗ/схемы -> PLC stages -> plc_hmi_tags (names/types, add_tags JSON, CSV) ----------------------------+
Storage «ОПО/Картинки» --(manual copy; "one-button sync" is listed as NOT done, NEW_PROJECT.md:85-87)--+
      -> <project>/hmi/pictures/ -> pics_index_update -> pics_list -> pics_find "<mechanism>" (states -> file)
donor/template .cmtp (same `revision`) -> cmtp_info/windows/links/tags/objects/scene/render (JSON dumps read by the model)
      -> cmtp_edit [add_tags, clone_window(map_tags,map_texts), clone_object(objects[],map_tags,link_to), import_window,
         import_objects, picture_states, bind_tag, tag, text, move, resize, style, link, delete_*] -> out.cmtp (lock file)
      -> cmtp_check (rules) + cmtp_render PNG (+ vision) -> [Windows: ebpro-gui] / [Linux: scripts/ebpro_wine.sh open]
      -> human: Compile, Download to the panel, OPC UA / Modbus device settings
```
The window canvas of the desktop agent calls `cmtp_windows/cmtp_scene/cmtp_edit` directly (agents/desktop/lib/canvas.js:595-606,
451), outside the model: zero token cost there.

### 1.3 How the agent discovers things today

| Question | Answer today | Evidence |
|---|---|---|
| Which element kinds exist? | Not queryable. Only kinds that appear in the donor and were named by the parser: 17 type codes in `PART_KINDS` (6 of them guesses marked `?`: 0x18 MultiState, 0x1a SetWord, 0x1f ASCII, 0x24 WordLamp, 0x29 Trend, 0x40 OptionList) + shapes Line/Rect + ~10 unnamed codes shown as `part_XX` (0x1c 0x22 0x28 0x2a 0x2b 0x2f 0x30 0x32 0x48 0x4a) + "compact" widgets (opaque). | ebpcmtp.py:499-503; EASYBUILDER_FORMAT.md:95-114 |
| Properties of an element? | Only what the generic head gives: 3 device names, 3 address types, 3 address strings (slot 1 read, 2 write, 3 extra), frame, labels per state (text, font, size, x, y, colour, align), picture-group GUID or library-shape GUID, plain-box colours. **Per-kind property blocks are not decoded** (e.g. switch mode, lamp value-to-state rule, numeric format, visibility conditions). | ebpcmtp.py:896-967, 1326-1365; FORMAT.md:48-79, 151-168 |
| States of an element? | Derived: number of states of its picture group (`PicGroup<BitmapPicture>`) and number of label states. The mapping "value -> state i" is not decoded. | ebpcmtp.py:1199-1253 |
| Which pictures to use? | (1) `pics_find` by name over `hmi/pictures/` copy of the archive; (2) pictures of a donor via `cmtp_scene images_dir` (files named `<GUID>[-<colour>].png`, no semantics) | picindex.py:469-523; ebpcmtp.py:229-258 |
| Where are pictures stored? | (a) Storage «ОПО/Картинки» (server, `net_files` with sha256); (b) copy in `<project>/hmi/pictures/` + `pic-index.json`; (c) inside each `.cmtp` (library: 321 files in the 88-window project, 257 PNG / 61 BMP / 3 JPEG; 165 in the empty project = EasyBuilder's own system pictures) | NEW_PROJECT.md:97; FORMAT.md:26,31 |
| How to attach pictures to an element? | `cmtp_edit picture_states`: replaces pictures of ONE existing object; **state count must stay the same**; PNG only (<=8 MB, 1-4096 px, 32 MB per call) or an existing library GUID; kinds limited to Picture, BitLamp, MultiState?, WordLamp?, ToggleSwitch, FunctionKey (not SetBit, not Numeric*); creates a new picture group named "SYSAI picture states" | ebpedit.py:668-746, 749-782; server.py:468-486 |
| How to create a new object? | Only clone (`clone_object`/`clone_window`) or take from a donor (`import_objects`/`import_window`, donor must have the same `revision`). Objects with sub-objects, class definitions and compact "opaque" widgets are refused. | server.py:503-612; TOOL.md:229-243 |
| How to bind tags? | `bind_tag` only into a slot that **already holds an OPC UA tag** of the same type (no binding of an empty or "Local HMI" slot); `tag` op re-targets all uses in a window | ebpedit.py:266-292 (line 277) |

### 1.4 What pic-index does (picindex.py, server.py)

- **Method (FACT):** `derive()` tokenizes file/folder names (camelCase, digits, `_`/`-`), folds Cyrillic+Latin to one spelling
  (`нория`/`noria`/`noriya`), strips plural/Russian endings, and looks words up in constants `EQUIPMENT` (27 kinds),
  `STATE_WORDS` (9 groups: on, off, alarm, manual, warning, open, closed, opening, closing; state list has `unknown` as the 10th), `COLOR_STATES` (green/red/gray/yellow),
  `ORIENTATION_WORDS`. `name_0/_1/_2` are read as off/on/alarm only if two neighbours exist (`state_guess`). Sets = same
  folder + same `base`. No vision, no embeddings, nothing but the header (format, width, height) of a picture is read
  (picindex.py:1-5, 197-218, 222-287, 423-450).
- **Stores (FACT):** `pic-index.json` next to the pictures: per picture `file, sha1, bytes, mtime, format, width, height` +
  derived `equipment, state, orientation, base, words`; `labels{sha1: {equipment,state,orientation,base,note}}` made by hand
  with `pics_label_update` (survive renames, not redraws). MEASURED on a synthetic library: 58 KB for 175 pictures, 243 KB for
  733 (about 330 B/picture); index of 733 files built in 0.06 s (header + sha1 only). Reads the whole JSON and re-walks the folder on every call.
- **Answers (FACT):** `pics_find` (sets by meaning, best first, `states` state -> file, `files`, `size`, `by_orientation`,
  limit 10), `pics_list` (kinds + up to 1000 set rows), `pics_check` (incomplete sets, size mismatch inside a set, duplicates,
  unrecognised, heavy >1 MB or >1024 px; up to 2000 rows), `pics_index_update` (stats + first 50 unrecognised), `pics_label_update`.
- **Limits that matter for the owner's requirement:**
  1. No pixel knowledge. A picture with a meaningless name (`img0042.png`, a GUID) is "unrecognised"; TOOL.md tells the agent to
     look at it itself and label it (one vision call + one label call per picture).
  2. The naming convention of the real archive is an **open question** (NEW_PROJECT.md:152-153); all pic-index tests use a
     synthetic library (tests/test_hub_seed_pics.py:49-71). Real-data accuracy of the name rules is unmeasured (HYPOTHESIS: names
     in a customer archive are far messier than the test names).
  3. Pictures inside donors/templates are invisible to it (nameless GUIDs).
  4. It indexes 5 formats (PNG, BMP, JPEG, GIF, SVG) but the only attach path (`picture_states`) accepts PNG or an existing
     library GUID: indexed does not mean usable.
  5. The set model is "same folder + same base"; it knows nothing about which element a set fits (number of states, size of the
     frame, state roles).
  6. The archive itself has no semantic index: `files_find` searches name/path/description; `files_read` returns text up to 200 KB
     or a download URL; `files_write` accepts base64 up to 5 MB (app/net/mcp.py:398-403). Getting 300 pictures to the PC is one
     call per file by hand.

---------------------------------------------------------------------------------------------------------------------------

## 2. Token hot spots in the chain (MEASURED unless marked)

### 2.1 Static text (what sits in context or can be opened)

| Item | bytes | chars | tokens (o200k) | In context? |
|---|---|---|---|---|
| `tools/list` ebpro-cmtp (14 tools) | - | 16,285 | **4,601** (cmtp_edit alone 1,707; render 394; scene 302; objects 259; check 239) | yes, every request, if the server is allowed |
| `tools/list` pic-index (5) | - | 4,655 | **1,308** | yes |
| `tools/list` ebpro-gui (11) | - | 8,631 | 2,567 | yes (Windows only) |
| `tools/list` doc-kit (4) | - | 4,165 | 1,268 | yes |
| ebpro-cmtp TOOL.md | 34,138 | 22,265 | **7,011** | only if the agent opens it; ebpro-project/SKILL.md:63-64 points the agent to its «Ошибки» section on tool errors |
| pic-index / ebpro-gui / doc-kit TOOL.md | 10.9k / 8.7k / 12.8k | 7.0k / 5.6k / 7.9k | 2,236 / 1,679 / 2,504 | on demand |
| SKILL.md: ebpro-project / donor / mnemoscheme / team / easybuilder / plc-project | 8.8k / 6.7k / 7.9k / 6.6k / 7.6k / 18.3k | - | 1,756 / 1,336 / 1,217 / 1,257 / 1,445 / 3,633 | on demand |

- Raw `tools/list` JSON is what I measured; the token audit (A-token-audit.md section 2) measured the model-visible function form (name+description+inputSchema): ebpro-cmtp 3,882, ebpro-gui 2,125, and found that Hermes >= 0.22 may not send MCP definitions at all (a `tool_search` bridge of about 2.4k replaces them; HYPOTHESIS for the engineers' PCs, 5-minute check). Either way the order of magnitude is the same.
- Definitions of this chain: 5.9k (cmtp+pic) = 2.2% of the 273k average input per request; 8.5k with gui = 3.1%; x142 requests
  = 0.84M of 38.79M tokens, all cached. **The owner's hypothesis "definitions eat the context" is not supported for the screen
  chain** (other servers not measured here). A cheap cut that needs no code: `tools/list` is filtered by the access allowlist
  (agents/sync/sysai_agent/mcpwrap.py:148-171, rules.py:11-13): allowing only `cmtp_edit, cmtp_check, cmtp_render` leaves 2,340 tokens
  instead of 4,599; allowing the 5 new `hmi_*` tools would cost about 1.4k (pic-index's 5 tools = 1.3k).
- Ids are expensive: a 32-hex GUID = 20 tokens, sha256 hex = 33, an 8-char base32 id = 4. `cmtp_scene` repeats the GUID twice per
  picture state (`image`, `key`): 55 tokens per state entry. An address in `cmtp_objects` costs 72 tokens (the tag name appears
  three times: `address`, `tag`, `node`); compact form 26.

### 2.2 Dynamic answers (per-object costs measured on a synthetic project built with the test helpers' grammar: Russian labels, 40-char
OPC UA names with numeric `i:` nodes, picture groups; object counts from the real projects in EASYBUILDER_FORMAT.md)

| Call | Answer size | Notes |
|---|---|---|
| `cmtp_objects` window | **143 tok/object** on the synthetic mix (BitLamp 121, FunctionKey 82, NumericDisplay 150, Text 87, SetBit 231, ToggleSwitch 261, NumericInput 231, Rect 46) | 30-object card = 4,284. A 250-object main window (36k) is my assumption: the real projects average about 24 non-system objects per user window (2,523 - 2,012 = 511 objects in 21 windows, assuming the same system windows as the empty project, FORMAT.md:5,31-32), so a "hundreds of objects" window is the exception; the owner's mnemoschemes (20 multi-state lamps in the main window, FORMAT.md:103) are larger than a card |
| `cmtp_scene` window | **126 tok/object** (BitLamp 207: two GUIDs per state) | 30 objects = 3,765 |
| `cmtp_objects` / `cmtp_scene` of ALL 88 windows of the real project | kind-weighted with the real counts (Text 493, Picture 155, FunctionKey 862, Rect 326, Line 70, 617 others; FORMAT.md:95-97) and the measured per-kind costs: **about 0.25M** (0.23-0.29M) objects, 0.24-0.32M scene | ESTIMATE; ~80% of the objects sit in the 67 system windows (empty project: 2,012 objects, FORMAT.md:31-32); `cmtp_windows` lists them all by default (88 rows = 3,354 tok) |
| `cmtp_tags` (no filter) | 588 tags = **19,513 tok** (33/tag); real project 461 tags about 15k | skills call it unfiltered (ebpro-project SKILL.md:33-34; ebpro-mnemoscheme SKILL.md:16,31); cap 5000 rows (server.py:374) |
| `cmtp_labels` | 18 tok/row; cap **2000 rows = about 36k** (ESTIMATE from 18 tok/row measured on 163 rows) | every UTF-16 string incl. system windows (server.py:324-328) |
| `cmtp_links` | 52 tok/row; 150-250 window-changing keys = 8-13k (ESTIMATE of row count) | |
| `cmtp_check` | 47-50 tok/row, cap 3000 rows; before the font fix the customer project gave 500 false `text_fit` warnings = about 24k (FORMAT.md:146-147) | `window` filter exists but skills call it on the whole project |
| `cmtp_render` | one text line; with `show:true` returns the PNG **base64 up to 3,000,000 bytes** inside the MCP result (server.py:402-403) | Hermes attaches MCP images natively: about 1.5k tokens each, kept in history and **re-sent on every later request**, up to 4 per result (A-token-audit.md section 3, citing Hermes `vision_tools_history_budget.py`); so `show:true` is not a text-token disaster but is a permanent 1.5k per shot. The synthetic PNG was 4-11 KB, a real mnemoscheme with photos is not measured |
| `pics_list` (no filter) | 4,729 tok at 175 pictures; **17,917 at 733**; cap 1000 rows (~46k) | first call recommended by TOOL.md "Порядок" and plc-project SKILL.md:100-102 |
| `pics_find` top-10 | 337-1,014 tok | repeats every path twice (`states` and `files`) |
| `pics_check` | 1,843 at 175 pictures; 6,940 at 733; cap 2000 rows | |
| `pics_index_update` | 197-389 tok | fine |
| `cmtp_edit` | args 0.5-2k (a 3-op edit about 1k) + result 0.3-0.6k | `cmtp_edit` is the only tool that must stay |

### 2.3 Behaviour in the skills that multiplies the cost (FACT in text, effect ESTIMATED)

- ebpro-mnemoscheme/SKILL.md:31: "after every stage re-read `cmtp_scene`, `cmtp_objects`, `cmtp_links`, `cmtp_tags` and `cmtp_check`".
  One such pass on a 30-object window and a 588-tag project = 3.8k + 4.3k + 10k + 19.5k + 5k = about 42k tokens; six stages = 250k.
- ebpro-project: orientation per project is `cmtp_info, windows, links, tags` + `pics_index_update, pics_list` = 36-54k tokens before the first edit.
- Everything a tool returns stays in the history and is re-read by every later request; the number of requests, not only the size,
  is the multiplier (the average request carries 273k tokens: 38.79M/142). I cannot attribute the owner's $25 to this stage with the data I have.

### 2.4 Model of the screen stage, today (ESTIMATE; one pass, no retries; 12 mechanism windows of 30 objects, 588 tags)

| Step | Tokens | Calls |
|---|---|---|
| orientation: info 0.1k + windows 3.4k + links 7.8-13k + tags 19.5k + pics_update 0.4k + pics_list 4.7-17.9k | 36-54k (once) | 6 |
| per window: read donor card (objects 4.3k + scene 3.8k) | 8.1k | 2 |
| pics_find x3 | 2.1k | 3 |
| two edit calls (1.4k each) | 2.8k | 2 |
| verify: objects 4.3k + scene 3.8k + check(window) 0.5k + render 0.1k + vision summary 0.3k | 9.0k | 4 |
| **per window** | **about 22k** (28k if `cmtp_check` is run on the whole project) | **about 11-12** (+3-6 with retries) |
| **12 windows** | **298-397k** | about 138-150 (6 + 12 x 11-12) |

### 2.5 Cross-check with the other audits (read-only look at A-token-audit.md; not my measurements)

- Hermes spills an MCP result larger than 50,000 chars to disk and shows the model a 1,500-char preview + path; 200,000 chars per turn overall (A-token-audit.md section 3, Hermes `tools/budget_config.py`).
  My 588-tag `cmtp_tags` answer is 50,669 chars (just over the line) and a 250-object `cmtp_objects` answer is about 90 KB: both would arrive as preview + file, and the agent then needs `read_file`
  round trips to see the data, so the cap bounds a single call (about 12-25k tokens) but adds calls. Compact, paginated answers stay under the line and need no second call.
- Same audit: `plc_hmi_tags` for 588 OPC UA tags is about 65k tokens (it returns both `tags` and `add_tags`), 3x the `cmtp_tags` dump, and it is the first big dump of the screen stage
  (plc-project SKILL.md:96-99). The tag index (c) should be fed from `plc_hmi_tags(out_file=hmi/tags.csv)` and the project's tag table by code, never through the model.
- Same audit: `ebpro-cmtp` and `ebpro-gui` are not in the default access profile either (their section 1 note); the five `ebpro-*` skills are delivered to everyone and mention tools that may not exist.
- Their row-cost numbers agree with mine (cmtp_tags 32 vs my 33 tok/row; they guessed `cmtp_objects` at 110 tok/object, I measured 143 on a synthetic mix).

---------------------------------------------------------------------------------------------------------------------------

## 3. Design: element catalog, image library, tag index

### 3.1 Principles

1. Deterministic first. A model is used only for what is in pixels and only once per content hash.
2. The index answers "what can I put here and how do I do it", then returns a **recipe** (ready `cmtp_edit` ops) so the model
   does not re-derive it by reading dumps. Every answer is small, paginated, with a `why`.
3. Every fact carries a status: `observed` (seen in donors), `typed` (typed parse round-trips), `verified` (EasyBuilder opened/compiled
   it, with version), `unknown`. The agent is never told something is possible that no tool can do.
4. Content-hash keys (sha256) for images and labels, so one label serves the archive, local folders and every project library.
5. Target-neutral semantic layer (roles, states, signals) + target adapter (EasyBuilder now; ONI/Haiwell later, section 6).
6. Customer data (pictures, tags, projects) stays in private stores; the repo is public (FORMAT.md:8-9).

### 3.2 Data model (SQLite, FTS5)

Two databases: shared catalog `%SYSAI_HOME%\cache\hmi\catalog.sqlite` (element types, templates, image labels by sha256, synced
from a server snapshot + local additions) and per-project `<project>\.sysai\index\hmi-index.sqlite` (sources, image locations, bindings; `.sysai/index/` is the folder E-desktop-agent.md section 4.3 proposes for indexers, with sources listed in `.sysai/sources.json`: `library` links and inputs of role `picture-library`, and `index.state` shown as progress in the window).
Thumbnails: `...\thumbs\<sha[:2]>\<sha>.png`.

```sql
-- provenance and adapter boundary
CREATE TABLE target(id TEXT PRIMARY KEY, title TEXT, adapter TEXT, catalog_ver TEXT);              -- 'ebpro' | later 'haiwell','oni'
CREATE TABLE source(id INTEGER PRIMARY KEY, kind TEXT,        -- archive | local_folder | project_lib | donor | kit | eb_install | seed_pack
  uri TEXT, sha256 TEXT, target TEXT, build TEXT, revision INT, revision_min INT, trust TEXT,         -- client | vendor | generated
  added_at TEXT, last_scan TEXT);

-- (a) element catalog
CREATE TABLE element_type(id TEXT PRIMARY KEY, target TEXT, kind TEXT, type_code INT, class_name TEXT,  -- class_name filled from the binary later
  title TEXT, role TEXT,              -- indicator | command | input | display | nav | static | container
  state_model TEXT,                   -- binary | multistate | analog | command | none
  n_states_min INT, n_states_max INT,
  slots_json TEXT,   -- {"pic":[{"i":0,"role":"off"},{"i":1,"role":"on"}],"addr":[{"slot":1,"role":"read","types":["BOOL"]},
                     --  {"slot":2,"role":"write","types":["BOOL"]}],"label":[{"i":0},{"i":1}],"shape":true,"state_map":"unknown"}
  props_json TEXT,   -- configurable properties, known AND unknown: [{"name":"label","per_state":true,"edit":"text","status":"verified"},
                     --  {"name":"fill","edit":"style","status":"observed"},{"name":"switch_mode","type":"enum","edit":null,"status":"unknown"}]
  size_json TEXT,    -- {"typ":"80x40","min":"20x20","max":"400x400"}
  layers_json TEXT, ops_json TEXT,    -- {"create":["import_objects","clone_object"],"edit":["picture_states","bind_tag","text","resize","move"]}
  limits_json TEXT,                   -- e.g. {"states_fixed_by_template":true,"pic":"png<=8MB,<=4096px","bind_tag":"slot must hold OPC UA tag"}
  status TEXT,                        -- observed | typed | verified | gui_only | unknown
  seen_count INT, source_count INT, verified_in TEXT, evidence_json TEXT);
CREATE TABLE element_template(id TEXT PRIMARY KEY, element_type_id TEXT, source_id INT, window_no INT, window_name TEXT,
  object_idx INT, bbox TEXT, n_states INT, labels_json TEXT, group_name TEXT, addr_json TEXT, ops_ok_json TEXT, uses INT);
CREATE TABLE mech_template(id TEXT PRIMARY KEY, name TEXT, equipment_class TEXT, source_id INT, window_no INT,
  roles_json TEXT);       -- [{role:"run_lamp", object_idx:8, element:"lamp2", tag_slot:1}, {role:"start_stop", object_idx:12, element:"tgl2", ...}]

-- (b) image library (content-addressed)
CREATE TABLE image(id TEXT PRIMARY KEY, sha256 BLOB UNIQUE, bytes INT, w INT, h INT, fmt TEXT, alpha INT,
  mean_rgb INT, hue TEXT, phash INT,                 -- deterministic pixel features (own PNG/BMP decoders, ebprender.py:153-256)
  usable_ebpro INT, convert TEXT,                    -- png<=8MB<=4096px; 'bmp>png' via bmp_png(); transparent_hint
  equipment TEXT, class TEXT, state TEXT, orientation TEXT, variant TEXT,
  label_src TEXT, conf REAL, label_ver TEXT, vision_id INT);      -- name | usage | vision | manual | dedupe
CREATE TABLE image_tag(image_id TEXT, tag TEXT, src TEXT, conf REAL);    -- free tags: green, round, 3d, toggle, horizontal ... (name | usage | vision)
CREATE TABLE image_loc(image_id TEXT, source_id INT, ref TEXT, in_archive INT, PRIMARY KEY(image_id, source_id, ref));
CREATE TABLE image_set(id TEXT PRIMARY KEY, name TEXT, equipment TEXT, size TEXT, complete INT, origin TEXT, conf REAL);
CREATE TABLE image_set_member(set_id TEXT, image_id TEXT, state TEXT, state_i INT, orientation TEXT);
CREATE TABLE image_use(image_id TEXT, source_id INT, window_no INT, object_idx INT, element_kind TEXT, state_i INT,
  group_name TEXT, window_name TEXT, labels_json TEXT, tags_json TEXT);       -- evidence: where this picture is used and with what
CREATE TABLE vision_label(id INTEGER PRIMARY KEY, sha256 BLOB, model TEXT, prompt_ver TEXT, cell_px INT, raw_json TEXT, at TEXT);
CREATE VIRTUAL TABLE image_fts USING fts5(text, tokenize='unicode61 remove_diacritics 2');   -- folded RU/EN words (picindex.key), group names, labels, notes

-- (c) tag / IO index
CREATE TABLE plc_signal(id INTEGER PRIMARY KEY, project TEXT, name TEXT, type TEXT, dir TEXT,            -- in | out | internal
  io_module TEXT, io_channel TEXT, io_terminal TEXT, equipment TEXT, role TEXT,                          -- run | fault | cmd_start | sp | pv ...
  node TEXT, modbus TEXT, conf REAL, src TEXT);                                                          -- from plc_hmi_tags / plc_io / opo_tags
CREATE TABLE equipment(id TEXT PRIMARY KEY, project TEXT, name TEXT, class TEXT, prefix TEXT);
CREATE TABLE binding(project TEXT, window_no INT, object_idx INT, slot INT, role TEXT, tag TEXT, atype TEXT, device TEXT,
  node TEXT, signal_id INT, element_type_id TEXT, status TEXT);   -- ok | missing_in_panel | missing_in_plc | type_mismatch | dir_mismatch | unbound
CREATE VIRTUAL TABLE tag_fts USING fts5(text);
```

Capability seed for `ops_json` (FACT, generated from ebpedit/server code; the proposal is to generate it, not hand-copy, so it cannot drift):

| kind | create | text | move/resize | style | bind_tag/tag | picture_states | link |
|---|---|---|---|---|---|---|---|
| Text | clone/import | yes (frame widens) | yes | text_color, font_size | - | - | - |
| Rect / Line | clone/import | - | yes | fill, frame, width | - | - | - |
| Window (index 0) | no | - | - | opaque fill only | - | - | - |
| Picture, BitLamp, MultiState?, ToggleSwitch, WordLamp? | clone/import | yes | yes | text only | yes (slot must hold OPC UA) | **yes** (state count fixed) | - |
| FunctionKey | clone/import | yes | yes | text only | - | yes | yes (window/popup keys) |
| SetBit, NumericInput/Display, ASCII | clone/import | yes | yes | text only | yes | **no** (not in the kind list) | - |
| Trend?, OptionList?, part_1c | only if class numbering checks out ("holds sub-objects") | | | | | | |
| part_XX compact (events log, EasyAccess, near trend) | **no** (`opaque_object`) | | | | | | |

### 3.3 Deterministic build, then one-time vision

Reuse what exists: `ebpcmtp.Project` (parse), `picindex.derive/_finish/sets_of/check` (names, sets, checks), `ebprender` PNG/BMP decoders
and canvas (thumbnails and contact sheets without Pillow), `plc_hmi_tags`/`plc_io`/`plc_tag_check` outputs.

1. **Sources.** Register each source with sha256 and (for projects) `version`/`revision`/`revision_min`. Kinds: Storage archive folder
   (join with server `net_files.sha256`, app/net/files.py:183), local folders (not yet uploaded), the picture library of every
   `.cmtp` (donors, templates, the working project), **EasyBuilder kit projects** (TOOL.md:15-16 mention 48 kit projects, FORMAT.md:139-140 15 kit
   templates; these are same-build, vendor-made donors for every kind they contain), seed pack (section 5.3). EasyBuilder's own shape/picture libraries inside the install (`source.kind=eb_install`) are the only way to resolve a `shape` GUID to a picture and a name; their file format is unknown to the repo, so this source stays empty until the binary inventory (4.6 A).
2. **Project pass (no model, milliseconds per window).** For every object: kind, bbox, layer, labels per state, address slots with
   `atype`, picture group (key, **name**, states, transparent colours), shape GUID. Aggregate by (kind, n_states, slot/atype pattern, size)
   -> `element_type` rows with `seen_count` (empirical layer); keep distinct representatives as `element_template` (the "how to create": window + object index
   in a named source). Write `image_use` for every picture state (group name, state index, element kind, nearby texts, tag names, window name).
3. **Image pass (no model).** sha256, header size, alpha, mean colour/hue bucket, dHash (near-duplicate clusters; PNG and BMP only: `ebprender` has PNG/BMP decoders, JPEG is drawn as a frame, so JPEG/GIF get header-only features unless Pillow is present; they are rare in project libraries, 3 JPEG of 321. The existing tools are stdlib-only by choice, not by platform limit: a hub tool may ship `requirements.txt` and gets its own uv venv, `hubsync.py:82-98`, so Pillow/numpy are possible dependencies), `usable_ebpro`
   (PNG, <=8 MB, <=4096 px; BMP -> `convert: bmp>png` with transparent colour hint). Labels from names via `picindex.derive` (`label_src=name`).
4. **Usage inference (no model).** A picture used as state 1 of a BitLamp whose tag ends `_RUN` in window "Нория 3" is `on/run` for `нория`;
   state 0 of the same group is `off/stop`; the group name ("Лампа красная") and state texts ("Пуск"/"Стоп") go to FTS. Conflicts between
   name and usage lower `conf` and queue the image for review. This is the cheap, strong signal the current tools throw away.
5. **Vision, only for what is left** (no equipment, conf < 0.7, or name/usage conflict), **keyed by (sha256, prompt_ver)** so it never repeats:
   - dedupe first (sha256, then dHash Hamming <= 4): label one representative per cluster, others inherit with `label_src=dedupe`;
   - skip obvious non-symbols (>512 px photos/backgrounds: `class=background`);
   - **contact sheets**: 24 thumbnails (fit in 160x160 cell, alpha composited on mid-gray, number burned in) per image <= 1024x1024; JSON-only
     reply `{n, equipment, state, orientation, set_hint, conf}` constrained to the dictionary enums (27 equipment kinds, 10 states); context in the prompt: folder
     name, neighbour file names, usage evidence; temperature 0;
   - model: the gateway's **vision tier** (cheap; spend shows up as «зрение» in the existing column), batch of sheets, results stored raw in `vision_label`;
   - second look at 256 px single images only for conf < 0.6; always `label_src=vision`, never overwrites `manual`.
   - cost (ESTIMATE; image-token accounting is provider-specific): about 1.1k image + 0.6k prompt + 24 x 35 output = 2.5k tokens per sheet = about 105 tokens/image;
     1,000 images about 0.1M tokens, i.e. cents on a flash-lite class model (the owner's gemini-2.5-flash-lite shows $0.06 for 629k tokens) or about $0.1 on a mini model.
     The risk is accuracy (is green "on" for this customer?), not money, hence confidences and the review queue.
6. **Sets and completeness.** `image_set` from folder+base (existing rule), from project picture groups (a group IS a set with state order),
   and from vision `set_hint`; completeness rules from `pics_check` (on without off, size mismatch).
7. **Tag index.** Per project: join `.cmtp` addresses (tag, atype, device, node) with `cmtp_tags`, PLC variables (`plc_hmi_tags`), I/O channels
   (`plc_io`, server `opo_tags`: module/slot/channel/terminal), equipment roles. Validate node = CRC rule for M241/M251
   (`zlib.crc32(name) ^ 0xFFFFFFFF`, FORMAT.md:141-144) and direction (screen writes only to PLC outputs/internal).
8. **Embeddings: not in v1.** The vocabulary is closed (about 27 equipment kinds x 10 states); open-vocabulary text-to-image search is not needed
   and a CLIP-class model is a 100-350 MB client dependency. If ever: 512-d f16 = 1 KB/image (5k images = 5 MB) in a flat file or `sqlite-vec`,
   built server-side. FTS5 availability in the engineer's bundled Python is a HYPOTHESIS to check once (`create virtual table t using fts5(x)`).

### 3.4 Query API: five tools, small answers

All answers: `limit` default 3-5, `cursor` for the next page, soft cap about 2,500 chars (~700 tokens) with `truncated`, short ids
(10-char base32 of the content hash; 50 bits, uniqueness checked on insert), a `why` list of at most three short strings, status flags.

| Tool | Input (main) | Output |
|---|---|---|
| `hmi_element_find` | `need` (free text RU/EN), `kind?`, `states?`, `limit` | matching element types: id, kind, model, states, slots (picture/address/label), size, `how` (create + edit ops), `ok` (verification status), `why`; `detail=id` gives the full profile |
| `hmi_image_find` | `for` (element id) or `equipment`, `state?`, `size?`, `source?` (archive/local/project/any), `limit` | image **sets that fit the element's slots**: state -> image id, `fit` (exact / resize / scale), complete, source (archive / local-only / project), `by` (name/usage/vision/manual), conf, `why`; `sheet=true` writes a small contact-sheet PNG and returns the path for `vision_analyze` |
| `hmi_recipe` | `element` or `template`, `set`, `window`, `at`, `tags{slot:name}`, `text`, `apply?` | a validated list of `cmtp_edit` ops with `$new` placeholders, `checks` (types, state count, sizes, free window number, tag direction), `apply` executes it server-side (lock, one call) and returns a 2-line result |
| `hmi_bind_check` | `scope` project / window / tag, `only_problems`, `limit`, `cursor` | element-to-tag problems (missing in panel table, missing in PLC, type/direction mismatch, unbound), PLC signals with no element, counts |
| `hmi_index` | `action` status / refresh / label / review / register_source | counts by source (archive vs local-only), stale items, review queue (<=10 with a sheet path), manual label write |

Packaging: the simplest shape is to put the five tools into the existing `ebpro-cmtp` server process (shared parse cache, `hmi_recipe apply` calls `_edit` directly, no cross-tool import)
and keep target-specific code in adapter modules; a separate `hmi-index` server would have to import `ebpcmtp` from the sibling tool folder, and the hub spec has no tool-to-tool dependency
(`requires:` is a free list of strings, app/net/hub.py:198; HYPOTHESIS that it is informational only). Definitions of these five would be about 1.4k tokens (ESTIMATE from pic-index's five = 1.3k). Under the existing guard the 11 reader tools of
ebpro-cmtp can be hidden from the agent (they become internal calls of the index), leaving `cmtp_edit/check/render` (2.3k).

### 3.5 Example answers (illustrative values; counts are token costs of the exact text, MEASURED with o200k; files in `/tmp/research/d_examples/`)

**A. "binary switch with two pictures"**

```
hmi_element_find {"need":"binary switch, 2 pictures, writes BOOL"}                      -> 271 tokens
{"q":"binary switch, 2 pictures, writes BOOL","n":3,"items":[
{"id":"tgl2","kind":"ToggleSwitch","model":"binary","states":2,"slots":{"pic":["0 off","1 on"],"addr":["1 read BOOL","2 write BOOL"],"label":["0","1"]},
 "size":"80x40 (20..300)","how":"import_objects tpl:sw_toggle#3 > picture_states > bind_tag s1,s2 > text > resize",
 "ok":"opens in EB 6.10.02.421 (wine); panel behaviour not verified","why":["2 pic states + read/write BOOL","seen 41x in 3 projects"]},
{"id":"setbit","kind":"SetBit","model":"command","states":0,"slots":{"pic":[],"addr":["1 write BOOL"]},"how":"clone_object tpl:btn_set#1 > text > bind_tag s1","why":["command only, no state picture"]},
{"id":"lamp2","kind":"BitLamp","model":"binary","states":2,"slots":{"pic":["0 off","1 on"],"addr":["1 read BOOL"]},"how":"import_objects tpl:lamp2#5 > picture_states","why":["indicator only; pair with SetBit"]}],"next":null}

hmi_image_find {"for":"tgl2","equipment":"switch","size":"80x40"}                          -> 228 tokens
{"for":"tgl2","n":2,"sets":[
{"set":"s7k3q2","name":"switch_round","size":"80x40","pic":{"0":"i4h9d1x2ab","1":"i8m2c5p0qe"},"fit":"exact","complete":true,"src":"archive:ОПО/Картинки/switches","by":"name+usage","conf":0.96,
 "why":["off,on present","80x40 = frame of tpl:sw_toggle","used on ToggleSwitch in 2 projects"]},
{"set":"s2n8f0","name":"tumbler_green","size":"64x64","pic":{"0":"i1b7r4k9zc","1":"i5t0w3v6ya"},"fit":"resize 80x40 to 64x64","complete":true,"src":"local:hmi/pictures/new","by":"vision","conf":0.78,
 "why":["vision: toggle, off/on","not in archive yet"]}],"next":null}

hmi_recipe {"element":"tgl2","set":"s7k3q2","window":"Нория 11","at":[500,200],
            "tags":{"1":"GVL.NORIA_11_RUN","2":"GVL.NORIA_11_CMD"},"text":["Стоп","Пуск"]}   -> 256 tokens
{"recipe":"r01","ops":[
{"op":"import_objects","from":"tpl:sw_toggle","window":"T","objects":[3],"to":"Нория 11","dx":500,"dy":200},
{"op":"picture_states","window":"Нория 11","object":"$new","states":[{"image_file":"hmi/pictures/switches/sw_off.png"},{"image_file":"hmi/pictures/switches/sw_on.png"}]},
{"op":"bind_tag","window":"Нория 11","object":"$new","slot":1,"new":"GVL.NORIA_11_RUN"},
{"op":"bind_tag","window":"Нория 11","object":"$new","slot":2,"new":"GVL.NORIA_11_CMD"},
{"op":"text","window":"Нория 11","object":"$new","old":"OFF","new":"Стоп"},{"op":"text","window":"Нория 11","object":"$new","old":"ON","new":"Пуск"}],
"checks":["BOOL/BOOL ok","GVL.NORIA_11_CMD is a PLC output: ok","frame 80x40 = pictures 80x40"],"apply":"hmi_recipe apply=r01"}
```
End to end about 755 tokens and 3 calls (+1 to apply) instead of reading donor dumps (4.3k + 3.8k) and guessing pictures.

**B. "motor with run / fault / stop states"**

```
hmi_element_find {"need":"motor lamp stop/run/fault"}                                      -> 237 tokens
{"q":"motor lamp stop/run/fault","n":2,"items":[
{"id":"ms3","kind":"MultiState?(0x18)","model":"multistate","states":"3","slots":{"pic":["0 stop","1 run","2 fault"],"addr":["1 read INT/UINT (state no.)"]},
 "how":"import_objects tpl:mech_card#5 > picture_states(3) > bind_tag s1",
 "ok":"value>state mapping not decoded: template values kept (0,1,2); check in EB","why":["3 pic states + INT read","20x in the main window of the 88-window project"]},
{"id":"lamp2x2","kind":"BitLamp x2 (RUN, FAULT)","model":"binary+binary","how":"2x import_objects tpl:lamp2#5; fault on top of run","why":["PLC has bits RUN, FAULT, no state word"]}],
"needs":["PLC word '<mech>.STATE' (0 stop,1 run,2 fault) not in plc tags; found bits RUN, FAULT"],"next":null}

hmi_image_find {"for":"ms3","equipment":"двигатель","state":"off,on,alarm"}                  -> 157 tokens
{"for":"ms3","n":1,"sets":[{"set":"s9d2m1","name":"motor_3state","size":"96x96","pic":{"0":"i3c8n2h7ka","1":"i6p1x9q4mt","2":"i2v5b0z8wd"},
 "map":{"0":"off/stop","1":"on/run","2":"alarm/fault"},"fit":"exact","complete":true,"src":"archive:ОПО/Картинки/motors","by":"name+vision","conf":0.91,
 "why":["off,on,alarm present","same size 96x96","colours gray/green/red"]}],"next":null}

hmi_bind_check {"scope":"window","name":"Нория 11"}                                         -> 108 tokens
{"scope":"window Нория 11","elements":9,"ok":7,"issues":[{"obj":14,"slot":2,"tag":"GVL.NORIA_11_CMD","problem":"not in panel tag table","fix":"add_tags BOOL"},
{"obj":17,"slot":1,"tag":"GVL.NORIA_11_SP","problem":"screen INT, PLC REAL"}],"plc_unbound":["GVL.NORIA_11_ALM BOOL role=alarm: no element"],"next":null}
```
The "needs" line is the point: the index knows the element wants a state word and says the PLC does not provide one, so the PLC side can be changed
before screens are built (cross-area link to the ST generator).

### 3.6 Refresh triggers, storage, sizes

| Trigger | Action |
|---|---|
| file added/changed in the archive folder or a local folder (hash of listing + mtime) | image pass for new sha256 only; names re-derived for all rows (cheap, like `_finish`) |
| a `.cmtp` registered as donor/template/kit, or `cmtp_edit` wrote the working project | project pass of that file (ms per window), incremental `binding` + `image_use` |
| `picture_states` / `import_*` added library files | register new sha256 with `source=project_lib` |
| PLC tag list or I/O map changed (`plc_hmi_tags`, `plc_io`) | rebuild `plc_signal`, re-run `hmi_bind_check`, report stale screens |
| EasyBuilder build changes (version string / `revision_min`) | mark templates `needs_reverify`; donors from another build are not interchangeable (FORMAT.md:15-17; `open_donor` compares only `revision`, ebpedit.py:327) |
| dictionary or prompt version bump | rules: recompute all; vision: relabel only rows whose `prompt_ver` is older and conf < 0.8 |
| manual `hmi_index label` | `label_src=manual`, never overwritten |
| a local-only image appears in the archive (same sha256) | set `in_archive=1`, no relabel |

Sizes (ESTIMATE from the measured 330 B/picture of pic-index.json): 5,000 images = 2 MB rows + 1 MB FTS + 15 MB thumbnails (96 px, about 3 KB);
embeddings would add 5 MB. Catalog: a few hundred `element_type`/`element_template` rows. Per-object rows only for the working project (about 3k).
Build time: header+hash pass was 0.06 s for 733 files (MEASURED); pixel features need a PNG decode (HYPOTHESIS: tens of ms per image in pure Python,
so under 5 min for 5,000 images, one time, incremental afterwards).

### 3.7 Expected tokens per screen, today vs proposed (ESTIMATE from the measured unit costs above)

| | Today (2.4) | Proposed |
|---|---|---|
| orientation per project | 36-54k | about 1.1k: `hmi_index status` 0.3k + `hmi_bind_check` project summary 0.5k + catalog overview 0.3k |
| per mechanism window | about 22k (28.6k worst) | about 2.6-3.9k: recipe from a mechanism template 1.0-1.5k (10-20 ops in one answer) + apply 0.3k + `hmi_bind_check(window)` 0.4k + `cmtp_check(window)` 0.5k + render+vision 0.4k; add 0.8k if element/image lookups are done separately |
| round trips per window | 11-12 (+3-6 with retries) | 5-7 |
| 12-window project | **0.30-0.40M tokens, about 138-150 calls** (about 175-220 with retries; +0.26M tokens for six literal full re-read passes) | **about 32-48k tokens, about 63-87 calls** |
| ratio | | about 6-12x fewer output tokens (12-20x against the literal re-read skill), about 2x fewer requests (2-3.5x against retries) |
| tool definitions per request | 4.6k (cmtp) + 1.3k (pics) | about 1.4k (`hmi_*`) + 2.3k (`cmtp_edit/check/render`) = 3.7k |
| one-time vision labelling of a 1,000-picture library | by hand: 1 vision call + 1 label call per unlabelled picture | about 0.1M tokens, shared by all engineers through hash-keyed labels |

Cost driver: every round trip re-reads the whole history (average 273k tokens, 98% cached; blended $5.05/38.79M = $0.13 per million tokens on the spend screen), so both fewer requests and a smaller history
matter. I cannot convert this into dollars of the owner's $25 without the trace of a real run.

### 3.8 Interim fixes that need no new index (small, measurable; each is a separate decision for the plan)

1. Access profile: expose only `cmtp_edit, cmtp_check, cmtp_render` (+ `cmtp_windows`, `cmtp_objects` if needed): -2.3k per request, zero code (mcpwrap.py:148-171).
2. Compact default for `cmtp_objects`/`cmtp_scene`/`cmtp_links`/`cmtp_check`: one line per object, short ids, `limit/offset`, `detail=true` for today's JSON.
   MEASURED on the 30-object card: 4,284 -> 885 tokens (4.8x). Hide system windows (about 67 of 88) by default in `cmtp_windows`.
3. Skills: replace "re-read five dumps after each stage" by `cmtp_check(window)` + `cmtp_render`; ban unfiltered `cmtp_tags`/`pics_list`/`pics_check`; never `show:true`.
4. `pics_find`: drop the duplicated `files` list when `states` exists; `pics_list` default `limit=30` and require `equipment`.
5. Expose picture-group **names and state counts** per object in `cmtp_objects` (already parsed: ebpcmtp.py:1248) and make `cmtp_extract_images` write BMP and name files by GUID.
6. `open_donor`: also compare `revision_min`/build (ebpedit.py:325-330) because FORMAT.md:15-17 says objects differ between .300 and .421.

---------------------------------------------------------------------------------------------------------------------------

## 4. REA and binary-level work

### 4.1 What REA is (FACT: README via `gh api repos/mytechnotalent/rea/readme`, repo metadata, source tree; copies in `/tmp/research/d_rea_README.md`, `d_rea_PLAN.md`)

- `github.com/mytechnotalent/rea`, Rust, **MIT**, created 2024-08-02, last push 2026-10-21, 109 stars, 28 forks, 0 open issues, one contributor account
  (42 contributions), repo 54 MB (binaries committed under `releases/`: macOS ARM v1-v4, one Windows exe v1.0.0; no Linux build; GitHub releases have 0 assets).
  Releases v5.6.4 (Oct 15) -> v6.0.0 (Oct 15) -> v6.0.2 (Oct 16) -> v7.0.0 (Oct 16) -> v7.1.0 (Oct 21): five tags in six days.
- v7.1.0: one-shot `rea <file>` writes Markdown/PDF/JSON report; subcommands `decompile` (pseudo-C), `strings`, `crypto`, `rules`, `audit`, `emulate`, `unpack`, `dotnet`, `sbom`, ...;
  `rea serve` = MCP over stdio (README lists 31 tools such as `open_binary`, `list_functions`, `decompile_function`, `find_xrefs`, `search_strings`; PLAN.md says 35 after v6.0.0).
  Optional "AI analyst" loop only with an OpenRouter key; offline deterministic mode otherwise.
- Engine: `goblin` for PE/ELF/Mach-O, `iced-x86` for x86 decoding (both well-known crates), SLEIGH specs for other architectures, a custom decompiler (cfg/ir/optimize/structurer/emit, about 125 KB of source),
  emulator, rules/YARA-subset, SBOM. `pe.rs` is 18 KB: imports, exports (ordinal-only exports dropped, `pe.rs:295`), sections, mitigations, imphash, PDB/rich/TLS facts; **no PE resource parsing (dialogs, string tables) visible**.
- Style: strict conventions gate (functions <= 8 lines), tests required per file, README claims "every claim verified". No published accuracy or large-binary results; no MFC / C++ class / vtable recovery mentioned (grep of README/PLAN).

### 4.2 Fit for EasyBuilder Pro (HYPOTHESIS; I did not run REA, and the binary is not available)

- Target is a native x86 MFC (and partly Qt) application: `Reverse-engineered from EasyBuilder Pro 6.10.02.300 (x86)` (ebpcmtp.py:3), "VC++ 2015 runtime" and "32-bit Mesa for the Qt part" (ebpro_wine.sh:8-9, 12), `CRuntimeClass`/`CArchive` (FORMAT.md:50-52). .NET is unlikely; check the CLR header once (REA or `file` shows it in seconds).
- REA is useful for **first triage** (sections, entropy/packer check, imports incl. MFC ordinals, strings, function list, an initial `decompile` of a few addresses) because it installs nothing but needs building from source (Rust toolchain; no Linux prebuilt).
- It is **not the right primary tool for the key job**: recovering what `CPartObject::Serialize` reads and writes for each widget type. All widgets are ONE MFC class with a type byte (0x15-0x50, FORMAT.md:50-52,54; ebpcmtp.py:499-504); the code is a large function (or switch of helpers) full of virtual calls and inlined `CArchive` operators. That needs vtable/class data types and a mature decompiler (Ghidra), not triage-grade pseudo-C. REA's value there is a second opinion and a ready MCP interface.
- The `.cmtp` format itself needs no binary tool: AES-128-ECB with a key found without disassembly (ebpcmtp.py:21-27), the rest solved from samples. REA has nothing for MFC archives.

### 4.3 What FORMAT.md learned without REA (FACT)

Container and key; body map; windows with layers (3-4 layers in real projects); MFC archive numbering (every class and object takes a map slot; insert recomputes class references; layers take none);
widget head (type, name, 3 devices, 3 address types, 3 address strings), frame at -63 bytes before the first label, colours, labels, picture refs, FunctionKey action bytes (action/value, 6.08.02+), data-type byte of addresses
(`c1` BOOL, `c3` INT, `0b` UINT, `06` UDINT; LW 08, LB 00), OPC UA tag table in both layouts, M241/M251 node ids = CRC-32, picture library + picture objects (groups, states, transparent colour), array sub-objects,
font width calibration; verified by byte-exact round trip on all projects and by EasyBuilder 6.10.02.421 opening every edit kind under Wine (FORMAT.md:116-125, TOOL.md:24-30).

### 4.4 Unknowns that need binary-level work (or samples)

| Unknown | Source | Binary needed? |
|---|---|---|
| per-kind property blocks (switch mode, lamp value-to-state rule, numeric formats, visibility, security, ...), compact "opaque" widgets (events log, EasyAccess, trend neighbours), `0x18 0x1a 0x1c 0x1f 0x22 0x24 0x28 0x29 0x2a 0x2f 0x32 0x40 0x2b 0x30 0x48 0x4a` | FORMAT.md:99-114 | yes (one `Serialize` with a switch), or per-kind sample diffs |
| class names behind type codes (6 guesses marked `?`) | ebpcmtp.py:499-502 | yes (`CRuntimeClass` names / factory) |
| EasyBuilder library shapes (`shape` GUID: UUID v5-like, drawn by EasyBuilder, "not parsed", FORMAT.md:63-67) | | yes (library files in the install, derivation of the id) |
| the tail: system settings, device list (PLC endpoint, security), fonts, languages, users | FORMAT.md:151-158 | samples first; binary for completeness |
| Modbus devices/addresses and REAL tags (no sample has them, FORMAT.md:87-89) | | samples (need one project with a Modbus device and a REAL tag) |
| the "gap" between library and windows (16.8 KB / 8.7 KB, "not parsed", FORMAT.md:27,32) | | **HYPOTHESIS: already parsed since** - it matches the picture-object table (`picture_section`, ebpcmtp.py:1199-1253): about 90 B per two-state group gives about 186 groups for 321 files, about 95 for the empty project's 165; the doc looks stale |
| window header fields (`kind`, `sub`), group objects, `revision_min` semantics | | binary or samples |

### 4.5 Tool per target

| Target | Best tool | Why / where REA fits |
|---|---|---|
| `.cmtp` bytes | existing Python parser + golden files saved by EasyBuilder + the corpus (3 customer projects, 48 kit projects) | the format is data; round trip is the oracle (FORMAT.md:116-125); REA none |
| `EasyBuilder Pro.exe` triage (packing, imports, strings, CLR header, command-line switches, resources vs Qt `.qm`) | `rea` one-shot **or** `pefile`/`strings`/`rabin2` | minutes; REA is fine here, so is anything else |
| `CPartObject::Serialize` and `CDrawObject::Serialize` | **Ghidra headless** (Apache-2.0, mature x86/MSVC decompiler; scripted; JDK 21) as primary; radare2/rizin + r2ghidra as the lighter alternative; REA `decompile_function` as second opinion | vtables/data types and a battle-tested decompiler matter more than install size |
| PE resources (dialog captions, string tables) = names/enums of properties | `pefile` / `wrestool` / `rabin2 -R` / ResourceHacker; if the dialogs are Qt, parse the `.qm` files | REA does not parse resources |
| .NET assemblies (only if the CLR check finds any) | `ilspycmd` (ILSpy CLI) | real C# output; REA gives CIL listings only |
| dynamic questions (which files/registry/licence checks, how Download talks) | Wine (`WINEDEBUG=+relay,+file`, `winedbg`) | MFC `CArchive` stream operators are inline in the headers (HYPOTHESIS; the imports will show), so relay will not give per-field traces; do not use it for structure discovery |
| validation of any new field table | `dump(load(f)) == f` on all samples, counts == declared, EasyBuilder open under Wine, one-property-flip diffs | this is the oracle that keeps an LLM in a verified loop |

### 4.6 Practical low-cost workflow (no big runs here; each stage has a stop condition)

- **A. Inventory (1-2 hours, no LLM).** Get the installer from the owner's Drive, `innoextract` it (as ebpro_wine.sh:5-6 expects), list PE files, sizes, versions;
  `rea` or `pefile`: imports (MFC DLL dynamic or static?), entropy/packer signatures, CLR header, `strings` for `CRuntimeClass`-style names (`CBitLamp`...), command-line switches (a batch compile/convert switch would
  give headless verification), dialog resources vs `.qm`. Output: a private `eb_inventory.json` (<200 KB). **Stop if the exe is packed/protected** (then Wine+samples only).
- **B. Class table (hours).** Ghidra headless project; scripted pass: find `CRuntimeClass` records (name pointer, size, schema, factory, base) in `.data`; find `CPartObject` vtable; `Serialize` is the third virtual slot of `CObject` (MFC convention, to be confirmed on this binary);
  export the decompiled `Serialize` bodies and helpers to depth 3 into a private folder.
- **C. Field tables (agent-assisted, cheap model, verified).** Turn archive-call sequences into per-type field lists (type, order, widths); implement as typed readers/writers in `ebpcmtp`; accept a type only
  when all sample projects round-trip byte-exactly and counts match. Names of fields from dialog resources.
- **D. Behaviour (few GUI runs).** Flip one property at a time in EasyBuilder (Wine+xdotool or `ebpro-gui`), save, diff with the known structure to confirm names and enumerations; only for the kinds in the first element list.
- Effort (ESTIMATE): A hours; B about a day; C about 1-2 days for the 15-20 kinds that matter for mechanism screens; D incremental. None of this is needed for the owner's first target (switch/lamp with pictures): the seed pack covers it (5.3).
- **Legal/ownership check first** (not decided by me): analysing EasyBuilder under its licence; the repo is public, so only derived field tables and tests with synthetic data go in, never Weintek binaries, decompiled code or customer files (FORMAT.md:8-9, :168).

---------------------------------------------------------------------------------------------------------------------------

## 5. Risks and gaps: screens from scratch, deliverable, verification, panel side

### 5.1 "Screens from scratch without a donor" (FACT unless marked)

1. **No factory.** The 10 verified widget kinds, Rect/Line and (with `?`) 6 guessed kinds can be cloned/imported; none can be created from nothing. Compact/opaque widgets cannot even be cloned (`opaque_object`); sub-object widgets only if class numbering checks out (TOOL.md:229-243).
2. **No empty base per panel/protocol.** Only one empty project exists: cMT 1024x600, 6.10.02.421, device "Local HMI" only (FORMAT.md:5,31-32,153-155). The real customer project "Родина" is cMT 1920x1080, saved by 6.10.01.371 (FORMAT.md:127).
3. **Devices and system settings are not parsed** (the tail): PLC endpoint/IP, OPC UA security, Modbus, panel IP, passwords, fonts, language. `map_devices` renames device *names* only (server.py:244-245). A project built on a template base keeps the base's device settings, including its PLC address (the objects taken from a donor only name the device).
4. **Binding limits:** `bind_tag` needs an OPC UA slot already present and of the same type; `add_tags` only for types already in the project (BOOL, INT, UINT, UDINT known; REAL listed but unverified); Modbus not decodable (FORMAT.md:87-89; ebpedit.py:266-292; TOOL.md:246-248).
5. **States are fixed by the template.** `picture_states` keeps the state count; the value-to-state rule of multi-state objects is not decoded, so a 3-state motor lamp works only if a 3-state template with the right mapping exists.
6. **Pictures:** attach path is PNG/GUID only, BMP transparent-colour semantics are per state in the group (partly handled), JPEG/GIF/SVG unusable; EasyBuilder's own shape library cannot be rendered or listed (GUID only) so "switch" shapes from EasyBuilder's library are invisible to any index until extracted from the install.
7. **Version drift:** donors must come from the same build (.300 vs .421 write some objects differently, FORMAT.md:15-17) but the guard checks `revision` only (ebpedit.py:327).
8. **Heuristic parser, not typed:** objects are located by byte patterns and class-number arithmetic; "not recognised" objects make insert/delete refuse; real projects needed several fixes in one day (FORMAT.md:127-149). Every new customer project can expose a new layout.
9. **Window slot arithmetic** ("where a record ends is not known") limits inserts in some windows (TOOL.md:236-238).

### 5.2 Producing a deliverable that loads in EasyBuilder / the HMI

| Level | What it proves | Status |
|---|---|---|
| V0 static (Python, seconds): round trip, declared == read, `cmtp_check`, `plc_tag_check`, picture constraints, link graph, new `hmi_bind_check` | structural sanity | exists except `hmi_bind_check` |
| V1 EasyBuilder **opens** the file under Wine (`ebpro_wine.sh open`: copies to `C:\p`, Ctrl+O, handles recovery and missing-font dialogs, prints `OPENED <title>` or `FAILED ... corrupted`) | not corrupt | **done 11 Nov 2026** for all edit kinds, EasyBuilder 6.10.02.421, Wine 10.0 (ebpro_wine.sh:20; TOOL.md:24-30) |
| V2 EasyBuilder **compiles** (Build) | addresses/devices/tags valid for the compiler | **not automated and not verified**; the script has only setup/start/open/shot/stop; `ebpro-gui` could click it (Windows) but is itself unverified on live EasyBuilder (ebpro-gui/TOOL.md:17). Check the exe for a command-line compile switch (4.6 A) |
| V3 EasyBuilder offline simulation + screenshots per state vs `cmtp_render(states)` | the object does what the screen says (states, text, pictures) | not done |
| V4 bench: real cMT + M241 (or Machine Expert simulation with OPC UA) | tags read/write, node ids, latency | not done; node-id CRC rule matched 582/582 tags of one real project (FORMAT.md:141-144) but was never tried against a live server |
| V5 commissioning | field | human |

Wine-script limits (read): sleep/coordinate-driven GUI automation (75 s startup, 150 s open), needs `innoextract`-ed EasyBuilder, Xvfb, xdotool, xwininfo, ImageMagick, 32-bit Mesa/GLVND for the Qt part, fonts copied into the prefix; one project at a time; "open" only.

### 5.3 Pragmatic path to "no donor" (proposal)

A **seed pack**: tiny EasyBuilder-saved projects (same build as production), one per panel model/resolution x protocol (OPC UA, Modbus TCP) with the right device and one window, plus one mini-donor per element kind and per state model
(2-state lamp, 2-state toggle, 3-state multi-state, set-bit button, numeric in/out, function key, text, rect/line). Made once by a human or by `ebpro-gui`, registered as `source.kind=seed_pack`, indexed as `element_template`. This turns "no donor" into "template from the pack" immediately and gives the
byte-level diffs needed for the typed parser later.

### 5.4 What "ready to install in the cabinet" needs on the panel side (and what the pipeline does / does not do)

- **Pipeline can do:** a project file with windows, objects, pictures, OPC UA tags (names, types, M241 node ids), link choice by models (`plc_link`: OPC UA vs Modbus TCP/RTU, panel resolution and file kind, plclink.py:101-175).
- **Pipeline cannot do (human or unverified):**
  1. Device settings on the panel project: OPC UA server URL (for M241 `opc.tcp://<ip>:4840`, HYPOTHESIS: port), security mode/policy, credentials/certificate trust, refresh/timeouts; or Modbus TCP IP/port/station/addressing base, word order, data types. Not parsed (5.1 item 3).
  2. Panel network (IP/mask), time, language, fonts present on the panel (project uses "GOST Type AU" etc.), user passwords/permissions, screensaver. Same tail.
  3. **Compile** in EasyBuilder, then **Download** (Ethernet/USB). `ebpro-gui` refuses Download/Upload/Password/Firmware windows by design (ebpro-gui/TOOL.md:65-70). Panel OS/firmware compatibility with the EasyBuilder build (EasyBuilder may ask to update the panel OS): unverified.
  4. PLC side: OPC UA server enabled in Machine Expert (`Ethernet_1 -> OPC UA Server Configuration`, firmware with OPC UA server), symbol configuration / `{attribute 'symbol' := 'readwrite'}`, controller IP in the cabinet network (plclink.py:19-27; plc-project SKILL.md:70-73). The `.project` itself is **not yet compiled in a real Machine Expert** (cross-area; NEW_PROJECT.md:80-83).
  5. Tag names must match the compiled PLC symbols exactly; node ids stay valid only for identical names.
  6. Acceptance: bench test with the real panel, each state of each mechanism, write commands interlocked per ТЗ (the skills already forbid inventing addresses and enabling commands without engineer approval, ebpro-mnemoscheme SKILL.md:19-20).
- **Currently unverified (summary):** compile of generated projects; behaviour of edited/created objects on a panel or simulator; `ebpro-gui` on a live EasyBuilder; Modbus/REAL encodings; REA and Ghidra on the real exe; value-to-state mapping of multi-state objects; shape library; picture/EasyBuilder memory limits on real cMT hardware; the real archive's naming and size.

---------------------------------------------------------------------------------------------------------------------------

## 6. ONI / Haiwell: adapter boundary (future only)

The repo has zero mentions of Haiwell or ONI (grep); TASKS.md:654 (ОПО-17.1) already anticipates other targets (Vijeo Designer, Operator Terminal Expert, web visualisation, SCADA), and `plclink.py` already holds model tables
(`CONTROLLERS`, `PANELS`: add rows, no redesign). Boundary proposed so adding them later costs rows + one adapter, not a schema change:

- **Neutral layer (target-independent):** `hmi/plan.json` (windows, roles, equipment, signals, navigation) as the source of truth that survives regeneration; element **roles** and **state roles** (off/on/alarm/...), image sets and labels
  (content-addressed, vendor-independent), `plc_signal`, `equipment`, `binding(role, signal)`.
- **Target layer:** `target` column on `element_type`, `element_template`, `source`; `target_address` JSON on `binding` (OPC UA node, Modbus register, vendor register) validated by the adapter.
- **Adapter interface (6 functions):** `capabilities()` (panel models, resolutions, picture formats/limits, element types), `read_project(path)` (donors and verification), `build(plan, base, out)` (deterministic writer: `ebpcmtp` today),
  `verify(out)` (open/compile/render), `address_codec()` (tag -> native address string, e.g. `name (ns:2;i:N)` for EasyBuilder/M241), `handoff()` (panel-side install checklist from 5.4).
- **PLC side:** a `plc_dialect` reader emitting the same `plc_signal` rows (ST/CODESYS today; Haiwell LD later); the HMI index never sees dialect syntax.
- **Tools:** the five `hmi_*` tool schemas stay identical; `target` defaults from `project.json`. Target-specific servers (`ebpro-cmtp` now) sit behind the adapter. Skills reference `hmi_*` and roles, not `cmtp_*` names, except inside the EasyBuilder adapter skill.
- Cost of not doing this now: skills and tools named `ebpro-*`/`cmtp_*` leak the target into every plan; renaming later touches skills, tests and access profiles.

---------------------------------------------------------------------------------------------------------------------------

## 7. Not verified, open questions, cross-area notes

**Not verified by me:** real-run token traces of a screen stage (all per-screen numbers are models over measured unit costs); sizes of real `cmtp_objects` for the main window (synthetic object cost x real counts);
how Hermes treats MCP images and large results (taken from A-token-audit.md, not re-checked); whether TOOL.md is loaded into the model context (A-token-audit.md: never auto-loaded); REA behaviour (not built or run); the EasyBuilder binary (not available);
FTS5 in the engineers' Python; vision accuracy on the real archive; real archive size/naming; whether EasyBuilder has a command-line compile; legal status of binary analysis.

**Questions for the owner:** (1) Is `ebpro-cmtp: *` (and `ebpro-gui`) in his access profile? (2) How many pictures are in the archive and how are they named; which are customer-specific? (3) Does he have the EasyBuilder kit/template projects and the Weintek `Sample` folder from the install?
(4) Which EasyBuilder build runs on the production panels, and which panel OS? (5) First element list (switch, lamp, multi-state motor, set-bit button, numeric in/out, nav key?) and the real state words in his projects. (6) One sample project with a Modbus device and a REAL tag.

**Cross-area notes:**
- Token audit: definitions of this chain are 2-3% of per-request input; history retention and round trips dominate. Hidden-tools allowlist exists (mcpwrap.py).
- Access: default profile lacks `ebpro-cmtp`/`ebpro-gui` (access.py:43,81); if that is also the owner's profile, a missing server could explain the agent falling back to Python generators (HYPOTHESIS for the `*.py` clutter; Machine Expert/clutter agents should check).
- Machine Expert pipeline: the multi-state/state-word example shows screens depend on PLC state variables (`<mech>.STATE`); put that into the ST generation template.
- Desktop UX (E-desktop-agent.md section 4.3): the proposed `.sysai/sources.json` manifest and `.sysai/index/` folder are the natural home of this index; the window can show `index.state` and a "review pictures" panel from the same SQLite, and the canvas already calls `cmtp_*` outside the model (no tokens).
- `app/net/hub_bundles.py:11-21`: seeds are downloadable zips; nothing installs `ebpro-cmtp` on the PC automatically (publish in the Hub + allow in the profile).

---------------------------------------------------------------------------------------------------------------------------

## Appendix A. Measurement method and files (all under `/tmp/research/`, nothing in the repo)

- `d_measure_static.py`: sizes and o200k tokens of TOOL.md/SKILL.md and `tools/list` JSON of ebpro-cmtp, pic-index, ebpro-gui, doc-kit (imported from a copy in `d_work/seed`).
- `d_tool_defs.py`: per-tool definition tokens (cmtp_edit 1,707; render 394; scene 302; objects 259; check 239; pack 254; relabel 233; links 200; extract 191; tags 173; labels 175; unpack 170; windows 153; info 149).
- `d_synth.py` builds `d_work/synth.cmtp` with the test-suite grammar (2 windows x 30 objects, 588 tags with numeric nodes, 40 library pictures in 20 groups) and measures `cmtp_objects`, `cmtp_scene`, `cmtp_tags`, `cmtp_labels`, `cmtp_windows`; per-object numbers above come from it. Limits: synthetic content, not a real customer window.
- `d_flow_model.py`: arithmetic of the screen-stage model (sections 2.4 and 3.7) from the measured unit costs; assumptions at the top of the file.
- `d_verify_facts.py`: confirms on a synthetic project that `picture_section()` exposes group names and state counts and that `images()` (used by `cmtp_extract_images`) misses BMP files that are in the library.
- `d_compact.py`: compact line format vs JSON for the same window (885 vs 4,284 tokens).
- `d_pics_measure.py`: synthetic libraries (175 and 733 pictures, 80% conventional names) through `picindex.refresh/find/overview/check`.
- `d_render_measure.py`: `ebprender.render` works headless (PNG 4 KB at 0.5, 11.5 KB at 1.0 for the synthetic card); real PNGs not measured.
- `d_tokens_misc.py` (GUID/sha/short id tokens, link/check rows, address forms, RU vs EN), `d_ex_tok.py` + `d_examples/*.json` (example answers).
- `d_rea_README.md`, `d_rea_PLAN.md`: REA README and plan as fetched.

## Appendix B. Evidence index (file:line)

- Pipeline/skills: `hub/seed/plc-project/SKILL.md:96-111`; `ebpro-project/SKILL.md:31-54`; `ebpro-mnemoscheme/SKILL.md:14-31`; `ebpro-donor/SKILL.md:17-50`; `hub/seed/README.md:7-60`.
- cmtp: `server.py:21-250` (tools), `:324-328` labels cap 2000, `:370-374` tags cap 5000, `:387-391` check cap 3000, `:392-404` render + base64 3 MB, `:468-486` picture_states call; `ebpcmtp.py:171-188` images (PNG/JPEG only), `:229-258` scene, `:499-503` kinds, `:1162-1253` library and picture objects (name at 1225-1226, 1248), `:1326-1365` look;
  `ebpedit.py:198-263` style, `:266-292` bind_tag, `:325-330` open_donor, `:356-465` Graft, `:668-746` picture_states, `:749-782` PNG validation, `:786-858` check.
- pic-index: `picindex.py:1-5, 23-84` dictionaries, `:197-218` headers, `:222-287` derive/finish, `:343-402` refresh, `:423-450` sets, `:469-523` find, `:526-544` overview (rows[:1000]), `:586-626` check; `pic-index/TOOL.md:38-97`.
- Docs: `docs/opo/EASYBUILDER_FORMAT.md:11-36, 48-79, 93-114, 127-168`; `docs/opo/NEW_PROJECT.md:50-57, 85-87, 97, 114-135, 143-154`; `TASKS.md:649-663`.
- Access/guard: `app/net/access.py:43, 81, 532-538`; `agents/sync/sysai_agent/mcpwrap.py:148-171`; `rules.py:11-13`; `app/net/mcp.py:398-403`; `app/net/hub_bundles.py:11-21`.
- Verification: `scripts/ebpro_wine.sh:1-22, 39-92`; `hub/seed/ebpro-gui/TOOL.md:17, 65-70`; `plc-kit/plclink.py:14-60, 101-175`.
- REA: README, `crates/rea-engine/{Cargo.toml, src/pe.rs, src/decompiler/*}`, `PLAN.md` (via `gh api`).
