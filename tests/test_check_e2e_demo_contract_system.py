import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "scripts", "check_e2e_demo_contract.py")


def test_check_e2e_demo_contract_selftest():
    r = subprocess.run([sys.executable, SCRIPT, "selftest"], capture_output=True, text=True,
                       cwd=REPO, timeout=30, stdin=subprocess.DEVNULL)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "PASS" in r.stdout, r.stdout
