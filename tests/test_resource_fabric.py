from __future__ import annotations

import asyncio
import sys

import pytest

from simplicio_loop.process_supervisor import ProcessSpec
from simplicio_loop.resource_fabric import (
    AuthorityConflict,
    CapacityExceeded,
    FabricDraining,
    HostCapacity,
    ResourceFabric,
    ResourceRequest,
)
from simplicio_loop.slot_lease import StaleFence


def _fabric(tmp_path, *, owner="loop", process_slots=1):
    return ResourceFabric(
        tmp_path,
        owner_id=owner,
        capacity=HostCapacity(cpu_units=2, io_units=2, process_slots=process_slots),
    )


def test_process_claim_requires_authority_and_capacity_is_bounded(tmp_path):
    fabric = _fabric(tmp_path)
    request = ResourceRequest("one", "process", owner_id="loop")
    with pytest.raises(AuthorityConflict):
        fabric.claim(request)
    fabric.start()
    first = fabric.claim(request)
    assert first["status"] == "CLAIMED"
    with pytest.raises(CapacityExceeded):
        fabric.claim(ResourceRequest("two", "process", owner_id="loop"))
    fabric.release(first)
    assert fabric.status()["usage"]["process"] == 0


def test_runtime_takeover_invalidates_python_claims(tmp_path):
    fabric = _fabric(tmp_path)
    fabric.start()
    claim = fabric.claim(ResourceRequest("one", "process", owner_id="loop"))
    handshake = fabric.prepare_takeover("runtime")
    receipt = fabric.takeover(handshake)
    assert receipt["event"] == "AUTHORITY_TAKEOVER"
    assert receipt["invalidated_claims"] >= 1
    with pytest.raises(StaleFence):
        fabric.heartbeat(claim)
    with pytest.raises(AuthorityConflict):
        fabric.claim(ResourceRequest("old", "process", owner_id="loop"))
    current = fabric.claim(ResourceRequest("new", "process", owner_id="runtime"))
    assert current["lease"]["owner_id"] == "runtime"


def test_takeover_handshake_is_single_use_and_fail_closed(tmp_path):
    fabric = _fabric(tmp_path)
    fabric.start()
    handshake = fabric.prepare_takeover("runtime")
    fabric.takeover(handshake)
    with pytest.raises(Exception, match="TAKEOVER_STALE|TAKEOVER_INVALID"):
        fabric.takeover(handshake)


def test_spawn_is_fenced_and_reaped_by_process_adapter(tmp_path):
    fabric = _fabric(tmp_path)
    fabric.start()
    spec = ProcessSpec(
        (sys.executable, "-c", "print('resource-ok')"),
        cwd=str(tmp_path), cwd_allowlist=(str(tmp_path),), timeout_seconds=5,
    )
    result = asyncio.run(fabric.spawn(ResourceRequest("proc", "process"), spec))
    assert result["status"] == "COMPLETED"
    assert result["process"]["returncode"] == 0
    assert "resource-ok" in result["process"]["stdout"]
    assert fabric.status()["processes"] == []


def test_drain_blocks_new_claims_and_invalidates_existing_leases(tmp_path):
    fabric = _fabric(tmp_path)
    fabric.start()
    claim = fabric.claim(ResourceRequest("one", "process"))
    asyncio.run(fabric.drain(reason="test-stop"))
    assert fabric.status()["draining"] is True
    with pytest.raises(FabricDraining):
        fabric.claim(ResourceRequest("two", "process"))
    with pytest.raises(StaleFence):
        fabric.release(claim)
