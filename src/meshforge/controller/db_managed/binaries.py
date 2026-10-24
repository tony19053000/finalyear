"""The bundled PostgreSQL build: the pinned source and the installed programs.

**Pinned source (spike SP2, see ``lab/spikes/SP2.md``).** Relocatable PostgreSQL
builds from ``theseus-rs/postgresql-binaries`` (PostgreSQL licence): full client and
server programs (``pg_dump``/``pg_restore`` included, which zonky's embedded
binaries lack), ``$ORIGIN`` RUNPATHs, OpenSSL 3 and ICU bundled. The archive is
fetched **at build time** by the packaging tooling, verified against ``sha256``
and trimmed; the controller itself never downloads anything. The upstream
release assets have been rebuilt in place before (same tag, new upload), so the
sha256 pin - not the URL - is the identity, and G2 must mirror the archive.

**Installed programs.** :class:`PgBinaries` is the ``bin`` dir of the (trimmed)
bundle: it checks every program the controller uses exists and that
``postgres --version`` reports the expected major (a wrong glibc or a damaged
bundle fails here, typed, instead of in the middle of ``initdb``).
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from meshforge.controller.db_managed.errors import ManagedDbError, ManagedDbReason
from meshforge.controller.db_managed.pidfile import parse_server_version

__all__ = [
    "BUNDLED_MAJOR",
    "LINUX_X86_64",
    "REQUIRED_PROGRAMS",
    "WINDOWS_X86_64",
    "PgBinaries",
    "PgBuild",
]

BUNDLED_MAJOR: Final = "16"
"""The PostgreSQL major the controller bundles (the dev Compose image is 16.15 too)."""

REQUIRED_PROGRAMS: Final = ("postgres", "initdb", "pg_dump", "pg_restore")
"""Programs the managed mode runs. ``pg_ctl``, ``pg_controldata``, ``pg_resetwal``
and ``pg_upgrade`` are kept in the trimmed bundle for a later major upgrade but are
not required to run."""

_VERSION_TIMEOUT_S: Final = 15.0


@dataclass(frozen=True, slots=True)
class PgBuild:
    """One pinned upstream archive."""

    version: str
    """PostgreSQL version (``16.15``)."""
    target: str
    """Rust-style target triple of the build."""
    url: str
    sha256: str
    """sha256 of the archive file as downloaded (the pin)."""
    archive_root: str
    """Top-level directory inside the archive."""
    min_glibc: str | None
    """Lowest glibc the build runs on (Linux GNU only); None elsewhere."""


_THESEUS: Final = (
    "https://github.com/theseus-rs/postgresql-binaries/releases/download/16.15.1/"
)

LINUX_X86_64: Final = PgBuild(
    version="16.15",
    target="x86_64-unknown-linux-gnu",
    url=_THESEUS + "postgresql-16.15.1-x86_64-unknown-linux-gnu.tar.gz",
    sha256="0c42e7c05f0cb596b82ac07990c96fc261ec2e8106055d557c76442d712dbf74",
    archive_root="postgresql-16.15.1-x86_64-unknown-linux-gnu",
    min_glibc="2.36",
)

WINDOWS_X86_64: Final = PgBuild(
    version="16.15",
    target="x86_64-pc-windows-msvc",
    url=_THESEUS + "postgresql-16.15.1-x86_64-pc-windows-msvc.zip",
    sha256="1b56bcc86b33f6fd741f22ed670b9ed3d7a4feb25b45ae4ec012c442e15a7cb5",
    archive_root="postgresql-16.15.1-x86_64-pc-windows-msvc",
    min_glibc=None,
)
"""Recorded for later (Windows controller is out of scope for V0.17, D390)."""


@dataclass(frozen=True, slots=True)
class PgBinaries:
    """The ``bin`` directory of an installed PostgreSQL bundle."""

    bin_dir: Path

    def program(self, name: str) -> Path:
        """Absolute path of ``name`` (``.exe`` added on Windows)."""
        suffix = ".exe" if os.name == "nt" else ""
        return self.bin_dir / f"{name}{suffix}"

    def check(self, *, expected_major: str = BUNDLED_MAJOR) -> str:
        """Verify the bundle; return the full server version (``"16.15"``).

        Raises :class:`ManagedDbError` (``binaries_missing`` / ``binaries_unusable``).
        Blocking (runs ``postgres --version``); call it via ``asyncio.to_thread``.
        """
        missing = [
            name
            for name in REQUIRED_PROGRAMS
            if not os.access(self.program(name), os.X_OK)
        ]
        if missing:
            raise ManagedDbError(
                f"the bundled PostgreSQL in {self.bin_dir} is incomplete: "
                f"{', '.join(missing)} missing or not executable; reinstall MeshForge",
                reason=ManagedDbReason.BINARIES_MISSING,
                details={"bin_dir": str(self.bin_dir), "missing": missing},
            )
        try:
            done = subprocess.run(  # noqa: S603 - fixed argv, our own bundle
                [str(self.program("postgres")), "--version"],
                capture_output=True,
                text=True,
                timeout=_VERSION_TIMEOUT_S,
                check=False,
                env={"LC_ALL": "C", "PATH": os.defpath},
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise _unusable(self.bin_dir, f"cannot run postgres: {exc}") from exc
        if done.returncode != 0:
            # e.g. "version `GLIBC_2.36' not found" on an older distribution.
            raise _unusable(
                self.bin_dir,
                f"postgres --version exited {done.returncode}: "
                f"{done.stderr.strip()[-300:]}",
            )
        try:
            major, full = parse_server_version(done.stdout)
        except ValueError as exc:
            raise _unusable(self.bin_dir, str(exc)) from exc
        if major != expected_major:
            raise _unusable(
                self.bin_dir,
                f"bundle is PostgreSQL {full}, expected major {expected_major}",
            )
        return full


def _unusable(bin_dir: Path, why: str) -> ManagedDbError:
    return ManagedDbError(
        f"the bundled PostgreSQL in {bin_dir} cannot run: {why}",
        reason=ManagedDbReason.BINARIES_UNUSABLE,
        details={"bin_dir": str(bin_dir), "why": why},
    )
