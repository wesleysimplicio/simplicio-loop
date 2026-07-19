#!/usr/bin/env python3
"""Project/label synchronizer for Simplicio lifecycle comments.

GitHub Actions calls this script for the canonical lifecycle comment emitted by
the loop.  It deliberately treats ``runtime`` as metadata: Claude, Codex,
Cursor, Gemini, Kiro, Antigravity, Hermes/Simplicio Agent, OpenClaw, and future
providers all use the same GitHub source contract.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


LIFECYCLE_MARKER = "<!-- simplicio-loop:lifecycle-status:v1 -->"
LIFECYCLE_STATES = {
    "DISCOVERED", "CLAIMED", "PLANNED", "IN_PROGRESS", "VERIFYING", "BLOCKED",
    "PAUSED_NETWORK", "AWAITING_DECISION", "PR_OPEN", "MERGE_READY", "MERGED",
    "CLOSING", "CLOSE_PENDING_RECONCILIATION", "CLOSED", "RELEASED",
}
LABEL_PREFIX = "simplicio:status:"
PROJECT_STATUS_NAMES = {
    "DISCOVERED": ("Todo",),
    "CLAIMED": ("In Progress", "Todo"),
    "PLANNED": ("Todo", "In Progress"),
    "IN_PROGRESS": ("In Progress",),
    "VERIFYING": ("In Progress", "Review"),
    "BLOCKED": ("Blocked", "In Progress"),
    "PAUSED_NETWORK": ("Blocked", "In Progress"),
    "AWAITING_DECISION": ("Blocked", "In Progress"),
    "PR_OPEN": ("Review", "In Progress"),
    "MERGE_READY": ("Ready", "Review", "In Progress"),
    "MERGED": ("Done",),
    "CLOSING": ("Done", "In Progress"),
    "CLOSE_PENDING_RECONCILIATION": ("Blocked", "In Progress"),
    "CLOSED": ("Done",),
    "RELEASED": ("Done",),
}


class GitHubError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class GitHubApi:
    def __init__(self, token: str, *, api_url: str = "https://api.github.com") -> None:
        self.base = api_url.rstrip("/")
        self.token = token

    def request(self, method: str, path: str, payload: Optional[Mapping[str, Any]] = None) -> Any:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(self.base + path, data=body, method=method, headers={
            "Accept": "application/vnd.github+json",
            "Authorization": "Bearer " + self.token,
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        })
        try:
            with urlopen(request, timeout=30) as response:
                raw = response.read()
                return json.loads(raw.decode("utf-8")) if raw else None
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise GitHubError(exc.code, detail[:500]) from exc
        except URLError as exc:
            raise GitHubError(0, str(exc)) from exc

    def graphql(self, query: str, variables: Mapping[str, Any]) -> Dict[str, Any]:
        result = self.request("POST", "/graphql", {"query": query, "variables": dict(variables)})
        if result.get("errors"):
            raise GitHubError(0, json.dumps(result["errors"])[:500])
        return result.get("data") or {}


def extract_state(body: str) -> Optional[str]:
    if LIFECYCLE_MARKER not in (body or ""):
        return None
    match = re.search(r"\|\s*(?:Estado|Status)\s*\|\s*([A-Z_]+)\s*\|", body or "")
    state = match.group(1) if match else None
    return state if state in LIFECYCLE_STATES else None


def status_label(state: str) -> str:
    return LABEL_PREFIX + state.lower()


def _safe_label_name(name: str) -> str:
    return quote(name, safe="")


def sync_issue_label(api: GitHubApi, repo: str, issue_number: int, state: str) -> str:
    labels = api.request("GET", f"/repos/{repo}/issues/{issue_number}/labels?per_page=100") or []
    for item in labels:
        name = str(item.get("name") or "")
        if name.startswith(LABEL_PREFIX) and name != status_label(state):
            api.request("DELETE", f"/repos/{repo}/issues/{issue_number}/labels/{_safe_label_name(name)}")
    label = status_label(state)
    try:
        api.request("POST", f"/repos/{repo}/labels", {
            "name": label, "color": "6f42c1",
            "description": "Simplicio-loop lifecycle status",
        })
    except GitHubError as exc:
        if exc.status != 422:
            raise
    api.request("POST", f"/repos/{repo}/issues/{issue_number}/labels", {"labels": [label]})
    return label


def _project_query() -> str:
    return """query($owner:String!, $repo:String!, $number:Int!) {
      repository(owner:$owner, name:$repo) { projectV2(number:$number) {
        id
        fields(first:100) { nodes { ... on ProjectV2SingleSelectField { id name options { id name } } } }
        items(first:100) { nodes { id content { ... on Issue { number repository { nameWithOwner } } } } }
      } }
    }"""


def _projects_query() -> str:
    return """query($owner:String!, $repo:String!) {
      repository(owner:$owner, name:$repo) {
        projectsV2(first:100, orderBy:{field:UPDATED_AT,direction:DESC}) {
          nodes { id number title }
        }
      }
    }"""


def _issue_node_query() -> str:
    return """query($owner:String!, $repo:String!, $number:Int!) {
      repository(owner:$owner, name:$repo) { issue(number:$number) { id } }
    }"""


def _repo_parts(repo: str) -> tuple[str, str]:
    parts = repo.split("/", 1)
    if len(parts) != 2 or not all(parts):
        raise ValueError(f"repository must be owner/name, got {repo!r}")
    return parts[0], parts[1]


def discover_project_number(api: GitHubApi, repo: str) -> Optional[int]:
    """Find the repository-owned Project, conservatively and deterministically."""
    owner, name = _repo_parts(repo)
    data = api.graphql(_projects_query(), {"owner": owner, "repo": name})
    projects = (((data.get("repository") or {}).get("projectsV2") or {}).get("nodes") or [])
    if len(projects) == 1:
        return int(projects[0]["number"])
    accepted = {name.casefold(), name.replace("-", " ").casefold()}
    matches = [p for p in projects if str(p.get("title") or "").strip().casefold() in accepted]
    return int(matches[0]["number"]) if len(matches) == 1 else None


def _add_issue_to_project(api: GitHubApi, repo: str, issue_number: int, project_id: str) -> str:
    owner, name = _repo_parts(repo)
    data = api.graphql(_issue_node_query(), {"owner": owner, "repo": name, "number": issue_number})
    issue_id = str((((data.get("repository") or {}).get("issue") or {}).get("id")) or "")
    if not issue_id:
        return ""
    mutation = """mutation($project:ID!, $content:ID!) {
      addProjectV2ItemById(input:{projectId:$project, contentId:$content}) { item { id } }
    }"""
    data = api.graphql(mutation, {"project": project_id, "content": issue_id})
    return str(((((data.get("addProjectV2ItemById") or {}).get("item")) or {}).get("id")) or "")


def sync_project_item(api: GitHubApi, repo: str, issue_number: int, state: str,
                      *, owner: str, project_number: int,
                      owner_type: str = "repository", status_field: str = "Status") -> bool:
    """Add the issue to, then move it within, the repository-owned Project."""
    del owner, owner_type  # repository ownership is canonical; kept for CLI compatibility
    repo_owner, repo_name = _repo_parts(repo)
    data = api.graphql(_project_query(), {"owner": repo_owner, "repo": repo_name, "number": project_number})
    project = ((data.get("repository") or {}).get("projectV2"))
    if not project:
        return False
    issue_item = None
    for item in (project.get("items", {}).get("nodes", []) or []):
        content = item.get("content") or {}
        if (content.get("number") == issue_number and
                str((content.get("repository") or {}).get("nameWithOwner")) == repo):
            issue_item = item
            break
    if not issue_item:
        item_id = _add_issue_to_project(api, repo, issue_number, str(project.get("id") or ""))
        if not item_id:
            return False
        issue_item = {"id": item_id}
    field = None
    for candidate in project.get("fields", {}).get("nodes", []) or []:
        if str(candidate.get("name", "")).casefold() == status_field.casefold():
            field = candidate
            break
    if not field:
        return False
    option = None
    wanted = PROJECT_STATUS_NAMES.get(state, ("In Progress",))
    for desired in wanted:
        option = next((o for o in field.get("options", [])
                       if str(o.get("name", "")).casefold() == desired.casefold()), None)
        if option:
            break
    if not option:
        return False
    mutation = """mutation($project:ID!, $item:ID!, $field:ID!, $option:String!) {
      updateProjectV2ItemFieldValue(input:{projectId:$project,itemId:$item,fieldId:$field,value:{singleSelectOptionId:$option}}) {
        projectV2Item { id }
      }
    }"""
    api.graphql(mutation, {"project": project["id"], "item": issue_item["id"],
                           "field": field["id"], "option": option["id"]})
    return True


def event_state(event_name: str, event: Mapping[str, Any], forced_state: str = "") -> Optional[str]:
    if forced_state:
        return forced_state if forced_state in LIFECYCLE_STATES else None
    if event_name == "issue_comment":
        if event.get("action") == "deleted":
            return None
        return extract_state(str((event.get("comment") or {}).get("body") or ""))
    action = str(event.get("action") or "")
    if event_name == "issues":
        return {"opened": "DISCOVERED", "closed": "CLOSED", "reopened": "IN_PROGRESS"}.get(action)
    return None


def sync_event(api: GitHubApi, repo: str, event_name: str, event: Mapping[str, Any], *,
               project_number: Optional[int] = None, project_owner: str = "",
               project_owner_type: str = "repository", status_field: str = "Status",
               forced_state: str = "") -> Dict[str, Any]:
    state = event_state(event_name, event, forced_state)
    issue = event.get("issue") or {}
    issue_number = issue.get("number")
    if not state or not issue_number:
        return {"status": "skipped", "reason": "no lifecycle state or issue"}
    if state not in LIFECYCLE_STATES:
        return {"status": "skipped", "reason": "invalid lifecycle state"}
    label = sync_issue_label(api, repo, int(issue_number), state)
    selected_number = project_number or discover_project_number(api, repo)
    moved = bool(selected_number and sync_project_item(
        api, repo, int(issue_number), state, owner=project_owner or repo.split("/", 1)[0],
        project_number=int(selected_number), owner_type=project_owner_type,
        status_field=status_field,
    ))
    return {"status": "synced", "issue": int(issue_number), "state": state,
            "label": label, "project_moved": moved,
            "project_number": selected_number,
            "project_reason": "moved" if moved else ("project_not_found" if not selected_number else "not_moved")}


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="sync Simplicio lifecycle status to GitHub")
    parser.add_argument("--event", required=True, help="GitHub event JSON path")
    parser.add_argument("--event-name", default=os.environ.get("GITHUB_EVENT_NAME", ""))
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--token", default=os.environ.get("GITHUB_TOKEN", ""))
    parser.add_argument("--project-number", type=int, default=0)
    parser.add_argument("--project-owner", default="")
    parser.add_argument("--project-owner-type", choices=("repository", "organization", "user"), default="repository")
    parser.add_argument("--status-field", default="Status")
    parser.add_argument("--state", default="")
    parser.add_argument("--issue-number", type=int, default=0)
    args = parser.parse_args(argv)
    if not args.repo or not args.token:
        print("github-status-sync: missing --repo or GitHub token", file=sys.stderr)
        return 2
    event = json.loads(Path(args.event).read_text(encoding="utf-8"))
    if args.issue_number and not event.get("issue"):
        event["issue"] = {"number": args.issue_number}
    api = GitHubApi(args.token)
    result = sync_event(api, args.repo, args.event_name, event,
                        project_number=args.project_number or None,
                        project_owner=args.project_owner,
                        project_owner_type=args.project_owner_type,
                        status_field=args.status_field, forced_state=args.state)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
