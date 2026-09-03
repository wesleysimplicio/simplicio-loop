#!/usr/bin/env python3
"""Offline contract tests for scripts/submodules.py."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import submodules  # noqa: E402


class SubmoduleContracts(unittest.TestCase):
    def test_manifest_and_gitmodules_have_three_known_pins(self):
        pins = submodules.load_pins(ROOT / "components" / "submodules.json")
        modules = submodules.load_gitmodules(ROOT / ".gitmodules")
        self.assertEqual(set(pins), {"simplicio-mapper", "simplicio-dev-cli", "simplicio-fast"})
        self.assertEqual(len(modules), 3)
        for item in pins.values():
            self.assertEqual(modules[item["path"]]["path"], item["path"])
            self.assertEqual(modules[item["path"]]["shallow"], "true")
            self.assertEqual(modules[item["path"]]["branch"], item["ref"])
            self.assertEqual(len(item["sha"]), 40)

    def test_committed_receipt_covers_the_three_gitlinks(self):
        receipt = json.loads((ROOT / "tests" / "fixtures" / "submodules_receipt.json").read_text())
        pins = submodules.load_pins(ROOT / "components" / "submodules.json")
        self.assertEqual(receipt["schema"], "simplicio.loop-submodules-receipt/v1")
        self.assertEqual(set(receipt["components"]), set(pins))
        for name, item in pins.items():
            self.assertEqual(receipt["components"][name]["sha"], item["sha"])
        self.assertEqual(receipt["metrics"]["duration_ms"], None)
        self.assertEqual(receipt["metrics"]["network_requests"], 0)

    def test_floating_policy_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pins.json"
            document = json.loads((ROOT / "components" / "submodules.json").read_text())
            document["policy"]["floating_updates"] = True
            path.write_text(json.dumps(document))
            with self.assertRaises(submodules.SubmoduleError):
                submodules.load_pins(path)

    def test_status_reports_missing_without_mutation(self):
        pins = submodules.load_pins(ROOT / "components" / "submodules.json")
        missing = lambda _path, expected_sha: {
            "state": "missing", "expected_sha": expected_sha, "observed_sha": None,
        }
        with mock.patch.object(submodules, "_gitlink_shas", return_value={
            item["path"]: item["sha"] for item in pins.values()
        }), mock.patch.object(submodules, "_path_status", side_effect=missing):
            report = submodules.inspect()
        self.assertEqual(report["components"]["simplicio-fast"]["state"], "missing")
        self.assertFalse(report["ok"])

    def test_clean_checkout_and_divergence_receipts(self):
        """Exercise the same checkout probe used by a clean-clone preflight."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            component = root / "components" / "simplicio-fast"
            component.mkdir(parents=True)
            subprocess.run(["git", "init", "-q", str(component)], check=True)
            subprocess.run(["git", "-C", str(component), "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(component), "config", "user.name", "submodule-test"], check=True)
            (component / "README").write_text("pinned\n")
            subprocess.run(["git", "-C", str(component), "add", "README"], check=True)
            subprocess.run(["git", "-C", str(component), "commit", "-qm", "fixture"], check=True)
            sha = subprocess.check_output(["git", "-C", str(component), "rev-parse", "HEAD"], text=True).strip()
            pins = {
                "simplicio-fast": {"path": "components/simplicio-fast", "url": "file:///fixture", "ref": "master", "sha": sha},
            }
            modules = {
                "components/simplicio-fast": {
                    "path": "components/simplicio-fast",
                    "url": "file:///fixture",
                    "branch": "master",
                }
            }
            with mock.patch.object(submodules, "REPO", root), mock.patch.object(submodules, "load_pins", return_value=pins), mock.patch.object(submodules, "load_gitmodules", return_value=modules), mock.patch.object(submodules, "_gitlink_shas", return_value={"components/simplicio-fast": sha}):
                self.assertTrue(submodules.inspect()["ok"])
                (component / "dirty").write_text("x")
                self.assertEqual(submodules.inspect()["components"]["simplicio-fast"]["state"], "dirty")
                (component / "dirty").unlink()
                subprocess.run(["git", "-C", str(component), "commit", "--allow-empty", "-qm", "divergence"], check=True)
                self.assertEqual(submodules.inspect()["components"]["simplicio-fast"]["state"], "diverged")

    def test_uninitialized_and_foreign_checkouts_never_use_parent_head(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            uninitialized = root / "components" / "simplicio-mapper"
            uninitialized.mkdir(parents=True)
            with mock.patch.object(submodules, "REPO", root):
                report = submodules._path_status(uninitialized, "0" * 40)
            self.assertEqual(report["state"], "uninitialized")
            self.assertIsNone(report["observed_sha"])

            foreign_repo = root / "foreign"
            subprocess.run(["git", "init", "-q", str(foreign_repo)], check=True)
            foreign_checkout = root / "components" / "simplicio-fast"
            foreign_checkout.mkdir(parents=True)
            (foreign_checkout / ".git").write_text(
                f"gitdir: {foreign_repo / '.git'}\n", encoding="utf-8"
            )
            with mock.patch.object(submodules, "REPO", root):
                report = submodules._path_status(foreign_checkout, "0" * 40)
            self.assertEqual(report["state"], "wrong_repository")
            self.assertIsNone(report["observed_sha"])

    def test_run_manifest_requires_verified_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "run.json"
            with mock.patch.object(submodules, "verify", side_effect=submodules.SubmoduleError("missing")):
                with self.assertRaises(submodules.SubmoduleError):
                    submodules.write_run_manifest(destination)
            self.assertFalse(destination.exists())

    def test_no_remote_update_command_is_present(self):
        source = (ROOT / "scripts" / "submodules.py").read_text()
        self.assertNotIn('"submodule", "update", "--remote"', source)
        self.assertIn("floating_updates", source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
