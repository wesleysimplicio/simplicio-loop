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


def test_forced_bind_metadata_matches_install_policy(tmp_path: Path):
    payload = runtime_matrix.build_matrix(["claude", "aider"], tmp_path, runner=_runner_ok)
    rows = {row["runtime"]: row for row in payload["runtimes"]}
    assert rows["claude"]["forced_native_bind"] is True
    assert rows["aider"]["forced_native_bind"] is ("aider" in install_lib.FORCED_BIND_RUNTIMES)
