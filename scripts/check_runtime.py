"""Bounded subprocess and reason-summary primitives for :mod:`scripts.check`."""

import os
import re
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Optional, Sequence, Set

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

PHASE_TIMEOUT_SECONDS = {
    "pytest_probe": 10.0,
    "pytest_collect": 60.0,
    "claims_audit": 120.0,
    "mirror_parity": 60.0,
    # A clean container can legitimately need more than five minutes for the
    # full core selection (3.6k+ tests).  Keep the phase strictly bounded, but
    # leave enough headroom for slower filesystems and cold interpreter starts.
    "core_tests": 600.0,
    "tests": 600.0,
    "stdlib_test": 60.0,
    "loop_contract": 60.0,
    "clean_env": 60.0,
    "token_budget": 60.0,
    "repo_budget": 60.0,
    "conformance": 60.0,
    "package_content": 300.0,
}
MAX_CAPTURE_BYTES = 1024 * 1024
POST_KILL_DRAIN_SECONDS = 1.0
POST_EXIT_DISCOVERY_SECONDS = 0.2
DESCENDANT_LEAK_EXIT_CODE = 125

REASON_CATEGORIES = (
    "regression",
    "capability_unavailable",
    "external_integration",
)
_SKIP_REASON_PATTERNS = {
    "capability_unavailable": re.compile(
        r"SKIPPED\s+\[(\d+)\].*CAPABILITY_UNAVAILABLE\[([^\]]+)\]"
    ),
    "external_integration": re.compile(
        r"SKIPPED\s+\[(\d+)\].*EXTERNAL_INTEGRATION_UNAVAILABLE\[([^\]]+)\]"
    ),
}
_EXTERNAL_EXCLUSION_PATTERN = re.compile(
    r"EXTERNAL_INTEGRATION_EXCLUDED\[([^\]]+)\]=(\d+)"
)


class CommandReason(str, Enum):
    """Machine-readable outcome reasons emitted by :func:`run_bounded`."""

    OK = "ok"
    TIMEOUT = "timeout"
    DESCENDANT_LEAK = "descendant_leak"
    CONTAINMENT_UNAVAILABLE = "containment_unavailable"


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    timed_out: bool = False
    stdout: str = ""
    stderr: str = ""
    reason: CommandReason = CommandReason.OK


@dataclass(frozen=True)
class GateResult:
    ok: bool
    reason_code: str = "ok"
    reasons: Dict[str, Dict[str, int]] = field(default_factory=dict)


class _ContainmentUnavailable(RuntimeError):
    """Containment discovery failed after spawn, retaining cleanup identity."""

    def __init__(self, descendants: Set[int], baseline: Set[int]):
        super().__init__("process containment unavailable")
        self.descendants = set(descendants)
        self.baseline = set(baseline)


def _repo_env(base: Optional[Dict[str, str]] = None, home: Optional[str] = None) -> Dict[str, str]:
    """Return the deliberately small environment used by gate subprocesses."""
    source = os.environ if base is None else base
    # This is intentionally an allowlist, not a denylist: credentials,
    # proxies, pytest injection flags and arbitrary CI variables must never
    # reach a supposedly hermetic gate subprocess.
    allowed = (
        "PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "SYSTEMROOT",
        "SystemRoot", "WINDIR", "COMSPEC", "ComSpec", "PATHEXT",
        "SIMPLICIO_CORE_NO_NETWORK", "SIMPLICIO_SYSTEM_TEST_NESTED",
    )
    env = {name: source[name] for name in allowed if source.get(name)}
    env["PATH"] = env.get("PATH", os.defpath)
    isolated_home = home or tempfile.gettempdir()
    env.update({
        "HOME": isolated_home,
        "USERPROFILE": isolated_home,
        "TEMP": isolated_home,
        "TMP": isolated_home,
        "TMPDIR": isolated_home,
        "PYTHONPATH": REPO,
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "PYTHONHASHSEED": "0",
        "PYTHONUTF8": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    return env


@dataclass(frozen=True)
class _DescendantDiscoveryError:
    """Failed scan carrying every descendant whose namespace PID is safe."""

    descendants: Set[int]
    reason: str


def _posix_descendants(root_pid: int):
    """Best-effort process-tree snapshot, including children that call setsid()."""
    if os.name == "nt" or not os.path.isdir("/proc"):
        return _DescendantDiscoveryError(set(), "procfs_unavailable")
    namespace_depth = _caller_namespace_depth()
    # Do not silently select host PIDs when NSpid data is absent or malformed:
    # signaling an ancestor would be materially worse than failing the phase.
    if namespace_depth is None:
        return _DescendantDiscoveryError(set(), "namespace_depth_unavailable")
    namespace_pids = {}
    parents = {}
    try:
        entries = os.listdir("/proc")
    except OSError:
        return _DescendantDiscoveryError(set(), "procfs_scan_unavailable")
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            host_pid = int(entry)
            values = _nspid_values("/proc/%s/status" % entry)
            visible_pid = _visible_namespace_pid(host_pid, values, namespace_depth)
            if visible_pid is not None:
                namespace_pids[host_pid] = visible_pid
            with open("/proc/%s/stat" % entry, "r") as handle:
                fields = handle.read().rsplit(") ", 1)[1].split()
            parents[host_pid] = int(fields[1])  # state, host-ppid, ...
        except (OSError, IndexError, StopIteration, ValueError):
            continue
    roots = {host_pid for host_pid, pid in namespace_pids.items() if pid == root_pid}
    if not roots:
        # ``root_pid`` names a process in the caller's namespace.  If it is
        # still addressable but its proc entry could not be mapped through
        # NSpid, accepting an empty tree would let an escaped child go unseen.
        try:
            os.kill(root_pid, 0)
        except ProcessLookupError:
            return set()
        except (PermissionError, OSError):
            return _DescendantDiscoveryError(set(), "root_identity_unavailable")
        return _DescendantDiscoveryError(set(), "root_identity_unavailable")
    found = set()
    incomplete = False
    changed = True
    while changed:
        changed = False
        children = [
            pid for pid, parent in parents.items()
            if parent in roots and pid not in roots
        ]
        # Add every safe sibling before reporting an incomplete mapping. This
        # lets the caller clean all identities it can prove without ever
        # signaling the host PID of the unmapped process.
        for pid in children:
            roots.add(pid)
            changed = True
            if pid in namespace_pids:
                found.add(namespace_pids[pid])
            else:
                incomplete = True
    if incomplete:
        return _DescendantDiscoveryError(
            found, "descendant_namespace_identity_unavailable",
        )
    return found


def _nspid_values(status_path: str):
    """Read the host-to-inner PID vector from one Linux status file."""
    try:
        with open(status_path, "r") as handle:
            line = next(row for row in handle if row.startswith("NSpid:"))
        return [int(value) for value in line.split()[1:]]
    except (OSError, StopIteration, ValueError):
        return None


def _caller_namespace_depth() -> Optional[int]:
    """Return the caller's index in Linux ``NSpid`` vectors."""
    values = _nspid_values("/proc/self/status")
    return len(values) - 1 if values else None


def _visible_namespace_pid(proc_pid: int, values, namespace_depth: Optional[int]):
    """Select the PID visible to the caller, not the target's deepest namespace."""
    if namespace_depth is None:
        return None
    if values is None:
        # Even in the host namespace, a relevant process without NSpid data
        # cannot be positively tied to the PID we would signal.  The gate
        # deliberately fails closed instead of falling back to procfs names.
        return None
    if namespace_depth < len(values):
        return values[namespace_depth]
    return None


def _terminate_and_reap(
    proc: subprocess.Popen, descendants: Optional[Set[int]] = None,
    *, baseline: Optional[Set[int]] = None, discover: bool = False,
) -> bool:
    """Terminate a bounded phase and its ordinary descendants, then reap its leader."""
    discovery_available = True
    known = descendants if descendants is not None else set()

    def refresh_known() -> None:
        """Capture late double-forks without discarding already-known PIDs."""
        nonlocal discovery_available
        if not discover or os.name == "nt":
            return
        if not _observe_descendants(proc.pid, known, baseline or set()):
            discovery_available = False

    if os.name == "nt":
        try:
            killed = subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
            if killed.returncode != 0 and proc.poll() is None:
                proc.kill()
        except (OSError, subprocess.SubprocessError):
            proc.kill()
    else:
        # A descendant can escape the process group with setsid().  PIDs seen
        # while the leader was alive are still ours and must be cleaned up.
        for pid in known:
            try:
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            if proc.poll() is None:
                proc.terminate()
        # The leader can have exited while a child still owns stdout/stderr.
        # Kill the group regardless of the leader's state so communicate() cannot
        # wait forever on that descendant.
        # A SIGTERM handler can fork+setsid after the first snapshot.  Re-scan
        # before SIGKILL, retaining known PIDs if discovery became unavailable.
        time.sleep(0.05)
        refresh_known()
        for pid in known:
            try:
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            if proc.poll() is None:
                proc.kill()
        refresh_known()
        for pid in known:
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        # A TERM handler may fork concurrently with both snapshots. Keep the
        # subreaper discovery window bounded but long enough to observe and
        # kill a late setsid child before returning.
        rescan_deadline = time.monotonic() + min(0.2, POST_KILL_DRAIN_SECONDS)
        while discover and time.monotonic() < rescan_deadline:
            refresh_known()
            for pid in known:
                if pid == proc.pid:
                    continue
                try:
                    os.kill(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    pass
            time.sleep(0.01)
    try:
        proc.wait(timeout=POST_KILL_DRAIN_SECONDS)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=POST_KILL_DRAIN_SECONDS)
    # A Linux subreaper becomes the parent of escaped grandchildren.  Merely
    # signaling them leaves zombies behind until this process exits, which is
    # both observable and can exhaust the PID table in a long gate run.
    pending = set(known)
    pending.discard(proc.pid)
    deadline = time.monotonic() + POST_KILL_DRAIN_SECONDS
    while pending and time.monotonic() < deadline:
        refresh_known()
        pending.update(known)
        pending.discard(proc.pid)
        reaped = _reap_adopted(pending, exclude=proc.pid)
        pending.difference_update(reaped)
        if pending:
            time.sleep(0.01)
    return discovery_available


def _containment_unavailable_result(
    proc: subprocess.Popen, descendants: Set[int], baseline: Set[int],
) -> CommandResult:
    """Clean every known post-spawn process before reporting lost discovery."""
    # Discovery is already unavailable.  Re-discovering here cannot add safe
    # identity and must not prevent deterministic cleanup of the PIDs observed
    # while containment was healthy.
    _terminate_and_reap(proc, descendants, baseline=baseline, discover=False)
    return CommandResult(
        126, reason=CommandReason.CONTAINMENT_UNAVAILABLE,
        stderr="CAPABILITY_UNAVAILABLE[process_containment]",
    )


def _pid_is_running(pid: int) -> Optional[bool]:
    """Return liveness, or ``None`` when namespace identity is unavailable."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    except OSError:
        return False
    try:
        entries = os.listdir("/proc")
    except OSError:
        return None
    namespace_depth = _caller_namespace_depth()
    if namespace_depth is None:
        return None
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            values = _nspid_values("/proc/%s/status" % entry)
            visible_pid = _visible_namespace_pid(int(entry), values, namespace_depth)
            if visible_pid is None:
                continue
            if visible_pid != pid:
                continue
            with open("/proc/%s/stat" % entry, "r") as handle:
                handle.read()
            # A zombie still occupies a PID and, for an adopted descendant,
            # proves that our cleanup has not reaped the child yet.  Treat it
            # as a survivor so the caller performs the final reap rather than
            # reporting a false-clean process tree.
            return True
        except (OSError, IndexError, ValueError):
            continue
    # The process can exit between kill(0) and /proc iteration.  Only report
    # it dead after confirming that race; otherwise lack of NSpid metadata is
    # a containment failure, never evidence that a live process disappeared.
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return None
    except OSError:
        return False
    return None


def _surviving_descendants(descendants: Set[int]) -> Optional[Set[int]]:
    """Return still-running descendants observed while the leader was alive."""
    if os.name == "nt":
        return set()
    survivors = set()
    for pid in descendants:
        running = _pid_is_running(pid)
        if running is None:
            return None
        if running:
            survivors.add(pid)
    return survivors


def _reap_adopted(descendants: Set[int], *, exclude: int) -> Set[int]:
    """Reap terminated orphan descendants adopted by the Linux subreaper.

    Without this, a grandchild killed by code inside the phase remains a zombie
    owned by the gate process until the whole phase exits.  That changes normal
    child semantics and makes in-phase liveness checks report a false leak.
    Never reap the phase leader: ``Popen`` must retain ownership of its status.
    """
    reaped = set()
    if os.name == "nt" or not hasattr(os, "waitpid"):
        return reaped
    for pid in descendants:
        if pid == exclude:
            continue
        try:
            waited, _status = os.waitpid(pid, os.WNOHANG)
        except (ChildProcessError, ProcessLookupError, OSError):
            continue
        if waited == pid:
            reaped.add(pid)
    return reaped


def _observe_descendants(root_pid: int, descendants: Set[int], baseline: Set[int]) -> bool:
    """Combine leader and adopted scans, retaining partial safe identities."""
    complete = True
    observed = _posix_descendants(root_pid)
    if isinstance(observed, _DescendantDiscoveryError):
        descendants.update(observed.descendants)
        complete = False
    else:
        descendants.update(observed)

    # Always perform this independent scan even when leader discovery was
    # partial: a setsid/double-fork may already have been adopted by the gate.
    adopted = _posix_descendants(os.getpid())
    if isinstance(adopted, _DescendantDiscoveryError):
        adopted_values = adopted.descendants
        complete = False
    else:
        adopted_values = adopted
    newly_adopted = adopted_values - baseline
    descendants.update(newly_adopted)
    descendants.difference_update(_reap_adopted(newly_adopted, exclude=root_pid))
    return complete


def _post_exit_survivors(
    root_pid: int, descendants: Set[int], baseline: Set[int],
) -> Optional[Set[int]]:
    """Stabilize late subreaper adoption after the phase leader exits.

    Leader status and pipe EOF are not an atomic process-tree boundary.  An
    escaped child can be reparented to this process immediately after both are
    observed, especially during an immediate double fork.  Keep discovery
    active for one short, bounded grace period; return early as soon as a live
    descendant is visible so cleanup is not delayed for actual leaks.
    """
    deadline = time.monotonic() + POST_EXIT_DISCOVERY_SECONDS
    while True:
        adopted = _posix_descendants(os.getpid())
        if isinstance(adopted, _DescendantDiscoveryError):
            descendants.update(adopted.descendants - baseline)
            return None
        newly_adopted = adopted - baseline
        descendants.update(newly_adopted)
        descendants.difference_update(
            _reap_adopted(newly_adopted, exclude=root_pid)
        )
        survivors = _surviving_descendants(descendants)
        if survivors is None or survivors:
            return survivors
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return set()
        time.sleep(min(0.01, remaining))


def _enable_linux_subreaper() -> bool:
    """Adopt escaped grandchildren so an immediate double-fork is observable.

    Linux only: ``PR_SET_CHILD_SUBREAPER`` makes this gate process the parent
    of orphaned descendants instead of init.  This closes the setsid/double
    fork race that a process-group-only implementation cannot observe.
    """
    if not (sys.platform.startswith("linux") and os.path.isdir("/proc")):
        return False
    try:
        import ctypes
        return ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) == 0
    except (AttributeError, OSError):
        return False


class _CaptureBuffer:
    """Keep deterministic head and tail evidence within one stream budget."""

    def __init__(self, limit: int = MAX_CAPTURE_BYTES):
        self.limit = limit
        self.head_limit = limit // 2
        self.head = bytearray()
        self.tail = deque()
        self.tail_size = 0
        self.total = 0

    def add(self, data: bytes) -> None:
        self.total += len(data)
        room = self.head_limit - len(self.head)
        if room > 0:
            self.head.extend(data[:room])
            data = data[room:]
        if not data:
            return
        self.tail.append(data)
        self.tail_size += len(data)
        tail_limit = self.limit - self.head_limit
        while self.tail and self.tail_size > tail_limit:
            excess = self.tail_size - tail_limit
            first = self.tail[0]
            if len(first) <= excess:
                self.tail.popleft()
                self.tail_size -= len(first)
            else:
                self.tail[0] = first[excess:]
                self.tail_size -= excess

    @staticmethod
    def _utf8_prefix(value: str, budget: int) -> str:
        """Take a valid UTF-8 prefix that fits ``budget`` bytes."""
        return value.encode("utf-8")[:budget].decode("utf-8", "ignore")

    @staticmethod
    def _utf8_suffix(value: str, budget: int) -> str:
        """Take a valid UTF-8 suffix that fits ``budget`` bytes."""
        return value.encode("utf-8")[-budget:].decode("utf-8", "ignore") if budget else ""

    def render(self, name: str) -> str:
        raw = bytes(self.head) + b"".join(self.tail)
        if self.total <= self.limit:
            rendered = raw.decode("utf-8", "replace")
            if len(rendered.encode("utf-8")) <= self.limit:
                return rendered
        marker = "\nOUTPUT_TRUNCATED[%s total_bytes=%d]\n" % (name, self.total)
        marker_size = len(marker.encode("utf-8"))
        # Replacement characters introduced by decoding malformed output use
        # three UTF-8 bytes.  Trim after decoding so the public string, not
        # just its raw pipe representation, obeys MAX_CAPTURE_BYTES.
        available = max(0, self.limit - marker_size)
        head = bytes(self.head).decode("utf-8", "replace")
        tail = b"".join(self.tail).decode("utf-8", "replace")
        prefix = self._utf8_prefix(head, available // 2)
        suffix = self._utf8_suffix(tail, available - len(prefix.encode("utf-8")))
        return prefix + marker + suffix


def _bounded_capture_threads(
    proc: subprocess.Popen, timeout: float, baseline: Set[int], *, discover: bool,
):
    """Portable pipe capture for hosts where selectors reject pipe HANDLEs."""
    chunks = None
    finished = None
    readers = []
    reader_errors = []
    pipes = ()
    descendants = set()
    cleanup_started = False

    def read_pipe(name, pipe) -> None:
        try:
            while True:
                data = os.read(pipe.fileno(), 65536)
                if not data:
                    return
                chunks[name].add(data)
        except (OSError, ValueError) as exc:
            reader_errors.append(exc)
            return
        finally:
            finished[name].set()

    def terminate_once() -> None:
        nonlocal cleanup_started
        if cleanup_started:
            return
        cleanup_started = True
        _terminate_and_reap(proc, descendants, baseline=baseline, discover=discover)

    try:
        # Every allocation below occurs after the process was spawned and is
        # therefore inside the same cleanup region as polling and rendering.
        pipes = tuple(pipe for pipe in (proc.stdout, proc.stderr) if pipe is not None)
        chunks = {"stdout": _CaptureBuffer(), "stderr": _CaptureBuffer()}
        finished = {"stdout": threading.Event(), "stderr": threading.Event()}
        for name, pipe in (("stdout", proc.stdout), ("stderr", proc.stderr)):
            if pipe is None:
                finished[name].set()
                continue
            reader = threading.Thread(
                target=read_pipe, args=(name, pipe),
                name="simplicio-check-%s" % name, daemon=True,
            )
            reader.start()
            readers.append(reader)
        deadline = time.monotonic() + timeout
        timed_out = False
        while True:
            if discover and not _observe_descendants(proc.pid, descendants, baseline):
                raise _ContainmentUnavailable(descendants, baseline)
            if proc.poll() is not None and all(
                event.is_set() for event in finished.values()
            ):
                break
            if reader_errors:
                raise reader_errors[0]
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                terminate_once()
                break
            time.sleep(min(0.02, remaining))
        if reader_errors:
            raise reader_errors[0]

        # A descendant can retain an inherited pipe after the leader exits.
        # Give readers one bounded drain interval before finally closing our
        # handles and letting daemon readers unwind.
        drain_deadline = time.monotonic() + POST_KILL_DRAIN_SECONDS
        for reader in readers:
            reader.join(max(0.0, drain_deadline - time.monotonic()))
        return (
            chunks["stdout"].render("stdout"), chunks["stderr"].render("stderr"),
            timed_out, False,
        )
    except BaseException:
        # Covers setup plus every operational step after it: poll, wait,
        # reader coordination and rendering. A spawned phase never escapes an
        # exceptional capture path.
        terminate_once()
        raise
    finally:
        for pipe in pipes:
            try:
                pipe.close()
            except (OSError, ValueError):
                pass
        for reader in readers:
            reader.join(0.05)


def _bounded_capture(
    proc: subprocess.Popen, timeout: float, baseline: Set[int], *, discover: bool,
):
    """Read pipes without an unbounded post-timeout communicate()."""
    if os.name == "nt":
        return _bounded_capture_threads(
            proc, timeout, baseline, discover=discover,
        )
    selector = None
    chunks = None
    pipes = ()
    descendants = set()
    deadline = time.monotonic() + timeout
    timed_out = False
    try:
        pipes = tuple(pipe for pipe in (proc.stdout, proc.stderr) if pipe is not None)
        selector = selectors.DefaultSelector()
        chunks = {"stdout": _CaptureBuffer(), "stderr": _CaptureBuffer()}
        for name, pipe in (("stdout", proc.stdout), ("stderr", proc.stderr)):
            if pipe is not None:
                os.set_blocking(pipe.fileno(), False)
                selector.register(pipe, selectors.EVENT_READ, name)
        while selector.get_map() or proc.poll() is None:
            if discover and not _observe_descendants(proc.pid, descendants, baseline):
                raise _ContainmentUnavailable(descendants, baseline)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            for key, _ in selector.select(min(0.05, remaining)):
                try:
                    data = os.read(key.fileobj.fileno(), 65536)
                except BlockingIOError:
                    continue
                if not data:
                    selector.unregister(key.fileobj)
                    continue
                name = key.data
                chunks[name].add(data)
        if timed_out:
            if not _terminate_and_reap(
                proc, descendants, baseline=baseline, discover=discover,
            ):
                raise _ContainmentUnavailable(descendants, baseline)
            # Pipes inherited by an escaped process may never reach EOF.  Drain
            # only for a fixed interval and then close our ends.
            drain_deadline = time.monotonic() + POST_KILL_DRAIN_SECONDS
            while selector.get_map() and time.monotonic() < drain_deadline:
                for key, _ in selector.select(min(0.05, drain_deadline - time.monotonic())):
                    try:
                        data = os.read(key.fileobj.fileno(), 65536)
                    except BlockingIOError:
                        continue
                    if not data:
                        selector.unregister(key.fileobj)
                        continue
                    name = key.data
                    chunks[name].add(data)
        if discover:
            survivors = _post_exit_survivors(proc.pid, descendants, baseline)
            if survivors is None:
                raise _ContainmentUnavailable(descendants, baseline)
        else:
            survivors = set()
        leaked_descendant = bool(survivors)
        if leaked_descendant:
            if not _terminate_and_reap(
                proc, survivors, baseline=baseline, discover=discover,
            ):
                raise _ContainmentUnavailable(descendants, baseline)
        return (chunks["stdout"].render("stdout"),
                chunks["stderr"].render("stderr"), timed_out,
                leaked_descendant)
    except _ContainmentUnavailable:
        raise
    except BaseException:
        # Selector creation/registration and pipe setup happen after spawn.
        # Any failure there must still terminate the phase before propagating.
        _terminate_and_reap(
            proc, descendants, baseline=baseline, discover=False,
        )
        raise
    finally:
        try:
            if selector is not None:
                for key in list(selector.get_map().values()):
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                selector.close()
        finally:
            for pipe in pipes:
                try:
                    pipe.close()
                except (OSError, ValueError):
                    pass


def run_bounded(
    argv: Sequence[str],
    *,
    phase: str,
    cwd: str = REPO,
    env: Optional[Dict[str, str]] = None,
    capture_output: bool = False,
    timeout_seconds: Optional[float] = None,
) -> CommandResult:
    """Run a phase in its own process group and classify a bounded timeout."""
    timeout = PHASE_TIMEOUT_SECONDS[phase] if timeout_seconds is None else timeout_seconds
    isolated_home = tempfile.mkdtemp(prefix="simplicio-check-")
    try:
        # Linux receives the strongest contract: procfs plus a subreaper can
        # discover escaped double-forks.  Other supported hosts still run in
        # a fresh process group (Windows uses taskkill /T); their bounded
        # lifecycle does not pretend to provide Linux-only escape discovery.
        discover = (
            os.name != "nt" and sys.platform.startswith("linux")
            and os.path.isdir("/proc")
        )
        if discover and not _enable_linux_subreaper():
            return CommandResult(
                126, reason=CommandReason.CONTAINMENT_UNAVAILABLE,
                stderr="CAPABILITY_UNAVAILABLE[process_containment]",
            )
        baseline: Set[int] = set()
        if discover:
            observed_baseline = _posix_descendants(os.getpid())
            if isinstance(observed_baseline, _DescendantDiscoveryError):
                return CommandResult(
                    126, reason=CommandReason.CONTAINMENT_UNAVAILABLE,
                    stderr="CAPABILITY_UNAVAILABLE[process_containment]",
                )
            baseline = observed_baseline
        kwargs = {
            "cwd": cwd,
            "env": _repo_env(env, isolated_home),
            "stdin": subprocess.DEVNULL,
            "text": True,
        }
        if capture_output:
            kwargs.update({"stdout": subprocess.PIPE, "stderr": subprocess.PIPE})
        kwargs["start_new_session"] = True
        proc = subprocess.Popen(list(argv), **kwargs)
        if capture_output:
            try:
                stdout, stderr, timed_out, leaked_descendant = _bounded_capture(
                    proc, timeout, baseline, discover=discover,
                )
            except _ContainmentUnavailable as exc:
                return _containment_unavailable_result(
                    proc, exc.descendants, exc.baseline,
                )
            if not timed_out:
                if leaked_descendant:
                    return CommandResult(
                        DESCENDANT_LEAK_EXIT_CODE, stdout=stdout, stderr=stderr,
                        reason=CommandReason.DESCENDANT_LEAK,
                    )
                return CommandResult(proc.returncode, stdout=stdout, stderr=stderr)
        else:
            descendants = set()
            deadline = time.monotonic() + timeout
            while proc.poll() is None and time.monotonic() < deadline:
                if discover and not _observe_descendants(proc.pid, descendants, baseline):
                    return _containment_unavailable_result(proc, descendants, baseline)
                time.sleep(0.02)
            if proc.poll() is not None:
                if discover:
                    survivors = _post_exit_survivors(
                        proc.pid, descendants, baseline,
                    )
                    if survivors is None:
                        return _containment_unavailable_result(proc, descendants, baseline)
                else:
                    survivors = set()
                if survivors:
                    if not _terminate_and_reap(
                        proc, survivors, baseline=baseline, discover=discover,
                    ):
                        return _containment_unavailable_result(proc, descendants, baseline)
                    return CommandResult(
                        DESCENDANT_LEAK_EXIT_CODE, reason=CommandReason.DESCENDANT_LEAK,
                    )
                return CommandResult(proc.returncode)
            discovered = _terminate_and_reap(
                proc, descendants, baseline=baseline, discover=discover,
            )
            # A double-fork can be adopted only after the process-group kill.
            # Re-scan after termination and reap those late adoptions before
            # returning the timeout result from the non-capturing path.
            if discover:
                survivors = _post_exit_survivors(
                    proc.pid, descendants, baseline,
                )
            else:
                survivors = set()
            if not discovered or survivors is None:
                return _containment_unavailable_result(proc, descendants, baseline)
            if survivors:
                if not _terminate_and_reap(
                    proc, survivors, baseline=baseline, discover=discover,
                ):
                    return _containment_unavailable_result(proc, descendants, baseline)
            stdout, stderr = "", ""
            timed_out = True
        if timed_out:
            print(
                "TIMEOUT[%s_timeout]: phase exceeded %.1fs and its process group was terminated"
                % (phase, timeout),
                file=sys.stderr,
            )
            return CommandResult(
                proc.returncode if proc.returncode is not None else 124,
                timed_out=True,
                stdout=stdout or "",
                stderr=stderr or "",
                reason=CommandReason.TIMEOUT,
            )
    finally:
        shutil.rmtree(isolated_home, ignore_errors=True)


def gate_result(phase: str, command: CommandResult) -> GateResult:
    if command.timed_out:
        return GateResult(False, "%s_timeout" % phase)
    if command.reason == CommandReason.DESCENDANT_LEAK:
        return GateResult(False, "%s_descendant_leak" % phase)
    if command.reason == CommandReason.CONTAINMENT_UNAVAILABLE:
        return GateResult(False, "%s_containment_unavailable" % phase)
    if command.returncode == 0:
        return GateResult(True)
    if phase in {"core_tests", "tests"} and command.returncode == 5:
        return GateResult(False, "pytest_no_tests_collected")
    return GateResult(False, "%s_failed" % phase)


def pytest_collected_count(output: str) -> Optional[int]:
    """Return pytest's selected count from a collect-only summary."""
    matches = re.findall(r"(?:^|\n)(\d+)(?:/\d+)?\s+tests?\s+collected\b", output)
    return int(matches[-1]) if matches else None


def pytest_summary_count(output: str, outcome: str) -> int:
    """Read one outcome count from pytest's authoritative final summary line."""
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    match = re.search(r"(?:^|,\s*)(\d+)\s+%s\b" % re.escape(outcome), lines[-1] if lines else "")
    return int(match.group(1)) if match else 0


def classify_pytest_reasons(output: str) -> Dict[str, Dict[str, int]]:
    """Extract explicit pytest skip markers without guessing from prose."""
    reasons = {category: {} for category in REASON_CATEGORIES[1:]}
    for line in output.splitlines():
        for category, pattern in _SKIP_REASON_PATTERNS.items():
            match = pattern.search(line)
            if match:
                count, code = int(match.group(1)), match.group(2)
                reasons[category][code] = reasons[category].get(code, 0) + count
        exclusion = _EXTERNAL_EXCLUSION_PATTERN.search(line)
        if exclusion:
            code, count = exclusion.group(1), int(exclusion.group(2))
            reasons["external_integration"][code] = (
                reasons["external_integration"].get(code, 0) + count
            )
    return reasons


def aggregate_reason_groups(results: Dict[str, GateResult]) -> Dict[str, Dict[str, int]]:
    """Group failed phases and explicit environmental skips into stable categories."""
    grouped = {category: {} for category in REASON_CATEGORIES}
    for phase in sorted(results):
        result = results[phase]
        if not result.ok:
            code = result.reason_code or "%s_failed" % phase
            # Process containment can be unavailable because of a restricted
            # platform/kernel.  It is an explicit capability result, not a
            # product regression, and must remain separately actionable.
            category = (
                "capability_unavailable"
                if code.endswith("_containment_unavailable")
                else "regression"
            )
            grouped[category][code] = grouped[category].get(code, 0) + 1
        for category in REASON_CATEGORIES[1:]:
            for code, count in result.reasons.get(category, {}).items():
                grouped[category][code] = grouped[category].get(code, 0) + count
    return grouped


def print_reason_summary(results: Dict[str, GateResult]) -> None:
    print("\nreason-code summary:")
    for category, codes in aggregate_reason_groups(results).items():
        detail = ", ".join("%s=%d" % item for item in sorted(codes.items())) or "none"
        print("  %s: %s" % (category, detail))
