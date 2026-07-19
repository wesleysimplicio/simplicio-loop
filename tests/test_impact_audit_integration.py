import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IMPACT = os.path.join(REPO, "scripts", "impact_audit.py")


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.strip() + "\n", encoding="utf-8")


def _run(args, cwd):
    return subprocess.run([sys.executable, IMPACT] + args, capture_output=True, text=True, cwd=cwd)


def test_impact_audit_fails_on_transitive_reverse_dependency(tmp_path):
    _write(tmp_path / "app" / "util.py", """
def helper():
    return 1
""")
    _write(tmp_path / "app" / "service.py", """
from .util import helper

def compute():
    return helper()
""")
    _write(tmp_path / "app" / "controller.py", """
from app.service import compute

def run():
    return compute()
""")

    r = _run(["audit", str(tmp_path), "--file", "app/util.py", "--cover", "app/util.py"], cwd=REPO)
    assert r.returncode == 1, r.stdout
    assert "app/service.py" in r.stdout, r.stdout
    assert "app/controller.py" in r.stdout, r.stdout
    assert "uncovered_reverse_dependency" in r.stdout, r.stdout


def test_impact_audit_passes_when_callers_and_tests_are_covered(tmp_path):
    _write(tmp_path / "app" / "util.py", """
def helper():
    return 1
""")
    _write(tmp_path / "app" / "service.py", """
from .util import helper

def compute():
    return helper()
""")
    _write(tmp_path / "tests" / "test_service.py", """
from app.service import compute

def test_compute():
    assert compute() == 1
""")

    r = _run(
        [
            "audit",
            str(tmp_path),
            "--file",
            "app/util.py",
            "--cover",
            "app/util.py",
            "--cover",
            "app/service.py",
            "--cover",
            "tests/test_service.py",
        ],
        cwd=REPO,
    )
    assert r.returncode == 0, r.stdout
    assert "impact-audit: PASS" in r.stdout, r.stdout


def test_impact_audit_json_ok_tracks_fail_threshold(tmp_path):
    _write(tmp_path / "app" / "service.py", """
from .util import helper

def compute():
    return helper()
""")
    _write(tmp_path / "app" / "util.py", """
def helper():
    return 1
""")

    r = _run(
        [
            "audit",
            str(tmp_path),
            "--file",
            "app/service.py",
            "--cover",
            "app/service.py",
            "--fail-on",
            "medium",
            "--json",
        ],
        cwd=REPO,
    )
    assert r.returncode == 1, r.stdout
    payload = json.loads(r.stdout)
    assert payload["fail_on"] == "medium"
    assert payload["ok"] is False
    assert payload["counts"]["blocking_issues"] >= 1
    assert any(issue["code"] == "uncovered_local_dependency" for issue in payload["blocking_issues"])


def test_impact_audit_without_seed_is_blocked(tmp_path):
    r = _run(["audit", str(tmp_path)], cwd=REPO)
    assert r.returncode == 2, r.stdout
    assert "BLOCKED" in r.stdout, r.stdout


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _selfrun import run_module

    run_module(globals(), "test_impact_audit")
