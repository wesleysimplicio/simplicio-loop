#!/usr/bin/env python3
"""Compatibility shim for the packaged WorktreeQueue implementation."""
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from simplicio_loop.worktree_queue import *  # noqa: E402,F401,F403
from simplicio_loop.worktree_queue import _cli  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(_cli())
