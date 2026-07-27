from __future__ import annotations
import pytest
from simplicio_loop.progressive_context import PACKET_SCHEMA, PROGRESSIVE_CONTEXT_SCHEMA, ContextBudgetError, ProgressiveContext, ProgressiveContextError


def _packet(*, generation: str = "SFAST001:generation-a", content: str = "def run():\n    return True\n"):
    return {"schema": PACKET_SCHEMA, "generation": generation, "spans": [{"symbol": "run", "file": "app.py", "start_line": 1, "end_line": 2, "content": content, "tokens": 8}]}


def test_t0_manifest_has_handles_without_source_content():
    manifest = ProgressiveContext("task-760", "SFAST001:generation-a", max_bytes=4096).manifest()
    assert manifest["schema"] == PROGRESSIVE_CONTEXT_SCHEMA
    assert manifest["handles"] == []
    assert "spans" not in manifest
    assert manifest["remaining_bytes"] == 4096


def test_observe_packet_deduplicates_identical_handles_and_measures_bytes():
    context = ProgressiveContext("task-760", "SFAST001:generation-a", max_bytes=4096)
    first, second = context.observe_packet(_packet()), context.observe_packet(_packet())
    assert len(first["new_handles"]) == 1
    assert second["new_handles"] == []
    assert len(second["reused_handles"]) == 1
    assert first["new_bytes"] == second["manifest"]["context_bytes"]
    assert context.context_tokens == 8


def test_stale_generation_and_content_mutation_fail_closed():
    context = ProgressiveContext("task-760", "SFAST001:generation-a", max_bytes=4096)
    context.observe_packet(_packet())
    with pytest.raises(ProgressiveContextError, match="stale context generation"):
        context.observe_packet(_packet(generation="SFAST001:stale"))
    with pytest.raises(ProgressiveContextError, match="changed without a new generation"):
        context.observe_packet(_packet(content="def run():\n    return False\n"))


def test_budget_exhaustion_does_not_mutate_context():
    context = ProgressiveContext("task-760", "SFAST001:generation-a", max_bytes=4)
    with pytest.raises(ContextBudgetError):
        context.observe_packet(_packet())
    assert context.handle_ids == ()


def test_retry_delta_contains_only_handles_and_failure_delta():
    context = ProgressiveContext("task-760", "SFAST001:generation-a", max_bytes=4096)
    observed = context.observe_packet(_packet())
    delta = context.retry_delta(error="focused test failed", diff="app.py:2", evidence=["test-receipt"], affected_handles=observed["new_handles"])
    assert delta["schema"] == "simplicio.loop.failure-delta/v1"
    assert delta["affected_handles"] == observed["new_handles"]
    assert "content" not in delta and "spans" not in delta
    assert delta["context_bytes"] > 0
    assert context.materialize(observed["new_handles"])[0]["content"].startswith("def run")


def test_unknown_handle_and_invalid_packet_fail_closed():
    context = ProgressiveContext("task-760", "SFAST001: generation-a")
    with pytest.raises(ProgressiveContextError, match="unknown affected handles"):
        context.retry_delta(error="x", affected_handles=["fast:unknown"])
    with pytest.raises(ProgressiveContextError, match="unexpected context packet schema"):
        context.observe_packet({"schema": "simplicio.context-packet/v0", "generation": context.generation, "spans": []})
