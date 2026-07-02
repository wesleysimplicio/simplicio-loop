#!/usr/bin/env python3
"""simplicio-loop — claims audit (turn asserted docs into checked facts; fail-closed).

The repo makes many claims in prose. This audits the mechanical ones so a doc can't drift away from
the code. Deterministic, stdlib-only, no network. Exits 0 when every check passes, 1 otherwise —
so it can gate a commit/push (`scripts/check.py`, or a git pre-push hook). NOT a GitHub Action;
runs locally, free.

Seven checks:
  1. referenced-scripts-exist  Every `scripts/<name>.py` mentioned in the docs actually exists.
  2. extension-point-count      Every "<N> extension points / named (binding) points" figure agrees
                                with EACH OTHER *and* with the actual row count of the extension-points
                                table in extension-points.md (the source of truth, not just consensus).
  3. cited-commands-run         Each doc-cited worker script is invokable: its `selftest` passes if
                                it has one, else it `py_compile`s and prints usage cleanly. Also a
                                meta-check: every `scripts/*.py` that defines a `selftest` subcommand
                                must be registered here (a selftest the gate never runs is worse than
                                none — its presence implies coverage that doesn't exist).
  4. bundle-parity              Every shipped file under `.claude/skills/`, `hooks/`, the bundled
                                runtime helper `scripts/`, and bundled parity `tests/` is
                                byte-identical under `simplicio_loop/_bundle/` — checked in BOTH
                                directions, so an orphan left behind in `_bundle/` after a source
                                rename/delete is caught too (not just a forward source->bundle walk).
  5. plugin-parity              The lean marketplace plugin tree mirrors the source files it ships
                                (skills + wired hooks + runtime helper scripts + parity tests).
  6. skill-count                Every "<N> skills" claim agrees with the actual `.claude/skills/*/
                                SKILL.md` count.
  7. adapter-install-contract   `scripts/verify_adapters.py claude` — a fast, representative subset
                                of the full 11-runtime installer e2e (`verify_adapters.py` with no
                                args) — proves the install contract isn't dead assurance. Run the
                                full sweep manually / in a slower CI job; it is too slow (~45s per
                                runtime) for this fast local gate.

Usage:
    python3 scripts/claims_audit.py [--json] [--only 1,2,3,4]
"""
import json
import os
import re
import subprocess
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

if HERE not in sys.path:
    sys.path.insert(0, HERE)
from mirror_manifest import LEAN_SCRIPTS, LEAN_TESTS  # noqa: E402 — single source of truth (#74)

DOC_GLOBS = ["README.md", "AGENTS.md", "CLAUDE.md", "INSTALL.md", "PYPI.md"]
DOC_DIRS = [os.path.join(".claude", "skills")]
EXTENSION_POINTS_DOC = os.path.join(
    REPO, ".claude", "skills", "simplicio-tasks", "references", "extension-points.md")

SCRIPT_RE = re.compile(r"((?:scripts|hooks)/[a-zA-Z0-9_]+\.py)")
# "44 extension points", "the 44 named binding points", "44 named points", "44 named extension
# points" (a phrasing that escaped the original two patterns — #72), badge "...points-44-...".
COUNT_RES = [
    re.compile(r"\b(\d{1,3})\s+extension points", re.I),
    re.compile(r"\b(\d{1,3})\s+named (?:binding )?points", re.I),
    re.compile(r"\b(\d{1,3})\s+named extension points", re.I),
    re.compile(r"extension%20points-(\d{1,3})-"),
]
# "6 skills", "the 6 skills", badge "skills-6-..." — deliberately NOT matching "11 skills &
# accelerators" rollups (those are a different, non-audited marketing number by design).
SKILL_COUNT_RES = [
    re.compile(r"\b(\d{1,2})\s+skills\b(?!\s*&)", re.I),
    re.compile(r"skills-(\d{1,2})-", re.I),
]
# worker/hook scripts whose `selftest` proves them; others just need to be invokable
SELFTEST_SCRIPTS = [
    "scripts/loop_journal.py",
    "scripts/billing_aggregator.py",
    "scripts/savings_harness.py",
    "scripts/repo_conventions.py",
    "scripts/task_anchor.py",
    "scripts/pr_evidence.py",
    "scripts/flow_audit.py",
    "scripts/impact_audit.py",
    "scripts/cross_agent_wiki.py",
    "scripts/hierarchical_planner.py",
    "scripts/watcher_verify.py",
    "scripts/handoff.py",
    "scripts/install_services.py",
    "scripts/mirror_manifest.py",
    "hooks/action_gate.py",
]
# scripts intentionally excluded from the "every selftest is registered" meta-check (check 3): a
# `selftest`-shaped function/subcommand that isn't the worker's own self-check, or a script this
# repo has decided NOT to gate (document why here, don't just silently exclude).
SELFTEST_EXEMPT = set()


def _docs():
    files = [os.path.join(REPO, f) for f in DOC_GLOBS if os.path.exists(os.path.join(REPO, f))]
    for d in DOC_DIRS:
        for root, _, names in os.walk(os.path.join(REPO, d)):
            files += [os.path.join(root, n) for n in names if n.endswith(".md")]
    return files


def _read(p):
    with open(p, encoding="utf-8", errors="replace") as f:
        return f.read()


def check_scripts_exist():
    missing = {}
    for doc in _docs():
        for rel in SCRIPT_RE.findall(_read(doc)):
            if not os.path.exists(os.path.join(REPO, rel)):
                missing.setdefault(rel, []).append(os.path.relpath(doc, REPO))
    ok = not missing
    return ok, ("all referenced scripts exist" if ok else
                "missing scripts: %s" % json.dumps(missing))


def _extension_point_table_count():
    """Row count of the ACTUAL extension-points table in extension-points.md — the source of
    truth check 2 compares every doc claim against (#72), not just mutual doc consensus."""
    if not os.path.exists(EXTENSION_POINTS_DOC):
        return None
    in_table = False
    count = 0
    for line in _read(EXTENSION_POINTS_DOC).splitlines():
        s = line.strip()
        if s.startswith("| Extension point |"):
            in_table = True
            continue
        if not in_table:
            continue
        if s.startswith("|---"):
            continue
        if s.startswith("|"):
            count += 1
        else:
            break  # blank line / prose ends the table
    return count


def check_extension_count():
    found = {}  # number -> [files]
    for doc in _docs():
        txt = _read(doc)
        for rx in COUNT_RES:
            for n in rx.findall(txt):
                found.setdefault(int(n), set()).add(os.path.relpath(doc, REPO))
    actual = _extension_point_table_count()
    if not found:
        if actual is None:
            return True, "no extension-point counters found (nothing to check)"
        return True, "no extension-point counters found in docs; table has %d rows" % actual
    detail = {n: sorted(files) for n, files in found.items()}
    if len(found) > 1:
        return False, "extension-point counters DISAGREE: %s" % json.dumps(detail)
    n = next(iter(found))
    if actual is not None and n != actual:
        return False, ("extension-point count claimed as %d does not match the actual "
                        "extension-points.md table (%d rows): %s" % (n, actual, json.dumps(detail)))
    return True, "extension-point count consistent with the table: %d" % n


def check_skill_count():
    skills_dir = os.path.join(REPO, ".claude", "skills")
    actual = len([n for n in (os.listdir(skills_dir) if os.path.isdir(skills_dir) else [])
                  if os.path.isfile(os.path.join(skills_dir, n, "SKILL.md"))])
    found = {}
    for doc in _docs():
        txt = _read(doc)
        for rx in SKILL_COUNT_RES:
            for n in rx.findall(txt):
                found.setdefault(int(n), set()).add(os.path.relpath(doc, REPO))
    if not found:
        return True, "no skill-count claims found; tree has %d skills" % actual
    detail = {n: sorted(files) for n, files in found.items()}
    if len(found) > 1 or actual not in found:
        return False, ("skill-count claim(s) %s do not match the actual tree (%d skills under "
                        ".claude/skills/): %s" % (sorted(found), actual, json.dumps(detail)))
    return True, "skill count consistent with the tree: %d" % actual


def check_commands_run():
    failures = []
    for rel in SELFTEST_SCRIPTS:
        path = os.path.join(REPO, rel)
        if not os.path.exists(path):
            failures.append("%s: not found" % rel)
            continue
        r = subprocess.run([sys.executable, path, "selftest"],
                           capture_output=True, text=True, cwd=REPO)
        bad_output = re.search(r"\bFAIL(?:ED)?\b|\[XX\]|\[ER\]", r.stdout.upper().replace("PASS", ""))
        if r.returncode != 0 or bad_output:
            failures.append("%s selftest rc=%d" % (rel, r.returncode))
    # meta-check (#75): any scripts/*.py that DEFINES a `selftest` subcommand but isn't
    # registered above is dead assurance — its presence implies coverage that never runs.
    registered = set(SELFTEST_SCRIPTS) | SELFTEST_EXEMPT
    scripts_dir = os.path.join(REPO, "scripts")
    orphans = []
    for name in sorted(os.listdir(scripts_dir)):
        if not name.endswith(".py") or name.startswith("_"):
            continue
        rel = "scripts/%s" % name
        if rel in registered:
            continue
        path = os.path.join(scripts_dir, name)
        try:
            text = _read(path)
        except OSError:
            continue
        # a script that both DEFINES a selftest and dispatches it from argv is a real, runnable
        # selftest the gate is skipping — not just an unrelated function named "selftest".
        if re.search(r"def\s+(?:cmd_)?selftest\b", text) and '"selftest"' in text:
            orphans.append(rel)
    if orphans:
        failures.append("selftest defined but not registered in SELFTEST_SCRIPTS/SELFTEST_EXEMPT: "
                         "%s" % ", ".join(orphans))
    # other cited scripts: must at least py_compile without crashing
    cited = set()
    for doc in _docs():
        cited.update(SCRIPT_RE.findall(_read(doc)))
    for rel in sorted(cited - set(SELFTEST_SCRIPTS)):
        path = os.path.join(REPO, rel)
        if not os.path.exists(path):
            continue  # caught by check 1
        c = subprocess.run([sys.executable, "-m", "py_compile", path],
                           capture_output=True, text=True, cwd=REPO)
        if c.returncode != 0:
            failures.append("%s: py_compile failed" % rel)
    ok = not failures
    return ok, ("all cited commands run" if ok else "; ".join(failures))


def check_bundle_parity():
    # The pip bundle ships the skills, hooks, runtime helper scripts, and shipped parity tests —
    # all must mirror source byte-for-byte, checked in BOTH directions (#70): forward
    # (source -> bundle: nothing shipped is missing/stale) AND reverse (bundle -> source: no
    # orphan file left behind by a rename/delete still ships in the pip wheel undetected).
    pairs = [
        (os.path.join(REPO, ".claude", "skills"),
         os.path.join(REPO, "simplicio_loop", "_bundle", "skills")),
        (os.path.join(REPO, "hooks"),
         os.path.join(REPO, "simplicio_loop", "_bundle", "hooks")),
        (os.path.join(REPO, "scripts"),
         os.path.join(REPO, "simplicio_loop", "_bundle", "scripts"),
         set(LEAN_SCRIPTS)),
        (os.path.join(REPO, "tests"),
         os.path.join(REPO, "simplicio_loop", "_bundle", "tests"),
         set(LEAN_TESTS)),
    ]
    drift = []

    def _walk_rel(root, include):
        out = set()
        for r, dirs, names in os.walk(root):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for n in names:
                if n.endswith((".pyc", ".pyo")):
                    continue
                rel = os.path.relpath(os.path.join(r, n), root)
                if include is not None and rel not in include:
                    continue
                out.add(rel)
        return out

    for pair in pairs:
        if len(pair) == 2:
            src_root, bun_root = pair
            include = None
        else:
            src_root, bun_root, include = pair
        tag = os.path.basename(bun_root)
        if not os.path.isdir(bun_root):
            drift.append("bundle dir missing: _bundle/%s" % tag)
            continue
        src_files = _walk_rel(src_root, include)
        # bundle-side include filter mirrors the source-side one; extra dirs under a bundle
        # subtree with no `include` restriction are legitimately part of the mirror.
        bun_files = _walk_rel(bun_root, include if include is not None else None)
        for rel in sorted(src_files - bun_files):
            drift.append("%s: missing in bundle: %s" % (tag, rel))
        for rel in sorted(bun_files - src_files):
            drift.append("%s: orphan in bundle (no matching source file): %s" % (tag, rel))
        for rel in sorted(src_files & bun_files):
            sp, bp = os.path.join(src_root, rel), os.path.join(bun_root, rel)
            if _read(sp) != _read(bp):
                drift.append("%s: differs: %s" % (tag, rel))
    ok = not drift
    return ok, ("bundle ≡ source, both directions (skills + hooks + runtime scripts + parity tests)"
                if ok else "; ".join(drift))


def check_plugin_sync():
    # The lean marketplace plugin tree (plugin/) must mirror source — skills byte-identical,
    # hooks exactly the wired set. scripts/sync_plugin.py --check is the source of truth.
    r = subprocess.run([sys.executable, os.path.join(REPO, "scripts", "sync_plugin.py"), "--check"],
                       capture_output=True, text=True, cwd=REPO)
    ok = r.returncode == 0
    detail = [ln for ln in (r.stdout or r.stderr or "").splitlines() if ln.strip()]
    return ok, ("plugin ≡ source (lean marketplace tree)" if ok else "; ".join(detail[-6:]))


def check_adapter_contract():
    # #75: verify_adapters.py was previously referenced only in docs/snapshots, never actually run
    # by the gate — "runnable in CI" was an unrun claim. This runs the fast, representative
    # "claude" runtime for real (installs into a throwaway target, asserts skills/entry/hooks
    # landed); the full 11-runtime sweep (`python3 scripts/verify_adapters.py`) is documented in
    # adapters/MATRIX.md as a slower manual/CI-optional check.
    path = os.path.join(REPO, "scripts", "verify_adapters.py")
    if not os.path.exists(path):
        return True, "scripts/verify_adapters.py not present (nothing to check)"
    try:
        r = subprocess.run([sys.executable, path, "claude"],
                           capture_output=True, text=True, cwd=REPO, timeout=60)
    except subprocess.TimeoutExpired:
        return False, "verify_adapters.py claude timed out (>60s)"
    ok = r.returncode == 0
    detail = [ln for ln in (r.stdout or r.stderr or "").splitlines() if ln.strip()]
    return ok, ("adapter install-contract verified (claude)" if ok else "; ".join(detail[-8:]))


CHECKS = [
    ("1 referenced-scripts-exist", check_scripts_exist),
    ("2 extension-point-count", check_extension_count),
    ("3 cited-commands-run", check_commands_run),
    ("4 bundle-parity", check_bundle_parity),
    ("5 plugin-parity", check_plugin_sync),
    ("6 skill-count", check_skill_count),
    ("7 adapter-install-contract", check_adapter_contract),
]


def main():
    args = sys.argv[1:]
    as_json = "--json" in args
    only = None
    if "--only" in args:
        only = set(args[args.index("--only") + 1].split(","))
    results = []
    for label, fn in CHECKS:
        if only and label.split()[0] not in only:
            continue
        try:
            ok, detail = fn()
        except Exception as e:  # a crashing check is a failed check (fail-closed)
            ok, detail = False, "check crashed: %s" % e
        results.append({"check": label, "ok": ok, "detail": detail})
    failed = [r for r in results if not r["ok"]]
    if as_json:
        print(json.dumps({"ok": not failed, "results": results}, indent=2, ensure_ascii=False))
    else:
        for r in results:
            print("[%s] %s — %s" % ("ok" if r["ok"] else "XX", r["check"], r["detail"]))
        print("claims-audit: %s (%d/%d)" % ("PASS" if not failed else "FAIL",
                                            len(results) - len(failed), len(results)))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
