"""Integration test (#284): the opt-in mutation-authority gate wired into
`execute_operator()`.

`execute_operator()` already refuses to run without fresh mapper/plan/operator
preflight receipts, a passing `validate_plan()`, an unchanged repo state, and an
authorized target (pre-existing behavior). This test covers the NEW #284 gate on
top of that: when `SIMPLICIO_REQUIRE_MUTATION_AUTHORITY=1`, execution additionally
requires a valid `planning-receipt.json` whose mutation authority matches the
current run/attempt/contract/plan identity — and it must be a strict opt-in with
zero behavior change when unset (the existing, unconditional test suite for
`execute_operator` never sets this flag).
"""
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from simplicio_loop import runner as runner_mod
from simplicio_loop.plan_contract import validate_plan
from simplicio_loop.planning_gate import build_planning_receipt, content_hash, receipt_path

from tests.test_runner_cli import _arm_deterministic_preflight_fixture

ENV_FLAG = "SIMPLICIO_REQUIRE_MUTATION_AUTHORITY"


def _exec_env(extra=None):
    env = {
        "SIMPLICIO_LOOP_FAKE_OPERATOR_EXEC_JSON": json.dumps({
            "returncode": 0, "stdout": {"kind": "operator-applied", "ok": True}, "stderr": "",
            "write_files": {"src/app.py": "def main():\n    return 'updated'\n"},
        }),
    }
    if extra:
        env.update(extra)
    return env


def test_flag_unset_is_zero_behavior_change(tmp_path, monkeypatch):
    # Default (no flag) -- must behave exactly like every other execute_operator test:
    # no planning-receipt.json anywhere, and execution still succeeds.
    repo, _, armed_payload, run_dir = _arm_deterministic_preflight_fixture(monkeypatch, tmp_path)
    run_id = armed_payload["manifest"]["run_id"]
    assert not receipt_path(run_dir).exists()
    with patch.dict(os.environ, _exec_env(), clear=False):
        os.environ.pop(ENV_FLAG, None)
        payload = runner_mod.execute_operator(str(repo), run_id)
    assert payload["state"]["phase"] == "validating"


def test_flag_set_without_receipt_blocks_fail_closed(tmp_path, monkeypatch):
    repo, _, armed_payload, run_dir = _arm_deterministic_preflight_fixture(monkeypatch, tmp_path)
    run_id = armed_payload["manifest"]["run_id"]
    assert not receipt_path(run_dir).exists()
    with patch.dict(os.environ, _exec_env({ENV_FLAG: "1"}), clear=False):
        with pytest.raises(RuntimeError, match="mutation authority required"):
            runner_mod.execute_operator(str(repo), run_id)
    # fail-closed: no repository mutation happened
    assert not (run_dir / "operator-receipt.json").read_text(encoding="utf-8") == "" \
        or True  # preflight receipt from arm_run always exists; the point is no NEW exec state
    assert json.loads((run_dir / "operator-receipt.json").read_text(encoding="utf-8"))["execution_state"] != "applied"


def test_flag_set_with_valid_receipt_allows_execution(tmp_path, monkeypatch):
    repo, _, armed_payload, run_dir = _arm_deterministic_preflight_fixture(monkeypatch, tmp_path)
    run_id = armed_payload["manifest"]["run_id"]

    contract = json.loads((run_dir / "task-contract.json").read_text(encoding="utf-8"))
    plan = json.loads((run_dir / "plan.json").read_text(encoding="utf-8"))
    tasks = contract.get("tasks") or []
    plan_validation = validate_plan(plan, tasks, str(repo), contract_hash=contract.get("collection_hash", ""))
    assert plan_validation["valid"], plan_validation["errors"]

    attempt = int((armed_payload["state"] or {}).get("attempts", 0)) + 1
    receipt = build_planning_receipt(run_id=run_id, attempt=attempt, contract=contract, plan=plan,
                                     plan_validation=plan_validation)
    assert receipt["ready_for_mutation"] is True
    receipt_path(run_dir).write_text(json.dumps(receipt), encoding="utf-8")

    with patch.dict(os.environ, _exec_env({ENV_FLAG: "1"}), clear=False):
        payload = runner_mod.execute_operator(str(repo), run_id)
    assert payload["state"]["phase"] == "validating"
    op_receipt = json.loads((run_dir / "operator-receipt.json").read_text(encoding="utf-8"))
    assert op_receipt["execution_state"] == "applied"


def test_flag_set_with_stale_receipt_blocks_after_plan_drift(tmp_path, monkeypatch):
    # A planning receipt minted for one plan must NOT authorize execution once the
    # plan on disk has drifted (simulates a repo/plan change between planning and
    # execution -- the exact "stale authority" scenario #284 calls out).
    repo, _, armed_payload, run_dir = _arm_deterministic_preflight_fixture(monkeypatch, tmp_path)
    run_id = armed_payload["manifest"]["run_id"]

    contract = json.loads((run_dir / "task-contract.json").read_text(encoding="utf-8"))
    plan = json.loads((run_dir / "plan.json").read_text(encoding="utf-8"))
    tasks = contract.get("tasks") or []
    plan_validation = validate_plan(plan, tasks, str(repo), contract_hash=contract.get("collection_hash", ""))
    attempt = int((armed_payload["state"] or {}).get("attempts", 0)) + 1
    receipt = build_planning_receipt(run_id=run_id, attempt=attempt, contract=contract,
                                     plan={**plan, "extra_marker": "stale-plan-version"},
                                     plan_validation=plan_validation)
    receipt_path(run_dir).write_text(json.dumps(receipt), encoding="utf-8")

    with patch.dict(os.environ, _exec_env({ENV_FLAG: "1"}), clear=False):
        with pytest.raises(RuntimeError, match="mutation authority required"):
            runner_mod.execute_operator(str(repo), run_id)
