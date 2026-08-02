from __future__ import annotations

import json

import pytest

from simplicio_loop import runner as runner_mod
from simplicio_loop.cli_impl import main
from simplicio_loop.stack_lock import (
    STACK_LOCK_SCHEMA,
    StackLock,
    StackLockError,
    observe_components,
    load_stack_lock,
    observe_component,
    validate_stack_lock,
    write_stack_lock,
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


def test_persistence_is_atomic_and_immutable(tmp_path):
    mapper = _component(tmp_path)
    lock = StackLock.create([mapper], "standalone", run_id="run-1")
    path = tmp_path / "run" / "stack-lock.json"

    assert write_stack_lock(lock, path) == path
    assert load_stack_lock(path).lock_hash == lock.lock_hash
    assert write_stack_lock(lock, path) == path

    changed = StackLock.create([_component(tmp_path, content=b"changed")], "standalone", run_id="run-1")
    with pytest.raises(StackLockError, match="different hash"):
        write_stack_lock(changed, path)


def test_persisted_component_tampering_is_rejected(tmp_path):
    mapper = _component(tmp_path)
    lock = StackLock.create([mapper], "standalone")
    path = tmp_path / "stack-lock.json"
    write_stack_lock(lock, path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["components"][0]["version"] = "9.9.9"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(StackLockError, match="lock_hash_mismatch"):
        load_stack_lock(path)


def test_stack_cli_lock_and_verify_fail_closed_on_artifact_drift(tmp_path, capsys):
    binary = tmp_path / "simplicio-mapper"
    binary.write_bytes(b"mapper")
    components = tmp_path / "components.json"
    components.write_text(json.dumps({
        "components": [{
            "name": "simplicio-mapper",
            "version": "1.0.0",
            "executable": str(binary),
            "build_sha": "build",
            "capabilities": ["map"],
        }]
    }), encoding="utf-8")
    lock_path = tmp_path / "stack-lock.json"

    assert main([
        "stack", "lock", "--components", str(components), "--route", "standalone",
        "--run-id", "run-1", "--output", str(lock_path),
    ]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "LOCKED"

    assert main([
        "stack", "verify", "--lock", str(lock_path), "--components", str(components),
    ]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "VERIFIED"

    binary.write_bytes(b"mapper-upgraded")
    assert main([
        "stack", "verify", "--lock", str(lock_path), "--components", str(components),
    ]) == 2
    blocked = json.loads(capsys.readouterr().out)
    assert blocked["status"] == "BLOCKED"
    assert "stack drift" in blocked["error"]


def test_observation_payload_and_stack_doctor_report_routes(tmp_path, monkeypatch, capsys):
    loop_binary = tmp_path / "simplicio-loop"
    mapper_binary = tmp_path / "simplicio-mapper"
    loop_binary.write_bytes(b"loop")
    mapper_binary.write_bytes(b"mapper")
    observations = {
        "components": [
            {"name": "simplicio-loop", "version": "1.0.0", "executable": str(loop_binary)},
            {"name": "simplicio-mapper", "version": "1.0.0", "executable": str(mapper_binary)},
        ]
    }
    assert len(observe_components(observations)) == 2
    path = tmp_path / "components.json"
    path.write_text(json.dumps(observations), encoding="utf-8")
    monkeypatch.setenv("SIMPLICIO_STACK_COMPONENTS_FILE", str(path))

    assert main(["doctor", "stack", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["schema"] == "simplicio.stack-doctor/v1"
    assert result["status"] == "READY"
    assert result["routes"]["standalone"]["available"] is True
    assert result["routes"]["runtime-backed"]["available"] is False
    assert result["routes"]["runtime-backed"]["missing"] == ["simplicio-runtime"]


def test_runner_freezes_and_verifies_stack_lock_at_boundaries(tmp_path, monkeypatch):
    binary = tmp_path / "simplicio-mapper"
    binary.write_bytes(b"mapper")
    def discover():
        return (observe_component("simplicio-mapper", "1.0.0", binary, capabilities=("map",)),)

    monkeypatch.setattr(runner_mod, "discover_installed_components", discover)
    monkeypatch.setenv("SIMPLICIO_EXECUTION_PROFILE", "standalone")
    run_root = tmp_path / "run"
    run_root.mkdir()

    lock = runner_mod._freeze_stack_lock(run_root, "run-1")
    assert (run_root / "stack-lock.json").exists()
    assert runner_mod._verify_run_stack_lock(run_root).lock_hash == lock.lock_hash

    binary.write_bytes(b"mapper-upgraded")
    with pytest.raises(StackLockError, match="stack drift"):
        runner_mod._verify_run_stack_lock(run_root)
