#!/usr/bin/env python3
"""Offline preflight and validity gate for benchmark sessions.

This module evaluates an observed session descriptor.  It never calls a model,
provider, MCP server, or shell command, so a passing report is only evidence
that the supplied evidence is complete and internally consistent.  Missing
observations are ``unknown`` and block readiness; they are never inferred.

The descriptor deliberately keeps the checks separate from the savings scorer:
``savings_harness.py`` can estimate tokens from captured text, while this gate
decides whether a run is eligible to be discussed as a completed comparison.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

SCHEMA = "simplicio.benchmark-preflight/v1"
CHECK_NAMES = (
    "task_count",
    "source_files",
    "arm_config",
    "capture_proxy",
    "executable",
    "authenticated_mcp_call",
    "es_module_import",
    "treatment_routing",
    "off_routing",
    "pinned_model_response",
    "captured_priced_generation",
    "actual_upstream",
    "stdin_safe_loop",
    "no_stale_tool_names",
    "tools_list_capability",
)


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    observed: Any = None
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "name": self.name,
            "status": self.status,
            "observed": self.observed,
        }
        if self.reason is not None:
            result["reason"] = self.reason
        return result


def _check(name: str, value: bool | None, observed: Any, reason: str) -> Check:
    if value is True:
        return Check(name, "pass", observed)
    if value is False:
        return Check(name, "fail", observed, reason)
    return Check(name, "unknown", observed, reason)


def _mapping(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _safe_relative(path: Any) -> bool:
    if not isinstance(path, str) or not path:
        return False
    normalized = path.replace("\\", "/")
    return not normalized.startswith("/") and ".." not in normalized.split("/")


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def evaluate_preflight(
    descriptor: Mapping[str, Any], *, expected_tasks: int | None = None
) -> dict[str, Any]:
    """Return the fifteen observed preflight checks and a fail-closed verdict."""
    if not isinstance(descriptor, Mapping):
        raise TypeError("benchmark descriptor must be an object")
    expected_tasks = expected_tasks if expected_tasks is not None else descriptor.get("expected_tasks")
    raw_task_count = descriptor.get("task_count")
    task_count = (
        {"actual": raw_task_count}
        if isinstance(raw_task_count, int) and not isinstance(raw_task_count, bool)
        else _mapping(raw_task_count)
    )
    if task_count is None:
        task_count_check = _check("task_count", None, None, "missing_task_count")
    else:
        actual = task_count.get("actual")
        expected = task_count.get("expected", expected_tasks)
        task_count_check = _check(
            "task_count",
            isinstance(actual, int) and not isinstance(actual, bool)
            and isinstance(expected, int) and actual == expected and actual > 0,
            {"actual": actual, "expected": expected},
            "task_count_mismatch_or_invalid",
        )

    raw_source = descriptor.get("source_files")
    source = (
        {"count": len(raw_source), "paths": raw_source}
        if isinstance(raw_source, list)
        else _mapping(raw_source)
    )
    source_check = _check(
        "source_files",
        bool(source)
        and isinstance(source.get("paths"), list)
        and bool(source.get("paths"))
        and source.get("count") == len(source["paths"])
        and all(_safe_relative(item) for item in source["paths"]),
        None if source is None else {"count": source.get("count"), "paths": source.get("paths")},
        "source_files_missing_or_unbounded",
    )

    arms = _mapping(descriptor.get("arm_config"))
    baseline = _mapping(arms.get("baseline")) if arms else None
    treatment = _mapping(arms.get("treatment")) if arms else None
    arm_check = _check(
        "arm_config",
        bool(arms)
        and baseline is not None
        and treatment is not None
        and baseline.get("task_count") == treatment.get("task_count") == expected_tasks
        and arms.get("alternation") is True
        and baseline.get("name") != treatment.get("name"),
        None if arms is None else {"baseline": baseline, "treatment": treatment},
        "arms_missing_unequal_or_not_alternated",
    )

    capture = _mapping(descriptor.get("capture_proxy"))
    capture_digest = capture.get("artifact_digest") if capture else None
    capture_check = _check(
        "capture_proxy",
        bool(capture)
        and capture.get("kind") in {"jsonl", "receipt"}
        and isinstance(capture.get("path"), str)
        and isinstance(capture.get("records"), int)
        and capture.get("records") == expected_tasks
        and isinstance(capture_digest, str)
        and len(capture_digest) == 64,
        None if capture is None else dict(capture),
        "capture_proxy_missing_unhashed_or_incomplete",
    )

    executable = _mapping(descriptor.get("executable"))
    executable_check = _check(
        "executable",
        bool(executable)
        and executable.get("present") is True
        and isinstance(executable.get("path"), str)
        and bool(executable.get("path"))
        and isinstance(executable.get("version"), str)
        and bool(executable.get("version"))
        and isinstance(executable.get("sha256"), str)
        and len(executable.get("sha256")) == 64,
        None if executable is None else dict(executable),
        "executable_identity_or_digest_missing",
    )

    mcp = _mapping(descriptor.get("authenticated_mcp_call"))
    mcp_check = _check(
        "authenticated_mcp_call",
        bool(mcp)
        and mcp.get("authenticated") is True
        and mcp.get("status") == "success"
        and isinstance(mcp.get("method"), str)
        and bool(mcp.get("method"))
        and bool(mcp.get("receipt")),
        None if mcp is None else dict(mcp),
        "authenticated_mcp_call_missing_or_failed",
    )

    module = _mapping(descriptor.get("es_module_import"))
    module_check = _check(
        "es_module_import",
        bool(module)
        and module.get("imported") is True
        and module.get("node_check_only") is False
        and isinstance(module.get("module"), str)
        and bool(module.get("module")),
        None if module is None else dict(module),
        "es_module_import_not_executed",
    )

    checks = [task_count_check, source_check, arm_check, capture_check, executable_check, mcp_check, module_check]
    for name, key in (("treatment_routing", "treatment_routing"), ("off_routing", "off_routing")):
        route = _mapping(descriptor.get(key))
        checks.append(
            _check(
                name,
                bool(route)
                and route.get("expected") is not None
                and route.get("observed") == route.get("expected")
                and bool(route.get("receipt")),
                None if route is None else dict(route),
                "route_missing_mismatched_or_unreceipted",
            )
        )

    pinned = _mapping(descriptor.get("pinned_model_response"))
    checks.append(
        _check(
            "pinned_model_response",
            bool(pinned)
            and pinned.get("completed") is True
            and pinned.get("model") == descriptor.get("pinned_model")
            and bool(pinned.get("response_id")),
            None if pinned is None else dict(pinned),
            "pinned_model_response_missing_or_drifted",
        )
    )

    priced = _mapping(descriptor.get("captured_priced_generation"))
    checks.append(
        _check(
            "captured_priced_generation",
            bool(priced)
            and bool(priced.get("generation_id"))
            and isinstance(priced.get("cost_usd"), (int, float))
            and not isinstance(priced.get("cost_usd"), bool)
            and priced.get("cost_usd") >= 0
            and priced.get("priced") is True
            and isinstance(priced.get("input_tokens"), int)
            and not isinstance(priced.get("input_tokens"), bool)
            and isinstance(priced.get("output_tokens"), int)
            and not isinstance(priced.get("output_tokens"), bool),
            None if priced is None else dict(priced),
            "priced_generation_or_usage_missing",
        )
    )

    upstream = _mapping(descriptor.get("actual_upstream"))
    checks.append(
        _check(
            "actual_upstream",
            bool(upstream)
            and bool(upstream.get("expected"))
            and upstream.get("observed") == upstream.get("expected")
            and bool(upstream.get("receipt")),
            None if upstream is None else dict(upstream),
            "upstream_missing_or_drifted",
        )
    )

    stdin = _mapping(descriptor.get("stdin_safe_loop"))
    checks.append(
        _check(
            "stdin_safe_loop",
            bool(stdin)
            and stdin.get("stdin") == "DEVNULL"
            and stdin.get("completed_tasks") == expected_tasks
            and stdin.get("hung") is False
            and stdin.get("output_limited") is True,
            None if stdin is None else dict(stdin),
            "stdin_not_isolated_or_tasks_incomplete",
        )
    )

    tools = _mapping(descriptor.get("no_stale_tool_names"))
    checks.append(
        _check(
            "no_stale_tool_names",
            bool(tools)
            and isinstance(tools.get("stale"), list)
            and not tools.get("stale")
            and isinstance(tools.get("observed"), list)
            and bool(tools.get("observed")),
            None if tools is None else dict(tools),
            "stale_or_unobserved_tool_names",
        )
    )

    listed = _mapping(descriptor.get("tools_list_capability"))
    required = listed.get("required") if listed else None
    available = listed.get("available") if listed else None
    checks.append(
        _check(
            "tools_list_capability",
            isinstance(required, list)
            and bool(required)
            and isinstance(available, list)
            and set(required).issubset(available),
            None if listed is None else dict(listed),
            "tools_list_missing_or_unverified",
        )
    )

    by_name = {item.name: item for item in checks}
    missing = [item.name for item in checks if item.status != "pass"]
    return {
        "schema": SCHEMA,
        "descriptor_digest": _digest(descriptor),
        "ready": not missing,
        "status": "READY" if not missing else "BLOCKED",
        "checks": [by_name[name].to_dict() for name in CHECK_NAMES],
        "failed_or_unknown": missing,
    }


def evaluate_trial(trial: Mapping[str, Any], *, expected_tasks: int) -> dict[str, Any]:
    """Gate a completed comparison before savings are reported."""
    if not isinstance(trial, Mapping):
        raise TypeError("trial must be an object")
    reasons: list[str] = []
    completed = trial.get("completed_tasks")
    if completed != expected_tasks:
        reasons.append("incomplete_turns")
    if trial.get("quality_passed") is not True:
        reasons.append("quality_failed")
    if trial.get("baseline_completed_tasks") != trial.get("treatment_completed_tasks"):
        reasons.append("unequal_completed_work")
    if trial.get("priced") is not True or not trial.get("generation_id"):
        reasons.append("unpriced_generation")
    if trial.get("provider_expected") != trial.get("provider_observed"):
        reasons.append("provider_drift")
    if trial.get("route_expected") != trial.get("route_observed"):
        reasons.append("unknown_or_mismatched_route")
    timeout_output = trial.get("timeout_output_bytes")
    if isinstance(timeout_output, int) and timeout_output > int(trial.get("timeout_output_limit", 0) or 0):
        reasons.append("timeout_output_limit_exceeded")
    retries = trial.get("retries")
    if not isinstance(retries, list):
        retries = []
    return {
        "schema": "simplicio.benchmark-trial-validity/v1",
        "valid": not reasons,
        "status": "VALID" if not reasons else "INVALID",
        "reasons": reasons,
        "retries": retries,
        "quality_passed": trial.get("quality_passed") is True,
        "completed_tasks": completed,
    }


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("descriptor must be a JSON object")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="benchmark-preflight")
    parser.add_argument("descriptor", type=Path)
    parser.add_argument("--expected-tasks", type=int)
    parser.add_argument("--trial", type=Path, help="also evaluate a completed trial")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    descriptor = _load(args.descriptor)
    report = evaluate_preflight(descriptor, expected_tasks=args.expected_tasks)
    if args.trial:
        report["trial"] = evaluate_trial(_load(args.trial), expected_tasks=args.expected_tasks or descriptor.get("expected_tasks", 0))
        report["ready"] = report["ready"] and report["trial"]["valid"]
        report["status"] = "READY" if report["ready"] else "BLOCKED"
    print(json.dumps(report, ensure_ascii=False, sort_keys=True) if args.json else report["status"])
    return 0 if report["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
