"""Runtime PATH preference: never treat pip simplicio-py alias as Runtime."""
from __future__ import annotations

from types import SimpleNamespace

from simplicio_loop import strict_mode


def test_looks_like_native_runtime_accepts_runtime_banner():
    assert strict_mode._looks_like_native_runtime(
        "Simplicio Runtime 3.5.7",
        r"C:\Users\x\.local\bin\simplicio.exe",
    )
    assert not strict_mode._looks_like_native_runtime(
        "simplicio-py 0.18.6",
        r"C:\Python\Scripts\simplicio.exe",
    )


def test_runtime_status_prefers_env_bin_over_alias(monkeypatch, tmp_path):
    real = tmp_path / "real-runtime.exe"
    alias = tmp_path / "alias-simplicio.exe"
    real.write_text("x", encoding="utf-8")
    alias.write_text("y", encoding="utf-8")

    def fake_run(command, **_kwargs):
        path = str(command[0])
        if path == str(real):
            return SimpleNamespace(returncode=0, stdout="Simplicio Runtime 3.5.7\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="simplicio-py 0.18.6\n", stderr="")

    monkeypatch.setattr(strict_mode.subprocess, "run", fake_run)
    monkeypatch.setattr(strict_mode, "_which_all", lambda _b: [str(alias), str(real)])
    monkeypatch.setattr(strict_mode.shutil, "which", lambda _b: str(alias))

    status = strict_mode.runtime_status({"SIMPLICIO_RUNTIME_BIN": str(real)})
    assert status["operational"] is True
    assert "3.5.7" in status["version"]
    assert "Runtime" in status["version"] or "runtime" in status["version"].lower()
    assert status["path"] == str(real)


def test_runtime_status_skips_py_alias_on_path(monkeypatch, tmp_path):
    real = tmp_path / "simplicio-runtime-bin.exe"
    alias = tmp_path / "pip-simplicio.exe"
    real.write_text("x", encoding="utf-8")
    alias.write_text("y", encoding="utf-8")

    def fake_run(command, **_kwargs):
        path = str(command[0])
        if path == str(real):
            return SimpleNamespace(returncode=0, stdout="Simplicio Runtime 3.5.7\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="simplicio-py 0.18.6\n", stderr="")

    monkeypatch.setattr(strict_mode.subprocess, "run", fake_run)
    # Alias appears first on PATH (the Windows failure mode).
    monkeypatch.setattr(strict_mode, "_which_all", lambda _b: [str(alias), str(real)])
    monkeypatch.setattr(strict_mode.shutil, "which", lambda _b: str(alias))
    # No home hints.
    monkeypatch.setattr(strict_mode.Path, "home", classmethod(lambda cls: tmp_path / "nohome"))

    status = strict_mode.runtime_status({})
    assert status["operational"] is True
    assert status["path"] == str(real)
    assert "simplicio-py" not in status["version"]


def test_runtime_status_reports_clear_error_when_only_alias(monkeypatch, tmp_path):
    alias = tmp_path / "only-alias.exe"
    alias.write_text("y", encoding="utf-8")

    monkeypatch.setattr(
        strict_mode.subprocess,
        "run",
        lambda command, **_k: SimpleNamespace(
            returncode=0, stdout="simplicio-py 0.18.6\n", stderr=""
        ),
    )
    monkeypatch.setattr(strict_mode, "_which_all", lambda _b: [str(alias)])
    monkeypatch.setattr(strict_mode.shutil, "which", lambda _b: str(alias))
    monkeypatch.setattr(strict_mode.Path, "home", classmethod(lambda cls: tmp_path / "nohome"))

    status = strict_mode.runtime_status({})
    assert status["operational"] is False
    assert status["present"] is True
    assert "SIMPLICIO_RUNTIME_BIN" in status["error"]
