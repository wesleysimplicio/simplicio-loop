#!/usr/bin/env python3
"""simplicio-loop — pre-commit hook: auto-sync plugin/ and _bundle/ from source (#98).

Triggered by git pre-commit. Detects staged changes in the monitored source paths
(defined in mirror_manifest.py as the single source of truth) and runs
`sync_plugin.py` automatically, adding regenerated files to the commit.

Fail-open: if the sync fails (e.g. python3 unavailable in the hook), the commit
proceeds silently and `python3 scripts/check.py` catches the drift as a backstop.

Install:
    # Via installer:
    bash scripts/install.sh <runtime> --global   # includes this hook

    # Manual:
    cp hooks/pre-commit.py .git/hooks/pre-commit
    chmod +x .git/hooks/pre-commit

Refs: #98 (auto-sync), #74 (mirror_manifest.py single source of truth).
"""

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

# Paths to watch — defined in mirror_manifest.py as the single source of truth.
# Any staged change to these triggers an auto-sync of plugin/ and _bundle/.
WATCHED_PREFIXES = [
    os.path.join(".claude", "skills"),
    "hooks/",
    "scripts/",
]


def _monitored_paths():
    """Return set of absolute paths under REPO that are monitored."""
    watched = set()
    for prefix in WATCHED_PREFIXES:
        full = os.path.join(REPO, prefix)
        if os.path.exists(full):
            watched.add(full)
    return watched


def _staged_changes_touch_monitored():
    """Check if any staged file is under a monitored path."""
    try:
        r = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            capture_output=True, text=True, cwd=REPO, timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False  # can't check → don't block (fail-open)

    if r.returncode != 0:
        return False

    for rel_path in r.stdout.splitlines():
        for watched in _monitored_paths():
            abs_path = os.path.join(REPO, rel_path)
            if os.path.commonpath([abs_path, watched]) == watched:
                return True
    return False


def _run_sync():
    """Run sync_plugin.py; returns True on success, False on failure (fail-open)."""
    sync_path = os.path.join(REPO, "scripts", "sync_plugin.py")
    if not os.path.exists(sync_path):
        return True  # nothing to sync

    try:
        r = subprocess.run(
            [sys.executable, sync_path],
            capture_output=True, text=True, cwd=REPO, timeout=60,
        )
        if r.returncode != 0:
            print("[pre-commit] sync_plugin.py warning: %s" % (r.stderr or r.stdout)[:200])
            return False
        return True
    except (subprocess.TimeoutExpired, OSError) as e:
        print("[pre-commit] sync_plugin.py error (fail-open): %s" % e)
        return False


def _stage_generated_files():
    """Stage any files under plugin/ that were regenerated."""
    try:
        subprocess.run(
            ["git", "add", os.path.join(REPO, "plugin")],
            capture_output=True, cwd=REPO, timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError):
        pass  # fail-open


def main():
    if not _staged_changes_touch_monitored():
        return 0  # nothing to sync — pass

    print("[pre-commit] simplicio-loop: source files changed — syncing plugin/...")
    _run_sync()
    _stage_generated_files()
    return 0  # always exit 0 (fail-open — check.py is the backstop)


if __name__ == "__main__":
    sys.exit(main())
