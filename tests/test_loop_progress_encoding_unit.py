"""Encoding-safe banner tests for issue #465 (Windows cp1252 UnicodeEncodeError).

Proves _warning_banner never raises and degrades to ASCII when the output
encoding cannot represent the Unicode warning sign.
"""
import io
import sys
import importlib.util
import os

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SPEC = importlib.util.spec_from_file_location(
    "loop_progress_465", os.path.join(ROOT, "scripts", "loop_progress.py")
)
loop_progress = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(loop_progress)


class _FakeStream(io.TextIOWrapper):
    """Minimal stdout stand-in with a forced encoding."""

    def __init__(self, encoding):
        self._encoding = encoding

    @property
    def encoding(self):
        return self._encoding


def _snapshot_with(detail, status="blocked"):
    return {"last_status": status, "last_detail": detail}


def test_banner_utf8_uses_warning_sign(monkeypatch):
    monkeypatch.setattr(sys, "stdout", _FakeStream("utf-8"))
    out = loop_progress._warning_banner(_snapshot_with("DRIFT detected"))
    assert "\u26a0" in out
    assert "DRIFT" in out


def test_banner_cp1252_falls_back_to_ascii(monkeypatch):
    monkeypatch.setattr(sys, "stdout", _FakeStream("cp1252"))
    out = loop_progress._warning_banner(_snapshot_with("STALLED detected"))
    assert "\u26a0" not in out
    assert "!!" in out
    assert "STALLED" in out


def test_banner_ascii_falls_back_to_ascii(monkeypatch):
    monkeypatch.setattr(sys, "stdout", _FakeStream("ascii"))
    out = loop_progress._warning_banner(_snapshot_with("DRIFT x"))
    assert "!!" in out


def test_banner_non_blocked_returns_empty():
    out = loop_progress._warning_banner(_snapshot_with("DRIFT", status="running"))
    assert out == ""


def test_banner_encodes_without_error(monkeypatch):
    """The produced banner must be encodable in the simulated output encoding."""
    monkeypatch.setattr(sys, "stdout", _FakeStream("cp1252"))
    out = loop_progress._warning_banner(_snapshot_with("STALLED"))
    # Must not raise UnicodeEncodeError
    out.encode("cp1252")
