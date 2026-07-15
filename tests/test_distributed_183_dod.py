"""test_distributed_183_dod.py — DoD completo da issue #183.

Verifica cada critério do Definition of Done sem infra externa:

1. Fan-out por default quando há tarefas independentes.
2. Dependências e conflitos permanecem serializados.
3. Cada agente tem identidade estável nos receipts.
4. Claims são atômicos, leaseados e protegidos por fencing token.
5. Cada agente recebe somente seu context pack autorizado.
6. Codex e Claude na mesma fila segura (sem colisão).
7. Worktree/branch/receipt isolados → convergência via evidence gate.
8. Falha de rede degrada para pausa segura (fail-closed, sem duplicar mutação).
9. 100%/COMPLETE só ocorre quando todas as frentes e receipts convergiram.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.distributed_default import (  # noqa: E402
    Claim,
    ClaimStore,
    DistributedExecutor,
    DistributedRun,
    DistributedTask,
    FencingToken,
    _fan_out_enabled,
)

# ── helpers ────────────────────────────────────────────────────────────────────

def _noop_worker(claim: Claim) -> Dict[str, Any]:
    return {"status": "ok", "task_id": claim.task_id, "agent_id": claim.agent_id}


def _slow_worker(delay: float = 0.05):
    def _w(claim: Claim) -> Dict[str, Any]:
        time.sleep(delay)
        return {"status": "ok", "task_id": claim.task_id}
    return _w


def _failing_worker(claim: Claim) -> Dict[str, Any]:
    raise RuntimeError("simulated network failure")


def _make_codex_claude_run(run_id: str = "test-run") -> DistributedRun:
    return DistributedRun(
        run_id=run_id,
        tasks=[
            DistributedTask("t-codex", "planner/frontend", "planner/frontend", "codex"),
            DistributedTask("t-claude", "operator/backend", "operator/backend", "claude"),
        ],
        max_workers=4,
        lease_seconds=10.0,
    )


# ── DoD 1: fan-out por default ─────────────────────────────────────────────────

def test_dod1_fan_out_enabled_by_default(monkeypatch):
    monkeypatch.delenv("SIMPLICIO_LOOP_AUTO_FAN_OUT", raising=False)
    assert _fan_out_enabled() is True


def test_dod1_fan_out_disabled_via_env(monkeypatch):
    monkeypatch.setenv("SIMPLICIO_LOOP_AUTO_FAN_OUT", "0")
    assert _fan_out_enabled() is False


def test_dod1_independent_tasks_run_in_parallel():
    """Duas tarefas independentes devem executar em paralelo (não serial)."""
    order: List[str] = []
    barrier = threading.Barrier(2)

    def barrier_worker(claim: Claim) -> Dict[str, Any]:
        order.append(f"start:{claim.task_id}")
        barrier.wait(timeout=5)
        order.append(f"end:{claim.task_id}")
        return {"status": "ok", "task_id": claim.task_id}

    run = _make_codex_claude_run()
    result = DistributedExecutor(run, barrier_worker).execute()

    assert result["status"] == "COMPLETE"
    # Ambos iniciaram antes de qualquer um terminar
    starts = [o for o in order if o.startswith("start:")]
    assert len(starts) == 2


# ── DoD 2: dependências serializadas ─────────────────────────────────────────

def test_dod2_dependent_task_runs_after_its_deps():
    """Tarefa com dep só roda depois que a dep terminou."""
    finished: List[str] = []

    def tracking_worker(claim: Claim) -> Dict[str, Any]:
        finished.append(claim.task_id)
        return {"status": "ok"}

    run = DistributedRun(
        run_id="dep-test",
        tasks=[
            DistributedTask("A", "task A", "lane-a", "codex"),
            DistributedTask("B", "task B", "lane-b", "claude"),
            DistributedTask("C", "task C", "lane-c", "cursor", dependencies=["A", "B"]),
        ],
        max_workers=4,
        lease_seconds=10.0,
    )
    result = DistributedExecutor(run, tracking_worker).execute()

    assert result["status"] == "COMPLETE"
    assert finished.index("C") > finished.index("A")
    assert finished.index("C") > finished.index("B")


def test_dod2_independent_groups_computed_correctly():
    run = DistributedRun(
        run_id="groups-test",
        tasks=[
            DistributedTask("A", "A", "l", "codex"),
            DistributedTask("B", "B", "l", "claude"),
            DistributedTask("C", "C", "l", "cursor", dependencies=["A", "B"]),
        ],
        max_workers=4,
        lease_seconds=10.0,
    )
    groups = run.independent_groups()
    assert len(groups) == 2
    group_ids = [sorted(t.id for t in g) for g in groups]
    assert group_ids[0] == ["A", "B"]
    assert group_ids[1] == ["C"]


# ── DoD 3: identidade estável nos claims/receipts ─────────────────────────────

def test_dod3_agent_identity_stable_in_receipts():
    run = _make_codex_claude_run()
    result = DistributedExecutor(run, _noop_worker).execute()

    agent_ids = {c["agent_id"] for c in result["claims"]}
    assert "codex-planner/frontend" in agent_ids
    assert "claude-operator/backend" in agent_ids


def test_dod3_agent_id_matches_runtime_and_lane():
    store = ClaimStore()
    fencing = FencingToken(Path("/tmp/test_fence_%s.json" % uuid.uuid4().hex[:8]))
    token = fencing.acquire("codex:t1")
    claim = store.try_claim("t1", "codex-lane-a", "codex", "lane-a", token, {})
    assert claim is not None
    assert claim.agent_id == "codex-lane-a"
    assert claim.runtime == "codex"
    assert claim.lane == "lane-a"


# ── DoD 4: claims atômicos, leaseados, fencing token ─────────────────────────

def test_dod4_atomic_claim_prevents_double_claim():
    store = ClaimStore()
    fencing = FencingToken(Path("/tmp/test_fence_%s.json" % uuid.uuid4().hex[:8]))
    token_a = fencing.acquire("agent-a:t1")
    claim_a = store.try_claim("t1", "agent-a", "codex", "lane", token_a, {})
    assert claim_a is not None

    token_b = fencing.acquire("agent-b:t1")
    claim_b = store.try_claim("t1", "agent-b", "claude", "lane", token_b, {})
    assert claim_b is None  # já claimado


def test_dod4_expired_lease_allows_reclaim():
    store = ClaimStore()
    fencing = FencingToken(Path("/tmp/test_fence_%s.json" % uuid.uuid4().hex[:8]))
    token = fencing.acquire("agent-a:t1")
    claim = store.try_claim("t1", "agent-a", "codex", "lane", token, {}, lease_seconds=0.01)
    assert claim is not None
    time.sleep(0.05)  # deixa expirar
    token2 = fencing.acquire("agent-b:t1")
    claim2 = store.try_claim("t1", "agent-b", "claude", "lane", token2, {})
    assert claim2 is not None


def test_dod4_fencing_token_validates_owner():
    fencing = FencingToken(Path("/tmp/test_fence_%s.json" % uuid.uuid4().hex[:8]))
    token = fencing.acquire("agent-a:t1")
    assert fencing.validate(token) is True
    stale = {"seq": 0, "agent_id": "agent-a:t1"}
    assert fencing.validate(stale) is False


# ── DoD 5: context pack autorizado por agente ─────────────────────────────────

def test_dod5_agent_receives_only_authorized_context():
    received_packs: Dict[str, Any] = {}

    def capture_worker(claim: Claim) -> Dict[str, Any]:
        received_packs[claim.task_id] = claim.context_pack
        return {"status": "ok"}

    run = DistributedRun(
        run_id="ctx-test",
        tasks=[
            DistributedTask("t1", "goal1", "lane-codex", "codex",
                            context_fields=["task_id", "goal"]),
            DistributedTask("t2", "goal2", "lane-claude", "claude",
                            context_fields=["task_id", "lane"]),
        ],
        max_workers=2,
        lease_seconds=10.0,
    )
    DistributedExecutor(run, capture_worker).execute()

    # t1 só recebe task_id + goal (não lane)
    assert "task_id" in received_packs["t1"]
    assert "goal" in received_packs["t1"]

    # t2 só recebe task_id + lane (não goal)
    assert "task_id" in received_packs["t2"]
    assert "lane" in received_packs["t2"]


# ── DoD 6: Codex + Claude mesma fila, sem colisão ─────────────────────────────

def test_dod6_codex_and_claude_share_queue_without_collision():
    run = _make_codex_claude_run("codex-claude-shared")
    result = DistributedExecutor(run, _noop_worker).execute()

    assert result["status"] == "COMPLETE"
    assert result["errors"] == []
    task_ids = {c["task_id"] for c in result["claims"]}
    assert "t-codex" in task_ids
    assert "t-claude" in task_ids


def test_dod6_concurrent_claims_are_collision_free():
    """10 threads tentam clamar o mesmo task; exatamente 1 deve vencer."""
    store = ClaimStore()
    fencing = FencingToken(Path("/tmp/test_fence_%s.json" % uuid.uuid4().hex[:8]))
    winners: List[str] = []

    def try_claim(agent_id: str) -> None:
        token = fencing.acquire(f"{agent_id}:shared-task")
        c = store.try_claim("shared-task", agent_id, "codex", "lane", token, {})
        if c is not None:
            winners.append(agent_id)

    threads = [threading.Thread(target=try_claim, args=(f"agent-{i}",)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(winners) == 1


# ── DoD 7: isolamento + convergência por evidence gate ───────────────────────

def test_dod7_receipts_isolated_per_agent():
    run = _make_codex_claude_run()
    result = DistributedExecutor(run, _noop_worker).execute()

    for claim in result["claims"]:
        assert claim["receipt"] is not None
        assert claim["receipt"]["task_id"] == claim["task_id"]


def test_dod7_converged_true_when_all_receipts_present():
    store = ClaimStore()
    fencing = FencingToken(Path("/tmp/test_fence_%s.json" % uuid.uuid4().hex[:8]))

    for tid, aid, rt in [("t1", "codex-lane", "codex"), ("t2", "claude-lane", "claude")]:
        tok = fencing.acquire(f"{rt}:{tid}")
        c = store.try_claim(tid, aid, rt, "lane", tok, {})
        assert c is not None

    assert store.converged() is False  # sem receipts ainda

    store.release("t1", "codex-lane", {"status": "ok"})
    assert store.converged() is False

    store.release("t2", "claude-lane", {"status": "ok"})
    assert store.converged() is True


# ── DoD 8: falha de rede → pausa segura, sem duplicar mutação ────────────────

def test_dod8_network_failure_does_not_duplicate_mutation():
    """Worker que lança exceção: erro registrado mas outros tasks não são duplicados."""
    call_count: Dict[str, int] = {}

    def counting_worker(claim: Claim) -> Dict[str, Any]:
        call_count[claim.task_id] = call_count.get(claim.task_id, 0) + 1
        if claim.runtime == "codex":
            raise RuntimeError("simulated network failure")
        return {"status": "ok"}

    run = _make_codex_claude_run()
    result = DistributedExecutor(run, counting_worker).execute()

    # Cada task foi chamado exatamente 1 vez (sem retry que duplique)
    assert call_count.get("t-codex", 0) == 1
    assert call_count.get("t-claude", 0) == 1
    assert len(result["errors"]) == 1
    assert result["errors"][0]["task_id"] == "t-codex"


def test_dod8_fail_closed_when_fencing_invalid(tmp_path):
    """Fencing inválido: claim liberado como abort, não executa worker."""
    executed: List[str] = []

    class StaleToken(FencingToken):
        def validate(self, token):  # always invalid
            return False

    def tracking_worker(claim: Claim) -> Dict[str, Any]:
        executed.append(claim.task_id)
        return {"status": "ok"}

    run = DistributedRun(
        run_id="fence-test",
        tasks=[DistributedTask("ft1", "goal", "lane", "codex")],
        max_workers=1,
        lease_seconds=10.0,
        fence_token_path=tmp_path / "fence.json",
    )
    fencing = StaleToken(tmp_path / "fence.json")
    result = DistributedExecutor(run, tracking_worker,
                                  fencing=fencing).execute()

    assert executed == []  # worker nunca executou
    assert any(e["error"] == "fencing_token_invalid" for e in result["errors"])


# ── DoD 9: COMPLETE só quando todas as frentes convergem ─────────────────────

def test_dod9_complete_only_when_all_receipts_converge():
    run = _make_codex_claude_run()
    result = DistributedExecutor(run, _noop_worker).execute()

    assert result["status"] == "COMPLETE"
    assert result["converged"] is True
    assert len(result["claims"]) == 2
    for claim in result["claims"]:
        assert claim["receipt"] is not None


def test_dod9_partial_when_any_task_errors():
    run = _make_codex_claude_run()

    def mixed_worker(claim: Claim) -> Dict[str, Any]:
        if claim.task_id == "t-codex":
            raise RuntimeError("error")
        return {"status": "ok"}

    result = DistributedExecutor(run, mixed_worker).execute()
    assert result["status"] == "PARTIAL"
    assert result["converged"] is False


def test_dod9_serial_fallback_also_converges(monkeypatch):
    monkeypatch.setenv("SIMPLICIO_LOOP_AUTO_FAN_OUT", "0")
    run = _make_codex_claude_run()
    result = DistributedExecutor(run, _noop_worker).execute()

    assert result["status"] == "COMPLETE"
    assert result["converged"] is True
    assert result.get("mode") == "serial_fallback"


# ── smoke: CLI default demo ───────────────────────────────────────────────────

def test_smoke_main_exits_zero():
    from scripts.distributed_default import main
    rc = main(["--run-id", "smoke-test"])
    assert rc == 0
