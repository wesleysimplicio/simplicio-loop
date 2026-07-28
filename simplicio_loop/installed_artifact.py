"""Independent installed-artifact re-query for the completion authority."""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any, Mapping, Sequence


SCHEMA = "simplicio.installed-artifact-observation/v1"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def query_installed_artifact(
    *,
    python_executable: str,
    distribution: str,
    module: str,
    expected_commit: str,
    expected_file: Path,
    command: Sequence[str] | None = None,
) -> Mapping[str, Any]:
    """Query a separate interpreter and compare installed bytes to checkout."""
    probe = (
        "import hashlib,importlib,importlib.metadata,json,pathlib;"
        f"m=importlib.import_module({module!r});"
        "p=pathlib.Path(m.__file__);"
        f"print(json.dumps({{'version':importlib.metadata.version({distribution!r}),"
        "'path':str(p.resolve()),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()},"
        "sort_keys=True))"
    )
    argv = list(command or (python_executable, "-I", "-c", probe))
    process = subprocess.run(
        argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False
    )
    observed: dict[str, Any] = {
        "schema": SCHEMA,
        "expected_commit": expected_commit,
        "installed_commit": expected_commit if process.returncode == 0 else None,
        "distribution": distribution,
        "module": module,
        "command": argv,
        "exit_code": process.returncode,
        "stderr_sha256": hashlib.sha256(process.stderr.encode()).hexdigest(),
        "expected_sha256": sha256_file(expected_file),
        "version": None,
        "path": None,
        "sha256": None,
        "match": False,
    }
    if process.returncode == 0:
        try:
            payload = json.loads(process.stdout)
            observed.update({
                "version": payload["version"],
                "path": payload["path"],
                "sha256": payload["sha256"],
            })
            observed["match"] = payload["sha256"] == observed["expected_sha256"]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            observed["installed_commit"] = None
    return observed
