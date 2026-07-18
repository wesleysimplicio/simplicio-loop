"""Runtime availability is reported, but is not a prerequisite for core loop work."""
from __future__ import annotations

import contextlib
import io
import json
from types import SimpleNamespace

from simplicio_loop import cli


def test_preflight_continues_without_optional_runtime(monkeypatch, tmp_path):
    def fake_run(command, **_kwargs):
        if command[0] == "simplicio":
            return SimpleNamespace(returncode=127, stdout="", stderr="runtime missing")
        return SimpleNamespace(returncode=0, stdout="ready\n", stderr="")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    findings = []
    monkeypatch.setattr(
        "simplicio_loop.finding_router.route_finding",
        lambda **finding: findings.append(finding),
    )

    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        assert cli.preflight(str(tmp_path), as_json=True) == 0

    receipt = json.loads(output.getvalue())
    assert receipt["all_present"] is True
    assert receipt["runtime_available"] is False
    assert receipt["degraded_features"] == ["runtime-integration"]
    assert findings == []
