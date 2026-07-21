"""Planning-phase binding: every planned item carries acceptance criteria + a receipt.

Closes the protocol gap where ``GitHubDrainIntake.run()`` produced planned items
without the acceptance criteria and planning receipt the loop's planning phase
requires before any mutation.  Read-only: no GitHub state is touched.
"""
from pathlib import Path

from simplicio_loop.github_drain_intake import (
    PLANNER_REVISION,
    PLANNING_RECEIPT_SCHEMA,
    GitHubDrainIntake,
    _build_item_planning_receipt,
    _derive_acceptance_criteria,
)


class _FakeSource:
    def __init__(self, issues):
        self._issues = {str(i["number"]): i for i in issues}

    def list_ready(self, *, state="open"):
        items = [{"number": i["number"], "title": i["title"], "state": "open"}
                 for i in self._issues.values() if i.get("state", "open") == "open"]
        return {"provider": "github", "repo": "acme/widgets", "count": len(items),
                "items": items, "observed_at": "t"}

    def get_details(self, issue):
        i = self._issues[str(issue)]
        return {"provider": "github", "repo": "acme/widgets", "issue": str(issue),
                "state": i.get("state", "open"), "title": i["title"],
                "body": i.get("body", ""), "labels": i.get("labels", []),
                "url": "https://github.com/acme/widgets/issues/%s" % issue,
                "source_revision": i.get("revision", "r1"), "observed_at": "t"}

    def requery(self, issue):
        return self.get_details(issue)


def _run(tmp_path, source):
    ctrl = GitHubDrainIntake(source=source, checkpoint=str(tmp_path / "c.json"),
                              workspace=".", map_reader=None)
    return ctrl.run("drain all issues from acme/widgets")


def test_derive_acceptance_criteria_minimum():
    acs = _derive_acceptance_criteria({"title": "Fazer X", "body": ""})
    ids = [a["id"] for a in acs]
    assert "AC-TITLE" in ids
    assert "AC-EVIDENCE-GATE" in ids
    for a in acs:
        assert a["origin"] in {"source", "derived"}
        assert a["text"]
        assert a["state"] == "pending"


def test_derive_acceptance_criteria_from_section():
    body = "## Acceptance criteria\n- [ ] deve rodar\n- cobre Y\n## Other\n- ignore"
    acs = _derive_acceptance_criteria({"title": "T", "body": body})
    texts = " ".join(a["text"].lower() for a in acs)
    assert "deve rodar" in texts
    assert "cobre y" in texts


def test_planning_receipt_is_deterministic_and_well_formed():
    acs = _derive_acceptance_criteria({"title": "T", "body": ""})
    a = _build_item_planning_receipt(item_number=1, source_revision="r1",
                                     acceptance_criteria=acs, planner_revision=PLANNER_REVISION)
    b = _build_item_planning_receipt(item_number=1, source_revision="r1",
                                     acceptance_criteria=acs, planner_revision=PLANNER_REVISION)
    assert a == b
    assert a["schema"] == PLANNING_RECEIPT_SCHEMA
    assert a["receipt_hash"] and a["acceptance_criteria_hash"] and a["plan_hash"]
    assert a["item_number"] == 1 and a["source_revision"] == "r1"


def test_run_binds_acs_and_receipt_per_item(tmp_path):
    source = _FakeSource([
        {"number": 1, "title": "Primeiro", "body": "## Acceptance criteria\n- [ ] ok", "revision": "r1"},
        {"number": 2, "title": "Segundo", "body": "", "revision": "r2"},
    ])
    result = _run(tmp_path, source)
    assert result["outcome"]["status"] == "PLANNED_NOT_EXECUTED"
    assert len(result["items"]) == 2
    for num, item in result["items"].items():
        assert item["state"] == "planned"
        assert item["acceptance_criteria"], num
        assert item["planning_receipt"]["schema"] == PLANNING_RECEIPT_SCHEMA
        assert item["planning_receipt"]["receipt_hash"]
