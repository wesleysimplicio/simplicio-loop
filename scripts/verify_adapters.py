#!/usr/bin/env python3
"""simplicio-tasks — adapter install-contract verifier (per-runtime e2e).

Runs the universal installer into a throwaway target for each runtime and asserts the adapter's
contract actually lands: the 6 skills are copied, the runtime's entry/instructions file exists and
carries the idempotent marker block, hooks are present where the adapter promises them, and (for
Claude) the loop hooks are wired into settings.json. This is the runnable half of issue #11 — it
proves the installer + thin adapters end-to-end on THIS machine, in isolation, with zero risk to
the user's real config (always `--target <tmpdir>`, never `--global`).

What it does NOT cover: launching the actual runtime binary (antigravity / kiro / opencode /
aider) and running `/simplicio-tasks` inside it. That manual smoke step is listed per-runtime in
adapters/MATRIX.md and the per-adapter READMEs; this harness gates everything up to that point.

Usage:
    python3 scripts/verify_adapters.py [runtime ...]   # default: all 11
    python3 scripts/verify_adapters.py antigravity kiro opencode aider   # the #11 TODO set
Exit code 0 = all verified, 1 = at least one runtime failed its contract.
"""
import os
import shutil
import subprocess
import sys
import tempfile

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import install_lib  # single source of truth for SKILLS + RUNTIMES + marker  # noqa: E402

REPO = os.path.dirname(HERE)


def _install(runtime, target):
    """Invoke the real installer CLI (true e2e), isolated to `target`."""
    return subprocess.run(
        [sys.executable, os.path.join(HERE, "install_lib.py"), runtime, "--target", target],
        capture_output=True, text=True, encoding="utf-8", errors="replace")


def _check(runtime, target):
    """Return a list of failure strings ([] = contract satisfied)."""
    fails = []
    cfg = install_lib.RUNTIMES[runtime]

    # 1) all 6 skills copied
    skills_root = os.path.join(target, ".claude", "skills")
    for s in install_lib.SKILLS:
        if not os.path.isdir(os.path.join(skills_root, s)):
            fails.append("missing skill: %s" % s)
    # a skill must carry its SKILL.md (not just an empty dir)
    st = os.path.join(skills_root, "simplicio-tasks", "SKILL.md")
    if not os.path.isfile(st):
        fails.append("simplicio-tasks/SKILL.md not copied")

    # 2) entry file exists and carries the idempotent marker block
    if cfg["entry"]:
        entry = os.path.join(target, cfg["entry"])
        if not os.path.isfile(entry):
            fails.append("entry file missing: %s" % cfg["entry"])
        else:
            with open(entry, encoding="utf-8") as f:
                body = f.read()
            if install_lib.MARK_A not in body or install_lib.MARK_B not in body:
                fails.append("entry file lacks marker block: %s" % cfg["entry"])

    # 3) hooks present (project install copies hooks/ unless target IS the source repo)
    if os.path.abspath(target) != os.path.abspath(REPO):
        hook = os.path.join(install_lib.hooks_dir(target, False), "loop_stop.py")
        if not os.path.isfile(hook):
            fails.append("hooks not copied (loop_stop.py absent)")

    # 4) Claude wires the loop into settings.json
    if cfg["hooks"] == "claude":
        settings = os.path.join(target, ".claude", "settings.json")
        if not os.path.isfile(settings):
            fails.append("claude settings.json not written")
        else:
            with open(settings, encoding="utf-8") as f:
                if "loop_stop.py" not in f.read():
                    fails.append("Stop hook (loop_stop.py) not wired into settings.json")
    return fails


def verify(runtime):
    target = tempfile.mkdtemp(prefix="st-verify-%s-" % runtime)
    try:
        r = _install(runtime, target)
        if r.returncode != 0:
            return ["installer exited %d: %s" % (r.returncode, (r.stderr or r.stdout).strip()[:300])]
        return _check(runtime, target)
    finally:
        shutil.rmtree(target, ignore_errors=True)


def main():
    runtimes = sys.argv[1:] or list(install_lib.RUNTIMES)
    unknown = [r for r in runtimes if r not in install_lib.RUNTIMES]
    if unknown:
        print("unknown runtime(s): %s" % " ".join(unknown))
        sys.exit(2)
    print("adapter install-contract verification (%d runtime(s))" % len(runtimes))
    print("-" * 60)
    failed = 0
    for rt in runtimes:
        fails = verify(rt)
        if fails:
            failed += 1
            print("FAIL  %-12s" % rt)
            for f in fails:
                print("        - " + f)
        else:
            print("PASS  %-12s  skills+entry+hooks landed" % rt)
    print("-" * 60)
    print("%d passed, %d failed" % (len(runtimes) - failed, failed))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
