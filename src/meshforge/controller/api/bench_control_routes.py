"""``/v1/bench/campaigns/{id}/{start|pause|resume|cancel|status}`` (V0.9 G5, D219/D246).

**Admin only**, like every campaign route (a campaign is job submission at
scale). All controls are idempotent (:mod:`meshforge.controller.bench.control`):

* ``POST .../start`` ``{max_concurrent_per_node?, cooldown_s?,
  max_infra_retries?}`` - the background runner owns the campaign from now
  (G3's ``submit`` is then refused ``409`` ``campaign_running``); also resumes
  a paused one;
* ``POST .../pause`` - nothing new is submitted; running jobs finish;
* ``POST .../resume`` - back to ``active``;
* ``POST .../cancel`` - the campaign's non-terminal benchmark jobs (run jobs,
  fillers, warm-ups) are cancelled, its open runs ``skipped`` (``cancelled``);
* ``GET .../status`` - counts by state, done %, ETA from the median run time,
  the infrastructure failure rate (``owner_preempted`` excluded, D246).

A control a finished campaign cannot take is ``409 conflict``.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Path, status
from pydantic import Field

from meshforge.common.errors import FailureType
from meshforge.common.logging import get_logger
from meshforge.controller.api.auth import AdminUser
from meshforge.controller.api.bench_routes import (
    Bench,
    BenchApi,
    RunCounts,
    _campaign_id,
    _synced,
    _unavailable,
)
from meshforge.controller.api.errors import ApiError
from meshforge.controller.api.models import ApiModel, Utc
from meshforge.controller.bench.control import (
    CampaignControl,
    CampaignStateError,
    Throttle,
)
from meshforge.controller.bench.store import BenchStoreError, CampaignRecord

__all__ = ["bench_control_router"]

log = get_logger(__name__)

_ID_MAX = 40

bench_control_router = APIRouter(prefix="/v1/bench", tags=["bench"])

CampaignId = Annotated[str, Path(max_length=_ID_MAX)]


class StartBody(ApiModel):
    max_concurrent_per_node: int | None = Field(
        default=None,
        ge=1,
        le=64,
        strict=True,
        description="Benchmark jobs at once per node.",
    )
    cooldown_s: float | None = Field(
        default=None,
        ge=0,
        le=86_400,
        allow_inf_nan=False,
        strict=True,
        description="Pause on a node after each of its runs ends.",
    )
    max_infra_retries: int | None = Field(
        default=None,
        ge=0,
        le=10,
        strict=True,
        description="Retries of an infrastructure failure.",
    )


class CampaignControlOut(ApiModel):
    campaign_id: str
    state: Literal["active", "paused", "done", "cancelled"]
    runner_owned: bool
    started_at: Utc | None = None
    finished_reason: Literal["complete", "budget", "cancelled"] | None = None
    max_concurrent_per_node: int
    cooldown_s: float
    max_infra_retries: int
    counts: RunCounts
    cancelled_jobs: int = 0
    """``cancel`` only: the benchmark jobs a cancel was requested for."""


class CampaignStatusOut(CampaignControlOut):
    done_percent: float
    median_run_s: float | None = None
    eta_s: float | None = None
    """Open runs x median run time / (open nodes x per-node concurrency)."""
    infra_failure_rate: float | None = None
    """Infrastructure failures / run records, ``owner_preempted`` excluded."""
    records: int
    infra_failures: int
    preempted: int
    """Attempts the owner guard stopped (requeued, not failures)."""
    retries: int
    """Infrastructure retries spent over all runs."""
    runner_degraded: bool = False


def _control(bench: BenchApi) -> CampaignControl:
    if bench.control is None:
        raise ApiError(
            FailureType.CONFIG_ERROR, "the campaign runner is not configured"
        )
    return bench.control


def _out(record: CampaignRecord, *, cancelled: int = 0) -> dict[str, Any]:
    return {
        "campaign_id": record.id,
        "state": record.state,
        "runner_owned": record.runner_owned,
        "started_at": record.started_at,
        "finished_reason": record.finished_reason,
        "max_concurrent_per_node": record.max_concurrent_per_node,
        "cooldown_s": record.cooldown_s,
        "max_infra_retries": record.max_infra_retries,
        "counts": RunCounts(**record.counts),
        "cancelled_jobs": cancelled,
    }


async def _apply(
    bench: BenchApi, campaign_id: str, verb: str, body: StartBody | None = None
) -> CampaignRecord:
    control = _control(bench)
    checked = _campaign_id(campaign_id)
    try:
        if verb == "start":
            found = await control.start(
                checked,
                None
                if body is None
                else Throttle(
                    max_concurrent_per_node=body.max_concurrent_per_node,
                    cooldown_s=body.cooldown_s,
                    max_infra_retries=body.max_infra_retries,
                ),
            )
        elif verb == "pause":
            found = await control.pause(checked)
        else:
            found = await control.resume(checked)
    except CampaignStateError as exc:
        raise ApiError(FailureType.CONFLICT, exc.message) from None
    except BenchStoreError:
        raise _unavailable() from None
    if not found:
        raise ApiError.not_found("campaign")
    if bench.runner is not None:
        bench.runner.wake()
    return await _synced(bench, checked)


@bench_control_router.post(
    "/campaigns/{campaign_id}/start",
    response_model=CampaignControlOut,
    summary="Hand a campaign to the background runner (admin; idempotent)",
)
async def start_campaign(
    admin: AdminUser,
    bench: Bench,
    campaign_id: CampaignId,
    body: Annotated[StartBody | None, Body()] = None,
) -> CampaignControlOut:
    record = await _apply(bench, campaign_id, "start", body)
    log.info(
        "controller.bench.campaign_started",
        campaign_id=record.id,
        user_id=admin.user_id,
        max_concurrent_per_node=record.max_concurrent_per_node,
        cooldown_s=record.cooldown_s,
        max_infra_retries=record.max_infra_retries,
    )
    return CampaignControlOut(**_out(record))


@bench_control_router.post(
    "/campaigns/{campaign_id}/pause",
    response_model=CampaignControlOut,
    summary="Stop submitting a campaign's runs; running jobs finish (admin)",
)
async def pause_campaign(
    admin: AdminUser, bench: Bench, campaign_id: CampaignId
) -> CampaignControlOut:
    record = await _apply(bench, campaign_id, "pause")
    log.info(
        "controller.bench.campaign_paused", campaign_id=record.id, user_id=admin.user_id
    )
    return CampaignControlOut(**_out(record))


@bench_control_router.post(
    "/campaigns/{campaign_id}/resume",
    response_model=CampaignControlOut,
    summary="Resume a paused campaign (admin)",
)
async def resume_campaign(
    admin: AdminUser, bench: Bench, campaign_id: CampaignId
) -> CampaignControlOut:
    record = await _apply(bench, campaign_id, "resume")
    log.info(
        "controller.bench.campaign_resumed",
        campaign_id=record.id,
        user_id=admin.user_id,
    )
    return CampaignControlOut(**_out(record))


@bench_control_router.post(
    "/campaigns/{campaign_id}/cancel",
    status_code=status.HTTP_200_OK,
    response_model=CampaignControlOut,
    summary="Cancel a campaign: its benchmark jobs cancelled, open runs skipped (admin)",
)
async def cancel_campaign(
    admin: AdminUser, bench: Bench, campaign_id: CampaignId
) -> CampaignControlOut:
    control = _control(bench)
    checked = _campaign_id(campaign_id)
    try:
        jobs = await control.cancel(checked)
    except CampaignStateError as exc:
        raise ApiError(FailureType.CONFLICT, exc.message) from None
    except BenchStoreError:
        raise _unavailable() from None
    if jobs is None:
        raise ApiError.not_found("campaign")
    cancelled = 0
    if bench.cancel_job is not None:
        for job_id in jobs:
            try:
                await bench.cancel_job(job_id)
            except Exception as exc:  # noqa: BLE001 - logged; a cancel is idempotent
                log.warning(
                    "controller.bench.cancel_job_failed",
                    campaign_id=checked,
                    job_id=job_id,
                    cause=type(exc).__name__,
                )
            else:
                cancelled += 1
    log.info(
        "controller.bench.campaign_cancelled",
        campaign_id=checked,
        jobs=cancelled,
        user_id=admin.user_id,
    )
    record = await _synced(bench, checked)
    return CampaignControlOut(**_out(record, cancelled=cancelled))


@bench_control_router.get(
    "/campaigns/{campaign_id}/status",
    response_model=CampaignStatusOut,
    summary="A campaign's progress: counts, done %, ETA, infra failure rate (admin)",
)
async def campaign_status(
    _admin: AdminUser, bench: Bench, campaign_id: CampaignId
) -> CampaignStatusOut:
    control = _control(bench)
    record = await _synced(bench, _campaign_id(campaign_id))
    try:
        stats = await control.status(
            record.id, record.counts, per_node=record.max_concurrent_per_node
        )
    except BenchStoreError:
        raise _unavailable() from None
    return CampaignStatusOut(
        **_out(record),
        done_percent=stats.done_percent,
        median_run_s=stats.median_run_s,
        eta_s=stats.eta_s,
        infra_failure_rate=stats.infra_failure_rate,
        records=stats.records,
        infra_failures=stats.infra_failures,
        preempted=stats.preempted,
        retries=stats.retries,
        runner_degraded=bench.runner is not None and bench.runner.degraded,
    )
