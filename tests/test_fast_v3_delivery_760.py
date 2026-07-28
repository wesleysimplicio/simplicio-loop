import pytest
import sys

from simplicio_loop.fast_v3_delivery import Budget, DeliveryRun, FastV3Runner, State
from simplicio_loop.fast_v3_cli import CommandVerifier, JsonCommandAdapter


def run(max_attempts=3, max_context_bytes=100):
    return DeliveryRun("fix issue", ["tests pass"], "o/r", "abc", "g1",
                       Budget(max_attempts, 100, max_context_bytes))


def test_transitions_are_evidence_gated_and_hash_chained():
    item = run()
    first = item.transition(State.PREFLIGHTED, evidence={"fast": "rust"})
    second = item.transition(State.PINNED, evidence={"base": "abc"})
    assert second["previous_receipt"]
    assert first["acceptance_hash"] == second["acceptance_hash"]
    with pytest.raises(ValueError, match="illegal transition"):
        item.transition(State.SEALED, evidence={"fake": True})


def test_progressive_context_deduplicates_and_expansion_needs_reason():
    item = run()
    manifest = item.add_context(0, [("symbol:a", b"")])
    assert manifest["added_handles"] == ["symbol:a"]
    assert item.add_context(0, [("symbol:a", b"")])["reused_handles"] == ["symbol:a"]
    with pytest.raises(ValueError, match="reason_code"):
        item.add_context(1, [("symbol:b", b"def b(): pass")])
    expanded = item.add_context(1, [("symbol:b", b"def b(): pass")], reason_code="missing_definition")
    assert expanded["tier"] == "T1"


def test_handle_drift_and_context_budget_fail_closed():
    item = run(max_context_bytes=2)
    item.add_context(0, [("a", b"x")])
    with pytest.raises(ValueError, match="drift"):
        item.add_context(0, [("a", b"y")])
    with pytest.raises(RuntimeError, match="exhausted"):
        item.add_context(0, [("b", b"xx")])
    assert item.state == State.HELD


def test_retry_is_delta_only_and_stall_changes_strategy():
    item = run()
    one = item.record_attempt(error="E", diff_digest="d", plan_digest="p",
                              changed_handles=["a"], test="pytest", observed_tokens=None)
    two = item.record_attempt(error="E", diff_digest="d", plan_digest="p",
                              changed_handles=["a"], test="pytest", observed_tokens=5)
    assert one["action"] == "retry_delta"
    assert one["context_content"] is None
    assert two["action"] == "expand_context_or_switch_strategy"
    assert item.spent_tokens == 5


def test_budget_exhaustion_holds_with_unknown_provider_metrics():
    item = run(max_attempts=1)
    delta = item.record_attempt(error="E", diff_digest="d", plan_digest="p",
                                changed_handles=[], test="pytest")
    assert delta["action"] == "held"
    assert item.spent_tokens is None
    assert item.state == State.HELD


def test_candidate_policy_defaults_to_one_and_is_adaptive():
    item = run()
    assert item.candidate_count(uncertainty=0.1, risk=0.2, stalled=False)["candidates"] == 1
    assert item.candidate_count(uncertainty=0.6, risk=0.2, stalled=False)["candidates"] == 2
    assert item.candidate_count(uncertainty=0.1, risk=0.2, stalled=True)["candidates"] == 3


def test_verify_only_can_skip_decision_and_seal_requires_all_gates():
    item = run()
    item.transition(State.PREFLIGHTED, evidence={"operators": "ok"})
    item.transition(State.PINNED, evidence={"generation": "g1"})
    item.transition(State.ORIENTED_T0, evidence={"handles": []})
    item.transition(State.VERIFY_FOCUSED, evidence={"llm_calls": 0})
    item.transition(State.VERIFY_FULL, evidence={"focused": True})
    item.transition(State.READY_TO_PROMOTE, evidence={"full": True})
    item.transition(State.PROMOTED, evidence={"winner": "mechanical"})
    with pytest.raises(ValueError, match="all ACs"):
        item.seal(ac_coverage={"tests pass": False}, gates={"tests": True})
    receipt = item.seal(ac_coverage={"tests pass": True}, gates={"tests": True})
    assert receipt["to"] == "SEALED"


def test_runner_verify_only_standalone_seals_without_llm():
    item = run()
    runner = FastV3Runner(
        orient=lambda task, budget: {"status": "READY", "provider": "fast-test",
                                     "handles": [{"handle": "symbol:a", "content": "def a(): pass"}]},
        verify=lambda scope: {"ok": True, "scope": scope})
    result = runner.execute(item, verify_only=True)
    assert result["sealed"] is True
    assert result["spent_tokens"] is None


def test_runner_full_fails_closed_without_runtime_authorization():
    item = run()
    runner = FastV3Runner(
        orient=lambda task, budget: {"status": "READY", "provider": "fast-test", "handles": []},
        verify=lambda scope: {"ok": True, "scope": scope})
    result = runner.execute(item, verify_only=True, full=True)
    assert result["state"] == "HELD"
    assert result["receipts"][-1]["evidence"]["reason"] == "runtime_authorization_missing"


def test_runner_dry_run_precedes_effect():
    calls = []
    item = run()
    runner = FastV3Runner(
        orient=lambda task, budget: {"status": "READY", "provider": "fast-test", "handles": []},
        decide=lambda context: {"change": "x"},
        apply=lambda plan: calls.append(plan["dry_run"]) or {"ok": True, "dry_run": plan["dry_run"]},
        verify=lambda scope: {"ok": True, "scope": scope})
    assert runner.execute(item)["sealed"] is True
    assert calls == [True, False]


def test_missing_verifier_never_seals():
    with pytest.raises(ValueError, match="both focused and full"):
        CommandVerifier([], [sys.executable, "-c", "pass"], cwd=".", timeout=2)


def test_red_real_verifier_holds_and_never_seals(tmp_path):
    item = run()
    verifier = CommandVerifier(
        [sys.executable, "-c", "raise SystemExit(7)"],
        [sys.executable, "-c", "pass"], cwd=tmp_path, timeout=2)
    runner = FastV3Runner(
        orient=lambda task, budget: {"status": "READY", "provider": "fixture", "handles": []},
        verify=verifier)
    result = runner.execute(item, verify_only=True)
    assert result["sealed"] is False
    assert result["state"] == "HELD"
    assert result["receipts"][-1]["evidence"]["reason"] == "focused_verification_failed"


def test_real_verifiers_pass_with_hashed_receipts(tmp_path):
    item = run()
    verifier = CommandVerifier(
        [sys.executable, "-c", "print('focused-pass')"],
        [sys.executable, "-c", "print('full-pass')"], cwd=tmp_path, timeout=2)
    runner = FastV3Runner(
        orient=lambda task, budget: {"status": "READY", "provider": "fixture", "handles": []},
        verify=verifier)
    result = runner.execute(item, verify_only=True)
    assert result["sealed"] is True
    verify_receipts = [x for x in result["receipts"]
                       if x["to"] in {"VERIFY_FULL", "READY_TO_PROMOTE"}]
    assert all(x["evidence"]["exit_code"] == 0 for x in verify_receipts)
    assert all(len(x["evidence"]["evidence_hash"]) == 64 for x in verify_receipts)


def test_cli_source_has_no_hardcoded_success_verifier():
    from pathlib import Path
    source = (Path(__file__).parents[1] / "simplicio_loop" / "fast_v3_cli.py").read_text()
    assert 'verify=lambda' not in source
    assert '"ok": True, "scope": scope' not in source


def test_normal_command_adapters_execute_complete_order(tmp_path):
    log = tmp_path / "events"
    script = tmp_path / "adapter.py"
    script.write_text(
        "import json,sys,pathlib\n"
        "p=pathlib.Path(sys.argv[1]); phase=sys.argv[2]\n"
        "payload=json.load(sys.stdin)\n"
        "mode=sys.argv[-1] if sys.argv[-1].startswith('--') else ''\n"
        "with p.open('a') as f: f.write(phase+mode+'\\n')\n"
        "print(json.dumps({'ok':True,'phase':phase,'payload_seen':bool(payload)}))\n")
    verify = tmp_path / "verify.py"
    verify.write_text(
        "import sys,pathlib\np=pathlib.Path(sys.argv[1]);"
        "open(p,'a').write(sys.argv[2]+'\\n')\n")
    item = run()
    runner = FastV3Runner(
        orient=lambda task, budget: (log.open("a").write("orient\n") and
                                     {"status": "READY", "provider": "fast", "handles": []}),
        decide=JsonCommandAdapter([sys.executable, str(script), str(log), "decide"],
                                  cwd=tmp_path, timeout=2, phase="decide"),
        apply=JsonCommandAdapter([sys.executable, str(script), str(log), "apply"],
                                 cwd=tmp_path, timeout=2, phase="apply"),
        verify=CommandVerifier(
            [sys.executable, str(verify), str(log), "focused"],
            [sys.executable, str(verify), str(log), "full"], cwd=tmp_path, timeout=2))
    assert runner.execute(item)["sealed"] is True
    assert log.read_text().splitlines() == [
        "orient", "decide", "apply--dry-run", "apply--apply", "focused", "full"]


def test_red_dry_run_gate_never_calls_apply_or_seals(tmp_path):
    log = tmp_path / "events"
    script = tmp_path / "adapter.py"
    script.write_text(
        "import json,sys,pathlib\njson.load(sys.stdin)\n"
        "mode=sys.argv[-1] if sys.argv[-1].startswith('--') else ''\n"
        "with pathlib.Path(sys.argv[1]).open('a') as f: f.write(mode+'\\n')\n"
        "print(json.dumps({'ok': mode != '--dry-run'}))\n")
    item = run()
    runner = FastV3Runner(
        orient=lambda task, budget: {"status": "READY", "provider": "fast", "handles": []},
        decide=lambda context: {"ok": True, "change": "x"},
        apply=JsonCommandAdapter([sys.executable, str(script), str(log)],
                                 cwd=tmp_path, timeout=2, phase="apply"),
        verify=lambda scope: pytest.fail("verification must not run after red dry-run"))
    result = runner.execute(item)
    assert result["sealed"] is False
    assert result["state"] == "HELD"
    assert log.read_text().splitlines() == ["--dry-run"]
