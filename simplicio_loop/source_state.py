from __future__ import annotations

import json
import os
import subprocess
from typing import Any, Dict


def _run_gh(args):
    return subprocess.run(["gh"] + list(args), capture_output=True, text=True, timeout=60)


def _fixture_payload(name: str) -> Dict[str, Any] | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    return json.loads(raw)


# #290 Fase 2.4 — "Paginar todas as review threads via GraphQL até hasNextPage=false" +
# "Complete pagination" invariant: a paginated query may only declare absence after recording
# has_next_page=false. This replaces the historical `open_threads: 0` default with a real,
# paginated, live GraphQL query — no page is skipped, and any failure/timeout/malformed page
# leaves `open_threads_verified=False` with a stable reason code instead of a favorable default.
_REVIEW_THREADS_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $cursor) {
        totalCount
        pageInfo { hasNextPage endCursor }
        nodes { isResolved }
      }
      reviews(states: APPROVED, first: 1) {
        totalCount
      }
    }
  }
}
"""

_MAX_REVIEW_THREAD_PAGES = 25


def query_review_threads(owner: str, name: str, number: int, *, max_pages: int = _MAX_REVIEW_THREAD_PAGES) -> Dict[str, Any]:
    """Live, paginated GraphQL review-thread + approved-review-count query.

    Returns a dict with `open_threads`, `open_threads_verified`, `approvals` (or None if it
    could not be read), `pages`, `total_threads` and, on any non-success path, `reason_code`.
    Never returns `open_threads_verified=True` unless a page was actually observed with
    `hasNextPage=false` — "unknown is not pass".
    """
    cursor: str | None = None
    unresolved = 0
    total_seen = 0
    approvals: int | None = None
    for page_num in range(1, max_pages + 1):
        args = [
            "api", "graphql",
            "-f", f"query={_REVIEW_THREADS_QUERY}",
            "-f", f"owner={owner}",
            "-f", f"name={name}",
            "-F", f"number={number}",
        ]
        if cursor:
            args += ["-f", f"cursor={cursor}"]
        try:
            result = _run_gh(args)
        except (subprocess.SubprocessError, OSError):
            return {
                "open_threads": unresolved, "open_threads_verified": False,
                "reason_code": "review_threads_query_failed", "pages": page_num,
                "approvals": approvals,
            }
        if result.returncode != 0:
            return {
                "open_threads": unresolved, "open_threads_verified": False,
                "reason_code": "review_threads_query_failed", "pages": page_num,
                "approvals": approvals,
            }
        try:
            data = json.loads(result.stdout or "{}")
            pr_node = data["data"]["repository"]["pullRequest"]
            threads = pr_node["reviewThreads"]
            nodes = threads["nodes"]
            page_info = threads["pageInfo"]
            if approvals is None:
                approvals = int((pr_node.get("reviews") or {}).get("totalCount", 0))
        except (KeyError, TypeError, ValueError):
            return {
                "open_threads": unresolved, "open_threads_verified": False,
                "reason_code": "review_threads_response_malformed", "pages": page_num,
                "approvals": approvals,
            }
        unresolved += sum(1 for node in nodes if not (node or {}).get("isResolved"))
        total_seen += len(nodes)
        has_next = bool(page_info.get("hasNextPage"))
        cursor = page_info.get("endCursor")
        if not has_next:
            return {
                "open_threads": unresolved, "open_threads_verified": True,
                "reason_code": None, "pages": page_num,
                "approvals": approvals, "total_threads": total_seen,
            }
    # #290 invariant: an incomplete pagination (page budget exhausted while hasNextPage was
    # still true) must never be reported as verified-clear.
    return {
        "open_threads": unresolved, "open_threads_verified": False,
        "reason_code": "pagination_incomplete", "pages": max_pages,
        "approvals": approvals,
    }


def infer_github_delivery_state(payload: Dict[str, Any]) -> str:
    # #290 — "unknown is not pass": a field is only allowed to promote the inferred state when
    # it carries an explicit `*_verified` (or equivalent real-check) marker set True by an actual
    # query. A merely-present boolean that was fabricated by the adapter for convenience (the
    # historical bug this closes) must never be trusted here.
    deployment = payload.get("deployment") or {}
    if deployment.get("environment") and (deployment.get("smoke") or {}).get("passed"):
        return "deployed"

    release = payload.get("release") or {}
    install_smoke = payload.get("install_smoke") or {}
    if (
        release.get("tag")
        and release.get("assets")
        and release.get("checksums_verified")
        and release.get("signatures_verified")
        and release.get("sbom_present")
        and install_smoke.get("passed")
    ):
        return "released"

    merge = payload.get("merge") or {}
    if merge.get("commit_sha") and merge.get("merged_at") and merge.get("commit_in_default_branch"):
        return "merged"

    pr = payload.get("pr") or {}
    if pr.get("url") and pr.get("head_sha") and pr.get("base_sha"):
        reviews = payload.get("reviews") or {}
        checks = payload.get("checks") or {}
        branch = payload.get("branch") or {}
        if (
            checks.get("green")
            and int(reviews.get("approvals", 0)) >= 1
            and int(reviews.get("open_threads", 1)) == 0
            and bool(reviews.get("open_threads_verified"))
            and branch.get("up_to_date")
        ):
            return "merge-ready"
        return "pr-open"

    return "verified"


def _should_verify_branch_reachability(target_state: str, verify_reachability: bool | None) -> bool:
    if verify_reachability is not None:
        return verify_reachability
    override = os.environ.get("SIMPLICIO_LOOP_VERIFY_BRANCH_REACHABILITY", "").strip().lower()
    if override:
        return override not in ("0", "false", "no")
    # #290 Fase 3 — only pay the two extra live `gh api` calls (default-branch discovery +
    # compare) when the caller is actually trying to promote to `merged` or beyond; earlier
    # targets (pr-open, merge-ready) never need proof the commit already reached the
    # default branch.
    return target_state in ("merged", "released", "deployed")


def _should_verify_release_artifacts(target_state: str, verify_artifacts: bool | None) -> bool:
    if verify_artifacts is not None:
        return verify_artifacts
    override = os.environ.get("SIMPLICIO_LOOP_VERIFY_RELEASE_ARTIFACTS", "").strip().lower()
    if override:
        return override not in ("0", "false", "no")
    # #290 Fase 4 — only pay the download/attestation/install-smoke cost when the caller is
    # actually trying to promote to `released`/`deployed`; other targets (pr-open, merge-ready)
    # never need it and must stay fast.
    return target_state in ("released", "deployed")


def _should_verify_deployment(target_state: str, verify_deployment: bool | None) -> bool:
    if verify_deployment is not None:
        return verify_deployment
    override = os.environ.get("SIMPLICIO_LOOP_VERIFY_DEPLOYMENT", "").strip().lower()
    if override:
        return override not in ("0", "false", "no")
    # #290 Fase 5 — the deployment verifier (release reachability + byte verification +
    # install smoke against the downloaded bytes) only runs when the caller is actually
    # trying to promote to `deployed`; every earlier target stays fast and never pays this
    # cost.
    return target_state == "deployed"


def github_delivery_payload(repo: str, pr: int | None = None, tag: str = "", target_state: str = "",
                            verify_artifacts: bool | None = None,
                            verify_reachability: bool | None = None,
                            environment: str = "",
                            verify_deployment: bool | None = None) -> Dict[str, Any]:
    fixture = _fixture_payload("SIMPLICIO_LOOP_GITHUB_FIXTURE_JSON")
    if fixture is not None:
        fixture.setdefault("source_query", {
            "provider": "github",
            "repo": repo,
            "pr": pr,
            "tag": tag,
            "target_state": target_state,
            "environment": environment,
            "mode": "fixture",
        })
        return fixture
    payload: Dict[str, Any] = {}
    payload["source_query"] = {
        "provider": "github",
        "repo": repo,
        "pr": pr,
        "tag": tag,
        "target_state": target_state,
        "environment": environment,
        "mode": "live",
    }
    if pr is not None:
        view = _run_gh([
            "pr", "view", str(pr), "--repo", repo, "--json",
            "url,headRefOid,baseRefOid,reviewDecision,isDraft,mergeStateStatus,mergedAt,mergeCommit,statusCheckRollup"
        ])
        if view.returncode != 0:
            raise RuntimeError("gh pr view failed: " + (view.stderr or "").strip())
        prj = json.loads(view.stdout or "{}")
        checks = prj.get("statusCheckRollup") or []
        green = bool(checks) and all((item or {}).get("conclusion") == "SUCCESS" for item in checks if isinstance(item, dict))
        payload["pr"] = {
            "url": prj.get("url", ""),
            "head_sha": prj.get("headRefOid", ""),
            "base_sha": prj.get("baseRefOid", ""),
            "evidence": "github-pr-view",
        }
        payload["checks"] = {"green": green}
        # #290 Fase 2.4 — `open_threads` used to default to 0 (no threads) whenever no paginated
        # GraphQL review-thread query ran; that read as a positive "clear to merge" signal that
        # was never actually observed. A `SIMPLICIO_LOOP_GITHUB_REVIEWS_FIXTURE_JSON` override is
        # kept ONLY for deterministic tests (marked `trust_level=test-fixture`, per the "no
        # fixture promotion" invariant); the live path now runs a real paginated GraphQL query
        # (`query_review_threads`) that walks every page until `hasNextPage=false` before it will
        # ever set `open_threads_verified=True`.
        reviews_fix = _fixture_payload("SIMPLICIO_LOOP_GITHUB_REVIEWS_FIXTURE_JSON")
        if reviews_fix is not None:
            payload["reviews"] = {
                "approvals": int(reviews_fix.get("approvals", 0)),
                "open_threads": int(reviews_fix.get("open_threads", 0)),
                "open_threads_verified": True,
                "trust_level": "test-fixture",
            }
        else:
            owner, _, name = repo.partition("/")
            thread_result = query_review_threads(owner, name, pr)
            approvals = thread_result.get("approvals")
            if approvals is None:
                approvals = 1 if prj.get("reviewDecision") == "APPROVED" else 0
            payload["reviews"] = {
                "approvals": approvals,
                "open_threads": thread_result["open_threads"],
                "open_threads_verified": thread_result["open_threads_verified"],
                "pagination_pages": thread_result["pages"],
                "evidence": "github-graphql-review-threads",
            }
            if thread_result.get("reason_code"):
                payload["reviews"]["reason_code"] = thread_result["reason_code"]
        # #290 — CLEAN, HAS_HOOKS and UNSTABLE are not the same fact: only CLEAN proves the head
        # is mergeable and up to date with base under the current protection rules. HAS_HOOKS
        # (pending required status checks) and UNSTABLE (failing non-required checks / behind
        # base) must not be folded into the same "up to date" signal.
        payload["branch"] = {
            "up_to_date": str(prj.get("mergeStateStatus", "")).upper() == "CLEAN",
            "merge_state_status": prj.get("mergeStateStatus", ""),
        }
        if str(prj.get("mergedAt") or "").strip():
            # #290 — a merged PR proves the PR event, not that the merge commit is reachable
            # from the real default branch right now (branch protection changes, rebases, or a
            # revert could all diverge this). By default this stays fail-closed
            # (`commit_in_default_branch=False`) unless the caller actually asks for the proof
            # (`target_state` promoting to `merged`+, or `verify_reachability=True`/env
            # override) -- see `_should_verify_branch_reachability`. When requested, the real
            # `BranchReachabilityVerifier` (`external_verifiers.verify_branch_reachability`)
            # discovers the *real* default branch and proves ancestry via the GitHub compare
            # API before this is ever set True.
            merge_commit_sha = (prj.get("mergeCommit") or {}).get("oid") or ""
            payload["merge"] = {
                "commit_sha": merge_commit_sha,
                "default_branch": "",
                "merged_at": prj.get("mergedAt"),
                "commit_in_default_branch": False,
                "commit_in_default_branch_reason_code": "merge_reachability_unverified",
            }
            if _should_verify_branch_reachability(target_state, verify_reachability):
                from .external_verifiers import verify_branch_reachability
                reach = verify_branch_reachability(repo, merge_commit_sha)
                payload["merge"]["default_branch"] = reach.get("default_branch", "")
                if reach.get("ok") and reach.get("reachable"):
                    payload["merge"]["commit_in_default_branch"] = True
                    payload["merge"].pop("commit_in_default_branch_reason_code", None)
                    payload["merge"]["evidence"] = "github-compare-reachability"
                    payload["merge"]["compare_status"] = reach.get("compare_status")
                else:
                    payload["merge"]["commit_in_default_branch_reason_code"] = reach.get(
                        "reason_code", "merge_reachability_unverified")
    if tag:
        release = _run_gh(["release", "view", tag, "--repo", repo, "--json", "tagName,isDraft,isPrerelease,assets"])
        if release.returncode != 0:
            raise RuntimeError("gh release view failed: " + (release.stderr or "").strip())
        rel = json.loads(release.stdout or "{}")
        asset_names = [asset.get("name") for asset in (rel.get("assets") or []) if isinstance(asset, dict)]
        # #290 Fase 4 — presence of a release/tag proves nothing about the bytes attached to it.
        # By default these stay fail-closed (previously hardcoded to `true`, which let `released`
        # be reported without any of this ever running). When verification is requested (either
        # because `target_state` is `released`/`deployed`, or explicitly via `verify_artifacts=`
        # / `SIMPLICIO_LOOP_VERIFY_RELEASE_ARTIFACTS=1`), `external_verifiers.verify_release`
        # downloads the real asset bytes, recomputes their SHA-256, attempts a real
        # `gh attestation verify`, parses an SBOM asset if present, and install-smokes the
        # downloaded wheel in a throwaway venv.
        payload["release"] = {
            "tag": rel.get("tagName", tag),
            "assets": asset_names,
            "checksums_verified": False,
            "signatures_verified": False,
            "sbom_present": False,
            "verification_reason_code": "release_artifact_unverified",
        }
        payload["install_smoke"] = {"passed": False, "reason_code": "install_smoke_unqueried"}
        if _should_verify_release_artifacts(target_state, verify_artifacts):
            from .external_verifiers import verify_release
            verify_result = verify_release(repo, rel.get("tagName", tag), asset_names)
            payload["release"]["checksums_verified"] = verify_result["checksums_verified"]
            payload["release"]["signatures_verified"] = verify_result["signatures_verified"]
            payload["release"]["sbom_present"] = verify_result["sbom_present"]
            payload["release"]["digests"] = verify_result["digests"]
            payload["release"]["assets_verified"] = verify_result["assets_verified"]
            payload["release"]["evidence"] = "external-verifiers-byte-level"
            reason_codes = {
                key: verify_result[key]
                for key in ("checksum_reason_code", "signature_reason_code", "sbom_reason_code")
                if verify_result.get(key)
            }
            if reason_codes:
                payload["release"]["reason_codes"] = reason_codes
            else:
                payload["release"].pop("verification_reason_code", None)
            payload["install_smoke"] = verify_result["install_smoke"]
        # #290 Fase 5 — `deployment` used to be caller-supplied only: an adapter or test could
        # simply hand in `{"environment": "prod", "smoke": {"passed": True}}` and
        # `infer_github_delivery_state` would trust it as "deployed" with zero live proof. By
        # default this stays fail-closed (`deployed_unqueried`) unless the caller actually asks
        # for the `DeploymentVerifier` to run (`target_state="deployed"`, `environment=` set, or
        # `verify_deployment=True` / `SIMPLICIO_LOOP_VERIFY_DEPLOYMENT=1`). When it runs, it
        # composes the same byte-level `verify_release_artifacts`/`run_install_smoke` used by
        # `released` — "deployed" for a package repo means "installable from the published,
        # reachability-proven artifact", not a fabricated boolean.
        payload["deployment"] = {
            "environment": environment,
            "verified_at": None,
            "smoke": {"passed": False, "reason_code": "deployment_unqueried"},
            "reason_code": "deployment_unverified",
        }
        if environment and _should_verify_deployment(target_state, verify_deployment):
            from .external_verifiers import DeploymentVerifier
            merge_commit_sha = ((payload.get("merge") or {}).get("commit_sha")) or None
            deployment_result = DeploymentVerifier().verify(
                repo, rel.get("tagName", tag), environment,
                asset_names=asset_names, expected_commit_sha=merge_commit_sha,
            )
            payload["deployment"] = {
                "environment": deployment_result.get("environment", environment),
                "commit_sha": deployment_result.get("commit_sha"),
                "artifact_digest": deployment_result.get("artifact_digest"),
                "verified_at": deployment_result.get("verified_at"),
                "smoke": deployment_result.get("smoke", {"passed": False, "reason_code": "deployment_unqueried"}),
                "evidence": deployment_result.get("evidence", "external-verifiers-deployment-install-smoke"),
            }
            if not deployment_result.get("ok"):
                payload["deployment"]["reason_code"] = deployment_result.get("reason_code", "deployment_unverified")
    return payload
