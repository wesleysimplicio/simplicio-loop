"""Probe installed public CLI help; never treat this as workflow execution."""
from __future__ import annotations

import argparse
import datetime
import hashlib
import importlib.metadata
import json
from pathlib import Path
import re
import shutil
import subprocess
import time


def children(output: str) -> list[str]:
    """Only expand argparse's positional subparser list, not option choices."""
    section = output.split("positional arguments:", 1)
    if len(section) != 2:
        return []
    first = next((line.strip() for line in section[1].splitlines() if line.strip()), "")
    match = re.fullmatch(r"\{([a-zA-Z0-9_,.-]+)\}(?:\s+.*)?", first)
    return match[1].split(",") if match else []


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    distribution = importlib.metadata.distribution("simplicio-loop")
    entries = sorted(ep.name for ep in distribution.entry_points if ep.group == "console_scripts")
    pending = [[name] for name in entries]
    # The installed CLI forwards this surface before its top-level argparse
    # parser. It cannot be discovered from that parser's positional choices.
    pending.extend([["simplicio-loop", "tasks"], ["simplicio-loop", "tasks", "run"]])
    seen = set()
    expanded_help = set()
    rows = []
    while pending:
        route = pending.pop(0)
        if tuple(route) in seen:
            continue
        seen.add(tuple(route))
        argv = route + ["--help"]
        start = time.perf_counter_ns()
        try:
            result = subprocess.run(argv, capture_output=True, text=True,
                                    stdin=subprocess.DEVNULL, timeout=30)
            code, stdout, stderr = result.returncode, result.stdout, result.stderr
            state = "help-only" if code == 0 else "help-failed"
        except (subprocess.TimeoutExpired, OSError) as exc:
            code, stdout, stderr, state = None, "", type(exc).__name__, "help-blocked"
        elapsed = time.perf_counter_ns() - start
        index = len(rows)
        (output / f"{index:04}.stdout.txt").write_text(stdout)
        (output / f"{index:04}.stderr.txt").write_text(stderr)
        executable = shutil.which(route[0])
        rows.append({"argv": argv, "status": state, "exit_code": code,
                     "help_wall_ns": elapsed, "executable": executable,
                     "executable_sha256": hashlib.sha256(Path(executable).read_bytes()).hexdigest() if executable else None,
                     "stdout": f"{index:04}.stdout.txt", "stderr": f"{index:04}.stderr.txt",
                     "workflow_status": "not-run"})
        # Forwarding wrappers may ignore trailing argv and repeat parent help.
        # Do not turn such a response into an infinitely expanding command tree.
        help_identity = (route[0], hashlib.sha256(stdout.encode()).hexdigest())
        if code == 0 and help_identity not in expanded_help and len(route) < 6:
            expanded_help.add(help_identity)
            pending.extend(route + [child] for child in children(stdout))
        elif code == 0 and children(stdout):
            rows[-1]["discovery_warning"] = "repeated_help_or_depth_bound; manual reconciliation required"
        report = {"schema": "loop-benchmark-help-coverage/v1",
                  "version": distribution.version, "entry_points": entries,
                  "recorded_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                  "discovery": "installed entry points, verified tasks forwarding routes, and argparse positional help lists; other parser styles require manual reconciliation",
                  "provider_calls": 0, "rows": rows}
        (output / "coverage.json").write_text(json.dumps(report, indent=2))
        print(state, " ".join(route), flush=True)
    print(f"interfaces={len(rows)}; workflows_executed=0; artifact={output}")
    return 0 if all(row["status"] == "help-only" for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
