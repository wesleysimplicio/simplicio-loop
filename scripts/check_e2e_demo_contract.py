#!/usr/bin/env python3
"""Validate the e2e demo anti-false-completion fixtures against the real audit command.

This is the fixture gate for issue #141's capstone demo contract: a fully-measured receipt set
must pass, while simulated/malformed/duplicate-hop receipts must fail closed. The validator does
not re-describe the logic in prose; it shells out to the real `scripts/e2e_demo.py audit
--require-measured` command for each fixture and compares the actual JSON verdict with the fixture's
expected.json.

Usage:
    python3 scripts/check_e2e_demo_contract.py
    python3 scripts/check_e2e_demo_contract.py selftest
    python3 scripts/check_e2e_demo_contract.py --describe-cli
"""
import json
import os
import subprocess
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
FIXTURES = os.path.join(REPO, "contracts", "e2e-demo", "v1", "fixtures")
AUDIT = os.path.join(REPO, "scripts", "e2e_demo.py")
REQUIRED_FIELDS = ("schema", "hop", "proof", "tokens", "note")


def _load_events(path):
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if s:
                rows.append(json.loads(s))
    return rows


def _structural_errors(rows, name):
    errors = []
    for i, row in enumerate(rows, 1):
        missing = [k for k in REQUIRED_FIELDS if k not in row]
        if missing:
            errors.append("%s: event line %d missing required field(s) %s" % (name, i, missing))
    return errors


def validate():
    if not os.path.isdir(FIXTURES):
        print("check_e2e_demo_contract: FAIL (fixtures dir missing: %s)" % FIXTURES)
        return False
    errors = []
    fixture_names = []
    for name in sorted(os.listdir(FIXTURES)):
        fixture_dir = os.path.join(FIXTURES, name)
        if not os.path.isdir(fixture_dir):
            continue
        fixture_names.append(name)
        events_path = os.path.join(fixture_dir, "events.jsonl")
        expected_path = os.path.join(fixture_dir, "expected.json")
        before = len(errors)
        if not os.path.exists(events_path):
            errors.append("%s: missing events.jsonl" % name)
        if not os.path.exists(expected_path):
            errors.append("%s: missing expected.json" % name)
        if len(errors) != before:
            print("  [XX] %s" % name)
            continue
        rows = _load_events(events_path)
        errors.extend(_structural_errors(rows, name))
        with open(expected_path, encoding="utf-8") as f:
            expected = json.load(f)
        r = subprocess.run(
            [sys.executable, AUDIT, "audit", "--events", events_path, "--require-measured"],
            capture_output=True, text=True, cwd=REPO, timeout=30, stdin=subprocess.DEVNULL,
        )
        try:
            payload = json.loads(r.stdout)
        except ValueError:
            errors.append("%s: audit stdout is not valid JSON: %r" % (name, r.stdout[:200]))
            print("  [XX] %s" % name)
            continue
        want = expected.get("expected", {})
        if r.returncode != want.get("returncode", 0):
            errors.append("%s: returncode=%d want %d" %
                          (name, r.returncode, want.get("returncode", 0)))
        for key, val in want.get("payload_contains", {}).items():
            if payload.get(key) != val:
                errors.append("%s: payload[%r]=%r want %r" %
                              (name, key, payload.get(key), val))
        ok = len(errors) == before
        print("  [%s] %s" % ("ok" if ok else "XX", name))
    if errors:
        print("\ncheck_e2e_demo_contract: FAIL (%d issue(s))" % len(errors))
        for err in errors:
            print("  - %s" % err)
        return False
    print("\ncheck_e2e_demo_contract: PASS (%d fixtures)" % len(fixture_names))
    return True


def main():
    argv = sys.argv[1:]
    if argv and argv[0] == "--describe-cli":
        print(json.dumps({"verbs": ["validate", "selftest"], "flags": ["--describe-cli"]}))
        raise SystemExit(0)
    ok = validate()
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
