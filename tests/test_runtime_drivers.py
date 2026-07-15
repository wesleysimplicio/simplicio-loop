import subprocess

import pytest

from simplicio_loop import runtime_drivers as rd
from simplicio_loop.receipt_verifier import ReceiptStatus, verify_receipt
from simplicio_loop.runtime_execution_receipt import RUNTIME_EXECUTION_RECEIPT_SCHEMA, UNAVAILABLE


def _cp(argv, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(argv, returncode, stdout, stderr)


def test_codex_driver_missing_binary_reports_honest_failure(monkeypatch):
    driver = rd.CodexRuntimeDriver()
    monkeypatch.setattr(driver, "is_installed", lambda: False)
    result = driver.execute("do the task")
    assert result.ok is False
    assert result.stop_reason == "error"
    assert result.resolved_model is None
    assert "not found on PATH" in result.error
    assert result.usage["tokens"] == UNAVAILABLE


def test_codex_driver_parses_real_jsonl_event_stream(monkeypatch):
    driver = rd.CodexRuntimeDriver()
    monkeypatch.setattr(driver, "is_installed", lambda: True)
    stdout = "\n".join([
        '{"type":"thread.started","thread_id":"abc"}',
        '{"type":"item.completed","item":{"id":"item_2","type":"agent_message","text":"PING_OK"}}',
        '{"type":"turn.completed","usage":{"input_tokens":100,"cached_input_tokens":10,"output_tokens":9}}',
    ])
    monkeypatch.setattr(rd, "_run_cli", lambda argv, cwd, timeout: _cp(argv, 0, stdout, ""))
    result = driver.execute("reply with PING_OK")
    assert result.ok is True
    assert result.stop_reason == "completed"
    assert result.stdout == "PING_OK"
    assert result.usage["tokens"] == 119
    assert result.resolved_model["model_id"] == UNAVAILABLE  # codex JSON stream never echoes model
    assert result.resolved_model["verified"] is False


def test_codex_driver_nonzero_exit_is_a_real_reported_failure(monkeypatch):
    driver = rd.CodexRuntimeDriver()
    monkeypatch.setattr(driver, "is_installed", lambda: True)
    monkeypatch.setattr(rd, "_run_cli", lambda argv, cwd, timeout: _cp(argv, 1, "", "boom"))
    result = driver.execute("do the task")
    assert result.ok is False
    assert result.exit_status == 1
    assert result.stop_reason == "error"
    assert "boom" in result.error


def test_codex_driver_timeout_is_reported_not_swallowed(monkeypatch):
    driver = rd.CodexRuntimeDriver()
    monkeypatch.setattr(driver, "is_installed", lambda: True)

    def _raise(argv, cwd, timeout):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout, output="partial", stderr="")

    monkeypatch.setattr(rd, "_run_cli", _raise)
    result = driver.execute("do the task", timeout=5)
    assert result.ok is False
    assert result.stop_reason == "timeout"
    assert "timed out" in result.error


def test_claude_driver_parses_real_error_envelope_honestly(monkeypatch):
    driver = rd.ClaudeRuntimeDriver()
    monkeypatch.setattr(driver, "is_installed", lambda: True)
    stdout = (
        '{"type":"result","subtype":"success","is_error":true,"api_error_status":403,'
        '"result":"Your organization has disabled Claude subscription access",'
        '"usage":{"input_tokens":0,"output_tokens":0}}'
    )
    monkeypatch.setattr(rd, "_run_cli", lambda argv, cwd, timeout: _cp(argv, 1, stdout, ""))
    result = driver.execute("do the task")
    assert result.ok is False
    assert result.stop_reason == "error"
    assert "disabled Claude subscription access" in result.error
    assert result.resolved_model["model_id"] == UNAVAILABLE


def test_claude_driver_parses_real_success_envelope(monkeypatch):
    driver = rd.ClaudeRuntimeDriver()
    monkeypatch.setattr(driver, "is_installed", lambda: True)
    stdout = (
        '{"type":"result","subtype":"success","is_error":false,'
        '"result":"PING_OK","total_cost_usd":0.01,'
        '"usage":{"input_tokens":50,"output_tokens":9}}'
    )
    monkeypatch.setattr(rd, "_run_cli", lambda argv, cwd, timeout: _cp(argv, 0, stdout, ""))
    result = driver.execute("reply with PING_OK")
    assert result.ok is True
    assert result.stdout == "PING_OK"
    assert result.usage["tokens"] == 59
    assert result.usage["cost_usd"] == 0.01


def test_execute_rejects_blank_prompt():
    driver = rd.CodexRuntimeDriver()
    with pytest.raises(rd.RuntimeDriverError):
        driver.execute("   ")


def test_driver_for_runtime_returns_none_for_unwired_runtime():
    assert rd.driver_for_runtime("cursor") is None
    assert isinstance(rd.driver_for_runtime("codex"), rd.CodexRuntimeDriver)
    assert isinstance(rd.driver_for_runtime("claude"), rd.ClaudeRuntimeDriver)


def test_probe_cli_hook_never_fabricates_and_reports_measured(monkeypatch):
    driver = rd.CodexRuntimeDriver()
    monkeypatch.setattr(rd, "driver_for_runtime", lambda runtime: driver if runtime == "codex" else None)
    monkeypatch.setattr(driver, "is_installed", lambda: False)
    result = rd.probe_cli_hook({"runtime": "codex"})
    assert result["status"] == "MEASURED"
    assert result["available"] is False

    unwired = rd.probe_cli_hook({"runtime": "cursor"})
    assert unwired["status"] == "UNVERIFIED"
    assert unwired["available"] is False


def test_build_receipt_from_real_result_verifies_end_to_end(monkeypatch):
    driver = rd.CodexRuntimeDriver()
    monkeypatch.setattr(driver, "is_installed", lambda: True)
    monkeypatch.setattr(driver, "version", lambda: "codex-cli 0.144.1")
    stdout = "\n".join([
        '{"type":"item.completed","item":{"id":"item_2","type":"agent_message","text":"PING_OK"}}',
        '{"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":9}}',
    ])
    monkeypatch.setattr(rd, "_run_cli", lambda argv, cwd, timeout: _cp(argv, 0, stdout, ""))
    result = driver.execute("reply with PING_OK")
    receipt = driver.build_receipt(
        route_id="route-abc",
        requested={"runtime": "codex", "provider": "openai", "model_id": "gpt-5.6", "verified": True},
        session={"worker_id": "w1", "device_id": "d1", "attempt_id": "a1", "lease_id": "l1", "fence_token": "f1"},
        result=result,
        tree={"base_sha": "abc", "head_sha": "def", "changed_paths": []},
    )
    assert receipt["schema"] == "simplicio.runtime-execution-receipt/v1"
    assert receipt["driver"]["identity_verified"] is True
    assert receipt["exit_status"] == 0
    verdict = verify_receipt(receipt, schema=RUNTIME_EXECUTION_RECEIPT_SCHEMA)
    assert verdict.status == ReceiptStatus.VERIFIED
