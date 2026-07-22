from __future__ import annotations

import errno

import pytest

from simplicio_loop import platform_capabilities
from simplicio_loop.platform_capabilities import probe_af_unix, probe_hub_transport


class _ProbeSocket:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True

    def bind(self, endpoint: str) -> None:
        self.endpoint = endpoint


def test_af_unix_probe_closes_successful_probe() -> None:
    probe = _ProbeSocket()
    result = probe_af_unix(lambda *_args: probe, platform_name="posix")
    assert result.available is True
    assert result.reason_code == "ok"
    assert probe.closed is True
    assert probe.endpoint.endswith("probe.sock")


def test_af_unix_probe_classifies_only_eperm_as_unavailable() -> None:
    def denied(*_args):
        raise PermissionError(errno.EPERM, "denied")

    result = probe_af_unix(denied, platform_name="posix")
    assert result.available is False
    assert result.reason_code == "af_unix_eperm"


def test_af_unix_probe_does_not_hide_other_socket_errors() -> None:
    def broken(*_args):
        raise PermissionError(errno.EACCES, "bad path permissions")

    with pytest.raises(PermissionError) as exc_info:
        probe_af_unix(broken, platform_name="posix")
    assert exc_info.value.errno == errno.EACCES


def test_af_unix_probe_classifies_private_bind_eacces() -> None:
    class DeniedBind(_ProbeSocket):
        def bind(self, _endpoint: str) -> None:
            raise PermissionError(errno.EACCES, "bad bind permissions")

    capability = probe_af_unix(lambda *_args: DeniedBind(), platform_name="posix")
    assert capability.available is False
    assert capability.reason_code == "af_unix_eacces"


def test_af_unix_probe_does_not_hide_temp_root_eacces(monkeypatch) -> None:
    probe = _ProbeSocket()

    def denied_directory(*_args, **_kwargs):
        raise PermissionError(errno.EACCES, "external temp root denied")

    monkeypatch.setattr(platform_capabilities.tempfile, "TemporaryDirectory", denied_directory)
    with pytest.raises(PermissionError) as exc_info:
        probe_af_unix(lambda *_args: probe, platform_name="posix")
    assert exc_info.value.errno == errno.EACCES
    assert probe.closed is True


def test_default_named_pipe_transport_does_not_probe_af_unix() -> None:
    calls = []

    def forbidden_socket(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("named-pipe capability must not create an AF_UNIX socket")

    capability = probe_hub_transport("named-pipe", forbidden_socket, platform_name="nt")

    assert capability.available is True
    assert capability.reason_code == "ok"
    assert calls == []


def test_named_pipe_transport_is_not_reported_available_on_posix() -> None:
    capability = probe_hub_transport("named-pipe", platform_name="posix")

    assert capability.available is False
    assert capability.reason_code == "named_pipe_unsupported"


def test_default_unix_transport_uses_the_af_unix_probe() -> None:
    def denied_socket(*_args, **_kwargs):
        raise PermissionError(errno.EPERM, "not permitted")

    capability = probe_hub_transport("unix", denied_socket, platform_name="posix")

    assert capability.available is False
    assert capability.reason_code == "af_unix_eperm"


@pytest.mark.parametrize("error_name", ["EAFNOSUPPORT", "EPROTONOSUPPORT"])
def test_af_unix_probe_classifies_creation_unsupported(error_name: str) -> None:
    error_number = getattr(errno, error_name, None)
    if error_number is None:
        pytest.skip("errno.%s is unavailable on this platform" % error_name)

    def unsupported(*_args):
        raise OSError(error_number, "unsupported")

    capability = probe_af_unix(unsupported, platform_name="posix")
    assert capability.available is False
    assert capability.reason_code == "af_unix_unsupported"


def test_af_unix_probe_respects_injected_windows_platform() -> None:
    calls = []

    def forbidden(*args):
        calls.append(args)
        raise AssertionError("Windows probe must not create AF_UNIX sockets")

    capability = probe_af_unix(forbidden, platform_name="nt")
    assert capability.available is False
    assert capability.reason_code == "af_unix_unsupported"
    assert calls == []
