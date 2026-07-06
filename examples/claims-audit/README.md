# Example: Executable prose — claims audit (#97)

Demonstrates how `claims_audit.py` validates doc-cited worker commands against real CLIs.

## Usage

```bash
# Run the full claims audit (includes check 9: prose-commands-valid)
python3 scripts/claims_audit.py

# Run only the prose validation
python3 scripts/claims_audit.py --only 9

# Check if a specific worker supports --describe-cli
python3 scripts/task_anchor.py --describe-cli
python3 scripts/loop_journal.py --describe-cli
python3 scripts/savings_harness.py --describe-cli
python3 scripts/pr_evidence.py --describe-cli
python3 scripts/e2e_demo.py --describe-cli
```

## How it works

Workers that export `--describe-cli` emit a JSON spec of accepted verbs and flags.
The audit check 9 extracts code-block invocations from SKILL.md and references/*.md,
then validates each flag/verb against the real CLI spec.

A divergence (e.g. doc shows `--format toon` but the worker doesn't accept it) is
reported with file:line so it can be fixed before merge.
