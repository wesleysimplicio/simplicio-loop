#!/usr/bin/env python3
"""Post-install smoke for a FILE-based (skills+hooks+scripts) install target (#293 §6, richer
half).

`scripts/install_smoke.py` proves the PACKAGE clean-room story (build a wheel, install it into a
throwaway venv, run `simplicio-loop --help` for real). This module proves the other half: once
`install_executor.apply()` has copied the worker scripts into a `--target` directory, the copies
that landed there must actually RUN and produce real, meaningful output — not just parse as valid
Python (that was the previous, weaker bar in `tests/test_system_clean_install.py`).

Concretely, against the installed `<target>/scripts/` copies:

  * `doctor.py --help`   — real argparse usage text, proves the entrypoint itself runs;
  * `doctor.py --json`   — a real JSON array of component checks (name/tier/status per item);
  * `preflight.py --help` — real argparse usage text;
  * `task_anchor.py selftest` — a REAL minimal task run through the installed toolchain: this
    worker's own `selftest` subcommand exercises freeze/preserve/drift/coverage/gate/checklist
    deterministically (no filesystem/network needed) and prints a `PASS (n/n)` / `FAIL (n/n)`
    line — the closest a fast, host-independent smoke can get to "executar uma task mínima" from
    the issue's plan without requiring the real `simplicio-dev-cli`/`simplicio-mapper` operators
    to be installed on the smoke-test host.

Every check asserts on the ACTUAL parsed/printed content (argparse usage substrings, JSON shape,
a numeric pass/fail count), not merely a zero exit code or "the file is there" — the concrete gap
#293's plan called out ("não apenas 'arquivo existe'").
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from typing import Any, Dict, List, Optional

SCHEMA = "simplicio.install-post-smoke/v1"


def _run(cmd: List[str], *, cwd: str, timeout: int = 30, env: Optional[Dict[str, str]] = None
         ) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout,
                          stdin=subprocess.DEVNULL, env=env or os.environ.copy())


def _check_help(name: str, script_path: str, target: str) -> Dict[str, Any]:
    if not os.path.isfile(script_path):
        return {"name": name, "ok": False, "reason": "script_missing", "path": script_path}
    r = _run([sys.executable, script_path, "--help"], cwd=target)
    out = (r.stdout or "") + (r.stderr or "")
    ok = r.returncode == 0 and "usage" in out.lower()
    return {"name": name, "ok": ok, "returncode": r.returncode,
           "stdout_tail": out.strip().splitlines()[-10:]}


def _check_doctor_json(target: str) -> Dict[str, Any]:
    script_path = os.path.join(target, "scripts", "doctor.py")
    if not os.path.isfile(script_path):
        return {"name": "doctor --json", "ok": False, "reason": "script_missing"}
    r = _run([sys.executable, script_path, "--json"], cwd=target, timeout=60)
    ok = False
    detail: Dict[str, Any] = {}
    try:
        components = json.loads(r.stdout)
        ok = (isinstance(components, list) and len(components) > 0
              and all(isinstance(c, dict) and "name" in c and "tier" in c and "status" in c
                      for c in components))
        detail["component_count"] = len(components) if isinstance(components, list) else 0
        detail["tiers_seen"] = sorted({c.get("tier") for c in components}) if ok else []
    except (ValueError, TypeError):
        detail["parse_error"] = True
    return {"name": "doctor --json", "ok": ok, "returncode": r.returncode, **detail}


def _check_task_anchor_selftest(target: str) -> Dict[str, Any]:
    """Run the installed `task_anchor.py`'s own `selftest` subcommand — a real, minimal task
    exercised end-to-end through the copies this install put on disk. Asserts a real
    `PASS|FAIL (n/n)` line with n > 0, not merely a zero exit code."""
    script_path = os.path.join(target, "scripts", "task_anchor.py")
    if not os.path.isfile(script_path):
        return {"name": "task_anchor selftest", "ok": False, "reason": "script_missing"}
    r = _run([sys.executable, script_path, "selftest"], cwd=target, timeout=30)
    out = (r.stdout or "") + (r.stderr or "")
    m = re.search(r"selftest:\s*(PASS|FAIL)\s*\((\d+)/(\d+)\)", out)
    ok = bool(m) and m.group(1) == "PASS" and int(m.group(3)) > 0
    return {
        "name": "task_anchor selftest", "ok": ok, "returncode": r.returncode,
        "checks_passed": int(m.group(2)) if m else None,
        "checks_total": int(m.group(3)) if m else None,
        "stdout_tail": out.strip().splitlines()[-5:],
    }


def run_post_install_smoke(target: str) -> Dict[str, Any]:
    """Run the full richer smoke suite against an already-installed `target` directory (i.e.
    after `install_executor.apply()` copied scripts/hooks/skills into it). Returns a
    `simplicio.install-post-smoke/v1` receipt with one entry per check plus an overall `ok`."""
    target = os.path.abspath(target)
    checks = [
        _check_help("doctor --help", os.path.join(target, "scripts", "doctor.py"), target),
        _check_help("preflight --help", os.path.join(target, "scripts", "preflight.py"), target),
        _check_doctor_json(target),
        _check_task_anchor_selftest(target),
    ]
    return {
        "schema": SCHEMA,
        "target": target,
        "checks": checks,
        "ok": all(c.get("ok") for c in checks),
    }


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Run real post-install smoke checks (--help/doctor/preflight/a minimal task) "
                    "against an already-installed target directory.")
    parser.add_argument("--target", required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    receipt = run_post_install_smoke(args.target)
    print(json.dumps(receipt, indent=None if args.json else 2, sort_keys=True))
    return 0 if receipt["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
