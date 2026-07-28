"""Bound operators: core always required; Runtime adaptive when operational."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

_spec = importlib.util.spec_from_file_location("hooks.loop_stop", REPO_ROOT / "hooks" / "loop_stop.py")
loop_stop = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(loop_stop)


def _marker(tmp_path, monkeypatch):
    marker_dir = tmp_path / ".claude" / "skills" / "simplicio-loop"
    marker_dir.mkdir(parents=True)
    (marker_dir / "SKILL.md").write_text("stub", encoding="utf-8")
    monkeypatch.setattr(loop_stop, "SIMPLICIO_LOOP_SKILL_MARKER", str(marker_dir / "SKILL.md"))


def test_core_bound_operators_constant():
    assert loop_stop.BOUND_OPERATORS == ("simplicio-mapper", "simplicio-dev-cli")


def test_missing_bound_operators_flags_required_mapper(monkeypatch, tmp_path):
    _marker(tmp_path, monkeypatch)
    monkeypatch.setattr(loop_stop, "_binary_operational", lambda binary, args=("--version",): binary != "simplicio-mapper")
    monkeypatch.setattr(loop_stop, "_action_operator_operational", lambda: True)
    monkeypatch.setattr(loop_stop.shutil, "which", lambda b: None if b == "simplicio-mapper" else "/usr/bin/" + b)
    monkeypatch.delenv("SIMPLICIO_LOOP_STRICT", raising=False)
    monkeypatch.setenv("SIMPLICIO_LOOP_REQUIRE_RUNTIME", "off")
    assert loop_stop.missing_bound_operators() == ["simplicio-mapper"]


def test_missing_bound_operators_empty_when_core_present_runtime_absent(monkeypatch, tmp_path):
    _marker(tmp_path, monkeypatch)
    monkeypatch.setattr(
        loop_stop,
        "_binary_operational",
        lambda binary, args=("--version",): binary in {"simplicio-mapper"},
    )
    monkeypatch.setattr(loop_stop, "_action_operator_operational", lambda: True)
    monkeypatch.setattr(loop_stop.shutil, "which", lambda b: None if b == "simplicio" else "/usr/bin/" + b)
    monkeypatch.setenv("SIMPLICIO_LOOP_REQUIRE_RUNTIME", "auto")
    monkeypatch.delenv("SIMPLICIO_LOOP_STRICT", raising=False)
    assert loop_stop.missing_bound_operators() == []


def test_runtime_required_when_operational_auto(monkeypatch, tmp_path):
    _marker(tmp_path, monkeypatch)

    def operational(binary, args=("--version",)):
        return binary in {"simplicio-mapper", "simplicio"}

    monkeypatch.setattr(loop_stop, "_binary_operational", operational)
    monkeypatch.setattr(loop_stop, "_action_operator_operational", lambda: True)
    monkeypatch.setattr(loop_stop.shutil, "which", lambda b: "/usr/bin/" + b)
    monkeypatch.setenv("SIMPLICIO_LOOP_REQUIRE_RUNTIME", "auto")
    required = loop_stop.required_bound_operators()
    assert "simplicio" in required
    # Still present → not missing
    assert loop_stop.missing_bound_operators() == []


def test_runtime_missing_blocks_when_required(monkeypatch, tmp_path):
    _marker(tmp_path, monkeypatch)
    monkeypatch.setattr(
        loop_stop,
        "_binary_operational",
        lambda binary, args=("--version",): binary == "simplicio-mapper",
    )
    monkeypatch.setattr(loop_stop, "_action_operator_operational", lambda: True)
    monkeypatch.setattr(loop_stop.shutil, "which", lambda b: None if b == "simplicio" else "/usr/bin/" + b)
    monkeypatch.setenv("SIMPLICIO_LOOP_REQUIRE_RUNTIME", "required")
    assert "simplicio" in loop_stop.missing_bound_operators()


def test_strict_requires_operational_fast(monkeypatch, tmp_path):
    _marker(tmp_path, monkeypatch)

    def operational(binary, args=("--version",)):
        return binary in {"simplicio-mapper", "simplicio-fast"}

    monkeypatch.setattr(loop_stop, "_binary_operational", operational)
    monkeypatch.setattr(loop_stop, "_action_operator_operational", lambda: True)
    monkeypatch.setattr(loop_stop.shutil, "which", lambda b: "/usr/bin/" + b)
    monkeypatch.setenv("SIMPLICIO_LOOP_STRICT", "1")
    monkeypatch.setenv("SIMPLICIO_LOOP_REQUIRE_RUNTIME", "off")
    assert "simplicio-fast" in loop_stop.required_bound_operators()
