"""One result file per spike *run*: quick runs and re-runs never overwrite.

File names (inside ``lab/reports/raw/<day>-<topic>/``)::

    <host>[__<suffix>].json                  first full run
    <host>[__<suffix>]__run2.json            second full run of the same slot, ...
    <host>[__<suffix>]__quick.json           a --quick (smoke) run
    <host>[__<suffix>]__quick__run2.json     a second quick run, ...

A *slot* is the file stem without the ``__quick`` / ``__runN`` parts, i.e. one
laptop (and, for S1/S2/S3, one peer + link). :func:`write_run_envelope` never
replaces a file that already exists: it writes to the next free ``__runN``
name, so a full run cannot destroy an earlier full run or a quick run, and a
quick run cannot destroy anything. This is the run-file counterpart of the
0.2.13 report rule ("never destroy recorded work silently"). Unlike a report,
a measurement cannot be merged, and refusing to write would throw away a run
that may have taken an hour, so a new name is the non-destructive choice. To
discard a bad run, delete its file by hand.

Report builders call :func:`latest_runs` after loading a folder: per slot,
the newest **full** run is counted; a quick run is used only when the slot has
no full run at all (and is still marked quick by the spike's report, so it can
never pass as a measurement). Every other file is listed as superseded, never
silently dropped.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from spikes._harness.atomic import atomic_write_text
from spikes._harness.envelope import ResultCollisionError, dumps
from spikes._harness.reps import JSON

RUN_SUFFIX: Final = "__run"
#: Upper bound on ``__runN`` probing: a folder with this many runs of one slot
#: is a bug (a loop), not a lab session.
MAX_RUNS: Final = 999
_STEM_RE: Final = re.compile(
    r"^(?P<base>.*?)(?P<quick>__quick)?(?:__run(?P<run>[2-9]|[1-9][0-9]+))?$"
)


@dataclass(frozen=True)
class SlotName:
    """A result file name split into slot, quick flag and run number (1 = none)."""

    base: str
    quick: bool
    run: int


def parse_stem(stem: str) -> SlotName:
    match = _STEM_RE.match(stem)
    if match is None or not match.group("base"):  # pragma: no cover - regex total
        return SlotName(stem, False, 1)
    run = int(match.group("run")) if match.group("run") else 1
    return SlotName(match.group("base"), bool(match.group("quick")), run)


def run_path(path: Path, run: int) -> Path:
    """``path`` for run number ``run`` (``1`` is ``path`` itself)."""
    if run <= 1:
        return path
    return path.with_name(f"{path.stem}{RUN_SUFFIX}{run}{path.suffix}")


def _hostname_in(path: Path) -> str | None:
    try:
        old = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    host = old.get("host") if isinstance(old, dict) else None
    name = host.get("hostname") if isinstance(host, dict) else None
    return name if isinstance(name, str) else None


def _reserve(path: Path) -> bool:
    """Create ``path`` empty and exclusively; ``False`` if it already exists."""
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return False
    os.close(fd)
    return True


def write_run_envelope(doc: Mapping[str, JSON], path: Path) -> Path:
    """Write ``doc`` to ``path`` or, if that exists, the next free ``__runN``
    name. Returns the path actually written. Never replaces an existing file.

    ``path`` holding a result of a *different* host is still refused with
    :class:`ResultCollisionError` (two hosts mapped to one name: a naming bug,
    not a re-run). The name is reserved with ``O_EXCL`` first, so two runs
    racing for the same slot get different files; the content is then written
    atomically (temp file + ``os.replace``). On failure the reserved name is
    removed again. Raises ``OSError``.
    """
    new_host = doc.get("host")
    new_name = new_host.get("hostname") if isinstance(new_host, dict) else None
    if path.exists():
        old_host = _hostname_in(path)
        if old_host is not None and old_host != new_name:
            raise ResultCollisionError(
                f"{path} already holds results from host {old_host!r}; "
                f"refusing to write results from {new_name!r} next to it"
            )
    text = dumps(doc)
    path.parent.mkdir(parents=True, exist_ok=True)
    for run in range(1, MAX_RUNS + 1):
        target = run_path(path, run)
        if not _reserve(target):
            continue
        try:
            atomic_write_text(target, text)
        except BaseException:
            with contextlib.suppress(OSError):
                target.unlink()
            raise
        return target
    raise FileExistsError(
        f"{path}: {MAX_RUNS} runs of this slot already exist; refusing to add more"
    )


def is_quick(doc: Mapping[str, JSON]) -> bool:
    """A smoke run: ``extra.quick``/``params.quick`` is true or the file name
    carries ``__quick`` (older files only have the flag in the JSON)."""
    for key in ("extra", "params"):
        part = doc.get(key)
        if isinstance(part, dict) and part.get("quick") is True:
            return True
    stem = _stem(doc)
    return bool(stem) and parse_stem(stem).quick


def _stem(doc: Mapping[str, JSON]) -> str:
    return Path(str(doc.get("_source") or "")).stem


@dataclass
class RunSelection:
    """What a report counts (``kept``, in input order) and what it does not."""

    kept: list[dict[str, JSON]] = field(default_factory=list)
    #: ``"<file>: superseded by <file> (...)"`` per file that is not counted.
    superseded: list[str] = field(default_factory=list)


def _rank(doc: Mapping[str, JSON]) -> tuple[bool, str, int, str]:
    stem = _stem(doc)
    return (
        not is_quick(doc),  # any full run beats every quick run
        str(doc.get("timestamp_utc") or ""),  # then the newest
        parse_stem(stem).run,  # same second: the later run number
        stem,
    )


def latest_runs(docs: Sequence[dict[str, JSON]]) -> RunSelection:
    """Keep one envelope per (spike, slot): the newest full run, else the
    newest quick run. Needs ``_source`` (set by ``load_dir``); envelopes
    without it are each their own slot and always kept."""
    best: dict[tuple[str, str], dict[str, JSON]] = {}
    for index, doc in enumerate(docs):
        stem = _stem(doc)
        base = parse_stem(stem).base if stem else f"<unnamed {index}>"
        key = (str(doc.get("spike")), base)
        current = best.get(key)
        if current is None or _rank(doc) > _rank(current):
            best[key] = doc
    winners = {id(d) for d in best.values()}
    selection = RunSelection()
    for index, doc in enumerate(docs):
        if id(doc) in winners:
            selection.kept.append(doc)
            continue
        stem = _stem(doc)
        base = parse_stem(stem).base if stem else f"<unnamed {index}>"
        winner = best[(str(doc.get("spike")), base)]
        why = "quick run" if is_quick(doc) and not is_quick(winner) else "older run"
        selection.superseded.append(
            f"`{doc.get('_source')}`: superseded by `{winner.get('_source')}` "
            f"({why}; kept on disk, not counted)"
        )
    return selection


def superseded_block(lines: Sequence[str]) -> str:
    """Markdown list of :attr:`RunSelection.superseded` lines for a report's
    extra results (``""`` when nothing was superseded)."""
    if not lines:
        return ""
    return (
        "\nSuperseded result files (an older or quick run of the same laptop/peer; "
        "kept on disk, not counted):\n" + "".join(f"- {line}\n" for line in lines)
    )
