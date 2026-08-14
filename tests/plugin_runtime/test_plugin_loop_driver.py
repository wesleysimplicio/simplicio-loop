from pathlib import Path

import pytest

from simplicio_loop.plugin_runtime import LoopControlDecision, PluginLoopDriver, PluginLoopError


def test_start_tick_continue_in_standalone(tmp_path: Path):
    driver = PluginLoopDriver(tmp_path, mode="standalone")
    session = driver.start({"goal": "fix roster", "max_iterations": 3})
    assert session["mode"] == "standalone"
    assert session["can_prove_external_effect"] is False
    decision = driver.tick()
    assert isinstance(decision, LoopControlDecision)
    assert decision.action == "continue"
    assert decision.goal_digest == session["goal_digest"]


def test_missing_receipt_is_not_success_when_runtime_bound(tmp_path: Path):
    driver = PluginLoopDriver(tmp_path, mode="runtime-bound")
    driver.start({"goal": "drain issue"})
    observed = driver.observe({"schema": "receipt", "status": ""})
    assert observed["status"] == "ambiguous"
    decision = driver.tick()
    assert decision.action == "refeed"
    assert decision.completion_ready is False


def test_watcher_tamper_rejects_completion(tmp_path: Path):
    driver = PluginLoopDriver(tmp_path, mode="runtime-bound")
    driver.start({"goal": "fix"})
    driver.observe({"status": "measured", "evidence_complete": True})
    driver.set_watcher(fresh=False, tampered=True)
    decision = driver.stop()
    assert decision.action == "refeed"
    assert decision.reason == "watcher_stale_or_tampered"


def test_resume_preserves_iteration_and_goal_digest(tmp_path: Path):
    driver = PluginLoopDriver(tmp_path, mode="standalone")
    started = driver.start({"goal": "resume-me", "max_iterations": 5})
    driver.tick()
    driver.tick()
    snapshot = {
        "goal": "resume-me",
        "goal_digest": started["goal_digest"],
        "iteration": 2,
        "receipts": [],
    }
    resumed = driver.resume(snapshot)
    assert resumed["iteration"] == 2
    assert resumed["goal_digest"] == started["goal_digest"]
    other = PluginLoopDriver(tmp_path, mode="standalone")
    other.start({"goal": "other"})
    with pytest.raises(PluginLoopError, match="goal digest"):
        other.resume(snapshot)


def test_standalone_cannot_prove_external_effect(tmp_path: Path):
    driver = PluginLoopDriver(tmp_path, mode="standalone")
    driver.start({"goal": "local only"})
    driver.observe({"status": "measured", "evidence_complete": True})
    decision = driver.stop()
    assert decision.action == "stop"
    assert decision.completion_ready is True
    assert decision.can_prove_external_effect is False
    assert driver.handoff()["applies_effects"] is False


def test_max_iterations_and_cancel(tmp_path: Path):
    driver = PluginLoopDriver(tmp_path, mode="standalone")
    driver.start({"goal": "cap", "max_iterations": 1})
    driver.tick()
    assert driver.tick().reason == "max_iterations"
    driver.cancel()
    assert driver.tick().action == "pause"
