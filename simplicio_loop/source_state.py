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


def github_delivery_payload(repo: str, pr: int | None = None, tag: str = "", target_state: str = "") -> Dict[str, Any]:
    fixture = _fixture_payload("SIMPLICIO_LOOP_GITHUB_FIXTURE_JSON")
    if fixture is not None:
        fixture.setdefault("source_query", {
            "provider": "github",
            "repo": repo,
            "pr": pr,
            "tag": tag,
            "target_state": target_state,
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
        approvals = 0
        open_threads = 0
        reviews_fix = _fixture_payload("SIMPLICIO_LOOP_GITHUB_REVIEWS_FIXTURE_JSON")
        if reviews_fix is not None:
            approvals = int(reviews_fix.get("approvals", 0))
            open_threads = int(reviews_fix.get("open_threads", 0))
        payload["pr"] = {
            "url": prj.get("url", ""),
            "head_sha": prj.get("headRefOid", ""),
            "base_sha": prj.get("baseRefOid", ""),
            "evidence": "github-pr-view",
        }
        payload["checks"] = {"green": green}
        # #290 — `open_threads` was silently defaulted to 0 (no threads) whenever no paginated
        # GraphQL review-thread query ran; that reads as a positive "clear to merge" signal that
        # was never actually observed. Keep the count (still 0 by convention when unqueried, for
        # backward JSON shape) but attach an explicit `open_threads_verified` marker so
        # `infer_github_delivery_state` never promotes to merge-ready on an unqueried assumption.
        payload["reviews"] = {
            "approvals": approvals if approvals else (1 if prj.get("reviewDecision") == "APPROVED" else 0),
            "open_threads": open_threads,
            "open_threads_verified": reviews_fix is not None,
        }
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
            # revert could all diverge this). No BranchReachabilityVerifier exists yet, so this
            # must fail closed rather than assert `commit_in_default_branch: true` for free.
            payload["merge"] = {
                "commit_sha": ((prj.get("mergeCommit") or {}).get("oid") or ""),
                "default_branch": "main",
                "merged_at": prj.get("mergedAt"),
                "commit_in_default_branch": False,
                "commit_in_default_branch_reason_code": "merge_reachability_unverified",
            }
    if tag:
        release = _run_gh(["release", "view", tag, "--repo", repo, "--json", "tagName,isDraft,isPrerelease,assets"])
        if release.returncode != 0:
            raise RuntimeError("gh release view failed: " + (release.stderr or "").strip())
        rel = json.loads(release.stdout or "{}")
        # #290 — presence of a release/tag proves nothing about the bytes attached to it. No
        # ReleaseArtifactVerifier/AttestationVerifier/SbomVerifier/InstallSmokeVerifier exists yet
        # to download and validate checksums/signatures/SBOM/install, so these must fail closed
        # (previously hardcoded to `true`, which let `released` be reported without any of that
        # ever running).
        payload["release"] = {
            "tag": rel.get("tagName", tag),
            "assets": [asset.get("name") for asset in (rel.get("assets") or []) if isinstance(asset, dict)],
            "checksums_verified": False,
            "signatures_verified": False,
            "sbom_present": False,
            "verification_reason_code": "release_artifact_unverified",
        }
        payload["install_smoke"] = {"passed": False, "reason_code": "install_smoke_unqueried"}
    return payload
