"""CLI-level clock-injected rate-limit test for issue #301 AC7.

The #301 review comments flagged this precise gap: "no clock-injected rate-limit test at the CLI
(progress-comment) level; the underlying pure functions are tested with an injected clock" (see
`tests/test_delivery_progress.py::test_rate_limit_blocks_within_window_and_clears_after`, which
only calls `pr_evidence._rate_limited`/`_record_post` in-process).

This closes that gap by driving the REAL `python3 scripts/pr_evidence.py progress-comment` CLI via
subprocess — not the internal helper functions — and proving the rate-limit gate end to end:

  * two calls ~1s apart (by injected clock) -> the SECOND is suppressed ("skip" on stdout, the
    `gh` post never happens for it);
  * two calls ~61s apart (by injected clock) -> the SECOND is allowed through (no "skip").

To make this deterministic and network-free, `scripts/pr_evidence.py` grew a minimal, real
clock-injection seam for exactly this purpose (#301 AC7): `--now-epoch <epoch-seconds>` and
`--state-path <file>` flags on `progress-comment`, threaded into `_rate_limited`/`_record_post`
(previously only reachable from in-process unit tests, never from the CLI). No real sleeping, no
mocking of internals — the CLI subprocess is driven with a controlled clock and an isolated rate
limiter state file, and a stub `gh` on PATH stands in for the network dependency (this test is
about the rate limiter, not about GitHub's API — that live round trip is already covered by
`tests/test_progress_comment_live_e2e.py`, AC4).
"""
import os
import stat
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PR_EVIDENCE = os.path.join(REPO, "scripts", "pr_evidence.py")

_GH_STUB_PY = '''\
import json
import sys

args = sys.argv[1:]
# `gh api ... -f body=...` (create) or `gh api -X PATCH ...` (update) -> a write; anything else
# (the `--paginate` comment listing) -> a read.
if "-f" in args or "-X" in args:
    print(json.dumps({"id": 999001, "html_url": "https://example.invalid/issues/comments/999001"}))
else:
    print("[]")
sys.exit(0)
'''

_GH_STUB_CMD = '@echo off\r\n"%s" "%%~dp0gh_stub.py" %%*\r\n'
_GH_STUB_SH = '#!/bin/sh\n"%s" "$(dirname "$0")/gh_stub.py" "$@"\n'


def _install_stub_gh(bin_dir):
    """Write a fake `gh` executable into `bin_dir` that answers instantly, offline, with a
    plausible JSON reply — enough for progress-comment's `shutil.which("gh")` check and its
    `_gh_run` subprocess calls to succeed without ever touching the network."""
    os.makedirs(bin_dir, exist_ok=True)
    stub_py = os.path.join(bin_dir, "gh_stub.py")
    with open(stub_py, "w", encoding="utf-8") as f:
        f.write(_GH_STUB_PY)
    if os.name == "nt":
        wrapper = os.path.join(bin_dir, "gh.cmd")
        with open(wrapper, "w", encoding="utf-8") as f:
            f.write(_GH_STUB_CMD % sys.executable)
    else:
        wrapper = os.path.join(bin_dir, "gh")
        with open(wrapper, "w", encoding="utf-8") as f:
            f.write(_GH_STUB_SH % sys.executable)
        st = os.stat(wrapper)
        os.chmod(wrapper, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    # PATH entries are directories, never executable paths. Returning the
    # wrapper here made callers construct ``.../stubbin/gh:$PATH`` and caused
    # the real CLI to report that gh was unavailable.
    return bin_dir


def _env_with_stub_gh(bin_dir):
    env = dict(os.environ)
    env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")
    return env


def _run(env, now_epoch, state_path, issue="12345", min_interval=60):
    return subprocess.run(
        [sys.executable, PR_EVIDENCE, "progress-comment",
         "--issue", issue,
         "--min-interval", str(min_interval),
         "--now-epoch", str(now_epoch),
         "--state-path", state_path],
        capture_output=True, text=True, cwd=REPO, env=env, stdin=subprocess.DEVNULL)


def test_second_call_within_window_is_suppressed_via_injected_clock(tmp_path):
    """Two CLI calls ~1s apart (by injected clock, default 60s window) -> the second is a "skip",
    proving the rate limiter engages at the real CLI entrypoint, not just in the pure function."""
    bin_dir = str(tmp_path / "stubbin")
    env = _env_with_stub_gh(_install_stub_gh(bin_dir))
    state_path = str(tmp_path / "state.json")

    r1 = _run(env, now_epoch=1_700_000_000.0, state_path=state_path)
    assert r1.returncode == 0, r1.stdout + r1.stderr
    assert "skip" not in r1.stdout, "first call should NOT be rate-limited: %r" % r1.stdout
    assert "progress-comment" in r1.stdout, r1.stdout + r1.stderr

    r2 = _run(env, now_epoch=1_700_000_001.0, state_path=state_path)  # +1s
    assert r2.returncode == 0, r2.stdout + r2.stderr
    assert r2.stdout.strip() == "skip", (
        "second call 1s later (within the 60s window) should be suppressed, got: %r" % r2.stdout)


def test_second_call_after_window_elapses_is_allowed_via_injected_clock(tmp_path):
    """Two CLI calls ~61s apart (by injected clock) -> the second is allowed through (no "skip"),
    same real CLI entrypoint and isolated state file as the suppressed case above."""
    bin_dir = str(tmp_path / "stubbin")
    env = _env_with_stub_gh(_install_stub_gh(bin_dir))
    state_path = str(tmp_path / "state.json")

    r1 = _run(env, now_epoch=1_700_000_000.0, state_path=state_path)
    assert r1.returncode == 0, r1.stdout + r1.stderr
    assert "skip" not in r1.stdout, "first call should NOT be rate-limited: %r" % r1.stdout

    r2 = _run(env, now_epoch=1_700_000_061.0, state_path=state_path)  # +61s
    assert r2.returncode == 0, r2.stdout + r2.stderr
    assert r2.stdout.strip() != "skip", (
        "second call 61s later (past the 60s window) should be allowed through, got: %r"
        % r2.stdout)
    assert "progress-comment" in r2.stdout, r2.stdout + r2.stderr


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _selfrun import run_module
    run_module(globals(), "test_progress_comment_ratelimit_cli_e2e")
