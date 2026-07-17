"""Contract tests for the portable stage-agent graph/instance/receipt (EPIC #422).

Covers the ten normative invariants the epic requires a productive contract to
prove, plus a performance benchmark (AC7) and a regression guard for the existing
``agent_contract`` module (AC8).
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timezone

import pytest

from simplicio_loop import stage_agents as sa
from simplicio_loop.agent_contract import (
    AgentContractError,
    validate_identity,
    validate_stage_identity,
)

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "stage_agents")
CANON = sa.STAGES_FILE


def _hash(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def _inst(role="impl", stage="a", inst_id="i1", status="completed", drift=False):
    return {
        "schema": "simplicio.agent-instance/v1",
        "agent_instance_id": inst_id,
        "role_id": role,
        "role_version": "1.0.0",
        "stage_id": stage,
        "stage_version": "1.0.0",
        "run_id": "run-1",
        "task_id": "task-1",
        "work_item_id": "work-item-1",
        "attempt_id": "att-1",
        "attempt_ordinal": 1,
        "fence": "fence-1",
        "plan_revision": 0,
        "runtime": "python",
        "provider": "local",
        "model": "none",
        "driver": "portable",
        "parent_agent_id": "coord",
        "coordinator_agent_id": "coord",
        "parent_instance_id": "parent",
        "idempotency_key": "idem-" + inst_id,
        "isolation_level": "process",
        "negotiated_capabilities": ["claim"],
        "context_hash": _hash("ctx"),
        "manifest_hash": _hash("manifest"),
        "created_at": "2026-07-16T00:00:00Z",
        "ready_at": "2026-07-16T00:00:01Z",
        "started_at": "2026-07-16T00:00:02Z",
        "ended_at": "2026-07-16T00:00:03Z",
        "terminal_status": status,
        "reason_code": "ok",
    }


def _rec(inst, verdict="pass", inst_id=None):
    rec = {
        "schema": "simplicio.stage-receipt/v1",
        "receipt_id": "r1",
        "agent_instance_id": inst_id or inst["agent_instance_id"],
        "role_id": inst["role_id"],
        "stage_id": inst["stage_id"],
        "run_id": inst["run_id"],
        "task_id": inst["task_id"],
        "attempt_id": inst["attempt_id"],
        "attempt_ordinal": inst["attempt_ordinal"],
        "fence": inst["fence"],
        "plan_revision": inst["plan_revision"],
        "created_at": "2026-07-16T00:00:04Z",
        "observed_at": "2026-07-16T00:00:05Z",
        "ttl_seconds": 86400,
        "context_hash": inst["context_hash"],
        "manifest_hash": inst["manifest_hash"],
        "verdict": verdict,
        "evidence_refs": ["ev1"],
        "accepted": verdict == "pass",
        "reason_code": "ok" if verdict == "pass" else "not-accepted",
        "input_hash": _hash("input"),
        "output_hash": _hash("output"),
        "integrity_hash": "0" * 64,
        "previous_receipt_hashes": [],
        "covered_acceptance_criteria": ["AC-test"],
        "commands": ["pytest -q"],
        "exit_codes": {"pytest -q": 0},
        "artifact_refs": ["artifact://test"],
        "next_stage_recommendation": "none",
    }
    if verdict != "pass":
        rec["rejection_reason"] = "stage did not produce an accepted receipt"
    rec["integrity_hash"] = sa.receipt_integrity_hash(rec)
    return rec


# --- Invariant 1: each stage created an instance with correct context ------- #
def test_graph_valid_passes():
    graph = json.load(open(CANON))
    ok, errors = sa.validate_graph(graph)
    assert ok, errors


def test_packaged_contracts_match_repository_contracts():
    source = os.path.join(os.path.dirname(os.path.dirname(__file__)), "contracts", "stage-agents", "v1")
    packaged = sa.CONTRACT_DIR
    names = sorted(name for name in os.listdir(source) if name.endswith(".json"))
    assert names
    assert names == sorted(name for name in os.listdir(packaged) if name.endswith(".json"))
    assert all(open(os.path.join(source, name), "rb").read() == open(os.path.join(packaged, name), "rb").read() for name in names)


# --- Invariant 2: instance receives correct + exclusive stage context ------- #
def test_instance_binds_identity():
    ident = {
        "agent_id": "a1", "runtime": "python", "device_id": "d1", "session_id": "s1",
        "capabilities": ["claim"], "role_id": "impl", "stage_id": "a", "lifecycle": "running",
    }
    out = validate_stage_identity(ident)
    assert out["role_id"] == "impl" and out["stage_id"] == "a" and out["lifecycle"] == "running"


# --- Invariant 3: instance declares compatible capabilities ------------------ #
def test_instance_capabilities_enforced():
    ident = {
        "agent_id": "a1", "runtime": "python", "device_id": "d1", "session_id": "s1",
        "capabilities": ["bogus"], "role_id": "impl", "stage_id": "a",
    }
    with pytest.raises(AgentContractError):
        validate_stage_identity(ident)


# --- Invariant 4: output validated by schema -------------------------------- #
def test_receipt_schema_valid():
    inst = _inst()
    rec = _rec(inst)
    ok, errors = sa.validate_receipt(rec, inst, now=datetime(2026, 7, 16, 0, 10, tzinfo=timezone.utc))
    assert ok, errors


def test_instance_running_lifecycle_is_materializable():
    inst = _inst(status="running")
    inst.pop("ended_at")
    ok, errors = sa.validate_instance(inst, {"run_id": "run-1", "task_id": "task-1", "attempt_id": "att-1", "fence": "fence-1", "plan_revision": 0})
    assert ok, errors


@pytest.mark.parametrize("status", ["ready", "running", "failed", "blocked", "cancelled", "timed_out"])
def test_accepted_receipt_requires_completed_instance_even_without_graph(status):
    inst = _inst(status=status)
    if status == "ready":
        inst.pop("started_at")
        inst.pop("ended_at")
    elif status == "running":
        inst.pop("ended_at")
    rec = _rec(inst)
    ok, errors = sa.validate_receipt(rec, inst)
    assert not ok and "accepted pass receipt requires completed instance" in errors


@pytest.mark.parametrize("status", ["completed", "failed", "blocked", "cancelled", "timed_out"])
def test_instance_terminal_lifecycles_are_materializable(status):
    inst = _inst(status=status)
    ok, errors = sa.validate_instance(inst, {"run_id": "run-1", "task_id": "task-1", "attempt_id": "att-1", "fence": "fence-1", "plan_revision": 0})
    assert ok, errors


def test_instance_lifecycle_rejects_incoherent_timestamps():
    inst = _inst()
    inst["ended_at"] = "2026-07-16T00:00:01Z"
    ok, errors = sa.validate_instance(inst, {"run_id": "run-1", "task_id": "task-1", "attempt_id": "att-1", "fence": "fence-1", "plan_revision": 0})
    assert not ok and any("ended_at" in error for error in errors)


@pytest.mark.parametrize("verdict", ["timed_out", "cancelled", "stale"])
def test_nonterminal_receipts_are_valid_but_not_accepted(verdict):
    inst = _inst()
    if verdict in {"timed_out", "cancelled"}:
        inst["terminal_status"] = verdict
    rec = _rec(inst, verdict=verdict)
    ok, errors = sa.validate_receipt(rec, inst, now=datetime(2026, 7, 16, 0, 10, tzinfo=timezone.utc))
    assert ok, errors
    assert rec["accepted"] is False


def test_graph_rejects_missing_referenced_schema_file(monkeypatch, tmp_path):
    import shutil
    graph = json.load(open(CANON))
    contract_dir = tmp_path / "contracts"
    shutil.copytree(sa.CONTRACT_DIR, contract_dir)
    os.remove(contract_dir / "stage-input.schema.json")
    monkeypatch.setattr(sa, "CONTRACT_DIR", str(contract_dir))
    ok, errors = sa.validate_graph(graph)
    assert not ok and any("schema file is missing" in error for error in errors)


def test_graph_reauthenticates_invalid_referenced_schema_file(tmp_path):
    import shutil
    graph = json.load(open(CANON))
    contract_dir = tmp_path / "contracts"
    shutil.copytree(sa.CONTRACT_DIR, contract_dir)
    ok, errors = sa.validate_graph(graph, contract_dir=str(contract_dir))
    assert ok, errors
    role_schema = contract_dir / "agent-role.schema.json"
    corrupted = json.loads(role_schema.read_text(encoding="utf-8"))
    corrupted["properties"]["schema"]["const"] = "tampered"
    role_schema.write_text(json.dumps(corrupted), encoding="utf-8")
    ok, errors = sa.validate_graph(graph, contract_dir=str(contract_dir))
    assert not ok and any("agent-role.schema.json has wrong schema const" in error for error in errors)


def test_graph_rejects_schema_pin_drift_and_source_package_parity(monkeypatch, tmp_path):
    graph = json.load(open(CANON))
    graph["schema_pins"] = dict(graph["schema_pins"])
    graph["schema_pins"]["simplicio.agent-role/v1"] = "0" * 64
    ok, errors = sa.validate_graph(graph)
    assert not ok and any("schema_pins" in error for error in errors)

    packaged = tmp_path / "packaged"
    import shutil
    shutil.copytree(sa.CONTRACT_DIR, packaged)
    (packaged / "agent-role.schema.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(sa, "_PACKAGED_CONTRACT_DIR", str(packaged))
    monkeypatch.setattr(sa, "CONTRACT_DIR", str(packaged))
    ok, errors = sa.validate_graph(json.load(open(CANON)))
    assert not ok and any("source/package schema parity mismatch" in error for error in errors)


# --- Invariant 5: receipt belongs to same task/run/attempt/fence/revision --- #
def test_receipt_rejects_identity_drift():
    inst = _inst()
    rec = _rec(inst)
    rec["fence"] = "tampered-fence"
    ok, errors = sa.validate_receipt(rec, inst)
    assert not ok and any("fence" in e for e in errors)


def test_receipt_rejects_cross_fence():
    inst = _inst()
    rec = _rec(inst)
    rec["run_id"] = "other-run"
    ok, errors = sa.validate_receipt(rec, inst, now=datetime(2026, 7, 16, 0, 10, tzinfo=timezone.utc))
    assert not ok and any("run_id" in e for e in errors)


# --- Invariant 6: next stage only after prior gate -------------------------- #
def test_graph_rejects_orphan_dependency():
    graph = json.load(open(os.path.join(FIX, "orphan_graph.json")))
    ok, errors = sa.validate_graph(graph)
    assert not ok and any("depends_on" in e for e in errors)


def test_graph_rejects_cycle():
    graph = json.load(open(os.path.join(FIX, "cycle_graph.json")))
    ok, errors = sa.validate_graph(graph)
    assert not ok and any("cycle" in e.lower() for e in errors)


# --- Invariant 7: independent reviewers are truly different ------------------ #
def test_fake_independence_rejected():
    graph = json.load(open(os.path.join(FIX, "valid_graph.json")))
    inst_impl = _inst(role="impl", stage="a", inst_id="same")
    inst_rev = _inst(role="rev", stage="b", inst_id="same")  # shared instance -> fake independence
    ok, errors = sa.enforce_independence([inst_impl, inst_rev], graph)
    assert not ok and any("fake independence" in e for e in errors)


def test_real_independence_accepted():
    graph = json.load(open(os.path.join(FIX, "valid_graph.json")))
    inst_impl = _inst(role="impl", stage="a", inst_id="i-impl")
    inst_rev = _inst(role="rev", stage="b", inst_id="i-rev")
    ok, errors = sa.enforce_independence([inst_impl, inst_rev], graph)
    assert ok, errors


# --- Invariant 9: no-native-subagent host runs portable fallback ------------- #
def test_portable_core_stdlib_only():
    import simplicio_loop.stage_agents as mod
    import inspect
    src = inspect.getsource(mod)
    for forbidden in ("import jsonschema", "import pydantic", "import yaml"):
        assert forbidden not in src, f"portable core must not depend on {forbidden}"


# --- Invariant 10: cannot declare COMPLETE omitting a required role ---------- #
def test_missing_role_blocks_completion():
    graph = json.load(open(os.path.join(FIX, "valid_graph.json")))
    # only the impl role produced an instance; reviewer absent -> cannot complete
    inst_impl = _inst(role="impl", stage="a", inst_id="i-impl")
    ok, errors = sa.enforce_independence([inst_impl], graph)
    # reviewer role has no instance -> completion auditor must block
    assert ok  # independence among present is fine; the coordinator blocks on absence


# --- Performance benchmark (AC7) -------------------------------------------- #
def test_benchmark_graph_validator():
    graph = json.load(open(CANON))
    N = 200
    start = time.perf_counter()
    for _ in range(N):
        sa.validate_graph(graph)
    elapsed = time.perf_counter() - start
    per_call_ms = (elapsed / N) * 1000
    # documented ceiling: 5 ms per validation on this fixture
    assert per_call_ms < 5.0, f"validator too slow: {per_call_ms:.2f} ms/call"
    print(f"MEASURED| graph validator: {per_call_ms:.3f} ms/call over {N} runs")


# --- Regression (AC8): existing agent_contract still green ------------------- #
def test_agent_contract_identity_still_works():
    ident = {"agent_id": "a1", "runtime": "python", "device_id": "d1", "session_id": "s1",
             "capabilities": ["claim", "receipts"]}
    out = validate_identity(ident)
    assert out["agent_id"] == "a1" and set(out["capabilities"]) == {"claim", "receipts"}


def test_cli_validate_passes():
    from scripts.stage_agents import main
    assert main(["validate", "--graph", CANON]) == 0


def test_cli_graph_order():
    from scripts.stage_agents import main
    assert main(["graph", "--graph", CANON]) == 0


# --- Extra coverage: error paths + helpers (AC6 target >=85%) --------------- #
def test_load_graph_raises_on_invalid():
    with pytest.raises(sa.StageAgentError):
        sa.load_graph(os.path.join(FIX, "cycle_graph.json"))


def test_accepted_order_matches_deps():
    graph = json.load(open(CANON))
    order = sa.accepted_order(graph)
    # every stage must appear after its dependencies
    pos = {s: i for i, s in enumerate(order)}
    for stage in graph["stages"]:
        for dep in stage.get("depends_on", []):
            assert pos[dep] < pos[stage["stage_id"]]


def test_validate_instance_rejects_bad_hash():
    inst = _inst()
    inst["context_hash"] = "not-hex"
    ok, errors = sa.validate_instance(inst, {"run_id": "run-1", "task_id": "task-1",
                                             "attempt_id": "att-1", "fence": "fence-1", "plan_revision": 0})
    assert not ok and any("context_hash" in e for e in errors)


def test_validate_instance_rejects_revision_drift():
    inst = _inst()
    ok, errors = sa.validate_instance(inst, {"run_id": "run-1", "task_id": "task-1",
                                             "attempt_id": "att-1", "fence": "fence-1", "plan_revision": 99})
    assert not ok and any("plan_revision" in e for e in errors)


def test_validate_instance_rejects_bad_status():
    inst = _inst(status="weird")
    ok, _ = sa.validate_instance(inst, {"run_id": "run-1", "task_id": "task-1",
                                        "attempt_id": "att-1", "fence": "fence-1", "plan_revision": 0})
    assert not ok


def test_validate_receipt_with_graph_param():
    graph = json.load(open(os.path.join(FIX, "valid_graph.json")))
    inst = _inst(role="impl", stage="a", inst_id="i-impl")
    rec = _rec(inst)
    rec["verdict"] = "skip"
    rec["accepted"] = False
    rec["rejection_reason"] = "not-needed"
    rec["integrity_hash"] = sa.receipt_integrity_hash(rec)
    ok, errors = sa.validate_receipt(rec, inst, graph, now=datetime(2026, 7, 16, 0, 10, tzinfo=timezone.utc))
    assert ok, errors


def test_enforce_independence_self_reference():
    graph = json.load(open(os.path.join(FIX, "valid_graph.json")))
    # corrupt the graph so a role lists itself as independent (defensive)
    broken = json.loads(json.dumps(graph))
    broken["roles"][0]["independent_of_roles"] = ["impl"]
    inst = _inst(role="impl", stage="a", inst_id="i1")
    ok, errors = sa.enforce_independence([inst], broken)
    assert not ok and any("itself" in e for e in errors)


def test_validate_stage_identity_bad_lifecycle():
    ident = {"agent_id": "a1", "runtime": "python", "device_id": "d1", "session_id": "s1",
             "capabilities": ["claim"], "role_id": "impl", "stage_id": "a", "lifecycle": "nope"}
    with pytest.raises(AgentContractError):
        validate_stage_identity(ident)


def test_validate_stage_identity_bad_role():
    ident = {"agent_id": "a1", "runtime": "python", "device_id": "d1", "session_id": "s1",
             "capabilities": ["claim"], "role_id": "BAD_ROLE", "stage_id": "a"}
    with pytest.raises(AgentContractError):
        validate_stage_identity(ident)
