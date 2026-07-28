#!/usr/bin/env python3
"""Build, install and independently re-query the issue #785 wheel."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from simplicio_loop.installed_artifact import query_installed_artifact  # noqa: E402


def checked(argv):
    environment = dict(os.environ)
    environment["PIP_CACHE_DIR"] = "/tmp/simplicio-loop-785-pip-cache"
    process = subprocess.run(
        argv, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, check=False, env=environment,
    )
    if process.returncode:
        raise RuntimeError(
            "command failed (%d): %s\n%s" % (
                process.returncode, " ".join(argv), process.stderr
            )
        )
    return process


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="loop-785-installed-") as directory:
        root = Path(directory)
        wheel_dir = root / "wheel"
        wheel_dir.mkdir()
        checked([
            sys.executable, "-m", "pip", "wheel", "--no-deps",
            "--no-build-isolation", "--wheel-dir", str(wheel_dir), ".",
        ])
        wheel = next(wheel_dir.glob("simplicio_loop-*.whl"))
        venv = root / "venv"
        checked([sys.executable, "-m", "venv", str(venv)])
        python = venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        checked([
            str(python), "-m", "pip", "install", "--no-deps",
            "--disable-pip-version-check", str(wheel),
        ])
        observed = dict(query_installed_artifact(
            python_executable=str(python),
            distribution="simplicio-loop",
            module="simplicio_loop.work_gap_ledger",
            expected_commit=args.commit,
            expected_file=ROOT / "simplicio_loop" / "work_gap_ledger.py",
        ))
        observed["wheel"] = {
            "name": wheel.name,
            "size": wheel.stat().st_size,
        }
        observed["classification"] = "MEASURED_LOCAL"
        observed["local_llm"] = False
        if not observed["match"]:
            raise RuntimeError("installed module differs from checkout")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(observed, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        print(json.dumps({
            "match": observed["match"], "version": observed["version"],
            "sha256": observed["sha256"], "output": str(args.output),
        }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
