"""Format repair cannot retry unresolved provider requests."""
import json
import runpy
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("previous", [
    {"status": "request_started"},
    {"status": "failed_or_unknown_no_retry"},
    {"status": "response_received", "response": {"choices": [
        {"finish_reason": "length", "message": {"content": "{}"}}]}},
    {"status": "response_received", "response": {"choices": [
        {"finish_reason": "stop", "message": {"content": '{"files":{}}'}}]}},
])
def test_repair_refuses_unresolved_or_wrong_failure(tmp_path, monkeypatch, previous):
    task = tmp_path / "task.md"
    context = tmp_path / "context.json"
    prior = tmp_path / "prior.json"
    output = tmp_path / "output.json"
    task.write_text("Repair src/example.py")
    context.write_text("{}")
    prior.write_text(json.dumps(previous))
    monkeypatch.setenv("OPENROUTER_API_KEY", "unit-test-not-a-secret")
    monkeypatch.setattr(sys, "argv", ["probe", "--task", str(task), "--context", str(context),
        "--output", str(output), "--repair-response", str(prior)])
    def no_network(*args, **kwargs):
        pytest.fail("invalid repair reached network")
    monkeypatch.setattr("urllib.request.urlopen", no_network)
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(Path(__file__).resolve().parents[1] / "bench/openrouter_worker_probe.py"), run_name="__main__")
    assert exc.value.code == 2
    assert not output.exists()
