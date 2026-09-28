"""Lifecycle machinery for MeshForge test containers (DEFECT-0.1.8-01).

Imported as the top-level module ``containers``: ``tests/conftest.py`` puts
``tests/_support`` on ``sys.path`` (``tests/`` has no ``__init__.py``; pytest runs in
prepend import mode) and ``[tool.mypy] mypy_path`` lists it for type checking.

What lives here (the policy is documented in ``tests/component/conftest.py``):

* the Docker **label scheme** every test container carries, including the owner's
  PID-namespace identity and process start time;
* the **stale sweep** that removes labelled containers whose owner process is gone;
* :func:`sigint_deferred`, the **SIGINT shield** used while removing containers;
* :class:`ContainerCleanup` and the process-wide **cleanup registry**.

Everything is stateless apart from the registry and a cached namespace identity,
and best-effort: Docker errors are logged, never raised.
"""

from __future__ import annotations

import atexit
import contextlib
import functools
import logging
import os
import signal
import socket
import sys
import threading
from collections.abc import Iterator
from types import FrameType
from typing import Any, Final
from uuid import uuid4

LABEL_TEST: Final = "meshforge.test"
LABEL_OWNER_PID: Final = "meshforge.test.owner-pid"
LABEL_OWNER_START: Final = "meshforge.test.owner-start"
LABEL_OWNER_HOST: Final = "meshforge.test.owner-host"
LABEL_OWNER_NS: Final = "meshforge.test.owner-ns"
LABEL_RUN_ID: Final = "meshforge.test.run-id"
RUN_ID_ENV: Final = "MESHFORGE_TEST_RUN_ID"

RUN_ID: Final = os.environ.get(RUN_ID_ENV) or uuid4().hex
#: Not `sys.platform` inline: mypy would treat the non-Linux fallbacks as unreachable.
_IS_LINUX: Final[bool] = sys.platform.startswith("linux")
_log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- identity


@functools.cache
def pid_namespace_id() -> str | None:
    """Identity of the PID namespace this process lives in, or ``None`` if unknown.

    Two processes may compare PIDs only if their identities are equal:

    * Linux (incl. each WSL2 distro and each container): ``linux:<boot_id>:<ino>``
      — the kernel boot id plus the inode of ``/proc/self/ns/pid``. WSL2 distros
      share one kernel (same ``boot_id``) but not a PID namespace (different
      inode); a reboot changes ``boot_id``.
    * Windows: ``win32:<MachineGuid>`` — one PID namespace per Windows install.
    * Anything else: ``None`` (the sweep then removes nothing).
    """
    if sys.platform == "win32":
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Cryptography",
                0,
                winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
            ) as key:
                guid, _ = winreg.QueryValueEx(key, "MachineGuid")
        except OSError:
            return None
        return f"win32:{guid}" if guid else None
    if _IS_LINUX:
        try:
            with open("/proc/sys/kernel/random/boot_id", encoding="ascii") as f:
                boot_id = f.read().strip()
            ns_inode = os.stat("/proc/self/ns/pid").st_ino
        except OSError:
            return None
        return f"linux:{boot_id}:{ns_inode}" if boot_id else None
    return None


def process_start_time(pid: int) -> str | None:
    """Opaque, exact start-time token for ``pid``; ``None`` if it cannot be read.

    Same PID + different token means the PID was reused by another process. Linux
    uses ``/proc/<pid>/stat`` field 22 (clock ticks since boot, exact integer);
    elsewhere ``psutil.Process(pid).create_time()`` (millisecond precision).
    Tokens are only compared within one namespace identity, so they always come
    from the same method.
    """
    if _IS_LINUX:
        try:
            with open(f"/proc/{pid}/stat", encoding="ascii", errors="replace") as f:
                stat = f.read()
        except OSError:
            return None
        # Field 2 (comm) is in parentheses and may contain spaces: split after it.
        fields = stat.rpartition(")")[2].split()
        # fields[0] is field 3 (state), so field 22 (starttime) is fields[19].
        return fields[19] if len(fields) > 19 else None
    try:
        import psutil  # type: ignore[import-untyped]  # no stubs; agent extra only
    except ImportError:
        return None
    try:
        return f"{psutil.Process(pid).create_time():.3f}"
    except Exception:  # noqa: BLE001 - NoSuchProcess / AccessDenied / ...
        return None


def pid_alive(pid: int) -> bool | None:
    """``True``/``False`` if known, ``None`` if liveness cannot be determined."""
    try:
        import psutil
    except ImportError:
        psutil = None
    if psutil is not None:
        return bool(psutil.pid_exists(pid))
    if sys.platform == "win32":
        return None  # os.kill(pid, 0) is CTRL_C_EVENT on Windows: never use it
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by another user
    except OSError:
        return None
    return True


def owner_is_gone(pid: int, recorded_start: str | None) -> bool:
    """``True`` only if the owner process is *known* to be gone.

    Gone = no process with ``pid``, or a process whose start time differs from the
    recorded one (PID reuse). Anything undeterminable counts as alive.
    """
    alive = pid_alive(pid)
    if alive is None:
        return False
    if not alive:
        return True
    if not recorded_start:
        return False  # label from an older scheme: cannot detect reuse, keep it
    current = process_start_time(pid)
    return current is not None and current != recorded_start


def meshforge_test_labels() -> dict[str, str]:
    """Labels every MeshForge test container carries (see component conftest)."""
    pid = os.getpid()
    return {
        LABEL_TEST: "1",
        LABEL_OWNER_PID: str(pid),
        LABEL_OWNER_START: process_start_time(pid) or "",
        LABEL_OWNER_HOST: socket.gethostname(),
        LABEL_OWNER_NS: pid_namespace_id() or "",
        LABEL_RUN_ID: RUN_ID,
    }


# ------------------------------------------------------------------------------ sweep


def sweep_stale_test_containers(client: Any) -> list[str]:
    """Remove labelled test containers whose owner process is known to be gone.

    ``client`` is a ``docker.DockerClient``. A container is removed only if it is
    labelled ``meshforge.test=1``, its ``owner-ns`` equals this process's
    :func:`pid_namespace_id` (never when that is unknown), its ``owner-host``
    equals this host name, and :func:`owner_is_gone`. This is a *global* sweep:
    it considers every such container on the daemon, not only this run's.
    Best-effort: errors are logged, never raised. Returns the removed ids.
    """
    removed: list[str] = []
    namespace = pid_namespace_id()
    if namespace is None:
        return removed
    host = socket.gethostname()
    try:
        candidates = client.containers.list(
            all=True, filters={"label": f"{LABEL_TEST}=1"}, ignore_removed=True
        )
    except Exception as exc:  # noqa: BLE001 - best-effort sweep
        _log.warning("stale test-container sweep: listing failed: %s", exc)
        return removed
    for container in candidates:
        labels = container.labels or {}
        if labels.get(LABEL_OWNER_NS) != namespace:
            continue  # other PID namespace (WSL vs Windows, CI job, container...)
        if labels.get(LABEL_OWNER_HOST) != host:
            continue
        try:
            owner = int(labels.get(LABEL_OWNER_PID, ""))
        except ValueError:
            continue
        if not owner_is_gone(owner, labels.get(LABEL_OWNER_START) or None):
            continue
        try:
            container.remove(force=True, v=True)
        except Exception as exc:  # noqa: BLE001 - e.g. already being removed
            _log.warning("stale test-container sweep: %s: %s", container.id, exc)
            continue
        removed.append(container.id)
    return removed


# ----------------------------------------------------------------------- SIGINT shield


class _SigintRecorder:
    """SIGINT handler that only records that the signal arrived."""

    def __init__(self) -> None:
        self.received = False

    def __call__(self, signum: int, frame: FrameType | None) -> None:
        self.received = True


@contextlib.contextmanager
def sigint_deferred() -> Iterator[None]:
    """Defer Ctrl-C until the block ends, then raise ``KeyboardInterrupt``.

    Python runs signal handlers only in the main thread, whichever OS thread the
    signal hits, so temporarily replacing the SIGINT handler shields the block
    against a SIGINT delivered to *any* thread, on POSIX and Windows alike. The
    previous handler is restored on exit; if a SIGINT arrived meanwhile,
    ``KeyboardInterrupt`` is raised after the block completed (unless the block
    raised). Off the main thread (handlers cannot be installed there) and when
    already inside a shield, the block runs unshielded / under the outer shield.
    """
    if threading.current_thread() is not threading.main_thread() or isinstance(
        signal.getsignal(signal.SIGINT), _SigintRecorder
    ):
        yield
        return
    recorder = _SigintRecorder()
    try:
        previous = signal.signal(signal.SIGINT, recorder)
    except (ValueError, OSError):  # e.g. interpreter shutting down
        yield
        return
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)
    if recorder.received:
        raise KeyboardInterrupt


# --------------------------------------------------------------------- cleanup registry


class ContainerCleanup:
    """Idempotent, interrupt-shielded removal of one testcontainers container."""

    def __init__(self, container: Any) -> None:
        self._container = container
        self._done = False

    def __call__(self) -> None:
        if self._done:
            return
        with sigint_deferred():
            self.remove_unshielded()

    def remove_unshielded(self) -> None:
        """Remove the container; callers provide the SIGINT shield."""
        if self._done:
            return
        try:
            wrapped = self._container.get_wrapped_container()
            if wrapped is not None:
                wrapped.remove(force=True, v=True)
        except Exception as exc:  # noqa: BLE001 - gone / removal in progress
            _log.debug("test container removal: %s", exc)
        with contextlib.suppress(Exception):
            self._container.get_docker_client().client.close()
        # Marked done only after the removal attempt: an exception escaping above
        # (none should) leaves the atexit hook armed for another try.
        self._done = True
        atexit.unregister(self)
        with contextlib.suppress(ValueError):
            _ACTIVE_CLEANUPS.remove(self)


_ACTIVE_CLEANUPS: list[ContainerCleanup] = []


def register_cleanup(container: Any) -> ContainerCleanup:
    """Arm removal of ``container`` at interpreter exit; call before ``start()``.

    atexit runs handlers LIFO: testcontainers registers its Ryuk-stopping hook
    inside ``start()``, i.e. after this one, so at exit Ryuk is stopped *before*
    this cleanup runs. That is fine — removal here uses the Docker API directly —
    but it is why Ryuk is no safety net on exits that run ``atexit``.
    """
    cleanup = ContainerCleanup(container)
    atexit.register(cleanup)
    _ACTIVE_CLEANUPS.append(cleanup)
    return cleanup


def run_all_cleanups() -> None:
    """Remove every registered container now, under one SIGINT shield."""
    with sigint_deferred():
        while _ACTIVE_CLEANUPS:
            _ACTIVE_CLEANUPS.pop().remove_unshielded()
