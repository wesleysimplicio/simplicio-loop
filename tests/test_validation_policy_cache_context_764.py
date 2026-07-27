from simplicio_loop.validation_policy import (
    ValidationCandidate,
    ValidationInputs,
    ValidationPolicy,
)


def _inputs(cache_context):
    return ValidationInputs(
        phase="edit",
        candidates=(ValidationCandidate(name="test:unit", tier="focused"),),
        cache_context=tuple(sorted(cache_context.items())),
    )


def test_cache_requires_all_execution_context_hashes():
    receipt = ValidationPolicy().decide(_inputs({"source_hash": "source"}))
    assert receipt.cache_allowed is False
    assert "CACHE_CONTEXT_INCOMPLETE" in receipt.reason_codes


def test_cache_key_changes_when_test_or_environment_hash_changes():
    context = {
        "source_hash": "source",
        "test_hash": "tests-1",
        "dependency_hash": "deps",
        "environment_hash": "env-1",
        "command_hash": "command",
    }
    first = ValidationPolicy().decide(_inputs(context))
    changed = dict(context, environment_hash="env-2")
    second = ValidationPolicy().decide(_inputs(changed))
    assert first.cache_allowed is True
    assert second.cache_allowed is True
    assert first.cache_key != second.cache_key
    assert first.as_dict()["cache_context"]["environment_hash"] == "env-1"
