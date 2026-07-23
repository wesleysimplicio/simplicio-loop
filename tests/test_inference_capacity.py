from simplicio_loop.inference_capacity import (
    SCHEMA,
    CapacitySnapshot,
    CapacitySnapshotError,
    choose_affinity,
)


def _payload(**extra):
    value = {
        "schema": SCHEMA,
        "backend": "llama",
        "model": "small",
        "generation": "g1",
        "available_slots": 1,
        "max_slots": 2,
        "affinity_hint": "opaque:g1",
    }
    value.update(extra)
    return value


def test_absent_or_unknown_runtime_snapshot_keeps_legacy_path():
    assert CapacitySnapshot.from_payload(None) is None
    assert CapacitySnapshot.from_payload({"schema": "other/v1"}) is None


def test_snapshot_rejects_invalid_capacity_and_raw_slot_data():
    try:
        CapacitySnapshot.from_payload(_payload(available_slots=3))
    except CapacitySnapshotError as exc:
        assert "available_slots" in str(exc)
    else:
        raise AssertionError("invalid capacity was accepted")
    try:
        CapacitySnapshot.from_payload(_payload(affinity_hint="slot_id:123"))
    except CapacitySnapshotError as exc:
        assert "raw slot" in str(exc)
    else:
        raise AssertionError("raw slot identifier was accepted")


def test_locality_is_deterministic_but_deadline_and_age_override_it():
    local = CapacitySnapshot.from_payload(_payload(affinity_hint="opaque:g1"))
    other = CapacitySnapshot.from_payload(_payload(generation="g2", affinity_hint="opaque:g2"))
    task = {"backend": "llama", "model": "small", "affinity_hint": "opaque:g1", "priority": 1}
    assert choose_affinity(task, [other, local]) == local
    assert choose_affinity({**task, "deadline_override": True}, [local]) is None
    assert choose_affinity({**task, "queue_age": 301}, [local]) is None
