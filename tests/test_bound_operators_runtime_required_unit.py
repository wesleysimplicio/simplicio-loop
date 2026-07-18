"""The native runtime augments the loop but never blocks its core operators."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

_spec = importlib.util.spec_from_file_location("hooks.loop_stop", REPO_ROOT / "hooks" / "loop_stop.py")
loop_stop = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(loop_stop)


def test_simplicio_runtime_is_not_a_bound_operator():
    assert "simplicio" not in loop_stop.BOUND_OPERATORS


def test_missing_bound_operators_does_not_flag_optional_runtime(monkeypatch, tmp_path):
    marker_dir = tmp_path / ".claude" / "skills" / "simplicio-loop"
    marker_dir.mkdir(parents=True)
    (marker_dir / "SKILL.md").write_text("stub", encoding="utf-8")
    monkeypatch.setattr(loop_stop, "SIMPLICIO_LOOP_SKILL_MARKER", str(marker_dir / "SKILL.md"))
    monkeypatch.setattr(loop_stop.shutil, "which", lambda b: None if b == "simplicio" else "/usr/bin/" + b)

    assert loop_stop.missing_bound_operators() == []


def test_missing_bound_operators_still_flags_required_mapper(monkeypatch, tmp_path):
    marker_dir = tmp_path / ".claude" / "skills" / "simplicio-loop"
    marker_dir.mkdir(parents=True)
    (marker_dir / "SKILL.md").write_text("stub", encoding="utf-8")
    monkeypatch.setattr(loop_stop, "SIMPLICIO_LOOP_SKILL_MARKER", str(marker_dir / "SKILL.md"))
    monkeypatch.setattr(
        loop_stop.shutil,
        "which",
        lambda binary: None if binary == "simplicio-mapper" else "/usr/bin/" + binary,
    )

    assert loop_stop.missing_bound_operators() == ["simplicio-mapper"]


def test_missing_bound_operators_empty_when_all_present(monkeypatch, tmp_path):
    marker_dir = tmp_path / ".claude" / "skills" / "simplicio-loop"
    marker_dir.mkdir(parents=True)
    (marker_dir / "SKILL.md").write_text("stub", encoding="utf-8")
    monkeypatch.setattr(loop_stop, "SIMPLICIO_LOOP_SKILL_MARKER", str(marker_dir / "SKILL.md"))
    monkeypatch.setattr(loop_stop.shutil, "which", lambda b: "/usr/bin/" + b)

    assert loop_stop.missing_bound_operators() == []
