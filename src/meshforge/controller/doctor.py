"""``meshforge-controller doctor``: can this controller install run? (V0.17 G2, SP2 §7.6)

One function, :func:`run_checks`, used by the CLI (human or ``--json`` output,
exit non-zero on any failure) and by the controller's setup page (V0.17 G2
"create fabric"), so both say the same thing. Every check is a
:class:`Check` with ``ok`` / ``warn`` / ``fail``: ``warn`` is something that is
expected before the first start or the fabric setup (no data dir yet, no CA
yet) or worth knowing (little disk); ``fail`` is something that stops the
controller.

Managed mode (D390): the bundled PostgreSQL runs (``postgres --version``, which
also proves the glibc is new enough - D398), the glibc version itself, the
database port is free (or used by this controller's own running cluster), the
password files are private, the data dir is this major. Always: free disk at the
state folder, the fabric CA, the dashboard files. External mode: the database
answers.

Nothing here prints or logs a password; the checks only read file modes and
owners (``read_private``).
"""

from __future__ import annotations

import platform
import shutil
import socket
import sys
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Final

from meshforge.common.errors import MeshForgeError

if TYPE_CHECKING:
    from meshforge.controller.settings import ControllerSettings

__all__ = [
    "DISK_FAIL_BYTES",
    "DISK_WARN_BYTES",
    "Check",
    "CheckStatus",
    "DoctorReport",
    "run_checks",
]

DISK_WARN_BYTES: Final = 2 * 1024**3
"""Below 2 GiB free at the state folder: a warning (blobs and the database grow)."""
DISK_FAIL_BYTES: Final = 512 * 1024**2
"""Below 512 MiB: a failure (PostgreSQL refuses writes when the disk fills)."""
_MIN_GLIBC: Final = (2, 36)


class CheckStatus(StrEnum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    status: CheckStatus
    detail: str
    hint: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "status": self.status.value,
            "detail": self.detail,
            "hint": self.hint,
        }


@dataclass(frozen=True, slots=True)
class DoctorReport:
    checks: tuple[Check, ...]

    @property
    def ok(self) -> bool:
        return all(c.status is not CheckStatus.FAIL for c in self.checks)

    def as_dict(self) -> dict[str, object]:
        return {"ok": self.ok, "checks": [c.as_dict() for c in self.checks]}

    def lines(self) -> list[str]:
        out = []
        for check in self.checks:
            out.append(f"[{check.status.value:>4}] {check.name}: {check.detail}")
            if check.hint and check.status is not CheckStatus.OK:
                out.append(f"       -> {check.hint}")
        out.append("all checks passed" if self.ok else "some checks FAILED")
        return out


# ------------------------------------------------------------------ checks
def _glibc() -> Check:
    if not sys.platform.startswith("linux"):
        return Check("glibc", CheckStatus.OK, f"not Linux ({sys.platform})")
    name, version = platform.libc_ver()
    if name != "glibc" or not version:
        return Check(
            "glibc",
            CheckStatus.FAIL,
            f"not glibc ({name or 'unknown'})",
            "the bundled PostgreSQL needs glibc >= 2.36 (Ubuntu 24.04+)",
        )
    parts = tuple(int(p) for p in version.split(".")[:2] if p.isdigit())
    if parts < _MIN_GLIBC:
        return Check(
            "glibc",
            CheckStatus.FAIL,
            f"glibc {version}",
            "the controller needs Ubuntu 24.04 or newer (glibc >= 2.36, D398)",
        )
    return Check("glibc", CheckStatus.OK, f"glibc {version}")


def _binaries(settings: ControllerSettings) -> Check:
    from meshforge.controller.db_managed.binaries import PgBinaries
    from meshforge.controller.db_managed.runtime import pg_bin_dir

    try:
        bin_dir = pg_bin_dir(settings)
        version = PgBinaries(bin_dir).check()
    except MeshForgeError as exc:
        return Check(
            "postgresql binaries",
            CheckStatus.FAIL,
            str(exc),
            "reinstall the meshforge-controller package",
        )
    return Check(
        "postgresql binaries", CheckStatus.OK, f"PostgreSQL {version} in {bin_dir}"
    )


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _db_port(settings: ControllerSettings, cluster_running: bool) -> Check:
    port = settings.controller.db.port
    name = "database port"
    if cluster_running:
        return Check(
            name, CheckStatus.OK, f"127.0.0.1:{port} (this controller's database)"
        )
    if _port_free(port):
        return Check(name, CheckStatus.OK, f"127.0.0.1:{port} is free")
    return Check(
        name,
        CheckStatus.FAIL,
        f"127.0.0.1:{port} is used by another program",
        "stop that program or set [controller.db] port in /etc/meshforge/controller.toml",
    )


def _password_files(root: Path) -> Check:
    from meshforge.common.private_files import PrivateFileError
    from meshforge.controller.db_managed.secret import read_password_file

    name = "database passwords"
    files = [root / "admin.password", root / "app.password"]
    missing = [p.name for p in files if not p.exists()]
    if len(missing) == len(files):
        return Check(
            name, CheckStatus.WARN, "not created yet", "created on the first start"
        )
    for path in files:
        if not path.exists():
            continue
        try:
            read_password_file(path)
        except PrivateFileError as exc:
            return Check(
                name,
                CheckStatus.FAIL,
                str(exc),
                "only the service user may read them: "
                f"sudo chown meshforge: {path} && sudo chmod 600 {path}",
            )
    if missing:
        return Check(
            name,
            CheckStatus.WARN,
            f"{', '.join(missing)} missing",
            "created on the next start",
        )
    return Check(name, CheckStatus.OK, "private (0600, owned by this user)")


def _data_major(root: Path) -> Check:
    from meshforge.controller.db_managed.binaries import BUNDLED_MAJOR
    from meshforge.controller.db_managed.pidfile import read_cluster_major

    data = root / "data"
    name = "database files"
    if not data.exists():
        return Check(
            name, CheckStatus.WARN, "not initialised yet", "created on the first start"
        )
    major = read_cluster_major(data)
    if major is None:
        return Check(
            name,
            CheckStatus.FAIL,
            f"{data} holds no PostgreSQL cluster",
            "move that folder away; the controller never touches it",
        )
    if major != BUNDLED_MAJOR:
        return Check(
            name,
            CheckStatus.FAIL,
            f"made by PostgreSQL {major}, this controller bundles {BUNDLED_MAJOR}",
            "restore a pg_dump backup into a fresh database (db restore)",
        )
    return Check(name, CheckStatus.OK, f"PostgreSQL {major} cluster in {data}")


def _disk(path: Path) -> Check:
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        free = shutil.disk_usage(probe).free
    except OSError as exc:
        return Check("free disk", CheckStatus.WARN, f"cannot measure: {exc}")
    text = f"{free / 1024**3:.1f} GiB free at {probe}"
    if free < DISK_FAIL_BYTES:
        return Check("free disk", CheckStatus.FAIL, text, "free some disk space")
    if free < DISK_WARN_BYTES:
        return Check("free disk", CheckStatus.WARN, text, "the database and blobs grow")
    return Check("free disk", CheckStatus.OK, text)


def _ca(settings: ControllerSettings) -> Check:
    from meshforge.controller.fabric_ca import fabric_present

    if fabric_present(settings.ca_dir):
        return Check("fabric CA", CheckStatus.OK, f"present in {settings.ca_dir}")
    return Check(
        "fabric CA",
        CheckStatus.WARN,
        "not created yet",
        "create the fabric on the setup page (or meshforge-controller fabric create)",
    )


def _spa(dist: Path | None) -> Check:
    from meshforge.controller.web.spa import DEFAULT_DIST

    index = (dist or DEFAULT_DIST) / "index.html"
    if index.is_file():
        return Check("dashboard", CheckStatus.OK, "built and installed")
    return Check(
        "dashboard",
        CheckStatus.FAIL,
        f"{index} is missing",
        "reinstall the package (a developer: npm --prefix frontend run build)",
    )


def _external_db(settings: ControllerSettings) -> Check:
    import asyncio

    from meshforge.controller.store.engine import check_connection, create_engine

    async def go() -> None:
        engine = create_engine(settings.database)
        try:
            await check_connection(engine, settings.database)
        finally:
            await engine.dispose()

    try:
        asyncio.run(go())
    except MeshForgeError as exc:
        return Check(
            "database",
            CheckStatus.FAIL,
            str(exc),
            "start that PostgreSQL or fix [database] in the config file",
        )
    return Check(
        "database", CheckStatus.OK, f"reachable at {settings.database.display_url()}"
    )


def run_checks(
    settings: ControllerSettings,
    *,
    dist: Path | None = None,
    include_external_db: bool = True,
    cluster_running: Callable[[], bool] | None = None,
) -> DoctorReport:
    """Every check for these settings (module docstring). Blocking (runs
    ``postgres --version``; external mode connects): call it via
    ``asyncio.to_thread`` from a running loop."""
    checks: list[Check] = []
    if settings.managed_db:
        from meshforge.controller.db_managed.runtime import (
            cluster_running as lock_held,
        )

        root = settings.db_root
        running = (
            cluster_running() if cluster_running is not None else lock_held(settings)
        )
        checks += [
            _glibc(),
            _binaries(settings),
            _db_port(settings, running),
            _password_files(root),
            _data_major(root),
        ]
    elif include_external_db:
        checks.append(_external_db(settings))
    checks += [_disk(settings.data_dir), _ca(settings), _spa(dist)]
    return DoctorReport(tuple(checks))
