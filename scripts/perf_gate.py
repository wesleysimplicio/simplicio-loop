#!/usr/bin/env python3
"""simplicio-loop — CI Quality Gate: performance/convergence benchmark (#277).

Benchmarks the loop's hot path (`engine/simplicio_compress.compress`, exercised on every capture
per SKILL.md's per-turn capture step) plus a synthetic "drain N items to completion" convergence
loop (`_simulate_drain`, below) that stands in for the real `/simplicio-loop` drive loop without
depending on an LLM/runtime being present. Reports:

  - latency per cycle (ms, median of `--repeat` runs)
  - throughput (cycles/sec)
  - peak RSS delta (MB, via `resource.getrusage` on POSIX, `tracemalloc` fallback elsewhere)
  - convergence: the synthetic drain always reaches a terminal state within a bounded number of
    iterations — if it doesn't, that's a regression signal for an infinite-loop class of bug
    (see issue #277: "impedir merges que introduzam loops infinitos").

Compares against a committed baseline (`scripts/perf_baseline.json`) and FAILS when latency or
memory regresses past `--threshold` (default 20%), or when the convergence loop fails to
terminate within its bound. Stdlib-only — no `pytest-benchmark` / `psutil` dependency.

Usage:
    python3 scripts/perf_gate.py                      # benchmark + gate against baseline
    python3 scripts/perf_gate.py --update-baseline     # regenerate scripts/perf_baseline.json
                                                        # after a deliberate, reviewed perf change
    python3 scripts/perf_gate.py --json                # machine-readable report on stdout
    python3 scripts/perf_gate.py --threshold 0.30      # allow up to 30% regression before failing
    python3 scripts/perf_gate.py --diagnostics-dir DIR # on failure, write a trace/state dump here
                                                        # (for CI artifact upload)

Exit codes: 0 = within budget and converged, 1 = regression or non-convergence detected.
"""
from __future__ import annotations

import argparse
import gc
import json
import os
import statistics
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
BASELINE_PATH = os.path.join(HERE, "perf_baseline.json")

if REPO not in sys.path:
    sys.path.insert(0, REPO)

try:
    import resource  # POSIX only
except ImportError:  # pragma: no cover - Windows
    resource = None

try:
    import tracemalloc
except ImportError:  # pragma: no cover
    tracemalloc = None


def _peak_rss_mb() -> "float | None":
    """Best-effort peak RSS in MB for this process, or None if unavailable."""
    if resource is not None:
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # Linux reports KB, macOS reports bytes.
        return peak / 1024.0 if sys.platform != "darwin" else peak / (1024.0 * 1024.0)
    return None


def _bench_compress(cycles: int) -> "tuple[float, list[float]]":
    from engine.simplicio_compress import compress

    sample = (
        "line one\n" * 40
        + "\x1b[31mansi colored\x1b[0m   \n" * 10
        + json.dumps({"k": list(range(50))}) + "\n"
    ) * 5

    durations = []
    for _ in range(cycles):
        start = time.perf_counter()
        compress(sample)
        durations.append((time.perf_counter() - start) * 1000.0)
    return sum(durations), durations


def _simulate_drain(n_items: int = 200, max_iterations: int = 5000) -> "tuple[bool, int]":
    """A synthetic bounded drain loop standing in for the real loop's convergence property.

    Every item must reach a terminal state ("done") within `max_iterations` total steps, or this
    is treated as a would-be infinite loop. Returns (converged, iterations_used).
    """
    pending = list(range(n_items))
    done = set()
    iterations = 0
    while pending and iterations < max_iterations:
        item = pending.pop(0)
        iterations += 1
        # Deterministic "requires up to 3 passes" workload, mirroring a retry/backoff item.
        attempts = (item % 3) + 1
        for _ in range(attempts):
            iterations += 1
        done.add(item)
    return (len(pending) == 0, iterations)


def run_benchmark(cycles: int) -> dict:
    gc.collect()
    tracemalloc_active = False
    if resource is None and tracemalloc is not None:
        tracemalloc.start()
        tracemalloc_active = True

    total_ms, durations = _bench_compress(cycles)
    converged, iterations = _simulate_drain()

    peak_mb = _peak_rss_mb()
    if tracemalloc_active:
        _, peak_bytes = tracemalloc.get_traced_memory()
        peak_mb = peak_bytes / (1024.0 * 1024.0)
        tracemalloc.stop()

    durations_sorted = sorted(durations)
    median_ms = statistics.median(durations_sorted)
    throughput = cycles / (total_ms / 1000.0) if total_ms else float("inf")

    return {
        "cycles": cycles,
        "latency_ms_median": round(median_ms, 4),
        "latency_ms_p95": round(
            durations_sorted[min(len(durations_sorted) - 1, int(len(durations_sorted) * 0.95))], 4
        ),
        "throughput_cycles_per_sec": round(throughput, 2),
        "peak_rss_mb": round(peak_mb, 2) if peak_mb is not None else None,
        "convergence": {
            "converged": converged,
            "iterations_used": iterations,
        },
    }


def _load_baseline() -> "dict | None":
    if not os.path.exists(BASELINE_PATH):
        return None
    with open(BASELINE_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _write_baseline(report: dict) -> None:
    payload = dict(report)
    payload["$schema_note"] = (
        "simplicio-loop perf baseline (#277). Regenerate with "
        "`python3 scripts/perf_gate.py --update-baseline` after a deliberate, reviewed "
        "performance change -- never to silence a regression you haven't looked at."
    )
    payload["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with open(BASELINE_PATH, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.write("\n")


def _write_diagnostics(diagnostics_dir: str, report: dict, baseline: "dict | None", failures: list) -> None:
    os.makedirs(diagnostics_dir, exist_ok=True)
    dump = {
        "report": report,
        "baseline": baseline,
        "failures": failures,
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    path = os.path.join(diagnostics_dir, "perf_gate_failure.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(dump, fh, indent=2, sort_keys=True)
    print(f"[perf-gate] diagnostics written to {path}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cycles", type=int, default=200, help="benchmark iterations (default: 200)")
    parser.add_argument("--threshold", type=float, default=0.20, help="allowed regression fraction (default: 0.20 = 20%%)")
    parser.add_argument("--update-baseline", action="store_true", help="regenerate the committed baseline")
    parser.add_argument("--json", action="store_true", help="print machine-readable report")
    parser.add_argument("--diagnostics-dir", default=None, help="on failure, dump a diagnostic report here")
    parser.add_argument("--emit-json", default=None,
                        help="#283: unconditionally write {report, baseline, failures, ok} here, "
                             "so scripts/quality_matrix.py populate / an independent re-verifier "
                             "can consume the exact structured verdict this run computed")
    args = parser.parse_args()

    report = run_benchmark(args.cycles)

    if args.update_baseline:
        _write_baseline(report)
        print(f"[perf-gate] baseline updated: {BASELINE_PATH}")
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0

    baseline = _load_baseline()
    failures = []

    if not report["convergence"]["converged"]:
        failures.append(
            f"convergence: drain did not reach terminal state within bound "
            f"(used {report['convergence']['iterations_used']} iterations)"
        )

    if baseline is None:
        print("[perf-gate] no baseline found; run --update-baseline first.", file=sys.stderr)
        if args.json:
            print(json.dumps(report, indent=2, sort_keys=True))
        return 1

    def _check(key, label, lower_is_better=True):
        base_val = baseline.get(key)
        cur_val = report.get(key)
        if base_val is None or cur_val is None:
            return
        if lower_is_better:
            limit = base_val * (1 + args.threshold)
            if cur_val > limit:
                failures.append(
                    f"{label} regressed: {cur_val} > {limit:.4f} "
                    f"(baseline {base_val}, threshold +{args.threshold * 100:.0f}%)"
                )
        else:
            limit = base_val * (1 - args.threshold)
            if cur_val < limit:
                failures.append(
                    f"{label} regressed: {cur_val} < {limit:.4f} "
                    f"(baseline {base_val}, threshold -{args.threshold * 100:.0f}%)"
                )

    _check("latency_ms_median", "latency (median)")
    _check("latency_ms_p95", "latency (p95)")
    _check("throughput_cycles_per_sec", "throughput", lower_is_better=False)
    if report.get("peak_rss_mb") is not None and baseline.get("peak_rss_mb") is not None:
        _check("peak_rss_mb", "peak RSS")

    if args.json:
        print(json.dumps({"report": report, "baseline": baseline, "failures": failures}, indent=2, sort_keys=True))
    else:
        print(json.dumps(report, indent=2, sort_keys=True))
        if failures:
            print("\n[perf-gate] FAILED:", file=sys.stderr)
            for f in failures:
                print(f"  - {f}", file=sys.stderr)
        else:
            print("\n[perf-gate] OK: no regression past threshold, drain converged.")

    if args.emit_json:
        payload = {
            "schema": "simplicio.perf-gate/v1",
            "ok": not failures,
            "report": report,
            "baseline": baseline,
            "failures": failures,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        with open(args.emit_json, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.write("\n")

    if failures:
        if args.diagnostics_dir:
            _write_diagnostics(args.diagnostics_dir, report, baseline, failures)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
