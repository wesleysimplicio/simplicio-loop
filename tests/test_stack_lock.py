from __future__ import annotations

import pytest

from simplicio_loop.stack_lock import (
    STACK_LOCK_SCHEMA,
    StackLock,
    StackLockError,
    observe_component,
    validate_stack_lock,
)


def _component(tmp_path, name="simplicio-mapper", content=b"mapper"):
    binary = tmp_path / name
    binary.write_bytes(content)
    return observe_component(name, "1.0.0", binary, build_sha="build", capabilities=("map",))


def test_lock_hash_is_canonical_and_route_is_frozen(tmp_path):
    mapper = _component(tmp_path)
    fast = _component(tmp_path, "simplicio-fast", b"fast")
    runtime = _component(tmp_path, "simplicio-runtime", b"runtime")
    first = StackLock.create([fast, mapper, runtime], "standalone", run_id="run-1")
    second = StackLock.create([runtime, mapper, fast], "standalone", run_id="run-1")
    assert first.lock_hash == second.lock_hash
    assert first.to_dict()["schema"] == STACK_LOCK_SCHEMA
    first.verify_unchanged([mapper, fast, runtime], "standalone")
    with pytest.raises(StackLockError, match="stack drift"):
        first.verify_unchanged([mapper, fast, runtime], "runtime-backed")


def test_runtime_backed_requires_runtime_and_artifact_drift_blocks(tmp_path):
    mapper = _component(tmp_path)
    with pytest.raises(StackLockError, match="requires an available"):
        StackLock.create([mapper], "runtime-backed")
    runtime = _component(tmp_path, "simplicio-runtime", b"runtime")
    lock = StackLock.create([mapper, runtime], "runtime-backed")
    runtime_binary = tmp_path / "simplicio-runtime"
    runtime_binary.write_bytes(b"runtime-upgraded")
    changed = observe_component("simplicio-runtime", "1.0.0", runtime_binary, build_sha="build", capabilities=("map",))
    with pytest.raises(StackLockError, match="stack drift"):
        lock.verify_unchanged([mapper, changed], "runtime-backed")


def test_missing_optional_runtime_is_valid_standalone_and_serialized_lock_is_checked(tmp_path):
    mapper = _component(tmp_path)
    lock = StackLock.create([mapper], "standalone")
    payload = lock.to_dict()
    assert validate_stack_lock(payload) == []
    payload["lock_hash"] = "bad"
    assert "lock_hash_invalid" in validate_stack_lock(payload)
