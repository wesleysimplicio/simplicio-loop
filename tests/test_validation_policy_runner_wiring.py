import json
from pathlib import Path

from simplicio_loop import runner as runner_mod


def _plan(*, fresh=True):
    return {
        "freshness": {"verified": fresh},
        "mapper_pack_hash": "mapper-1" if fresh else "",
        "context_pack_hash": "mapper-1" if fresh else "",
        "mapper_targets": ["src/app.py"] if fresh else [],
        "steps": [
            {
                "steps": [
                    {
                        "plan": {
                            "test_paths": ["tests/test_app.py"],
                            "test_commands": ["pytest tests/test_app.py"],
                        }
                    }
                ]
            }
        ],
    }


def test_execute_policy_receipt_is_persisted_from_plan_context(tmp_path):
    run_dir = tmp_path / ".simplicio" / "loop-runs" / "run-1"
    run_dir.mkdir(parents=True)
    state = {"phase": "executing", "history": []}
    payload = runner_mod._persist_validation_policy_receipt(
        run_dir,
        "run-1",
        1,
        task={"change_kind": "code"},
        contract={"collection_hash": "contract-1"},
        plan=_plan(),
        state=state,
    )

    receipt_path = Path(payload["receipt_path"])
    assert receipt_path == run_dir / "validation-policy" / "task-1.json"
    assert receipt_path.exists()
    assert payload["schema"] == "simplicio.validation-receipt/v1"
    assert payload["runner_schema"] == "simplicio.loop.validation-policy-receipt/v1"
    assert payload["selected_tests"] == [
        "command:pytest tests/test_app.py",
        "test:tests/test_app.py",
    ]
    assert payload["reason_codes"] == []
    assert payload["local_llm_started"] is False
    assert state["validation_policy"]["receipt"] == str(receipt_path)
    persisted = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    assert persisted["validation_policy"]["receipt"] == str(receipt_path)


def test_stale_plan_is_recorded_as_conservative_without_blocking(tmp_path):
    run_dir = tmp_path / ".simplicio" / "loop-runs" / "run-2"
    run_dir.mkdir(parents=True)
    payload = runner_mod._persist_validation_policy_receipt(
        run_dir,
        "run-2",
        1,
        task={"change_kind": "code"},
        contract={"collection_hash": "contract-2"},
        plan=_plan(fresh=False),
        state={"phase": "executing", "history": []},
    )

    assert payload["profile"] == "converge"
    assert payload["final_gate_required"] is True
    assert {"MAP_STALE", "IMPACT_UNKNOWN", "CONSERVATIVE_ESCALATION"}.issubset(
        payload["reason_codes"]
    )
    assert "CACHE_DISABLED" in payload["reason_codes"]
