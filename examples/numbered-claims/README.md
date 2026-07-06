# Example: Numbered claims (#96)

Demonstrates how quantitative claims are audited against a manifest.

## Usage

```bash
# Run the quantitative claims check
python3 scripts/claims_audit.py --only 8

# View the claims manifest
python3 scripts/claims_manifest.py

# Run the manifest selftest
python3 scripts/claims_manifest.py selftest
```

## Claims manifest

Every quantitative number in README/SKILLs must either:
- Point to a receipt artifact (file path), or
- Be explicitly labelled "unverified" in the text

See `scripts/claims_manifest.py` for the current state. New quantitative claims
must be added to the manifest before `check.py` can pass.

## Current status

All quantitative claims are currently **unverified** (no receipt snapshots exist).
This is the honest starting point — as receipts are produced via `savings_harness.py`
snapshots, claims graduate to "verified".
