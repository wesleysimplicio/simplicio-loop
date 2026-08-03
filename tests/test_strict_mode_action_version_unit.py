"""Preflight operate-binary probes must report real versions, not usage banners."""
from __future__ import annotations

from types import SimpleNamespace

from simplicio_loop import strict_mode


def test_sanitize_version_banner_drops_usage():
    assert strict_mode._sanitize_version_banner("usage: simplicio-py [-h]") == ""
    assert strict_mode._sanitize_version_banner("simplicio-dev-cli 0.18.6") == "simplicio-dev-cli 0.18.6"


def test_action_operator_status_prefers_version_flag(monkeypatch):
    calls: list[tuple[str, ...]] = []

    def fake_run(command, **_kwargs):
        calls.append(tuple(command))
        binary = command[0]
        flag = command[1] if len(command) > 1 else ""
        if flag == "--version":
            return SimpleNamespace(returncode=0, stdout=f"{binary} 0.18.6\n", stderr="")
        if flag == "--help":
            return SimpleNamespace(returncode=0, stdout="usage: simplicio-py [-h]\n", stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="no")

    monkeypatch.setattr(strict_mode.shutil, "which", lambda name: name)
    monkeypatch.setattr(strict_mode.subprocess, "run", fake_run)

    status = strict_mode.action_operator_status({})
    assert status["operational"] is True
    assert status["resolved_as"] == "simplicio-dev-cli"
    assert status["version"] == "simplicio-dev-cli 0.18.6"
    assert any(call[1:] == ("--version",) for call in calls)
    assert not any("usage:" in status["version"].lower() for _ in [0])


def test_preflight_payload_reports_version_not_usage(monkeypatch, tmp_path):
    def which(name: str):
        return f"/bin/{name}"

    def fake_run(command, **_kwargs):
        binary = command[0]
        flag = command[1] if len(command) > 1 else ""
        if "simplicio-dev-cli" in binary and flag == "--version":
            return SimpleNamespace(returncode=0, stdout="simplicio-dev-cli 0.18.6\n", stderr="")
        if "simplicio-py" in binary and flag == "--version":
            return SimpleNamespace(returncode=0, stdout="simplicio-py 0.18.6\n", stderr="")
        if "simplicio-mapper" in binary:
            return SimpleNamespace(returncode=0, stdout="0.26.11\n", stderr="")
        if "simplicio-fast" in binary:
            return SimpleNamespace(returncode=0, stdout="simplicio-fast 2.0.23\n", stderr="")
        if binary.endswith("simplicio") or binary == "simplicio" or binary.endswith("/simplicio"):
            return SimpleNamespace(returncode=0, stdout="Simplicio Runtime 3.5.7\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="ok\n", stderr="")

    monkeypatch.setattr(strict_mode.shutil, "which", which)
    monkeypatch.setattr(strict_mode.subprocess, "run", fake_run)

    receipt = strict_mode.preflight_payload(str(tmp_path), strict=True)
    ops = {item["name"]: item for item in receipt["operators"]}
    assert ops["simplicio-dev-cli"]["present"] is True
    assert "0.18.6" in ops["simplicio-dev-cli"]["version"]
    assert not ops["simplicio-dev-cli"]["version"].lower().startswith("usage:")
    assert ops["simplicio-py"]["present"] is True
    assert "0.18.6" in ops["simplicio-py"]["version"]
