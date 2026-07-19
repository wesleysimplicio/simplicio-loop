from simplicio_loop.map_service_repository_watchers import RepositoryWatcherHub


def test_one_repository_watcher_coalesces_events_for_many_clients():
    events = []
    hub = RepositoryWatcherHub(debounce_seconds=10)
    hub.subscribe("repo", "worktree-a", events.append)
    hub.subscribe("repo", "worktree-b", events.append)
    hub.emit("repo", "worktree-a", ["a.py"])
    hub.emit("repo", "worktree-b", ["b.py", "a.py"])
    assert hub.status()["repositories"] == 1
    assert hub.status()["pending"] == 1
    emitted = hub.flush(force=True)
    assert len(emitted) == 2
    assert emitted[0]["paths"] == ["a.py", "b.py"]
    assert emitted[0]["identity_keys"] == ["worktree-a", "worktree-b"]


def test_transition_is_detected_and_coalesced():
    events = []
    hub = RepositoryWatcherHub()
    hub.subscribe("repo", "worktree", events.append)
    assert hub.observe_transition("repo", "worktree", head="one", branch="main") is False
    assert hub.observe_transition("repo", "worktree", head="two", branch="main") is True
    payload = hub.flush(force=True)[0]
    assert payload["reason"] == "branch_transition"
    assert ".git/HEAD" in payload["paths"]


def test_active_worktree_is_flushed_before_background_work_without_starvation():
    seen = []
    hub = RepositoryWatcherHub(debounce_seconds=0)
    hub.subscribe("repo-a", "active", lambda event: seen.append(("active", event["sequence"])))
    hub.subscribe("repo-b", "background", lambda event: seen.append(("background", event["sequence"])))
    hub.emit("repo-b", "background", ["b.py"])
    hub.emit("repo-a", "active", ["a.py"], active=True)
    hub.flush(force=True)
    assert [name for name, _sequence in seen] == ["active", "background"]
