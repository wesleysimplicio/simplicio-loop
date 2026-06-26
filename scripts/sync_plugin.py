#!/usr/bin/env python3
"""simplicio-loop — sync the LEAN marketplace plugin tree (`plugin/`) from source.

The repo doubles as a pip package (engine + proxy + token-monitor dashboard) AND a Claude
marketplace plugin. A marketplace install copies the WHOLE plugin source to the user's cache, so the
plugin must NOT carry the heavy pip-only assets. `plugin/` is therefore a SLIM mirror containing only
what the plugin actually loads:

  plugin/skills/   <- byte-identical copy of .claude/skills/  (the 6 skills)
  plugin/hooks/    <- ONLY the hooks wired by hooks.claude.json (+ orient_clamp, a runtime dep)

Excluded by design (pip-only, never wired into the plugin): the capture proxy (`engine/`), the
token-monitor dashboard (`hooks/simplicio_dashboard.py`), the 24/7 watcher (`hooks/simplicio_watch.py`),
the Cursor-only `loop_capture.py`/`hooks.json`, `scripts/`. Run this after editing skills or
a wired hook; `scripts/claims_audit.py` (check 5) fails if `plugin/` drifts from source.

Usage:  python3 scripts/sync_plugin.py        # rewrite plugin/skills + plugin/hooks from source
        python3 scripts/sync_plugin.py --check # exit 1 if plugin/ is out of sync (no writes)
"""
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

SRC_SKILLS = os.path.join(REPO, ".claude", "skills")
DST_SKILLS = os.path.join(REPO, "plugin", "skills")
SRC_HOOKS = os.path.join(REPO, "hooks")
DST_HOOKS = os.path.join(REPO, "plugin", "hooks")

# The ONLY hook files the marketplace plugin ships: those wired in hooks.claude.json + their deps.
# loop_stop/learn_stop (Stop) · action_gate/orient_rewrite (PreToolUse) · orient_clamp (orient_rewrite
# shells out to it) · hooks.claude.json (the wiring) · README.md (lean doc).
LEAN_HOOKS = ["loop_stop.py", "learn_stop.py", "action_gate.py", "orient_rewrite.py",
              "orient_clamp.py", "hooks.claude.json"]


def _read(p):
    with open(p, "rb") as f:
        return f.read()


def _walk_rel(root):
    out = []
    for r, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for n in files:
            if n.endswith((".pyc", ".pyo")):
                continue
            out.append(os.path.relpath(os.path.join(r, n), root))
    return sorted(out)


def sync():
    # skills: full byte-identical mirror
    if os.path.isdir(DST_SKILLS):
        shutil.rmtree(DST_SKILLS)
    shutil.copytree(SRC_SKILLS, DST_SKILLS, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    # hooks: only the lean wired set
    if os.path.isdir(DST_HOOKS):
        shutil.rmtree(DST_HOOKS)
    os.makedirs(DST_HOOKS, exist_ok=True)
    for name in LEAN_HOOKS:
        src = os.path.join(SRC_HOOKS, name)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(DST_HOOKS, name))
    print("synced plugin/: %d skill files, %d hook files" % (
        len(_walk_rel(DST_SKILLS)), len(_walk_rel(DST_HOOKS))))


def check():
    """Return list of drift strings (empty == in sync)."""
    drift = []
    if not os.path.isdir(DST_SKILLS):
        return ["plugin/skills missing — run scripts/sync_plugin.py"]
    src = set(_walk_rel(SRC_SKILLS))
    dst = set(_walk_rel(DST_SKILLS))
    for rel in sorted(src - dst):
        drift.append("plugin/skills: missing %s" % rel)
    for rel in sorted(dst - src):
        drift.append("plugin/skills: extra %s" % rel)
    for rel in sorted(src & dst):
        if _read(os.path.join(SRC_SKILLS, rel)) != _read(os.path.join(DST_SKILLS, rel)):
            drift.append("plugin/skills: differs %s" % rel)
    # hooks: exactly the lean set, each byte-identical to source; none of the excluded files present
    have = set(_walk_rel(DST_HOOKS)) if os.path.isdir(DST_HOOKS) else set()
    want = set(n for n in LEAN_HOOKS if os.path.exists(os.path.join(SRC_HOOKS, n)))
    for rel in sorted(want - have):
        drift.append("plugin/hooks: missing %s" % rel)
    for rel in sorted(have - want):
        drift.append("plugin/hooks: unexpected %s (lean plugin ships only the wired set)" % rel)
    for rel in sorted(want & have):
        if _read(os.path.join(SRC_HOOKS, rel)) != _read(os.path.join(DST_HOOKS, rel)):
            drift.append("plugin/hooks: differs %s" % rel)
    return drift


def main():
    if "--check" in sys.argv[1:]:
        drift = check()
        if drift:
            print("plugin sync: DRIFT (%d)" % len(drift))
            for d in drift:
                print("  " + d)
            sys.exit(1)
        print("plugin sync: ok (plugin/ == source)")
        sys.exit(0)
    sync()


if __name__ == "__main__":
    main()
