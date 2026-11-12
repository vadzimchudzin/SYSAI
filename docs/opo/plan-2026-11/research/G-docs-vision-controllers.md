# G. Web research: drawings/PDF ingestion, vision cost, indexing, controller connectivity, Weintek

Date of research: 2026-11-12 (all "fetched" dates below are this day, UTC). Author: research agent G. STUDY ONLY: nothing in `/workspace/SYSAI` was touched.
Language: English; the owner reads Russian.

Legend. **FACT** = seen in a fetched primary source (URL + date given, or `[Sxx]` that resolves to URL + date in section 10). **MEASURED** = I ran it here (script path given). **ESTIMATE** = arithmetic from FACT inputs (inputs and formula shown). **HYPOTHESIS** = my inference, not verified. **NOT VERIFIED** = I could not confirm it.

TL;DR (details and sources below):
1. No drawing/PDF/E3 reader exists in the repo (only planned, `TASKS.md:362-408`); the main model reads schemes itself. Best-supported design: **deterministic extraction first (PDF words+bbox, DXF, E3 COM), cached by hash; vision only on crops by a cheap batch model**.
2. Image tokens today: Claude = `ceil(w/28) x ceil(h/28)` (the old pixels/750 rule is gone), OpenAI = 32 px patches x 1.2, Gemini = 258 per <=384 px image or 280/560/1,120 tokens by `media_resolution` (Gemini 3/4). **500 icons described once cost ~$0.03-0.08** (flash-lite, gpt-6-luna, haiku-5.5); the same job in the agent loop would cost ~$9-19.
3. Index: SQLite FTS5 + Snowball-stemmed Russian + trigram for tags (measured), RRF with optional embeddings; hosted `bge-m3` is $0.01 per M tokens; skip ColPali, FAISS, jina (NC).
4. M241: OPC UA (4840) and Modbus TCP (502) are **off by default**; OPC UA default policy is Basic256Sha256 + SignAndEncrypt, 2 sessions, read-only symbols, anonymous allowed unless disabled; Machine Expert uses TCP 1105/11740, UDP 1740. **No usable open-source CODESYS online client exists**; firmware < 5.4.13.12 has a channel-exhaustion DoS (CVE-2025-13901). Safest path: OPC UA read-only first.
5. Weintek: EBPro 6.10.02 requires account+password for download/upload; OPC UA Client driver supports policy/auth/subscription; remote download is scriptable only via EasyAccess CLI / `easydownload_cli` (undocumented here).
6. Urgent cross-area: model defaults in `app/net/gateway.py:35-39` hit OpenAI/Google shutdowns on 2026-10-23, 2026-12-01, 2026-12-11, 2027-01-14.

How sources were reached: no web-search tool existed. I used WebFetch, `curl` (JSON APIs, PDFs, docs), `gh api` (GitHub REST incl. code search), and the context7 MCP (library docs). Search engines (DuckDuckGo) were blocked (HTTP 202 challenge), CODESYS help (`content.helpme-codesys.com`) returned 403, Schneider product help (`product-help.se.com`) returned 403 for curl, WebFetch and a headless browser (Akamai "Access Denied"); I did not try to bypass this.

---------------------------------------------------------------------------------------------------

## 1. What the repo does today (FACT, file:line)

| Area | State | Evidence |
|---|---|---|
| Drawing/PDF/E3 ingestion tool | **Does not exist.** Planned as "stage 7 plc-kit: разбор чертежей" (`pdf_words`, `pdf_lines`, `pdf_tables`, `page_image`, `dxf_blocks`, `aml_channels`, scans via vision with "кэш по хэшу листа"). `cad/` MCP and drawing part of plc-kit listed as "Будет". | `docs/opo/TASKS.md:362-408` (esp. :394), `agents/README.md:25-28`; no pdf/dxf code in `hub/seed/*` |
| How the agent reads schemes now | Skill only says "Прочитай всё в `materials/`" and "пересчитай"; Hermes `pdf` skill ("чтение и распознавание листов") is named as the reader. So the main (expensive) model reads PDFs/images itself. | `hub/seed/plc-project/SKILL.md:44`, `docs/opo/TASKS.md:544` |
| `doc-kit` | Only Word (.docx) templates: read outline/text, build .docx from Markdown. Not drawings. stdlib-only, no network. | `hub/seed/doc-kit/TOOL.md:30-35`, `server.py` |
| `app/doctext.py` (business docs) | PDF text via `pypdf`; if text < 30 chars x min(pages,5) -> `NeedsVision`; then the **whole PDF is base64-sent as a `file` part** to `vision_model` (default `google/gemini-2.5-flash`), max_tokens 8000. | `app/doctext.py:79-87, 89-105` (default model :91; `requirements.txt:13 pypdf==5.6.0`) |
| `app/doc_intake.py` | Invoices/acts etc. (requisites with quotes). Not engineering. Skimmed only. | `app/doc_intake.py:1-80` |
| `pic-index` | Index of picture library by file/folder **names** + manual labels; labels keyed by content `sha1` (survive rename). "Внутрь картинок инструмент не смотрит ... Картинку без распознанного оборудования агент должен посмотреть сам и подписать." | `hub/seed/pic-index/TOOL.md:92-93`, `picindex.py:256-268, 365-378` |
| Per-tool Python deps on the engineer's PC | A hub tool with `requirements.txt` gets its own `uv venv` (installs from PyPI at sync time). Existing tools are stdlib-only. So adding `asyncua`/`pymupdf` etc. is supported by the sync agent. | `agents/sync/sysai_agent/hubsync.py:1-3, 82-97` |
| Machine Expert scripting | `me_scan`, `me_online` (login `OnlineChangeOption.Keep` for read, `download/start/stop/write` need `confirm:true`); starts `LogicBuilderShell.exe`, then `LogicBuilder.exe --runscript --noUI`, then CODESYS. Declared **"Не проверено на живом Machine Expert"**. | `hub/seed/machine-expert/TOOL.md:23-25, 41-45, 86-96`, `me_job.py:601-690`, `mehost.py:10-12` |
| Download to panel | Windows titled Download/Upload/Password/Firmware are "только человек". | `hub/seed/ebpro-gui/TOOL.md:67-68` |
| OPC UA node ids for M241/M251 | Repo already derives numeric ids `ns:2;i:<CRC-32 of "List.Variable">` (seen in real projects), string ids otherwise. | `hub/seed/ebpro-cmtp/ebpcmtp.py:593-662`, `ebpedit.py:154-165` |
| Model defaults | `cheap=google/gemini-2.5-flash-lite`, `mid=google/gemini-2.5-flash`, `strong=google/gemini-2.5-pro`; fallbacks `openai/gpt-4.1-nano`, `gpt-4.1-mini`, `gpt-5`; vision falls back to `google/gemini-2.5-flash`. | `app/net/gateway.py:35-39, 112-124` |

---------------------------------------------------------------------------------------------------

## 2. Ingesting engineering documents cheaply

### 2.1 Core finding (FACT + reasoning)

The cheapest reliable pipeline is **deterministic first, vision last and targeted**:

1. If the source is a CAD/EDA tool, take structured data, not pictures (E3.series COM API, EPLAN export/API, DXF blocks with attributes).
2. If it is a vector PDF with a text layer, extract **words with bounding boxes** and vector lines (milliseconds per page), classify sheets by title block, parse tables by geometry.
3. Only for scans/outlined text/symbol semantics use OCR, then a cheap VLM on **crops**, cached by hash.
4. Keep the main agent away from raw images: give it compact "sheet cards" (JSON/CSV), not pictures.

Research support (arXiv, all 2026):
- **Index Light, Reason Deep (DVI)** [S20a]: describing every page with a VLM at ingestion and embedding the descriptions "loses critical visual details" and "embedding retrieval systematically fails on highly similar documents". DVI uses **zero VLM calls at ingestion**, builds an index from structure (TOC, drawing numbers), retrieves with **BM25**, and sends the original page image + question to a VLM only at query time. Reported: Bridge engineering drawings (1,323 questions) end-to-end accuracy **65.6 % vs 24.3 %** for pre-ingestion; ColPali only **20.1 % PageR@3** on that set; CircuitVQA ImgR@3 **31.2 % vs 0.7 %**. (Paper's own datasets, not the owner's; direction is what matters.)
- **P&ID grounding** [S20c]: VLMs "often invent or miss process connections"; image-only prompting gave **36.7-41.3 %** exact match on 3,000 topology questions; with a recovered graph and read-only query operators **74.3-76.0 %** (recovered graph F1 0.742 on exact connections).
- **ChatP&ID** [S20d]: graph representation +18 % accuracy vs raw image input and **-85 % token cost** vs ingesting smart-P&ID files; GPT-5-mini + ContextRAG 91 % at **$0.004 per task**.
- **OmniSch** [S20e]: LMMs show "unreliable fine-grained grounding, brittle layout-to-graph parsing, inconsistent global connectivity reasoning" on PCB schematics (not GOST cabinet drawings; indicative only).
- **Small objects** [S20b]: VLMs see an image through a fixed token budget; raising resolution by factor a costs a^2 tokens; **splitting into overlapping tiles that never zoom out raised recall** (up to +0.28, 0.62 -> 0.90) while splits that zoom out lowered recall in 19 of 21 settings; cost is known before the call.

### 2.2 PDF vector/text extraction

| Library | Licence | Latest (PyPI, upload) | Notes |
|---|---|---|---|
| **PyMuPDF** | **AGPL-3.0 or Artifex commercial** | 2.1 (2026-11-02), py>=3.11, wheel **~69 MB** | `page.get_text("words")` -> `(x0,y0,x1,y1,word,block,line,word_no)`; `page.get_drawings()` -> vector paths (rect, colors, items); `page.find_tables()` uses vector lines/rects; vendor benchmark 7,031 pages: text 8.01 s (PyMuPDF) vs 227.27 s (PDFMiner), render 367 s. AGPL: PyMuPDF docs say read the licence, contact Artifex if AGPL cannot be met. [S16, S31] |
| **pypdfium2** | BSD-3 / Apache-2.0 (+deps) | 5.15.2 (2026-11-09), wheels ~5 MB | `page.render(scale, crop=(l,b,r,t))` -> bitmap/PIL (DPI = scale*72), `textpage.get_text_bounded(l,b,r,t)`, `get_rect`. Permissive route for **region render + region text**. [S11, S31] |
| **pdfplumber** | MIT | 0.11.10 (2026-06-15) | chars/lines/rects with coordinates; `extract_tables`. Built on pdfminer.six (MIT, 20260107). |
| pypdf | BSD-3 | 7.0.1 (2026-11-10) | What `app/doctext.py` uses; text only, no geometry for tables. |
| camelot-py | MIT | 2.0.0 (2026-06-04) | lattice/stream table extraction (not needed if words+bbox clustering is used). |
| docling | MIT | 2.150.0 (2026-11-10) | Generic DOCX/PDF parser (heavy, ML). Not verified on drawings. |

Useful precedent: `EPDF-Extractor/indu-doc-transformer` (MIT, py>=3.13, pushed 2025-10-31) converts **EPLAN PDFs** to AutomationML via a plugin with `footers_extractor`, `table_extractor`, `page_processor`, per-page settings; depends on `pymupdf` + `pdfplumber` [S35b]. This shows E-CAD PDFs are processed by words + footers (title block) + tables, no vision.

**Licence decision needed (not legal advice):** SYSAI is a server (network service) and a desktop agent distributed to PCs. PyMuPDF is AGPL/commercial. A permissive stack = `pypdfium2` (render, region text) + `pdfplumber`/`pdfminer.six` (geometry). Speed cost: PDFMiner is ~28x slower than PyMuPDF on the vendor's mixed text pages (227 s vs 8 s per 7,031 pages = 32 ms vs 1.1 ms per page). Dense CAD vector pages are slower still (HYPOTHESIS 0.5-3 s/page); at "a few hundred pages per project" this is minutes, once, cached.

**First check on a real E3 PDF (cheap, decisive):** count words from `get_text("words")`. If > ~100 per sheet, text layer exists -> deterministic route. If ~0, text is outlined/rasterized -> OCR. E3 export properties are **NOT VERIFIED** (no public E3 doc reached; zuken.com page only lists "E3.PLCBridge - PLC information exchange" [S34c]).

### 2.3 E3.series / EPLAN export formats

| Source | What I could verify | Source |
|---|---|---|
| **E3.series COM / Python** | Official Zuken E3 GmbH PyPI package `e3series` 27.10.0 (2026-09-08), MIT: "Python wrapper and utilities for the E3.series COM interface". Official examples print the **connection list (from/to device and pin)** and write **parts lists to xlsx**. Needs E3 installed (Windows). | [S34a, S34b] |
| E3 MCP (third party) | `bydavidgenie/Zuken-E3.series-MCP`: inspect sheets, search components/cables/connectors, pin configuration, export sheet images (0 stars, 2026-07). | [S34d] |
| **E3 file/PDF export formats (XML/CSV/AML)** | **NOT VERIFIED.** No public E3 format spec reached. | - |
| **EPLAN** | `Canary-ECAD/eplan-mcp` (MIT, 149 stars, pushed 2026-11-12): MCP over EPLAN's remoting (File > Settings > Workstation > Interfaces > Remote access), actions, scripts, "exports". Parts XML parsers exist on GitHub (`ET_XML`). **AutomationML (CAEX)** is an open exchange format; official `AutomationML/automationml-python` (MIT, 2026-10-17) reads/validates CAEX 3.0. Which EPLAN versions export PLC data to AML: **NOT VERIFIED** (repo's own TASKS.md:389-391 claims it). | [S35a, S35c] |

Practical consequence: **if the engineer has E3 on the PC, a small COM script that dumps devices, pins, connections and PLC channels to CSV/JSON beats any PDF parsing** (exact, free, and gives the "channel -> device" mapping stage 7.3 wants). Pilot needed to see what the owner's E3 projects contain.

### 2.4 OCR for Russian technical drawings (only for scans / outlined text)

| Engine | Licence | Russian | CPU feasibility | Notes / numbers |
|---|---|---|---|---|
| **PaddleOCR 3.7.0** (2026-06-11) | Apache-2.0 | `eslav_PP-OCRv5_mobile_rec` (Russian, Belarusian, Ukrainian, English) | Yes. Models are tiny: eslav rec **7.8 MB**, mobile det **4.7 MB** (HF). Vendor CPU benchmark (Xeon Gold 6271C, ~10 cores busy): PP-OCRv5 mobile **1.75 s/image**, peak RAM 2.2 GB, on 200 general/document images (not drawings). | Vendor accuracy for eslav on own 7,031-image East-Slavic set: **81.6 %** (+31.4 % vs previous gen). Needs `paddlepaddle` 3.3.1 (Linux wheel **~195 MB**, PyPI 2026-03-26). [S13, S22, S11] |
| **RapidOCR 3.10.0** (2026-10-08) | Apache-2.0 | `cyrillic_PP-OCRv5_rec_mobile` (docs) | Yes: ONNX Runtime/OpenVINO, no Paddle dependency; wheel 27 MB. | Same PP-OCR models via ONNX: lightest install route. [S13b] |
| **Tesseract 5.5.4** (2026-10-28) | Apache-2.0 | `rus.traineddata`: fast **3.9 MB**, best **15.3 MB** | Yes, classic CPU engine. | No public accuracy number for GOST drawings found (**NOT VERIFIED**). Needs preprocessing (deskew, binarize, upscale). |
| EasyOCR 1.7.2 (2024-09-24) | Apache-2.0 | yes (Cyrillic) | PyTorch (heavy) | Last release 2024; skip. |
| **docTR 1.1.0** (2026-08-21) | Apache-2.0 | **Not out of the box**: `VOCABS["russian"]` exists in code, but the shipped `crnn_vgg16_bn`/`parseq` default configs use `VOCABS["french"]`. Needs fine-tuning for Cyrillic. | - | [S15] Skip. |
| **Surya 0.23.0** (2026-10-29) | Code Apache-2.0; **weights modified AI Pubs Open Rail-M: free for research, personal use, startups under $5M funding/revenue**, otherwise commercial licence | Vendor internal 91-language benchmark: Russian **71.7 %** (Surya 2.5) vs 77.8 % (Surya 2) | Needs `vllm` (NVIDIA GPU) or `llama.cpp` (CPU/Apple) server: VLM-based, heavy on CPU | [S14] Skip for now (licence + weight). |
| OCRmyPDF 17.14.1 (MPL-2.0) | MPL-2.0 | via Tesseract | - | Adds a text layer to scanned PDFs; useful wrapper if Tesseract route chosen. |

Recommendation: if scans are common, **RapidOCR (cyrillic/eslav PP-OCRv5 ONNX) or Tesseract rus** for text + boxes, then a VLM only for what OCR cannot do (symbol semantics, low confidence). Measure on 3 real sheets first (no public benchmark for GOST fonts: **NOT VERIFIED**).

### 2.5 DXF / DWG

- **ezdxf 1.4.5** (MIT, 2026-10-16): reads DXF; INSERT entities expose block attributes (AutoCAD Electrical-style tags); `ezdxf.addons.odafc.readfile()` converts DWG -> temp DXF through an installed **ODA File Converter** [S19].
- **ODA File Converter**: Windows 10+/Linux/macOS, GUI + **command line**: source dir, target dir, filter, output version/type, recursive, audit flags; listed under ODA's "Free" products [S17]. Licence/redistribution text **NOT READ** (check before bundling).
- **LibreDWG** (GPL-3.0-or-later): "can read all DWG versions, just some very advanced R2010+ objects fail to read and are skipped"; tools `dwg2dxf`, `dwggrep`, `dwglayer`; writing unreliable [S18]. GPL makes bundling harder than calling ODA as an external program.
- Path: DWG -> (ODA CLI) -> DXF -> ezdxf -> blocks+attributes+texts+layers -> JSON. Effort S-M. Repo already plans `dwg_to_dxf` via ODA (`docs/opo/TASKS.md:512`).

### 2.6 When a vision LLM is really needed, and how to cut its cost

Needed: (a) scans/outlined text with no text layer after OCR fails; (b) **symbol semantics** and unlabeled devices; (c) graphic-only connectivity (process schemes; wire routing when no connection list exists); (d) redlines/handwriting. Not needed: PLC overview sheets, terminal plans, cable journals, equipment lists, BOMs (text + table geometry).

Cost levers (all FACT-backed below in section 3, ESTIMATE where computed):
1. **Never send whole A3 sheets at "high" and hope**: the provider downsizes them. Claude standard tier shrinks an A3 sheet to 1307x924 = **~79 DPI effective** (high-res tier 2296x1624 = ~139 DPI) at any input DPI (MEASURED with the vendor's own `resized_size`, `g-scripts/sheet_tokens.py`). 2.5 mm text (~8 px tall at 79 DPI) is not readable.
2. **Crop by region** (title block, PLC module block, terminal strip): use words+lines to find regions deterministically; render only that crop with `pypdfium2 render(scale, crop=...)`.
3. **Tile at native resolution with ~10 % overlap, never zoom out** [S20b]; 1024 px tiles at 200 DPI: A3 = 12 tiles.
4. **Two-pass**: 1 thumbnail (~500 px) to classify the sheet, then crops only where needed.
5. **Cache by hash**: key = `sha256(pdf)` + page + crop rect + dpi + model + prompt-hash -> JSON; mark results "проверить" (repo already wants this: `TASKS.md:394`). pic-index already keys labels by `sha1`.
6. **Cheap model + batch**: OpenRouter Batch API (`POST /api/v1/batches`, within 24 h, "typically billed at 50 %") and `flex` tier (Gemini 2.5 flash-lite `google-ai-studio/flex` $0.05/$0.20 per M) [S10, S2].
7. Set media-resolution knobs per image: Gemini 3/4 `media_resolution` low 280 / medium 560 / high 1,120 / ultra 2,240 tokens per image [S6]; OpenAI `detail: low|high|original|auto` [S3].
8. Keep instructions as a stable prefix (prompt caching: OpenAI caches "text, images, documents" in the rendered prefix; GPT-5.6+ cache reads 0.1x, writes 1.25x for 30 min) [S3b].

ESTIMATE (arithmetic, `g-scripts/sheet_tokens.py`): per A3 sheet at 200 DPI in 12 tiles of 1024x1024:

| Provider (formula source) | tokens/tile | tokens/sheet (12 tiles) | 100 sheets | input cost for 100 sheets |
|---|---|---|---|---|
| Claude (28 px patches, cap 1,568/4,784) | 1,369 | 16,428 | 1.64 M | $0.164 @ $0.10/M (haiku-5.5) |
| OpenAI patch (32 px, x1.2) | 1,229 | 14,748 | 1.47 M | $0.295 @ $0.20/M (gpt-5.4-nano) |
| Gemini 2.5 (258 per tile, rough rule) | 1,032 | 12,384 | 1.24 M | $0.124 @ $0.10/M (flash-lite) |
| Gemini 3/4 `high` | 1,120 | 13,440 | 1.34 M | n/a (3.x flash-lite $0.25-0.30/M) |

Add output (say 1,500 tokens/sheet): +$0.06-0.19 per 100 sheets. So a **batch/cheap-model vision pass over 100 sheets costs roughly $0.2-0.5**; at 300 DPI (24 tiles) double it. The same images pushed through the main agent loop (273k-token context per request, section 7) would cost orders of magnitude more.

---------------------------------------------------------------------------------------------------

## 3. Per-image token costs and the "500 icons" calculation

### 3.1 Official formulas (FACT)

**OpenAI** (`developers.openai.com/api/docs/guides/images-vision`, fetched 2026-11-12) [S3]:
- Patch-based models: `patch_count = ceil(w/32) x ceil(h/32)` after the detail level's pixel limit, then budget shrink if the model has one, then **tokens = ceil(patches x multiplier)**; reject above 30,000 patches. Multipliers: gpt-6-astra, gpt-5.6-sol/terra/luna, gpt-5.5, 5.4, 5.4-mini, 5.4-nano, 5.2 = **1.2**; gpt-5-mini 1.2*; gpt-5-nano 1.5*; **gpt-4.1-mini 1.62; gpt-4.1-nano 2.46\*** (2025-04-14); o4-mini 1.72* (* = "deprecated and scheduled for shutdown"). `gpt-6-luna` is **not in the doc table (multiplier NOT VERIFIED; 1.2 assumed below)**.
- Sizing: gpt-5.4/-mini/-nano `high`: <=2,500 patches and 2,048 px; `original`: <=10,000 patches and 6,000 px; `low` uses 2,048 px and a 6,144-patch budget ("can use more tokens than high"). gpt-6-astra/5.6: `low` fits 512x512; `high` 2,500 patches.
- Tile models (gpt-4o, gpt-4.1: 85 base + 170/tile of 512 px; gpt-4o-mini 2,833 + 5,667/tile; gpt-5.1/gpt-5: 70 + 140). `low` = base only.
- Vendor examples reproduced by my script: 1024x1024 -> 1,229; 2048x2048 -> 3,000; 4096x512 -> 2,458 (`sheet_tokens.py` sanity lines).

**Gemini** (`ai.google.dev/gemini-api/docs/image-understanding` -> `aistudio.google.com/docs/image-understanding`, fetched 2026-11-12) [S5, S6]:
- "258 tokens if both dimensions <= 384 pixels. Larger images are tiled into 768x768 pixel tiles, each costing 258 tokens"; rough rule: crop unit = floor(min(w,h)/1.5), tiles = ceil(w/crop) x ceil(h/crop) (their 960x540 example = 6 tiles; reproduced).
- Gemini 3/4 `media_resolution` (images): low **280**, medium **560**, high **1,120** (default/recommended), ultra_high **2,240**; PDFs: medium 560 "+ native text"; max 3,600 images per request.

**Claude** (`platform.claude.com/docs/en/build-with-claude/vision` and `.../vision-coordinates`, fetched 2026-11-12) [S7, S8]:
- **The old "pixels/750" rule is gone from the current docs.** Now: `tokens = ceil(w/28) x ceil(h/28)`; images are downscaled so the long edge <= **1,568 px / <= 1,568 tokens (standard tier)** or **2,576 px / <= 4,784 tokens (high-resolution tier = Claude 4.7 and later)**. Doc examples: 200x200 = 64 tokens; 1000x1000 = 1,296; 1920x1080 = 1,560 (std) / 2,691 (high); 3840x2160 = 4,784 (high). Claude Haiku 4.5 at $1/M: 1000x1000 ~ $1.30 per 1,000 images.
- Doc warns of mistakes on "very small images under 200 pixels"; limits: 600 images/request (100 for 200k-context models), 8000x8000 max, stricter 3,000 px per-image limit above 20 images.

**OpenRouter semantics** [S1, S10]: model JSON has `pricing.prompt/completion/image/input_cache_read...`; docs only say `image` = "Cost per image input" (unit not stated). For `google/gemini-2.5-flash-lite` the `image` price equals the prompt price (0.0000001), so images are billed like input tokens (**inference, docs silent**). OpenAI/Claude models have no `image` price (tokens only).

### 3.2 Prices used (OpenRouter `GET /api/v1/models`, 468 models, fetched 2026-11-12 12:39 UTC; local copy `/tmp/research/g-data/openrouter/models.json`)

| Model | in $/M | out $/M | cache read $/M | Notes |
|---|---|---|---|---|
| google/gemini-2.5-flash-lite | 0.10 | 0.40 | 0.01 | not deprecated by Google ("until further notice") [S9]; `flex` endpoint 0.05/0.20 [S2]; reasoning optional |
| openai/gpt-4.1-nano | 0.10 | 0.40 | 0.03 | **OpenAI shut it down 2026-10-23** [S4]; OpenRouter lists only Azure endpoints (0.10/0.40, 0.11/0.44) [S2] |
| openai/gpt-5.4-nano | 0.20 | 1.25 | 0.02 | OpenAI: removal 2027-04-01 -> gpt-6-luna [S4] |
| openai/gpt-6-luna | 0.10 | 0.50 | 0.01 | OpenAI's named replacement for nano/mini class; `:batch` 0.05/0.25; reasoning enabled by default (effort medium; set `none` for captions) |
| anthropic/claude-haiku-5.5 | 0.10 | 0.50 | 0.01 | >100k prompt override 0.50/2.50 |
| openai/gpt-6.1-terra (main model in spend table) | 1.00 | 5.00 | 0.05 | **prompt >= 272,000 tokens: 2.00 / 7.50 / 0.10** (see section 7) |

### 3.3 Cost of describing 500 small icons once (ESTIMATE; `g-scripts/icon_cost.py`)

Assumptions (mine): N = 500 icons; instruction text P = 200 tokens per call; output O = 80 tokens per icon (JSON: equipment, state, orientation, RU+EN tags; a sample RU description = 41 tokens by tiktoken o200k, MEASURED); no reasoning tokens; one icon per request (worst case; batching 16-20 icons per request only lowers it).
Formula: `total = N x (image_tokens + P) x in_price + N x O x out_price`.

128x128 px icons:

| Model | image tokens/icon | input = 500 x (img+200) x price | output = 500 x 80 x price | **Total** |
|---|---|---|---|---|
| gemini-2.5-flash-lite | 258 (<=384 px) | 500 x 458 = 229,000 x $0.10/M = $0.0229 | 40,000 x $0.40/M = $0.0160 | **$0.039** |
| gpt-4.1-nano | ceil(16 x 2.46) = 40 | 500 x 240 = 120,000 x $0.10/M = $0.0120 | $0.0160 | **$0.028** (model shut down at OpenAI) |
| gpt-5.4-nano | ceil(16 x 1.2) = 20 | 110,000 x $0.20/M = $0.0220 | 40,000 x $1.25/M = $0.0500 | **$0.072** |
| gpt-6-luna (x1.2 assumed) | 20 | 110,000 x $0.10/M = $0.0110 | 40,000 x $0.50/M = $0.0200 | **$0.031** |
| claude-haiku-5.5 | ceil(128/28)^2 = 25 | 112,500 x $0.10/M = $0.0113 | $0.0200 | **$0.031** |

At 64x64: $0.027-0.071; at 256x256: $0.034-0.078 (Gemini flat 258 tokens up to 384 px; OpenAI 158/77 tokens; Claude 100). Even with 5x longer output (400 tokens/icon) Gemini flash-lite = ~$0.10. Batch API or `flex` halves it. Upscale tiny icons to >=200 px for Claude (docs warn <200 px; costs only 64 tokens).

**Perspective (ESTIMATE from the owner's numbers):** one project = ~$25; describing 500 icons ~ $0.03-0.08 = 0.1-0.3 % of that. By contrast, having the main agent look at each icon in its own loop would pay ~273k tokens of context per request: 38.79M tokens / 142 requests = 273,169 per request; at 98 % cache read x $0.05/M = $0.0134 + 5.5k uncached x $1/M = $0.0055 -> **~$0.019 per request before output** (at the >=272k-token rates 2.00/0.10 per M it would be ~$0.038), i.e. **~$9.5 (up to ~$19) for 500 one-icon requests**, about 100-250x the batch job (HYPOTHESIS: assumes the same average context; not measured on icon work). Conclusion: do icon labeling as a **separate cheap batch job writing into the existing sha1-keyed label store** (`picindex.py:263`), never in the agent loop.

---------------------------------------------------------------------------------------------------

## 4. Indexing and retrieval

### 4.1 SQLite FTS5 for exact + Russian (MEASURED, `g-scripts/fts5_ru_test.py`, `fts5_tags_test.py`; SQLite 3.48.0 here, FTS5 compiled in)

| Test | Result |
|---|---|
| `unicode61`, query `НОРИЯ` vs docs `нория/Нория` | case-folded Cyrillic works (3/3 hits) |
| `unicode61`, query `нории` (genitive) vs `нория` | **no morphology**: found only the doc with exactly `нории` (1 of 4) |
| `porter unicode61` | English-only stemmer: same 1 of 4 for Russian |
| prefix `нори*` | all 4 forms found (works for short stems) |
| `trigram`, `нори` | all 4; `норией` -> 0 hits (needs a shared 3-gram) |
| `ё`/`е` (`подает` vs `подаёт`) | **0 hits even with `remove_diacritics 2`**: normalize ё->е in code |
| **Pre-stem with Snowball Russian** (`snowballstemmer` 3.1.1, BSD-3, pure Python, 34 languages) then `unicode61` | `нория/нории/норий/нориях` -> `нор`: **4/4**; `задвижку` -> `задвижк` (found `задвижка/задвижки`); `транспортеры` found `транспортёр` (stemmer folds ё) |
| Identifiers: `unicode61` default | `-SB1` indexed as `sb1` (OK for tags) |
| `unicode61 tokenchars '-.%:'` | whole tokens kept: `"-KM1"` does not match `-KM10`; `"%IX0.3"`, `"I0.3"` exact |
| `trigram` for identifiers | substring: `KM1`, `SB1`, `%IX0.3` found (min 3 chars) |

Design (works with stdlib `sqlite3`): two FTS5 tables per corpus: (a) words: `unicode61`, text lowercased, ё->е, Snowball-stemmed in Python on write and on query; (b) identifiers: `trigram` or `unicode61 tokenchars '-.%:'`. Rank with built-in `bm25()` (`ORDER BY rank`). Alternatives: PyStemmer 3.1.0 (C, MIT/BSD), `pymorphy3` 2.0.6 (MIT, real lemmatizer, heavier dictionaries; likely unnecessary for equipment nouns). **NOT VERIFIED:** FTS5 availability in the engineer's Windows Python (check `PRAGMA compile_options`).

Corpus size arithmetic (ESTIMATE): "few hundred pages" ~ 300 pages x ~600-3,000 tokens ~ 0.2-1 M tokens; Anthropic's own guidance: a knowledge base below ~200k tokens (~500 pages) can simply be put in the prompt [S21] - but that is exactly the pattern that made each agent request 273k tokens; index + retrieve instead.

### 4.2 Hybrid search (FACT)

- Anthropic Contextual Retrieval (2024-09-19) [S21]: top-20 retrieval failure 5.7 % (embeddings) -> 3.7 % (contextual embeddings, -35 %) -> **2.9 % (+ contextual BM25, -49 %)** -> 1.9 % (+ reranking, -67 %); contextualizing chunks costs "$1.02 per million document tokens" (2024 prices).
- Reciprocal Rank Fusion in SQL with FTS5 + sqlite-vec, `rrf_k = 60`, weights 1.0 each: official example `examples/nbc-headlines/3_search.ipynb` [S31]. Pattern: two top-k lists -> `1/(60+rank)` each -> sum.
- DVI paper above: for homogeneous engineering documents **BM25 over structure/drawing numbers beat embedding retrieval** (ColPali 20.1 % PageR@3) [S20a]. So BM25/identifier search is the primary path; embeddings are an add-on for natural-language questions and icon similarity.

### 4.3 Text embeddings, RU+EN (HF API/model cards, fetched 2026-11-12) [S22]

| Model | Licence | Params | Context / dim | Notes |
|---|---|---|---|---|
| **multilingual-e5-small** | MIT | 117.7 M (12 layers, 384-d) | 512 tokens | needs `query: `/`passage: ` prefixes; 100 languages (xlm-r vocab); smallest sane choice for CPU |
| multilingual-e5-base / large | MIT | 278.0 M / 559.9 M | 512 | |
| **bge-m3** | MIT | XLM-R-large class (API gave no total; ~568 M widely quoted, NOT VERIFIED here) | 8,192, 1,024-d | dense + sparse + multi-vector in one model; 100+ languages |
| deepvk/USER-bge-m3 | Apache-2.0 | 359.0 M | - | Russian-tuned bge-m3 distillation |
| **Qwen3-Embedding-0.6B** | Apache-2.0 | 595.8 M (28 layers) | 32k, 32-1,024-d MRL, instruction-aware | 100+ languages; updated 2026-04-20 |
| EmbeddingGemma-300m (2025) | **Gemma terms** (gated) | 302.9 M | | licence is not Apache |
| **EmbeddingGemma 2** (2026-10-28) | Apache-2.0 per card | 740 M total (270 M text backbone + 170 M vision + 300 M audio, loadable separately) | 8,192; 768-d, MRL 128/256/512/768 | **multimodal**: text+image+video+audio in one space; MTEB multilingual v2 mean 61.36; MIEB lite 65.67; image = 280 tokens by default (70-1,120 budget); "designed for consumer hardware"; float32 recommended on most CPUs. Two weeks old: maturity unknown |
| granite-embedding-97m / 311m multilingual r2 | Apache-2.0 | 97.4 M / 311.7 M | | released 2026-05-18, 52 languages; small and permissive |
| jina-embeddings-v3, jina-clip-v2 | **CC-BY-NC-4.0** | 572 M / 865 M | | non-commercial: skip |
| nomic-embed-text-v2-moe | Apache-2.0 | 475 M | | |

CPU speed: **NOT MEASURED** (no model downloads > 50 MB allowed here). ESTIMATE from FLOPs ~ 2 x non-embedding params per token: e5-small ~ 42 MFLOP/token; a 0.5 M-token corpus = 21 TFLOP = ~3.5 min at 100 GFLOPS effective, ~17 min at 20 GFLOPS (HYPOTHESIS on laptop throughput). bge-m3 (~300 M non-embedding) ~ 0.6 GFLOP/token -> ~50 min at 100 GFLOPS; Qwen3-0.6B (~0.44 B non-embedding) -> ~73 min. So **local CPU embedding is fine for e5-small/granite-97m, painful for the 0.5-0.6 B models**.

**Hosted alternative (FACT, `GET https://openrouter.ai/api/v1/embeddings/models`, 40 models, 2026-11-12):** `baai/bge-m3` **$0.01 per M tokens** (ctx 8,194), `qwen/qwen3-embedding-8b` $0.01/M, `qwen/qwen3-embedding-4b` $0.02/M, `intfloat/multilingual-e5-large` $0.01/M, `openai/text-embedding-3-small` $0.02/M, `google/gemini-embedding-2` $0.20/M (text+image+file+audio+video), `cohere/embed-5-fast` $0.08/M (text+image), free `nvidia/llama-nemotron-embed-vl-1b-v2:free` (text+image). Endpoint `POST /api/v1/embeddings` (fields `input` incl. `image_url`, `model`, `dimensions`, `input_type`) [S10]. ESTIMATE: embedding 0.5 M tokens with bge-m3 = **$0.005**. Hosted embeddings remove the need to ship torch/onnx to engineer PCs; query-time needs one tiny call (or a server endpoint).

### 4.4 Image embeddings and page retrieval [S22]

| Model | Licence | Params | Verdict for "a few thousand icons" |
|---|---|---|---|
| **DINOv2-small** | Apache-2.0 | 22.1 M | Cheap image-to-image similarity (group on/off/alarm variants, find duplicates). 4k icons at 224 px ~ 4.6 GFLOP each = ~18 TFLOP (ESTIMATE) ~ a few minutes on CPU |
| **SigLIP 2** base-patch16-224/256 | Apache-2.0 | 375 M (vision+text) | Text-to-image and image-to-image; paper title "SigLIP 2: Multilingual Vision-Language Encoders ..." (WebLI); Russian quality on technical icons **NOT VERIFIED** |
| `clip-ViT-B-32-multilingual-v1` | Apache-2.0 | 134.7 M text tower aligned to CLIP ViT-B/32 | Card lists `ru` among 50+ aligned languages; simple Russian-query-to-icon search |
| EmbeddingGemma 2 | Apache-2.0 | see above | One model for text and images; new |
| ColPali v1.3 (PaliGemma-3B) / ColQwen2.5 v0.2 (Qwen2.5-VL-3B) / ColSmol-256M | cards say MIT; **backbone licences NOT VERIFIED** (PaliGemma = Gemma terms family; ColQwen2 card says Qwen2-VL is Apache-2.0) | 3B-class, ColSmol 256M | trained on **English data by design** (ColSmol card: "fully English ... zero-shot generalization to non-English"); GPU-class; DVI paper: ColPali 20.1 % PageR@3 on homogeneous drawings. **Skip** for a few hundred pages |

Verdict: for icons, **text labels from the cheap-VLM batch + FTS5 are enough for search**; add DINOv2-small or SigLIP2 vectors only for "find similar" and variant grouping. For pages, **skip ColPali/ColQwen**; use words/BM25 + drawing-number index, and VLM on demand.

### 4.5 Vector stores (FACT, PyPI/GitHub 2026-11-12)

| Option | Licence | State | Verdict |
|---|---|---|---|
| NumPy brute force over BLOB column | - | - | 5,000 vectors x 1,024 dims x 4 B = **20.5 MB**; one query = 5.1 M multiply-adds -> milliseconds. **Enough up to ~100k vectors** |
| sqlite-vec 0.1.9 (2026-03-31) | MIT OR Apache-2.0 | pre-1.0; `vec0` is **FLAT brute force by default**, DiskANN opt-in (`INDEXED BY diskann`, compile flag) [S31]; wheels 0.2-0.3 MB incl. win_amd64; Python needs `enable_load_extension` | Optional, keeps everything in one SQLite file next to FTS5 |
| usearch 2.26.6 | Apache-2.0 | active | unnecessary at this scale |
| faiss-cpu 1.16.0 | MIT | 17-20 MB wheel | unnecessary |
| hnswlib 0.8.0 (2023-12), no wheels | - | stale | skip |

---------------------------------------------------------------------------------------------------

## 5. Controller connectivity: Schneider M241 / M251 (CODESYS V3)

Main primary source: **Schneider "Modicon M241 Logic Controller Programming Guide", EIO0000003059.11, 10/2026**, 228 pp., file `M241-Programming-Guide-EN-EIO0000003059-11.pdf`, retrieved from `https://download.se.com/files?p_Doc_Ref=EIO0000003059` on 2026-11-12 (Last-Modified 2026-10-28) [S23]. Text extracted locally; page numbers below are the guide's. **M251 guide could not be retrieved** (document reference unknown; guessed refs returned 403): M251 specifics are NOT VERIFIED except where CISA lists it with the same firmware line.

Note on the guide's own licence text (p.2): "Schneider Electric does not grant any right or license for commercial use of the document or its content, except for a non-exclusive and personal license to consult it on an 'as is' basis", and no part may be reproduced without written permission. **Do not copy this guide into a shared knowledge base** (no legal advice; check with Schneider).

### 5.1 Ports and default state (guide p.129, "Used Ports")

| Service | Port | Default in default project |
|---|---|---|
| Programming software (Machine Expert) | **TCP 1105, TCP 11740 (Fast TCP), UDP 1740** | Yes |
| FTP over TLS | TCP 21 | Yes |
| HTTP (auto-redirect) / HTTPS | TCP 80 / 443 | Yes |
| **Modbus TCP** | **TCP 502** | **No** |
| **OPC UA** | **TCP 4840** | **No** |
| Discovery | UDP 27126, 27127 | Yes |
| SNMP / NVL / EtherNet/IP / WebVisu | UDP 161 / UDP 1202 / TCP 44818+UDP 2222 / TCP 8080,8089 | No |

Security parameters of the Ethernet config (p.102): Discovery Active, FTP server Active, **"CoDeSys protocol" Active** ("when deactivated ... no connection is possible on Ethernet from a programming PC, from an HMI target that wants to exchange variables with this controller, from an OPC server, or from Controller Assistant"), **Modbus Server Inactive**, Remote connection (Fast TCP) Active, HTTPS Active, SNMP Inactive, WebVisu Inactive. Embedded firewall script example (p.128): `Firewall Eth1 Reject tcp port 502;`, `Firewall Eth1 Allow TCP port 11740;`. Max **10 simultaneous TLS contexts** shared by WebVisu/HTTPS, Machine Expert encrypted communication and Modbus TCP Secure (p.18). Firmware line today: **5.4.13.12 with Machine Expert v2.5.0.1** (CISA, below). The guide gives **no minimum firmware for the OPC UA server** (NOT VERIFIED; repo's `plclink.py` says "нужна прошивка с сервером OPC UA").

### 5.2 (i) OPC UA server in M241 (guide pp.176-183)

- Enable/configure: Devices tree -> double-click controller -> **"OPC UA Server Configuration"** tab; symbols: Application tree -> Add object -> **"OPC UA Symbol Configuration"** ("The logic controller starts the OPC UA server") (pp.177, 182).
- **Default: "encrypted communication with maximum security settings"**: Security policy default `Basic256Sha256`, message security `SignAndEncrypt`; options `None`, `Basic256 (deprecated)`, `Basic256Sha256`; modes None/Sign/SignAndEncrypt (p.178-179).
- **"Disable anonymous login": unchecked by default -> clients can connect anonymously** (p.179). With User Rights enabled OPC UA needs login/password; with User Rights disabled it accepts Anonymous; before any password exists "no login possible" (p.67). Groups include **"Function OPC UA"** (access to server), "Function Monitor" (monitoring IEC variables), "Function Variable" (read/write IEC variables), "Function Symbol Configuration" (p.68).
- Server port 4840 (0-65535); **max sessions 1-4, default 2**; max subscriptions/session 1-100 (20); max monitored items/subscription 1-1,000 (100), product <= 1,000; min publishing interval 200-5000 ms (1000); min KeepAlive 500-5000 (500); sampling rates up to 3 of 200-5000 ms (500/1000/2000); **identifier type Numeric (default) or String** (p.179-180).
- Client certificates: first connection is **quarantined**; a human must trust it in Machine Expert **View > Security Screen** (Devices -> Quarantined -> Upload -> Trusted -> Download) (p.180-181). Device certificate is regenerated when the IP changes (p.18).
- Symbols: **max 1,000**, each with access type **read-only (default)** / write-only / read-write; `%MX/%IX/%QX` not directly accessible (group them in the Relocation Table registers first); arrays <=3 dims, structs without UNION; type map BOOL->Boolean, INT->Int16, REAL->Float, STRING->String ... (pp.182-183). The guide **does not mention** the `{attribute 'symbol'}` pragma used by the repo (NOT VERIFIED for M241).
- Files on the controller: `/usr/App/settings.conf` (OPC UA configuration), `/usr/App/OpcUASymbolConf.map` (symbols), `/usr/Syslog/opcuatrace.log` (+5 rotations, 1,000 entries) (pp.26-27).
- Interop evidence with `asyncua`: GitHub issue #1520 (2023-12, closed): **Schneider M241 writes failed with `BadWriteNotSupported` until the DataValue was written without server timestamp** [S30a]. Issue #1594 (open, B&R PLC): `BadSessionNotActivated` with Basic256Sha256 + user/password; several users report same; workarounds: security None, old `python-opcua`, or asyncua 0.9.95 [S30b]. HYPOTHESIS: M241's default secure profile may hit the same class of problem; test early.

### 5.3 (ii) Modbus TCP (guide pp.102-103, 151-156, 23-29)

- Embedded server is in firmware, "accessible in RUNNING, STOPPED and EMPTY states"; **inactive by default**, enabled in the Ethernet config "Modbus Server" parameter (p.102-103).
- Function codes (embedded server): **1** read %Q, **2** read %I, **3** read holding (%MW), **6** write single (%MW), **8** diagnostics, **15** write multiple %Q, **16** write multiple (%MW), **23** read/write multiple (%MW), **43/14** read device identification. **FC 4 and FC 5 are not listed** (p.103). Time-consistency only per single 16-bit word.
- Embedded server addressed with **Unit ID 255**; the optional "Modbus TCP Slave Device" uses Unit ID 1-247 with %IW/%QW areas of 2-120 registers each (FC 3/6/16/23 only) (p.151-154). Port 502, changeable by SD-card script `changeModbusPort "1502"` (runs twice max, reverts after power cycle) (p.155-156). "Embedded Modbus connections may be closed when more than 8 connections are needed" (p.152).
- Map: `%MW0-%MW59999` application words; **`%MW60000-60199` system/diagnostic, Modbus-read-only**; **`%MW60200-61999` Read Relocation Table, read-only**; `%MW62000-62199` system/diag (R/W); **`%MW62200-63999` Write Relocation Table (R/W)**; `%MW64000-65535` reserved (p.24, 29). One Relocation Table object per controller regroups non-contiguous data into contiguous registers.
- Consequence for "read-only monitoring": a Relocation Table **read area (60200-61999)** is a purpose-built read-only window; Modbus has no authentication (use firewall/segmentation).

### 5.4 (iii) CODESYS online protocol, gateway, discovery, open-source clients

- Schneider calls it the "CoDeSys protocol"/"Machine Expert protocol"; ports **TCP 1105 and 11740 (Fast TCP), UDP 1740** (p.129) [S23]. CODESYS ScriptEngine reference: "`find_address_by_ip(address, port=11740)`", "`set_gateway_and_ip_address(gateway, ip, port=11740)` ... Omit to use the default port 11740"; the block driver is `CmpBlkDrvTcp` [S25]. Third parties: V3 runtimes "typically use TCP/11740 and/or UDP/1740"; the engineering software's **gateway protocol is TCP/1217** [S29]; reverse-engineering notes: TCP 11740-11743, UDP 1740-1743 [S27]. (The gateway port 1217 and "name service" for UDP 1740 are **third-party statements, not in the Schneider guide**.)
- **CVE-2025-13901** (CISA ICSA-26-078-01, 2026-03-10, updated 2026-03-19; CVSS 5.3): unauthenticated attacker can "occupy active communication channels" -> partial DoS on the **Machine Expert protocol**; affects M241/M251 firmware < **5.4.13.12** and M262 < 5.4.10.12; fix ships with Machine Expert v2.5.0.1; mitigations: filter ports/IP with the embedded firewall, encrypted links, VPN [S24]. Also ICSA-26-078-02 (XSS in web server, M241/M251 < 5.4.13.12, M258/LMC058 no fix).
- Open-source implementations (all research-grade, none maintained for production use):
  - `HAHermsen/codesys-runtime` (Apache-2.0, v0.1.0, pushed 2026-07-10, 0 stars): "clean-room" Python (stdlib only) client; verified byte-for-byte only against **one capture of runtime 3.5.14.10** (plaintext `0xCD55` frames); implemented: login/session, **file list/pull (backup)**; **app start/stop planned**; live direct-connect path "not yet exercised against a live unit"; **no variable read/write** [S27].
  - `microsoft/CoDe16` (MIT, **archived 2023-08**): Python `codesysv3_protocol` (channel, device, encryption, files_transfer, plcshell) inside a "CODESYS Version Extractor" from vulnerability research (CVE-2022-47379..); login + PLCShell; not for monitoring [S28].
  - Dissectors/scanners: `fridgebuyer/codesys3-dissector` (Lua, 3 stars, no licence), `Pavels-Dev/CODESYS-V3-discovery` (Nmap NSE), Wireshark main tree has no CODESYS dissector (checked tree), `reidmefirst/codesys-controllers` list [S29].
  - Commercial reimplementation exists: Weintek EBPro drivers "CODESYS V3 (Ethernet)" and "Schneider SoMachine M Series (Ethernet)" (release notes 6.10.02) [S33a].
  - HYPOTHESIS: M241 encrypts Machine Expert traffic (guide p.18 mentions "encrypted communication between the controller and Machine Expert"); the open clients handle plaintext of an old runtime only, so **they will not talk to a current M241 and must not be pointed at production hardware** (DoS CVE above, firmware/EULA risks).
- Conclusion: **no usable open-source client for CODESYS online services** (variable read/write/download). The supported ways are Machine Expert/CODESYS itself (ScriptEngine), OPC UA, Modbus.

### 5.5 (iv) Machine Expert / CODESYS ScriptEngine online API

From the CODESYS ScriptEngine API reference (copies: `greenforge-labs/codescribe` SP11 reference, `johannesPettersson80/codesys-api/scriptDoc.txt` newer) [S25]:
- Entry: `online.create_online_application(app)`; `online.set_default_credentials(user, pw)`, `set_specific_credentials(target, user, pw)`; `online.gateways`, `gateway.perform_network_scan()`, `find_address_by_ip`, `dev.set_gateway_and_ip_address(...)`.
- Application: `login(change_option, delete_foreign_apps)`, `logout()`, `read_value(expr)`, `read_values([..])`, `set_prepared_value`+`write_prepared_values()`, `force_prepared_values`, `start()`, `stop()`, `reset()`, `create_boot_application()`, `source_download()`, `is_logged_in`, `application_state`, `operation_state`, `timeout`.
- Device: `connect/disconnect`, `upload_file/download_file/delete_file/get_file_list_of_directory`, `download_source`, **`upload_source(archive_path)`**, `current_logged_on_username`, `reset_origin`, user management.
- `OnlineChangeOption`: Never=0 (full download), Try=1, Force=2, **Keep=3 "Try to login. Do not online update. Do not download. Keep as it is."** (newer doc; absent in SP11 copy; repo's `me_job.py` already uses `Keep` for read).
- Command line: `--noUI`, `--runscript`, `--textPrompts`; "when running via `--runscript` in `--noUI` mode, script exit also terminates CODESYS" [S25]. Third-party summary: `CODESYS.exe --noUI --runscript=watcher.py`; script code is **IronPython 2.7** (ASCII-only, no f-strings); needs CODESYS V3.5 "and its Scripting add-on" (tested SP21/SP22; "SP16 and later should work") [S26].
- **Machine Expert**: **NOT VERIFIED publicly that `LogicBuilder.exe` accepts `--runscript/--noUI/--profile`.** The best public MCP toolkit (131 stars, pushed 2026-11-10) says: "this toolkit has never been run against it [Machine Expert], so it isn't supported yet" and asks testers whether the program accepts those switches [S26]. `tarpas/codesys-bridge` (2025-04) installs a ScriptEngine command into Machine Expert V2.x ("Tools -> Scripting" menu exists there) [S26b]. The M241 guide: Machine Expert **cannot upload the executable/boot application** to a PC; program source can be uploaded only if it was stored on the controller at download time (`/usr/App/Archive.prj`) (p.26).
- Safety precedent: `codesys-mcp-toolkit` "never touches a real controller; the only online connection is to CODESYS's own simulation, and every online call checks for it first" [S26].
- Schneider product help for ScriptEngine (`product-help.se.com/.../ScriptEngine/...`) is cited by others but was **not reachable** (403).

### 5.6 (v) Licensing/EULA statements found (cite only; no legal advice)

- **CODESYS GmbH EULA** (engineering software, German, store.codesys.com, fetched 2026-11-12) §3: "Es ist nicht gestattet, die gelieferte Software zu bearbeiten bzw. zu verändern, zu modifizieren, zu disassemblieren, zu dekompilieren, andere Verfahren des Reverse-Engineering anzuwenden, den Lizenzierungsmechanismus zu umgehen oder diese Aufgaben Dritten zu überlassen" (except as required to exercise rights under included open-source licences); "Private" licence: no commercial use; "Gratis" software has no automated licence check [S32]. This is CODESYS's text, **not Schneider's Machine Expert EULA**.
- **Schneider Machine Expert EULA: NOT RETRIEVED** (no public URL reached). So whether headless scripting/automation or protocol reimplementation is permitted is **NOT VERIFIED**. The repo's own plan already says to check the Schneider licence before any "own Machine Expert" work (`docs/opo/TASKS.md:514-516`) and `machine-expert/TOOL.md` says the Schneider shell "needs a licence".
- Schneider guide legal notice (above): personal consultation licence only.
- Open clean-room reimplementations claim interoperability rights (HAHermsen README "Legality"): that is the author's statement, not a verified legal position.

### 5.7 Libraries

| Library | Licence | Latest | Async | Maturity notes |
|---|---|---|---|---|
| **asyncua** (OPC UA) | **LGPL-3.0(+)** | 2.1.0 (2026-10-06); 2.0 on 2026-06-05, 2.0.1 2026-06-29; py>=3.10; 1,515 stars | yes (asyncio) | Policies in source: None, Basic128Rsa15, **Basic256, Basic256Sha256**, Aes128Sha256RsaOaep, Aes256Sha256RsaPss (covers M241's None/Basic256/Basic256Sha256). `Client.set_security_string("Basic256Sha256,SignAndEncrypt,cert.pem,key.pem[,server_cert]")`, `set_user/set_password`, `get_node("ns=2;i=...")`, `read_value()`, browse, subscriptions [S31]. Known issues above. LGPL: fine as a pip-installed dependency in the per-tool venv; check if you ever freeze/bundle it |
| **pymodbus** | BSD-3-Clause (LICENSE file BSD-style; GitHub shows NOASSERTION) | 3.16.2 (2026-11-03); 2,797 stars; py>=3.10 | yes (`AsyncModbusTcpClient`) | Since **3.10.0 `slave=` was renamed `device_id=`** [S31]: e.g. `await client.read_holding_registers(10, count=2, device_id=255)`. Errors returned as response objects (`rr.isError()`), not exceptions |
| `e3series` | MIT | 27.10.0 (2026-09-08) | sync COM | Windows + E3 |

### 5.8 Safest staged path (read-only first)

Stage 0 (offline, no PLC): derive the expected tag map from the generated project (`plc_hmi_tags`) and verify it in Machine Expert **simulation** (`me_online simulation:true read:[...]`: exists, still unverified on live ME).
Stage 1 (passive/harmless): TCP-connect/ping to 4840/502/11740, Schneider discovery (UDP 27126/27127) or `me_scan`; no sessions.
Stage 2 (preferred first real contact): **OPC UA read-only** with `asyncua`: User Rights on, **"Disable anonymous login" ticked**, a dedicated user in group "Function OPC UA" only, Basic256Sha256 + SignAndEncrypt, certificate trusted by a human in the Security Screen, symbols exposed as **read-only (default)**; keep max sessions >= 2 (the panel uses one); poll with subscription >= 1 s; never write. Alternative/complement: Modbus FC 3 reads of the **Relocation Table read area %MW60200-61999** and diagnostics %MW60000-60199 (unit 255), on an isolated VLAN (no auth in Modbus).
Stage 3 (any variable without prior symbol config, i.e. "like Machine Expert"): ScriptEngine `login(OnlineChangeOption.Keep)` + `read_value`, through `me_online` (one session, logout in `finally`, serialize jobs, keep sessions short because of CVE-2025-13901's "occupy channels" behaviour; heavy: Machine Expert starts for minutes; needs Schneider licence/shell, unverified).
Stage 4 (writes): simulation only, then `confirm:true` per call with the engineer in the chat (already the repo's rule).
Stage 5 (download/start/stop/firmware): human only in Machine Expert (repo policy `ebpro-gui` and `me_online`).
Hygiene: firmware >= 5.4.13.12; leave embedded firewall rules on; do not point reverse-engineered clients (HAHermsen, CoDe16) at production controllers.

---------------------------------------------------------------------------------------------------

## 6. Weintek cMT side

FACT (primary sources: Weintek release-notes PDFs and doc pages, fetched 2026-11-12) [S33]:
- **EBPro 6.10.02** release notes: "Enhanced HMI security by requiring an account and password for **downloading, uploading, and connecting via cMT Viewer**"; "Auto account mode" kept for backward compatibility; explicit accounts admin/update/history/debugger/user (needs EasyWeb 2.0 >= 2.3.34 "Enhanced EasyBuilder Security"). New [Remote] page can switch off **Pass-Through, cMT Viewer and Diagnoser** and close their ports. Drivers touched: **[OPC UA Client]** (array alignment fix; fixed a "compilation failure when multiple drivers are used"; OPC UA methods support), **[Schneider SoMachine M Series (Ethernet)]** ("Fixed communication issues with newer firmware versions"), **[CODESYS V3 (Ethernet)]** ("fixed communication issues when using CODESYS V3.5 SP21"). EBPro also gained an "AI Assistant" (support Q&A). Also "Decompilation" is a documented feature (compile/decompile cycle).
- **OPC UA Client in EBPro** (`https://support.ihmi.net/iiot/opc-ua/opc-ua-client/`): System Parameters -> add OPC UA -> OPC UA Client; device settings: IP, port, "Security, Authentication...": server name, **security policy and message security mode "same as the server"**, authentication Anonymous / user+password / certificate+private key; session name; update mode **Request or Subscription**; tags via **"Get Tag Info..." (browses the server) or "Import Tags..." (.ini)**, export tags; supported types BOOL, BINARY, SINT, INT, DINT, REAL, LINT, BYTE, UDINT, WORD, DWORD, UINT, STRING; scalars, arrays, matrices; requires EBPro >= V5.06.01 and an Ethernet model (not iP series).
- **Fit with M241 (from the Schneider guide):** the panel needs: M241 OPC UA server enabled and symbols published; matching policy (default Basic256Sha256 + SignAndEncrypt); the panel's certificate **trusted in the Security Screen** (first connect is quarantined); a user in "Function OPC UA" (or anonymous if rights are off); the panel uses 1 of default **2 sessions**; node ids Numeric by default (repo already handles `i:` ids). Modbus TCP alternative: server must be switched on; embedded server unit id 255 (HYPOTHESIS: set the panel's station number to 255, not verified in Weintek docs); only FC 1/2/3/6/15/16/23 exist on the PLC side.
- **Getting a project onto the panel (what is documented):**
  - Weintek release notes require account+password for download/upload (above). cMT3162X spec page lists LAN1 1 Gbit, LAN2 100 Mbit, **USB Host x1**, COM/CAN (https://www.weintek.com/Product/Model/cMT3162X); the page does not state download methods.
  - **EasyAccess 2.0 CLI** `ea20_cli.exe` (Windows only, must be running, enable "CLI" in app settings): `--login/--login-saved`, `--get-hmi-list` (JSON), `--connect <hw_key>` (returns VPN IP; then VNC :5900, FTP, HTTP on that IP), `--set-hmi-passthrough`, exit codes 0/-1/-2; the doc says it can be integrated "with other tools such as **`easydownload_cli`** for remote project updates" (`https://support.ihmi.net/ea20/ea20-cli/`). Weintek news (2026-07-01, Weincloud V1.5): "Tasks such as project upload and download, backup, and pass-through configuration can be processed automatically in batches by running scripts" (https://www.weintek.com/About/NewsDetail/TechNews-Vol.78). **`easydownload_cli` documentation was not found** (NOT VERIFIED what it accepts/needs).
  - **EasyBuilder X** (new IDE, announced 2026-09-15, v1.01.376 notes): object-oriented, variable-centric; supports cMT X models with OS >= 20260715; a different project model than `.cmtp/.emtp` (https://www.weintek.com/About/NewsDetail/ProductNews-Vol.68). Out of current scope; watch for it.
  - **NOT VERIFIED (not in any retrievable doc):** EBPro "Download" dialog options (Ethernet / USB cable / USB disk or SD), "Compile" outputs (`.exob`), `cMT Viewer`/`Diagnoser` operation details, any EBPro command-line compile switch. Weintek manuals were not reachable (old `dl.weintek.com/public/...` paths 404; forum redirects to home).
- **What could be automated:** (1) everything up to the file: project generation (repo `ebpro-cmtp`) and EBPro **compile via GUI automation** (`ebpro-gui`); (2) tag import via OPC UA ".ini"/browse; (3) remote download only through EasyAccess CLI/`easydownload_cli` **if** the customer uses EasyAccess (needs account, VPN, credentials) - unverified; (4) keep final download + password windows human-only (current policy is right, and 6.10.02 makes it credentialed anyway).

---------------------------------------------------------------------------------------------------

## 7. Cross-area findings (brief, for other agents)

1. **Model lifecycle will break the router defaults** (FACT [S4, S9]): OpenAI **shut down `gpt-4.1-nano` on 2026-10-23** (OpenRouter still lists it but only via Azure); `gpt-5`, `gpt-5-mini`, `gpt-5-nano` removal **2026-12-11**; `gpt-4.1-mini`, `gpt-4.1`, `gpt-4o`, `gpt-4o-mini` removal **2027-01-14**; `gpt-5.4-nano` removal 2027-04-01; OpenAI's named replacement is `gpt-6-luna`. Google: `gemini-2.5-flash` and `gemini-2.5-pro` **auto-route on 2026-12-01** to `gemini-3.5-flash-lite` and `gemini-3.8-flash` (billed at those rates; 3.5-flash-lite has mandatory reasoning per OpenRouter JSON); `gemini-2.5-flash-lite` has no shutdown date. `app/net/gateway.py:35-39` uses exactly these as defaults/fallbacks; `app/doctext.py:91` and `settings_store.py:108` default vision to `gemini-2.5-flash`. HYPOTHESIS: part of "Jev Router fails" may be related; not verified here.
2. **Long-context price step** (FACT [S1]): OpenRouter lists for `openai/gpt-6.1-terra` an override at **prompt >= 272,000 tokens: input $2.00, output $7.50, cache read $0.10, cache write $2.50** (base $1.00 / $5.00 / $0.05 / $1.25). The owner's average is 273k input tokens per request (38.79M / 142) - right at the step. HYPOTHESIS: a good share of requests may be billed at the doubled rate; check the per-request `prompt_tokens` histogram against 272k.
3. OpenRouter also lists **time-of-day price overrides** for `deepseek/deepseek-v4.1-flash-1017` (weekday UTC windows appear to be 01:00-04:00 and 06:00-10:00 at 2x: $0.30/$1.20 vs $0.15/$0.60; encoding `utc_start` 100/400/600/1000 read as HHMM - interpretation not verified) [S1]. Also reasoning is enabled by default (effort high) on that model.
4. **MCP tool-definition bloat is real and acknowledged by tool authors**: `Canary-ECAD/eplan-mcp` publishes 199 tools in `full` mode and a `discovery` mode with **13 tools** "trading a token-heavy tool list for one extra search round-trip ... worth it for MCP clients that send every tool's full schema on every request"; it says **Claude Code already defers tool schemas**, so discovery is not needed there [S35a]. The SYSAI agent runs Hermes (per brief), whose behaviour is not documented in what I fetched (NOT VERIFIED).
5. Current Machine Expert is **v2.5.0.1** with firmware **5.4.13.12** (CISA 2026-03); the repo's verified format work is from Machine Expert 2.2 (CODESYS 3.5.19.40) (`hub/seed/plc-kit/TOOL.md:24-28`). Worth confirming the generated `.project` against 2.5.x.
6. Weintek EBPro 6.10.02 introduced account/password for download; the Weintek "Picture Library" (BMP/GIF/PNG/SVG sets: Blower, Motor, Pumps, Tanks, Valves, Hoppers...) is published at `https://www.weintek.com/Download/Library` (assets under `dl.weintek.com/wksite/PictureLibrary/`) - licence terms not read.

---------------------------------------------------------------------------------------------------

## 8. Recommendations (ranked; effort in engineer-days, my estimate)

Adopt:
1. **Deterministic drawing reader first ("drawing-kit", stage 7 core), no LLM** - 6-10 d for PDF words/lines/title-block/table baseline + DXF (ezdxf) + XLSX; outputs compact per-sheet JSON "cards" cached by `sha256`; agent never sees raw images except by request. Decide **PyMuPDF (AGPL/commercial) vs pypdfium2+pdfplumber (permissive)** first (0.5 d). Evidence: DVI paper, ChatP&ID, PyMuPDF/pdfminer speed. Biggest lever on cost *and* accuracy.
2. **If E3.series is on the engineer's PC: COM export script** (`e3series`, MIT) dumping devices, pins, connections, PLC channels to CSV/JSON - 2-3 d + one pilot project. Exact data; no OCR. (EPLAN: only if customers use it; AML/XML export NOT VERIFIED.)
3. **Icon labels by a cheap-VLM batch into the existing sha1 label store** (`pics_describe`: contact sheets or 1 icon/request, model `gemini-2.5-flash-lite` or `gpt-6-luna`/`claude-haiku-5.5`, reasoning off) - 1.5-3 d; **~$0.03-0.08 per 500 icons**; then pic-index + FTS5 search by text. Add DINOv2-small/SigLIP2 vectors only if "similar icon" is needed (+2 d).
4. **Index store = SQLite FTS5 (+ Snowball-stemmed RU, ё->е; trigram/`tokenchars` for tags), NumPy vectors in BLOB, RRF fusion** - 2-4 d. Embeddings optional: start with hosted `baai/bge-m3` ($0.01/M tokens) or local `multilingual-e5-small`; skip FAISS/usearch/sqlite-vec until >100k vectors.
5. **Targeted vision service** for what remains (scans, symbols, graphic-only connectivity): region crops/tiles at >=200 DPI, 10 % overlap, no zoom-out, per-crop cache keyed by hash+prompt+model, cheap model, Batch/flex, results flagged "проверить" - 3-5 d. ~$0.2-0.5 per 100 sheets (ESTIMATE).
6. **Read-only controller monitor** (`plc-monitor` hub tool with `requirements.txt: asyncua`, optional pymodbus FC 3) following the staged path in 5.8; **no write functions in v1** - 4-6 d + 1-2 d live testing with a real M241 (asyncua secure-session risk). Requires the generated project to enable OPC UA server + symbols read-only.
7. **First real run of `me_scan/me_online` and `--runscript` on a licensed Machine Expert with the engineer present** - 0.5-1 d; it unblocks both compile (cross-area) and Stage 3 reads. Record Machine Expert version (2.5.0.1?), switches accepted, licence prompts.
8. **Weintek: keep download human-only**; add a pre-flight checklist (account/password mode, OPC UA policy match, certificate trust, sessions) to the project hand-off; research `easydownload_cli`/EasyAccess CLI only if remote updates are wanted - 1 d research.
9. (Urgent hygiene, cross-area) Replace model defaults/fallbacks that die on 2026-10-23 / 12-01 / 12-11 / 2027-01-14 - 0.5-1 d.

Skip (for now):
- **Surya** (weights licence, VLM-heavy), **docTR** (no Russian weights), **EasyOCR** (stale), **ColPali/ColQwen** (English-trained, GPU, weak on homogeneous drawings per DVI), **jina-embeddings-v3 / jina-clip-v2** (CC-BY-NC), **EmbeddingGemma-300m** (Gemma terms; v2 is Apache but 2 weeks old), FAISS/usearch/hnswlib.
- **Any reverse-engineered CODESYS client against production controllers** (HAHermsen, CoDe16): old runtime only, DoS CVE on the channel layer, EULA/licence uncertainty.
- Reimplementing the Machine Expert protocol or `.project` writing beyond what plc-kit already does; first obtain the Schneider licence text (`TASKS.md:514-516` already says so).
- LibreDWG bundling (GPL); prefer ODA File Converter as an external program after reading its licence.
- Dumping Schneider guides into a shared RAG (personal-use licence text).

---------------------------------------------------------------------------------------------------

## 9. NOT VERIFIED / unreachable

- Schneider **Machine Expert EULA**; Machine Expert command-line switches (`--runscript` etc.); Schneider ScriptEngine product help (403); M251 programming guide; OPC UA minimum firmware for M241.
- E3.series PDF/XML/CSV export properties (text layer or outlines?); EPLAN export formats (docs 404); AutomationML export from EPLAN versions.
- Weintek: EBPro manual (download dialog, USB/SD options, compile outputs, command line), `easydownload_cli` docs, cMT Viewer/Diagnoser docs, Modbus driver station-number advice for M241.
- OCR accuracy on GOST/Russian drawings (no public benchmark found); real CPU speeds for embedding models (not run); FTS5 availability in the engineer's Windows Python.
- `gpt-6-luna` image multiplier (assumed 1.2); OpenRouter `pricing.image` unit (assumed per token).
- Hermes behaviour on MCP tool schemas.

---------------------------------------------------------------------------------------------------

## 10. Source key

Fetched 2026-11-12 unless another date is given.

- S1 OpenRouter models: https://openrouter.ai/api/v1/models (12:39 UTC; local `/tmp/research/g-data/openrouter/models.json`); embeddings list: https://openrouter.ai/api/v1/embeddings/models
- S2 OpenRouter endpoints: https://openrouter.ai/api/v1/models/{openai/gpt-4.1-nano | openai/gpt-6-luna | google/gemini-2.5-flash-lite}/endpoints
- S3 OpenAI images/vision: https://developers.openai.com/api/docs/guides/images-vision ; S3b prompt caching: https://developers.openai.com/api/docs/guides/prompt-caching ; pricing: https://developers.openai.com/api/docs/pricing
- S4 OpenAI deprecations: https://developers.openai.com/api/docs/deprecations
- S5 Gemini image understanding: https://ai.google.dev/gemini-api/docs/image-understanding (-> aistudio.google.com/docs/image-understanding)
- S6 Gemini media resolution: https://ai.google.dev/gemini-api/docs/media-resolution
- S7 Claude vision: https://platform.claude.com/docs/en/build-with-claude/vision ; S8 resize rule: https://platform.claude.com/docs/en/build-with-claude/vision-coordinates
- S9 Gemini deprecations: https://ai.google.dev/gemini-api/docs/deprecations (-> aistudio.google.com/docs/deprecations)
- S10 OpenRouter docs: https://openrouter.ai/docs/guides/overview/multimodal/pdfs ; https://openrouter.ai/docs/guides/features/batch ; https://openrouter.ai/docs/api-reference/embeddings/create-embeddings ; https://openrouter.ai/docs/guides/overview/models
- S11 PyPI JSON API (`https://pypi.org/pypi/<pkg>/json`) for PyMuPDF, pypdfium2, pdfplumber, pdfminer.six, pypdf, camelot-py, ezdxf, paddleocr, rapidocr, python-doctr, surya-ocr, easyocr, ocrmypdf, docling, asyncua, pymodbus, e3series, snowballstemmer, PyStemmer, pymorphy3, sqlite-vec, usearch, faiss-cpu, hnswlib, sentence-transformers, fastembed, onnxruntime, open_clip_torch
- S12 GitHub REST API (repo metadata, releases, code search, issues), e.g. https://api.github.com/repos/pymupdf/PyMuPDF
- S13 PaddleOCR docs: https://github.com/PaddlePaddle/PaddleOCR/blob/main/docs/version3.x/algorithm/PP-OCRv5/PP-OCRv5_multi_languages.en.md and `.../PP-OCRv5.en.md`; S13b RapidOCR: https://rapidai.github.io/RapidOCRDocs/main/model_list/
- S14 Surya: https://github.com/datalab-to/surya (README, MODEL_LICENSE)
- S15 docTR: https://github.com/mindee/doctr/blob/main/doctr/datasets/vocabs.py and `doctr/models/recognition/crnn/pytorch.py`
- S16 PyMuPDF: https://pymupdf.readthedocs.io/en/latest/about.html
- S17 ODA File Converter: https://www.opendesign.com/guestfiles/oda_file_converter ; S18 LibreDWG: https://github.com/LibreDWG/libredwg ; S19 ezdxf: https://ezdxf.readthedocs.io/en/stable/addons/odafc.html
- S20 arXiv: (a) https://arxiv.org/abs/2602.14162 (2026-02-15); (b) https://arxiv.org/abs/2610.09313 (2026-10-07); (c) https://arxiv.org/abs/2609.05880 (2026-09-05); (d) https://arxiv.org/abs/2603.22528 (2026-03-23); (e) https://arxiv.org/abs/2604.00270 (2026-03-31)
- S21 Anthropic Contextual Retrieval: https://www.anthropic.com/news/contextual-retrieval (published 2024-09-19)
- S22 Hugging Face API/cards: https://huggingface.co/api/models/<id> and `https://huggingface.co/<id>/raw/main/README.md` for BAAI/bge-m3, Qwen/Qwen3-Embedding-0.6B, intfloat/multilingual-e5-{small,base,large}, google/embeddinggemma-{300m,2}, ibm-granite/granite-embedding-{97m,311m}-multilingual-r2, jinaai/jina-embeddings-v3, jinaai/jina-clip-v2, nomic-ai/nomic-embed-text-v2-moe, deepvk/USER-bge-m3, google/siglip2-*, sentence-transformers/clip-ViT-B-32-multilingual-v1, facebook/dinov2-{small,base}, vidore/colpali-v1.3, vidore/colqwen2.5-v0.2, vidore/colSmol-256M, PaddlePaddle/*PP-OCRv5*
- S23 Schneider M241 Programming Guide EIO0000003059.11 (10/2026): https://download.se.com/files?p_Doc_Ref=EIO0000003059 (local `/tmp/research/g-data/schneider/`, not for redistribution)
- S24 CISA ICSA-26-078-01 https://www.cisa.gov/news-events/ics-advisories/icsa-26-078-01 (JSON: https://github.com/cisagov/CSAF/blob/develop/csaf_files/OT/white/2026/icsa-26-078-01.json), ICSA-26-078-02 (same folder); Schneider SEVD-2026-069-01
- S25 CODESYS ScriptEngine reference copies: https://github.com/greenforge-labs/codescribe/blob/main/docs/references/codesys_scriptengine_v3.5_sp11_api_reference.md ; https://github.com/johannesPettersson80/codesys-api (scriptDoc.txt)
- S26 https://github.com/johannesPettersson80/codesys-mcp-toolkit (README.md, docs/machine-expert-testing.md; pushed 2026-11-10); S26b https://github.com/tarpas/codesys-bridge (2025-04)
- S27 https://github.com/HAHermsen/codesys-runtime (README, docs/protocol-notes.md; pushed 2026-07-10); S28 https://github.com/microsoft/CoDe16 (archived 2023-08); S29 https://github.com/reidmefirst/codesys-controllers
- S30 asyncua issues: (a) https://github.com/FreeOpcUa/opcua-asyncio/issues/1520 ; (b) https://github.com/FreeOpcUa/opcua-asyncio/issues/1594
- S31 context7 (library docs): pymodbus https://github.com/pymodbus-dev/pymodbus/blob/dev/doc/source/api_changes.md and examples.md; sqlite-vec https://github.com/asg017/sqlite-vec (examples/nbc-headlines/3_search.ipynb, sqlite-vec.c); asyncua https://github.com/freeopcua/opcua-asyncio (client.py `set_security_string`); PyMuPDF https://pymupdf.readthedocs.io/en/latest/app1.html and page.html; pypdfium2 https://pypdfium2.readthedocs.io/en/stable/python_api.html; ezdxf https://ezdxf.readthedocs.io/en/stable/blocks/insert.html
- S32 CODESYS EULA (German): https://store.codesys.com/media/n98_media_assets/files/1101000000/3/CODESYS_EULA_Engineering_de.pdf
- S33 Weintek: (a) https://dl.weintek.com/Software/EasyBuilderPro/ReleaseNote/EBPro-6.10.02-ReleaseNotes-eng.pdf (also 6.10.01, 6.09.02); EasyBuilder X 1.01 notes https://dl.weintek.com/Software/EasyBuilderX/ReleaseNote/EasyBuilder-X-1.01-ReleaseNotes-eng.pdf ; (b) https://support.ihmi.net/iiot/opc-ua/opc-ua-client/ ; (c) https://support.ihmi.net/ea20/ea20-cli/ ; (d) https://www.weintek.com/About/NewsDetail/TechNews-Vol.78 (2026-07-01), .../ProductNews-Vol.68 (2026-09-15) ; (e) https://www.weintek.com/Product/Model/cMT3162X
- S34 (a) https://pypi.org/project/e3series/ ; (b) https://github.com/Zuken-E3-GmbH/E3SeriesPythonExamples ; (c) https://www.zuken.com/en/product/e3series/ ; (d) https://github.com/bydavidgenie/Zuken-E3.series-MCP
- S35 (a) https://github.com/Canary-ECAD/eplan-mcp ; (b) https://github.com/EPDF-Extractor/indu-doc-transformer ; (c) https://github.com/AutomationML/automationml-python

Local evidence and scripts (not in any git repo): data in `/tmp/research/g-data/` (one sub-directory per download: `openrouter`, `openai-vision`, `openai-deprec`, `gemini-docs`, `gemini-deprec`, `schneider` (M241 guide + extracted text), `codesys-eula`, `weintek-rn`, `ihmi`, `hf-cards*`, `cisa`, `gh-*`); scripts in `/tmp/research/g-scripts/`: `icon_cost.py` (section 3.3), `sheet_tokens.py` (section 2.6), `fts5_ru_test.py`, `fts5_tags_test.py` (section 4.1), `pypi_info.py`, `hf_info.py`, `or_inspect.py`, `pdf2txt.py`. Run any Python on downloaded data with `python3 -I`.
