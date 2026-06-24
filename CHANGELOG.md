# Changelog

All notable changes to **simplicio-loop** are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); the project uses SemVer.

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
