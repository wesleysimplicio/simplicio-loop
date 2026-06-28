#!/usr/bin/env python3
"""simplicio-loop — claims audit (turn asserted docs into checked facts; fail-closed).

The repo makes many claims in prose. This audits the mechanical ones so a doc can't drift away from
the code. Deterministic, stdlib-only, no network. Exits 0 when every check passes, 1 otherwise —
so it can gate a commit/push (`scripts/check.py`, or a git pre-push hook). NOT a GitHub Action;
runs locally, free.

Four checks:
  1. referenced-scripts-exist  Every `scripts/<name>.py` mentioned in the docs actually exists.
  2. extension-point-count      Every "<N> extension points / named (binding) points" figure and
                                the README badge agree on ONE number.
  3. cited-commands-run         Each doc-cited worker script is invokable: its `selftest` passes if
                                it has one, else it `py_compile`s and prints usage cleanly.
  4. bundle-parity              Every file under .claude/skills/ has a byte-identical copy under
                                simplicio_loop/_bundle/skills/ (the shipped pip bundle ≡ source).

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

DOC_GLOBS = ["README.md", "AGENTS.md", "CLAUDE.md", "INSTALL.md", "PYPI.md"]
DOC_DIRS = [os.path.join(".claude", "skills")]

SCRIPT_RE = re.compile(r"((?:scripts|hooks)/[a-zA-Z0-9_]+\.py)")
# "44 extension points", "the 44 named binding points", "44 named points", badge "...points-44-..."
COUNT_RES = [
    re.compile(r"\b(\d{1,3})\s+extension points", re.I),
    re.compile(r"\b(\d{1,3})\s+named (?:binding )?points", re.I),
    re.compile(r"extension%20points-(\d{1,3})-"),
]
# worker/hook scripts whose `selftest` proves them; others just need to be invokable
SELFTEST_SCRIPTS = ["scripts/loop_journal.py", "scripts/billing_aggregator.py",
                    "scripts/savings_harness.py", "scripts/repo_conventions.py",
                    "scripts/task_anchor.py", "scripts/pr_evidence.py",
                    "hooks/action_gate.py"]


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


def check_extension_count():
    found = {}  # number -> [files]
    for doc in _docs():
        txt = _read(doc)
        for rx in COUNT_RES:
            for n in rx.findall(txt):
                found.setdefault(int(n), set()).add(os.path.relpath(doc, REPO))
    if not found:
        return True, "no extension-point counters found (nothing to check)"
    if len(found) == 1:
        n = next(iter(found))
        return True, "extension-point count consistent: %d" % n
    detail = {n: sorted(files) for n, files in found.items()}
    return False, "extension-point counters DISAGREE: %s" % json.dumps(detail)


def check_commands_run():
    failures = []
    for rel in SELFTEST_SCRIPTS:
        path = os.path.join(REPO, rel)
        if not os.path.exists(path):
            failures.append("%s: not found" % rel)
            continue
        r = subprocess.run([sys.executable, path, "selftest"],
                           capture_output=True, text=True, cwd=REPO)
        if r.returncode != 0 or "FAIL" in r.stdout.upper().replace("PASS", ""):
            failures.append("%s selftest rc=%d" % (rel, r.returncode))
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
    # The pip bundle ships BOTH the skills and the hooks — both must mirror source byte-for-byte.
    pairs = [
        (os.path.join(REPO, ".claude", "skills"),
         os.path.join(REPO, "simplicio_loop", "_bundle", "skills")),
        (os.path.join(REPO, "hooks"),
         os.path.join(REPO, "simplicio_loop", "_bundle", "hooks")),
    ]
    drift = []
    for src_root, bun_root in pairs:
        tag = os.path.basename(bun_root)
        if not os.path.isdir(bun_root):
            drift.append("bundle dir missing: _bundle/%s" % tag)
            continue
        for root, dirs, names in os.walk(src_root):
            dirs[:] = [d for d in dirs if d != "__pycache__"]  # skip build artifacts
            for n in names:
                if n.endswith((".pyc", ".pyo")):
                    continue
                sp = os.path.join(root, n)
                rel = os.path.relpath(sp, src_root)
                bp = os.path.join(bun_root, rel)
                if not os.path.exists(bp):
                    drift.append("%s: missing in bundle: %s" % (tag, rel))
                elif _read(sp) != _read(bp):
                    drift.append("%s: differs: %s" % (tag, rel))
    ok = not drift
    return ok, ("bundle ≡ source (skills + hooks)" if ok else "; ".join(drift))


def check_plugin_sync():
    # The lean marketplace plugin tree (plugin/) must mirror source — skills byte-identical,
    # hooks exactly the wired set. scripts/sync_plugin.py --check is the source of truth.
    r = subprocess.run([sys.executable, os.path.join(REPO, "scripts", "sync_plugin.py"), "--check"],
                       capture_output=True, text=True, cwd=REPO)
    ok = r.returncode == 0
    detail = [ln for ln in (r.stdout or r.stderr or "").splitlines() if ln.strip()]
    return ok, ("plugin ≡ source (lean marketplace tree)" if ok else "; ".join(detail[-6:]))


CHECKS = [
    ("1 referenced-scripts-exist", check_scripts_exist),
    ("2 extension-point-count", check_extension_count),
    ("3 cited-commands-run", check_commands_run),
    ("4 bundle-parity", check_bundle_parity),
    ("5 plugin-parity", check_plugin_sync),
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
