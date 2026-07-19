import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "scripts", "mirror_parity.py")


def _run(*args):
    return subprocess.run([sys.executable, SCRIPT] + list(args), capture_output=True, text=True,
                          cwd=REPO, stdin=subprocess.DEVNULL, timeout=60)


def test_mirror_parity_selftest():
    r = _run("selftest")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "PASS" in r.stdout, r.stdout


def test_mirror_parity_check_emits_structured_json():
    r = _run("check")
    assert r.returncode in (0, 1), r.stdout + r.stderr
    payload = json.loads(r.stdout)
    assert "ok" in payload
    assert isinstance(payload.get("checks"), list)
    names = {row.get("name") for row in payload["checks"]}
    assert {"bundle_parity", "plugin_parity", "skill_pair_parity"} <= names


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _selfrun import run_module
    run_module(globals(), "test_mirror_parity")
