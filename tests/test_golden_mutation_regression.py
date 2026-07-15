import json, subprocess, sys
from pathlib import Path
from scripts.golden_mutation import run

def test_all_ordering_mutants_are_rejected_with_receipt():
    receipt = run(); assert receipt["schema"] == "simplicio.golden-mutation-receipt/v1"; assert receipt["match"] is True
    assert all(item["rejected"] for item in receipt["mutations"]); assert receipt["receipt_hash"]

def test_cli_emits_machine_readable_receipt():
    script = Path(__file__).resolve().parents[1] / "scripts" / "golden_mutation.py"
    result = subprocess.run([sys.executable, str(script), "--json"], capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr; assert json.loads(result.stdout)["match"] is True
