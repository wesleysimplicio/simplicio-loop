"""Deterministic Prism route for the public Loop surface."""
from __future__ import annotations

import argparse
import hashlib
import json
from typing import Any

from .capability_catalog import load_catalog

def route(task: str) -> dict[str, Any]:
    text = task.casefold()
    if any(x in text for x in ("gate", "checkpoint", "receipt", "mcp", "govern", "evidence")):
        intent = "govern"
        selected = ["runtime.gate", "runtime.checkpoint", "runtime.receipt", "runtime.reconcile"]
        pre = ["external Runtime is installed"]
        fallback = ["use Loop without Runtime when governance is not required"]
    elif any(x in text for x in ("all issues", "todas as issues", "batch", "parallel", "multiple", "retry", "until done")):
        intent = "orchestrate"
        selected = ["mapper.snapshot-create", "mapper.context-select", "fast.search", "loop.plan", "loop.slot-dispatch", "loop.retry", "loop.complete"]
        pre = ["repository and revision are known", "one task per slot"]
        fallback = ["run a single-task recipe when fan-out is unnecessary"]
    elif any(x in text for x in ("test", "validate", "verify", "lint", "check")):
        intent = "validate"
        selected = ["mapper.snapshot-create", "mapper.context-select", "dev-cli.preflight", "dev-cli.tests", "dev-cli.evidence"]
        pre = ["fresh compatible Mapper snapshot"]
        fallback = ["bounded read-only validation"]
    elif any(x in text for x in ("fix", "implement", "add", "edit", "change", "refactor", "corrigir", "implementar")):
        intent = "mutate"
        selected = ["mapper.snapshot-create", "mapper.context-select", "dev-cli.preflight", "dev-cli.edit", "dev-cli.tests", "dev-cli.evidence"]
        pre = ["fresh compatible Mapper snapshot", "Dev CLI preflight passes"]
        fallback = ["stop when revision or scope is unknown"]
    elif any(x in text for x in ("search", "find", "where", "similar", "precedent", "buscar", "localizar")):
        intent = "retrieve"
        selected = ["mapper.snapshot-create", "mapper.context-select", "fast.search", "fast.rank"]
        pre = ["compatible Fast index for broad retrieval"]
        fallback = ["bounded Mapper survey when Fast is unavailable"]
    else:
        intent = "survey"
        selected = ["mapper.project-survey", "mapper.snapshot-create", "mapper.context-select"]
        pre = ["repository, revision and scope are pinned"]
        fallback = ["survey-degraded read-only fallback"]
    catalog = load_catalog()
    known = {item["id"]: item for item in catalog["capabilities"]}
    selected = [capability for capability in selected if capability in known]
    skills = sorted({known[capability]["skill"] for capability in selected})
    adapters = []
    if any(capability.startswith("mapper.") for capability in selected):
        adapters.append({"component": "mapper", "surface": "scripts/preflight.py"})
    if any(capability.startswith("loop.") for capability in selected):
        adapters.append({"component": "loop", "surface": "scripts/route_mode.py"})
    if any(capability.startswith("runtime.") for capability in selected):
        adapters.append({"component": "runtime", "surface": "simplicio doctor --json"})
    digest = hashlib.sha256(task.encode()).hexdigest()[:16]
    return {"schema": "simplicio.route/v1", "catalog_schema": catalog["schema"], "route_id": "simplicio.route/" + digest, "intent": intent, "selected_capabilities": selected, "skills_to_load": skills, "existing_adapters": adapters, "order": selected, "preconditions": pre, "fallbacks": fallback, "cost_estimate": {"mode": "static", "status": "requires_measurement"}, "evidence_requirements": ["selected capability emits evidence", "completion oracle is proven"], "unresolved": []}

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Route a task to Simplicio capabilities")
    parser.add_argument("task", nargs="+")
    args = parser.parse_args(argv)
    print(json.dumps(route(" ".join(args.task)), ensure_ascii=False, sort_keys=True))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())

