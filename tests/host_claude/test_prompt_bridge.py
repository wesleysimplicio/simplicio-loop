from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import simplicio_loop.prompt_bridge as bridge
from jsonschema import Draft7Validator
from simplicio_loop.route_decision import POLICY_VERSION, validate_route_decision


@dataclass
class Completed:
    returncode: int
    stdout: str
    stderr: str = ""


def _runtime_route(handles: list[str], *, max_bytes: int = 4096) -> dict[str, object]:
    return {
        "schema": bridge.ROUTE_SCHEMA,
        "decision_id": "runtime-route:test",
        "lane": "standard",
        "reason": "runtime-owned deterministic route",
        "capability": "prompt.enrich",
        "intent": "mutate",
        "selected_handles": handles,
        "max_skills": 8,
        "max_bytes": max_bytes,
        "runtime_status": "available",
        "authority": dict(bridge.AUTHORITY_LOCKED),
        "provenance": {"producer": "simplicio-runtime"},
    }


def _handles_from_argv(argv: list[str]) -> list[str]:
    return [
        argv[index + 1]
        for index, item in enumerate(argv[:-1])
        if item == "--selected-handle"
    ]


def test_runtime_success_uses_direct_argv_and_materializes_selected_skills(tmp_path: Path):
    calls: list[tuple[list[str], float]] = []

    def runner(argv: list[str], timeout: float, env):
        calls.append((argv, timeout))
        handles = _handles_from_argv(argv)
        payload = {"prompt_route": _runtime_route(handles)}
        return Completed(0, json.dumps(payload))

    result = bridge.enrich_user_prompt(
        "implement the roster fix",
        session_id="session-1",
        repo=tmp_path,
        env={
            "SIMPLICIO_RUNTIME_BIN": "/opt/simplicio",
            "SIMPLICIO_PROMPT_MAX_BYTES": "4096",
        },
        runner=runner,
        body_loader=lambda handle: f"body for {handle}",
    )

    assert calls
    argv, timeout = calls[0]
    assert isinstance(argv, list)
    assert argv[:4] == ["/opt/simplicio", "loop", "decide", "--task"]
    assert "--prompt-route" in argv
    assert "--no-write" in argv
    assert "--selected-handle" in argv
    assert timeout <= 10
    assert result["route"]["runtime_status"] == "available"
    assert result["route_decision"] == result["route"]
    assert result["route_decision"]["policy_version"] == POLICY_VERSION
    validate_route_decision(result["route_decision"])
    assert result["receipt"]["fallback"]["used"] is False
    assert result["receipt"]["authority"] == {"writes": False, "effects": False}
    assert result["receipt"]["materialized_handles"]
    assert "## Simplicio skill:" in result["additional_context"]
    assert bridge.RECEIPT_SCHEMA in result["additional_context"]


def test_runtime_absence_is_visible_and_portable_route_still_enriches():
    result = bridge.enrich_user_prompt(
        "implement the parser",
        session_id="session-degraded",
        env={"SIMPLICIO_RUNTIME_AVAILABLE": "0"},
        body_loader=lambda handle: f"fallback body for {handle}",
    )

    assert result["route"]["schema"] == bridge.ROUTE_SCHEMA
    assert result["route"]["runtime_status"] == "unavailable"
    assert result["route"]["intent"] == "mutate"
    assert result["route_decision"]["decision_id"].startswith("loop-route/")
    assert result["receipt"]["fallback"]["used"] is True
    assert result["receipt"]["fallback"]["reason_code"] == "runtime_declared_unavailable"
    assert "simplicio-dev-cli" in result["receipt"]["selected_handles"]
    assert result["receipt"]["materialized_handles"]


def test_malformed_runtime_output_never_claims_runtime_success():
    def runner(argv: list[str], timeout: float, env):
        return Completed(0, "not-json")

    result = bridge.enrich_user_prompt(
        "survey the repository",
        env={"SIMPLICIO_RUNTIME_AVAILABLE": "1"},
        runner=runner,
        body_loader=lambda handle: "read only skill",
    )

    assert result["receipt"]["fallback"]["used"] is True
    assert result["receipt"]["fallback"]["reason_code"] == "runtime_output_not_json"
    assert result["route"]["runtime_status"] == "unavailable"
    assert result["route_decision"]["reason"] == "runtime_output_not_json"


def test_existing_runtime_route_environment_avoids_process_call():
    route = _runtime_route(["simplicio-orient", "simplicio-mapper"])

    def runner(argv: list[str], timeout: float, env):
        raise AssertionError("runtime process must not run when route is already supplied")

    result = bridge.enrich_user_prompt(
        "survey repository",
        env={"SIMPLICIO_ROUTE_DECISION": json.dumps(route)},
        runner=runner,
        body_loader=lambda handle: "skill",
    )

    assert result["receipt"]["fallback"]["used"] is False
    assert result["receipt"]["runtime"]["source"] == "env:SIMPLICIO_ROUTE_DECISION"
    assert result["route"]["decision_id"].startswith("loop-route/")
    assert result["route"]["provenance"]["source_decision_id"] == "runtime-route:test"


def test_route_decision_is_stable_and_distinguishes_read_from_mutation():
    read_one = bridge.enrich_user_prompt(
        "survey repository",
        env={"SIMPLICIO_RUNTIME_AVAILABLE": "0"},
        body_loader=lambda handle: "skill",
    )["route_decision"]
    read_two = bridge.enrich_user_prompt(
        "survey repository",
        env={"SIMPLICIO_RUNTIME_AVAILABLE": "0"},
        body_loader=lambda handle: "skill",
    )["route_decision"]
    mutation = bridge.enrich_user_prompt(
        "implement the parser",
        env={"SIMPLICIO_RUNTIME_AVAILABLE": "0"},
        body_loader=lambda handle: "skill",
    )["route_decision"]

    assert read_one == read_two
    assert read_one["intent"] == "survey"
    assert read_one["lane"] == "interactive"
    assert mutation["intent"] == "mutate"
    assert mutation["lane"] == "standard"
    assert read_one["decision_id"] != mutation["decision_id"]
    assert read_one["authority"] == {"writes": False, "effects": False}


def test_route_decision_matches_published_contract():
    result = bridge.enrich_user_prompt(
        "implement the parser",
        env={"SIMPLICIO_RUNTIME_AVAILABLE": "0"},
        body_loader=lambda handle: "skill",
    )
    schema_path = Path(__file__).resolve().parents[2] / "contracts" / "runtime-routing" / "v1" / "route-decision.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft7Validator.check_schema(schema)
    Draft7Validator(schema).validate(result["route_decision"])


def test_skill_materialization_honors_strict_byte_budget():
    result = bridge.enrich_user_prompt(
        "implement the large feature",
        env={
            "SIMPLICIO_RUNTIME_AVAILABLE": "0",
            "SIMPLICIO_PROMPT_MAX_BYTES": "1024",
        },
        body_loader=lambda handle: "x" * 20_000,
    )

    context = result["additional_context"].split(f"<!-- {bridge.RECEIPT_SCHEMA}", 1)[0].rstrip()
    assert len(context.encode("utf-8")) <= 1024
    assert result["receipt"]["context_bytes"] <= 1024
    assert result["receipt"]["context_truncated"] is True


def test_runtime_route_rejects_unlocked_authority():
    route = _runtime_route(["simplicio-mapper"])
    route["authority"] = {"writes": True, "effects": False}

    def runner(argv: list[str], timeout: float, env):
        return Completed(0, json.dumps({"prompt_route": route}))

    result = bridge.enrich_user_prompt(
        "survey repository",
        env={"SIMPLICIO_RUNTIME_AVAILABLE": "1"},
        runner=runner,
        body_loader=lambda handle: "skill",
    )

    assert result["receipt"]["fallback"]["used"] is True
    assert result["receipt"]["fallback"]["reason_code"] == "route_authority_not_locked"
    assert result["route"]["authority"] == {"writes": False, "effects": False}


def test_prompt_package_estimator_is_used_when_available(monkeypatch):
    class Estimate:
        count = 7
        encoding = "test-bpe"
        source = "tiktoken"
        fallback_reason = None

    monkeypatch.setattr(bridge, "_prompt_estimate_text", lambda text, enabled=True: Estimate())
    monkeypatch.setattr(bridge, "_prompt_version", lambda: "1.14.3")

    result = bridge.enrich_user_prompt(
        "survey repository",
        env={"SIMPLICIO_RUNTIME_AVAILABLE": "0"},
        body_loader=lambda handle: "skill",
    )

    before = result["receipt"]["token_estimator"]["before"]
    assert before == {
        "count": 7,
        "encoding": "test-bpe",
        "source": "simplicio-prompt/1.14.3:tiktoken",
    }


def test_default_package_skill_cache_is_bounded_and_reports_hits():
    bridge.reset_cache()
    first = bridge.enrich_user_prompt(
        "survey repository",
        session_id="cache-session",
        env={"SIMPLICIO_RUNTIME_AVAILABLE": "0"},
    )
    second = bridge.enrich_user_prompt(
        "survey repository",
        session_id="cache-session",
        env={"SIMPLICIO_RUNTIME_AVAILABLE": "0"},
    )

    assert first["receipt"]["cache"]["hit"] is False
    assert second["receipt"]["cache"]["hit"] is True
    assert second["receipt"]["selected_digests"] == first["receipt"]["selected_digests"]
