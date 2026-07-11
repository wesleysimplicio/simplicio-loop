import pytest

from simplicio_loop.phase_events import (
    PhaseEventError, build_phase_event, phase_to_board_state, reconcile_events,
)


def event(sequence=1, **kwargs):
    return build_phase_event(
        run_id="run-1", work_item_id="wi-1", actor="codex@host-a", cause="operator",
        sequence=sequence, event_id="e-%d" % sequence, from_phase=kwargs.pop("from_phase", None),
        to_phase=kwargs.pop("to_phase", "intake" if sequence == 1 else "mapping"),
        attempt_id="attempt-1", **kwargs,
    )


def test_event_contains_identity_and_derived_board_state():
    value = event()
    assert value["schema"] == "simplicio.loop-event/v1"
    assert value["attempt_id"] == "attempt-1"
    assert value["board_state"] == "queued"


def test_invalid_transition_fails_closed():
    with pytest.raises(PhaseEventError, match="invalid transition"):
        event(from_phase="intake", to_phase="done")


def test_board_state_cannot_be_forged():
    value = event()
    value["board_state"] = "completed"
    with pytest.raises(PhaseEventError, match="derived"):
        from simplicio_loop.phase_events import validate_phase_event
        validate_phase_event(value)


def test_reconcile_deduplicates_and_requires_contiguous_sequence():
    first = event()
    second = event(2, from_phase="intake", to_phase="mapping")
    assert [item["event_id"] for item in reconcile_events([second, first, first])] == ["e-1", "e-2"]
    with pytest.raises(PhaseEventError, match="sequence gap"):
        reconcile_events([second])


def test_reconcile_rejects_conflicting_duplicate():
    first = event()
    changed = dict(first, cause="tampered")
    with pytest.raises(PhaseEventError, match="conflicting duplicate"):
        reconcile_events([first, changed])


def test_terminal_board_mapping_is_runtime_derived():
    assert phase_to_board_state("done") == "completed"


@pytest.mark.parametrize(
    "path",
    [
        ["intake", "mapping", "planning", "executing", "validating", "watching", "delivering", "done"],
        ["intake", "mapping", "planning", "executing", "validating", "executing", "validating", "watching", "delivering", "partial"],
        ["intake", "blocked"],
        ["intake", "mapping", "planning", "cancelled"],
        ["intake", "mapping", "planning", "awaiting_decision", "planning", "executing", "validating", "watching", "delivering", "done"],
        ["intake", "mapping", "planning", "executing", "blocked"],
    ],
)
def test_golden_streams_cover_success_retry_block_cancel_handoff_and_resume(path):
    events = []
    previous = None
    for index, phase in enumerate(path, 1):
        events.append(event(index, from_phase=previous, to_phase=phase))
        previous = phase
    replayed = reconcile_events(list(reversed(events)))
    assert [item["to_phase"] for item in replayed] == path
