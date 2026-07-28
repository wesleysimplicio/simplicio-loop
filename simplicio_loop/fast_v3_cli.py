"""JSON-first CLI adapter for the Fast V3 delivery runner."""
import argparse
import hashlib
import json
import subprocess
from pathlib import Path
from .fast_v3_delivery import Budget, DeliveryRun, FastV3Runner


class CommandVerifier:
    """Run explicit argv commands without a shell and return auditable evidence."""

    def __init__(self, focused, full, *, cwd, timeout):
        if not focused or not full:
            raise ValueError("both focused and full verifier commands are required")
        self.commands = {"focused": list(focused), "full": list(full)}
        self.cwd, self.timeout = str(cwd), timeout

    def __call__(self, scope):
        argv = self.commands.get(scope)
        if not argv:
            return {"ok": False, "scope": scope, "reason": "verifier_missing"}
        try:
            completed = subprocess.run(
                argv, cwd=self.cwd, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                timeout=self.timeout, shell=False, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            return {"ok": False, "scope": scope, "reason": "verifier_execution_failed",
                    "error": str(exc)}
        evidence = {
            "scope": scope, "argv": argv, "exit_code": completed.returncode,
            "stdout_tail": (completed.stdout or "")[-4000:],
            "stderr_tail": (completed.stderr or "")[-4000:],
        }
        evidence["evidence_hash"] = hashlib.sha256(
            json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        evidence["ok"] = completed.returncode == 0
        return evidence


class JsonCommandAdapter:
    """Concrete JSON stdin/stdout adapter for decision, Dev apply and Runtime auth."""

    def __init__(self, argv, *, cwd, timeout, phase):
        if not argv or not all(isinstance(x, str) and x for x in argv):
            raise ValueError("%s command is required" % phase)
        self.argv, self.cwd, self.timeout, self.phase = list(argv), str(cwd), timeout, phase

    def __call__(self, payload):
        argv = list(self.argv)
        if self.phase == "apply":
            argv.append("--dry-run" if payload.get("dry_run") else "--apply")
        encoded = json.dumps(payload, sort_keys=True)
        try:
            completed = subprocess.run(
                argv, cwd=self.cwd, input=encoded, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, timeout=self.timeout,
                shell=False, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            return {"ok": False, "phase": self.phase, "reason": "adapter_execution_failed",
                    "error": str(exc)}
        evidence = {"phase": self.phase, "argv": argv, "exit_code": completed.returncode,
                    "stderr_tail": (completed.stderr or "")[-4000:]}
        evidence["evidence_hash"] = hashlib.sha256(
            json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if completed.returncode != 0:
            return {**evidence, "ok": False, "reason": "adapter_red"}
        try:
            result = json.loads(completed.stdout or "{}")
        except json.JSONDecodeError:
            return {**evidence, "ok": False, "reason": "adapter_invalid_json"}
        if not isinstance(result, dict):
            return {**evidence, "ok": False, "reason": "adapter_invalid_shape"}
        # Success must be asserted by the external adapter, never inferred from exit 0.
        return {**result, **evidence, "ok": result.get("ok") is True}


def main(argv=None):
    from .fast_integration import FastConfig, FastLoopIntegration
    p = argparse.ArgumentParser(prog="simplicio-loop fast-v3")
    p.add_argument("--repo", default=".")
    p.add_argument("--task", required=True)
    p.add_argument("--acceptance", action="append", required=True)
    p.add_argument("--commit", required=True)
    p.add_argument("--generation", required=True)
    p.add_argument("--engine", choices=("auto", "rust", "python", "off"), default="auto")
    p.add_argument("--verify-only", action="store_true")
    p.add_argument("--full", action="store_true")
    p.add_argument("--focused-command", required=True,
                   help="JSON argv array for focused verification; shell is never used")
    p.add_argument("--full-command", required=True,
                   help="JSON argv array for full verification; shell is never used")
    p.add_argument("--verify-timeout", type=float, default=300.0)
    p.add_argument("--decide-command", default="",
                   help="JSON argv array; receives orient context JSON on stdin")
    p.add_argument("--apply-command", default="",
                   help="JSON argv array; receives plan JSON and --dry-run/--apply")
    p.add_argument("--authorize-command", default="",
                   help="JSON argv array for Runtime authorization in --full mode")
    p.add_argument("--max-attempts", type=int, default=3)
    p.add_argument("--max-tokens", type=int)
    p.add_argument("--max-context-bytes", type=int, default=48000)
    p.add_argument("--receipt", default="")
    a = p.parse_args(argv)
    try:
        focused = json.loads(a.focused_command)
        full_command = json.loads(a.full_command)
    except json.JSONDecodeError as exc:
        p.error("verifier commands must be JSON argv arrays: %s" % exc)
    if not isinstance(focused, list) or not all(isinstance(x, str) and x for x in focused):
        p.error("--focused-command must be a non-empty JSON string array")
    if not isinstance(full_command, list) or not all(isinstance(x, str) and x for x in full_command):
        p.error("--full-command must be a non-empty JSON string array")
    if a.verify_timeout <= 0:
        p.error("--verify-timeout must be positive")
    def command(value, flag):
        if not value:
            return None
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            p.error("%s must be a JSON argv array: %s" % (flag, exc))
        if not isinstance(parsed, list) or not all(isinstance(x, str) and x for x in parsed):
            p.error("%s must be a non-empty JSON string array" % flag)
        return parsed
    decide_command = command(a.decide_command, "--decide-command")
    apply_command = command(a.apply_command, "--apply-command")
    authorize_command = command(a.authorize_command, "--authorize-command")
    if not a.verify_only and (not decide_command or not apply_command):
        p.error("normal mode requires --decide-command and --apply-command")
    if a.full and not authorize_command:
        p.error("--full requires --authorize-command")
    repo = Path(a.repo).resolve()
    def orient(task, budget):
        payload = FastLoopIntegration(repo, config=FastConfig(
            mode="standalone" if a.engine == "off" else "required",
            engine=a.engine, max_bytes=budget)).prepare(task)
        payload.setdefault("provider", "simplicio-fast")
        payload["handles"] = [{"handle": str(x.get("handle") or x.get("id")),
                               "content": x.get("content", "")}
                              for x in payload.get("handles", [])
                              if x.get("handle") or x.get("id")]
        return payload
    run = DeliveryRun(a.task, a.acceptance, str(repo), a.commit, a.generation,
                      Budget(a.max_attempts, a.max_tokens, a.max_context_bytes))
    verifier = CommandVerifier(focused, full_command, cwd=repo, timeout=a.verify_timeout)
    result = FastV3Runner(
        orient=orient, verify=verifier,
        decide=JsonCommandAdapter(decide_command, cwd=repo, timeout=a.verify_timeout,
                                  phase="decide") if decide_command else None,
        apply=JsonCommandAdapter(apply_command, cwd=repo, timeout=a.verify_timeout,
                                 phase="apply") if apply_command else None,
        authorize=JsonCommandAdapter(authorize_command, cwd=repo, timeout=a.verify_timeout,
                                     phase="authorize") if authorize_command else None,
    ).execute(
            run, verify_only=a.verify_only, full=a.full)
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if a.receipt:
        Path(a.receipt).write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if result["sealed"] else 2
