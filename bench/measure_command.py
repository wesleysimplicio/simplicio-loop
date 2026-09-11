"""Measure an owned command tree; never infer provider tokens or task success."""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import resource
import signal
import subprocess
import time

import psutil


def measure(argv, cwd, output, interval=0.1, timeout=180):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    started = time.perf_counter_ns()
    samples, observed_cpu, errors = [], {}, []
    timed_out = False
    usage_before = resource.getrusage(resource.RUSAGE_CHILDREN)
    psutil.cpu_percent(interval=None)
    with (output / "stdout.txt").open("w") as stdout, (output / "stderr.txt").open("w") as stderr:
        child = subprocess.Popen(argv, cwd=cwd, stdout=stdout, stderr=stderr,
                                 stdin=subprocess.DEVNULL, start_new_session=True)
        root = psutil.Process(child.pid)
        while True:
            now = time.perf_counter_ns()
            processes = [root]
            try:
                processes += root.children(recursive=True)
            except psutil.Error as exc:
                errors.append(type(exc).__name__)
            rss, count = 0, 0
            for process in processes:
                try:
                    with process.oneshot():
                        identity = (process.pid, process.create_time())
                        cpu = process.cpu_times()
                        observed_cpu[identity] = max(observed_cpu.get(identity, 0), cpu.user + cpu.system)
                        rss += process.memory_info().rss
                        count += 1
                except psutil.Error as exc:
                    errors.append(type(exc).__name__)
            samples.append({"elapsed_ns": now - started, "tree_rss_bytes": rss,
                            "observed_processes": count,
                            "host_available_ram_bytes": psutil.virtual_memory().available,
                            "host_cpu_percent": psutil.cpu_percent(interval=None) if samples else None,
                            "host_load_1m": os.getloadavg()[0]})
            if child.poll() is not None:
                break
            if (now - started) / 1e9 > timeout:
                timed_out = True
                os.killpg(child.pid, signal.SIGTERM)
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait()
                break
            time.sleep(interval)
        code = child.wait()
    wall = time.perf_counter_ns() - started
    usage_after = resource.getrusage(resource.RUSAGE_CHILDREN)
    mean_rss = None
    if len(samples) > 1:
        span = samples[-1]["elapsed_ns"] - samples[0]["elapsed_ns"]
        if span:
            mean_rss = sum((b["elapsed_ns"] - a["elapsed_ns"]) *
                           (a["tree_rss_bytes"] + b["tree_rss_bytes"]) / 2
                           for a, b in zip(samples, samples[1:])) / span
    receipt = {"schema": "loop-command-resource-measurement/v1", "argv": argv,
               "cwd": str(Path(cwd).resolve()), "exit_code": code, "timed_out": timed_out,
               "wall_ns": wall, "sample_interval_seconds": interval,
               "reaped_children_cpu_seconds": (usage_after.ru_utime + usage_after.ru_stime
                                               - usage_before.ru_utime - usage_before.ru_stime),
               "cpu_seconds_observed_lower_bound": sum(observed_cpu.values()),
               "peak_tree_rss_bytes_sampled": max((x["tree_rss_bytes"] for x in samples), default=None),
               "mean_tree_rss_bytes_time_weighted": mean_rss,
               "unique_processes_observed": len(observed_cpu), "sampling_errors": sorted(set(errors)),
               "machine": {"platform": platform.platform(), "cpu_count": os.cpu_count(),
                           "ram_bytes": psutil.virtual_memory().total,
                           "psutil_version": importlib.metadata.version("psutil")},
               "limitations": ["short-lived children may escape sampling",
                               "sampled RSS is not exact OS peak RSS",
                               "RSS sums can double count shared memory",
                               "host load average is not measured CPU utilization",
                               "no provider metrics inferred; use provider receipt",
                               "exit zero alone does not prove a delivered task"],
               "stdout": "stdout.txt", "stderr": "stderr.txt", "samples": samples}
    (output / "receipt.json").write_text(json.dumps(receipt, indent=2))
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cwd", default=".")
    parser.add_argument("--output", required=True)
    parser.add_argument("--interval", type=float, default=0.1)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    argv = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not argv or args.interval <= 0 or args.timeout <= 0:
        parser.error("command and positive interval/timeout are required")
    if any("sk-or-" in item for item in argv):
        parser.error("credentials must stay in the inherited environment")
    result = measure(argv, args.cwd, args.output, args.interval, args.timeout)
    print(json.dumps({key: value for key, value in result.items() if key != "samples"}, indent=2))
    return 2 if result["timed_out"] else result["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
