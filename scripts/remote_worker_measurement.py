#!/usr/bin/env python3
"""remote_worker_measurement — CLI for the LOCAL_ONLY/REMOTE_READY/REMOTE_MEASURED
tri-state that `scripts/doctor.py` reports for the remote-worker capability (#286).

  status   print the current tri-state (JSON with --json)
  record   actually re-run an accepted cross-process proof and, ONLY if it genuinely
           passes, write the REMOTE_MEASURED receipt doctor.py reads
  clear    delete the receipt to force a fresh re-proof next time

See `simplicio_loop/remote_worker_measurement.py` for the full contract and
`docs/REMOTE_WORKER_RUNBOOK.md` for the operational story.

Usage:
  python3 scripts/remote_worker_measurement.py status [--json]
  python3 scripts/remote_worker_measurement.py record [--proof tests/test_remote_worker_http_e2e.py]
  python3 scripts/remote_worker_measurement.py record --proof physical-two-machine --note "..."
  python3 scripts/remote_worker_measurement.py clear
"""
import argparse
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from simplicio_loop.remote_worker_measurement import (  # noqa: E402
    ACCEPTED_PROOFS, DEFAULT_PROOF, clear_measurement, record_measurement,
    remote_worker_status, run_proof,
)


def cmd_status(args):
    status = remote_worker_status(REPO)
    if args.json:
        print(json.dumps(status, indent=2, sort_keys=True))
    else:
        print("remote-worker status: %s" % status["status"])
        print("configured: %s" % status["configured"])
        if status["measurement"]:
            m = status["measurement"]
            print("measured by: %s at %s (git_sha=%s, host=%s)"
                  % (m.get("proof"), m.get("measured_at"), m.get("git_sha", "")[:12], m.get("host")))
    return 0


def cmd_record(args):
    proof = args.proof
    if proof not in ACCEPTED_PROOFS:
        print("proof %r is not recognized; accepted: %s" % (proof, ", ".join(ACCEPTED_PROOFS)), file=sys.stderr)
        return 2
    if proof == "physical-two-machine":
        if not args.note:
            print("recording physical-two-machine requires --note describing the two real devices "
                  "and how the proof was observed", file=sys.stderr)
            return 2
        record_measurement(REPO, proof=proof, extra={"note": args.note})
        print("recorded REMOTE_MEASURED via physical-two-machine")
        return 0
    result = run_proof(REPO, proof, timeout=args.timeout)
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    if result.returncode != 0:
        print("proof %s did NOT pass (rc=%d) -- measurement NOT recorded" % (proof, result.returncode),
              file=sys.stderr)
        return result.returncode
    record_measurement(REPO, proof=proof)
    print("proof %s passed -- recorded REMOTE_MEASURED" % proof)
    return 0


def cmd_clear(args):
    removed = clear_measurement(REPO)
    print("measurement cleared" if removed else "no measurement to clear")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="remote_worker_measurement",
                                  description="tri-state remote-worker capability measurement (#286)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_status = sub.add_parser("status", help="print the current tri-state")
    p_status.add_argument("--json", action="store_true")
    p_status.set_defaults(func=cmd_status)

    p_record = sub.add_parser("record", help="re-run a proof for real; record only if it passes")
    p_record.add_argument("--proof", default=DEFAULT_PROOF, choices=ACCEPTED_PROOFS)
    p_record.add_argument("--note", default="", help="required for --proof physical-two-machine")
    p_record.add_argument("--timeout", type=int, default=300)
    p_record.set_defaults(func=cmd_record)

    p_clear = sub.add_parser("clear", help="delete the measurement receipt")
    p_clear.set_defaults(func=cmd_clear)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
