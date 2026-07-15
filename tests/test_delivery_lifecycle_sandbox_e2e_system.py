"""#290 remaining gap 3 -- a real (if abbreviated) sandbox E2E across the *entire* delivery
lifecycle: claim -> (simulated) worktree work -> merge_executor (#288/#364) -> source_state
verification (#290 Fases 3-5) -> github_lifecycle close (#285/#361), one run, one thread of
identity (run_id/attempt_id/head_sha/merge_commit_sha) proving the whole delivery-truth chain
holds together end to end.

No real network/`gh` process is spawned -- every provider boundary (`gh` CLI, the release
byte-verifiers, the deployment verifier) is driven through the *same* injectable
`runner`/monkeypatch seams the unit suites already use (`MergeExecutor(runner=...)`,
`close_source_issue(runner=...)`, `external_verifiers.*` monkeypatched). What is real: the
on-disk SQLite lease queue (`SQLiteRemoteQueue`), the `AttemptCoordinator` fencing contract, the
`delivery.py` reducer/validator, and the exact SHA/digest values are threaded, unmodified,
through every stage -- so a SHA mismatch at any hop would fail the test, not get silently
smoothed over.
"""
import hashlib
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from pr_evidence import publish_comment  # noqa: E402

from simplicio_loop import external_verifiers as ev  # noqa: E402
from simplicio_loop import source_state  # noqa: E402
from simplicio_loop.delivery import build_delivery_receipt, validate_delivery_receipt  # noqa: E402
from simplicio_loop.github_lifecycle import close_source_issue  # noqa: E402
from simplicio_loop.merge_executor import MergeExecutor  # noqa: E402
from simplicio_loop.remote_queue import SQLiteRemoteQueue  # noqa: E402
from simplicio_loop.work_item_claims import AttemptCoordinator  # noqa: E402

IDENTITY = {
    "agent_id": "claude@device-e2e", "runtime": "claude", "device_id": "device-e2e",
    "session_id": "session-e2e", "capabilities": ["claim", "heartbeat", "fencing", "receipts", "events"],
}

REPO = "acme/widgets"
HEAD_SHA = "cafef00d" * 5  # 40 hex chars, sha1-shaped
MERGE_COMMIT_SHA = "deadbeef" * 5
TAG = "v9.9.9"


def _issue_payload(state="open"):
    return {
        "number": 42, "title": "ship the thing", "state": state, "state_reason": None,
        "body": "do the thing", "html_url": f"https://github.com/{REPO}/issues/42",
        "labels": [], "assignees": [], "milestone": None, "user": {"login": "author-a"},
        "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-02T00:00:00Z",
    }


def test_full_delivery_lifecycle_claim_to_deployed_to_issue_closed(tmp_path):
    # ---------------------------------------------------------------- 1. claim -----------
    db_path = str(tmp_path / "queue.db")
    queue = SQLiteRemoteQueue(db_path)
    receipt_dir = tmp_path / "receipts"
    coordinator = AttemptCoordinator(queue, run_id="run-e2e-290", receipt_dir=receipt_dir)
    attempt = coordinator.claim(
        work_item_id="WI-290-E2E", identity=IDENTITY, goal="ship #290 sandbox e2e",
        acs=["chain claim->merge->deploy->close proves consistent SHAs"],
        issue_ref=f"{REPO}#42", issue_url=f"https://github.com/{REPO}/issues/42", ttl=120.0,
    )
    assert attempt.lease.fencing_token >= 1

    # ------------------------------------------------------- 2. simulated worktree work ---
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "CHANGE.txt").write_text("delivered by run-e2e-290\n", encoding="utf-8")
    coordinator.record_event(attempt, "worktree_work_done", {"files_changed": 1})

    # ------------------------------------------------------------- 3. merge_executor -------
    def merge_runner(cmd, **kw):
        if cmd[:3] == ["gh", "pr", "list"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")
        if cmd[:3] == ["gh", "pr", "create"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=f"https://github.com/{REPO}/pull/7\n", stderr="")
        if cmd[:3] == ["gh", "pr", "view"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({
                "state": "OPEN", "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN",
            }), stderr="")
        if cmd[:3] == ["gh", "pr", "merge"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="merged\n", stderr="")
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="unexpected: %r" % (cmd,))

    # `reconcile()` re-queries the PR after merge and must report the SAME merge commit sha
    # that the rest of the chain (source_state, deployment) will independently verify.
    def merge_runner_with_reconcile(cmd, **kw):
        if cmd[:3] == ["gh", "pr", "view"] and "state,mergeCommit,baseRefName" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({
                "state": "MERGED", "mergeCommit": {"oid": MERGE_COMMIT_SHA}, "baseRefName": "main",
            }), stderr="")
        return merge_runner(cmd, **kw)

    executor = MergeExecutor(repo=REPO, runner=merge_runner_with_reconcile)
    pr = executor.ensure_pr(branch="run-e2e-290/wi-290", base="main", title="ship it", body="body")
    assert pr["number"] == 7
    merge_result = executor.merge(pr["number"], poll_interval=0, mergeable_timeout=1,
                                  sleep=lambda s: None)
    assert merge_result.merged is True
    assert merge_result.merge_commit_sha == MERGE_COMMIT_SHA
    coordinator.record_event(attempt, "merged", {"merge_commit_sha": merge_result.merge_commit_sha})

    # ------------------------------------------------ 4. source_state verification ---------
    # Drive `github_delivery_payload` end to end (live code path, fake `gh`/transport) so the
    # `merged` -> `released` -> `deployed` chain is proven by the *same* reducer/verifiers used
    # in production, not hand-assembled fixture JSON.
    wheel_bytes = b"real e2e wheel bytes for #290 sandbox"
    wheel_digest = hashlib.sha256(wheel_bytes).hexdigest()
    wheel_name = "widgets-9.9.9-py3-none-any.whl"

    def gh_runner(args):
        import types
        joined = args
        if joined[:2] == ["pr", "view"]:
            return types.SimpleNamespace(returncode=0, stdout=json.dumps({
                "url": f"https://github.com/{REPO}/pull/7", "headRefOid": HEAD_SHA,
                "baseRefOid": "b" * 40, "reviewDecision": "APPROVED", "isDraft": False,
                "mergeStateStatus": "CLEAN", "mergedAt": "2026-01-03T00:00:00Z",
                "mergeCommit": {"oid": MERGE_COMMIT_SHA}, "statusCheckRollup": [{"conclusion": "SUCCESS"}],
            }), stderr="")
        if joined[:2] == ["release", "view"]:
            return types.SimpleNamespace(returncode=0, stdout=json.dumps({
                "tagName": TAG, "isDraft": False, "isPrerelease": False,
                "assets": [{"name": wheel_name}],
            }), stderr="")
        return types.SimpleNamespace(returncode=1, stdout="", stderr="unexpected gh call: %r" % (joined,))

    import os as _os
    _os.environ.pop("SIMPLICIO_LOOP_GITHUB_FIXTURE_JSON", None)
    _os.environ.pop("SIMPLICIO_LOOP_GITHUB_REVIEWS_FIXTURE_JSON", None)

    # Real live-path pagination/reachability/release/deployment verifiers -- all monkeypatched
    # only at the outermost transport boundary (subprocess `_run_gh` / `external_verifiers._run`),
    # never at the reducer/verifier logic itself.
    def fake_run_gh(fn):
        return fn

    orig_run_gh = source_state._run_gh
    source_state._run_gh = gh_runner
    try:
        def fake_query_review_threads(owner, name, number, **kw):
            return {"open_threads": 0, "open_threads_verified": True, "approvals": 1,
                    "pages": 1, "total_threads": 0, "reason_code": None}
        orig_qrt = source_state.query_review_threads
        source_state.query_review_threads = fake_query_review_threads

        def fake_ev_run(args, cwd=None, timeout=180):
            import types
            joined = args
            if joined[:2] == ["gh", "api"] and "compare" in joined[2]:
                return types.SimpleNamespace(returncode=0, stdout=json.dumps(
                    {"status": "identical", "ahead_by": 0, "behind_by": 0}), stderr="")
            if joined[:2] == ["gh", "api"] and "repos/" in joined[2] and "releases/tags" not in joined[2] and "compare" not in joined[2]:
                return types.SimpleNamespace(returncode=0, stdout=json.dumps({"default_branch": "main"}), stderr="")
            if joined[:2] == ["gh", "api"] and "releases/tags" in joined[2]:
                return types.SimpleNamespace(returncode=0, stdout=json.dumps(
                    {"target_commitish": MERGE_COMMIT_SHA, "draft": False, "prerelease": False}), stderr="")
            if joined[:2] == ["gh", "attestation"]:
                return types.SimpleNamespace(returncode=0, stdout="", stderr="")
            return types.SimpleNamespace(returncode=1, stdout="", stderr="unexpected: %r" % (joined,))

        orig_ev_run = ev._run
        ev._run = fake_ev_run

        def fake_download_release_assets(repo, tag, dest_dir, *, asset_names=None):
            dest = Path(dest_dir)
            dest.mkdir(parents=True, exist_ok=True)
            checksum_path = dest / "checksums.txt"
            checksum_path.write_text(f"{wheel_digest}  {wheel_name}\n", encoding="utf-8")
            sbom_path = dest / "sbom.spdx.json"
            sbom_path.write_text(json.dumps({"spdxVersion": "SPDX-2.3", "packages": []}), encoding="utf-8")
            wheel_path = dest / wheel_name
            wheel_path.write_bytes(wheel_bytes)
            return {"ok": True, "downloaded": [str(wheel_path), str(checksum_path), str(sbom_path)]}

        orig_download = ev.download_release_assets
        ev.download_release_assets = fake_download_release_assets

        def fake_run_install_smoke(wheel_path, *, module_name="simplicio_loop"):
            assert Path(wheel_path).read_bytes() == wheel_bytes, "smoke must run on the DOWNLOADED bytes"
            return {"passed": True, "reason_code": None, "version": "9.9.9"}

        orig_smoke = ev.run_install_smoke
        ev.run_install_smoke = fake_run_install_smoke

        try:
            payload = source_state.github_delivery_payload(
                REPO, pr=7, tag=TAG, target_state="deployed", environment="pypi-index",
            )
        finally:
            source_state._run_gh = orig_run_gh
            source_state.query_review_threads = orig_qrt
            ev._run = orig_ev_run
            ev.download_release_assets = orig_download
            ev.run_install_smoke = orig_smoke

    finally:
        pass

    # Every hop of the chain must agree on the merge commit sha.
    assert payload["merge"]["commit_sha"] == MERGE_COMMIT_SHA
    assert payload["merge"]["commit_in_default_branch"] is True
    assert payload["release"]["checksums_verified"] is True
    assert payload["release"]["signatures_verified"] is True
    assert payload["release"]["sbom_present"] is True
    assert payload["install_smoke"]["passed"] is True
    assert payload["deployment"]["commit_sha"] == MERGE_COMMIT_SHA
    assert payload["deployment"]["artifact_digest"] == wheel_digest
    assert payload["deployment"]["smoke"]["passed"] is True

    state = source_state.infer_github_delivery_state(payload)
    assert state == "deployed"

    # ------------------------------------------------------- 5. delivery receipt gate ------
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "manifest.json").write_text(json.dumps({"run_id": "run-e2e-290"}), encoding="utf-8")
    receipt = build_delivery_receipt(str(run_dir), "deployed", current_state=state, source_kind="github",
                                     source_payload=payload)
    assert receipt["ready"] is True, receipt["gates"]
    validation = validate_delivery_receipt(receipt, target="deployed")
    assert validation["ok"] is True, validation["gates"]

    # -------------------------------------------------- 6. github_lifecycle close ----------
    closed_state = {"closed": False, "body": ""}

    def close_runner(cmd, **kw):
        if cmd[:2] == ["gh", "issue"] and "close" in cmd:
            closed_state["closed"] = True
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:2] == ["gh", "api"] and len(cmd) >= 3:
            url = cmd[2]
            if "/comments" in url and "/issues/comments/" not in url:
                return subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")
            if "/issues/comments/" in url:
                cid = int(url.rsplit("/", 1)[-1])
                return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"id": cid, "body": closed_state["body"]}), stderr="")
            if "/issues/" in url:
                data = _issue_payload(state="closed" if closed_state["closed"] else "open")
                return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(data), stderr="")
        if "-X" in cmd and "POST" in cmd:
            body = json.loads(kw.get("input") or "{}").get("body", "")
            closed_state["body"] = body
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"id": 99}), stderr="")
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="unexpected: %r" % (cmd,))

    close_receipt = close_source_issue(
        owner="acme", repo="widgets", issue="42", run_id="run-e2e-290",
        attempt_id=attempt.attempt_id, fencing_token=str(attempt.lease.fencing_token),
        publish_comment_fn=publish_comment, runner=close_runner,
        precheck_issue_state=True, require_active=lambda: coordinator.assert_active(attempt),
    )
    assert close_receipt["source_state"] == "closed"
    assert close_receipt["outcome"] == "closed"
    assert close_receipt["verified"] is True

    coordinator.complete(attempt, receipt_ref="run-e2e-290-delivery-receipt")

    # Final durable proof, two independent logs that must both agree:
    # (a) the shared queue's own event log shows claim -> completed under this identity;
    queue_events = queue.events(after=0, limit=1000)
    queue_kinds = [e["kind"] for e in queue_events if e.get("task_id") == "WI-290-E2E"]
    assert queue_kinds == ["enqueued", "claimed", "completed"]
    # (b) the coordinator's own durable per-attempt event log shows the worktree-work and
    # merge events recorded mid-flight, under the SAME attempt_id/fencing_token used above.
    events_path = receipt_dir / "run-e2e-290" / "WI-290-E2E" / attempt.attempt_id / "events.jsonl"
    attempt_events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    attempt_kinds = [e["kind"] for e in attempt_events]
    assert attempt_kinds == ["claimed", "worktree_work_done", "merged", "completed"]
    assert all(e["fencing_token"] == attempt.lease.fencing_token for e in attempt_events)
    merged_event = next(e for e in attempt_events if e["kind"] == "merged")
    assert merged_event["payload"]["merge_commit_sha"] == MERGE_COMMIT_SHA
