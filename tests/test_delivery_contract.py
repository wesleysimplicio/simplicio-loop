from simplicio_loop.delivery_contract import DELIVERY_CONTRACT_SCHEMA, enforce_diff_contract, normalize_contract, validate_contract


def contract(**overrides):
    return normalize_contract(overrides)


def test_contract_rejects_unknown_fields_and_wrong_types():
    verdict = validate_contract({"schema": DELIVERY_CONTRACT_SCHEMA, "open_pr": "yes"})
    assert verdict["ok"] is False
    assert any("unknown" in item or "missing" in item or "open_pr" in item for item in verdict["errors"])


def test_defaults_are_explicit_and_normalized():
    result = contract()
    assert result["open_pr"] is True
    assert result["allow_new_files_in_repo"] is True


def test_new_file_guard_is_fail_closed():
    result = enforce_diff_contract(changed_paths=["+FooTests.cs"], added_lines=[], contract=contract(allow_new_files_in_repo=False))
    assert result["ok"] is False
    assert "new files" in result["errors"][0]


def test_comment_guard_rejects_supported_comment_prefixes():
    result = enforce_diff_contract(changed_paths=["src/a.py"], added_lines=["# new comment"], contract=contract(allow_comments_in_code=False))
    assert result["ok"] is False


def test_clean_diff_passes():
    result = enforce_diff_contract(changed_paths=["src/a.py"], added_lines=["value = 1"], contract=contract())
    assert result["ok"] is True
