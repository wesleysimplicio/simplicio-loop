#!/usr/bin/env python3
"""Compatibility entry point for the packaged capability-inventory generator."""

from pathlib import Path
import runpy


TARGET = (
    Path(__file__).resolve().parents[1]
    / ".claude"
    / "skills"
    / "simplicio-prism"
    / "scripts"
    / "generate_capability_inventory.py"
)


if __name__ == "__main__":
    runpy.run_path(str(TARGET), run_name="__main__")
