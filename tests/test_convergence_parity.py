import copy
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from simplicio_loop.convergence_parity import FixtureError, evaluate_fixture, main


FIXTURES = Path(__file__).parent / "fixtures" / "convergence_parity"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_same_fixture_produces_equivalent_convergence_and_evidence_receipts(tmp_path):
    receipt = evaluate_fixture(load_fixture("verified.json"), repo=tmp_path)

    assert receipt["schema"] == "simplicio.convergence-parity/v1"
    assert receipt["status"] == "VERIFIED"
    assert receipt["parity"] is True
    assert receipt["runtime_backed"]["status"] == "VERIFIED"
    assert receipt["standalone"]["status"] == "VERIFIED"
    assert (
        receipt["runtime_backed"]["semantic_receipts"]
        == receipt["standalone"]["semantic_receipts"]
    )
    assert (
        receipt["runtime_backed"]["acceptance_evidence"]
        == receipt["standalone"]["acceptance_evidence"]
    )
    assert (
        receipt["runtime_backed"]["execution_report"]["schema"]
        == "simplicio.execution-report/v1"
    )
    assert (
        receipt["runtime_backed"]["execution_report"]["execution_profile"]
        == "runtime-backed"
    )
    assert (
        receipt["standalone"]["execution_report"]["execution_profile"]
        == "operator-standalone"
    )
    assert (
        receipt["runtime_backed"]["execution_report"]["provenance"]
        == receipt["standalone"]["execution_report"]["provenance"]
    )


def test_missing_runtime_is_unsupported_without_standalone_fallback(tmp_path):
    receipt = evaluate_fixture(load_fixture("runtime-unavailable.json"), repo=tmp_path)

    assert receipt["status"] == "UNSUPPORTED"
    assert receipt["parity"] is False
    assert receipt["comparison"] == {
        "semantic_receipts_equal": None,
        "acceptance_evidence_equal": None,
    }
    assert receipt["runtime_backed"]["status"] == "UNSUPPORTED"
    assert receipt["runtime_backed"]["reason_code"] == "runtime_decision_absent"
    assert receipt["runtime_backed"]["effects_attempted"] is False
    assert receipt["runtime_backed"]["semantic_receipts"] == []
    assert receipt["standalone"]["status"] == "VERIFIED"
    assert (
        receipt["standalone"]["runtime_observation"]["reason_code"]
        == "runtime_binary_not_found"
    )
    task_metrics = receipt["runtime_backed"]["execution_report"]["tasks"][0]
    assert task_metrics["tokens"]["tokens_in"] is None
    assert task_metrics["tokens"]["tokens_out"] is None
    assert task_metrics["tokens"]["source"] == "absent"
    assert (
        "tokens_*"
        in receipt["runtime_backed"]["execution_report"]["unavailable_reasons"]
    )


@pytest.mark.parametrize(
    ("decision_change", "reason_code"),
    [
        ({"use_loop": False}, "runtime_loop_not_activated"),
        (
            {"schema": "simplicio.loop-policy-decision/v0"},
            "runtime_decision_incompatible",
        ),
        ({"host_may_override": True}, "runtime_authority_invalid"),
        ({"task_fingerprint": ""}, "runtime_provenance_absent"),
    ],
)
def test_runtime_activation_must_be_authoritative_and_compatible(
    tmp_path, decision_change, reason_code
):
    fixture = load_fixture("verified.json")
    fixture["runtime_decision"].update(decision_change)

    receipt = evaluate_fixture(fixture, repo=tmp_path)

    assert receipt["status"] == "UNSUPPORTED"
    assert receipt["runtime_backed"]["reason_code"] == reason_code
    assert receipt["runtime_backed"]["effects_attempted"] is False
    assert receipt["runtime_backed"]["semantic_receipts"] == []


def test_standalone_requires_explicit_runtime_absence_and_no_effect_attempt(tmp_path):
    fixture = load_fixture("verified.json")
    fixture["standalone_runtime"] = {
        "status": "unavailable",
        "reason_code": "runtime_unavailable",
        "effects_attempted": True,
    }

    receipt = evaluate_fixture(fixture, repo=tmp_path)

    assert receipt["status"] == "UNSUPPORTED"
    assert receipt["standalone"]["reason_code"] == "standalone_effect_boundary_unsafe"
    assert receipt["standalone"]["semantic_receipts"] == []


def test_module_cli_emits_verified_system_receipt(tmp_path):
    repo_root = Path(__file__).parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(repo_root)
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "simplicio_loop.convergence_parity",
            str(FIXTURES / "verified.json"),
            "--repo",
            str(tmp_path),
        ],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    receipt = json.loads(completed.stdout)
    assert receipt["status"] == "VERIFIED"
    assert receipt["parity"] is True


@pytest.mark.external_integration
def test_installed_runtime_decision_drives_runtime_backed_path(tmp_path):
    runtime = shutil.which("simplicio")
    if runtime is None:
        pytest.skip("UNSUPPORTED|simplicio Runtime CLI is not installed")
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    completed = subprocess.run(
        [
            runtime,
            "loop",
            "decide",
            "--task",
            "Implement issue 1140 and prove convergence parity",
            "--repo",
            str(tmp_path),
            "--json",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        pytest.skip(
            "UNSUPPORTED|Runtime loop decision failed: "
            f"exit={completed.returncode} stderr={completed.stderr.strip()[:200]}"
        )
    try:
        decision = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        pytest.skip(f"UNSUPPORTED|Runtime loop decision was not JSON: {exc}")

    fixture = copy.deepcopy(load_fixture("verified.json"))
    receipt = evaluate_fixture(fixture, repo=tmp_path, runtime_decision=decision)

    assert decision["authority"] == "runtime"
    assert decision["host_may_override"] is False
    assert decision["use_loop"] is True
    assert receipt["status"] == "VERIFIED"
    assert receipt["runtime_backed"]["runtime_decision"] == decision
    assert receipt["parity"] is True


def test_module_cli_help_describes_parity_protocol():
    completed = subprocess.run(
        [sys.executable, "-m", "simplicio_loop.convergence_parity", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0
    assert "Runtime-backed and standalone paths" in completed.stdout
    assert "--runtime-decision" in completed.stdout


def test_fixture_and_receipts_conform_to_public_json_schemas(tmp_path):
    jsonschema = pytest.importorskip("jsonschema")
    contracts = Path(__file__).parents[1] / "simplicio_loop" / "_contracts"
    fixture_schema = json.loads(
        (contracts / "convergence-parity-fixture-v1.schema.json").read_text(
            encoding="utf-8"
        )
    )
    receipt_schema = json.loads(
        (contracts / "convergence-parity-v1.schema.json").read_text(encoding="utf-8")
    )

    for fixture_name in ("verified.json", "runtime-unavailable.json"):
        fixture = load_fixture(fixture_name)
        receipt = evaluate_fixture(fixture, repo=tmp_path)
        jsonschema.Draft202012Validator(fixture_schema).validate(fixture)
        jsonschema.Draft202012Validator(receipt_schema).validate(receipt)


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("schema", "fixture schema"),
        ("fixture_id", "fixture_id is required"),
        ("provenance", "fixture provenance"),
        ("task", "fixture task must be a mapping"),
        ("task_fields", "fixture task_id and title are required"),
        ("signals", "fixture signals must be a non-empty sequence"),
        ("loop_evidence", "fixture loop_evidence must be a sequence"),
        ("length", "signals and loop_evidence must have the same length"),
        ("entry", "signals and loop_evidence entries must be mappings"),
    ],
)
def test_malformed_fixtures_fail_closed(tmp_path, case, message):
    fixture = load_fixture("verified.json")
    if case == "schema":
        fixture["schema"] = "wrong"
    elif case == "fixture_id":
        fixture["fixture_id"] = ""
    elif case == "provenance":
        fixture["provenance"] = {}
    elif case == "task":
        fixture["task"] = None
    elif case == "task_fields":
        fixture["task"]["title"] = ""
    elif case == "signals":
        fixture["signals"] = []
    elif case == "loop_evidence":
        fixture["loop_evidence"] = "invalid"
    elif case == "length":
        fixture["loop_evidence"] = fixture["loop_evidence"][:-1]
    else:
        fixture["signals"][0] = "invalid"

    with pytest.raises(FixtureError, match=message):
        evaluate_fixture(fixture, repo=tmp_path)


@pytest.mark.parametrize(
    ("observation", "reason_code"),
    [
        (None, "standalone_runtime_observation_absent"),
        (
            {
                "status": "available",
                "reason_code": "present",
                "effects_attempted": False,
            },
            "standalone_runtime_status_invalid",
        ),
        (
            {"status": "unavailable", "reason_code": "", "effects_attempted": False},
            "standalone_runtime_reason_absent",
        ),
    ],
)
def test_standalone_runtime_observation_must_be_explicit(
    tmp_path, observation, reason_code
):
    fixture = load_fixture("verified.json")
    fixture["standalone_runtime"] = observation

    receipt = evaluate_fixture(fixture, repo=tmp_path)

    assert receipt["status"] == "UNSUPPORTED"
    assert receipt["standalone"]["reason_code"] == reason_code
    assert receipt["standalone"]["effects_attempted"] is False


def test_equivalent_non_terminal_paths_fail_instead_of_fabricating_convergence(
    tmp_path,
):
    fixture = load_fixture("verified.json")
    fixture["signals"] = fixture["signals"][:1]
    fixture["loop_evidence"] = fixture["loop_evidence"][:1]

    receipt = evaluate_fixture(fixture, repo=tmp_path)

    assert receipt["parity"] is True
    assert receipt["runtime_backed"]["status"] == "FAILED"
    assert receipt["standalone"]["status"] == "FAILED"
    assert receipt["status"] == "FAILED"


def test_main_reports_invalid_and_unsupported_inputs(capsys, tmp_path):
    missing = tmp_path / "missing.json"
    assert main([str(missing), "--repo", str(tmp_path)]) == 2
    invalid = json.loads(capsys.readouterr().out)
    assert invalid["status"] == "INVALID"

    assert (
        main([str(FIXTURES / "runtime-unavailable.json"), "--repo", str(tmp_path)]) == 2
    )
    unsupported = json.loads(capsys.readouterr().out)
    assert unsupported["status"] == "UNSUPPORTED"
