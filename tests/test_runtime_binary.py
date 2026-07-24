from types import SimpleNamespace

import pytest

import simplicio_loop.runtime_binary as runtime_binary

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


def test_product_identity_rejects_loop_collision():
    binary = RuntimeBinary("/tmp/simplicio", "path")
    with pytest.raises(RuntimeBinaryError, match="product identity"):
        probe_version(binary, runner=lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout="simplicio-loop 9.0.0\n"))


def test_default_path_collision_falls_back_to_legacy_alias(monkeypatch, tmp_path):
    canonical = tmp_path / "simplicio"
    legacy = tmp_path / "simplicio-runtime"
    canonical.write_text("collision", encoding="utf-8")
    legacy.write_text("runtime", encoding="utf-8")
    paths = {"simplicio": str(canonical), "simplicio-runtime": str(legacy)}

    monkeypatch.setattr(runtime_binary.shutil, "which", lambda name: paths.get(name))

    def run(argv, **kwargs):
        output = "simplicio-loop 9.0.0\n" if argv[0] == str(canonical) else "simplicio 3.5.2\n"
        return SimpleNamespace(returncode=0, stdout=output)

    resolved = runtime_binary.resolve_and_probe_simplicio_binary(runner=run)
    assert resolved.source == "legacy-path"
    assert resolved.compiled_identity == "simplicio"


def test_identity_cache_is_invalidated_when_binary_is_replaced(monkeypatch, tmp_path):
    binary = tmp_path / "simplicio"
    binary.write_text("release-one", encoding="utf-8")
    calls = []

    def run(argv, **kwargs):
        calls.append(argv[0])
        version = "simplicio 1.0.0\n" if len(calls) == 1 else "simplicio 2.0.0\n"
        return SimpleNamespace(returncode=0, stdout=version)

    monkeypatch.setattr(runtime_binary.subprocess, "run", run)
    first = probe_version(RuntimeBinary(str(binary), "explicit"))
    cached = probe_version(RuntimeBinary(str(binary), "explicit"))
    binary.write_text("release-two", encoding="utf-8")
    replaced = probe_version(RuntimeBinary(str(binary), "explicit"))
    assert first.sha256 == cached.sha256
    assert cached.version == "simplicio 1.0.0"
    assert replaced.version == "simplicio 2.0.0"
    assert len(calls) == 2


def test_tools_list_is_authoritative_for_required_capabilities():
    receipt = verify_mcp_capabilities(
        {"protocolVersion": "2024-11-05", "capabilities": {"tools": []}},
        tools_result={"tools": [{"name": "simplicio_exec"}]},
        required_tools=("simplicio_exec",),
    )
    assert receipt["tools"] == ["simplicio_exec"]


def test_explicit_directory_and_missing_path_have_repairable_diagnostics(tmp_path):
    with pytest.raises(RuntimeBinaryError, match="not a file.*Repair"):
        resolve_simplicio_binary(explicit=str(tmp_path))
    with pytest.raises(RuntimeBinaryError, match="not found.*Repair"):
        runtime_binary.resolve_and_probe_simplicio_binary(
            environ={}, candidates=("definitely-missing-runtime",), runner=lambda *a, **k: None
        )


def test_probe_runner_failures_and_empty_identity_fail_closed(tmp_path):
    binary = tmp_path / "simplicio"
    binary.write_text("fixture", encoding="utf-8")
    with pytest.raises(RuntimeBinaryError, match="version probe"):
        probe_version(RuntimeBinary(str(binary), "explicit"), runner=lambda *a, **k: (_ for _ in ()).throw(OSError("offline")))
    with pytest.raises(RuntimeBinaryError, match="version"):
        probe_version(RuntimeBinary(str(binary), "explicit"), runner=lambda *a, **k: SimpleNamespace(returncode=0, stdout=""))


@pytest.mark.parametrize(
    "payload, message",
    [
        (None, "object"),
        ({"protocolVersion": "old", "capabilities": {}}, "protocol"),
        ({"protocolVersion": "2024-11-05"}, "capabilities"),
        ({"protocolVersion": "2024-11-05", "capabilities": {"tools": "bad"}}, "missing required"),
    ],
)
def test_mcp_preflight_rejects_malformed_initialize(payload, message):
    with pytest.raises(RuntimeBinaryError, match=message):
        verify_mcp_capabilities(payload, required_tools=("simplicio_exec",))


def test_server_identity_and_hbi_hbp_compatibility_are_recorded(tmp_path):
    binary = RuntimeBinary(str(tmp_path / "simplicio"), "explicit", "simplicio 3.5.2", "sha256:x", "simplicio")
    result = runtime_binary.runtime_preflight(
        binary=binary,
        initialize_result={
            "protocolVersion": "2024-11-05",
            "serverInfo": {"name": "simplicio", "version": "3.5.2", "hbi": "v1", "hbp": "v1"},
            "capabilities": {"tools": []},
        },
        tools_result={"tools": [{"name": "simplicio_exec"}]},
        required_tools=("simplicio_exec",),
        require_server_identity=True,
    )
    assert result["compatibility"] == {"runtime_version": "PASS", "hbi": "PASS", "hbp": "PASS"}
