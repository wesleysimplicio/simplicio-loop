# Changelog

All notable changes to **simplicio-loop** are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); the project uses SemVer.

## [Unreleased]

### Added — loop attempt-memory + stall detector (`scripts/loop_journal.py`)
- **Durable run-journal** `.orchestrator/loop/journal.jsonl` (append-only: `iteration`, `action`,
  `hypothesis`, `gate`, error `fingerprint`) — the loop's working memory of WHAT WAS TRIED, beside
  the scratchpad's WHAT (the goal). Closes the two failure modes of a memoryless re-feed loop:
  re-deriving the same triage every turn, and **oscillation** (try X → fail → try X again).
- **Stable error fingerprint** — failing gate output is hashed with line numbers, paths, hex/uuids,
  timestamps and durations normalized away, so the SAME bug is recognized across turns.
- **Stall detector** — `loop_journal.py stall`: STALLED when the last K consecutive attempts share
  the same fingerprint (default K=3); names the dead-end actions and recommends `switch-strategy`
  (K) or `escalate` (>K), `--exit-code` 10 for hook/`if:` gating. `resume` prints the anti-oscillation
  read at the top of each turn. Deterministic + model-free; `selftest` proves it (9/9, no files).
- Wired into `simplicio-loop` (loop contract steps 2–4, new § Run-journal + stall detector, the
  "good loop" criteria) and `simplicio-tasks` (Step 4 quality loop). Docs: README, AGENTS, CLAUDE;
  `_bundle` synced.

### Added — billing aggregator for the open-core paid tier (`scripts/billing_aggregator.py`)
- **Deterministic, model-free, privacy-preserving meter→invoice** over the metering records the loop
  already produces (`loop-budget.json`, `savings/snapshots.jsonl`, `trajectory/*.jsonl`,
  `tee/video/ledger.txt`). Verbs: `collect`/`meter`/`invoice`/`export`/`rates`/`selftest`.
- **Privacy boundary**: the savings snapshots store raw baseline/treatment TEXT; `collect` counts
  tokens (`ceil(chars/4)`) then **discards** the text — usage records carry counts only, never code,
  diffs, or rendered videos. **Fail-safe**: `invoice --prepaid` over-balance maps to the existing
  kill-switch `state: "halted"` (never over-serves). `selftest` proves the arithmetic (11/11, no files).
- Three price levers: per-seat (Pro), per-run (Team, one delivered+merged item), metered (Cloud:
  token passthrough + markup, render-minutes, operator-minutes). Implements the `PRICING.md` sketch.

### Added — demo-video creation + evidence via hyperframes (`video_evidence`, extension point #44)
- **`video_evidence` extension point** — binds [hyperframes](https://github.com/heygen-com/hyperframes)
  (HeyGen): renders HTML/CSS compositions to a **deterministic MP4** ("same input, same frames, same
  output"). Two jobs: (1) fulfil an explicit request — `/simplicio-tasks faça um vídeo demonstrativo
  da tela X` routes the work-item to the producer; (2) act as a CI-reproducible "works, not just
  compiles" proof for a UI change and a valid evidence-gated `<promise>` for the loop.
- **Worker** `scripts/video_evidence.py` — five verbs (`detect`/`scaffold`/`lint`/`render`/`verify`).
  `detect` classifies the request in-terminal (EN/PT/ES regex, no LLM); `verify` scaffolds a
  hyperframes composition from the `web_verify` per-step screenshots and renders the MP4 under
  `.orchestrator/tee/video/`. Missing toolchain (Node 22+, FFmpeg, hyperframes) → **BLOCKED**, never
  a fake pass. Chains after `web_verify` (Playwright captures the screens; hyperframes assembles them).
- **Contract** `.claude/skills/simplicio-tasks/references/video-evidence.md`; wired into
  `simplicio-tasks` (Step 2b routing + Step 4b evidence) and `simplicio-loop` (in-turn evidence
  producer). Extension-point count 43 → 44; skills/accelerators 10 → 11. Docs updated: README (EN +
  pt-BR), AGENTS.md, CLAUDE.md. Bundled skills under `simplicio_loop/_bundle/` synced.

## [3.3.0] — 2026-06-24

### Added — automatic capture routing for Claude + Codex (the monitor now measures all three)
- **`simplicio-economy.sh wire` now routes Claude (Anthropic) AND Codex/OpenAI through the capture
  proxy**, not just OpenAI — so the Token Monitor measures **Hermes + Claude + Codex** with no manual
  step. It sets `ANTHROPIC_BASE_URL` (no `/v1` — Claude appends `/v1/messages`) and `OPENAI_BASE_URL`
  (`/v1`) in the shell profile; `install_services.py wire` does the same cross-platform (`setx` on
  Windows). The engine routes each model to its **real** provider (`claude→anthropic`, `gpt→openai`,
  `deepseek→deepseek`) — **no model swap**. `setup_simplicio.sh` runs `wire` at install, so it is
  automatic.
- **Verified live**: through the proxy, an unauth'd `claude-3-5-sonnet` request returned Anthropic's
  own auth error + `request_id`, and a `gpt-4o-mini` request returned OpenAI's 401 — proving
  transparent forwarding to the real providers. `status` now shows `Claude ✓ · Codex/OpenAI ✓ · Hermes ✓`.
- **Idempotent · reversible · opt-outable**: re-running `wire` doesn't duplicate; `unwire` deletes the
  proxy routing deterministically (fixed a bug where a re-wire could poison the backup); a pristine
  `~/.zshrc.simplicio-bak` is kept; `SIMPLICIO_NO_WIRE=1` skips wiring entirely.

## [3.2.3] — 2026-06-24

### Changed
- README: added the "Running `simplicio-tasks`: economy vs measurement (per runtime)" subsection —
  economy applies on every runtime; measurement only counts traffic routed through the capture proxy.

## [3.2.2] — 2026-06-24

### Changed
- Synced all 14 translated READMEs to the comprehensive English README (capture-engine commands, ONNX
  models, Rust core, Token Monitor, corrected token-economy table). Tracked the project `.codex/` config.

## [3.2.1] — 2026-06-24

### Changed
- Comprehensive, transparent English README: documented the full capture-engine command surface (16
  commands), the 4 optional ONNX models, and the 4 native Rust crates.

## [3.2.0] — 2026-06-24

### Added — the two token-economy techniques the README claimed but didn't implement, now real (2 agents)
- **Signatures-only reads** — `engine/simplicio_signatures.py` + `simplicio signatures <file>`: an
  `ast`-based skeleton view (imports, class/def signatures, first docstring line, top-level consts;
  bodies stripped to `...`), regex fallback for js/ts/go/java/rust/…. Verified: `simplicio_dashboard.py`
  **870 → 65 lines (93% saved)** with every `def`/`class` preserved and no body leakage. Saves the
  tokens to read+navigate code.
- **Native response cache** — `engine/simplicio_cache.py`, wired into the capture proxy: a repeated
  **deterministic** request (`temperature == 0`, non-streaming) is served byte-exact from disk and the
  upstream LLM call is **skipped entirely → ~100% token saving on the hit**. Content-addressed key
  ignores volatile fields (`stream`/`user`/ids); LRU-bounded (500 entries / 50 MB); never caches 4xx or
  streaming/temp>0. On by default (`SIMPLICIO_CACHE=0` to disable). Verified end-to-end: an identical
  second request returned `X-Simplicio-Cache: HIT` with **zero** upstream calls. This also makes the
  dashboard's `cache_hit_pct` real (it was always 0). `simplicio cache stats|clear`.

### Changed
- README token-economy table corrected to reality: `CAP_TREE=100` → the real caps
  (`CAP_ERRORS/CAP_WARNINGS/CAP_LIST`); the `LMCache KV cache` row (an *external* optional accelerator,
  never built-in code) replaced by the now-implemented **native response cache**; signatures listed as
  the real `simplicio signatures` tool.

## [3.1.0] — 2026-06-24

### Added — the last two Rust crates (the port is now literally complete: every crate)
- **`rust/simplicio-proxy`** — the upstream `headroom-proxy` (Rust/axum transparent reverse proxy),
  vendored + rebranded (zero residual `headroom`; `x-headroom-`→`x-simplicio-`). **Built (40.8 MB
  binary) and verified running**: it forwarded a request to a local upstream byte-exact, preserved +
  injected headers (`x-forwarded-*`, `x-request-id`), rewrote `host`, and `/healthz` →
  `{"ok":true,"service":"simplicio-proxy"}`. **227 lib unit tests pass.**
- **`rust/simplicio-parity`** — the upstream `headroom-parity` Rust-vs-Python parity harness, vendored
  + rebranded + built (`parity-run` binary, 7 transforms). **4 parity tests pass.**
- (Honest: the proxy's 50 *integration* test binaries couldn't finish linking here — disk-full, ~200 MB
  free; each statically links the ~40 MB ONNX/AWS tree. The release binary + lib tests built and passed.)

### Done — every subsystem AND every crate of upstream headroom is now in Simplicio
All four Rust crates (`simplicio-core` / `-py` / `-proxy` / `-parity`) build; the full Python functional
surface runs; the four real ONNX models (kompress / technique-router / MiniLM / SigLIP) run; Copilot
OAuth works. **headroom → simplicio: complete.**

## [3.0.0] — 2026-06-24

### Added — the native Rust performance core, built for real (the last literal piece)
- **`rust/simplicio-core` + `rust/simplicio-py`** — the upstream headroom Rust crates
  (`headroom-core` + the PyO3 bindings `headroom-py`), **vendored and rebranded** headroom→simplicio
  (~70 source files: smart_crusher, diff/log/search compressors, tokenizer, relevance, CCR, content
  detection), Apache-2.0 with `NOTICE` crediting upstream. The rebrand is baked into the compiled
  binary, not cosmetic (`hello()` → `simplicio-core`, tag sentinel `{{SIMPLICIO_TAG_…}}`, env
  `SIMPLICIO_*`).
- **It builds and runs.** `maturin build --release` produced a real wheel
  (`simplicio_core-…-abi3-…arm64.whl`); `import simplicio._core` works and real functions run
  (`LogCompressor` 5700→566 bytes, `SmartCrusher`, `DiffCompressor`, `detect_content_type` via magika).
  **843 Rust tests pass.** Built with rustc/cargo 1.95.0 + maturin 1.14.1.

### Milestone — the headroom port is complete (capability + the native layer)
Every subsystem of the upstream headroom repository is now in Simplicio: the full Python functional
surface (deterministic + extractive compression, the **four real ONNX models** kompress /
technique-router / MiniLM embedder / SigLIP image, content detection + smart routing, RAG, input+output
capture, per-provider routing, MCP, CCR memory, init/wrap/report/verify/audit/capture/evals, copilot
OAuth) **and** the native Rust performance core. headroom→simplicio: done. (Skipped only `headroom-proxy`
— a Rust axum passthrough that duplicates the working Python proxy — and `headroom-parity` — a test
harness; both are non-capability.)

## [2.12.0] — 2026-06-24

### Added — Copilot OAuth (the last functional subsystem)
- **`simplicio copilot {login|token|status|logout}`** (`engine/simplicio_copilot.py`, stdlib) — GitHub
  Copilot OAuth **device flow** + Copilot token exchange, so Copilot CLI traffic can be routed through
  the capture proxy. Verified **live against the real GitHub API**: the device-code handshake returned a
  real `device_code`/`user_code`/`verification_uri`, and the poll returned the expected
  `authorization_pending`; `status`/`logout`/empty-store paths verified; token stored 0600 under
  `~/.simplicio`. (Honest: the post-auth Copilot token exchange can't be exercised here without a real
  Copilot account; the code path mirrors upstream exactly.)

### Milestone — capability-complete
With Copilot auth, **every functional subsystem of the upstream headroom repo is now ported to
Simplicio** and verified: all deterministic + extractive compression, the **four real ONNX models**
(kompress / technique-router / MiniLM embedder / SigLIP image), content detection + smart routing, RAG
(TF-IDF + embedding), input+output capture, per-provider routing, MCP, CCR memory, client init, wrap,
report, verify, audit, capture, evals, and copilot-auth. The **only** thing not reimplemented is the
upstream's Rust `crates/` — a pyo3 **performance** re-implementation of the Python (`headroom-parity`
asserts Rust == Python), which adds **no new capability**, only native speed.

## [2.11.0] — 2026-06-24

### Added — image compression (the 4th and last real headroom model)
- **`simplicio image <path>`** (`engine/simplicio_image.py`) — vision-LLM image compression ported from
  headroom's `image/` subsystem (techniques preserve/full_low/crop/transcode = aspect-preserving LANCZOS
  downscale + efficient re-encode), using the **REAL** `chopratejas/siglip-image-encoder-onnx` (~94 MB)
  as a content-similarity verifier so compression never destroys content. Verified: 1600×1200 → 768×576,
  90.6% bytes saved, SigLIP cosine ~0.997; a 512px tier cuts OpenAI vision tokens ~67%. Pillow-only
  fallback works without the model. (`[onnx]` extra now includes pillow.)
- **All four real headroom ONNX models now run inside Simplicio**: kompress-v2-base (compression),
  technique-router-onnx (routing), all-MiniLM-L6-v2-onnx (embeddings), siglip-image-encoder-onnx (image).

### Scope note — the Rust crates
The upstream's `crates/` (headroom-core/proxy/py) are a **pyo3 performance re-implementation** of the
Python — `headroom-parity` literally asserts Rust == Python. They add **no new capability** (just native
speed). The functional surface they cover is already in Simplicio's Python engine, so there is no
*capability* gap there — only an optional native-speed rewrite, which is out of scope for the token monitor.

## [2.10.0] — 2026-06-24

### Added — more upstream subsystems ported (3 agents; 2 more REAL headroom models)
- **`simplicio detect`** (`engine/simplicio_detect.py`, stdlib) — content-type detector (JSON/code/log/
  markdown/prose) + a **universal smart-compress** that routes each block to the best technique
  (JSON→minify, log→full pipeline, code/prose left intact). Verified 15/15: JSON 60%, log 95% saved,
  code/prose byte-preserved.
- **`simplicio router`** (`engine/simplicio_router.py`) — the **REAL** `chopratejas/technique-router-onnx`
  model (~32 MB, INT8): tokenize → ONNX → softmax → technique class (transcode/crop/preserve/full_low).
  Verified running on the real weights. (Note: this router was trained on image-edit *intents*, so raw
  text blobs tend to route to `preserve` — the model runs correctly; its training domain differs.)
- **`simplicio embed`** (`engine/simplicio_embed.py`) — the **EXACT** upstream embedder
  `Qdrant/all-MiniLM-L6-v2-onnx` (~90 MB): masked mean-pooling → 384-dim L2-normalized vectors;
  embedding RAG over the CCR store. Verified: paraphrase cosine **0.957**, unrelated −0.01, #1 rank.
- New `[onnx]` optional extra installs onnxruntime + huggingface_hub + tokenizers for `kompress`/`router`/`embed`.

## [2.9.0] — 2026-06-24

### Added — the REAL headroom ONNX compression model, integrated (the gap is closed, not substituted)
- **`simplicio kompress`** (`engine/simplicio_kompress.py`) runs the **actual upstream model**
  `chopratejas/kompress-v2-base` — the real ONNX semantic token-pruning model headroom uses. Turns out
  its weights are **public on HuggingFace** (Apache-2.0), not proprietary: so this is the genuine
  article, not a look-alike. It tokenizes (ModernBERT), runs the ONNX session
  (`input_ids`/`attention_mask` → per-token `final_scores` keep probability), keeps the top
  `--keep` fraction of words, drops filler, and reconstructs — **reversibly** (the dropped spans are
  retained). Verified with the real model: e.g. `--keep 0.5` → 48.7% words pruned, high-signal tokens
  (identifiers, numbers, errors) preserved.
- Opt-in: `pip install "simplicio-loop[kompress]"` (onnxruntime + huggingface_hub + tokenizers; the
  ~274 MB model downloads on first use). Without it, `simplicio kompress` reports how to enable it.

### Fixed
- Engine CLI now forwards sibling-command args **verbatim** (raw passthrough) — `argparse` REMAINDER was
  mangling `--flag value` ordering (e.g. `kompress --keep 0.5` arrived as `0.5 --keep`).

### Scope — now honestly complete on the implementable + the model
With the real `kompress-v2-base` integrated, the upstream's ONNX semantic compression is no longer a
gap — it's the same model, in Simplicio. Combined with the deterministic 12-algo + extractive
compression, the model2vec embedding backend, and TF-IDF/embedding RAG, the headroom compression+RAG
surface is covered (deterministic core stdlib-only; the heavy models are optional extras).

## [2.8.0] — 2026-06-24

### Added — REAL embedding ML backend (the ML gap, done honestly — not stubbed)
- **`engine/simplicio_semantic_ml.py`** — an optional, dependency-gated embedding backend using a
  **real sentence-embedding model** (`model2vec`, static embeddings, ~30 MB, no torch):
  - **`simplicio semantic --ml`** — embedding **semantic dedup**: drops paraphrased / semantically
    redundant lines that TF-IDF + SimHash can't catch, **reversibly** (byte-exact restore). Verified
    with the real model: paraphrase cluster → 27-40% saved, round-trip OK.
  - **`simplicio rag --ml "<query>"`** — retrieval by **meaning** (embedding cosine), not keyword.
    Verified: matched a query to a lexically-disjoint memory (cosine 0.42, ranked #1) — a match
    TF-IDF would miss.
- **Opt-in + graceful**: needs `pip install "simplicio-loop[ml]"` (model2vec + numpy). Without it,
  `--ml` prints how to enable it and the system falls back to the deterministic `semantic`/`rag`.
  The native engine itself stays **stdlib-only / zero-dependency**.
- Added the `[ml]` optional-dependency extra; `--ml` routes via `parse_known_args` passthrough.

### Honest note
This uses a *real* trained embedding model (so semantic similarity genuinely works — paraphrases
match, unrelated text doesn't). It is the light static-embedding tier; a larger model catches more
paraphrase. It is NOT a reimplementation of the upstream's specific trained ONNX compression model
(that exact model isn't replicable) — but the ML *capability* (semantic compression + meaning
retrieval) is now real and verified, behind an optional dependency.

## [2.7.0] — 2026-06-24

### Added — semantic-lite compression + RAG (the honest take on the ML gap)
- **`simplicio semantic`** (`engine/simplicio_semantic.py`) — **reversible extractive** compression for
  large content: scores lines/sentences by TF-IDF + position + length, keeps the salient ones (always
  keeps headers/ERROR lines), and elides the rest with a marker — the dropped bytes are retained so
  `semantic_restore` reproduces the **byte-exact original** (lossless round-trip). Plus **SimHash**
  near-duplicate block folding, and optional CCR integration (stash the restore blob in the memory
  store, retrieve on demand). Verified: 121-line doc → 56.3% smaller, byte-exact restore.
- **`simplicio rag`** (`engine/simplicio_rag.py`) — **TF-IDF cosine retrieval** over the CCR memory
  store: `rag "<query>"` ranks stored memories by relevance with snippets; `rag remember <key> <text>`
  populates it. Verified: relevant doc ranks #1 across queries.

### Honest scope
These are **deterministic** techniques — extractive summarization + SimHash + TF-IDF retrieval — **not**
trained embedding/ONNX models. They address the "semantic compression" and "RAG" gaps with real,
zero-dependency, reversible methods; they do not do abstractive rewriting or embedding-space matching.
The trained-ONNX semantic model and embedding-vector RAG of the upstream remain out of scope (they
require ML models, not stdlib code) — and are not faked.

## [2.6.0] — 2026-06-24

### Added — output token capture (input + output now complete)
- The proxy was only counting **input** (prompt) tokens; it now also captures **output/completion**
  tokens by reading the upstream response's `usage` (OpenAI `completion_tokens` / Anthropic
  `output_tokens`) from a bounded 64 KB response tail — **without breaking streaming** (chunks are
  written through immediately; only a small tail is kept). Honest: if the upstream doesn't report
  usage, output is 0 (no fabricated estimate). Recorded as `total_output_tokens` (lifetime + session)
  and `tok_out=` in the PERF log. Verified isolated: a response with `completion_tokens:42` → captured 42.
- Dashboard shows a **tokens out** KPI (replacing the always-zero "cache hit" card — the native engine
  doesn't cache).

## [2.5.0] — 2026-06-24

### Added — more native commands + quality gates (5 more parallel agents, each self-tested)
- **`simplicio audit <paths>`** — scan files/dirs and rank how many tokens compression would save.
- **`simplicio capture --file body.json`** — dry-run analyzer: what a request would compress/save, no send.
- **`simplicio evals`** — compression eval + **regression gate** (corpus → %saved, asserts prose/code stay
  byte-identical + idempotence). Doubles as CI: exits non-zero if a change corrupts content or stops saving.
  Current gate: **4/4 invariants PASS, avg ~44% saved**.
- **`engine/simplicio_tokens.py`** — calibrated stdlib token estimator (prose ~4.1 c/tok, code ~2.9, json
  ~1.8). The proxy + capture now measure tokens with it instead of naive chars/4.
- **`engine/README.md`** — full engine reference (commands, capture mechanism, compression catalog, honest
  scope).

### Fixed
- The unified `bin/simplicio` CLI didn't route `wrap`/`report`/`verify` (and the new `audit`/`capture`/`evals`)
  — `ENGINE_CMDS` now forwards them all. README corrected accordingly.

## [2.4.0] — 2026-06-24

### Added — unified CLI + more engine commands (5 more parallel agents, each self-tested)
- **Unified `simplicio` command** (`engine/simplicio_cli.py` + `bin/simplicio`): one entry that dispatches
  `simplicio proxy|doctor|memory|mcp|init|wrap|report|verify|compress|version`.
- **`simplicio wrap <client>`** (`engine/simplicio_wrap.py`): run a client (claude/codex/cursor/opencode/aider)
  with capture routing injected for that run (OPENAI/ANTHROPIC_BASE_URL → proxy), warns if the proxy is down.
- **`simplicio report`** (`engine/simplicio_report.py`): savings report — lifetime/session totals + per-model
  and per-provider breakdown (deltas from the cumulative history), `--json`, `--since`, `--top`.
- **`simplicio verify`** (`engine/simplicio_verify.py`): one-command self-check of the whole stack
  (proxy, monitor, savings file, engine, compression, memory, MCP, operator) → PASS/WARN/FAIL table.
  Verified **8/8 PASS** on the dev machine.
- **`engine/simplicio_compress_extra.py`** — 4 more safe deterministic algorithms (markdown-table
  whitespace, repeated multi-line block fold, long-token elision, numbered-noise fold), chained after the
  base pipeline. Meaning-preserving + idempotent (prose/code byte-identical).
- `wrap`/`report`/`verify` are also reachable as `simplicio_engine` subcommands.

## [2.3.0] — 2026-06-24

### Added — native engine grows toward feature parity (built by 5 parallel agents, each self-tested)
- **`engine/simplicio_mcp.py`** — native stdio MCP server (JSON-RPC 2.0) exposing `simplicio_compress`,
  `simplicio_retrieve`, `simplicio_stats` tools. `simplicio_engine mcp` runs it.
- **`engine/simplicio_memory.py`** — CCR (compress-cache-retrieve) key-value store with byte-exact
  lossless recall (zlib+base64), atomic + thread-safe. `simplicio_engine memory remember/recall/forget/list`.
- **`engine/simplicio_compress.py`** — 8-algorithm deterministic compression (ANSI strip, trailing ws,
  blank collapse, line dedup, JSON minify, rule-run cap, hex-dump fold, fenced-log fold), idempotent and
  meaning-preserving. The proxy now uses it (verbose logs ~89-94% saved; clean prose/code untouched).
- **`engine/simplicio_init.py`** — native client integration writer (mirrors `headroom init`): registers
  the Simplicio MCP server into codex/claude/copilot/openclaw configs. **Dry-run by default**, `--apply`
  to write, idempotent. `simplicio_engine init <client>`.

### Verified
- **systemd activation field-tested on real Linux** (systemd PID 1 in Docker, aarch64): `systemctl start`
  brought the proxy up, `/health` returned `engine: simplicio`, and `Restart=always` re-spawned it after
  a kill — the previously-untested gap is now closed. `install_services.py` now sets `SIMPLICIO_HOME` on
  the services so savings/logs write even under an unset service `$HOME`.

### Fixed
- The compression module was named `compression`, which **collides with Python 3.14's new stdlib
  `compression` package** — renamed to `simplicio_compress` so the 8-algo pipeline actually loads on 3.14.

## [2.2.1] — 2026-06-24

### Verified — Linux is now field-tested (not just code-complete)
- Ran the stack inside a real Linux container (`python:3.12-slim`, py 3.12.13): the **native engine
  forwards + captures** (savings written), the **dashboard `get_status` + HTML** compose (7 runtimes),
  `install_services.py selftest` **PASS** (systemd units + Windows launchers), the **tray loads**
  (headless fallback, no crash), and the generated **systemd unit resolves the Linux Python path**.
  The honest remaining gap: systemd *daemon activation* (needs a real init host) and Windows *runtime*
  are still not exercised on those hosts — the software + artifacts are verified, service start-up is not.

## [2.2.0] — 2026-06-24

### Fixed — the installer now ships the token economy too
- **The main installer (`install_lib.py`) was disconnected from the token monitor.** It copied the
  6 skills + hooks but never set up the capture proxy / dashboard / tray, so a fresh user got the
  loop but **not** the token economy. Now the installer always prints how to enable the monitor, and
  `--with-monitor` installs the tray dep + registers the three services (`install_services.py install`).
  Verified on a fresh temp target: skills copied, hooks wired, monitor pointer shown, services
  selftest PASS.
- Removed a personal path (`~/Projetos/ai/hermes-agent/...`) from the `simplicio-engine` fallback —
  it would never resolve for other users.

## [2.1.0] — 2026-06-24

### Added
- **Live multi-runtime "active / blinking" detection.** The dashboard now detects which runtimes
  are actually RUNNING (process match) and shows them blinking: `● active` (blue) when running,
  `● capturing` (green) when their traffic is being saved in the last 10 min, `○ ready` otherwise.
  So with Claude open + Hermes on, both are recognized and blink. Header shows the active count.
- **Per-provider routing in the native engine** (`gpt→openai`, `claude→anthropic`, `deepseek→deepseek`,
  …): one capture proxy forwards each model to its REAL provider with the client's own key — captures
  every routable runtime **without swapping its model**. Verified live (gpt→OpenAI, deepseek→DeepSeek).
- **5-algorithm deterministic compression** (ANSI strip, rule-run cap, line dedup, whitespace, JSON
  minify). Verified capture coverage: OpenAI stream/non-stream, Anthropic, multimodal, concurrent,
  non-JSON fail-open — every request through the proxy is captured.

### Removed
- **Gemini, Kiro, Antigravity** dropped from the runtimes list — they use proprietary Google/AWS APIs
  the proxy can't intercept. Only the 7 genuinely-interceptable runtimes remain.

## [2.0.0] — 2026-06-24

### Added — native Simplicio capture engine (no external dependency)
- **`engine/simplicio_engine.py`** — a self-contained, stdlib-only capture proxy that **replaces the
  external `headroom-ai` binary** for the core capture path. It transparently forwards each request to
  the real upstream (**no model swap**), measures prompt tokens, applies **deterministic** compression
  (whitespace collapse, consecutive-line dedup, oversized-output capping), streams the response back,
  and writes `~/.simplicio/proxy_savings.json` in the same schema-v3 the Token Monitor reads. It is
  **fail-open**: any parse/compress error forwards the original bytes unchanged. Commands: `proxy`,
  `doctor`, `memory stats`, `--version`.

### Changed
- **The live capture proxy is now the native engine.** Verified end-to-end: a request through it
  reached DeepSeek's real API and returned DeepSeek's own auth error (proving transparent forwarding);
  a compressible payload was deduped 575→54 chars and recorded as real savings. Lifetime history was
  migrated `~/.headroom` → `~/.simplicio` for continuity (401,925 tokens preserved).
- `scripts/simplicio-engine` is **native-first** (falls back to an external binary only if the module
  is absent). `setup_simplicio.sh` and `install_services.py` run the native engine — `setup` no longer
  installs `headroom-ai`.
- README accelerator row + `token-capture.md` describe the native engine (schema-compatible with the
  OSS headroom project, credited).

### Honest scope
- The native engine is the **core** (transparent capture + measurement + deterministic compression).
  It is **not** a reimplementation of the upstream engine's 360k-LOC feature set (ONNX semantic
  compression, the 6-algorithm suite, RAG, MCP memory store). Those remain out of scope; the native
  engine delivers real, safe token savings without any external dependency.

## [1.9.0] — 2026-06-24

### Added
- **Active-LLM banner** — the dashboard now detects which LLM is currently being intercepted (from the
  latest request in `proxy_savings.json` history) and shows a banner "⚡ Saving tokens for `<model>`"
  with the **LLM's logo** (DeepSeek, Anthropic, OpenAI, Gemini, Llama, Mistral, Qwen, xAI, Kimi, Groq…),
  the tokens saved, and the **last-call datetime** + relative time.
- **Datetime records throughout** — real timestamps from the capture history: last-call datetime on the
  chart, session-start datetime in the footer, full `YYYY-MM-DD HH:MM:SS` "updated" stamp, and the
  per-request `ts` carried on the series.

### Fixed
- Topbar "intercepting" chip showed `0` — now reflects `<ready>/<total>` runtimes (e.g. 7/10).

## [1.8.1] — 2026-06-24

### Changed
- **Documented the Simplicio Token Monitor in the README** (web dashboard `:9090` + menu-bar tray +
  the `simplicio-economy.sh` module + cross-platform install) so it is a discoverable, complete
  deliverable. Rebranded the token-economy table's accelerator row to "Simplicio capture proxy".
- QA pass: monitor verified fully functional — all API fields present, real-time auto-refresh
  (live token count growing), no-data/error fallbacks, 0 console errors, tray reading live data.

### Fixed (cross-platform hardening)
- **pystray tray backend verified at runtime** (not just constructed) — renders the menu-bar icon;
  added `SIMPLICIO_TRAY_BACKEND=rumps|pystray|headless` to force/test a backend.
- **Windows Startup launcher bug**: `set K=V & ...` baked a trailing space into the value; now uses
  quoted `set "K=V"` per line.
- **systemd units** get an explicit `PATH` so the engine binary resolves under systemd's minimal env.
- **Dashboard engine call is cross-platform**: invokes the binary directly on Windows (the
  `simplicio-engine` bash wrapper can't run there).
- Added `python3 scripts/install_services.py selftest` — validates the generated systemd/Windows
  artifacts on any OS (PASS on macOS).

### Honest caveats
- Verified end-to-end on **macOS** (dashboard, rumps + pystray trays, launchd, real capture). The
  **Linux systemd and Windows Startup service activation are NOT yet run on those OSes** — only their
  generated artifacts are validated. The capture engine is the third-party `headroom-ai` binary.
  Provider interceptability (141/144) is a catalog estimate, not verified per provider.

## [1.8.0] — 2026-06-23

### Added
- **Cross-platform (macOS · Linux · Windows).** `scripts/install_services.py` registers the three
  always-on services on whichever OS you run it — launchd (macOS), systemd `--user` (Linux),
  Startup-folder launchers (Windows) — plus cross-platform `wire`/`unwire`/`status`. The tray
  (`app/simplicio_tray.py`) now auto-selects **rumps** on macOS (native menu-bar number) and
  **pystray** on Windows/Linux, with a headless print fallback.
- **Provider interceptability catalog (`app/providers.json`)** derived from the Hermes/OpenCode
  provider lists: **141 of 144 providers (98%) are interceptable** (139 OpenAI-compatible + 2
  Anthropic; only 3 Google-native are not). The dashboard surfaces the live `141/144` count next
  to the runtime panel — interception is really about providers, and we cover essentially all of them.

## [1.7.0] — 2026-06-23

### Added
- **Always-capture wiring (`simplicio-economy wire` / `unwire`).** Routes OpenAI-compatible
  clients (Codex, Cursor, OpenCode, any `OPENAI_BASE_URL` tool) through the local capture proxy —
  the **same upstream they already use, now intercepted + compressed**, with no model swap. This is
  the "works after install without invoking simplicio-loop" switch: once wired, every call is
  captured on the next shell/tool launch. Idempotent; backs up `~/.zshrc`; fully reversible via
  `unwire`. `setup_simplicio.sh` runs it so a fresh install turns capture on. `status` reports the
  wire state.

### Notes
- Activating always-capture rewrites `OPENAI_BASE_URL` in `~/.zshrc` (high blast radius across all
  OpenAI-compatible tools). That is intentional and what the install does on the user's behalf; an
  assistant running mid-session is (correctly) gated by the permission guard and must let the user
  run `simplicio-economy wire` themselves.

## [1.6.0] — 2026-06-23

### Added
- **Token-economy module (`scripts/simplicio-economy.sh`).** One entrypoint that brings up and
  reports the whole always-on savings stack — capture proxy + token monitor + menu-bar tray +
  the deterministic operator `simplicio-dev-cli` + lifetime savings — so token capture/savings
  work **after install without invoking simplicio-loop**. `setup_simplicio.sh` runs it at the end.
  Subcommands: `status`, `up`, `capture <openai|anthropic> [port]`.
- **Transparent capture proxy** (`simplicio-economy capture openai`) — forwards each call to the
  client's REAL provider, capturing tokens without swapping the model. **Verified end-to-end:** a
  real `gpt-5.4` request through the transparent proxy returned a genuine OpenAI response and the
  proxy's `/stats` recorded the request (`api_requests: 4`, `total_tokens_before: 124`). This is
  the correct path to capture Codex/Cursor/OpenCode, kept separate from the Hermes→DeepSeek proxy.

## [1.5.0] — 2026-06-23

### Added
- **Desktop menu-bar app (`app/simplicio_tray.py`).** A macOS tray + widget that lives in the
  menu bar showing live tokens saved (brand hexagon icon + compact count, e.g. `⬡ 102.9K`). The
  dropdown is the widget: lifetime tokens/$ saved, reduction %, requests, current-session savings,
  capture-proxy status, and "Open Token Monitor". Reads `proxy_savings.json` directly — no traffic
  of its own. Auto-starts as the `ai.simplicio.tray` launchd service; `setup_simplicio.sh` installs
  `rumps` and registers it.
- Brand `assets/tray-icon.png` for the menu-bar item.

## [1.4.0] — 2026-06-23

### Added
- **`scripts/simplicio-engine`** — a single Simplicio-branded wrapper around the capture engine
  binary, so the dashboard, scripts and docs speak `simplicio-engine` instead of the engine's
  own name. It is now the *only* place that resolves the underlying binary (fast lookup, no
  full-`$HOME` scan).

### Changed
- **Robust proxy detection.** The monitor now checks the proxy with a pure-Python socket connect
  instead of `lsof`, which the launchd service could not find on its restricted `PATH` (it lives
  in `/usr/sbin`) — the dashboard was falsely showing the proxy as down. Also added `/usr/sbin`
  to the generated service `PATH`s. "Always works", regardless of environment.
- Dashboard + capture script now call the engine through `simplicio-engine`; remaining `headroom`
  references are isolated to the wrapper's binary resolution, the engine's own data dirs
  (`~/.headroom`, read-only), and the literal `headroom-ai` package name.

### Notes
- **Capture activation verified.** `<engine> init <client>` was confirmed to add only a safe MCP
  integration (memory/retrieve tools) — it does NOT change a client's model or base URL. Real
  token capture requires routing a client's traffic through the proxy; with the current
  DeepSeek-pinned proxy that would swap OpenAI clients' model, so transparent multi-provider
  routing is required before activating Codex/Cursor/OpenCode (see
  `references/token-capture.md`). Claude (Anthropic format) can capture transparently.

## [1.3.0] — 2026-06-23

### Changed
- **Token Monitor is now data-forward.** Replaced the large logo hero with a compact top bar
  (small badge + green/yellow wordmark + live status chips) and gave the screen to the data:
  a **real-time token chart** (before / after / saved area) driven by the engine's request
  history, plus the savings gauge and a tighter KPI strip.
- **Primary data source is now `proxy_savings.json`** (lifetime + per-request history), with the
  raw proxy log kept as fallback — more robust and exact than log scraping, and it exposes the
  real provider/model of each intercepted request.

### Added
- **"LLMs / runtimes we intercept" panel with per-runtime logos** and an honest interceptability
  tier: `native` (engine durable integration: Claude, Codex, VS Code/Copilot, OpenClaw),
  `base-url` (OpenAI/Anthropic-compatible: Hermes, Cursor, OpenCode), `not interceptable`
  (proprietary APIs: Gemini, Kiro, Antigravity). Shows 7/10 interceptable, dimming the rest.
- **$ saved** KPI and a models-intercepted readout sourced from real request history.

## [1.2.0] — 2026-06-23

### Changed
- **Rebranded the token monitor from "headroom" to Simplicio.** The localhost dashboard is now
  the **Simplicio Token Monitor** (header + footer brand lockup rendered green + yellow).
  Our hooks/services/files were renamed: `hooks/headroom_dashboard.py` → `hooks/simplicio_dashboard.py`,
  `hooks/headroom_watch.py` → `hooks/simplicio_watch.py`, `scripts/setup_headroom.sh` →
  `scripts/setup_simplicio.sh`; launchd services `ai.simplicio.headroom` → `ai.simplicio.proxy`
  and `ai.simplicio.headroom-dashboard` → `ai.simplicio.token-monitor`; env var
  `HEADROOM_PORT` → `SIMPLICIO_PROXY_PORT` (old name still honored as fallback); proxy log
  targets → `~/.hermes/logs/simplicio-proxy*.log`.
- **Carve-out:** the underlying compression accelerator is the third-party **headroom-ai**
  product, so its real binary/install names are kept functional (`pip install headroom-ai`,
  `headroom proxy`, `headroom memory stats`) and its OSS attribution is preserved — only
  Simplicio-owned naming was changed.

### Added
- **Token Monitor auto-starts on macOS** via the renamed launchd service `ai.simplicio.token-monitor`,
  so the dashboard is live without a manual start.

## [1.1.0] — 2026-06-23

### Changed
- **Token dashboard redesigned to the Simplicio brand.** The localhost monitor
  (`hooks/simplicio_dashboard.py`, `:9090`) now renders the full Simplicio lockup faithfully
  in a neon-framed hero instead of a cropped square, echoes the brand tagline as four pillars
  (smart orchestration · neural cache · compressed context · maximum efficiency), and leads
  with a savings gauge (reduction %) + before→after token flow.
- **Runtime coverage is now a first-class panel** — all ten supported runtimes (Claude, Codex,
  Hermes, OpenClaw, VS Code, Gemini, Cursor, OpenCode, Kiro, Antigravity) each show how the
  skills load and how the loop drive is bound, with a coverage state pill.
- **Front-end construction cleaned up.** The single opaque HTML blob is split into
  `STYLE` / `BADGE_SVG` / `BODY` / `SCRIPT` constants composed via placeholder substitution —
  still single-file (deploy-friendly), no longer one unmaintainable string. The backend
  (`get_status` + handlers) is unchanged.

### Added
- **Repo-local brand asset** `assets/simplicio-loop-logo.png` — the dashboard now serves the
  logo from inside the repo (first logo candidate) instead of depending on a path outside it.
- **Faithful inline badge** (`BADGE_SVG`) — a vector of the hexagon-S mark (extruded S +
  stacked-layers core + circuit traces + speed particles), used as the favicon and the no-PNG
  fallback logo.

### Fixed
- Log viewer `tok_*=` highlight (`.hl`) had no CSS rule and rendered unstyled — now themed green.

## [1.0.5] — 2026-06-23

- Headroom integration: live web dashboard + monitor on `:9090`, context-compression proxy,
  MCP accelerator, setup script and launchd services.
- LMCache inference accelerator, agentsview session-observability source adapter.
- 11 runtime adapters + universal installer; hardened Ralph loop with bound operators
  (`simplicio-mapper` + `simplicio-cli`).
