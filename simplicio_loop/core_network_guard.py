"""Opt-in Python socket guard for the hermetic core gate.

The guard intentionally applies only inside Python interpreters which inherit
``SIMPLICIO_CORE_NO_NETWORK=1``.  It permits AF_UNIX and loopback INET, but
rejects other INET destinations before DNS resolution or connection.  It is a
Python-level guard; it does not claim to restrict non-Python executables.
"""
from __future__ import annotations

import errno
import inspect
import ipaddress
import os
import socket
import subprocess
import sys


_GUARD_ROOT = os.path.realpath(os.path.dirname(os.path.dirname(__file__)))
_AUDIT_INSTALLED = False
_CHILD_GUARD_MARKER = "__simplicio_core_child_guard__"


def _allowed_host(host):
    if host is None:
        return True
    if not isinstance(host, str):
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
    except ValueError:
        # Resolving another hostname would itself use the external network.
        return False


def _allowed_address(address):
    return isinstance(address, tuple) and bool(address) and _allowed_host(address[0])


def _allowed_bind_address(address):
    """Only permit loopback listeners in the hermetic lane.

    Binding a wildcard or a LAN address makes the gate observable from outside
    the process just as a connect does, so it is not an offline operation.
    """
    return _allowed_address(address)


def _blocked():
    return OSError(errno.ENETUNREACH, "network disabled by core gate")


def _looks_like_python(path):
    """Recognize the actual executable, including a symlink or changed argv0."""
    if not path:
        return False
    try:
        text = os.fsdecode(os.fspath(path))
    except TypeError:
        return False
    name = os.path.basename(text).lower()
    if name.startswith("python"):
        return True
    try:
        return os.path.realpath(text) == os.path.realpath(sys.executable)
    except OSError:
        return False


def _suppresses_guard(argv):
    """Whether Python's option parsing can prevent loading sitecustomize."""
    index = 1
    while index < len(argv):
        value = argv[index]
        try:
            option = os.fsdecode(os.fspath(value))
        except TypeError:
            return True
        if option == "--":
            break
        if not option.startswith("-") or option == "-":
            break
        # Python accepts combined short options, e.g. ``-IS``.  ``-E`` does
        # not disable ``site`` itself, but it ignores PYTHONPATH; a child with
        # a different cwd can then evade this repository's sitecustomize.
        if option in {"-S", "-I", "-E", "-P"} or (
            option.startswith("-") and any(flag in option[1:] for flag in "SIEP")
        ):
            return True
        # ``-X safe_path`` is equivalent to ``-P``.  Treat it as an
        # isolation request too: allowing it would make delivery depend on a
        # subtle interpreter-version distinction.
        if option == "-X" and index + 1 < len(argv):
            try:
                xoption = os.fsdecode(os.fspath(argv[index + 1]))
            except (TypeError, UnicodeError):
                return True
            if xoption == "safe_path":
                return True
            index += 2
            continue
        if option.startswith("-X"):
            if option[2:].lstrip("=") == "safe_path":
                return True
            index += 1
            continue
        # Warning controls consume their next token in the split form. That
        # token is not a script name, so continue parsing subsequent options.
        if option == "-W":
            index += 2
            continue
        if option.startswith("-W"):
            index += 1
            continue
        if option == "--check-hash-based-pycs":
            index += 2
            continue
        # -c and -m consume the next token and terminate interpreter option
        # parsing; strings such as "-S" inside code/module arguments are not
        # interpreter flags.
        if option in {"-c", "-m"}:
            return False
        index += 1
    return False


def _child_guard_env(env=None):
    """Copy *env* and make the core hook available to a Python child.

    Supervisors commonly supply a deliberately minimal environment.  Requiring
    them to know the implementation detail of ``sitecustomize`` turned normal
    Python work into a false ``spawn_error``.  Copy (never mutate) the supplied
    mapping, restore the opt-in marker, and prepend this checkout to
    ``PYTHONPATH``.  Byte environments are retained for the ``os.exec*`` and
    ``os.spawn*`` APIs that support them.
    """
    source = os.environ if env is None else env
    try:
        child_env = dict(source)
    except (TypeError, ValueError):
        return None
    binary = any(isinstance(key, bytes) for key in child_env)
    marker = b"SIMPLICIO_CORE_NO_NETWORK" if binary else "SIMPLICIO_CORE_NO_NETWORK"
    pythonpath_key = b"PYTHONPATH" if binary else "PYTHONPATH"
    root = os.fsencode(_GUARD_ROOT) if binary else _GUARD_ROOT
    separator = os.fsencode(os.pathsep) if binary else os.pathsep
    existing = child_env.get(pythonpath_key, b"" if binary else "")
    if not isinstance(existing, type(root)):
        return None
    parts = existing.split(separator) if existing else []
    if not any(part and os.path.realpath(os.fsdecode(part)) == _GUARD_ROOT for part in parts):
        child_env[pythonpath_key] = root if not existing else root + separator + existing
    child_env[marker] = b"1" if binary else "1"
    return child_env


def _inherited_guard_is_delivered():
    """Whether an env-less exec/spawn can safely inherit the hook."""
    if os.environ.get("SIMPLICIO_CORE_NO_NETWORK") != "1":
        return False
    pythonpath = os.environ.get("PYTHONPATH", "")
    return any(
        part and os.path.realpath(part) == _GUARD_ROOT
        for part in pythonpath.split(os.pathsep)
    )


def _is_python_guard_bypass(args, *, executable=None, kwargs=None):
    """Fail closed only for Python children that cannot inherit this guard."""
    kwargs = kwargs or {}
    # Shell command text cannot be parsed reliably: a sequence, an inline
    # assignment, expansion, or a nested shell can hide a Python invocation.
    # In the hermetic core lane no shell child is an acceptable escape hatch.
    if kwargs.get("shell"):
        return True
    if isinstance(args, (str, bytes, os.PathLike)):
        # Without ``shell=True`` a string is an opaque executable name, not a
        # shell command.  ``executable=python`` is still inspectable below.
        try:
            argv = [os.fsdecode(os.fspath(args))]
        except (TypeError, UnicodeError):
            return True
    else:
        try:
            argv = [os.fsdecode(os.fspath(value)) for value in args]
        except (TypeError, UnicodeError):
            return True
    if not argv:
        return False
    actual = executable or kwargs.get("executable") or argv[0]
    if not _looks_like_python(actual):
        return False
    return _suppresses_guard(argv)


def _python_child_options(args, *, executable=None, kwargs=None):
    """Return copied options with a delivered guard, or ``None`` if irrelevant."""
    kwargs = dict(kwargs or {})
    if kwargs.get("shell"):
        return False
    if isinstance(args, (str, bytes, os.PathLike)):
        try:
            argv = [os.fsdecode(os.fspath(args))]
        except (TypeError, UnicodeError):
            return False
    else:
        try:
            argv = [os.fsdecode(os.fspath(value)) for value in args]
        except (TypeError, UnicodeError):
            return False
    if not argv:
        return None
    actual = executable or kwargs.get("executable") or argv[0]
    if not _looks_like_python(actual):
        return None
    if _suppresses_guard(argv):
        return False
    kwargs["env"] = _child_guard_env(kwargs.get("env"))
    return kwargs if kwargs["env"] is not None else False


def _popen_options(original, args, args_rest, kwargs):
    """Normalize positional Popen options without changing Popen's errors."""
    # A failed bind is part of Popen's public calling contract.  In
    # particular, never normalize duplicate positional+keyword arguments into
    # a valid call and accidentally launch a process the stdlib would reject.
    bound = inspect.signature(original).bind(args, *args_rest, **kwargs)
    options = dict(bound.arguments)
    options.pop("args", None)
    return options


def _blocked_no_site_child(*_args, **_kwargs):
    raise OSError(errno.EPERM, "core gate forbids Python child network-guard bypass")


def _network_audit_hook(event, args):
    """Enforce the offline policy at CPython's socket audit boundary.

    Audit events are emitted by the C socket implementation too, including
    instances constructed from a type reference retained before installation.
    This avoids replacing public socket classes and therefore preserves their
    exact APIs, signatures, aliases and MROs.
    """
    if os.environ.get("SIMPLICIO_CORE_NO_NETWORK") != "1":
        return
    if event in {"socket.connect", "socket.sendto", "socket.sendmsg"}:
        sock, address = args[0], args[1]
        if event == "socket.sendmsg" and address is None:
            return
        if sock.family in (socket.AF_INET, socket.AF_INET6) and not _allowed_address(address):
            raise _blocked()
    elif event == "socket.bind":
        sock, address = args[0], args[1]
        if sock.family in (socket.AF_INET, socket.AF_INET6) and not _allowed_bind_address(address):
            raise _blocked()
    elif event in {
        "socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr",
    }:
        if not args or not _allowed_host(args[0]):
            raise _blocked()
    elif event == "socket.getnameinfo":
        if not args or not _allowed_address(args[0]):
            raise _blocked()


def install() -> bool:
    """Install the guard once in this interpreter when the opt-in flag is set.

    Return whether this call installed any part of it.  Audit/class markers
    make this safe for pytest and ``sitecustomize`` to invoke independently.
    """
    global _AUDIT_INSTALLED

    if os.environ.get("SIMPLICIO_CORE_NO_NETWORK") != "1":
        return False
    installed = False
    if not _AUDIT_INSTALLED:
        sys.addaudithook(_network_audit_hook)
        _AUDIT_INSTALLED = True
        installed = True
    if getattr(subprocess.Popen, _CHILD_GUARD_MARKER, False):
        return installed

    # A Python child with -S/-I does not load sitecustomize.  Reject that
    # bypass from all standard child-launch APIs used by Loop and its plugins.
    original_popen = subprocess.Popen

    class GuardedPopen(original_popen):
        """A real Popen subclass, so normal type checks/subclassing survive."""
        def __init__(self, args, *args_rest, **kwargs):
            try:
                options = _popen_options(original_popen, args, args_rest, kwargs)
            except TypeError:
                # Delegate the invalid call unchanged so callers receive the
                # stdlib's native TypeError (including its public message).
                super().__init__(args, *args_rest, **kwargs)
                return
            prepared = _python_child_options(
                args, executable=options.get("executable"), kwargs=options,
            )
            if prepared is False or _is_python_guard_bypass(
                args, executable=options.get("executable"), kwargs=options,
            ):
                _blocked_no_site_child()
            if prepared is None:
                super().__init__(args, *args_rest, **kwargs)
                return
            # Calling with normalized keywords also replaces a positional env.
            super().__init__(args, **prepared)

    GuardedPopen.__name__ = original_popen.__name__
    # Both class- and initializer-level introspection retain the stdlib
    # signatures even though the implementation accepts a version-neutral
    # forwarding shape (the concrete Popen signature differs by Python).
    GuardedPopen.__signature__ = inspect.signature(original_popen)
    GuardedPopen.__init__.__signature__ = inspect.signature(original_popen.__init__)
    setattr(GuardedPopen, _CHILD_GUARD_MARKER, True)
    subprocess.Popen = GuardedPopen

    def _guard_exec(original, *, has_env):
        def guarded(path, args, *args_rest):
            options = {"env": args_rest[0]} if has_env and args_rest else {}
            prepared = _python_child_options(args, executable=path, kwargs=options)
            if prepared is False or _is_python_guard_bypass(args, executable=path, kwargs=options):
                return _blocked_no_site_child()
            if not has_env and prepared is not None and not _inherited_guard_is_delivered():
                return _blocked_no_site_child()
            if has_env and prepared is not None:
                args_rest = (prepared["env"],) + args_rest[1:]
            return original(path, args, *args_rest)
        return guarded

    for name in ("execv", "execve", "execvp", "execvpe"):
        original = getattr(os, name, None)
        if original is not None:
            setattr(os, name, _guard_exec(original, has_env=name.endswith("e")))

    def _guard_execl(original, *, has_env):
        def guarded(path, *args):
            options = {"env": args[-1]} if has_env and args else {}
            command = args[:-1] if has_env else args
            prepared = _python_child_options(command, executable=path, kwargs=options)
            if prepared is False or _is_python_guard_bypass(command, executable=path, kwargs=options):
                return _blocked_no_site_child()
            if not has_env and prepared is not None and not _inherited_guard_is_delivered():
                return _blocked_no_site_child()
            if has_env and prepared is not None:
                args = args[:-1] + (prepared["env"],)
            return original(path, *args)
        return guarded

    for name in ("execl", "execle", "execlp", "execlpe"):
        original = getattr(os, name, None)
        if original is not None:
            setattr(os, name, _guard_execl(original, has_env=name.endswith("e")))

    def _guard_spawn(original, *, has_env):
        def guarded(mode, path, args, *args_rest):
            options = {"env": args_rest[0]} if has_env and args_rest else {}
            prepared = _python_child_options(args, executable=path, kwargs=options)
            if prepared is False or _is_python_guard_bypass(args, executable=path, kwargs=options):
                return _blocked_no_site_child()
            if not has_env and prepared is not None and not _inherited_guard_is_delivered():
                return _blocked_no_site_child()
            if has_env and prepared is not None:
                args_rest = (prepared["env"],) + args_rest[1:]
            return original(mode, path, args, *args_rest)
        return guarded

    for name in ("spawnv", "spawnve", "spawnvp", "spawnvpe"):
        original = getattr(os, name, None)
        if original is not None:
            setattr(os, name, _guard_spawn(original, has_env=name.endswith("e")))

    def _guard_posix_spawn(original):
        def guarded(path, args, env, *args_rest, **kwargs):
            options = {"env": env}
            prepared = _python_child_options(args, executable=path, kwargs=options)
            if prepared is False or _is_python_guard_bypass(args, executable=path, kwargs=options):
                return _blocked_no_site_child()
            if prepared is not None:
                env = prepared["env"]
            return original(path, args, env, *args_rest, **kwargs)
        return guarded

    for name in ("posix_spawn", "posix_spawnp"):
        original = getattr(os, name, None)
        if original is not None:
            setattr(os, name, _guard_posix_spawn(original))

    def guarded_system(_command):
        # os.system always delegates to a shell; command text is opaque and
        # cannot prove that a nested Python child received the hook.
        return _blocked_no_site_child()

    os.system = guarded_system

    def guarded_os_popen(_command, _mode="r", _buffering=-1):
        # Keep the signature of os.popen while applying the same fail-closed
        # policy as shell=True and os.system.
        return _blocked_no_site_child()

    os.popen = guarded_os_popen
    return True
