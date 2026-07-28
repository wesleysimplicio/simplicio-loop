from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from simplicio_loop.hookwall_rollout import HookwallRollout


def test_shadow_canary_enforced_and_rollback_never_bypass_gate(tmp_path):
    rollout = HookwallRollout(tmp_path / "rollout.json")
    previous = None
    for mode in ("shadow", "canary", "enforced", "rollback"):
        receipt = rollout.transition(mode, actor="test", reason=f"exercise-{mode}")
        assert receipt["previous_mode"] == previous
        assert receipt["mutation_requires_hookwall"] is True
        assert rollout.read() == receipt
        previous = mode


def test_rollout_tamper_and_unknown_mode_fail_closed(tmp_path):
    path = tmp_path / "rollout.json"
    rollout = HookwallRollout(path)
    rollout.transition("enforced", actor="test", reason="ready")
    value = json.loads(path.read_text())
    value["mutation_requires_hookwall"] = False
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="tampered"):
        rollout.read()
    with pytest.raises(ValueError, match="invalid"):
        rollout.transition("disabled", actor="test", reason="unsafe")


def test_unchecked_effect_has_exactly_one_call_site_inside_hookwall():
    root = Path(__file__).parents[1]
    tree = ast.parse((root / "simplicio_loop" / "runner.py").read_text())
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_execute_operator_effect_unchecked"
    ]
    assert len(calls) == 1
    owner = calls[0]
    while owner and not isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)):
        owner = parents.get(owner)
    assert owner and owner.name == "_execute_operator_effect"


def test_cross_repo_inventory_is_explicit_and_never_claims_false_completion():
    root = Path(__file__).parents[1]
    inventory = json.loads(
        (root / "contracts" / "hookwall" / "v1" / "entrypoints.json").read_text()
    )
    loop = [item for item in inventory["entrypoints"] if item["repository"] == "simplicio-loop"]
    external = [item for item in inventory["entrypoints"] if item["repository"] != "simplicio-loop"]
    assert loop and all(item["verified"] for item in loop)
    assert {item["reason_code"] for item in external} == {
        "CROSS_REPO_RUNTIME_OPEN", "CROSS_REPO_DEV_CLI_OPEN"
    }
    assert all(item["verified"] is False and item["dependency"].startswith("https://") for item in external)
