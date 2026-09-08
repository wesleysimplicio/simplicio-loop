"""Clean-room installed-package regression coverage for issue #1233."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.external_integration


def _run(command: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=180,
    )


def _failure(result: subprocess.CompletedProcess[str]) -> str:
    return "\n".join((result.stdout, result.stderr)).strip()[-4000:]


def test_quality_provider_imports_from_clean_installed_wheel(tmp_path: Path) -> None:
    """Build and import the provider without the checkout on the interpreter path."""
    dist = tmp_path / "dist"
    dist.mkdir()
    build = _run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--no-build-isolation",
            str(REPO_ROOT),
            "--wheel-dir",
            str(dist),
        ],
        cwd=REPO_ROOT,
    )
    assert build.returncode == 0, _failure(build)
    wheels = sorted(dist.glob("*.whl"))
    assert len(wheels) == 1, [wheel.name for wheel in wheels]

    venv = tmp_path / "venv"
    created = _run([sys.executable, "-m", "venv", "--clear", str(venv)], cwd=REPO_ROOT)
    assert created.returncode == 0, _failure(created)
    venv_python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")

    installed = _run(
        [str(venv_python), "-m", "pip", "install", "--no-deps", "--no-index", str(wheels[0])],
        cwd=tmp_path,
    )
    assert installed.returncode == 0, _failure(installed)

    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["PYTHONNOUSERSITE"] = "1"
    probe = _run(
        [
            str(venv_python),
            "-I",
            "-c",
            (
                "import importlib.metadata as metadata, json\n"
                "from pathlib import Path\n"
                "from simplicio_loop.quality_providers import simplicio_loop_quality\n"
                "from scripts.check import CORE_GATE_TIMEOUT_SECONDS\n"
                "from scripts.check_runtime import CommandReason\n"
                "files = {str(path) for path in (metadata.files('simplicio-loop') or [])}\n"
                "print(json.dumps({\"version\": metadata.version('simplicio-loop'), \"module_file\": str(Path(simplicio_loop_quality.__file__).resolve()), \"scripts_check_packaged\": \"scripts/check.py\" in files, \"scripts_check_runtime_packaged\": \"scripts/check_runtime.py\" in files, \"core_timeout\": CORE_GATE_TIMEOUT_SECONDS, \"command_reason\": CommandReason.OK.value}, sort_keys=True))"
            ),
        ],
        cwd=tmp_path,
        env=env,
    )
    assert probe.returncode == 0, _failure(probe)
    observed = json.loads(probe.stdout)
    assert observed["version"]
    assert str(venv.resolve()) in observed["module_file"]
    assert observed["scripts_check_packaged"] is True
    assert observed["scripts_check_runtime_packaged"] is True
    assert observed["core_timeout"] == 900.0
    assert observed["command_reason"] == "ok"
