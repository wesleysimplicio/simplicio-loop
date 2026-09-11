"""Measured Mapper -> model -> native writer -> oracle pilot, not a Loop CLI arm."""
import argparse
import json
from pathlib import Path
import shutil
import sys
import time

from measure_command import measure
from proposal_to_edit_plan import compile_plan
from queue_workflow_benchmark import TASKS

TARGETS = dict(zip([task[0] for task in TASKS], [
    "src/slug.py", "src/total.py", "answers/timeout.json", "tests/test_total.py",
    "src/unique.py", "src/clamp.py", "docs/configuration.md", "answers/deprecated.json",
    "src/median.py", "src/boolean.py"]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", required=True, choices=list(TARGETS))
    parser.add_argument("--repo", required=True)
    parser.add_argument("--tasks-dir", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--context-intent", choices=["goal", "task-file"], default="goal")
    parser.add_argument("--repair-response")
    args = parser.parse_args()
    root, out = Path(args.repo).resolve(), Path(args.output).resolve()
    out.mkdir(parents=True, exist_ok=False)
    scripts = Path(__file__).resolve().parent
    native = shutil.which("simplicio")
    if not native:
        parser.error("native Simplicio is required for this diagnostic route")
    task = next(item for item in TASKS if item[0] == args.task)
    started = time.perf_counter_ns()
    summary = {"schema": "loop-composed-diagnostic-task/v1", "task": args.task,
               "eligible_loop_cli_arm": False, "verified": False, "stages": []}

    def persist():
        summary["wall_ns"] = time.perf_counter_ns() - started
        (out / "summary.json").write_text(json.dumps(summary, indent=2))

    def stage(name, argv):
        receipt = measure(argv, root, out / name, interval=.1, timeout=150)
        summary["stages"].append({"name": name, "receipt": str(out / name / "receipt.json"),
                                  "exit_code": receipt["exit_code"]})
        persist()
        if receipt["exit_code"] or receipt["timed_out"]:
            raise RuntimeError(name + " failed; no retry")

    try:
        stage("mapper_scan", ["simplicio-mapper", "scan", str(root), "--sync", "--json"])
        intent = ["--goal", task[3]] if args.context_intent == "goal" else [
            "--task-file", str(Path(args.tasks_dir).resolve() / (args.task + ".md"))]
        summary["context_intent"] = args.context_intent
        stage("mapper_handoff", ["simplicio-mapper", "handoff", str(root), *intent,
                                 "--token-budget", "12000", "--limit", "2", "--execution-context", "--json"])
        handoff = out / "mapper_handoff/stdout.txt"
        if json.loads(handoff.read_text()).get("ready") is not True:
            raise RuntimeError("Mapper handoff not ready; no provider call")
        proposal = out / "provider.json"
        repair = ["--repair-response", str(Path(args.repair_response).resolve())] if args.repair_response else []
        stage("provider", [sys.executable, str(scripts / "openrouter_worker_probe.py"),
                           "--task", str(Path(args.tasks_dir).resolve() / (args.task + ".md")),
                           "--context", str(handoff), "--handoff", "--output", str(proposal), *repair])
        response = json.loads(proposal.read_text())
        summary["usage"] = response.get("response", {}).get("usage")
        summary["provider"] = response.get("response", {}).get("provider")
        plan = compile_plan(response, root, [TARGETS[args.task]])
        plan_path = out / "edit-plan.json"
        plan_path.write_text(json.dumps(plan, indent=2))
        stage("apply", [native, "edit", "--repo", str(root), "--plan", str(plan_path)])
        stage("verify", [sys.executable, str(scripts / "verify_queue_fixture.py"),
                         "--repo", str(root), "--task", args.task])
        summary["verified"] = True
    except Exception as exc:
        summary["error"] = str(exc)
        persist()
        print(json.dumps(summary, indent=2))
        return 2
    persist()
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
