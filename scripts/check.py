#!/usr/bin/env python3
"""simplicio-loop — local check runner (the "CI" you run yourself, no paid minutes).

Runs the whole verification gate locally — deterministic, stdlib-only, cross-platform:

  1. claims-audit   `scripts/claims_audit.py` (referenced scripts exist · extension-point count
                    consistent · cited commands run, including flow/impact audit selftests
                    · _bundle ≡ source)
  2. mirror-parity  `scripts/mirror_parity.py check` — fail-closed mirror parity for the shipped
                    `_bundle/`, `plugin/`, and shared skill references, reported as a distinct
                    gate instead of being only an internal claims-audit detail.
  3. test suite     pytest if installed (`pytest -q tests/`); otherwise each `tests/test_*.py`
                    self-runs on bare python3 (the suite needs no pip).
  4. loop-contract  `scripts/check_loop_contract.py` — validates the exported
                    `simplicio.loop-execution/v1` converge/drain fixtures
                    (`contracts/loop-execution/v1/`) against the real `hooks/loop_stop.py` /
                    `scripts/loop_journal.py` producers (#115).
  5. clean-env      `scripts/clean_env_contract.py` — validates the package metadata / entrypoint /
                    bundled assets contract needed for a clean environment to load the CLI without
                    publishing or network access.
  6. token-budget   `scripts/token_budget.py` (#121) — estimates tokens for SKILL.md/AGENTS.md/
                    CLAUDE.md/the largest scripts and FAILS on a regression past the committed
                    baseline (`scripts/token_budget_baseline.json`), so a doc/script that quietly
                    balloons in size gets caught the same way a broken test would.
  7. repo-budget    `scripts/repository_budget.py` (#294) — measures the CURRENT tracked working
                    tree (never history) and FAILS on a brand-new file over the per-file cap or on
                    total-tree growth past the committed baseline
                    (`scripts/repository_budget_baseline.json`); pre-existing large assets are
                    grandfathered so this never retroactively fails on history it didn't create.

Exit 0 only when everything passes — so it gates a commit/push. The installer wires
`hooks/action_gate.py pre-push` as `.git/hooks/pre-push` (#291), which runs `--core-gate` (this
script's fast/mandatory subset) plus a secret-scan of the actual push range, fail-closed — see
`hooks/README.md` § "The safety gate". To wire this script directly instead (no secret-scan,
full gate every push):

    printf '#!/bin/sh\\npython3 scripts/check.py\\n' > .git/hooks/pre-push
    chmod +x .git/hooks/pre-push

Usage:
    python3 scripts/check.py              # audit + tests + loop-contract + token-budget + repo-budget
    python3 scripts/check.py --audit-only
    python3 scripts/check.py --tests-only
    python3 scripts/check.py --mirror-parity-only
    python3 scripts/check.py --loop-contract-only
    python3 scripts/check.py --clean-env-only
    python3 scripts/check.py --token-budget   # token-budget guard only
    python3 scripts/check.py --repo-budget    # repository size budget guard only
    python3 scripts/check.py --package-content  # wheel/sdist/npm pack content check (#294 AC11)
                                               # -- opt-in only: ~20-30s, builds real artifacts,
                                               # a release-time check, not part of the default
                                               # gate or --core-gate.
    python3 scripts/check.py --core-gate      # mandatory/fast gate only (#118): audit + loop-
                                               # contract + token-budget + CORE tests, skipping
                                               # satellite-only tests (adapters, autoresearch,
                                               # repo_conventions, schema_verify, fan_out, the
                                               # token-monitor dashboard). See
                                               # docs/SCRIPTS_INVENTORY.md for the core/satellite
                                               # classification this filters on.
"""
import os
import subprocess
import sys
import glob

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SYSTEM_TEST_NESTED_GUARD = "SIMPLICIO_SYSTEM_TEST_NESTED"

# #118 — test files that exercise a SATELLITE (opt-in/advanced) script or capability, not the
# mandatory loop drive. `--core-gate` skips these so the fast/mandatory path never waits on an
# adapter or an economy-stack test. Full classification + rationale: docs/SCRIPTS_INVENTORY.md.
SATELLITE_TEST_STEMS = frozenset([
    "test_agentsview_adapter",
    "test_autoresearch",
    "test_az_boards_adapter",
    "test_dashboard_hook",
    "test_fan_out_flow",
    "test_fan_out_unit",
    "test_learn_pipeline_removed",
    "test_repo_conventions_architecture",
    "test_schema_verify_integration",
    "test_schema_verify_unit",
])


def _hr(title):
    print("\n=== %s ===" % title)


def run_audit():
    _hr("claims-audit")
    r = subprocess.run([sys.executable, os.path.join(HERE, "claims_audit.py")], cwd=REPO)
    return r.returncode == 0


def _have_pytest():
    return subprocess.run([sys.executable, "-c", "import pytest"],
                          capture_output=True).returncode == 0


def _core_test_files(tests_dir):
    return sorted(
        tf for tf in glob.glob(os.path.join(tests_dir, "test_*.py"))
        if os.path.splitext(os.path.basename(tf))[0] not in SATELLITE_TEST_STEMS)


def run_tests(only_core=False):
    tests_dir = os.path.join(REPO, "tests")
    if not os.path.isdir(tests_dir):
        print("no tests/ dir — skipping")
        return True
    # `tests/test_system_check_system.py` exercises this wrapper by spawning `scripts/check.py`
    # again. When this wrapper itself is already the active top-level gate, propagate the
    # nested guard so those tests do not recursively ask for another full `--tests-only`
    # or no-flags gate inside the same run. The cheaper system variants still run.
    env = dict(os.environ)
    env[SYSTEM_TEST_NESTED_GUARD] = "1"
    if only_core:
        test_files = _core_test_files(tests_dir)
        label = "tests/ (core-gate — satellite tests skipped)"
    else:
        test_files = sorted(glob.glob(os.path.join(tests_dir, "test_*.py")))
        label = "tests/"
    if _have_pytest():
        _hr("pytest %s" % label)
        r = subprocess.run([sys.executable, "-m", "pytest", "-q"] + test_files,
                           cwd=REPO, env=env)
        return r.returncode == 0
    # zero-dependency fallback: each test file self-runs on bare python3
    _hr("%s (stdlib self-run — pytest not installed)" % label)
    ok = True
    for tf in test_files:
        r = subprocess.run([sys.executable, tf], cwd=REPO, env=env)
        ok = ok and r.returncode == 0
    return ok


def run_mirror_parity():
    _hr("mirror-parity")
    path = os.path.join(HERE, "mirror_parity.py")
    if not os.path.exists(path):
        print("scripts/mirror_parity.py not found — skipping")
        return True
    r = subprocess.run([sys.executable, path, "check"], cwd=REPO)
    return r.returncode == 0


def run_loop_contract():
    _hr("loop-contract (simplicio.loop-execution/v1)")
    path = os.path.join(HERE, "check_loop_contract.py")
    if not os.path.exists(path):
        print("scripts/check_loop_contract.py not found — skipping")
        return True
    r = subprocess.run([sys.executable, path], cwd=REPO)
    return r.returncode == 0


def run_clean_env_contract():
    _hr("clean-env-contract")
    path = os.path.join(HERE, "clean_env_contract.py")
    if not os.path.exists(path):
        print("scripts/clean_env_contract.py not found — skipping")
        return True
    r = subprocess.run([sys.executable, path, "check"], cwd=REPO)
    return r.returncode == 0


def run_token_budget():
    _hr("token-budget (#121)")
    path = os.path.join(HERE, "token_budget.py")
    if not os.path.exists(path):
        print("scripts/token_budget.py not found — skipping")
        return True
    r = subprocess.run([sys.executable, path], cwd=REPO)
    return r.returncode == 0


def run_repository_budget():
    _hr("repo-budget (#294)")
    path = os.path.join(HERE, "repository_budget.py")
    if not os.path.exists(path):
        print("scripts/repository_budget.py not found — skipping")
        return True
    r = subprocess.run([sys.executable, path], cwd=REPO)
    return r.returncode == 0


def run_package_content():
    # #294 AC11 — deliberately NOT part of the default/core gate: it actually builds a real
    # sdist + wheel + runs `npm pack --dry-run`, ~20-30s and requires the `build` module + `npm`
    # on PATH. This is a release-time check (issue step 6: "provar que wheel/npm/plugin não
    # carregam mídia ou mirrors desnecessários"), run explicitly via
    # `python3 scripts/check.py --package-content`, not on every push.
    _hr("package-content (#294 AC11)")
    path = os.path.join(HERE, "package_content_check.py")
    if not os.path.exists(path):
        print("scripts/package_content_check.py not found — skipping")
        return True
    r = subprocess.run([sys.executable, path], cwd=REPO)
    return r.returncode == 0


def main():
    args = sys.argv[1:]
    core_gate = "--core-gate" in args
    only_flags = {"--audit-only", "--tests-only", "--mirror-parity-only", "--loop-contract-only",
                  "--clean-env-only", "--token-budget", "--repo-budget", "--package-content"}
    any_only = any(a in args for a in only_flags) or core_gate
    audit_ok = mirror_ok = tests_ok = contract_ok = clean_env_ok = budget_ok = repo_budget_ok = True
    package_content_ok = True
    if not any_only or "--audit-only" in args or core_gate:
        audit_ok = run_audit()
    if not any_only or "--mirror-parity-only" in args or core_gate:
        mirror_ok = run_mirror_parity()
    if not any_only or "--tests-only" in args or core_gate:
        tests_ok = run_tests(only_core=core_gate)
    if not any_only or "--loop-contract-only" in args or core_gate:
        contract_ok = run_loop_contract()
    if not any_only or "--clean-env-only" in args or core_gate:
        clean_env_ok = run_clean_env_contract()
    if not any_only or "--token-budget" in args or core_gate:
        budget_ok = run_token_budget()
    if not any_only or "--repo-budget" in args or core_gate:
        repo_budget_ok = run_repository_budget()
    if "--package-content" in args:
        # Deliberately NOT included in "not any_only" (the default full run) or core_gate — see
        # run_package_content()'s docstring: opt-in only, ~20-30s, a release-time check.
        package_content_ok = run_package_content()
    ok = (audit_ok and mirror_ok and tests_ok and contract_ok and clean_env_ok and budget_ok
          and repo_budget_ok and package_content_ok)
    if core_gate:
        print("\ncore-gate: %s  (audit=%s · mirror-parity=%s · core-tests=%s · loop-contract=%s · clean-env=%s · token-budget=%s · repo-budget=%s)" % (
            "PASS" if ok else "FAIL", "ok" if audit_ok else "FAIL", "ok" if mirror_ok else "FAIL",
            "ok" if tests_ok else "FAIL", "ok" if contract_ok else "FAIL",
            "ok" if clean_env_ok else "FAIL", "ok" if budget_ok else "FAIL",
            "ok" if repo_budget_ok else "FAIL"))
    print("\ncheck: %s  (audit=%s · mirror-parity=%s · tests=%s · loop-contract=%s · clean-env=%s · token-budget=%s · repo-budget=%s)" % (
        "PASS" if ok else "FAIL", "ok" if audit_ok else "FAIL", "ok" if mirror_ok else "FAIL",
        "ok" if tests_ok else "FAIL", "ok" if contract_ok else "FAIL",
        "ok" if clean_env_ok else "FAIL", "ok" if budget_ok else "FAIL",
        "ok" if repo_budget_ok else "FAIL"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
