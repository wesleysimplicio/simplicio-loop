# Installer mutation inventory (issue #293 §1)

Exact inventory of every disk/PATH/service mutation the installer can perform, which script
performs it, its scope, its reversibility, and how it differs across Windows/macOS/Linux. This is
the "mapear todos os efeitos" + "matriz Windows/macOS/Linux e runtime por runtime" deliverable —
read alongside the machine-checkable plan a `--dry-run` prints
(`contracts/install-transaction/v1/schema.json`, produced by `scripts/install_plan.py`).

## 1. Effect inventory, by source file

| Source | Function | Effect | Scope | Reversible | Consent required |
|---|---|---|---|---|---|
| `install_lib.py` | `copy_skills()` | copies the 6 skills into `<target>/.claude/skills/<skill>` | project/user | yes (delete dir) | no (default-mode effect) |
| `install_lib.py` | `copy_hooks()` | copies `hooks/` into `<target>/hooks` (project) or `<target>/.claude/hooks` (global) | project/user | yes | no |
| `install_lib.py` | `copy_scripts()` | copies `scripts/*.py` (minus `__pycache__`/`*.pyc`) into `<target>/scripts` (project) or `<target>/.claude/scripts` (global) | project/user | yes | no |
| `install_lib.py` | `ensure_entry()` | creates/updates the runtime's entry file (`AGENTS.md`, `GEMINI.md`, `.github/copilot-instructions.md`, `.kiro/steering/simplicio-loop.md`, `CONVENTIONS.md`) between `<!-- simplicio-loop:begin/end -->` markers | project/user | yes (marker-delimited block is removable without touching the rest of the file) | no |
| `install_lib.py` | `merge_claude_hooks()` | merges `Stop` (+ project-local `PreToolUse`) hook entries into `.claude/settings.json` | project/user | yes (JSON merge; existing unrelated keys untouched) | no |
| `install_lib.py` | `install_git_precommit_hook()` | writes `.git/hooks/pre-commit` (only if the target is a git repo and no foreign hook already lives there) | project only | yes (file replace/delete) | no — but a *foreign* existing hook is never overwritten (logged, not clobbered) |
| `install_lib.py` | `ensure_operators()` / `_pip_install()` | `pip install -U simplicio-cli` (the 2 required operator binaries) | **global** Python environment (whatever `sys.executable` resolves to — a venv if active, else the system/user Python) | **not reversible by this installer** (a real `pip uninstall` is a separate, manual step) | **yes for `--break-system-packages`** — only attempted when pip's stderr specifically reports `externally-managed-environment` AND `--allow-break-system-packages` (or `SIMPLICIO_ALLOW_BREAK_SYSTEM_PACKAGES=1`) was explicitly passed (#293 §3 hardening, see below) |
| `install_lib.py` | `_link_console_script()` / `_link_operator_bins()` | symlinks console-scripts (`simplicio-dev-cli`, `simplicio-mapper`, `simplicio-loop`) into `~/.local/bin` when a `--user` pip install dropped them off PATH | user (`~/.local/bin`) | yes (remove symlink) | no (best-effort, never fails the install) |
| `install_lib.py` | `install_all_deps()` | `pip install -U .[onnx]` / `simplicio-loop[onnx]` + tray dep (`pystray`+`pillow` or `rumps` on macOS) — skipped by `--minimal` | global Python environment | not reversible by this installer | same `--allow-break-system-packages` gate as above |
| `install_lib.py` | `setup_monitor()` | registers the always-on capture proxy (`install_services.py install`/`wire` on Linux/Windows, `setup_simplicio.sh` — launchd — on macOS) + opens the Token Monitor dashboard once | **system service scope** (`full-stack`-equivalent effect even under the default non-`--minimal` flow — see "Known gap" below) | services: yes (`install_services.py uninstall`); dashboard open: not applicable (no persistent mutation) | should require `with_service`/`full-stack` consent per the issue's mode contract — **not yet gated this way**, see Known gaps |
| `install_services.py` | `install()` / `wire()` | Linux: writes a `systemd --user` unit; Windows: registers a Startup-folder shim; wiring: edits provider base-URL env for Claude/Codex/Simplicio Agent | user/system, OS-specific | yes (`uninstall()`) | same as above |
| `install_executor.py` | `apply()` | wraps every one of the above FILE effects (skills/hooks/scripts/entry/claude_settings) with a pre-mutation backup + before/after hash + persisted receipt under `<target>/.simplicio/receipts/<id>.json`; automatic rollback of every already-applied step if a later step raises | project/user | yes, byte-for-byte via `rollback()` | governed entirely by the plan's `permissions_required` (see `install_plan.py`) |
| `install_executor.py` | manifest reconciliation (`_stale_skills()` + the `reconcile` step) | removes a skill directory that a PRIOR install's manifest recorded but the CURRENT release no longer declares (an N-1 → N upgrade cleanup) | project/user | yes (same backup/restore mechanism as every other step) | no (this only ever removes paths the transaction's OWN prior manifest claims responsibility for) |

## 2. OS-specific differences

| Concern | Linux | macOS | Windows |
|---|---|---|---|
| Global install target | `HOME` (from `os.path.expanduser("~")`) | `HOME` | `HOME` (`%USERPROFILE%`) |
| `~/.local/bin` symlink target | real symlink (`os.symlink`) | real symlink | `os.symlink` requires Developer Mode or admin — falls back silently (best-effort; `_link_console_script` swallows `OSError`) |
| `chmod +x` on `.git/hooks/pre-commit` | applied (`0o755`) | applied | `os.chmod` is a no-op on most Windows filesystems; git only needs the file to be invocable via `sh` (`#!/usr/bin/env sh` shebang), which the file already has — the failed `chmod` is caught and logged as non-fatal |
| Externally-managed Python (PEP 668) | common on Debian/Ubuntu system Python | common on Homebrew Python | rare (python.org/Store installs are not externally-managed) — the detection in `_is_externally_managed_error()` still applies uniformly; it simply never fires here |
| Capture-proxy service registration | `install_services.py` → `systemd --user` unit | `setup_simplicio.sh` → `launchd` agent | `install_services.py` → Startup-folder shim |
| Path separators in receipts/manifest | POSIX (`/`) | POSIX (`/`) | `os.path` mixed; hashing walks (`_hash_path`) normalize relative paths to `/` before hashing so a receipt/manifest hash is stable across OSes for the same tree content |
| Console-script extension | none | none | `.exe` (pip) or `.CMD`/`.EXE` shims — `install_smoke.py`'s clean-room check and `_link_console_script()` both branch on `os.name == "nt"` |

## 3. Consent matrix (what requires an explicit flag)

Per `install_plan.py::_permissions_required()`:

| Effect | Trigger | Required consent |
|---|---|---|
| `global_package` | `scope in (user, system)` or `mode == full-stack` | scope/mode itself is the explicit choice (no separate flag) |
| `break_system_packages` | pip refuses with PEP 668's `externally-managed-environment` | `--allow-break-system-packages` / `SIMPLICIO_ALLOW_BREAK_SYSTEM_PACKAGES=1` — **never applied unconditionally** (#293 §3, hardened this round: see `install_lib.py::_pip_install()`) |
| `path_write` / `symlink` | `scope != project` | implied by `--global` |
| `service` | `--with-service` or `mode == full-stack` | explicit flag/mode |
| `proxy` | `--with-proxy` or `mode == full-stack` | explicit flag/mode |

A plan with an ungated `break_system_packages` permission is returned with `status: "BLOCKED"`
and mutates nothing (`install_plan.py` is a pure planner; `install_executor.py::apply()` returns
the BLOCKED plan as-is without persisting a transaction).

## 4. Known gaps (honest, not fixed by this document)

- `setup_monitor()` (capture proxy + dashboard + tray) currently runs by default (opt-out via
  `--minimal`) in the CURRENT (non-transactional) `install_lib.py main()` flow, which predates
  the mode/consent model this issue introduces. It is **not yet gated behind the planner's
  `service`/`proxy` permission flags** the way `install_plan.py`/`install_executor.py`'s
  transactional path is. Wiring the legacy default-install flow's `setup_monitor()` call through
  the same consent gate as the transactional executor is real remaining work, tracked as part of
  the still-open `runtime`/`full-stack`/`ci` executor-mode gap (see the PR that introduced this
  document for the full remaining-gap list).
- This document is maintained by hand against the source above, not generated from a single
  manifest file, because no single machine-readable "effects manifest" exists yet distinct from
  the per-run `install-transaction/v1` plan (which describes one run's effects, not the
  installer's full static capability surface). A generated version (§7 of the issue) remains a
  separate, larger piece of work.
