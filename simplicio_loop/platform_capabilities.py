"""Typed runtime capability probes used by hermetic local gates."""
from __future__ import annotations

import errno
import os
import socket
import tempfile
from dataclasses import dataclass
from typing import Callable, Optional


_AF_UNIX_UNSUPPORTED_ERRNOS = frozenset(
    value
    for value in (
        getattr(errno, "EAFNOSUPPORT", None),
        getattr(errno, "EPROTONOSUPPORT", None),
        getattr(errno, "ENOSYS", None),
        getattr(errno, "EOPNOTSUPP", None),
    )
    if value is not None
)


@dataclass(frozen=True)
class Capability:
    available: bool
    reason_code: str
    detail: str


def probe_af_unix(
    socket_factory: Optional[Callable[..., socket.socket]] = None,
    platform_name: Optional[str] = None,
) -> Capability:
    """Probe AF_UNIX without hiding product errors.

    A restricted sandbox can expose ``socket.AF_UNIX`` yet reject a real bind.
    The probe therefore creates and binds inside a private temporary directory.
    Only capability denials inside the private probe are classified as
    unavailable. Errors while creating that private directory remain visible,
    so an inaccessible caller-supplied/external path is never hidden.
    """
    current_platform = platform_name or os.name
    if current_platform == "nt" or not hasattr(socket, "AF_UNIX"):
        return Capability(False, "af_unix_unsupported", "AF_UNIX is not available on this OS")
    factory = socket_factory or socket.socket
    try:
        probe = factory(socket.AF_UNIX, socket.SOCK_STREAM)
    except OSError as exc:
        if exc.errno == errno.EPERM:
            return Capability(False, "af_unix_eperm", "kernel denied AF_UNIX socket creation (EPERM)")
        if exc.errno in _AF_UNIX_UNSUPPORTED_ERRNOS:
            return Capability(False, "af_unix_unsupported", "kernel does not support AF_UNIX sockets")
        raise
    try:
        with tempfile.TemporaryDirectory(prefix="simplicio-af-unix-") as directory:
            endpoint = os.path.join(directory, "probe.sock")
            try:
                probe.bind(endpoint)
            except OSError as exc:
                if exc.errno == errno.EPERM:
                    return Capability(False, "af_unix_eperm", "kernel denied AF_UNIX bind (EPERM)")
                if exc.errno == errno.EACCES:
                    return Capability(
                        False, "af_unix_eacces",
                        "sandbox denied bind inside the private AF_UNIX probe (EACCES)",
                    )
                if exc.errno in _AF_UNIX_UNSUPPORTED_ERRNOS:
                    return Capability(False, "af_unix_unsupported", "kernel does not support AF_UNIX bind")
                raise
            try:
                os.unlink(endpoint)
            except FileNotFoundError:
                pass
    finally:
        probe.close()
    return Capability(True, "ok", "AF_UNIX socket creation and bind succeeded")


def probe_hub_transport(
    transport: str,
    socket_factory: Optional[Callable[..., socket.socket]] = None,
    platform_name: Optional[str] = None,
) -> Capability:
    """Probe only the OS capability required by the selected Hub transport."""
    current_platform = platform_name or os.name
    if transport == "unix":
        return probe_af_unix(socket_factory, platform_name=current_platform)
    if transport == "named-pipe":
        if current_platform != "nt":
            return Capability(
                False,
                "named_pipe_unsupported",
                "named-pipe Hub transport requires Windows",
            )
        return Capability(True, "ok", "named-pipe transport does not require AF_UNIX")
    raise ValueError("transport must be unix or named-pipe")
