import json
import subprocess
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "execution_board_e2e.py"


def test_issue_183_aggregate_receipt_is_honest_about_local_and_external_boundaries(tmp_path):
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--out", str(tmp_path)],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads((tmp_path / "distributed-epic-evidence.json").read_text(encoding="utf-8"))
    assert payload["schema"] == "simplicio.distributed-epic-evidence/v1"
    assert payload["issue"] == 183
    assert payload["epic_closure_ready"] is False
    assert payload["criteria_audited"] == [6, 9]
    assert payload["criteria_not_audited"] == [1, 2, 3, 4, 5, 7, 8]
    assert payload["external_boundaries"]["physical_machines"] == "UNVERIFIED"
    assert payload["external_boundaries"]["tls_deploy"] == "UNVERIFIED"
    assert payload["external_boundaries"]["external_release"] == "UNVERIFIED"

    criteria = {row["criterion_id"]: row for row in payload["criteria"]}
    criterion6 = criteria[6]
    assert criterion6["same_queue_adapter_contracts"]["codex"]["contract_verified"] is True
    assert criterion6["same_queue_adapter_contracts"]["claude"]["contract_verified"] is True
    assert criterion6["physical_machine_status"] == "UNVERIFIED"
    assert criterion6["tls_deploy_status"] == "UNVERIFIED"
    assert criterion6["external_release_status"] == "UNVERIFIED"

    criterion9 = criteria[9]
    assert criterion9["tag"] == "MEASURED"
    assert criterion9["local_convergence_status"] == "PASS"
    assert criterion9["fronts_total"] == 4
    assert criterion9["fronts_converged"] == 4
    assert criterion9["oracle_complete_after_all_fronts"] is True
    assert criterion9["drain_verdict"] == "DRAINED"
