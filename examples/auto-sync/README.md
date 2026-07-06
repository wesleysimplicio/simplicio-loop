# Example: Auto-sync pre-commit hook (#98)

Demonstrates how `hooks/pre-commit.py` auto-syncs `plugin/` when source files change.

## Usage

```bash
# The hook auto-runs on `git commit`. To test manually:
python3 hooks/pre-commit.py

# Or simulate what the hook does:
python3 scripts/sync_plugin.py  # sync plugin/ from source

# Check parity
python3 scripts/sync_plugin.py --check

# The claims-audit check 5 (plugin-parity) is the backstop
python3 scripts/claims_audit.py --only 5
```

## How it works

1. Pre-commit detects staged changes to `.claude/skills/`, `hooks/`, `scripts/`
2. Runs `sync_plugin.py` to regenerate `plugin/`
3. Stages the generated files
4. Fail-open: if python3 is unavailable, commit proceeds and `check.py` catches drift
