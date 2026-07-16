"""Tests for simplicio_loop.stage_agent_coordinator (issue #424, epic #422).

Builds on the merged #423 contract (PR #435): simplicio_loop.stage_agents'
load_graph/validate_graph/validate_instance/validate_receipt over
contracts/stage-agents/v1/stages.json (roles: intake_planner,
implementation_agent, safety_gate, review_panel, delivery_agent,
feedback_recovery_agent, completion_auditor, github_reporter; stages: intake,
planning, executing, validating, watching, delivering, done).
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from simplicio_loop import stage_agent_coordinator as sc  # noqa: E402
from simplicio_loop import stage_agents as sa  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
ECHO_AGENT = REPO_ROOT / "contracts" / "stage-agents" / "v1" / "adapter-fixtures" / "echo_agent.py"


# --------------------------------------------------------------------------
# Fakes for unit-level adapter/registry/waves tests.
# --------------------------------------------------------------------------


def _ops_dict(ops):
    return {"spawn": ops.spawn, "send": ops.send, "poll": ops.poll,
            "cancel": ops.cancel, "collect": ops.collect}


class FakeNativeOps:
    """In-memory fake of a native host binding: spawn/send/poll/cancel."""

    def __init__(self, *, ready_after_polls: int = 1):
        self.instances: dict[str, dict] = {}
        self.ready_after_polls = ready_after_polls
        self._poll_counts: dict[str, int] = {}

    def spawn(self, *, role, stage, stage_context):
        instance_id = f"native-{len(self.instances) + 1}"
        self.instances[instance_id] = {
            "status": "created", "runtime": "fake-native", "provider": "fake", "model": "fake-model",
        }
        self._poll_counts[instance_id] = 0
        return instance_id

    def poll(self, instance_id):
        self._poll_counts[instance_id] += 1
        state = self.instances[instance_id]
        if state["status"] == "created" and self._poll_counts[instance_id] >= self.ready_after_polls:
            state["status"] = "ready"
        return dict(state, heartbeat_at=time.time())

    def send(self, instance_id, stage_input):
        self.instances[instance_id]["status"] = "running"
        self.instances[instance_id]["stage_input"] = stage_input

    def collect(self, instance_id):
        state = self.instances[instance_id]
        ctx = state["stage_input"]
        output = {"summary": "native fake output"}
        receipt = _receipt_from_context(ctx)
        return output, receipt

    def cancel(self, instance_id, *, reason):
        self.instances[instance_id]["status"] = "cancelled"

    def finish(self, instance_id):
        self.instances[instance_id]["status"] = "passed"


class FakeQueueClient:
    def __init__(self):
        self._leases: dict[str, dict] = {}

    def claim(self, *, role, stage, context):
        lease_id = f"lease-{len(self._leases) + 1}"
        self._leases[lease_id] = {"status": "ready", "context": context}
        return {"lease_id": lease_id}

    def status(self, lease_id):
        return {"status": self._leases[lease_id]["status"], "heartbeat_at": time.time()}

    def send(self, lease_id, stage_input):
        self._leases[lease_id]["status"] = "running"
        self._leases[lease_id]["input"] = stage_input

    def collect(self, lease_id):
        ctx = self._leases[lease_id]["input"]
        return {"output": {"summary": "queue fake output"}, "receipt": _receipt_from_context(ctx)}

    def cancel(self, lease_id, *, reason):
        self._leases[lease_id]["status"] = "cancelled"

    def finish(self, lease_id):
        self._leases[lease_id]["status"] = "passed"


def _receipt_from_context(ctx: dict) -> dict:
    return {
        "schema": "simplicio.stage-receipt/v1",
        "receipt_id": f"receipt-{ctx['attempt_id']}",
        "agent_instance_id": ctx["agent_instance_id"],
        "role_id": ctx["role_id"],
        "stage_id": ctx["stage_id"],
        "run_id": ctx["run_id"],
        "task_id": ctx["task_id"],
        "attempt_id": ctx["attempt_id"],
        "fence": ctx["fence"],
        "plan_revision": ctx["plan_revision"],
        "created_at": "2026-07-16T00:00:00Z",
        "verdict": "pass",
        "evidence_refs": ["fake-fixture"],
        "accepted": True,
    }


ROLE = {"role_id": "implementation_agent", "version": "1.0.0"}
STAGE_NONE = {"stage_id": "executing", "role_id": "implementation_agent", "isolation_level": "process", "timeout_seconds": 5}
STAGE_HUMAN = {"stage_id": "delivering", "role_id": "delivery_agent", "isolation_level": "human", "timeout_seconds": 5}


# --------------------------------------------------------------------------
# AdapterRegistry / fallback order.
# --------------------------------------------------------------------------


def test_registry_selects_native_when_available():
    ops = FakeNativeOps()
    registry = sc.AdapterRegistry([sc.NativeAgentAdapter(native_ops=_ops_dict(ops))])
    adapter = registry.select(role=ROLE, stage=STAGE_NONE)
    assert adapter.kind == "native"


def test_registry_falls_back_to_command_when_native_unavailable(tmp_path):
    native = sc.NativeAgentAdapter(native_ops={})  # no ops bound -> probe() False
    command = sc.CommandAgentAdapter(command=[sys.executable, str(ECHO_AGENT), "{input}", "{output}", "{receipt}"])
    registry = sc.AdapterRegistry([native, command])
    adapter = registry.select(role=ROLE, stage=STAGE_NONE)
    assert adapter.kind == "command"


def test_registry_blocks_with_stable_reason_code_when_nothing_compatible():
    registry = sc.AdapterRegistry([sc.NativeAgentAdapter(native_ops={})])
    with pytest.raises(sc.StageCoordinatorError) as excinfo:
        registry.select(role=ROLE, stage=STAGE_NONE)
    assert excinfo.value.reason_code == sc.REASON_NO_COMPATIBLE_ADAPTER


def test_registry_routes_human_stage_only_to_human_gate():
    registry = sc.AdapterRegistry([sc.HumanGateAdapter()])
    adapter = registry.select(role={"role_id": "delivery_agent"}, stage=STAGE_HUMAN)
    assert adapter.kind == "human"


def test_command_adapter_never_matches_human_isolation():
    command = sc.CommandAgentAdapter(command=[sys.executable, str(ECHO_AGENT)])
    assert command.compatible_with(ROLE, STAGE_HUMAN) is False


# --------------------------------------------------------------------------
# Waves / capacity.
# --------------------------------------------------------------------------


def test_plan_waves_groups_independent_stages_together():
    graph = sa.load_graph()
    waves = sc.plan_waves(graph)
    assert waves[0] == ["intake"]
    assert waves[1] == ["planning"]
    assert waves[2] == ["executing"]
    # every stage after "executing" is a serial chain in the canonical graph
    flat = [sid for wave in waves for sid in wave]
    assert flat.index("planning") < flat.index("executing") < flat.index("validating")


def test_available_slots_never_goes_negative():
    assert sc.available_slots(host_total_slots=1, coordinator_slots=4) == 0
    assert sc.available_slots(host_total_slots=4, coordinator_slots=1) == 3


# --------------------------------------------------------------------------
# NativeAgentAdapter: never assumes accepted spawn == ready.
# --------------------------------------------------------------------------


def test_native_adapter_requires_observed_ready_before_send():
    ops = FakeNativeOps(ready_after_polls=3)
    adapter = sc.NativeAgentAdapter(native_ops=_ops_dict(ops))
    instance = adapter.spawn(role=ROLE, stage=STAGE_NONE, stage_context={})
    assert instance.status == "created"
    adapter.poll(instance)
    assert instance.status == "created"  # still not ready after first poll
    with pytest.raises(sc.StageCoordinatorError) as excinfo:
        adapter.send(instance, {"x": 1})
    assert excinfo.value.reason_code == sc.REASON_NOT_READY


def test_native_adapter_captures_runtime_provider_model():
    ops = FakeNativeOps(ready_after_polls=1)
    adapter = sc.NativeAgentAdapter(native_ops=_ops_dict(ops))
    instance = adapter.spawn(role=ROLE, stage=STAGE_NONE, stage_context={})
    adapter.poll(instance)
    assert instance.status == "ready"
    assert instance.runtime == "fake-native"
    assert instance.provider == "fake"
    assert instance.model == "fake-model"


# --------------------------------------------------------------------------
# CommandAgentAdapter: real subprocess, no shell interpolation.
# --------------------------------------------------------------------------


def test_command_adapter_argv_has_no_shell_metacharacter_risk():
    adapter = sc.CommandAgentAdapter(command=[sys.executable, str(ECHO_AGENT), "{input}", "{output}", "{receipt}"])
    argv = adapter._render_argv(
        attempt_dir=Path("/tmp/x"), input_path=Path("/tmp/x/in.json"), output_path=Path("/tmp/x/out.json"),
        receipt_path=Path("/tmp/x/receipt.json"), role=ROLE, stage=STAGE_NONE,
    )
    assert argv[0] == sys.executable
    assert "{input}" not in " ".join(argv)


def test_command_adapter_end_to_end_runs_real_process(tmp_path):
    """Integration: a real subprocess (the echo_agent fixture) runs, is
    observed READY only once the OS has scheduled it, and produces a
    collectible stage_output + stage_receipt — never a fabricated pass."""
    adapter = sc.CommandAgentAdapter(
        command=[sys.executable, str(ECHO_AGENT), "{input}", "{output}", "{receipt}"],
        base_tmp_dir=tmp_path,
    )
    stage = dict(STAGE_NONE, timeout_seconds=30)
    stage_context = {
        "role_id": "implementation_agent", "stage_id": "executing", "run_id": "run-1",
        "task_id": "task-1", "attempt_id": "attempt-1", "fence": "fence-1",
        "plan_revision": 0, "isolation_level": "process", "agent_instance_id": "placeholder",
    }
    instance = adapter.spawn(role=ROLE, stage=stage, stage_context=stage_context)
    assert instance.status == "created"
    stage_context["agent_instance_id"] = instance.instance_id

    deadline = time.time() + 15
    while instance.status == "created" and time.time() < deadline:
        adapter.poll(instance)
    assert instance.status in ("ready", "running")

    adapter.send(instance, stage_context)
    assert instance.status == "running"

    while instance.status not in sc.TERMINAL_DRIVER_STATUSES and time.time() < deadline:
        adapter.poll(instance)
        time.sleep(0.02)

    assert instance.status == "passed"
    output, receipt = adapter.collect(instance)
    assert output is not None
    assert receipt["verdict"] == "pass"
    assert receipt["stage_id"] == "executing"
    assert receipt["agent_instance_id"] == instance.instance_id


def test_command_adapter_timeout_kills_process(tmp_path):
    sleeper = tmp_path / "sleeper.py"
    sleeper.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    adapter = sc.CommandAgentAdapter(command=[sys.executable, str(sleeper)], base_tmp_dir=tmp_path)
    stage = dict(STAGE_NONE, timeout_seconds=0.2)
    instance = adapter.spawn(role=ROLE, stage=stage, stage_context={})

    deadline = time.time() + 5
    while instance.status not in ("cancelled", "failed", "passed") and time.time() < deadline:
        adapter.poll(instance)
        time.sleep(0.05)
    assert instance.status == "cancelled"
    assert instance.error_reason_code == sc.REASON_TIMEOUT


def test_command_adapter_does_not_inherit_secrets_by_default(monkeypatch, tmp_path):
    monkeypatch.setenv("SUPER_SECRET_TOKEN", "should-not-leak")
    script = tmp_path / "print_env.py"
    script.write_text(
        "import os, sys\n"
        "open(sys.argv[2], 'w').write('leaked' if 'SUPER_SECRET_TOKEN' in os.environ else 'clean')\n",
        encoding="utf-8",
    )
    adapter = sc.CommandAgentAdapter(command=[sys.executable, str(script), "{input}", "{output}"], base_tmp_dir=tmp_path)
    instance = adapter.spawn(role=ROLE, stage=dict(STAGE_NONE, timeout_seconds=10), stage_context={})
    deadline = time.time() + 10
    while instance.status not in sc.TERMINAL_DRIVER_STATUSES and time.time() < deadline:
        adapter.poll(instance)
        time.sleep(0.02)
    proc = adapter._procs[instance.instance_id]
    assert proc.output_path.read_text(encoding="utf-8") == "clean"


# --------------------------------------------------------------------------
# QueueAgentAdapter (fake client).
# --------------------------------------------------------------------------


def test_queue_adapter_full_cycle():
    client = FakeQueueClient()
    adapter = sc.QueueAgentAdapter(queue_client=client)
    stage_context = {
        "role_id": "review_panel", "stage_id": "validating", "run_id": "run-1", "task_id": "task-1",
        "attempt_id": "attempt-1", "fence": "fence-1", "plan_revision": 0, "isolation_level": "worker",
        "agent_instance_id": "placeholder",
    }
    instance = adapter.spawn(role={"role_id": "review_panel"}, stage={"stage_id": "validating"}, stage_context=stage_context)
    stage_context["agent_instance_id"] = instance.instance_id
    adapter.poll(instance)
    assert instance.status == "ready"
    adapter.send(instance, stage_context)
    assert instance.status == "running"
    client.finish(instance.instance_id)
    adapter.poll(instance)
    output, receipt = adapter.collect(instance)
    assert receipt["verdict"] == "pass"


def test_queue_adapter_probe_false_without_client():
    adapter = sc.QueueAgentAdapter(queue_client=None)
    assert adapter.probe() is False


# --------------------------------------------------------------------------
# HumanGateAdapter.
# --------------------------------------------------------------------------


def test_human_gate_times_out_to_blocked_not_pass():
    adapter = sc.HumanGateAdapter(approval_source={})
    instance = adapter.spawn(role={"role_id": "delivery_agent"}, stage=STAGE_HUMAN, stage_context={})
    adapter.send(instance, {})
    adapter.cancel(instance, reason=sc.REASON_TIMEOUT)
    assert instance.status == "blocked"
    output, receipt = adapter.collect(instance)
    assert output is None and receipt is None


def test_human_gate_approval_bound_to_stage_role():
    approvals = {("delivering", "delivery_agent"): {"status": "passed", "output": {}, "receipt": {"verdict": "pass"}}}
    adapter = sc.HumanGateAdapter(approval_source=approvals)
    instance = adapter.spawn(role={"role_id": "delivery_agent"}, stage=STAGE_HUMAN, stage_context={})
    adapter.send(instance, {})
    adapter.poll(instance)
    assert instance.status == "passed"
    output, receipt = adapter.collect(instance)
    assert receipt["verdict"] == "pass"


# --------------------------------------------------------------------------
# StageAgentCoordinator: end-to-end over the real stages.json manifest.
# --------------------------------------------------------------------------


def _coordinator(tmp_path, adapters, **kwargs):
    journal = sc.StageCoordinatorJournal(tmp_path / "journal.jsonl")
    return sc.StageAgentCoordinator(
        run_id="run-1", task_id="task-1", adapters=adapters, journal=journal,
        host_total_slots=kwargs.pop("host_total_slots", 4), coordinator_slots=kwargs.pop("coordinator_slots", 1),
        poll_interval_seconds=0.01,
    )


def _echo_command_adapter(tmp_path):
    return sc.CommandAgentAdapter(
        command=[sys.executable, str(ECHO_AGENT), "{input}", "{output}", "{receipt}"], base_tmp_dir=tmp_path,
    )


def test_coordinator_blocks_when_no_adapter_covers_a_required_role(tmp_path):
    coordinator = _coordinator(tmp_path, adapters=[])  # nothing compatible at all
    result = coordinator.run_stage("intake")
    assert result.status == "blocked"
    assert result.reason_code == sc.REASON_NO_COMPATIBLE_ADAPTER


def test_coordinator_runs_single_stage_via_command_adapter(tmp_path):
    coordinator = _coordinator(tmp_path, adapters=[_echo_command_adapter(tmp_path)])
    result = coordinator.run_stage("intake")
    assert result.status == "passed"
    assert "intake" in coordinator.passed_stages


def test_coordinator_run_all_reaches_terminal_with_command_adapter(tmp_path):
    coordinator = _coordinator(tmp_path, adapters=[_echo_command_adapter(tmp_path)])
    results = coordinator.run_all()
    assert all(r.status == "passed" for r in results.values())
    assert coordinator.terminal_reached()


def test_coordinator_zero_capacity_blocks_rather_than_skips(tmp_path):
    coordinator = _coordinator(tmp_path, adapters=[_echo_command_adapter(tmp_path)], host_total_slots=1, coordinator_slots=1)
    result = coordinator.run_stage("intake")
    assert result.status == "blocked"
    assert result.reason_code == sc.REASON_ZERO_CAPACITY


def test_coordinator_journal_replay_skips_already_passed_stage(tmp_path):
    journal_path = tmp_path / "journal.jsonl"
    journal = sc.StageCoordinatorJournal(journal_path)
    coordinator = sc.StageAgentCoordinator(
        run_id="run-1", task_id="task-1", adapters=[_echo_command_adapter(tmp_path)], journal=journal,
        poll_interval_seconds=0.01,
    )
    first = coordinator.run_stage("intake")
    assert first.status == "passed"

    # Simulate restart: fresh coordinator instance reads the same journal.
    resumed = sc.StageAgentCoordinator(
        run_id="run-1", task_id="task-1", adapters=[_echo_command_adapter(tmp_path)], journal=journal,
        poll_interval_seconds=0.01,
    )
    assert "intake" in resumed.results
    assert resumed.results["intake"].status == "passed"
    # Re-running is idempotent: does not respawn, returns cached result.
    again = resumed.run_stage("intake")
    assert again.status == "passed"


def test_coordinator_rejects_stale_fence_instance(tmp_path):
    instance_record = {
        "schema": "simplicio.agent-instance/v1", "agent_instance_id": "i1", "role_id": "implementation_agent",
        "stage_id": "executing", "run_id": "run-1", "task_id": "task-1", "attempt_id": "a1",
        "fence": "fence-0", "plan_revision": 0, "runtime": "command", "provider": "command", "model": "n/a",
        "driver": "command", "parent_agent_id": "coordinator", "isolation_level": "process",
        "negotiated_capabilities": ["receipts"], "context_hash": "a" * 64, "manifest_hash": "b" * 64,
        "created_at": "2026-07-16T00:00:00Z", "ready_at": "2026-07-16T00:00:00Z",
        "started_at": "2026-07-16T00:00:00Z", "ended_at": "2026-07-16T00:00:01Z",
        "terminal_status": "completed", "reason_code": "ok",
    }
    ok, errors = sa.validate_instance(
        instance_record,
        run_identity={"run_id": "run-1", "task_id": "task-1", "attempt_id": "a1", "fence": "fence-1", "plan_revision": 0},
    )
    assert not ok
    assert any("fence" in e for e in errors)


def test_coordinator_status_report_shape(tmp_path):
    coordinator = _coordinator(tmp_path, adapters=[_echo_command_adapter(tmp_path)])
    coordinator.run_stage("intake")
    report = coordinator.status_report()
    assert report["run_id"] == "run-1"
    assert "intake" in report["passed_stages"]
    assert isinstance(report["terminal_reached"], bool)


# --------------------------------------------------------------------------
# Journal.
# --------------------------------------------------------------------------


def test_journal_append_and_replay(tmp_path):
    journal = sc.StageCoordinatorJournal(tmp_path / "j.jsonl")
    journal.append("instance_created", {"stage_id": "intake", "instance_id": "x1"})
    journal.append("stage_passed", {"stage_id": "intake"})
    events = journal.replay()
    assert len(events) == 2
    assert journal.passed_stage_ids() == {"intake"}


def test_journal_replay_empty_when_no_file(tmp_path):
    journal = sc.StageCoordinatorJournal(tmp_path / "missing.jsonl")
    assert journal.replay() == []
    assert journal.passed_stage_ids() == set()
