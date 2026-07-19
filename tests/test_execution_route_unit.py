import json

import pytest

from simplicio_loop.execution_route import (
    SCHEMA,
    ExecutionRoute,
    ExecutionRouteError,
    build_execution_route,
    decide_route,
    read_routes,
    record_route,
    verify_route_hash,
)


def test_worker_route_for_mechanical_task_with_worker_available():
    route = decide_route("mapear o repositorio e coletar CI", has_deterministic_worker=True, is_ambiguous=False)
    assert route.route == "worker"
    assert route.backend == "deterministic-worker"
    assert route.schema == SCHEMA
    assert route.confidence > 0.5


def test_worker_route_for_test_task():
    route = decide_route("run the test suite and validar schema", has_deterministic_worker=True, is_ambiguous=False)
    assert route.route == "worker"


def test_hybrid_route_when_mechanical_but_no_worker_available():
    route = decide_route("aplicar edicao mecanica no arquivo", has_deterministic_worker=False, is_ambiguous=False)
    assert route.route == "hybrid"


def test_agent_route_forced_by_ambiguous_flag_even_with_worker_keyword():
    route = decide_route("mapear objetivo ambiguo do usuario", has_deterministic_worker=True, is_ambiguous=True)
    assert route.route == "agent"
    assert route.backend == "llm"


def test_agent_route_for_plan_synthesis():
    route = decide_route("sintetizar plano de recuperacao", has_deterministic_worker=True, is_ambiguous=True)
    assert route.route == "agent"


def test_agent_route_default_for_unrecognized_task():
    route = decide_route("do something nobody described before", has_deterministic_worker=True, is_ambiguous=False)
    assert route.route == "agent"
    assert route.confidence < 0.5


def test_build_execution_route_rejects_bad_route():
    with pytest.raises(ExecutionRouteError):
        build_execution_route(route="bogus", reason="x", confidence=0.5)


def test_build_execution_route_rejects_bad_confidence():
    with pytest.raises(ExecutionRouteError):
        build_execution_route(route="worker", reason="x", confidence=1.5)


def test_build_execution_route_rejects_negative_tokens():
    with pytest.raises(ExecutionRouteError):
        build_execution_route(route="worker", reason="x", confidence=0.5, tokens_spent=-1)


def test_build_execution_route_requires_reason():
    with pytest.raises(ExecutionRouteError):
        build_execution_route(route="worker", reason="   ", confidence=0.5)


def test_receipt_has_stable_hash():
    route = decide_route("dedup entries", has_deterministic_worker=True, is_ambiguous=False)
    assert route.receipt_sha
    assert verify_route_hash(route.to_dict())


def test_journal_round_trip(tmp_path):
    path = tmp_path / "execution_route.jsonl"
    route1 = decide_route("mapear repositorio", has_deterministic_worker=True, is_ambiguous=False)
    route2 = decide_route("investigar falha nova no build", has_deterministic_worker=True, is_ambiguous=True)

    record_route(route1, str(path))
    record_route(route2, str(path))

    records = read_routes(str(path))
    assert len(records) == 2
    assert records[0]["route"] == "worker"
    assert records[1]["route"] == "agent"
    assert all(verify_route_hash(r) for r in records)

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    for line in lines:
        parsed = json.loads(line)
        assert parsed["schema"] == SCHEMA


def test_journal_appends_without_truncating(tmp_path):
    path = tmp_path / "nested" / "dir" / "route.jsonl"
    route = decide_route("test suite", has_deterministic_worker=True, is_ambiguous=False)
    record_route(route, str(path))
    record_route(route, str(path))
    assert len(read_routes(str(path))) == 2


def test_read_routes_missing_file_returns_empty(tmp_path):
    assert read_routes(str(tmp_path / "absent.jsonl")) == []


def test_record_route_rejects_non_execution_route(tmp_path):
    with pytest.raises(ExecutionRouteError):
        record_route({"not": "a route"}, str(tmp_path / "x.jsonl"))
