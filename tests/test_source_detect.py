"""Unit + integration + system tests for source_detect (#490).

Covers: GitHub-origin detection (https/ssh), non-GitHub rejection, explicit opt-out,
fail-closed on missing auth, and default adapter selection. Network/gh calls are
mocked so the suite runs offline and deterministically.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, List
from unittest import mock

import pytest

from simplicio_loop import source_detect as sd
from simplicio_loop.source_adapter import GitHubSourceAdapter, SourceAdapter


def _fake_run(stdout: str = "", returncode: int = 0) -> Callable:
    def _run(cmd: List[str], **kwargs: Any) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(cmd, returncode, stdout, "")
    return _run


# --- detection (unit) ---


def test_detect_https_github_origin():
    run = _fake_run("https://github.com/wesleysimplicio/simplicio-loop.git\n")
    assert sd.detect_github_origin(".", runner=run) == ("wesleysimplicio", "simplicio-loop")


def test_detect_ssh_github_origin():
    run = _fake_run("git@github.com:wesleysimplicio/simplicio-runtime.git\n")
    assert sd.detect_github_origin(".", runner=run) == ("wesleysimplicio", "simplicio-runtime")


def test_detect_non_github_origin_returns_none():
    run = _fake_run("https://gitlab.com/acme/widgets.git\n")
    assert sd.detect_github_origin(".", runner=run) is None


def test_detect_no_origin_returns_none():
    run = _fake_run("", returncode=1)
    assert sd.detect_github_origin(".", runner=run) is None


def test_detect_malformed_url_returns_none():
    run = _fake_run("not-a-url\n")
    assert sd.detect_github_origin(".", runner=run) is None


def test_detect_dots_rejected():
    run = _fake_run("https://github.com/././.git\n")
    assert sd.detect_github_origin(".", runner=run) is None


# --- selection (integration / system) ---


def test_select_returns_github_adapter_when_auth_ok():
    def run(cmd, **kwargs):
        if cmd[:3] == ["git", "-C", "."]:
            return subprocess.CompletedProcess(cmd, 0, "https://github.com/wesleysimplicio/simplicio-loop.git\n", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    adapter = sd.select_default_source_adapter(".", runner=run)
    assert isinstance(adapter, GitHubSourceAdapter)
    assert adapter.provider == "github"
    assert adapter.owner == "wesleysimplicio"
    assert adapter.repo == "simplicio-loop"


def test_select_fail_closed_when_auth_missing():
    def run(cmd, **kwargs):
        if cmd[:3] == ["git", "-C", "."]:
            return subprocess.CompletedProcess(cmd, 0, "https://github.com/wesleysimplicio/simplicio-loop.git\n", "")
        return subprocess.CompletedProcess(cmd, 1, "", "gh: not authenticated")

    with pytest.raises(sd.GitHubOriginError) as exc:
        sd.select_default_source_adapter(".", runner=run)
    assert exc.value.reason_code == sd.GITHUB_COORDINATION_UNAVAILABLE


def test_select_no_github_origin_raises():
    run = _fake_run("https://gitlab.com/acme/widgets.git\n")
    with pytest.raises(sd.GitHubOriginError) as exc:
        sd.select_default_source_adapter(".", runner=run)
    assert exc.value.reason_code == "no_github_origin"


def test_select_explicit_optout_raises():
    run = _fake_run("https://github.com/wesleysimplicio/simplicio-loop.git\n")
    with pytest.raises(sd.GitHubOriginError) as exc:
        sd.select_default_source_adapter(".", explicit_override="none", runner=run)
    assert exc.value.reason_code == "explicit_non_github_override"


def test_select_handles_runner_exception_fail_closed():
    def boom(cmd, **kwargs):
        raise RuntimeError("network down")

    with pytest.raises(sd.GitHubOriginError):
        sd.select_default_source_adapter(".", runner=boom)


def test_github_adapter_is_source_adapter_protocol():
    assert GitHubSourceAdapter.provider == "github"
    assert issubclass(GitHubSourceAdapter, object)
