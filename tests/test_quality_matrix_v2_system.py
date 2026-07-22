import json
import subprocess
import sys
from pathlib import Path
from jsonschema import Draft202012Validator

from tests.test_quality_matrix_v2_unit import valid_receipt

REPO = Path(__file__).resolve().parents[1]


def test_external_provider_conformance_cli_and_oracle_dispatch(tmp_path):
    receipt = tmp_path / "quality-matrix.json"
    receipt.write_text(json.dumps(valid_receipt()), encoding="utf-8")
    run = subprocess.run([sys.executable, str(REPO / "scripts/quality_matrix_v2.py"), "validate", str(receipt)],
                         text=True, capture_output=True)
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout)["ready"] is True
    from simplicio_loop.quality_matrix import evaluate_quality_matrix
    assert evaluate_quality_matrix(str(tmp_path))["ready"] is True
    schema = json.loads((REPO / "contracts/quality-matrix/v2/schema.json").read_text())
    Draft202012Validator(schema).validate(valid_receipt())


def test_migration_cli_round_trip_is_stable_and_fail_closed(tmp_path):
    old = tmp_path / "v1.json"; out = tmp_path / "v2.json"
    old.write_text(json.dumps({"schema": "simplicio.quality-matrix/v1", "requirements": {"unit": {"status": "pass"}}}), encoding="utf-8")
    run = subprocess.run([sys.executable, str(REPO / "scripts/quality_matrix_v2.py"), "migrate-v1", str(old), "--output", str(out)], capture_output=True, text=True)
    assert run.returncode == 0
    migrated = json.loads(out.read_text())
    assert migrated["lanes"]["unit_component"]["status"] == "BLOCKED"


def test_benchmark_surface_publishes_all_hot_path_numbers():
    run = subprocess.run([sys.executable, str(REPO / "scripts/quality_matrix_v2_bench.py"), "--repeats", "25"], capture_output=True, text=True)
    assert run.returncode == 0
    assert set(json.loads(run.stdout)["median_us"]) == {"parse", "projection", "oracle"}
