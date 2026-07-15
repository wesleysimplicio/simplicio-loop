# Scripts inventory — core vs satellite (#118)

`scripts/` has grown to ~39 files. This is the versioned classification of every one of them:
**core** (required for the `/simplicio-loop` drive or the local `check.py` gate) vs **satellite**
(an opt-in/advanced capability — a source adapter, an extension point not wired into the base
loop, or a separate economy/demo stack). `scripts/check.py --core-gate` runs ONLY the core gate
(claims-audit + loop-contract + token-budget + the core subset of `tests/`); the default
`scripts/check.py` (no flags) still runs everything, core and satellite, unchanged.

Status legend: **active** (invoked today, tested), **experimental** (tested but not yet wired to
any skill/doc invoker), **legacy** (superseded, kept for compatibility/history).

## Core (required for the loop drive or the local gate)

| Script | Invoker | Status |
|---|---|---|
| `check.py` | manual (`python3 scripts/check.py`) · git pre-push hook · `.github/workflows/ci.yml` | active |
| `claims_audit.py` | `check.py` | active |
| `claims_manifest.py` | imported by `claims_audit.py` (quantitative-claims registry) | active |
| `check_loop_contract.py` | `check.py` | active |
| `token_budget.py` | `check.py` (#121) | active |
| `repository_budget.py` | `check.py` (#294) — tracked-tree size budget, per-file cap + total-growth gate, history untouched | active |
| `mirror_manifest.py` | imported by `sync_plugin.py` + `claims_audit.py` (single source of truth for the lean hooks/scripts/tests sets) | active |
| `sync_plugin.py` | manual, after editing `.claude/skills/` or a shipped hook; `tests/test_system_check.py` exercises `--check` | active |
| `verify_adapters.py` | `claims_audit.py` check 7 (adapter-install-contract) | active |
| `doctor.py` | manual (`python3 scripts/doctor.py [--repair]`), also wrapped by `scripts/simplicio-engine doctor` | active |
| `preflight.py` | fail-closed identity, minimum-version, capability, and Runtime contract smoke gate (`python3 scripts/preflight.py --json`) | active |
| `install.sh` / `install.ps1` | README § Install; the plugin/marketplace install path | active |
| `install_lib.py` | imported by `install.sh` / `install.ps1` | active |
| `install_services.py` | install flow (service/daemon registration) | active |
| `setup_simplicio.sh` | install flow (environment bootstrap) | active |
| `update.sh` | manual update entrypoint (re-runs the install flow against a newer release) | active |
| `loop_journal.py` | SKILL.md § The loop contract / § Run-journal + stall detector; `hooks/loop_stop.py` | active |
| `task_anchor.py` | SKILL.md § The loop contract step 2; `hooks/loop_stop.py` (DoD gate) | active |
| `task_backlog.py` | SKILL.md § Phase 0 — intake & decomposition (frozen multi-item backlog above the anchor; genesis guard; `next`/`done` drain gate) | active |
| `watcher_verify.py` | SKILL.md § The loop contract step 3 (watcher-gate); `hooks/loop_stop.py` | active |
| `hierarchical_planner.py` | SKILL.md § HRM-style hierarchical planner; `hooks/loop_stop.py` (`plan` before re-feed) | active |
| `cross_agent_wiki.py` | SKILL.md § Cross-agent persistent wiki (per-turn capture) | active |
| `handoff.py` | SKILL.md § Agent-to-agent handoff (spindle/latch); `hooks/loop_stop.py` | active |
| `impact_audit.py` | SKILL.md § The loop contract step 2 (blast-radius gate) | active |
| `flow_audit.py` | SKILL.md § The loop contract step 2/3 (integration gate) | active |
| `video_evidence.py` | SKILL.md § Video evidence producer; `pr_evidence.py` | active |
| `web_verify.py` | `video_evidence.py`; `.github/workflows/web-verify.yml` | active |
| `pr_evidence.py` | PR-open flow (`pr_evidence.py build --require-evidence`) | active |
| `toon_codec.py` | imported by `task_anchor.py` / `loop_journal.py` (TOON-rendered `--format toon` output) | active |
| `coverage_gate.py` | `.github/workflows/quality-gate.yml` (#277) — 85% global / 90% critical-path line coverage gate | active |
| `perf_gate.py` | `.github/workflows/quality-gate.yml` (#277) — latency/throughput/RSS vs `scripts/perf_baseline.json` + bounded convergence check | active |
| `flaky_gate.py` | `.github/workflows/quality-gate.yml` (#277) — repeats the convergence/drain-critical test subset (or full suite in `--stress`) N times and flags inconsistent outcomes | active |
| `regression_test_gate.py` | `.github/workflows/quality-gate.yml` (#277) — fails a PR that changes source without an accompanying `tests/` change | active |
| `test_categories.py` | `quality_matrix.py populate`/`independent_reverify_quality_matrix` (#283) — per-category (`unit`/`integration`/`system`/`regression`) test-runner split, reads the `tests/*_<category>.py` filename convention | active |

## Satellite (opt-in / advanced capabilities)

| Script | Invoker | Status |
|---|---|---|
| `autoresearch.py` | the `simplicio-autoresearch` satellite skill | active |
| `agentsview_adapter.py` | AgentsView source adapter (README § Source adapters) | active |
| `az_boards_adapter.py` | Azure Boards source adapter (`.claude/skills/simplicio-loop/references/azure-devops-adapter.md`) | active |
| `repo_conventions.py` | the `repo_conventions` extension point (learns repo conventions on demand) | active |
| `schema_verify.py` | optional schema/migration-diff gate (invoked by hand or from a task's own AC, not every turn) | active |
| `billing_aggregator.py` | `scripts/simplicio-economy.sh` (economy/billing stack) | active |
| `savings_harness.py` | `scripts/simplicio-economy.sh`; `e2e_demo.py` | active |
| `e2e_demo.py` | manual capstone acceptance demo (README § the e2e savings demo) | active |
| `independent_watcher.py` | `docs/INDEPENDENT_WATCHER.md`; clean committed-snapshot behavioral verification | active |
| `simplicio-economy.sh` | manual (`bash scripts/simplicio-economy.sh {status\|up\|monitor\|tray\|wire}`) | active |
| `simplicio-capture.sh` | token-capture proxy control (pip-only asset, not shipped in the marketplace `plugin/`) | active |
| `simplicio-engine` | wrapper CLI around the capture/economy stack (`simplicio-engine doctor`, etc.) | active |
| `fan_out.py` | parallel task distribution — tested (`selftest` registered in `claims_audit.py`) but no documented invoker in any skill/reference doc yet | experimental |
| `blast-radius.sh` | none found — no reference in README/AGENTS/CLAUDE/skill docs, no test coverage; a shell-based selective-test-runner that predates (and overlaps with) `impact_audit.py`/`flow_audit.py` | legacy — candidate for removal or for being wired into `check.py --core-gate` in a follow-up issue, not decided here |

## How this maps to `scripts/check.py`

- `scripts/check.py` (no flags) — unchanged: audit + full `tests/` (core + satellite) +
  loop-contract + token-budget.
- `scripts/check.py --core-gate` — audit + loop-contract + token-budget + ONLY the `tests/` files
  that exercise a core script (see `SATELLITE_TEST_STEMS` in `scripts/check.py`, which mirrors this
  table). This is the fast, mandatory gate: no adapter credentials, no economy stack, no
  autoresearch loop needed to get a green core gate.
- `scripts/check.py --audit-only` / `--tests-only` / `--loop-contract-only` / `--token-budget` —
  unchanged, run exactly one full-suite phase.

Regenerating this table: there is no automated generator (it is maintainer-curated, cross-checked
against `scripts/claims_audit.py`'s `SELFTEST_SCRIPTS`/`SELFTEST_EXEMPT` and the doc references
each script's docstring/reference file points at). Re-verify it whenever a script is added,
removed, or its invoker changes.
