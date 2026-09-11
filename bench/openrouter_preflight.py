"""Verify benchmark provider access without persisting credentials or billing a completion."""
import argparse
import datetime
import json
import os
from pathlib import Path
import time
import urllib.error
import urllib.request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--model", default="deepseek/deepseek-v4.1-flash")
    args = parser.parse_args()
    destination = Path(args.output)
    if destination.exists():
        parser.error("output already exists; preserve earlier evidence")
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    report = {"schema": "loop-openrouter-preflight/v1", "model": args.model,
              "checked_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
              "credential_present": bool(key), "completion_calls": 0, "checks": []}
    selected = None
    if key:
        for name, endpoint in [("authentication", "/key"), ("model_catalog", "/models")]:
            started = time.perf_counter_ns()
            request = urllib.request.Request("https://openrouter.ai/api/v1" + endpoint,
                                             headers={"Authorization": "Bearer " + key})
            row = {"name": name, "endpoint": endpoint}
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    payload = json.load(response)
                    row["http_status"] = response.status
                if name == "model_catalog":
                    selected = next((item for item in payload.get("data", [])
                                     if item.get("id") == args.model), None)
                    row["exact_model_available"] = selected is not None
                    if selected:
                        report["model_metadata"] = {k: selected.get(k) for k in
                            ("id", "context_length", "pricing", "supported_parameters", "top_provider")}
                else:
                    row["authenticated"] = isinstance(payload.get("data"), dict)
            except urllib.error.HTTPError as exc:
                row["http_status"] = exc.code
                row["error"] = "HTTPError"  # Never persist auth response bodies.
            except (urllib.error.URLError, TimeoutError, ValueError) as exc:
                row["error"] = type(exc).__name__
            row["wall_ns"] = time.perf_counter_ns() - started
            report["checks"].append(row)
    report["ready"] = bool(selected and any(row.get("authenticated") for row in report["checks"]))
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    return 0 if report["ready"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
