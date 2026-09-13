"""Unit coverage for the governed OpenRouter -> deterministic plan boundary."""

from __future__ import annotations

import io
import json

import pytest

from simplicio_loop.openrouter_operator import (
    OpenRouterPlanError,
    external_preflight_admissible,
    request_mechanical_plan,
)


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _task(task_type: str = "criação") -> dict:
    return {
        "id": "TASK-CHECKERS-001",
        "identity": {"id": "TASK-CHECKERS-001", "type": task_type},
        "original_text": "Criar um jogo de dama funcional em site/checkers.html.",
    }


def _response(content: str) -> _Response:
    body = {
        "id": "gen-test",
        "choices": [{"finish_reason": "stop", "message": {"content": content}}],
        "usage": {
            "prompt_tokens": 11,
            "completion_tokens": 22,
            "total_tokens": 33,
            "prompt_tokens_details": {"cached_tokens": 4},
            "completion_tokens_details": {"reasoning_tokens": 3},
            "cost": 0.0001,
        },
    }
    return _Response(json.dumps(body).encode("utf-8"))


def _env(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-secret-that-must-not-be-persisted")
    monkeypatch.setenv("OPENROUTER_BASE_URL", "https://openrouter.example/api/v1")
    monkeypatch.setenv("SIMPLICIO_MODEL", "deepseek/deepseek-v4.1-flash")


def test_creation_proposal_is_compiled_and_receipt_is_secret_free(monkeypatch, tmp_path):
    _env(monkeypatch)
    html = "<!doctype html><html><body><main>Sim</main><script>start()</script></body></html>"
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda request, timeout: _response(json.dumps({"files": {"site/checkers.html": html}})),
    )

    plan, receipt = request_mechanical_plan(
        task=_task(), target="site/checkers.html", repo_path=tmp_path,
        mapper_context={"schema": "mapper", "foreground_generation": {"id": "g1"}},
        run_id="run-1", task_index=1, attempt=1,
    )

    assert plan["schema"] == "simplicio.mechanical-edit/v1"
    assert plan["operations"][0]["op"] == "create_file"
    assert plan["operations"][0]["path"] == "site/checkers.html"
    assert receipt["status"] == "validated"
    assert receipt["model"] == "deepseek/deepseek-v4.1-flash"
    assert receipt["usage"]["input_tokens"] == 11
    assert receipt["usage"]["reasoning_tokens"] == 3
    assert receipt["usage"]["cache_hit"] is None
    assert "test-secret-that-must-not-be-persisted" not in json.dumps(receipt)


def test_edit_proposal_is_bound_to_the_current_file_hash(monkeypatch, tmp_path):
    _env(monkeypatch)
    target = tmp_path / "site" / "checkers.html"
    target.parent.mkdir()
    current = "<!doctype html>\n<html><body><script>start()</script></body></html>\n"
    target.write_text(current, encoding="utf-8")
    edited = "<!doctype html>\n<html><body><h1>Score</h1><script>start();reset()</script></body></html>\n"
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda request, timeout: _response(json.dumps({"files": {"site/checkers.html": edited}})),
    )

    plan, _receipt = request_mechanical_plan(
        task=_task("edição"), target="site/checkers.html", repo_path=tmp_path,
        mapper_context={"schema": "mapper"}, run_id="run-2", task_index=2, attempt=1,
    )

    operation = plan["operations"][0]
    assert operation["op"] == "replace_range"
    assert operation["start_line"] == 1
    assert operation["end_line"] == len(current.splitlines())
    assert operation["file_sha256"]


def test_edit_proposal_cannot_be_a_byte_identical_noop(monkeypatch, tmp_path):
    _env(monkeypatch)
    target = tmp_path / "site" / "checkers.html"
    target.parent.mkdir()
    current = "<!doctype html>\n<html><body><script>start()</script></body></html>\n"
    target.write_text(current, encoding="utf-8")
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda request, timeout: _response(json.dumps({"files": {"site/checkers.html": current}})),
    )

    with pytest.raises(OpenRouterPlanError) as error:
        request_mechanical_plan(
            task=_task("edição"), target="site/checkers.html", repo_path=tmp_path,
            mapper_context={"schema": "mapper"}, run_id="run-noop", task_index=2, attempt=1,
        )

    assert error.value.receipt["status"] == "proposal_rejected"
    assert error.value.receipt["error_code"] == "ValueError"


def test_invalid_target_is_rejected_before_provider_request(monkeypatch, tmp_path):
    _env(monkeypatch)
    called = []
    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: called.append(True))

    with pytest.raises(OpenRouterPlanError) as error:
        request_mechanical_plan(
            task=_task(), target="../outside.html", repo_path=tmp_path,
            mapper_context={}, run_id="run-3", task_index=1, attempt=1,
        )

    assert called == []
    assert error.value.receipt["status"] == "proposal_rejected"
    assert error.value.receipt["request_sent"] is False


def test_external_preflight_accepts_only_explicit_dev_cli_llm_block(monkeypatch):
    _env(monkeypatch)
    receipt = {
        "execution_state": "blocked",
        "returncode": 1,
        "stdout": {"blocked_preconditions": [{"code": "llm_execution_disabled"}]},
    }
    assert external_preflight_admissible(receipt) is True
    assert external_preflight_admissible({**receipt, "stdout": {"blocked_preconditions": []}}) is False
