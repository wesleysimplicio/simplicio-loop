# Orca portable preset

Portable, secret-free setup for the canonical Simplicio Loop workflow:
executors (Simplicio Agent/Hermes) → Codex validation → Claude correction →
independent Codex final review. The Loop owns the lifecycle; Orca is its board projection.

Run `bash app/orca/install.sh` on macOS/Linux, or `app/orca/install.ps1` in PowerShell on Windows.
Install PyYAML first. The installer backs up local configuration and never copies API keys, sessions, or worktree history.
