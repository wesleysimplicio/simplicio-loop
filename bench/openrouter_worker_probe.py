"""One real model-worker proposal; not a completed Loop benchmark trial."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import time
import urllib.error
import urllib.request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--repair-response", help="terminal response rejected for missing files envelope; one explicit repair")
    parser.add_argument("--handoff", action="store_true", help="consume only the producer's execution context from a ready Mapper handoff")
    args = parser.parse_args()
    output = Path(args.output)
    if output.exists():
        parser.error("refusing to overwrite provider evidence or retry an existing request")
    context = Path(args.context).read_text()
    parent_context_hash = hashlib.sha256(context.encode()).hexdigest()
    if args.handoff:
        handoff = json.loads(context)
        packet = handoff.get("execution_context", {})
        if (handoff.get("ready") is not True
                or packet.get("schema") != "simplicio.execution-context/v1"
                or packet.get("needs_broader_context") is not False
                or not packet.get("fidelity", {}).get("sufficient")):
            parser.error("Mapper handoff is not execution-ready")
        context = json.dumps(packet, ensure_ascii=False, separators=(",", ":"))
    task = Path(args.task).read_text()
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not key:
        parser.error("OPENROUTER_API_KEY missing")
    payload = {"model": "deepseek/deepseek-v4.1-flash", "temperature": 0,
               "max_tokens": 2048, "response_format": {"type": "json_object"},
               "messages": [
                   {"role": "system", "content": "You are a coding worker, never an execution authority. Propose only the named task's file changes. Return JSON with a files object mapping relative paths to complete UTF-8 contents. Do not claim execution or verification. Mapper context follows:\n" + context},
                   {"role": "user", "content": task}]}
    report = {"schema": "loop-provider-worker-probe/v1", "status": "request_started",
              "model": payload["model"], "context_sha256": hashlib.sha256(context.encode()).hexdigest(),
              "task_sha256": hashlib.sha256(task.encode()).hexdigest(),
              "task": args.task, "context": args.context, "task_delivered": False}
    report["parent_context_sha256"] = parent_context_hash
    report["context_mode"] = "mapper_execution_context" if args.handoff else "raw_diagnostic"
    if args.repair_response:
        previous_text = Path(args.repair_response).read_text()
        previous = json.loads(previous_text)
        if previous.get("status") != "response_received":
            parser.error("cannot retry a request with unresolved provider state")
        choice = previous["response"]["choices"][0]
        if choice.get("finish_reason") != "stop":
            parser.error("repair is restricted to complete format-invalid responses")
        body = choice["message"]["content"]
        decoded = json.loads(body)
        if not isinstance(decoded, dict) or "files" in decoded:
            parser.error("repair is restricted to a missing files envelope")
        payload["messages"].extend([
            {"role": "assistant", "content": body},
            {"role": "user", "content": 'Your proposal was not applied: missing required top-level files object. Return {"files":{"relative/path":"complete file content"}} for the same task. No other changes.'}])
        report["repair_of"] = str(Path(args.repair_response).resolve())
        report["repair_response_sha256"] = hashlib.sha256(previous_text.encode()).hexdigest()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2))
    request = urllib.request.Request("https://openrouter.ai/api/v1/chat/completions",
        data=json.dumps(payload).encode(), headers={"Authorization": "Bearer " + key,
                                                    "Content-Type": "application/json"})
    started = time.perf_counter_ns()
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            result = json.load(response)
        report.update(status="response_received", response=result,
                      provider_wall_ns=time.perf_counter_ns() - started)
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        report.update(status="failed_or_unknown_no_retry", error=type(exc).__name__,
                      provider_wall_ns=time.perf_counter_ns() - started)
        if isinstance(exc, urllib.error.HTTPError):
            report["http_status"] = exc.code
    output.write_text(json.dumps(report, indent=2))
    print(json.dumps({"status": report["status"], "provider_wall_ns": report["provider_wall_ns"],
                      "usage": report.get("response", {}).get("usage"), "artifact": str(output)}, indent=2))
    return 0 if report["status"] == "response_received" else 2


if __name__ == "__main__":
    raise SystemExit(main())
