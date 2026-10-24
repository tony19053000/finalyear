"""The bundled PostgreSQL for tests (V0.17 G2): binaries + a session-long cluster.

* :func:`managed_pg_bin_dir` - the trimmed, sha256-pinned PostgreSQL 16 that the
  controller bundle ships (``scripts/pg_bundle.py``; downloaded once into
  ``.cache/meshforge-pg`` or ``$MESHFORGE_PG_CACHE``). Skips with the reason when
  offline, not Linux x86_64, or glibc < 2.36; a checksum mismatch fails.
* :class:`SessionCluster` - a :class:`ManagedCluster` started on a dedicated
  event-loop thread that lives as long as the cluster (its parent-death signal
  follows that thread), for ``MESHFORGE_TEST_PG=managed``: the whole component
  suite then runs against the bundled PostgreSQL instead of the Docker image
  (SP2 R5 / G2 brief item 8).

Imported as the top-level module ``mf_managed_pg``.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import platform
import shutil
import socket
import sys
import tempfile
import threading
import urllib.error
from collections.abc import Coroutine, Iterator
from pathlib import Path
from typing import Any, Final, TypeVar

import pytest

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
_SCRIPTS: Final = str(REPO_ROOT / "scripts")
TEST_PG_ENV: Final = "MESHFORGE_TEST_PG"
"""``managed`` = the shared ``postgres_server`` fixture runs the bundled
PostgreSQL instead of the Docker container."""

_T = TypeVar("_T")


def managed_pg_requested() -> bool:
    return os.environ.get(TEST_PG_ENV, "").strip().lower() == "managed"


def _glibc_ok(min_glibc: str) -> bool:
    name, version = platform.libc_ver()
    if name != "glibc" or not version:
        return False
    need = tuple(int(p) for p in min_glibc.split("."))
    return tuple(int(p) for p in version.split(".")[:2]) >= need


def managed_pg_bin_dir() -> Path:
    """The trimmed bundle's ``bin`` (module docstring); skips when unusable."""
    from meshforge.controller.db_managed import LINUX_X86_64

    if not (sys.platform.startswith("linux") and platform.machine() == "x86_64"):
        pytest.skip("the pinned PostgreSQL bundle is Linux x86_64 only")
    if not _glibc_ok(LINUX_X86_64.min_glibc or "0"):
        pytest.skip(f"the bundle needs glibc >= {LINUX_X86_64.min_glibc}")
    if _SCRIPTS not in sys.path:
        sys.path.insert(0, _SCRIPTS)
    import pg_bundle
    from bundle_lib import load_pins

    try:
        pin = pg_bundle.pg_pin(load_pins(pg_bundle.PINS))
        trimmed = pg_bundle.ensure_trimmed(pin)
    except pg_bundle.BundleError as exc:  # checksum mismatch etc.: a real failure
        pytest.fail(str(exc))
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        pytest.skip(f"cannot download the PostgreSQL bundle (offline?): {exc}")
    return trimmed.bin_dir


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextlib.contextmanager
def short_dir(prefix: str = "mfpg-") -> Iterator[Path]:
    """A short temp dir (unix socket paths are limited to 107 bytes)."""
    path = Path(tempfile.mkdtemp(prefix=prefix, dir="/tmp"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


class LoopThread:
    """An event loop on its own thread; ``run(coro)`` from any other thread."""

    def __init__(self, name: str = "mf-managed-pg") -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self.loop.run_forever, name=name, daemon=True
        )
        self._thread.start()

    def run(self, coro: Coroutine[Any, Any, _T], timeout: float = 300.0) -> _T:
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def close(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=10)
        if not self.loop.is_running():
            self.loop.close()


class SessionCluster:
    """A started managed cluster for a whole pytest session (module docstring)."""

    def __init__(self, bin_dir: Path, root: Path) -> None:
        from meshforge.controller.db_managed import (
            ManagedCluster,
            ManagedLayout,
            PgBinaries,
        )

        self._loop = LoopThread()
        self.cluster = ManagedCluster(
            PgBinaries(bin_dir),
            ManagedLayout(root, socket_dir_override=root / "s"),
            port=free_port(),
        )

    def start(self) -> None:
        self._loop.run(self.cluster.start())

    def stop(self) -> None:
        try:
            self._loop.run(self.cluster.stop(), timeout=120)
        finally:
            self._loop.close()

    @property
    def superuser_password(self) -> str:
        admin = self.cluster.admin_settings()
        return admin.password.get_secret_value()
