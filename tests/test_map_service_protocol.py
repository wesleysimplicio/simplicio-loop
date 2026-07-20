from simplicio_loop.map_service_protocol import (
    MapProtocolError, failure, negotiate, success, validate_request,
)


def test_all_operations_validate_their_required_fields():
    cases = {
        "resolve_repo": {"path": "/repo"},
        "get_view": {"cache_key": "view-1"},
        "build_canonical": {"identity_key": "repo-1", "tree_hash": "sha"},
        "build_overlay": {"identity_key": "repo-1", "tree_hash": "sha", "dirty_files": []},
        "subscribe": {"identity_key": "repo-1"},
        "invalidate": {"identity_key": "repo-1"},
        "release": {"cache_key": "view-1"},
        "gc": {},
    }
    for operation, payload in cases.items():
        assert validate_request(operation, payload) == payload
        assert success(operation, {"accepted": True})["ok"] is True


def test_incompatible_version_fails_closed_without_downgrade():
    try:
        negotiate(2)
    except MapProtocolError as error:
        assert error.code == "unsupported_version"
        assert failure(error)["ok"] is False
    else:
        raise AssertionError("version mismatch was accepted")


def test_invalid_operation_and_missing_field_have_typed_errors():
    for operation, payload, code in (
        ("not-real", {}, "unknown_operation"),
        ("resolve_repo", {}, "missing_field"),
        ("build_overlay", {"identity_key": "x", "tree_hash": "x", "dirty_files": "bad"}, "invalid_field"),
    ):
        try:
            validate_request(operation, payload)
        except MapProtocolError as error:
            assert error.code == code
        else:
            raise AssertionError("invalid request was accepted")
