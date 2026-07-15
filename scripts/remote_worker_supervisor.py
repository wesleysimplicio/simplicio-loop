#!/usr/bin/env python3
"""Backward-compatible shim over ``simplicio_loop.remote_worker_supervisor_cli`` (issue #286 step 11).

The real implementation now lives inside the ``simplicio_loop`` package -- unlike this
``scripts/`` file, that module ships in the installed wheel/sdist, so ``pip install
simplicio-loop`` yields a genuinely runnable supervisor binary (the
``simplicio-remote-worker-supervisor`` console script) instead of source that only works from a
git checkout. This file is kept so existing repo-local tooling/tests that invoke
``python3 scripts/remote_worker_supervisor.py ...`` directly keep working unchanged.
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from simplicio_loop.remote_worker_supervisor_cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
