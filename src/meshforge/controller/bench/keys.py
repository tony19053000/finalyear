"""Benchmark job Idempotency-Keys (V0.9 G3-G5): the one place their shape lives.

* a run's own job: ``bench:<run_id>`` (generation 0), ``bench:<run_id>:<g>``
  after the campaign runner requeued it ``g`` times (G5) - one job per
  generation, so no generation is ever submitted twice;
* a ``queue:K`` filler: ``bench:<run_id>:filler:<i>`` (ASCII digits, D244);
* a ``cache:warm`` warm-up: ``bench:<run_id>:warmup`` (``:warmup:<g>`` for a
  requeued run).

Every key starts with ``bench:`` - the benchmark identity the queue's aging
exemption and the owner guard read (D237). Pure.
"""

from __future__ import annotations

from typing import Final

from meshforge.common.errors import InvalidIdError
from meshforge.common.ids import IdKind, parse_id

__all__ = [
    "FILLER_TAG",
    "KEY_PREFIX",
    "WARMUP_TAG",
    "filler_key",
    "pre_job_role",
    "run_id_of_key",
    "run_key",
    "run_of_key",
    "warmup_key",
]

KEY_PREFIX: Final = "bench:"
FILLER_TAG: Final = "filler"
WARMUP_TAG: Final = "warmup"


def run_key(run_id: str, generation: int = 0) -> str:
    """The run job's Idempotency-Key (V0.9 G5): ``bench:<run_id>`` for the
    first submission (G3/G4 keys unchanged), ``bench:<run_id>:<generation>``
    after the runner requeued the run - one job per generation, never two."""
    if generation <= 0:
        return f"{KEY_PREFIX}{run_id}"
    return f"{KEY_PREFIX}{run_id}:{generation}"


def filler_key(run_id: str, index: int) -> str:
    """Filler ``index`` (1-based). A requeued run (generation g, k fillers)
    numbers its fillers ``g*k+1 .. g*k+k``: new jobs, same key shape."""
    return f"{KEY_PREFIX}{run_id}:{FILLER_TAG}:{index}"


def warmup_key(run_id: str, generation: int = 0) -> str:
    if generation <= 0:
        return f"{KEY_PREFIX}{run_id}:{WARMUP_TAG}"
    return f"{KEY_PREFIX}{run_id}:{WARMUP_TAG}:{generation}"


def _digits(text: str) -> bool:
    return text.isascii() and text.isdecimal() and not text.startswith("0")


def run_of_key(key: str | None) -> tuple[str, int] | None:
    """``(run_id, generation)`` of a run job's own key (:func:`run_key`; None
    for a filler, a warm-up or any other job)."""
    if not key or not key.startswith(KEY_PREFIX):
        return None
    run_id, sep, rest = key.removeprefix(KEY_PREFIX).partition(":")
    generation = 0
    if sep:
        if not _digits(rest) or len(rest) > 9:
            return None
        generation = int(rest)
    try:
        parse_id(run_id, IdKind.BENCH_RUN)
    except InvalidIdError:
        return None
    return run_id, generation


def run_id_of_key(key: str | None) -> str | None:
    """The run id of a run job's own key (any generation)."""
    found = run_of_key(key)
    return None if found is None else found[0]


def pre_job_role(key: str | None) -> tuple[str, str] | None:
    """``(role, run_id)`` of a filler (``bench:<run_id>:filler:<i>``) or a
    warm-up (``bench:<run_id>:warmup``) key; None for any other key."""
    if not key or not key.startswith(KEY_PREFIX):
        return None
    parts = key.removeprefix(KEY_PREFIX).split(":")
    if (
        len(parts) == 3
        and parts[1] == FILLER_TAG
        and parts[2].isascii()
        and parts[2].isdecimal()
    ):
        role = FILLER_TAG
    elif (len(parts) == 2 and parts[1] == WARMUP_TAG) or (
        len(parts) == 3 and parts[1] == WARMUP_TAG and _digits(parts[2])
    ):
        role = WARMUP_TAG
    else:
        return None
    try:
        parse_id(parts[0], IdKind.BENCH_RUN)
    except InvalidIdError:
        return None
    return role, parts[0]
