import json

from simplicio_loop.cli import main


def test_validation_explain_is_deterministic(tmp_path, capsys):
    receipt = tmp_path / "validation.json"
    receipt.write_text(json.dumps({
        "schema": "simplicio.validation-receipt/v1",
        "policy_version": "v1",
        "phase": "converge",
        "profile": "converge",
        "selected_tests": ["test:a", "test:b"],
        "reason_codes": ["PRIOR_FAILURE", "CACHE_DISABLED", "PRIOR_FAILURE"],
        "final_gate_required": True,
        "cache_allowed": False,
        "cache_key": "cache-key",
        "map_fresh": True,
        "impact_known": True,
    }), encoding="utf-8")
    assert main(["validation", "explain", "--receipt", str(receipt)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == "simplicio.validation-explain/v1"
    assert payload["reason_codes"] == ["CACHE_DISABLED", "PRIOR_FAILURE"]
    assert payload["selected_tests"] == ["test:a", "test:b"]
    assert payload["local_llm_started"] is False
