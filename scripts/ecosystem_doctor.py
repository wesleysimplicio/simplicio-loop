#!/usr/bin/env python3
"""Checkout-friendly entry point for ``simplicio.ecosystem-doctor/v1``.

The implementation lives in :mod:`simplicio_loop.ecosystem_doctor` so the same
doctor is available from a source checkout and from the installed wheel.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from simplicio_loop.ecosystem_doctor import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
