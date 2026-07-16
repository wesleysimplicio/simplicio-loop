"""simplicio-runtime is now a required bound operator (CLAUDE.md/AGENTS.md), not optional.

Regression test for the doc/code contradiction found by adversarial review of PR #445:
CLAUDE.md/AGENTS.md claimed the native bind BLOCKS the loop when missing, but
hooks/loop_stop.py::BOUND_OPERATORS never actually included it -- so the docs
overstated behavior the running driver didn't implement. This confirms the code now
matches the docs.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

_spec = importlib.util.spec_from_file_location("hooks.loop_stop", REPO_ROOT / "hooks" / "loop_stop.py")
loop_stop = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(loop_stop)


def test_simplicio_runtime_is_a_bound_operator():
    assert "simplicio" in loop_stop.BOUND_OPERATORS


def test_missing_bound_operators_flags_simplicio_when_absent(monkeypatch, tmp_path):
    marker_dir = tmp_path / ".claude" / "skills" / "simplicio-loop"
    marker_dir.mkdir(parents=True)
    (marker_dir / "SKILL.md").write_text("stub", encoding="utf-8")
    monkeypatch.setattr(loop_stop, "SIMPLICIO_LOOP_SKILL_MARKER", str(marker_dir / "SKILL.md"))
    monkeypatch.setattr(loop_stop.shutil, "which", lambda b: None if b == "simplicio" else "/usr/bin/" + b)

    missing = loop_stop.missing_bound_operators()

    assert "simplicio" in missing


def test_missing_bound_operators_empty_when_all_present(monkeypatch, tmp_path):
    marker_dir = tmp_path / ".claude" / "skills" / "simplicio-loop"
    marker_dir.mkdir(parents=True)
    (marker_dir / "SKILL.md").write_text("stub", encoding="utf-8")
    monkeypatch.setattr(loop_stop, "SIMPLICIO_LOOP_SKILL_MARKER", str(marker_dir / "SKILL.md"))
    monkeypatch.setattr(loop_stop.shutil, "which", lambda b: "/usr/bin/" + b)

    assert loop_stop.missing_bound_operators() == []
