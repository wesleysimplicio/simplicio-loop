# Orca portable preset

Portable, secret-free setup for the canonical Simplicio Loop workflow:
executors (Simplicio Agent/Hermes) → Codex validation → Claude correction →
independent Codex final review. The Loop owns the lifecycle; Orca is its board projection.

Run `bash app/orca/install.sh` on macOS/Linux, or `app/orca/install.ps1` in PowerShell on Windows.
Install PyYAML first. The installer backs up local configuration and never copies API keys, sessions, or worktree history.

## Validation

`tests/test_orca_preset_integration.py` validates the manifest contract and runs the macOS/Linux installer against an isolated home with fake CLIs. PowerShell syntax and end-to-end execution must run on a Windows runner.
