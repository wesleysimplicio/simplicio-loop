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


def test_video_evidence_playwright_blocks_without_url(tmp_path):
    # The DEFAULT engine is Playwright session recording; it needs --url. Without it the worker MUST
    # block (exit 3), never fake a "done" — same never-fake-pass discipline as the hyperframes path.
    r = _run(["video_evidence.py", "verify", "--name", "x", "--out", str(tmp_path / "v")],
             cwd=str(tmp_path))
    assert r.returncode == 3, "expected BLOCKED exit 3, got %d:\n%s" % (r.returncode, r.stdout)
    assert "blocked" in r.stdout.lower(), r.stdout
    assert "done" not in r.stdout.lower(), "fake-pass!\n%s" % r.stdout


def test_video_evidence_detect_intent():
    r = _run(["video_evidence.py", "detect", "--goal",
              "make a demo video of the login screen"], cwd=REPO)
    assert r.returncode == 0, r.stderr
    assert "video-task" in r.stdout, r.stdout


def test_video_evidence_detect_intent_multilingual():
    # The intent matcher is intentionally EN/PT/ES — keep coverage for non-English goals.
    for goal in ("faça um vídeo demonstrativo da tela de login",
                 "crea un video demo de la pantalla de inicio de sesión"):
        r = _run(["video_evidence.py", "detect", "--goal", goal], cwd=REPO)
        assert r.returncode == 0, r.stderr
        assert "video-task" in r.stdout, "%s -> %s" % (goal, r.stdout)


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


def test_repo_conventions_selftest():
    # The history-mining inference + formatters are model-free; the selftest proves them.
    r = _run(["repo_conventions.py", "selftest"], cwd=REPO)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "PASS" in r.stdout, r.stdout


def test_repo_conventions_formatters_default():
    # With no learned profile, formatters must produce a sane Conventional-Commits default and
    # map an item-type alias ('bug' -> 'fix') deterministically.
    b = _run(["repo_conventions.py", "branch", "--type", "bug", "--slug", "Null Token Crash",
              "--out", "no-such-conventions.json"], cwd=REPO)
    assert b.returncode == 0, b.stderr
    assert b.stdout.strip() == "fix/null-token-crash", b.stdout
    c = _run(["repo_conventions.py", "commit", "--type", "feature", "--scope", "auth",
              "--subject", "add SSO", "--out", "no-such-conventions.json"], cwd=REPO)
    assert c.returncode == 0, c.stderr
    assert c.stdout.strip() == "feat(auth): add SSO", c.stdout


def _run_anchor(args, env):
    return subprocess.run([sys.executable, os.path.join(REPO, "scripts", "task_anchor.py")] + args,
                          capture_output=True, text=True, cwd=REPO, env=env)


def test_task_anchor_gate_and_drift(tmp_path):
    # The anti-deviation guard: a freshly anchored task must BLOCK the done-gate (nothing verified),
    # flag a CHANGED goal as DRIFT, and only go READY once every AC is marked with a receipt.
    env = dict(os.environ, SIMPLICIO_ANCHOR_FILE=str(tmp_path / "anchor.json"))
    s = _run_anchor(["set", "--item", "9", "--goal", "Add SSO login",
                     "--ac", "Renders an SSO button", "--ac", "Redirects to the IdP"], env)
    assert s.returncode == 0, s.stdout + s.stderr

    g = _run_anchor(["gate", "--exit-code"], env)
    assert g.returncode == 12, "expected BLOCKED exit 12, got %d:\n%s" % (g.returncode, g.stdout)
    assert "blocked" in g.stdout.lower(), g.stdout

    d = _run_anchor(["check", "--goal", "refactor the database layer", "--exit-code"], env)
    assert d.returncode == 11, "expected DRIFT exit 11, got %d:\n%s" % (d.returncode, d.stdout)
    assert "drift" in d.stdout.lower(), d.stdout

    # marking done WITHOUT a receipt must be refused (no fake "done") and not record progress
    nm = _run_anchor(["mark", "--id", "AC1", "--status", "done"], env)
    assert nm.returncode == 12, nm.stdout
    assert "blocked" in nm.stdout.lower() and "requires --evidence" in nm.stdout.lower(), nm.stdout
    # the refused mark must NOT have advanced the gate
    still = _run_anchor(["gate", "--exit-code"], env)
    assert still.returncode == 12, "refused mark leaked progress:\n%s" % still.stdout

    _run_anchor(["mark", "--id", "AC1", "--status", "done", "--evidence", "a.png"], env)
    _run_anchor(["mark", "--id", "AC2", "--status", "done", "--evidence", "b.png"], env)
    ok = _run_anchor(["gate", "--exit-code"], env)
    assert ok.returncode == 0, "expected READY, got %d:\n%s" % (ok.returncode, ok.stdout)
    assert "ready" in ok.stdout.lower(), ok.stdout


def test_pr_evidence_blocks_without_evidence(tmp_path):
    # The "PR sem evidência" fix: with --require-evidence and neither a checklist nor a print,
    # building the PR body MUST block (exit 3) and never emit a body / claim done.
    env = dict(os.environ, SIMPLICIO_ANCHOR_FILE=str(tmp_path / "none.json"))
    r = subprocess.run([sys.executable, os.path.join(REPO, "scripts", "pr_evidence.py"), "build",
                        "--require-evidence", "--anchor", str(tmp_path / "none.json"),
                        "--shots-dir", str(tmp_path / "empty")],
                       capture_output=True, text=True, cwd=REPO, env=env)
    assert r.returncode == 3, "expected BLOCKED exit 3, got %d:\n%s" % (r.returncode, r.stdout)
    assert "blocked" in r.stdout.lower(), r.stdout
    assert "# " not in r.stdout, "leaked a PR body while blocked:\n%s" % r.stdout


def test_pr_evidence_builds_with_checklist_and_prints(tmp_path):
    # With an anchor + a captured print, the body MUST contain the item-by-item checklist and embed
    # the screenshot — the two things the client said were missing.
    anchor = str(tmp_path / "anchor.json")
    env = dict(os.environ, SIMPLICIO_ANCHOR_FILE=anchor)
    _run_anchor(["set", "--item", "9", "--goal", "Add SSO login",
                 "--ac", "Renders an SSO button"], env)
    _run_anchor(["mark", "--id", "AC1", "--status", "done", "--evidence", "login.png"], env)
    shots = tmp_path / "shots"
    shots.mkdir()
    (shots / "login.png").write_bytes(b"PNG")
    r = subprocess.run([sys.executable, os.path.join(REPO, "scripts", "pr_evidence.py"), "build",
                        "--title", "Add SSO login", "--item", "9", "--require-evidence",
                        "--anchor", anchor, "--shots-dir", str(shots)],
                       capture_output=True, text=True, cwd=REPO, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "[x] **AC1**" in r.stdout, "missing item-by-item checklist:\n%s" % r.stdout
    assert "login.png" in r.stdout, "missing embedded print:\n%s" % r.stdout


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _selfrun import run_module
    run_module(globals(), "test_worker_smoke")
