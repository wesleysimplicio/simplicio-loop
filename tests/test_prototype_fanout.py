from __future__ import annotations

import os
import sys
import time

import pytest

from simplicio_loop.prototype_gate import apply_decision, build_decision, build_plan, init_state
from simplicio_loop.prototype_fanout import (
    CandidateRunResult,
    CandidateSpec,
    LocalSubprocessExecutor,
    RuntimeSandboxExecutor,
    TERMINAL_STATUSES,
    build_candidate_from_run,
    candidate_run_evidence,
    dispatch_candidates,
)

PY = sys.executable


def _plan(level="P1"):
    return build_plan(
        work_item_id="wi-568-fanout", goal="fan out N prototype candidates", prototype_type="code_spike",
        source_sha="deadbeef", level=level,
    )


# --- LocalSubprocessExecutor: unit coverage ----------------------------------------------------


def test_successful_run_captures_artifacts():
    executor = LocalSubprocessExecutor()
    candidate = CandidateSpec(
        candidate_id="cand-ok",
        commands=[[PY, "-c", "open('out.txt', 'w').write('hello')"]],
    )
    result = executor.execute(candidate)
    assert result.status == "ok"
    assert result.exit_code == 0
    assert result.artifacts == ["out.txt"]
    assert result.error == ""
    # Default keep_workdir=False: the temp dir must be cleaned up, not dangling on disk.
    assert result.workdir == ""


def test_failing_command_captures_exit_code_and_stderr():
    executor = LocalSubprocessExecutor()
    candidate = CandidateSpec(
        candidate_id="cand-fail",
        commands=[[PY, "-c", "import sys; sys.stderr.write('boom'); sys.exit(3)"]],
    )
    result = executor.execute(candidate)
    assert result.status == "failed"
    assert result.exit_code == 3
    assert "boom" in result.stderr


def test_timeout_is_enforced_and_does_not_hang():
    executor = LocalSubprocessExecutor()
    candidate = CandidateSpec(
        candidate_id="cand-timeout",
        commands=[[PY, "-c", "import time; time.sleep(5)"]],
        timeout_s=0.3,
    )
    start = time.monotonic()
    result = executor.execute(candidate)
    elapsed = time.monotonic() - start
    assert result.status == "timeout"
    assert elapsed < 4.0  # proves the timeout actually fired instead of waiting out the sleep


def test_crash_on_unknown_command_is_captured_not_raised():
    executor = LocalSubprocessExecutor()
    candidate = CandidateSpec(candidate_id="cand-crash", commands=[["this-binary-does-not-exist-xyz"]])
    result = executor.execute(candidate)
    assert result.status == "crashed"
    assert result.error


def test_candidate_with_no_commands_crashes_honestly():
    executor = LocalSubprocessExecutor()
    candidate = CandidateSpec(candidate_id="cand-empty", commands=[])
    result = executor.execute(candidate)
    assert result.status == "crashed"


def test_concurrent_candidates_do_not_share_or_corrupt_temp_dirs():
    executor = LocalSubprocessExecutor(keep_workdir=True)
    candidates = [
        CandidateSpec(candidate_id=f"cand-{i}", commands=[[PY, "-c", "open('marker.txt', 'w').write(str(id(object())))"]])
        for i in range(6)
    ]
    plan = _plan()
    report = dispatch_candidates(plan, candidates, executor=executor, max_concurrency=4)
    workdirs = [r.workdir for r in report.results]
    assert len(set(workdirs)) == len(workdirs)  # every candidate got its own directory
    for workdir in workdirs:
        assert os.path.isdir(workdir)
        assert os.path.isfile(os.path.join(workdir, "marker.txt"))
    for workdir in workdirs:
        import shutil
        shutil.rmtree(workdir, ignore_errors=True)


def test_seed_files_are_written_before_commands_run():
    executor = LocalSubprocessExecutor()
    candidate = CandidateSpec(
        candidate_id="cand-seed",
        commands=[[PY, "-c", "print(open('input.txt').read())"]],
        seed_files={"input.txt": "seeded-content"},
    )
    result = executor.execute(candidate)
    assert result.status == "ok"
    assert "seeded-content" in result.stdout


def test_all_terminal_statuses_are_recognized():
    for status in TERMINAL_STATUSES:
        result = CandidateRunResult(
            candidate_id="x", status=status, exit_code=0, stdout="", stderr="",
            duration_ms=0.0, artifacts=[], workdir="",
        )
        assert result.status == status
    with pytest.raises(ValueError):
        CandidateRunResult(
            candidate_id="x", status="not-a-real-status", exit_code=0, stdout="", stderr="",
            duration_ms=0.0, artifacts=[], workdir="",
        )


def test_candidate_spec_rejects_empty_id():
    with pytest.raises(ValueError):
        CandidateSpec(candidate_id="", commands=[["true"]])


# --- RuntimeSandboxExecutor: documented plug point, not live -----------------------------------


def test_runtime_sandbox_executor_is_not_live_and_says_so():
    executor = RuntimeSandboxExecutor()
    candidate = CandidateSpec(candidate_id="cand-rt", commands=[["true"]])
    with pytest.raises(NotImplementedError) as excinfo:
        executor.execute(candidate)
    assert "simplicio prototype run" in str(excinfo.value)


# --- dispatch_candidates: fan-out orchestration -------------------------------------------------


def test_fanout_of_mixed_outcomes_completes_independently():
    plan = _plan()
    candidates = [
        CandidateSpec(candidate_id="a-ok", commands=[[PY, "-c", "pass"]]),
        CandidateSpec(candidate_id="b-fail", commands=[[PY, "-c", "import sys; sys.exit(1)"]]),
        CandidateSpec(candidate_id="c-timeout", commands=[[PY, "-c", "import time; time.sleep(5)"]], timeout_s=0.3),
        CandidateSpec(candidate_id="d-ok", commands=[[PY, "-c", "open('f.txt','w').write('x')"]]),
    ]
    report = dispatch_candidates(plan, candidates, max_concurrency=4)
    by_id = {r.candidate_id: r for r in report.results}
    assert len(report.results) == 4
    assert by_id["a-ok"].status == "ok"
    assert by_id["b-fail"].status == "failed"
    assert by_id["c-timeout"].status == "timeout"
    assert by_id["d-ok"].status == "ok"
    summary = report.summary()
    assert summary["total"] == 4
    assert summary["by_status"]["ok"] == 2
    assert summary["by_status"]["failed"] == 1
    assert summary["by_status"]["timeout"] == 1
    assert summary["plan_hash"] == plan["plan_hash"]


def test_fanout_respects_bounded_concurrency():
    # A worker that records how many run *concurrently* by touching a shared counter file.
    # With max_concurrency=2, the observed peak concurrency must never exceed 2.
    import tempfile

    counter_dir = tempfile.mkdtemp(prefix="fanout-concurrency-")
    script = (
        "import os, time, uuid\n"
        f"d = {counter_dir!r}\n"
        "marker = os.path.join(d, str(uuid.uuid4()) + '.marker')\n"
        "open(marker, 'w').close()\n"
        "time.sleep(0.2)\n"
        "peak = len([n for n in os.listdir(d) if n.endswith('.marker')])\n"
        "open(marker + '.peak', 'w').write(str(peak))\n"
        "os.remove(marker)\n"
    )
    candidates = [CandidateSpec(candidate_id=f"cand-{i}", commands=[[PY, "-c", script]]) for i in range(6)]
    plan = _plan()
    report = dispatch_candidates(plan, candidates, max_concurrency=2)
    assert all(r.status == "ok" for r in report.results)
    peaks = []
    for name in os.listdir(counter_dir):
        if name.endswith(".peak"):
            with open(os.path.join(counter_dir, name)) as fh:
                peaks.append(int(fh.read().strip()))
    assert peaks, "expected at least one peak sample"
    assert max(peaks) <= 2
    import shutil
    shutil.rmtree(counter_dir, ignore_errors=True)


def test_fanout_with_no_candidates_returns_empty_report():
    plan = _plan()
    report = dispatch_candidates(plan, [])
    assert report.results == []
    assert report.summary()["total"] == 0


def test_a_custom_executor_that_raises_does_not_take_down_the_batch():
    class FlakyExecutor:
        def execute(self, candidate: CandidateSpec) -> CandidateRunResult:
            if candidate.candidate_id == "boom":
                raise RuntimeError("simulated crash inside a custom executor")
            return CandidateRunResult(
                candidate_id=candidate.candidate_id, status="ok", exit_code=0, stdout="", stderr="",
                duration_ms=1.0, artifacts=[], workdir="",
            )

    plan = _plan()
    candidates = [
        CandidateSpec(candidate_id="boom", commands=[["true"]]),
        CandidateSpec(candidate_id="fine-1", commands=[["true"]]),
        CandidateSpec(candidate_id="fine-2", commands=[["true"]]),
    ]
    report = dispatch_candidates(plan, candidates, executor=FlakyExecutor(), max_concurrency=3)
    by_id = {r.candidate_id: r for r in report.results}
    assert by_id["boom"].status == "crashed"
    assert "simulated crash" in by_id["boom"].error
    assert by_id["fine-1"].status == "ok"
    assert by_id["fine-2"].status == "ok"


# --- killing one candidate mid-run doesn't block/prevent others (stall/crash recovery) ---------


def test_killed_candidate_does_not_prevent_others_from_completing():
    # Self-signals SIGKILL mid-run to simulate an external kill of the process. subprocess.run
    # surfaces this as a non-zero/negative returncode -- a real terminal "failed" result -- and
    # must not affect any sibling candidate running concurrently in the same fan-out.
    kill_script = "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"
    plan = _plan()
    candidates = [
        CandidateSpec(candidate_id="killed", commands=[[PY, "-c", kill_script]]),
        CandidateSpec(candidate_id="survivor-1", commands=[[PY, "-c", "open('ok.txt','w').write('1')"]]),
        CandidateSpec(candidate_id="survivor-2", commands=[[PY, "-c", "open('ok.txt','w').write('2')"]]),
    ]
    report = dispatch_candidates(plan, candidates, max_concurrency=3)
    by_id = {r.candidate_id: r for r in report.results}
    assert len(report.results) == 3
    assert by_id["killed"].status == "failed"
    assert by_id["killed"].exit_code != 0
    assert by_id["survivor-1"].status == "ok"
    assert by_id["survivor-2"].status == "ok"


# --- wiring fan-out results into the state machine (real receipts) ------------------------------


def test_build_candidate_from_run_reflects_real_execution_status():
    plan = _plan()
    ok_result = CandidateRunResult(
        candidate_id="cand-ok", status="ok", exit_code=0, stdout="", stderr="",
        duration_ms=12.3, artifacts=["out.txt"], workdir="",
    )
    candidate = build_candidate_from_run(plan=plan, result=ok_result, strategy="direct", agent_id="agent-1")
    assert candidate["status"] == "validated"
    assert candidate["plan_hash"] == plan["plan_hash"]
    assert candidate["evidence_refs"] == ["out.txt"]

    failed_result = CandidateRunResult(
        candidate_id="cand-fail", status="timeout", exit_code=None, stdout="", stderr="",
        duration_ms=999.0, artifacts=[], workdir="", error="wall-clock timeout",
    )
    rejected_candidate = build_candidate_from_run(
        plan=plan, result=failed_result, strategy="direct", agent_id="agent-1",
    )
    assert rejected_candidate["status"] == "rejected"
    assert "timeout" in rejected_candidate["terminal_reason"]


def test_build_candidate_from_run_hash_is_derived_from_real_result_not_fabricated():
    plan = _plan()
    result_a = CandidateRunResult(
        candidate_id="cand-1", status="ok", exit_code=0, stdout="", stderr="",
        duration_ms=1.0, artifacts=["a.txt"], workdir="",
    )
    result_b = CandidateRunResult(
        candidate_id="cand-1", status="failed", exit_code=1, stdout="", stderr="",
        duration_ms=1.0, artifacts=["a.txt"], workdir="",
    )
    candidate_a = build_candidate_from_run(plan=plan, result=result_a, strategy="direct", agent_id="agent-1")
    candidate_b = build_candidate_from_run(plan=plan, result=result_b, strategy="direct", agent_id="agent-1")
    # Two different outcomes for the "same" candidate id must produce two different hashes --
    # the hash is bound to what actually happened, not a static planning-time value.
    assert candidate_a["artifact_hash"] != candidate_b["artifact_hash"]


def test_decision_can_reference_a_real_fanout_receipt():
    plan = _plan()
    state = init_state(work_item_id="wi-568-fanout", plan=plan)
    ok_result = CandidateRunResult(
        candidate_id="cand-1", status="ok", exit_code=0, stdout="", stderr="",
        duration_ms=5.0, artifacts=["out.txt"], workdir="",
    )
    candidate = build_candidate_from_run(plan=plan, result=ok_result, strategy="direct", agent_id="agent-1")
    decision = build_decision(
        plan=plan, candidate_hash=candidate["candidate_hash"], decision="ACCEPT",
        reason="fanout candidate executed successfully with real evidence",
        ac_coverage=candidate_run_evidence(ok_result),
    )
    new_state = apply_decision(state, plan=plan, decision=decision, candidate_hash=candidate["candidate_hash"])
    assert new_state["status"] == "in_progress"  # P1 ACCEPT promotes to P2, not yet resolved
    assert new_state["current_level"] == "P2"
    assert new_state["history"][-1]["decision"] == "ACCEPT"


def test_full_integration_fanout_of_four_mixed_candidates():
    plan = _plan()
    candidates = [
        CandidateSpec(candidate_id="pass-1", commands=[[PY, "-c", "open('r.txt','w').write('ok')"]]),
        CandidateSpec(candidate_id="pass-2", commands=[[PY, "-c", "open('r.txt','w').write('ok')"]]),
        CandidateSpec(candidate_id="fail-1", commands=[[PY, "-c", "import sys; sys.exit(2)"]]),
        CandidateSpec(candidate_id="hang-1", commands=[[PY, "-c", "import time; time.sleep(5)"]], timeout_s=0.3),
    ]
    report = dispatch_candidates(plan, candidates, max_concurrency=4)
    summary = report.summary()
    assert summary["total"] == 4
    assert summary["by_status"] == {"ok": 2, "failed": 1, "timeout": 1}
    # Every candidate independently produces a receipt-bindable payload.
    receipts = [
        build_candidate_from_run(plan=plan, result=r, strategy="mixed-fanout", agent_id="agent-x")
        for r in report.results
    ]
    assert len(receipts) == 4
    assert sum(1 for r in receipts if r["status"] == "validated") == 2
    assert sum(1 for r in receipts if r["status"] == "rejected") == 2
