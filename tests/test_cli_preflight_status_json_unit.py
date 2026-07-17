"""Unit tests for WI-471 CLI contract: `preflight` subcommand + `status --json`.

Covers the two contracts the published `simplicio-loop 3.36.1` install was missing:
  * `simplicio-loop preflight --repo .` must exist and emit a machine-readable JSON doc.
  * `simplicio-loop status --repo . --json` must accept `--json` and emit JSON (never
    "unrecognized arguments"), and must not raise on a repo with no runs.
"""
from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
from pathlib import Path
from unittest import TestCase

REPO = Path(__file__).resolve().parents[1]


def _run_cli(args):
    """Invoke the CLI module as a subprocess; return (rc, stdout, stderr)."""
    proc = subprocess.run(
        [sys.executable, "-m", "simplicio_loop.cli", *args],
        cwd=str(REPO), capture_output=True, text=True, timeout=60,
    )
    return proc.returncode, proc.stdout, proc.stderr


class PreflightContractTest(TestCase):
    def test_preflight_exists_and_emits_json(self):
        rc, out, err = _run_cli(["preflight", "--repo", ".", "--json"])
        self.assertEqual(rc, 0, msg=f"preflight exited {rc}; stderr={err}")
        doc = json.loads(out)
        self.assertEqual(doc["schema"], "simplicio.preflight/v1")
        self.assertIn("all_present", doc)
        self.assertIn("operators", doc)
        self.assertIsInstance(doc["operators"], list)
        self.assertTrue(len(doc["operators"]) >= 3)

    def test_preflight_text_mode_names_operators(self):
        rc, out, err = _run_cli(["preflight", "--repo", "."])
        self.assertEqual(rc, 0, msg=f"stderr={err}")
        self.assertIn("simplicio-loop preflight", out)
        self.assertIn("simplicio-mapper", out)
        self.assertIn("simplicio-runtime", out)

    def test_preflight_rejects_unknown_subcommand_still_absent(self):
        # Sanity: a genuinely unknown command must still error (proves we only ADDED preflight).
        rc, _out, err = _run_cli(["nonexistent-xyz"])
        self.assertNotEqual(rc, 0)
        self.assertIn("invalid choice", err)


class StatusJsonContractTest(TestCase):
    def test_status_json_accepted_without_runs(self):
        rc, out, err = _run_cli(["status", "--repo", ".", "--json"])
        self.assertEqual(rc, 0, msg=f"status --json crashed; stderr={err}")
        self.assertEqual(err.count("unrecognized arguments"), 0,
                         msg="--json must be accepted, not 'unrecognized arguments'")
        doc = json.loads(out)
        # Without runs we expect a controlled UNVERIFIED doc, not a traceback.
        self.assertIn(doc.get("status"), ("UNVERIFIED", "ok"))
        self.assertIn("schema", doc)

    def test_status_json_flag_never_unrecognized(self):
        # The exact regression from issue #471: public install rejected `--json`.
        rc, _out, err = _run_cli(["status", "--repo", ".", "--json"])
        self.assertEqual(rc, 0)
        self.assertNotIn("unrecognized arguments: --json", err)

    def test_status_text_mode_without_runs_does_not_traceback(self):
        rc, out, err = _run_cli(["status", "--repo", ".", "--text"])
        self.assertEqual(rc, 0, msg=f"text status crashed; stderr={err}")
        self.assertNotIn("Traceback", err)
        self.assertIn("simplicio-loop run status", out)


class DirectCallCoverageTest(TestCase):
    """Import-level calls to raise line/branch coverage of the patched functions."""

    def test_preflight_direct_call_text(self):
        from simplicio_loop import cli
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.preflight(str(REPO), as_json=False)
        self.assertEqual(rc, 0)
        self.assertIn("simplicio-loop preflight", buf.getvalue())

    def test_preflight_direct_call_json(self):
        from simplicio_loop import cli
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.preflight(str(REPO), as_json=True)
        self.assertEqual(rc, 0)
        doc = json.loads(buf.getvalue())
        self.assertEqual(doc["schema"], "simplicio.preflight/v1")

    def test_status_direct_call_no_runs_json(self):
        from simplicio_loop import cli
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.status(str(REPO), "", as_json=True)
        self.assertEqual(rc, 0)
        doc = json.loads(buf.getvalue())
        self.assertEqual(doc["reason_code"], "run_missing")

    def test_status_direct_call_no_runs_text(self):
        from simplicio_loop import cli
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.status(str(REPO), "", as_text=True)
        self.assertEqual(rc, 0)
        self.assertIn("simplicio-loop run status", buf.getvalue())


class RenderStatusTextUnitTest(TestCase):
    """Isolated coverage of the patched _render_status_text helper."""

    def test_render_with_full_payload(self):
        from simplicio_loop import cli
        payload = {
            "run_dir": "/tmp/x",
            "state": {
                "phase": "executing",
                "completion": {"tag": "VERIFIED", "coverage": "3/3"},
                "delivery": {"ready": True},
            },
        }
        out = cli._render_status_text(payload)
        self.assertIn("run_dir: /tmp/x", out)
        self.assertIn("phase: executing", out)
        self.assertIn("completion_tag: VERIFIED", out)
        self.assertIn("coverage: 3/3", out)
        self.assertIn("delivery_ready: True", out)

    def test_render_with_minimal_payload(self):
        from simplicio_loop import cli
        payload = {"state": {}}
        out = cli._render_status_text(payload)
        self.assertIn("phase: UNKNOWN", out)
        self.assertIn("completion_tag: UNVERIFIED", out)
        self.assertIn("delivery_ready: False", out)
