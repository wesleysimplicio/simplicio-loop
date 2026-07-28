# Prism compatibility matrix

The installed `simplicio-loop` bundle has one honest baseline: Python 3.11 or
newer. This follows the strictest direct dependency (`simplicio-fast`) and the
native `asyncio.TaskGroup` scheduler. A resolver must reject Python 3.8-3.10
before installation.

| Component | Distribution floor | Python floor | Source branch policy | Runtime role |
| --- | --- | --- | --- | --- |
| Loop | `simplicio-loop>=3.38.5` | 3.11 | `main` | scheduling, ownership, reducer, completion |
| Mapper | `simplicio-mapper>=0.19.0` | 3.10 individually; 3.11 in bundle | `main` | facts, dependencies, conflicts |
| Dev CLI | `simplicio-cli>=0.16.3` | 3.10 individually; 3.11 in bundle | `main` | bounded external effects and receipts |
| Fast | `simplicio-fast>=2.0.14` | 3.11 | `master` | mmap snapshots, overlays, hot paths |
| Runtime | optional capability | implementation-specific | `main` | preferred acceleration when healthy |

The Fast `master` branch is intentional compatibility metadata. No branch is
renamed or deleted. Reproducible executions use the gitlink SHAs recorded in
`components/submodules.json`; branch tips never select run code.

## Gates

```bash
python3 scripts/prism_integrity.py --json
python3 scripts/submodules.py verify
python3 scripts/version_sync.py check
python3 scripts/install_smoke.py run --expected-version 3.38.5
```

`prism_integrity.py` blocks Python, dependency floor, version surface, branch,
shallow-clone, manifest SHA, and gitlink drift with machine-readable reason
codes. The ecosystem doctor additionally reports Python, platform, ABI and
component/schema capability compatibility.

The package is pure Python and targets Linux, macOS and Windows. Platform claims
become release evidence only after the clean-install jobs for those systems
pass; an unavailable observation is reported as unavailable, never inferred.
