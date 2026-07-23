from types import SimpleNamespace

import pytest

from simplicio_loop.runtime_binary import (
    RuntimeBinary,
    RuntimeBinaryError,
    probe_version,
    resolve_simplicio_binary,
    runtime_preflight,
    verify_mcp_capabilities,
)


def test_explicit_binary_has_provenance_and_version_probe(tmp_path):
    binary = tmp_path / "simplicio"
    binary.write_text("binary", encoding="utf-8")
    resolved = resolve_simplicio_binary(explicit=str(binary))
    assert resolved.source == "explicit"
    versioned = probe_version(resolved, runner=lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="simplicio 1.2.3\n"))
    assert versioned.version == "simplicio 1.2.3"


def test_environment_binary_is_used_before_path(tmp_path):
    binary = tmp_path / "runtime"
    binary.write_text("binary", encoding="utf-8")
    resolved = resolve_simplicio_binary(environ={"SIMPLICIO_RUNTIME_BIN": str(binary)})
    assert resolved.source == "environment"


def test_missing_binary_and_bad_probe_fail_closed(tmp_path):
    with pytest.raises(RuntimeBinaryError):
        resolve_simplicio_binary(explicit=str(tmp_path / "missing"))
    binary = RuntimeBinary(str(tmp_path), "test")
    with pytest.raises(RuntimeBinaryError, match="version"):
        probe_version(binary, runner=lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout=""))


def test_mcp_capability_receipt_is_not_effect_authority():
    result = verify_mcp_capabilities({"protocolVersion": "2024-11-05", "capabilities": {"tools": [{"name": "simplicio_exec"}]}}, required_tools=("simplicio_exec",))
    assert result["status"] == "READY"
    assert result["effects_authorized"] is False
    with pytest.raises(RuntimeBinaryError, match="missing required"):
        verify_mcp_capabilities({"protocolVersion": "2024-11-05", "capabilities": {"tools": []}}, required_tools=("simplicio_exec",))


def test_preflight_binds_binary_and_capabilities():
    receipt = runtime_preflight(binary=RuntimeBinary("/tmp/simplicio", "explicit", "1"), initialize_result={"protocolVersion": "2024-11-05", "capabilities": {"tools": ["simplicio_exec"]}}, required_tools=("simplicio_exec",))
    assert receipt["binary_source"] == "explicit"
    assert receipt["effects_authorized"] is False
