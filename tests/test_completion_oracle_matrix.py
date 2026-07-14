import json
from pathlib import Path

from scripts.completion_oracle_matrix import ADAPTERS, evaluate_adapter, evaluate_matrix


def _seed(tmp_path: Path, *, ready: bool) -> tuple[Path, Path]:
    loop = tmp_path / "loop"
    run = tmp_path / "run"
    loop.mkdir()
    run.mkdir()
    (loop / "scratchpad.md").write_text("---\ncompletion_promise: \"DONE\"\n---\ngoal\n", encoding="utf-8")
    (loop / "anchor.json").write_text(json.dumps({"criteria": [{"id": "AC1", "status": "done"}]}), encoding="utf-8")
    (loop / "watcher_challenge.json").write_text(json.dumps({"challenge": "c1", "written_at": "2026-07-10T00:00:00Z"}), encoding="utf-8")
    (loop / "watcher_state.json").write_text(json.dumps({"match": True, "status": "MEASURED", "challenge": "c1", "checked_at": "2026-07-10T00:00:01Z"}), encoding="utf-8")
    for name, payload in {
        "manifest.json": {"delivery_target": "verified"},
        "task-contract.json": {"schema": "simplicio.task-contract-collection/v1"},
        "mapper-context.json": {"handoff": {}},
        "operator-receipt.json": {"schema": "simplicio.operator-receipt/v0"},
        "evidence-receipt.json": {"schema": "simplicio.evidence-receipt/v1", "status": "VERIFIED" if ready else "UNVERIFIED", "criteria": [{"id": "AC1", "verification_state": "verified" if ready else "unverified"}], "summary": {"criteria_total": 1, "criteria_verified": 1 if ready else 0, "scenario_total": 1, "scenario_verified": 1 if ready else 0, "rule_total": 1, "rule_verified": 1 if ready else 0}},
        "delivery-receipt.json": {"schema": "simplicio.delivery-receipt/v1", "target": "verified", "current_state": "verified" if ready else "implemented", "ready": ready, "source_kind": "local", "source_payload": {"evidence_receipt": "evidence-receipt.json", "criteria_verified": 1 if ready else 0}},
        "quality-matrix.json": {
            "schema": "simplicio.quality-matrix/v1",
            "coverage_threshold": 85,
            "requirements": {
                name: {"status": "pass", "proof_ref": f"tests/{name}"}
                for name in ("implementation", "unit", "integration", "system", "regression", "benchmark")
            },
            "coverage": {"measured": 91.2},
        },
    }.items():
        (run / name).write_text(json.dumps(payload), encoding="utf-8")
    return loop, run


def test_matrix_is_identical_for_all_supported_adapters_when_blocked(tmp_path):
    loop, run = _seed(tmp_path, ready=False)
    payload = evaluate_matrix(str(loop), str(run), "<promise>DONE</promise>")
    assert payload["parity"] is True
    assert [row["adapter"] for row in payload["adapters"]] == list(ADAPTERS)
    assert payload["signature"][1] == "DELIVERY_PENDING"
    assert payload["signature"][2] == "evidence_not_verified"


def test_matrix_is_identical_for_all_supported_adapters_when_complete(tmp_path):
    loop, run = _seed(tmp_path, ready=True)
    payload = evaluate_matrix(str(loop), str(run), "<promise>DONE</promise>")
    assert payload["parity"] is True
    assert payload["signature"][:3] == [True, "COMPLETE", "completion_verified"] or tuple(payload["signature"][:3]) == (True, "COMPLETE", "completion_verified")


def test_hermes_and_simplicio_agent_share_a_signature(tmp_path):
    """N-1/N parity fixture (#262): the legacy `hermes` adapter and the canonical
    `simplicio_agent` adapter must evaluate to the same oracle signature."""
    loop, run = _seed(tmp_path, ready=True)
    assert "hermes" in ADAPTERS and "simplicio_agent" in ADAPTERS
    hermes_row = evaluate_adapter("hermes", str(loop), str(run), "<promise>DONE</promise>")
    agent_row = evaluate_adapter("simplicio_agent", str(loop), str(run), "<promise>DONE</promise>")
    assert (hermes_row["ready"], hermes_row["verdict"], hermes_row["reason_code"], hermes_row["tag"]) == (
        agent_row["ready"], agent_row["verdict"], agent_row["reason_code"], agent_row["tag"])


def test_unknown_adapter_fails_closed(tmp_path):
    loop, run = _seed(tmp_path, ready=False)
    try:
        evaluate_adapter("unknown", str(loop), str(run))
    except ValueError as exc:
        assert "unsupported adapter" in str(exc)
    else:
        raise AssertionError("unknown adapters must fail closed")
