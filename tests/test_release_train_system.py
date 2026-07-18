"""End-to-end tests for the `simplicio-loop release-train check` entrypoint (#558).

Covers:
  AC4  integration — the CLI subcommand dispatches to release_manifest and
       prints a structured JSON summary.
  AC5  system — running the entrypoint on a consistent repo exits 0.
  AC7  benchmark — the check completes within a sanity latency bound (ms).
"""
import json
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CLI = REPO / "simplicio_loop" / "cli.py"


def _run_check(repo: Path):
    return subprocess.run(
        [sys.executable, "-m", "simplicio_loop.cli", "release-train", "check", "--repo", str(repo)],
        capture_output=True,
        text=True,
        cwd=str(REPO),
    )


def test_release_train_check_prints_structured_json():
    proc = _run_check(REPO)
    # AC4: must emit parseable JSON summary
    assert proc.returncode in (0, 1), proc.stderr
    payload = json.loads(proc.stdout)
    assert "ready" in payload
    assert "schema_errors" in payload
    assert "manifest" in payload


def test_release_train_check_exits_zero_on_consistent_repo():
    # AC5: this repo's own manifests are consistent (pyproject/npm/plugin match)
    proc = _run_check(REPO)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_release_train_check_latency_within_bound():
    # AC7: benchmark — a small-repo check must finish well under 1s
    start = time.perf_counter()
    _run_check(REPO)
    elapsed_ms = (time.perf_counter() - start) * 1000.0
    assert elapsed_ms < 1000.0, f"release-train check took {elapsed_ms:.1f}ms"


def test_release_train_check_fails_on_bad_component_fixture(tmp_path: Path):
    # AC3 negative: a malformed component fixture must surface schema errors
    # and make the check non-ready (exit 1).
    fx = tmp_path / "tests" / "fixtures" / "release_train"
    fx.mkdir(parents=True)
    (fx / "component_release_ok.json").write_text(
        json.dumps({"component": "x", "version": "not-semver"})
    )
    # ecosystem fixture absent is fine; repo manifest may be consistent
    proc = subprocess.run(
        [sys.executable, "-m", "simplicio_loop.cli", "release-train", "check", "--repo", str(tmp_path)],
        capture_output=True,
        text=True,
        cwd=str(REPO),
    )
    assert proc.returncode == 1, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["ready"] is False
    assert any("component-release" in e for e in payload["schema_errors"])


def test_release_train_check_fails_on_repo_drift(tmp_path: Path):
    # AC2/AC5: local manifest drift must make the check non-ready.
    (tmp_path / "packaging" / "npm").mkdir(parents=True)
    (tmp_path / ".cursor-plugin").mkdir()
    (tmp_path / "simplicio_loop").mkdir()
    (tmp_path / "pyproject.toml").write_text('version = "1.2.3"\n')
    (tmp_path / "packaging" / "npm" / "package.json").write_text('{"version":"1.2.4"}')
    (tmp_path / ".cursor-plugin" / "plugin.json").write_text('{"version":"1.2.3"}')
    (tmp_path / "simplicio_loop" / "__init__.py").write_text('__version__ = "1.2.3"\n')
    proc = subprocess.run(
        [sys.executable, "-m", "simplicio_loop.cli", "release-train", "check", "--repo", str(tmp_path)],
        capture_output=True,
        text=True,
        cwd=str(REPO),
    )
    assert proc.returncode == 1, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["ready"] is False
    assert payload["manifest"]["ready"] is False
