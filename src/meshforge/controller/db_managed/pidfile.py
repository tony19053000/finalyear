"""``postmaster.pid`` parsing (pure) and the cluster's major version (``PG_VERSION``).

PostgreSQL >= 10 writes ``postmaster.pid`` in the data dir with one value per line
(``src/include/utils/pidfile.h``)::

    1 postmaster pid          5 first unix socket directory
    2 data directory          6 first listen address ("" or "*" or an address)
    3 start time (epoch s)    7 shared memory key and id ("  5432001   12")
    4 port                    8 status: "starting", "stopping", "ready   ", "standby "

Line 8 is how readiness is detected **without a client connection** (the postmaster
rewrites it to ``ready`` once it accepts connections). Lines 5-8 may be missing
while the postmaster is still writing the file; :func:`parse_postmaster_pid`
tolerates that and :class:`PostmasterPid` says ``status=None``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final

__all__ = [
    "PG_VERSION_FILE",
    "PIDFILE_NAME",
    "PidFileError",
    "PostmasterPid",
    "PostmasterStatus",
    "check_cluster_major",
    "parse_postmaster_pid",
    "parse_server_version",
    "read_cluster_major",
    "read_postmaster_pid",
]

PIDFILE_NAME: Final = "postmaster.pid"
PG_VERSION_FILE: Final = "PG_VERSION"
_MAX_PIDFILE_BYTES: Final = 4096


class PidFileError(ValueError):
    """``postmaster.pid`` exists but is not a valid lock file (empty, truncated,
    garbage). Never contains a secret: the file holds none."""


class PostmasterStatus(StrEnum):
    """Line 8 of ``postmaster.pid`` (trailing spaces stripped)."""

    STARTING = "starting"
    STOPPING = "stopping"
    READY = "ready"
    STANDBY = "standby"


@dataclass(frozen=True, slots=True)
class PostmasterPid:
    """The parsed lock file."""

    pid: int
    data_dir: str
    start_time: int | None
    port: int | None
    socket_dir: str | None
    listen_address: str | None
    shmem_key: str | None
    status: PostmasterStatus | None
    """None when the line is missing (file still being written) or unknown."""


def parse_postmaster_pid(text: str) -> PostmasterPid:
    """Parse the content of ``postmaster.pid``; :class:`PidFileError` if invalid.

    Only line 1 (a positive pid) and line 2 (the data dir) are mandatory - the
    postmaster writes them first.
    """
    lines = text.split("\n")
    if len(lines) < 2 or not lines[0].strip():
        raise PidFileError("postmaster.pid has no pid line")
    try:
        pid = int(lines[0].strip())
    except ValueError:
        raise PidFileError("postmaster.pid line 1 is not a number") from None
    # A negative pid marks a standalone backend (postgres --single); 0/negative
    # can never be signalled safely.
    if pid <= 0:
        raise PidFileError(f"postmaster.pid holds a non-positive pid ({pid})")
    data_dir = lines[1]
    if not data_dir:
        raise PidFileError("postmaster.pid has no data directory line")

    def field(index: int) -> str | None:
        return lines[index] if len(lines) > index else None

    def int_field(index: int) -> int | None:
        raw = field(index)
        if raw is None or not raw.strip():
            return None
        try:
            return int(raw.strip())
        except ValueError:
            return None

    raw_status = field(7)
    status: PostmasterStatus | None = None
    if raw_status is not None:
        try:
            status = PostmasterStatus(raw_status.strip())
        except ValueError:
            status = None
    return PostmasterPid(
        pid=pid,
        data_dir=data_dir,
        start_time=int_field(2),
        port=int_field(3),
        socket_dir=field(4) or None,
        listen_address=field(5) or None,
        shmem_key=(field(6) or "").strip() or None,
        status=status,
    )


def read_postmaster_pid(data_dir: Path) -> PostmasterPid | None:
    """The lock file of ``data_dir``; None when there is none.

    Raises :class:`PidFileError` when it exists but cannot be parsed, ``OSError``
    when it cannot be read.
    """
    path = data_dir / PIDFILE_NAME
    try:
        with path.open("rb") as fh:
            raw = fh.read(_MAX_PIDFILE_BYTES + 1)
    except FileNotFoundError:
        return None
    if len(raw) > _MAX_PIDFILE_BYTES:
        raise PidFileError("postmaster.pid is implausibly large")
    return parse_postmaster_pid(raw.decode("utf-8", errors="replace"))


def parse_server_version(output: str) -> tuple[str, str]:
    """``(major, full)`` from ``postgres --version`` output.

    ``"postgres (PostgreSQL) 16.15"`` -> ``("16", "16.15")``. Raises
    ``ValueError`` on anything else (e.g. a loader error printed instead).
    """
    text = output.strip()
    prefix = "postgres (PostgreSQL) "
    if not text.startswith(prefix):
        raise ValueError("not a postgres --version line")
    full = text[len(prefix) :].split()[0]
    match = re.match(r"\d+", full)
    major = match.group(0) if match else ""
    if not major or int(major) < 10:
        # >= 10 has a one-number major; older (9.6) is never bundled.
        raise ValueError(f"unexpected PostgreSQL version {full!r}")
    return major, full


def read_cluster_major(data_dir: Path) -> str | None:
    """Content of ``PG_VERSION`` (e.g. ``"16"``), or None when the file is missing."""
    try:
        raw = (data_dir / PG_VERSION_FILE).read_text(encoding="ascii", errors="replace")
    except FileNotFoundError:
        return None
    return raw.strip()


def check_cluster_major(data_major: str, binary_major: str) -> bool:
    """True when a cluster of ``data_major`` can be started by ``binary_major``.

    PostgreSQL's on-disk format is compatible within one major version only.
    """
    return data_major.strip() == binary_major.strip()
