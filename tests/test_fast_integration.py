from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from simplicio_loop.fast_integration import (
    FAST_CHANGESET_SCHEMA,
    FastConfig,
    FastLoopIntegration,
    FastStaleChangeset,
    validate_changeset,
)


class FakeFast:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.calls: list[list[str]] = []

    def __call__(self, command, **kwargs):
        command = list(command)
        self.calls.append(command)
        args = command[1:]
        if args == ["--version"]:
            return subprocess.CompletedProcess(command, 0, "simplicio-fast 2.0.2\n", "")
        if args == ["doctor", "--json"]:
            return subprocess.CompletedProcess(command, 0, json.dumps({"integrated_ready": True}), "")
        if command[0] == "git":
            return subprocess.CompletedProcess(command, 0, "abc123\n", "")
        if args and args[0] == "ingest":
            output = Path(args[args.index("--output") + 1])
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"snapshot")
            return subprocess.CompletedProcess(command, 0, json.dumps({"schema": "simplicio.fast.ingest/v2", "generation": "g1", "metrics": {}}), "")
        if args and args[0] == "understand":
            return subprocess.CompletedProcess(command, 0, json.dumps({"schema": "simplicio.fast.understanding/v2", "context": [{"file": "app.py", "content": "x"}]}), "")
        if args and args[0] == "plan":
            return subprocess.CompletedProcess(command, 0, json.dumps({"schema": "simplicio.fast.plandag/v2", "nodes": []}), "")
        if args and args[0] == "refresh":
            return subprocess.CompletedProcess(command, 0, json.dumps({"schema": "simplicio.fast.ingest/v2", "generation": "g2", "metrics": {}}), "")
        if args and args[0] == "apply":
            return subprocess.CompletedProcess(command, 0, json.dumps({"schema": "simplicio.fast.apply-receipt/v2", "outcome": "applied"}), "")
        raise AssertionError(command)


def test_prepare_ingests_once_and_pins_receipts(tmp_path: Path) -> None:
    fake = FakeFast(tmp_path)
    config = FastConfig(command=("fast",), snapshot=".fast/project.sfast", state=".fast/state.json")
    integration = FastLoopIntegration(tmp_path, config=config, runner=fake)
    first = integration.prepare("change app")
    second = integration.prepare("change app")
    assert first["status"] == "READY"
    assert first["generation"] == "g1"
    assert first["context_hash"].startswith("sha256:")
    assert first["plan"]["loop_receipt"]["plan_hash"].startswith("sha256:")
    assert second["generation"] == first["generation"]
    assert sum(call[1] == "ingest" for call in fake.calls) == 1
    assert [call[1] for call in fake.calls].count("understand") == 4
    assert [call[1] for call in fake.calls].count("plan") == 2


def test_stale_candidate_and_loser_are_fail_closed(tmp_path: Path) -> None:
    candidate = {
        "schema": FAST_CHANGESET_SCHEMA,
        "changes": [{"path": "app.py", "expected_sha256": "a" * 64, "replacements": [{"start_line": 1, "end_line": 1, "content": "x"}]}],
        "generation": "old",
        "context_hash": "ctx",
    }
    with pytest.raises(FastStaleChangeset):
        validate_changeset(candidate, generation="new", context_hash="ctx")
    integration = FastLoopIntegration(tmp_path, config=FastConfig(mode="standalone"))
    receipt = integration.apply(candidate, winner=False)
    assert receipt["status"] == "SKIPPED"
    assert receipt["applied"] is False


def test_fallback_is_visible_and_configurable(tmp_path: Path) -> None:
    integration = FastLoopIntegration(tmp_path, config=FastConfig(mode="standalone"))
    receipt = integration.prepare("change app")
    assert receipt["status"] == "FALLBACK"
    assert receipt["ingest"]["fallback"] is True
    assert receipt["ingest"]["fallback_mode"] == "mapper-dev-cli"
    required = FastLoopIntegration(tmp_path, config=FastConfig(mode="required"), runner=lambda *a, **k: (_ for _ in ()).throw(OSError("missing")))
    with pytest.raises(Exception):
        required.probe()


def test_real_fast_prepare_on_small_repository(tmp_path: Path) -> None:
    command = (sys.executable, "-m", "simplicio_fast.cli")
    try:
        version = subprocess.run([*command, "--version"], capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        pytest.skip("simplicio-fast is unavailable")
    if version.returncode != 0:
        pytest.skip("simplicio-fast is unavailable")
    (tmp_path / "app.py").write_text("class App:\n    pass\n", encoding="utf-8")
    integration = FastLoopIntegration(tmp_path, config=FastConfig(command=command, timeout_seconds=60))
    result = integration.prepare("change App")
    assert result["status"] in {"READY", "FALLBACK"}
    if result["status"] == "READY":
        assert result["plan"]["schema"] == "simplicio.fast.plandag/v2"
        assert result["generation"]

def test_apply_runtime_gate_and_incremental_refresh(tmp_path: Path) -> None:
    fake = FakeFast(tmp_path)
    runtime_calls: list[dict[str, object]] = []

    def runtime_apply(operation):
        runtime_calls.append(dict(operation))
        return {"status": "APPLIED", "receipt": "runtime-1"}

    integration = FastLoopIntegration(
        tmp_path, config=FastConfig(command=("fast",), snapshot=".fast/project.sfast", state=".fast/state.json"),
        runtime_apply=runtime_apply,
        runner=fake,
    )
    prepared = integration.prepare("change app")
    candidate = {
        "schema": FAST_CHANGESET_SCHEMA,
        "changes": [{"path": "app.py", "expected_sha256": "a" * 64, "replacements": []}],
        "generation": prepared["generation"],
        "context_hash": prepared["context_hash"],
    }
    applied = integration.apply(candidate)
    assert applied["status"] == "READY"
    assert applied["applied"] is True
    assert applied["runtime"]["status"] == "APPLIED"
    assert runtime_calls[0]["generation"] == prepared["generation"]
    refreshed = integration.refresh()
    assert refreshed["status"] == "MEASURED"
    assert refreshed["no_full_remap"] is True
    assert refreshed["generation"] == "g2"