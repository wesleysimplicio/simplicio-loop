import importlib.util
import unittest
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1] / "scripts" / "release_train_hops.py"
SPEC = importlib.util.spec_from_file_location("release_train_hops_slot", MODULE)
RELEASE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RELEASE)

class ReleaseTrainSlotTests(unittest.TestCase):
    def test_eight_repo_train_is_fail_closed_and_rollback_is_atomic(self):
        receipt = RELEASE.release_train_receipt([], release_id="r1", graph_hash="g")
        self.assertEqual(receipt["status"], "blocked")
        rollback = RELEASE.rollback_receipt(release_id="r2", previous_release_id="r1")
        self.assertTrue(rollback["atomic"])
        self.assertEqual(rollback["status"], "ready")

if __name__ == "__main__":
    unittest.main()
