# Quality Gate (#283) — what is actually implemented

This is the operator-facing documentation issue #283 asked for
("Implementar Quality Gate obrigatório com TDD, testes completos, cobertura mínima e
benchmark"). It describes the real, shipped mechanism — not the issue's aspirational spec — and
says explicitly, for each item the issue asked for, whether it exists, is partial, or is
genuinely out of scope for this repo. For the receipt schema itself see
[`contracts/quality-gate/v1/SCHEMA.md`](../contracts/quality-gate/v1/SCHEMA.md); this document is
about the surrounding tooling: what produces and consumes that receipt.

## 1. The gate, end to end

```
scripts/quality_matrix.py build      -> quality-matrix.json (empty template)
scripts/quality_matrix.py populate   -> auto-fills unit/integration/system/regression/benchmark/coverage
scripts/quality_matrix.py tdd-red    -> tdd-red-receipt.json  (opt-in, policy.tdd_required)
scripts/quality_matrix.py tdd-green  -> tdd-green-receipt.json
scripts/quality_matrix.py check      -> evaluate_quality_matrix (self-reported verdict)
scripts/quality_matrix.py reverify   -> independent_reverify_quality_matrix (re-derived verdict)
scripts/watcher_verify.py verify     -> attaches quality_gate: VERIFIED|BLOCKED|NOT_PRESENT
simplicio_loop/oracle.py::evaluate_completion -> refuses COMPLETE unless quality-matrix.json is ready
```

No code path marks a work item `done`/`COMPLETE` while skipping this: `evaluate_completion` reads
`quality-matrix.json` unconditionally (issue #278's baseline, extended by #283). This document
covers the pieces #283 specifically added or is still missing.

## 2. Per-category test-runner split (`scripts/test_categories.py`)

**What exists:** `unit` / `integration` / `system` / `regression` are now separately invokable,
each with its own pass/fail:

```bash
python3 scripts/test_categories.py status                      # counts per category, JSON
python3 scripts/test_categories.py list --category unit         # which files, exactly
python3 scripts/test_categories.py run --category unit           # run just that category
python3 scripts/test_categories.py run --category integration --emit-json report.json
python3 scripts/test_categories.py selftest                      # proves the partition logic
```

`scripts/quality_matrix.py populate` now calls this for `unit`/`integration`/`system` the same
way it already called `regression_test_gate.py`/`perf_gate.py`/`coverage_gate.py` for the other
three lanes — every measurable lane (all but `implementation`, which is inherently
change-specific) is now auto-filled from a real, standalone gate script instead of requiring a
hand-typed status. `simplicio_loop/quality_matrix.py::independent_reverify_quality_matrix` also
live-reruns `unit`/`integration`/`system` the same way it already reran `regression`/`benchmark`,
so a claimed `pass` in any of these lanes is independently re-executed, not just re-parsed from a
self-reported string.

**How categorization works, honestly:** the script reads the **existing** filename convention —
`tests/test_*_unit.py`, `_integration.py`, `_system.py`, `_regression.py` — 21 files out of the
~190 in `tests/` follow it today. Every other file is reported as `uncategorized`
(`test_categories.py list --category uncategorized`), not silently folded into one of the four
buckets. An earlier draft of this tool tried keyword-matching (`pytest -k unit`) against the
whole test-name corpus instead; that was dropped because it is unreliable — it sweeps in
unrelated tests whose *name* happens to contain the substring, including slow/live/e2e tests that
hang for minutes. Only the deliberate suffix convention is treated as ground truth.

**What this does NOT claim:** it does not classify the other ~165 test files into a category.
Growing the `_unit.py`/`_integration.py`/`_system.py`/`_regression.py`-suffixed set (renaming or
splitting existing files) is future work, tracked as part of the coverage migration below —
category coverage and line coverage are the same kind of debt.

## 3. Coverage baseline and migration (issue's Fase A–D plan)

**Fase A (done, this PR):** a real, measured baseline exists at
[`quality/coverage-baseline.json`](../quality/coverage-baseline.json) — produced by
`scripts/coverage_gate.py`, not invented. It measures a **defined critical subset**: the same 21
files the per-category split above formalizes (`unit`+`integration`+`system`+`regression`), plus
whichever `CRITICAL_MODULES` (see `scripts/coverage_gate.py`) those files happen to exercise. The
full `tests/` tree (~1700 tests, including live/e2e/network-touching suites) was not used for the
baseline: in this repo's sandboxed CI-equivalent shell, running the whole suite under coverage
instrumentation intermittently hangs (some live/e2e tests wait on external state) well past a
reasonable local-gate budget. `scripts/coverage_gate.py` gained a `--tests-path` flag (repeatable)
specifically so a defined subset like this can be measured reproducibly instead of only ever
running full-or-nothing; the flag defaults to `tests/` (the full tree) when omitted, so existing
`--tests-path`-less invocations (CI, `scripts/check.py`) are unaffected.

The measured numbers (16.60% global / 9.40% critical over that 21-file scope — see the file for
the full detail) are an honest under-estimate of the repo's real coverage, not a claim about the
whole codebase: only 21 of ~190 test files were exercised. `coverage_gate.py` does not yet compare
a fresh run against this baseline automatically (no `--baseline-file` flag) — it only checks the
fixed `--global-threshold`/`--critical-threshold` CLI flags. Wiring that comparison is Fase B/C
work (see below), deliberately not bundled into "record a real number" in this PR.

**Fase B (not yet started):** raise coverage module-by-module, remove temporary exclusions, add
tests for modules with clear gaps. Not attempted in this PR — the issue's own text frames this as
follow-on work, not a Fase A deliverable, and doing it well requires reading each undercovered
module rather than mechanically padding a number.

**Fase C (blocked, not by this repo):** `fail_under = 85` globally requires a fast, reliable
full-suite coverage run. That is blocked today by two things this PR found and only partially
fixed:
  1. `scripts/coverage_gate.py`'s critical-path percentage calculation crashed unconditionally on
     this environment (`coverage` 7.15.x + Python 3.14: the private `Coverage._analyze`/
     `_get_file_reporter` pair it used raised `TypeError: cannot use 'PythonFileReporter' as a
     dict key`) — **fixed in this PR**, replaced with the public `Coverage.analysis2()` API.
  2. Running the *entire* `tests/` tree under coverage in one shot is slow/flaky in a sandboxed
     shell for reasons unrelated to coverage itself (a handful of subprocess-spawning tests hit a
     `WinError 6` invalid-handle OSError on this specific host, and some live/e2e tests block on
     external state) — **not fixed**, out of scope for #283, tracked as a real, separate follow-up
     (the fix belongs in test isolation/environment hardening, not the quality gate).

**Fase D (not started):** evaluate raising the global target above 85% once B/C land. Nothing to
report yet.

## 4. Benchmark

Already covered by `scripts/perf_gate.py` (predates #283, extended by it): the committed baseline
lives at `scripts/perf_baseline.json` (regenerate deliberately with
`python3 scripts/perf_gate.py --update-baseline`, never silently). The issue's suggested path
`quality/benchmark-baseline.json` is not used — `perf_baseline.json` already is that file, just
alongside the script instead of under `quality/`, and `scripts/perf_gate.py --emit-json` gives
every consumer (`quality_matrix.py populate`, the independent reverifier) a structured,
reproducible verdict against it. Not renamed/moved in this PR to avoid an unrelated churn diff on
a file this issue didn't ask to change.

## 5. Multi-language adapters — N/A for this repo

Issue #283 §14 asks for adapters recognizing JS/TS, Rust, .NET, Go, and Java test/coverage/
benchmark tooling, explicitly allowing a "Python-first" initial phase. Checked for this PR: this
repository's only non-Python source surface is `packaging/npm/package.json`, a thin npm
**wrapper** that shells out to the installed Python package (no JS test suite, no `.ts`/`.js`
application code beyond that wrapper's `bin/` shim). There is no Rust, Go, .NET, or Java surface
at all (`Cargo.toml`/`go.mod`/`*.csproj`/`pom.xml`/`build.gradle` all absent). Building real
adapters for languages this repo does not contain would be speculative, untestable scaffolding —
explicitly scoped OUT rather than stubbed. If `simplicio-loop` is ever used to gate a *consuming*
project written in one of those languages, the adapter work belongs there, informed by that
project's real toolchain, not invented here against nothing.

## 6. CI job separation — moot (no CI substrate)

Issue #283 §13 asks for separate required GitHub Actions jobs (`unit`/`integration`/`system`/
`regression`/`coverage`/`benchmark`/`quality-gate`) with branch-protection wiring.
`.github/workflows/` was removed repository-wide in #311 for unrelated billing reasons — there is
currently no CI running on this repo's PRs at all, so there is nothing to split into jobs. The
per-category split in §2 above is exactly the piece that *would* back those jobs the moment CI
returns (`python3 scripts/test_categories.py run --category unit` etc. as a job step per lane).

## 7. Verifying it locally

```bash
python3 scripts/test_categories.py selftest
python3 scripts/quality_matrix.py selftest
python3 scripts/coverage_gate.py --tests-path tests/test_quality_matrix_unit.py --diagnostics-dir /tmp/cov
python3 scripts/claims_audit.py
```
