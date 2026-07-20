from simplicio_loop.map_service_invalidation import InvalidationCoordinator, StaleViewError


def test_commit_config_and_schema_changes_invalidate_and_notify():
    events = []
    coordinator = InvalidationCoordinator()
    coordinator.register(identity_key="repo", cache_key="view", commit="one", mapper_config={"v": 1}, schema="map/v1", payload={"files": ["a.py"]})
    coordinator.subscribe("repo", events.append)
    emitted = coordinator.refresh(identity_key="repo", commit="two", mapper_config={"v": 2}, schema="map/v2")
    assert emitted[0]["reason"] == "commit+config+schema"
    assert events == emitted
    try:
        coordinator.get("view")
    except StaleViewError:
        pass
    else:
        raise AssertionError("stale view was served under strict policy")


def test_stale_while_revalidate_requires_explicit_marker_and_permission():
    coordinator = InvalidationCoordinator(staleness="stale-while-revalidate")
    coordinator.register(identity_key="repo", cache_key="view", commit="one", mapper_config={}, schema="map/v1", payload={"content": "old"})
    coordinator.refresh(identity_key="repo", commit="two", mapper_config={}, schema="map/v1")
    try:
        coordinator.get("view")
    except StaleViewError:
        pass
    else:
        raise AssertionError("stale view was served without explicit permission")
    served = coordinator.get("view", allow_stale=True)
    assert served["stale"] is True
    assert served["stale_reason"] == "commit"
