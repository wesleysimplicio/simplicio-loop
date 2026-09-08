"""Source-checkout compatibility facade for the packaged process boundary."""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from simplicio_loop import quality_process as _quality_process

# Keep existing ``scripts.check_runtime`` imports, including its private test
# seams, pointed at the single canonical implementation.  The source quality
# gate still uses this path, while installed providers use the package path.
sys.modules[__name__] = _quality_process
