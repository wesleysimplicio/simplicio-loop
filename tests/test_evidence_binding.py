import json
import subprocess

from simplicio_loop.evidence_binding import (DERIVED_RECEIPTS, bind_receipt,
    capture_evidence_binding, invalidate_derived_evidence, validate_receipt_binding)


def _repo(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True)
    (tmp_path / "app.py").write_text("x=1\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "base"], check=True)
    return tmp_path


def _binding(repo, **kw):
    values = dict(run_id="r", task_id="t", attempt_id="a", policy={"coverage": 90},
                  config={}, toolchain={"python": "3.12"}, task_contract={"ac": ["safe"]})
    values.update(kw)
    return capture_evidence_binding(repo, **values)


def test_one_byte_mutation_blocks_even_without_tombstone(tmp_path):
    repo = _repo(tmp_path)
    receipt = bind_receipt({"ready": True}, _binding(repo))
    (repo / "app.py").write_text("x=2\n")  # simulates crash before explicit invalidation
    verdict = validate_receipt_binding(receipt, _binding(repo))
    assert not verdict["ok"] and verdict["reason_code"] == "evidence_binding_stale"
    assert "diff_hash" in verdict["changed_fields"]


def test_fresh_and_untracked_content_are_measured(tmp_path):
    repo = _repo(tmp_path)
    binding = _binding(repo)
    assert validate_receipt_binding(bind_receipt({}, binding), binding)["ok"]
    (repo / "new.txt").write_text("untracked byte")
    assert _binding(repo)["diff_hash"] != binding["diff_hash"]


def test_tampered_binding_is_invalid(tmp_path):
    binding = _binding(_repo(tmp_path))
    receipt = bind_receipt({}, binding)
    receipt["evidence_binding"]["tree_hash"] = "forged"
    assert validate_receipt_binding(receipt, binding)["reason_code"] == "evidence_binding_invalid"


def test_policy_and_attempt_drift_are_fail_closed(tmp_path):
    repo = _repo(tmp_path)
    receipt = bind_receipt({}, _binding(repo))
    assert "policy_hash" in validate_receipt_binding(receipt, _binding(repo, policy={"coverage": 91}))["changed_fields"]
    assert validate_receipt_binding(receipt, _binding(repo, attempt_id="b"))["reason_code"] == "evidence_attempt_mismatch"


def test_legacy_receipt_requires_explicit_reexecution(tmp_path):
    verdict = validate_receipt_binding({"ready": True}, _binding(_repo(tmp_path)))
    assert verdict == {"ok": False, "stale": True, "reason_code": "evidence_binding_missing",
                       "changed_fields": ["evidence_binding"], "migration": "reexecute_receipt"}


def test_invalidation_is_atomic_auditable_and_idempotent(tmp_path):
    first = invalidate_derived_evidence(tmp_path, "mutation", ["diff_hash"])
    second = invalidate_derived_evidence(tmp_path, "mutation", ["diff_hash"])
    assert first == second and tuple(first["receipts"]) == tuple(sorted(DERIVED_RECEIPTS))
    assert len((tmp_path / "evidence-invalidation.jsonl").read_text().splitlines()) == 1
    assert json.loads((tmp_path / "evidence-invalidation.jsonl").read_text())["event_id"] == first["event_id"]


def test_corrupt_history_is_retained_but_cannot_forge_idempotency(tmp_path):
    ledger = tmp_path / "evidence-invalidation.jsonl"
    ledger.write_text("not-json\n")
    event = invalidate_derived_evidence(tmp_path, "retry", ["attempt_id"])
    lines = ledger.read_text().splitlines()
    assert lines[0] == "not-json" and json.loads(lines[1])["event_id"] == event["event_id"]


def test_selective_freshness_requires_dependency_proof(tmp_path):
    all_event = invalidate_derived_evidence(tmp_path / "all", "policy", ["policy_hash"], affected_receipts=["quality-matrix.json"])
    selective = invalidate_derived_evidence(tmp_path / "some", "policy", ["policy_hash"],
        dependency_proof={"schema": "simplicio.evidence-dependencies/v1", "quality-matrix.json": ["policy_hash"]},
        affected_receipts=["quality-matrix.json"])
    assert set(all_event["receipts"]) == set(DERIVED_RECEIPTS)
    assert selective["receipts"] == ["quality-matrix.json"]
