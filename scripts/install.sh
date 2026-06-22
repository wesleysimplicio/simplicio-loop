#!/usr/bin/env bash
# simplicio-tasks installer (thin launcher → scripts/install_lib.py)
# Usage: bash scripts/install.sh <runtime> [--global] [--target DIR]
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$(command -v python3 || command -v python || true)"
if [ -z "$PY" ]; then
  echo "python3 is required (the skills, hooks, and installer are cross-platform Python)." >&2
  exit 1
fi
exec "$PY" "$DIR/install_lib.py" "$@"
