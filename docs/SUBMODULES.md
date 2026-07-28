# Reproducible Loop components

`simplicio-loop` coordinates three repositories as Git submodules:

| Component | Checkout | Declared branch | Pinned commit |
| --- | --- | --- | --- |
| `simplicio-mapper` | `components/simplicio-mapper` | `main` | `412d5fbe23188aef3f0bfd1dbe56b867e2ad6f96` |
| `simplicio-dev-cli` | `components/simplicio-dev-cli` | `main` | `229b9b245c1f2dec901d9206a900d750d6520d74` |
| `simplicio-fast` | `components/simplicio-fast` | `master` | `12f337149f908fa1a268fc9f6f8c7dd33b959ff0` |

The exact URLs, source branches observed when the pins were recorded, and policy are in
[`components/submodules.json`](../components/submodules.json). The superproject gitlink is the
only revision used by a run. The helper **never** calls `git submodule update --remote` and never
silently moves to a branch tip.

The branch names in `.gitmodules` are compatibility metadata and are checked
against the manifest. In particular, Fast intentionally remains on `master`;
this policy does not rename or delete any branch. Execution always uses the
reviewed gitlink SHA, never a floating branch head.

## Clone and install

For a full source checkout:

```bash
git clone --recurse-submodules https://github.com/wesleysimplicio/simplicio-loop.git
cd simplicio-loop
python3 scripts/submodules.py verify
bash scripts/install.sh claude       # or another supported runtime
```

For an existing clone, the equivalent is:

```bash
git submodule update --init --recursive
python3 scripts/submodules.py verify
```

`bootstrap` is an explicit, idempotent spelling of that operation:

```bash
python3 scripts/submodules.py bootstrap
python3 scripts/submodules.py status
```

Repeat `bootstrap` or `update` as often as needed. They reconcile the checkout to the SHAs already
committed in the superproject; they do not fetch a newer `main`/`master` tip. A deliberate pin
refresh is a reviewable change to `.gitmodules`, `components/submodules.json`, and the three
gitlinks in a pull request.

## Offline mode and diagnostics

If the objects are already in the local Git object database, use:

```bash
python3 scripts/submodules.py bootstrap --offline
```

An absent submodule, dirty worktree, or unexpected SHA is a hard, actionable failure:

```text
SUBMODULE_ERROR: submodule verification failed:
- simplicio-fast: checkout is diverged (observed <sha>, expected <sha>)
```

`status` is read-only JSON and is safe to use in a run preflight. `verify` is the fail-closed gate.
After successful verification, record the exact component SHAs in the run manifest:

```bash
python3 scripts/submodules.py manifest --output .simplicio/submodules-run.json
```

The manifest is intentionally independent of package installation. Wheels and source distributions
remain installable without a Git checkout or submodule objects; the components are optional source
accelerators, while the Python loop package remains the fallback.

## Windows, macOS and Linux

The implementation is pure Python + Git and uses no POSIX-only APIs. On Windows run the same
commands with `py`/`python`:

```powershell
py scripts/submodules.py bootstrap
py scripts/submodules.py verify
```

The URL and SHA checks are byte-for-byte identical on all three platforms. CI or local gates should
run the parser/unit tests without network access; a provider, LLM, or runtime is never started by
this helper.

## Rollback

Rollback is a normal Git revert of the superproject commit. Do not edit a component in place to
"fix" a pin. Restore the previous superproject revision, then run `python3 scripts/submodules.py
bootstrap`; the three gitlinks return to the previous known-good SHAs.
