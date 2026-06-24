"""Smoke tests for the evidence producers — the safety property that matters most:

a MISSING toolchain (or missing input) must yield **BLOCKED**, never a fake PASS. A demo video or
a web check that never actually ran can never be presented as proof. We assert the worker exits
with the BLOCKED code (3) and the word "blocked", and that it does NOT print "done".

Also smoke-tests the deterministic, model-free intent detector of `video_evidence` (no toolchain
needed) so the routing logic is covered.
"""
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run(args, cwd):
    return subprocess.run([sys.executable, os.path.join(REPO, "scripts", args[0])] + args[1:],
                          capture_output=True, text=True, cwd=cwd)


def test_video_evidence_blocks_without_composition(tmp_path):
    # render with no scaffolded composition (and/or no Node/FFmpeg) MUST block, never fake-pass.
    r = _run(["video_evidence.py", "render", "--name", "nope",
              "--out", str(tmp_path / "vid")], cwd=str(tmp_path))
    assert r.returncode == 3, "expected BLOCKED exit 3, got %d:\n%s" % (r.returncode, r.stdout)
    assert "blocked" in r.stdout.lower(), r.stdout
    assert "done" not in r.stdout.lower(), "fake-pass! render claimed done:\n%s" % r.stdout


def test_video_evidence_detect_intent_pt():
    r = _run(["video_evidence.py", "detect", "--goal",
              "faça um vídeo demonstrativo da tela de login"], cwd=REPO)
    assert r.returncode == 0, r.stderr
    assert "video-task" in r.stdout, r.stdout


def test_video_evidence_detect_skips_code_task():
    r = _run(["video_evidence.py", "detect", "--goal",
              "fix the login timeout bug and add a unit test"], cwd=REPO)
    assert r.returncode == 0, r.stderr
    assert "skip" in r.stdout, r.stdout


def test_web_verify_blocks_without_toolchain(tmp_path):
    # In an environment without Playwright/npx this MUST block, not fake-pass. If the toolchain
    # happens to be present the run may pass/fail on a dead URL — either way it must never silently
    # claim success without doing the work, so we only assert it does not fake a "done" while
    # also reporting blocked.
    r = _run(["web_verify.py", "run", "--url", "http://127.0.0.1:0/", "--expect", "x",
              "--out", str(tmp_path / "web")], cwd=str(tmp_path))
    out = r.stdout.lower()
    if "blocked" in out:
        assert r.returncode == 3, r.stdout
        assert "done" not in out, "fake-pass!\n%s" % r.stdout
    else:
        # toolchain present: a connection to port 0 cannot succeed → must be fail, never done
        assert "done" not in out, "web_verify claimed done against an unreachable URL:\n%s" % r.stdout


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _selfrun import run_module
    run_module(globals(), "test_worker_smoke")
