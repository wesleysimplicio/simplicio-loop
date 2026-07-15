import json
import subprocess
from pathlib import Path

import pytest

from scripts import install_lib, runtime_matrix


def _runner_ok(command, **kwargs):
    runtime = command[-1]
    return subprocess.CompletedProcess(command, 0, "PASS  %s        skills+entry+hooks landed\n" % runtime, "")


def test_matrix_is_machine_readable_and_marks_external_launch_unverified(tmp_path: Path):
    payload = runtime_matrix.build_matrix(["claude", "codex", "cursor"], tmp_path, runner=_runner_ok)
    assert payload["schema"] == "simplicio.runtime-matrix/v1"
    assert payload["ready"] is True
    assert payload["external_launch_verified"] is False
    assert {row["runtime"] for row in payload["runtimes"]} == {"claude", "codex", "cursor"}
    json.dumps(payload)


def test_unknown_runtime_fails_closed(tmp_path: Path):
    with pytest.raises(ValueError, match="unknown runtime"):
        runtime_matrix.build_matrix(["not-a-runtime"], tmp_path, runner=_runner_ok)


def test_missing_or_failed_adapter_is_not_inferred_as_pass(tmp_path: Path):
    def failed(command, **kwargs):
        return subprocess.CompletedProcess(command, 1, "FAIL  claude\n", "broken")

    payload = runtime_matrix.build_matrix(["claude"], tmp_path, runner=failed)
    assert payload["ready"] is False
    assert payload["runtimes"][0]["status"] == "FAIL"
    assert payload["runtimes"][0]["output_row"] == "FAIL"


def test_native_runtime_bind_is_optional_for_every_host(tmp_path: Path):
    payload = runtime_matrix.build_matrix(["claude", "aider"], tmp_path, runner=_runner_ok)
    rows = {row["runtime"]: row for row in payload["runtimes"]}
    assert install_lib.FORCED_BIND_RUNTIMES == set()
    assert rows["claude"]["forced_native_bind"] is False
    assert rows["aider"]["forced_native_bind"] is False
    assert "REQUIRED" not in install_lib.entry_block("codex")


def test_attempt_launch_default_off_keeps_prior_behavior(tmp_path: Path):
    # #287: default behavior (no attempt_launch) must stay byte-for-byte identical
    # to the pre-existing hardcoded UNVERIFIED/False contract other callers rely on.
    payload = runtime_matrix.build_matrix(["claude", "codex"], tmp_path, runner=_runner_ok)
    assert payload["external_launch_verified"] is False
    assert payload["external_launch_status"] == "UNVERIFIED"
    assert "external_launch_attempts" not in payload


def test_attempt_launch_reports_real_measured_outcomes_never_fabricated(tmp_path: Path, monkeypatch):
    import simplicio_loop.runtime_drivers as rd

    class _FakeResult:
        def __init__(self, ok, error=""):
            self.ok = ok
            self.exit_status = 0 if ok else 1
            self.stop_reason = "completed" if ok else "error"
            self.duration_seconds = 1.23
            self.stdout = "SIMPLICIO_RUNTIME_MATRIX_PROBE_OK" if ok else ""
            self.error = error

    class _FakeDriver:
        binary = "fake-cli"

        def __init__(self, ok, error=""):
            self._ok = ok
            self._error = error

        def is_installed(self):
            return True

        def execute(self, prompt, timeout=60):
            return _FakeResult(self._ok, self._error)

    def _fake_driver_for_runtime(runtime):
        if runtime == "codex":
            return _FakeDriver(True)
        if runtime == "claude":
            return _FakeDriver(False, "organization policy blocked access")
        return None

    monkeypatch.setattr(rd, "driver_for_runtime", _fake_driver_for_runtime)
    payload = runtime_matrix.build_matrix(["claude", "codex"], tmp_path, runner=_runner_ok, attempt_launch=True)
    attempts = {row["runtime"]: row for row in payload["external_launch_attempts"]}
    assert attempts["codex"]["status"] == "PASS"
    assert attempts["claude"]["status"] == "FAIL"
    assert "organization policy blocked access" in attempts["claude"]["detail"]
    # One real failure among the attempts means the aggregate is honestly not verified.
    assert payload["external_launch_verified"] is False
    assert payload["external_launch_status"] == "MEASURED"


def test_attempt_launch_reports_unverified_for_runtime_with_no_driver(tmp_path: Path):
    result = runtime_matrix.attempt_external_launch("cursor")
    assert result["attempted"] is False
    assert result["status"] == "UNVERIFIED"


def test_attempt_launch_reports_unavailable_for_missing_binary(tmp_path: Path, monkeypatch):
    import simplicio_loop.runtime_drivers as rd

    class _MissingDriver:
        binary = "codex"

        def is_installed(self):
            return False

    monkeypatch.setattr(rd, "driver_for_runtime", lambda runtime: _MissingDriver() if runtime == "codex" else None)
    result = runtime_matrix.attempt_external_launch("codex")
    assert result["attempted"] is True
    assert result["status"] == "UNAVAILABLE"
    assert "not found on PATH" in result["detail"]
