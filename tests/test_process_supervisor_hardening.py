import asyncio
import os
import sys
import tempfile
from pathlib import Path

import pytest

from simplicio_loop.async_io_supervisor import AsyncProcessSupervisor, DuplicateLease
from simplicio_loop.process_supervisor import (
    ProcessSpec,
    ProcessSpecError,
    PythonProcessAdapter,
)


def test_argv_with_shell_metacharacters_is_never_shell_interpreted() -> None:
    spec = ProcessSpec((sys.executable, "-c", "print('ok')", "; rm -rf /tmp/x && echo hi"))
    assert spec.argv[-1] == "; rm -rf /tmp/x && echo hi"
    assert spec.to_dict()["shell"] is False

    async def scenario() -> None:
        result = await PythonProcessAdapter().run(spec)
        assert result.returncode == 0
        assert result.stdout.strip() == "ok"

    asyncio.run(scenario())


def test_shell_string_as_single_argv_token_is_not_split_or_run() -> None:
    dangerous = ProcessSpec(("echo hi; touch /tmp/simplicio-should-not-exist",))

    async def scenario() -> None:
        result = await PythonProcessAdapter().run(dangerous)
        assert result.error_code == "executable_not_found"
        assert result.returncode is None

    asyncio.run(scenario())
    assert not Path("/tmp/simplicio-should-not-exist").exists()


def test_secret_env_var_is_stripped_unless_explicitly_allowlisted(monkeypatch) -> None:
    monkeypatch.setenv("SIMPLICIO_FAKE_SECRET_API_KEY", "sk-should-not-leak")
    monkeypatch.setenv("SIMPLICIO_TEST_ALLOWED", "visible")
    spec = ProcessSpec(
        (
            sys.executable,
            "-c",
            "import os; print(os.environ.get('SIMPLICIO_FAKE_SECRET_API_KEY', '<absent>')); "
            "print(os.environ.get('SIMPLICIO_TEST_ALLOWED', '<absent>'))",
        ),
        env_allowlist=("SIMPLICIO_TEST_ALLOWED",),
    )

    async def scenario() -> str:
        result = await PythonProcessAdapter().run(spec)
        return result.stdout

    stdout = asyncio.run(scenario())
    lines = stdout.strip().splitlines()
    assert lines[0] == "<absent>"
    assert lines[1] == "visible"


def test_cwd_outside_allowed_root_is_rejected() -> None:
    with tempfile.TemporaryDirectory() as root:
        allowed_root = str(Path(root).resolve())
        outside = str(Path(root).parent.resolve())
        with pytest.raises(ProcessSpecError):
            ProcessSpec(("echo",), cwd=outside, cwd_allowlist=(allowed_root,))


def test_cwd_path_traversal_out_of_allowed_root_is_rejected() -> None:
    with tempfile.TemporaryDirectory() as root:
        allowed_root = Path(root).resolve()
        nested = allowed_root / "nested"
        nested.mkdir()
        traversal_cwd = str(nested / ".." / ".." / "etc")
        with pytest.raises(ProcessSpecError):
            ProcessSpec(("echo",), cwd=traversal_cwd, cwd_allowlist=(str(allowed_root),))


def test_cwd_inside_allowed_root_is_accepted() -> None:
    with tempfile.TemporaryDirectory() as root:
        allowed_root = Path(root).resolve()
        nested = allowed_root / "nested"
        nested.mkdir()
        spec = ProcessSpec(("echo",), cwd=str(nested), cwd_allowlist=(str(allowed_root),))
        assert spec.cwd == str(nested)


def test_concurrent_submission_with_same_idempotency_key_executes_once() -> None:
    async def scenario() -> None:
        supervisor = AsyncProcessSupervisor(max_concurrency=2)
        spec = ProcessSpec(
            (sys.executable, "-c", "import time; time.sleep(0.05); print('done')"),
            idempotency_key="dup-key",
        )
        results = await asyncio.gather(
            supervisor.run(spec), supervisor.run(spec), return_exceptions=True
        )
        successes = [r for r in results if not isinstance(r, BaseException)]
        duplicates = [r for r in results if isinstance(r, DuplicateLease)]
        assert len(successes) == 1
        assert len(duplicates) == 1
        assert successes[0].returncode == 0

    asyncio.run(scenario())
