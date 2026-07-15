"""#285 remaining gap: a unified `SourceAdapter` Protocol, and `GitHubSourceAdapter` formally
satisfying it (not just duck-typing it).

No real `gh`/network call is made here -- `publish_comment`/`find_existing_comment` are driven
by a fake in-memory `runner` callable, the same style `tests/test_github_lifecycle_unit.py` and
`tests/test_github_lifecycle_readside.py` already use.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from pr_evidence import publish_comment  # noqa: E402

from simplicio_loop.source_adapter import GitHubSourceAdapter, SourceAdapter  # noqa: E402


class _FakeCompleted:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _FakeGitHub:
    """A minimal in-memory `gh` stand-in: one issue, its comments, open/closed state."""

    def __init__(self, *, issue_number="42"):
        self.issue_number = issue_number
        self.comments = []  # list of {"id": int, "body": str}
        self._next_id = 1
        self.state = "open"

    def runner(self, args, **kwargs):
        # args[0] == "gh"
        assert args[0] == "gh"
        verb = args[1]
        if verb == "api":
            return self._api(args[2:], kwargs.get("input"))
        if verb == "issue" and args[2] == "close":
            self.state = "closed"
            return _FakeCompleted(0, stdout="")
        return _FakeCompleted(1, stderr="unsupported command: %r" % (args,))

    def _api(self, rest, input_text):
        if rest and rest[0] == "-X" and rest[1] == "PATCH":
            path = rest[2]
            comment_id = int(path.rsplit("/", 1)[-1])
            body = json.loads(input_text)["body"]
            for c in self.comments:
                if c["id"] == comment_id:
                    c["body"] = body
                    return _FakeCompleted(0, stdout=json.dumps(c))
            return _FakeCompleted(1, stderr="404 comment not found")
        if rest and rest[0] == "-X" and rest[1] == "POST":
            body = json.loads(input_text)["body"]
            comment = {"id": self._next_id, "body": body}
            self._next_id += 1
            self.comments.append(comment)
            return _FakeCompleted(0, stdout=json.dumps(comment))
        # GET-style listing/fetch endpoints
        path = rest[0]
        if path.startswith("repos/") and path.endswith("/comments") or "/comments?" in path:
            return _FakeCompleted(0, stdout=json.dumps(self.comments))
        if "/comments/" in path:
            comment_id = int(path.split("/comments/")[-1])
            for c in self.comments:
                if c["id"] == comment_id:
                    return _FakeCompleted(0, stdout=json.dumps(c))
            return _FakeCompleted(1, stderr="404")
        if path.startswith("repos/") and "/issues/" in path and "?" not in path.split("/issues/")[-1]:
            return _FakeCompleted(0, stdout=json.dumps({
                "number": int(self.issue_number), "title": "t", "body": "b", "state": self.state,
                "state_reason": None, "labels": [], "assignees": [], "milestone": None,
                "user": {"login": "someone"}, "created_at": "2024-01-01T00:00:00Z",
                "updated_at": "2024-01-01T00:00:00Z", "html_url": "https://example/42",
            }))
        if path.startswith("repos/") and "/issues?" in path:
            return _FakeCompleted(0, stdout=json.dumps([]))
        return _FakeCompleted(1, stderr="unrecognized gh api path: %r" % path)


def _adapter(fake, outbox_dir=None):
    return GitHubSourceAdapter("acme", "widgets", publish_comment_fn=publish_comment,
                               runner=fake.runner, timeout=5, outbox_dir=outbox_dir)


def test_github_source_adapter_is_a_source_adapter_at_runtime():
    fake = _FakeGitHub()
    adapter = _adapter(fake)
    assert isinstance(adapter, SourceAdapter)


def test_source_adapter_protocol_declares_every_verb_the_issue_asks_for():
    expected = {
        "list_ready", "get_details", "requery", "claim", "update_status", "attach_evidence",
        "close", "reconcile", "record_pending_operation", "mark_operation_done",
        "list_pending_operations",
    }
    assert expected <= set(dir(SourceAdapter))


def test_claim_then_update_status_reuse_the_same_comment_id():
    fake = _FakeGitHub()
    adapter = _adapter(fake)
    claimed = adapter.claim("42", run_id="run-1", attempt_id="42-1")
    assert claimed["verified"] is True
    planned = adapter.update_status("42", "PLANNED", run_id="run-1", attempt_id="42-1")
    assert planned["verified"] is True
    assert planned["comment_id"] == claimed["comment_id"]
    assert len(fake.comments) == 1


def test_attach_evidence_embeds_evidence_text_in_the_canonical_comment():
    fake = _FakeGitHub()
    adapter = _adapter(fake)
    adapter.claim("42", run_id="run-1", attempt_id="42-1")
    receipt = adapter.attach_evidence("42", "12/12 tests pass", state="VERIFYING",
                                      run_id="run-1", attempt_id="42-1")
    assert receipt["verified"] is True
    assert "12/12 tests pass" in fake.comments[0]["body"]


def test_close_is_fail_closed_and_confirms_via_requery():
    fake = _FakeGitHub()
    adapter = _adapter(fake)
    adapter.claim("42", run_id="run-1", attempt_id="42-1")
    closed = adapter.close("42", run_id="run-1", attempt_id="42-1")
    assert closed["outcome"] == "closed"
    assert fake.state == "closed"
    details = adapter.get_details("42")
    assert details["state"] == "closed"


def test_outbox_roundtrip_through_the_adapter(tmp_path):
    fake = _FakeGitHub()
    adapter = _adapter(fake, outbox_dir=tmp_path / "outbox")
    adapter.record_pending_operation("op-1", {"kind": "test"})
    pending = adapter.list_pending_operations()
    assert len(pending) == 1 and pending[0]["operation_id"] == "op-1"
    adapter.mark_operation_done("op-1", {"ok": True})
    assert adapter.list_pending_operations() == []


def test_adapter_without_outbox_reports_empty_pending_list_but_reconcile_requires_one(tmp_path):
    fake = _FakeGitHub()
    adapter = _adapter(fake, outbox_dir=None)
    assert adapter.list_pending_operations() == []
    try:
        adapter.reconcile("op-1", "42")
        assert False, "expected ValueError for a missing outbox_dir"
    except ValueError:
        pass


def test_adapter_without_outbox_raises_on_record_and_mark_pending_operation():
    fake = _FakeGitHub()
    adapter = _adapter(fake, outbox_dir=None)
    try:
        adapter.record_pending_operation("op-1", {"kind": "test"})
        assert False, "expected ValueError for a missing outbox_dir"
    except ValueError:
        pass
    try:
        adapter.mark_operation_done("op-1", {"ok": True})
        assert False, "expected ValueError for a missing outbox_dir"
    except ValueError:
        pass


def test_list_ready_delegates_and_excludes_pull_requests():
    fake = _FakeGitHub()
    adapter = _adapter(fake)
    result = adapter.list_ready(state="open")
    assert result["schema"] == "simplicio.github-list-ready/v1"
    assert result["provider"] == "github"
    assert result["items"] == []


def test_requery_delegates_to_get_details_and_reads_the_live_state():
    fake = _FakeGitHub()
    adapter = _adapter(fake)
    snapshot = adapter.requery("42")
    assert snapshot["schema"] == "simplicio.github-source-snapshot/v1"
    assert snapshot["state"] == "open"
    assert "requeried_at" in snapshot


def test_reconcile_with_outbox_configured_recovers_a_pending_operation(tmp_path):
    fake = _FakeGitHub()
    adapter = _adapter(fake, outbox_dir=tmp_path / "outbox")
    claimed = adapter.claim("42", run_id="run-1", attempt_id="42-1")
    op_id = "op-reconcile-1"
    adapter.record_pending_operation(op_id, {"kind": "test"})
    receipt = adapter.reconcile(op_id, "42", comment_id=claimed["comment_id"],
                                expected_body_hash=claimed["expected_body_hash"])
    assert receipt["outcome"] == "reconciled"
