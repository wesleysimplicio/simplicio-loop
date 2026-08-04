from __future__ import annotations

import dataclasses
import hashlib
import itertools
import json
import struct
from pathlib import Path

import pytest

from simplicio_loop.hbp_ledger import canonical_sha256
from simplicio_loop.prism_contracts import (
    MIN_TASKS_PER_SLOT,
    PrismContractError,
    PrismExecution,
    SlotSupervisor,
    TaskOwnership,
    admit_task,
    decode_hbp_frame,
    encode_hbp_frame,
    read_legacy_task,
    validate_hierarchy,
)

SHA_A = "a" * 64
SHA_B = "b" * 64
CONTRACTS = (
    Path(__file__).resolve().parents[1]
    / "simplicio_loop"
    / "_contracts"
    / "prism"
    / "v1"
)


def prism(*, parent=None, child_slots=()):
    return PrismExecution(
        goal_id="goal-1",
        owner_agent="supervisor",
        policy_hash=SHA_A,
        config_hash=SHA_B,
        source_generation="gen-1",
        reducer_ref="reducer-1",
        parent_prism_id=parent,
        child_slot_ids=child_slots,
        budget=(("workers", 10),),
    )


def slot(parent_prism_id, *, tasks=(), parent_slot=None, capacity=10):
    return SlotSupervisor(
        parent_prism_id=parent_prism_id,
        supervisor_agent="slot-agent",
        capacity=capacity,
        task_ids=tasks,
        parent_slot_id=parent_slot,
        resource_budget=(("workers", capacity),),
    )


def ownership(task_id, slot_id, *, owner=None, fence=1, state="queued"):
    return TaskOwnership(
        task_id=task_id,
        slot_id=slot_id,
        attempt=1,
        owner_agent=owner or f"agent-{task_id}",
        lease_id=f"lease-{task_id}",
        fence=fence,
        source_generation="gen-1",
        capabilities=("implementation",),
        allowed_transitions=(
            "accepted",
            "blocked",
            "cancelled",
            "failed",
            "ready",
            "running",
        ),
        state=state,
    )


def test_contract_ids_are_permutation_invariant_and_hashed():
    left = PrismExecution(
        goal_id="goal",
        owner_agent="owner",
        policy_hash=SHA_A,
        config_hash=SHA_B,
        source_generation="g",
        reducer_ref="r",
        budget=(("z", 2), ("a", 1)),
        child_slot_ids=("slot-z", "slot-a"),
    )
    right = PrismExecution(
        goal_id="goal",
        owner_agent="owner",
        policy_hash=SHA_A,
        config_hash=SHA_B,
        source_generation="g",
        reducer_ref="r",
        budget=(("a", 1), ("z", 2)),
        child_slot_ids=("slot-a", "slot-z"),
    )
    assert left.prism_id == right.prism_id
    assert left.digest == canonical_sha256(left.to_dict())


def test_slot_has_minimum_ten_and_no_logical_upper_capacity():
    root = prism()
    current = slot(root.prism_id, capacity=100)
    for index in range(MIN_TASKS_PER_SLOT + 5):
        item = ownership(f"t-{index}", current.slot_id)
        prior_id = current.slot_id
        current, receipt = admit_task(current, item)
        assert current.slot_id == prior_id
        assert receipt.admitted is True
        assert receipt.reason_code == "ADMITTED"
        assert len(receipt.to_dict()["receipt_hash"]) == 64
    assert len(current.task_ids) == MIN_TASKS_PER_SLOT + 5


def test_hierarchy_validates_nested_slots_and_exactly_one_owner():
    root = prism()
    parent = slot(root.prism_id, tasks=("a",))
    child = slot(root.prism_id, tasks=("b",), parent_slot=parent.slot_id)
    root = dataclasses.replace(root, child_slot_ids=(parent.slot_id, child.slot_id))
    parent = dataclasses.replace(parent, child_slot_ids=(child.slot_id,))
    result = validate_hierarchy(
        [root],
        [parent, child],
        [ownership("a", parent.slot_id), ownership("b", child.slot_id)],
    )
    assert result["valid"] is True
    assert result["tasks"] == ["a", "b"]


@pytest.mark.parametrize(
    "case",
    [
        "duplicate-owner",
        "missing-owner",
        "cross-slot",
        "unknown-parent",
        "slot-cycle",
        "depth",
    ],
)
def test_hierarchy_fails_closed(case):
    root = prism()
    first = slot(root.prism_id, tasks=("a",))
    owners = [ownership("a", first.slot_id)]
    prisms = [root]
    slots = [first]
    if case == "duplicate-owner":
        owners.append(ownership("a", first.slot_id, owner="other"))
    elif case == "missing-owner":
        owners = []
    elif case == "cross-slot":
        other = slot(root.prism_id)
        slots.append(other)
        owners = [ownership("a", other.slot_id)]
    elif case == "unknown-parent":
        slots = [slot("prism:missing", tasks=("a",))]
        owners = [ownership("a", slots[0].slot_id)]
    elif case == "slot-cycle":
        object.__setattr__(first, "parent_slot_id", first.slot_id)
        slots = [first]
        owners = [ownership("a", first.slot_id)]
    else:
        previous = None
        slots = []
        for index in range(5):
            item = slot(root.prism_id, parent_slot=previous)
            slots.append(item)
            previous = item.slot_id
        owners = []
    with pytest.raises(PrismContractError):
        validate_hierarchy(prisms, slots, owners)


def test_transition_requires_current_owner_fence_and_allowed_state():
    item = ownership("a", "slot-a")
    accepted = item.transition("accepted", fence=1, owner_agent=item.owner_agent)
    assert accepted.state == "accepted"
    assert accepted.ownership_id == item.ownership_id
    with pytest.raises(PrismContractError):
        item.transition("accepted", fence=2, owner_agent=item.owner_agent)
    with pytest.raises(PrismContractError):
        item.transition("validating", fence=1, owner_agent=item.owner_agent)


def test_hbp_frame_round_trip_and_adversarial_rejection():
    value = {"schema": "fixture/v1", "tasks": ["a", "b"], "count": 2}
    frame = encode_hbp_frame(value)
    assert decode_hbp_frame(frame) == value
    for bad in (
        b"",
        b"NOPE" + frame[4:],
        frame[:-1],
        frame[:-1] + bytes([frame[-1] ^ 1]),
    ):
        with pytest.raises(PrismContractError):
            decode_hbp_frame(bad)


def test_checked_in_hbp_golden_is_byte_exact_and_decodes():
    golden = json.loads((CONTRACTS / "hbp-golden.json").read_text(encoding="utf-8"))
    frame = bytes.fromhex(golden["frame_hex"])
    assert len(frame) == golden["frame_bytes"]
    assert encode_hbp_frame(golden["value"]) == frame
    assert decode_hbp_frame(frame) == golden["value"]


def test_contract_schemas_and_adversarial_cases_are_packaged():
    for name in (
        "prism-execution.schema.json",
        "slot-supervisor.schema.json",
        "task-ownership.schema.json",
    ):
        document = json.loads((CONTRACTS / name).read_text(encoding="utf-8"))
        assert document["additionalProperties"] is False
        assert document["title"].endswith("/v1")
    cases = json.loads(
        (CONTRACTS / "conformance-cases.json").read_text(encoding="utf-8")
    )
    assert len(cases["cases"]) >= 4
    assert all(
        case.get("authorized") is False or case.get("admitted") is False
        or case.get("authoritative") is False or case.get("policy_only") is True
        for case in cases["cases"]
    )


def test_legacy_is_readable_but_never_authoritative():
    result = read_legacy_task({"schema": "old/v0", "task_id": "legacy"})
    assert result == {
        "schema": "simplicio.prism-legacy-read/v1",
        "legacy_schema": "old/v0",
        "task_id": "legacy",
        "authoritative": False,
        "reason_code": "LEGACY_NOT_AUTHORITATIVE",
    }


def test_invalid_contract_inputs_fail_closed():
    base = {
        "task_id": "t",
        "slot_id": "s",
        "attempt": 1,
        "owner_agent": "a",
        "lease_id": "l",
        "fence": 1,
        "source_generation": "g",
        "capabilities": ("c",),
        "allowed_transitions": ("accepted",),
    }
    for field, value in (
        ("task_id", ""),
        ("attempt", 0),
        ("fence", 0),
        ("capabilities", ()),
        ("allowed_transitions", ("made-up",)),
    ):
        with pytest.raises(PrismContractError):
            TaskOwnership(**{**base, field: value})
    with pytest.raises(PrismContractError):
        slot(prism().prism_id, capacity=9)


def test_all_contract_validation_boundaries_fail_closed():
    prism_args = {
        "goal_id": "g",
        "owner_agent": "a",
        "policy_hash": SHA_A,
        "config_hash": SHA_B,
        "source_generation": "gen",
        "reducer_ref": "r",
    }
    for patch in (
        {"policy_hash": "bad"},
        {"budget": (("workers", -1),)},
        {"budget": (("workers", True),)},
        {"schema": "future/v9"},
        {"state": "invented"},
        {"max_depth": 0},
        {"prism_id": "prism:wrong"},
    ):
        with pytest.raises(PrismContractError):
            PrismExecution(**{**prism_args, **patch})

    root = PrismExecution(**prism_args)
    slot_args = {
        "parent_prism_id": root.prism_id,
        "supervisor_agent": "a",
    }
    for patch in (
        {"schema": "future/v9"},
        {"state": "invented"},
        {"capacity": 9},
        {"slot_id": "slot:self", "child_slot_ids": ("slot:self",)},
        {"slot_id": "slot:wrong"},
    ):
        with pytest.raises(PrismContractError):
            SlotSupervisor(**{**slot_args, **patch})

    task_args = {
        "task_id": "t",
        "slot_id": "s",
        "attempt": 1,
        "owner_agent": "a",
        "lease_id": "l",
        "fence": 1,
        "source_generation": "g",
        "capabilities": ("c",),
        "allowed_transitions": ("accepted",),
    }
    for patch in (
        {"schema": "future/v9"},
        {"allowed_transitions": ()},
        {"state": "invented"},
        {"ownership_id": "ownership:wrong"},
    ):
        with pytest.raises(PrismContractError):
            TaskOwnership(**{**task_args, **patch})
    item = TaskOwnership(**task_args)
    assert item.to_dict()["schema"].endswith("/v1")
    assert len(item.digest) == 64


def test_admission_rejects_cross_slot_and_duplicate():
    root = prism()
    current = slot(root.prism_id, tasks=("a",))
    with pytest.raises(PrismContractError, match="crosses"):
        admit_task(current, ownership("b", "other-slot"))
    with pytest.raises(PrismContractError, match="duplicate"):
        admit_task(current, ownership("a", current.slot_id))


def test_hierarchy_reference_and_size_boundaries():
    root = prism()
    current = slot(root.prism_id, tasks=("a",))
    owner = ownership("a", current.slot_id)
    with pytest.raises(PrismContractError, match="max_depth"):
        validate_hierarchy([root], [current], [owner], max_depth=0)
    with pytest.raises(PrismContractError, match="duplicate"):
        validate_hierarchy([root, root], [current], [owner])

    missing_parent = dataclasses.replace(
        root, parent_prism_id="prism:missing", prism_id=""
    )
    with pytest.raises(PrismContractError, match="parent prism"):
        validate_hierarchy([missing_parent], [], [])
    missing_child = dataclasses.replace(root, child_slot_ids=("slot:missing",))
    with pytest.raises(PrismContractError, match="child slot"):
        validate_hierarchy([missing_child], [], [])

    bad_parent_slot = dataclasses.replace(
        current, parent_slot_id="slot:missing", slot_id=""
    )
    with pytest.raises(PrismContractError, match="parent slot"):
        validate_hierarchy(
            [root], [bad_parent_slot], [ownership("a", bad_parent_slot.slot_id)]
        )
    bad_child_slot = dataclasses.replace(current, child_slot_ids=("slot:missing",))
    with pytest.raises(PrismContractError, match="child slot"):
        validate_hierarchy(
            [root], [bad_child_slot], [ownership("a", bad_child_slot.slot_id)]
        )

    no_such_slot = ownership("a", "slot:missing")
    with pytest.raises(PrismContractError, match="unknown slot"):
        validate_hierarchy([root], [current], [no_such_slot])
    not_admitted = ownership("b", current.slot_id)
    with pytest.raises(PrismContractError, match="not admitted"):
        validate_hierarchy([root], [current], [not_admitted])

    many = [
        PrismExecution(
            goal_id=f"g-{index}",
            owner_agent="a",
            policy_hash=SHA_A,
            config_hash=SHA_B,
            source_generation="gen",
            reducer_ref="r",
        )
        for index in range(81)
    ]
    assert validate_hierarchy(many, [], []) ["valid"] is True


def _raw_frame(payload):
    return (
        b"SPH1"
        + struct.pack(">I", len(payload))
        + payload
        + hashlib.sha256(payload).digest()
    )


def test_hbp_frame_rejects_oversize_json_non_object_and_noncanonical():
    with pytest.raises(PrismContractError, match="size limit"):
        encode_hbp_frame({"payload": "x" * (8 * 1024 * 1024)})
    with pytest.raises(PrismContractError, match="length"):
        decode_hbp_frame(b"SPH1" + struct.pack(">I", 8 * 1024 * 1024 + 1) + b"x" * 32)
    with pytest.raises(PrismContractError, match="payload"):
        decode_hbp_frame(_raw_frame(b"{"))
    with pytest.raises(PrismContractError, match="object"):
        decode_hbp_frame(_raw_frame(b"[]"))
    noncanonical = json.dumps({"b": 1, "a": 2}, separators=(", ", ": ")).encode()
    with pytest.raises(PrismContractError, match="canonical"):
        decode_hbp_frame(_raw_frame(noncanonical))


def test_every_task_permutation_has_same_hierarchy_digest():
    root = prism()
    current = slot(root.prism_id, tasks=("a", "b", "c"))
    expected = None
    for order in itertools.permutations(("a", "b", "c")):
        result = validate_hierarchy(
            [root],
            [current],
            [ownership(task_id, current.slot_id) for task_id in order],
        )
        expected = expected or result["digest"]
        assert result["digest"] == expected
