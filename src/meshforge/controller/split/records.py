"""Chunk records: the telemetry of every chunk try (V0.10 G8, D271; schema v3).

Written by :class:`~.store.ChunkStore` **in the transaction that applies the
chunk's result** (checklist 35: commit, its record and the lease release
together) - so a record exists exactly for what the queue applied, and a
rolled-back result leaves none:

* a **committed** result -> the winner's row (its agent measurements, its
  verified output bytes) **plus** one row for every other lease of that chunk
  that has none yet: the speculative copy it beat (``superseded``), tries that
  expired or were released earlier (``expired`` / ``released``) - so both
  copies of a speculated chunk are rows (``speculative``, ``won``);
* an ``already_done`` result -> ``lost`` (it ran to the end; another result had
  won). If its lease already has a ``superseded`` row (written by the winner's
  commit, before this late report), that row is **completed** with the
  measurements the agent sent - the one update a record ever gets, and only of
  fields that were null (``run_ms IS NULL``). A late report for a lease already
  recorded as ``expired`` / ``released`` is dropped on purpose: that try's
  lease had ended, so its numbers are not trusted;
* a **failed** try -> ``failed`` with its failure type and stderr tail;
* a fenced message -> nothing (it was not applied); a ``released`` result ->
  nothing now (the lease's row is written when the chunk commits).

Leases of a chunk that never commits (the job failed or was cancelled) have
rows only for their failed tries. ``stderr_tail`` is decoded (UTF-8, invalid
bytes replaced, NUL dropped) and cut to its last ``[controller.split]
chunk_stderr_tail_bytes`` (failed tries) / ``chunk_stderr_ok_bytes`` (done
tries) at a character boundary - it was already redacted of the job's secret
values on the agent (D261), and it is never exported in the dataset.

Durations come from the agent's monotonic clock (D213): ``stage_ms`` /
``env_ms`` / ``run_ms`` / ``upload_ms``; ``leased_at`` / ``ended_at`` are the
controller's wall clock, for ordering only.
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from meshforge.common.errors import FailureType
from meshforge.common.split_chunks import chunk_span
from meshforge.controller.store.models.telemetry import STDERR_TAIL_CHARS, ChunkRecord
from meshforge.controller.telemetry.record import WORKLOAD_FAILURES
from meshforge.controller.telemetry.schema import SCHEMA_VERSION, FailureClass
from meshforge.workqueue.core import LeaseState
from meshforge.workqueue.tables import work_item, work_lease

__all__ = [
    "NO_RESULT_OUTCOMES",
    "ChunkReport",
    "StderrLimits",
    "chunk_failure_class",
    "clean_tail",
    "record_commit",
    "record_try",
]

NO_RESULT_OUTCOMES: Final[dict[str, str]] = {
    LeaseState.SUPERSEDED.value: "superseded",
    LeaseState.RELEASED.value: "released",
    LeaseState.EXPIRED.value: "expired",
    LeaseState.FAILED.value: "failed",
}
"""Lease state -> outcome of a lease whose own result was never applied."""
_MAX_MS: Final = 2**62


@dataclass(frozen=True, slots=True)
class StderrLimits:
    """``[controller.split] chunk_stderr_tail_bytes`` / ``chunk_stderr_ok_bytes``."""

    failed: int = 16_384
    ok: int = 2_048


@dataclass(frozen=True, slots=True)
class ChunkReport:
    """What a try's ``ChunkResult`` carried (already bounded by
    :func:`~.messages.parse_chunk_result`)."""

    phases: Mapping[str, float] = field(default_factory=dict)
    exit_code: int | None = None
    input_bytes: int | None = None
    stderr_tail: bytes = b""
    output_bytes: int | None = None
    """Bytes the lane's grant stored for the result (done tries)."""
    digest: str | None = None


def clean_tail(data: bytes, limit: int) -> str | None:
    """``data``'s last ``limit`` bytes as text (module docstring); None when
    there is nothing to keep."""
    if limit <= 0 or not data:
        return None
    cut = data[-min(limit, STDERR_TAIL_CHARS) :]
    text = cut.decode("utf-8", errors="replace").replace("\x00", "")
    if len(cut) < len(data) and text.startswith("�"):
        text = text.lstrip("�")  # a character cut in half by the limit
    text = text[-STDERR_TAIL_CHARS:]
    return text or None


def chunk_failure_class(outcome: str, failure_type: str | None) -> str:
    """The run record's classes, for a chunk try."""
    if outcome in ("committed", "lost"):
        return FailureClass.OK.value
    if outcome in ("superseded", "released"):
        return FailureClass.UNAVAILABLE.value
    if outcome == "expired":
        return FailureClass.INFRASTRUCTURE.value
    try:
        typed = FailureType(failure_type) if failure_type else None
    except ValueError:
        typed = None
    if typed in WORKLOAD_FAILURES:
        return FailureClass.WORKLOAD.value
    if typed in (FailureType.CANCELLED, FailureType.OWNER_PREEMPTED):
        return FailureClass.UNAVAILABLE.value
    return FailureClass.INFRASTRUCTURE.value


def _ms(seconds: object) -> int | None:
    if isinstance(seconds, bool) or not isinstance(seconds, int | float):
        return None
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return min(round(float(seconds) * 1000), _MAX_MS)


def _measured(report: ChunkReport | None, *, limit: int) -> dict[str, Any]:
    if report is None:
        return {
            "exit_code": None,
            "result_digest": None,
            "bytes_in": None,
            "bytes_out": None,
            "stage_ms": None,
            "env_ms": None,
            "run_ms": None,
            "upload_ms": None,
            "stderr_tail": None,
        }
    phases = report.phases
    return {
        "exit_code": report.exit_code,
        "result_digest": report.digest,
        "bytes_in": report.input_bytes,
        "bytes_out": report.output_bytes,
        "stage_ms": _ms(phases.get("stage_s")),
        "env_ms": _ms(phases.get("env_s")),
        "run_ms": _ms(phases.get("run_s")),
        "upload_ms": _ms(phases.get("upload_s")),
        "stderr_tail": clean_tail(report.stderr_tail, limit),
    }


async def _leases(db: AsyncSession, item_id: int) -> Sequence[Any]:
    return (
        await db.execute(
            select(
                work_lease.c.seq,
                work_lease.c.holder,
                work_lease.c.site,
                work_lease.c.state,
                work_lease.c.speculative,
                work_lease.c.leased_at,
                work_lease.c.ended_at,
                work_item.c.idx,
                work_item.c.payload,
                work_item.c.done_at,
            )
            .join(work_item, work_item.c.id == work_lease.c.item_id)
            .where(work_lease.c.item_id == item_id)
            .order_by(work_lease.c.seq)
        )
    ).all()


def _row(
    lease: Any,
    *,
    job_id: str,
    item_count: int,
    outcome: str,
    won: bool,
    failure_type: str | None,
    measured: dict[str, Any],
    ended_at: dt.datetime | None,
) -> dict[str, Any]:
    span = chunk_span(lease.payload, item_count)
    return {
        "schema_version": SCHEMA_VERSION,
        "job_id": job_id,
        "chunk_index": int(lease.idx),
        "lease_seq": int(lease.seq),
        "lane_attempt_id": str(lease.holder),
        "node_id": str(lease.site),
        "first_item": span.first,
        "items": span.count,
        "speculative": bool(lease.speculative),
        "won": won,
        "outcome": outcome,
        "failure_type": failure_type,
        "failure_class": chunk_failure_class(outcome, failure_type),
        **measured,
        "leased_at": lease.leased_at,
        "ended_at": ended_at,
    }


async def record_try(
    db: AsyncSession,
    *,
    job_id: str,
    item_count: int,
    item_id: int,
    seq: int,
    outcome: str,
    report: ChunkReport,
    failure_type: str | None,
    limits: StderrLimits,
    now: dt.datetime,
) -> None:
    """The row of one applied try that did not win: ``lost`` (it finished
    after another result won) or ``failed``. A ``superseded`` row of the same
    lease (written by the winner's commit) is completed with the
    measurements, never overwritten otherwise."""
    lease = next((x for x in await _leases(db, item_id) if int(x.seq) == seq), None)
    if lease is None:  # pragma: no cover - the queue just applied this lease
        return
    limit = limits.failed if outcome == "failed" else limits.ok
    row = _row(
        lease,
        job_id=job_id,
        item_count=item_count,
        outcome=outcome,
        won=False,
        failure_type=failure_type,
        measured=_measured(report, limit=limit),
        ended_at=lease.ended_at or now,
    )
    insert = pg_insert(ChunkRecord).values(row)
    filled = {
        name: insert.excluded[name]
        for name in (
            "outcome",
            "exit_code",
            "result_digest",
            "bytes_in",
            "bytes_out",
            "stage_ms",
            "env_ms",
            "run_ms",
            "upload_ms",
            "stderr_tail",
            "failure_class",
        )
    }
    await db.execute(
        insert.on_conflict_do_update(
            index_elements=["job_id", "chunk_index", "lease_seq"],
            set_=filled,
            where=(ChunkRecord.outcome == "superseded") & ChunkRecord.run_ms.is_(None),
        )
    )


async def record_commit(
    db: AsyncSession,
    *,
    job_id: str,
    item_count: int,
    item_id: int,
    seq: int,
    report: ChunkReport,
    limits: StderrLimits,
    now: dt.datetime,
) -> int:
    """The committed chunk's rows: the winner (``seq``) and every other lease
    of the chunk without a row yet (module docstring). The number written."""
    rows: list[dict[str, Any]] = []
    for lease in await _leases(db, item_id):
        if int(lease.seq) == seq:
            rows.append(
                _row(
                    lease,
                    job_id=job_id,
                    item_count=item_count,
                    outcome="committed",
                    won=True,
                    failure_type=None,
                    measured=_measured(report, limit=limits.ok),
                    ended_at=lease.done_at or now,
                )
            )
            continue
        outcome = NO_RESULT_OUTCOMES.get(str(lease.state))
        if outcome is None:  # still live: not possible after a commit
            continue
        rows.append(
            _row(
                lease,
                job_id=job_id,
                item_count=item_count,
                outcome=outcome,
                won=False,
                failure_type=None,
                measured=_measured(None, limit=0),
                ended_at=lease.ended_at,
            )
        )
    if not rows:  # pragma: no cover - the winner's lease exists
        return 0
    done = await db.scalars(
        pg_insert(ChunkRecord)
        .values(rows)
        .on_conflict_do_nothing(index_elements=["job_id", "chunk_index", "lease_seq"])
        .returning(ChunkRecord.lease_seq)
    )
    return len(list(done))
