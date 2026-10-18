"""``/v1/bench/campaigns`` - benchmark campaigns (V0.9 G3, roadmap 0.9.3/0.9.4, D215/D216).

**Admin only** (403 for a user): creating a campaign is job submission at
scale - hundreds of jobs on every owner's laptop - so it needs the fabric's
highest role (THREAT_MODEL). It runs only registered workload families
(``meshforge.bench.workloads``): a spec names families and parameters, never
code, and every parameter point passes the family's own model and the
5 s - 3 min window before anything is stored.

* ``POST /v1/bench/campaigns`` ``{"spec": "<YAML>"}`` - validate the spec and
  expand it **once** against the fabric's enrolled nodes (revoked ones
  excluded): ``201`` with the campaign and its run count. A spec error is
  ``400 invalid_input`` with ``error.problems`` (every problem, ``where: why``);
  a budget overflow names the run count / hours - nothing is stored.
* ``GET /v1/bench/campaigns`` - newest first, with per-state run counts;
  ``GET /v1/bench/campaigns/{id}`` adds the spec.
* ``GET /v1/bench/campaigns/{id}/runs?state=&after=&limit=`` - runs in
  ``seq`` order (keyset: ``after`` = the last ``seq`` seen).
* ``POST /v1/bench/campaigns/{id}/submit?count=k`` - submit the next ``k``
  planned runs in ``seq`` order (:mod:`meshforge.controller.bench.submit`):
  each is ``submitted`` / ``deferred`` (node offline now: stays planned) /
  ``skipped`` (can never run as planned) / ``already``. Idempotent per run.
  The **only** driver in G3 (D232); G5 adds the resumable runner.

Every read first moves ``submitted`` runs whose job ended to ``done`` /
``failed`` (:meth:`~meshforge.controller.bench.store.BenchStore.sync`).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Generator, Mapping
from dataclasses import dataclass
from typing import Annotated, Any, Final, Literal

from fastapi import APIRouter, Depends, Path, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import Field

from meshforge.bench.campaign import (
    MAX_SPEC_BYTES,
    CampaignSpecError,
    NodeRef,
    expand,
    load_spec,
    spec_digest,
)
from meshforge.common.bench_conditions import advertised_kinds
from meshforge.common.errors import FailureType, InvalidIdError
from meshforge.common.ids import IdKind, new_campaign_id, parse_id
from meshforge.common.logging import get_logger
from meshforge.controller.api.auth import AdminUser
from meshforge.controller.api.bench_label_models import LabelsOut, labels_out
from meshforge.controller.api.errors import ApiError
from meshforge.controller.api.models import ApiModel, Utc
from meshforge.controller.api.telemetry_routes import (
    CHUNK_BYTES,
    PARQUET_MEDIA_TYPE,
    TelemetryExport,
    get_export,
)
from meshforge.controller.bench.store import (
    MAX_LIST,
    BenchStore,
    BenchStoreError,
    CampaignRecord,
    RunRow,
)
from meshforge.controller.bench.submit import BenchSubmitError, BenchSubmitter
from meshforge.controller.jobs.queue_reads import MAX_NODES
from meshforge.controller.liveness import NodeState
from meshforge.controller.registry import Registry

__all__ = [
    "BENCH_STATE_KEY",
    "MAX_SUBMIT",
    "BenchApi",
    "bench_router",
]

log = get_logger(__name__)

BENCH_STATE_KEY: Final = "meshforge_bench"
MAX_SUBMIT: Final = 500
"""Runs one ``submit`` call may create (bounds one request's work)."""
_ID_MAX: Final = 40

bench_router = APIRouter(prefix="/v1/bench", tags=["bench"])

RunStateName = Literal["planned", "submitted", "done", "failed", "skipped"]


@dataclass(frozen=True, slots=True)
class BenchApi:
    store: BenchStore
    submitter: BenchSubmitter
    registry: Registry


def get_bench(request: Request) -> BenchApi:
    found = getattr(request.app.state, BENCH_STATE_KEY, None)
    if not isinstance(found, BenchApi):
        raise ApiError(
            FailureType.CONFIG_ERROR, "benchmark campaigns are not configured"
        )
    return found


Bench = Annotated[BenchApi, Depends(get_bench)]


class CampaignCreate(ApiModel):
    spec: str = Field(
        min_length=1,
        max_length=MAX_SPEC_BYTES,
        description="The campaign spec (YAML text; docs/BENCHMARKS.md).",
    )


class RunCounts(ApiModel):
    planned: int = 0
    submitted: int = 0
    done: int = 0
    failed: int = 0
    skipped: int = 0


class ExpansionSummary(ApiModel):
    groups: int
    configs: int
    dropped: int
    duplicates: int
    estimated_hours: float
    nodes: tuple[str, ...]
    unsupported: int = 0
    """V0.9 G4: runs stored skipped (``on_unsupported: skip``)."""
    session_label: str | None = None
    """V0.9 G4 (D241): the spec's session label (Wi-Fi vs Ethernet ...)."""


class CampaignOut(ApiModel):
    campaign_id: str
    name: str
    state: Literal["active", "done", "cancelled"]
    seed: int
    spec_digest: str
    run_count: int
    counts: RunCounts
    summary: ExpansionSummary
    created_by: str | None = None
    created_at: Utc


class CampaignDetail(CampaignOut):
    spec: str


class CampaignList(ApiModel):
    campaigns: tuple[CampaignOut, ...]
    count: int


class RunOut(ApiModel):
    run_id: str
    seq: int
    group_id: str
    config_id: str
    repetition: int
    family: str
    params: dict[str, Any]
    seed: int
    conditions: tuple[str, ...]
    strategy: Literal["local", "mode1"]
    node_id: str
    origin_node_id: str
    state: RunStateName
    job_id: str | None = None
    detail: str | None = None
    submitted_at: Utc | None = None
    ended_at: Utc | None = None


class RunList(ApiModel):
    campaign_id: str
    runs: tuple[RunOut, ...]
    count: int
    next_after: int | None = None
    """Pass as ``?after=`` for the next page; None = last page."""


class SubmitOutcome(ApiModel):
    run_id: str
    seq: int
    outcome: Literal["submitted", "deferred", "skipped", "already"]
    job_id: str | None = None
    detail: str | None = None


class SubmitResult(ApiModel):
    campaign_id: str
    outcomes: tuple[SubmitOutcome, ...]
    submitted: int
    deferred: int
    skipped: int
    counts: RunCounts


def _campaign_out(record: CampaignRecord) -> dict[str, Any]:
    summary = record.summary
    return {
        "campaign_id": record.id,
        "name": record.name,
        "state": record.state,
        "seed": record.seed,
        "spec_digest": record.spec_digest,
        "run_count": record.run_count,
        "counts": RunCounts(**record.counts),
        "summary": ExpansionSummary(
            groups=int(summary.get("groups", 0)),
            configs=int(summary.get("configs", 0)),
            dropped=int(summary.get("dropped", 0)),
            duplicates=int(summary.get("duplicates", 0)),
            estimated_hours=float(summary.get("estimated_hours", 0.0)),
            nodes=tuple(summary.get("nodes", ())),
            unsupported=int(summary.get("unsupported", 0) or 0),
            session_label=summary.get("session_label"),
        ),
        "created_by": record.created_by,
        "created_at": record.created_at,
    }


def _run_out(run: RunRow) -> RunOut:
    return RunOut(
        run_id=run.id,
        seq=run.seq,
        group_id=run.group_id,
        config_id=run.config_id,
        repetition=run.repetition,
        family=run.family,
        params=dict(run.params),
        seed=run.seed,
        conditions=run.conditions,
        strategy=run.strategy,  # type: ignore[arg-type]  # CHECK-constrained
        node_id=run.node_id,
        origin_node_id=run.origin_node_id,
        state=run.state,  # type: ignore[arg-type]  # CHECK-constrained
        job_id=run.job_id,
        detail=run.detail,
        submitted_at=run.submitted_at,
        ended_at=run.ended_at,
    )


def _campaign_id(value: str) -> str:
    try:
        parse_id(value, IdKind.CAMPAIGN)
    except InvalidIdError:
        raise ApiError.invalid("campaign_id is not a canonical camp_<ULID>") from None
    return value


def _unavailable() -> ApiError:
    return ApiError(
        FailureType.NETWORK_ERROR,
        "the database is unavailable; retry",
        headers={"Retry-After": "2"},
    )


async def _synced(bench: BenchApi, campaign_id: str) -> CampaignRecord:
    try:
        await bench.store.sync(campaign_id)
        record = await bench.store.get_campaign(campaign_id)
    except BenchStoreError:
        raise _unavailable() from None
    if record is None:
        raise ApiError.not_found("campaign")
    return record


def _threads(hardware: Mapping[str, Any]) -> int | None:
    for key in ("cpu_threads", "cpu_cores"):
        value = hardware.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
    return None


async def _nodes(registry: Registry) -> list[NodeRef]:
    """The campaign's candidate nodes with what they can do (V0.9 G4: the
    ``bench-conditions`` tag of their last Hello, their thread count)."""
    records = await registry.list_nodes(limit=MAX_NODES)
    return [
        NodeRef(
            r.node_id,
            r.hostname or None,
            conditions=advertised_kinds(
                [str(t) for t in r.hardware.get("capabilities") or []]
            ),
            threads=_threads(r.hardware),
        )
        for r in records
        if r.state is not NodeState.REVOKED
    ]


@bench_router.post(
    "/campaigns",
    status_code=status.HTTP_201_CREATED,
    response_model=CampaignDetail,
    summary="Create a benchmark campaign from a spec (admin): validate + expand once",
)
async def create_campaign(
    admin: AdminUser, bench: Bench, body: CampaignCreate
) -> CampaignDetail:
    nodes = await _nodes(bench.registry)
    try:
        spec = load_spec(body.spec)
        expansion = await asyncio.to_thread(expand, spec, nodes)
    except CampaignSpecError as exc:
        raise ApiError(
            FailureType.INVALID_INPUT,
            exc.message,
            extra={"problems": exc.problems[:50]},
        ) from None
    campaign_id = new_campaign_id()
    try:
        record = await bench.store.create_campaign(
            campaign_id=campaign_id,
            name=spec.name,
            spec_yaml=body.spec,
            spec_digest=spec_digest(spec),
            seed=spec.seed,
            expansion=expansion,
            nodes=[n.node_id for n in nodes],
            created_by=admin.user_id,
            session_label=spec.session_label,
        )
    except BenchStoreError:
        raise _unavailable() from None
    log.info(
        "controller.bench.campaign_created",
        campaign_id=campaign_id,
        name=spec.name,
        runs=record.run_count,
        configs=expansion.configs,
        estimated_hours=expansion.estimated_hours,
        user_id=admin.user_id,
    )
    return CampaignDetail(**_campaign_out(record), spec=record.spec_yaml)


@bench_router.get(
    "/campaigns",
    response_model=CampaignList,
    summary="Benchmark campaigns, newest first (admin)",
)
async def list_campaigns(
    _admin: AdminUser,
    bench: Bench,
    limit: Annotated[int, Query(ge=1, le=MAX_LIST)] = 100,
) -> CampaignList:
    try:
        await bench.store.sync()
        records = await bench.store.list_campaigns(limit=limit)
    except BenchStoreError:
        raise _unavailable() from None
    out = tuple(CampaignOut(**_campaign_out(r)) for r in records)
    return CampaignList(campaigns=out, count=len(out))


@bench_router.get(
    "/campaigns/{campaign_id}",
    response_model=CampaignDetail,
    summary="One benchmark campaign with its spec and run counts (admin)",
)
async def get_campaign(
    _admin: AdminUser,
    bench: Bench,
    campaign_id: Annotated[str, Path(max_length=_ID_MAX)],
) -> CampaignDetail:
    record = await _synced(bench, _campaign_id(campaign_id))
    return CampaignDetail(**_campaign_out(record), spec=record.spec_yaml)


# --- V0.9 G5 (0.9.6, D218/D246): labels, derived on demand -------------------


@bench_router.get(
    "/campaigns/{campaign_id}/labels",
    response_model=LabelsOut,
    summary="Per-group labels: best configuration, tie, insufficient or pending (admin)",
    responses={
        200: {
            "content": {PARQUET_MEDIA_TYPE: {}},
            "description": "JSON (default) or, with format=parquet, the labels "
            "table (labels_version 1, docs/TELEMETRY.md 'Labels')",
        }
    },
)
async def campaign_labels(
    admin: AdminUser,
    bench: Bench,
    export: Annotated[TelemetryExport, Depends(get_export)],
    campaign_id: Annotated[str, Path(max_length=_ID_MAX)],
    format: Annotated[  # noqa: A002 - the query parameter's public name
        Literal["json", "parquet"], Query(description="json (default) or parquet.")
    ] = "json",
) -> Any:
    """Labels from the campaign's run records (docs/TELEMETRY.md "Labels"):
    the one row filter (ok, self-check ok, no ``bench_role``), the latest
    attempt per (config, repetition), then the median-CI rule. Nothing is
    stored; the same records give the same answer (and the same Parquet
    bytes, D222). More label rows than ``[controller.telemetry]
    export_max_rows`` -> 413."""
    from meshforge.controller.telemetry.labels import build_labels
    from meshforge.controller.telemetry.labels_store import (
        LabelRowsLimitError,
        label_rows,
        planned_runs,
    )
    from meshforge.controller.telemetry.recorder import TelemetryStoreError

    record = await _synced(bench, _campaign_id(campaign_id))
    try:
        runs = await planned_runs(export.engine, record.id)
        rows = await label_rows(export.engine, record.id, limit=export.max_rows)
    except LabelRowsLimitError:
        raise ApiError(
            FailureType.INVALID_INPUT,
            f"more than {export.max_rows} records match; the campaign is too "
            "large to label in one request",
            http_status=413,
        ) from None
    except TelemetryStoreError:
        raise _unavailable() from None
    labels = await asyncio.to_thread(build_labels, record.id, runs, rows)
    log.info(
        "controller.bench.labels",
        campaign_id=record.id,
        groups=len(labels.groups),
        rows=labels.rows,
        format=format,
        user_id=admin.user_id,
        **labels.counts,
    )
    if format == "json":
        return labels_out(labels, campaign_state=record.state)
    from meshforge.controller.telemetry.labels_export import (
        iter_labels_parquet,
        labels_table_rows,
    )

    table = labels_table_rows(labels)
    return StreamingResponse(
        _label_pieces(iter_labels_parquet(table), campaign_id=record.id),
        media_type=PARQUET_MEDIA_TYPE,
        headers={
            "Content-Disposition": (
                f'attachment; filename="meshforge-labels-{record.id}.parquet"'
            ),
            "Cache-Control": "no-store",
            "X-MeshForge-Label-Rows": str(len(table)),
        },
    )


async def _label_pieces(
    pieces: Generator[bytes, None, None], *, campaign_id: str
) -> AsyncIterator[bytes]:
    """Each row group of the labels file as it is encoded (worker thread), in
    :data:`CHUNK_BYTES` slices - the record export's pattern (no
    ``Content-Length``; an encoding failure truncates the body: no footer)."""
    try:
        while True:
            piece = await asyncio.to_thread(next, pieces, None)
            if piece is None:
                break
            for start in range(0, len(piece), CHUNK_BYTES):
                yield piece[start : start + CHUNK_BYTES]
    except Exception as exc:
        log.exception(
            "controller.bench.labels_export_aborted",
            campaign_id=campaign_id,
            error_type=type(exc).__name__,
        )
        raise
    finally:
        try:
            pieces.close()
        except ValueError:  # still running in the worker thread (a hang-up)
            log.debug("controller.bench.labels_export_close_deferred")


@bench_router.get(
    "/campaigns/{campaign_id}/runs",
    response_model=RunList,
    summary="A campaign's runs in execution order (admin; keyset paging by seq)",
)
async def list_runs(
    _admin: AdminUser,
    bench: Bench,
    campaign_id: Annotated[str, Path(max_length=_ID_MAX)],
    state: Annotated[RunStateName | None, Query()] = None,
    after: Annotated[int, Query(ge=0, description="The last seq already seen.")] = 0,
    limit: Annotated[int, Query(ge=1, le=MAX_LIST)] = 100,
) -> RunList:
    record = await _synced(bench, _campaign_id(campaign_id))
    try:
        rows = await bench.store.runs(
            record.id, state=state, after_seq=after, limit=limit + 1
        )
    except BenchStoreError:
        raise _unavailable() from None
    page, more = rows[:limit], len(rows) > limit
    return RunList(
        campaign_id=record.id,
        runs=tuple(_run_out(r) for r in page),
        count=len(page),
        next_after=page[-1].seq if page and more else None,
    )


@bench_router.post(
    "/campaigns/{campaign_id}/submit",
    response_model=SubmitResult,
    summary="Submit the next k planned runs as pinned lowest-priority jobs (admin)",
)
async def submit_runs(
    admin: AdminUser,
    bench: Bench,
    campaign_id: Annotated[str, Path(max_length=_ID_MAX)],
    count: Annotated[int, Query(ge=1, le=MAX_SUBMIT)] = 1,
) -> SubmitResult:
    record = await _synced(bench, _campaign_id(campaign_id))
    if record.state != "active":
        raise ApiError(
            FailureType.CONFLICT, f"the campaign is {record.state}; nothing to submit"
        )
    started = time.monotonic()
    try:
        outcomes = await bench.submitter.submit_next(record, count)
        after = await bench.store.get_campaign(record.id)
    except (BenchSubmitError, BenchStoreError):
        raise _unavailable() from None
    tally = {
        kind: sum(1 for o in outcomes if o.outcome == kind)
        for kind in ("submitted", "deferred", "skipped")
    }
    log.info(
        "controller.bench.submitted",
        campaign_id=record.id,
        asked=count,
        user_id=admin.user_id,
        took_ms=int((time.monotonic() - started) * 1000),
        **tally,
    )
    return SubmitResult(
        campaign_id=record.id,
        outcomes=tuple(
            SubmitOutcome(
                run_id=o.run_id,
                seq=o.seq,
                outcome=o.outcome,
                job_id=o.job_id,
                detail=o.detail,
            )
            for o in outcomes
        ),
        submitted=tally["submitted"],
        deferred=tally["deferred"],
        skipped=tally["skipped"],
        counts=RunCounts(**(after or record).counts),
    )
