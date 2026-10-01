"""The gaps sidecar of an attempt's live logs (V0.5 G3, D94).

The live log file holds only the bytes received; a gap (a dropped chunk, a
failed write) is *recorded*, never filled (D71). Before V0.5 that record lived
in memory only, so after a controller restart ``compute logs`` showed a file
with silently missing bytes. Now it is kept in ``<logs dir>/<attempt_id>.gaps``:

* written by the hub's writer task whenever a stream gains a gap (before the
  bytes after the gap are appended), and once more when the attempt ends
  (``ended: true``) - or removed then, when no un-replaced stream has a gap;
* JSON, canonical (sorted keys, no spaces), replaced atomically (a temp file in
  the same folder, then ``os.replace``) - a crash leaves the old or the new
  record, never half of one. Not fsynced: losing the newest record to a power
  cut only loses what V0.4 lost on every restart;
* ``lost`` is the total width of every gap (also those past ``MAX_GAPS_KEPT``),
  so a hub that re-opens the attempt continues at the right stream offset
  (``received_to = file size + lost``) instead of recording a phantom gap;
* read back with a size bound, ``O_NOFOLLOW`` and full validation - a broken
  sidecar is ignored (the logs are still served), never trusted.

No database column: the record belongs to the files it describes and is
collected with them (:meth:`~.logs.LogHub.collect_garbage`).
"""

from __future__ import annotations

import contextlib
import errno
import json
import os
import secrets
import stat
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

__all__ = [
    "GAPS_SUFFIX",
    "MAX_SIDECAR_BYTES",
    "GapRecord",
    "StreamGaps",
    "decode",
    "encode",
    "read_sidecar",
    "write_sidecar",
]

GAPS_SUFFIX: Final = "gaps"
MAX_SIDECAR_BYTES: Final = 256 * 1024
"""Two streams x 1024 kept gaps of two 64-bit offsets fit in well under this."""
_VERSION: Final = 1
_MAX_ID: Final = 64
_MAX_OFFSET: Final = 1 << 62


@dataclass(frozen=True, slots=True)
class StreamGaps:
    gaps: tuple[tuple[int, int], ...]
    gaps_total: int
    lost: int
    final: bool = False


@dataclass(frozen=True, slots=True)
class GapRecord:
    job_id: str
    node_id: str
    fencing_token: int
    ended: bool
    streams: Mapping[str, StreamGaps]


def encode(record: GapRecord) -> bytes:
    body = {
        "v": _VERSION,
        "job_id": record.job_id,
        "node_id": record.node_id,
        "fencing_token": record.fencing_token,
        "ended": record.ended,
        "streams": {
            name: {
                "gaps": [list(g) for g in s.gaps],
                "gaps_total": s.gaps_total,
                "lost": s.lost,
                "final": s.final,
            }
            for name, s in sorted(record.streams.items())
        },
    }
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode()


def _int(value: Any, where: str, high: int = _MAX_OFFSET) -> int:
    if type(value) is not int or not 0 <= value <= high:
        raise ValueError(f"{where} is not an integer in [0, {high}]")
    return value


def _bool(value: Any, where: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{where} is not a boolean")
    return value


def _id(value: Any, where: str) -> str:
    if not isinstance(value, str) or not 0 < len(value) <= _MAX_ID:
        raise ValueError(f"{where} is not a short string")
    return value


def _stream(raw: Any, name: str, max_gaps: int) -> StreamGaps:
    if not isinstance(raw, dict):
        raise ValueError(f"streams.{name} is not an object")  # noqa: TRY004
    gaps_raw = raw.get("gaps")
    if not isinstance(gaps_raw, list) or len(gaps_raw) > max_gaps:
        raise ValueError(f"streams.{name}.gaps is not a list of <= {max_gaps}")
    gaps: list[tuple[int, int]] = []
    previous = 0
    for index, gap in enumerate(gaps_raw):
        where = f"streams.{name}.gaps[{index}]"
        if not isinstance(gap, list) or len(gap) != 2:
            raise ValueError(f"{where} is not a [from, to] pair")
        start, end = _int(gap[0], where), _int(gap[1], where)
        if not previous <= start < end:
            raise ValueError(f"{where} is empty or out of order")
        gaps.append((start, end))
        previous = end
    total = _int(raw.get("gaps_total"), f"streams.{name}.gaps_total")
    lost = _int(raw.get("lost"), f"streams.{name}.lost")
    if total < len(gaps) or lost < sum(b - a for a, b in gaps):
        raise ValueError(f"streams.{name} totals are below its kept gaps")
    return StreamGaps(
        gaps=tuple(gaps),
        gaps_total=total,
        lost=lost,
        final=_bool(raw.get("final", False), f"streams.{name}.final"),
    )


def decode(data: bytes, *, streams: Iterable[str], max_gaps: int) -> GapRecord:
    """The validated record; :class:`ValueError` for anything else."""
    if len(data) > MAX_SIDECAR_BYTES:
        raise ValueError("the gaps record is too large")
    try:
        body = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("the gaps record is not JSON") from exc
    if not isinstance(body, dict) or body.get("v") != _VERSION:
        raise ValueError("the gaps record has an unknown version")
    raw_streams = body.get("streams")
    allowed = set(streams)
    if not isinstance(raw_streams, dict) or not set(raw_streams) <= allowed:
        raise ValueError("the gaps record names an unknown stream")
    return GapRecord(
        job_id=_id(body.get("job_id"), "job_id"),
        node_id=_id(body.get("node_id"), "node_id"),
        fencing_token=_int(body.get("fencing_token"), "fencing_token", 2**63 - 1),
        ended=_bool(body.get("ended"), "ended"),
        streams={
            name: _stream(raw, name, max_gaps) for name, raw in raw_streams.items()
        },
    )


def write_sidecar(path: Path, data: bytes) -> None:
    """Replace ``path`` with ``data`` atomically (raises :class:`OSError`)."""
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(
        tmp,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()


def read_sidecar(path: Path) -> bytes | None:
    """The sidecar's bytes; ``None`` when there is none. Raises
    :class:`OSError` / :class:`ValueError` for one that cannot be read safely."""
    # O_NONBLOCK: opening a FIFO planted at the path must not block the event
    # loop; anything but a regular file is refused before a byte is read.
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as fh:
        if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
            raise OSError(errno.EINVAL, "not a regular file")
        data = fh.read(MAX_SIDECAR_BYTES + 1)
    if len(data) > MAX_SIDECAR_BYTES:
        raise ValueError("the gaps record is too large")
    return data
