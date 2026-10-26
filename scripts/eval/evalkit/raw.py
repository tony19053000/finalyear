"""The raw data folder of one campaign (D419) - what ``analyze.py`` reads.

::

    <raw>/campaign.json   written once: format, the parsed spec, its digest,
                          the schedule digest, the environment at the start
    <raw>/runs.jsonl      one line per finished run (append-only, fsync'd)
    <raw>/sessions.jsonl  one line per runner start: build (git SHA, package
                          version), controller version, inventory, profile
                          switches - the evidence of *what* ran
    <raw>/profiles/*.toml the controller profiles (for the operator's hook)

**Resume.** A runner started on an existing folder checks that the campaign
file's digest equals the stored one (else it refuses: a different campaign
must use a different folder), reads ``runs.jsonl`` and skips every run
already there. A line cut short by a crash (no newline) is ignored and
reported; the run is simply done again (its jobs are found again by their
idempotency keys - no duplicate job is submitted). Before it appends, the
runner cuts that unfinished tail off (:func:`cut_partial_tail`; the bytes go
to ``<file>.cut`` for the record) - otherwise the next line would be glued to
it and the file would no longer parse.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from evalkit.canonical import dumps, write_text

__all__ = [
    "CAMPAIGN_FILE",
    "FORMAT",
    "RUNS_FILE",
    "SESSIONS_FILE",
    "RawData",
    "RawError",
    "append_line",
    "cut_partial_tail",
    "read_raw",
    "write_campaign",
]

FORMAT: Final = "meshforge-eval-raw/1"
CAMPAIGN_FILE: Final = "campaign.json"
RUNS_FILE: Final = "runs.jsonl"
SESSIONS_FILE: Final = "sessions.jsonl"


class RawError(ValueError):
    """The raw folder cannot be used (wrong format, another campaign)."""


@dataclass(frozen=True, slots=True)
class RawData:
    campaign: Mapping[str, Any]
    runs: tuple[Mapping[str, Any], ...]
    sessions: tuple[Mapping[str, Any], ...]
    partial_lines: int = 0
    """Lines cut short by a crash (ignored)."""
    duplicates: tuple[str, ...] = field(default=())
    """Run ids found more than once (the first is kept)."""


def append_line(path: Path, record: Mapping[str, Any]) -> None:
    """Append one compact JSON line and fsync it (one ``write`` call)."""
    line = json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False)
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(line + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def cut_partial_tail(path: Path) -> int:
    """Remove an unfinished last line (no newline) from ``path``, appending
    its bytes to ``<path>.cut``; the number of bytes cut (0 = none)."""
    if not path.exists():
        return 0
    data = path.read_bytes()
    if not data or data.endswith(b"\n"):
        return 0
    keep = data.rfind(b"\n") + 1
    with path.with_name(path.name + ".cut").open("ab") as fh:
        fh.write(data[keep:] + b"\n")
        fh.flush()
        os.fsync(fh.fileno())
    with path.open("r+b") as fh:
        fh.truncate(keep)
        fh.flush()
        os.fsync(fh.fileno())
    return len(data) - keep


def _lines(path: Path) -> tuple[list[dict[str, Any]], int]:
    if not path.exists():
        return [], 0
    text = path.read_text("utf-8")
    out: list[dict[str, Any]] = []
    partial = 0
    pieces = text.split("\n")
    for index, line in enumerate(pieces):
        if not line.strip():
            continue
        last = index == len(pieces) - 1  # no newline after it: cut short
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            if last:
                partial += 1
                continue
            raise RawError(f"{path.name} line {index + 1} is not JSON") from None
        if last:
            partial += 1
            continue
        if not isinstance(value, dict):
            raise RawError(f"{path.name} line {index + 1} is not an object")
        out.append(value)
    return out, partial


def write_campaign(folder: Path, campaign: Mapping[str, Any]) -> None:
    write_text(folder / CAMPAIGN_FILE, dumps(dict(campaign)))


def read_raw(folder: Path) -> RawData:
    """Everything in the folder (module docstring)."""
    path = folder / CAMPAIGN_FILE
    if not path.exists():
        raise RawError(f"{folder} has no {CAMPAIGN_FILE}: not a raw campaign folder")
    campaign = json.loads(path.read_text("utf-8"))
    if not isinstance(campaign, dict) or campaign.get("format") != FORMAT:
        raise RawError(f"{path} is not a {FORMAT} file")
    runs, partial = _lines(folder / RUNS_FILE)
    sessions, partial_s = _lines(folder / SESSIONS_FILE)
    seen: dict[str, Mapping[str, Any]] = {}
    duplicates: list[str] = []
    for run in runs:
        run_id = str(run.get("run_id"))
        if run_id in seen:
            duplicates.append(run_id)
            continue
        seen[run_id] = run
    return RawData(
        campaign=campaign,
        runs=tuple(seen.values()),
        sessions=tuple(sessions),
        partial_lines=partial + partial_s,
        duplicates=tuple(sorted(duplicates)),
    )
