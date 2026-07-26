# Consumer repository tools

The installed package exposes one stable resolver for the loop's operational scripts. It works
from a consumer repository that does not contain a `scripts/` checkout, and keeps progress,
anchor, journal, and cleanup state in that consumer repository.

```powershell
python -m pip install --upgrade simplicio-loop
Set-Location C:\work\simplicio-agent
simplicio-loop-tools loop_progress render --turn-header
simplicio-loop-tools operator_check maybe-upgrade --json
simplicio-loop-tools task_anchor status --json
```

Use `--repo C:\path\to\consumer` when the current directory is not the target repository. The
resolver rejects unknown script names and reports `resolved_package_path` plus the exact
`python -m pip install --upgrade simplicio-loop` fallback if a wheel is missing one bundled
script.
