#!/usr/bin/env python3
from __future__ import annotations

import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from simplicio_loop.evidence import main


if __name__ == "__main__":
    raise SystemExit(main())
