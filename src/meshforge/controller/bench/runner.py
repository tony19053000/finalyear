"""The unattended campaign runner (V0.9 G5, roadmap 0.9.7, D219/D246).

A supervised task inside the controller (no new service). Every
``[controller.bench] tick_s`` (:meth:`BenchRunner.tick`):

1. :meth:`~.store.BenchStore.sync` - finished runs of runner-owned campaigns
   are settled (:mod:`.outcome`: done / failed / requeued with ``tries + 1``
   for an infra failure while ``tries < max_infra_retries`` / requeued with
   ``tries`` unchanged for ``owner_preempted``);
2. for every started, ``active`` campaign (oldest first): past its spec's
   ``budget.max_hours`` (database time since ``started_at``) it stops
   submitting (:meth:`~.control.CampaignControl.stop_for_budget`); otherwise
   each node that still has planned runs gets ``max_concurrent_per_node`` -
   its non-terminal benchmark jobs (any campaign) **free slots**, except a
   node in **cool-down** (its last run of this campaign ended less than
   ``cooldown_s`` ago - a run whose job is terminal counts as ended even
   before ``sync`` settles it, and load + last end come from one snapshot:
   G5c item 0.0, D255; with ``max_concurrent_per_node > 1`` a sibling job
   that ends between this read and the submit is not covered - there is no
   lock on job state - while with the default 1 the running job fills the
   slot), whose **owner guard** is on (paused / owner active),
   or that has a **user job** running or queued pinned there (checklist 30:
   a benchmark never competes with a user's job);
3. :meth:`~.submit.BenchSubmitter.submit_next` with ``count`` = the free
   slots, **capped at** ``max_submit_per_tick`` for the whole tick (D245/N4:
   small budgets, never a 2 000-run examine per tick) and at most
   ``max_examine_per_tick`` runs examined. A tick that hit that cap leaves a
   **cursor** (the last ``seq`` examined) and the next tick continues after
   it, wrapping to the head (fix r1, D249): more per-run deferrals at the head
   than the cap never stall the campaign.

**Resumable.** All state is in PostgreSQL; the runner keeps only caches (the
parsed budget). After a controller ``kill -9`` the next process's runner
continues where the rows are: a run is submitted under
``bench:<run_id>[:<generation>]`` (one job per generation - a crash between
the job's commit and the link is linked again, never duplicated), and
in-flight jobs are reconciled by the existing job paths.

**Supervised** like ``LiveStreams.run``: one crash in a tick is logged and
backed off (``tick_s * 2^n``, at most :data:`MAX_BACKOFF_S`) and never kills the
controller; after :data:`DEGRADED_AFTER` crashes in a row ``/healthz`` says
``degraded`` (``bench-runner``) until a tick succeeds.

**Events.** ``campaign.progress`` ``{campaign_id, state, counts, done_percent,
eta_s, infra_failure_rate}`` at most once a second per campaign (admins only).
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Protocol

from meshforge.bench.campaign import CampaignSpecError, load_spec
from meshforge.common.logging import get_logger
from meshforge.controller.bench.control import CampaignControl, RunnerCampaign
from meshforge.controller.bench.store import BenchStore
from meshforge.controller.bench.submit import BenchSubmitter, owner_guarded

__all__ = [
    "DEGRADED_AFTER",
    "MAX_BACKOFF_S",
    "BenchRunner",
    "RunnerSettings",
    "TickReport",
]

log = get_logger(__name__)

DEGRADED_AFTER: Final = 5
MAX_BACKOFF_S: Final = 60.0
PROGRESS_EVERY_S: Final = 1.0


class _Sessions(Protocol):
    def get(self, node_id: str) -> object | None: ...


@dataclass(frozen=True, slots=True)
class RunnerSettings:
    tick_s: float = 2.0
    max_submit_per_tick: int = 4
    max_examine_per_tick: int = 32


@dataclass(slots=True)
class TickReport:
    """What one tick did (tests and the debug log read it)."""

    campaigns: int = 0
    submitted: int = 0
    deferred: int = 0
    skipped: int = 0
    budget_stopped: list[str] = field(default_factory=list)
    excluded: dict[str, str] = field(default_factory=dict)
    """node_id -> why it got nothing (cooldown, owner_guard, user_job, full)."""


class BenchRunner:
    """See module docstring."""

    def __init__(
        self,
        *,
        store: BenchStore,
        control: CampaignControl,
        submitter: BenchSubmitter,
        settings: RunnerSettings,
        sessions: _Sessions | None = None,
        publish: Callable[[Mapping[str, Any]], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._store = store
        self._control = control
        self._submitter = submitter
        self.settings = settings
        self._sessions = sessions
        self._publish = publish
        self._clock = clock
        self._sleep = sleep
        self._budgets: dict[str, float | None] = {}
        self._last_progress: dict[str, float] = {}
        self._cursors: dict[str, int] = {}
        self._wake = asyncio.Event()
        self.crashes = 0
        self.ticks = 0

    @property
    def degraded(self) -> bool:
        return self.crashes >= DEGRADED_AFTER

    def wake(self) -> None:
        """Tick now (a control changed a campaign)."""
        self._wake.set()

    def _max_hours(self, campaign: RunnerCampaign) -> float | None:
        if campaign.id not in self._budgets:
            try:
                hours = load_spec(campaign.spec_yaml).budget.max_hours
            except CampaignSpecError:  # stored from a validated spec
                log.warning(
                    "controller.bench.budget_unreadable", campaign_id=campaign.id
                )
                hours = None
            self._budgets[campaign.id] = hours
        return self._budgets[campaign.id]

    async def tick(self) -> TickReport:
        """One pass (module docstring)."""
        report = TickReport()
        await self._store.sync()
        campaigns = await self._control.runner_campaigns()
        report.campaigns = len(campaigns)
        user_busy = await self._control.user_busy_nodes() if campaigns else set()
        left = self.settings.max_submit_per_tick
        for campaign in campaigns:
            hours = self._max_hours(campaign)
            if hours is not None and campaign.elapsed_s >= hours * 3600.0:
                skipped = await self._control.stop_for_budget(
                    campaign.id, f"budget: max_hours {hours:g} reached"
                )
                report.budget_stopped.append(campaign.id)
                log.info(
                    "controller.bench.budget_reached",
                    campaign_id=campaign.id,
                    max_hours=hours,
                    skipped=skipped,
                )
                continue
            if left <= 0:
                continue
            budget = await self._node_budget(campaign, user_busy, report)
            count = min(left, sum(budget.values()))
            if count <= 0:
                continue
            record = await self._store.get_campaign(campaign.id)
            if record is None or record.state != "active":
                continue
            cursor = self._cursors.get(campaign.id, 0)
            batch = await self._submitter.submit_next(
                record,
                count,
                node_budget=budget,
                max_examined=self.settings.max_examine_per_tick,
                **({"start_after": cursor} if cursor else {}),
            )
            # fix r1 (D249): a tick that hit the examine cap continues after
            # the last run it examined - per-run deferrals at the head cannot
            # stall the campaign; otherwise the next tick starts at the head
            self._cursors[campaign.id] = batch.cursor
            outcomes = batch.outcomes
            done = sum(1 for o in outcomes if o.outcome == "submitted")
            left -= done
            report.submitted += done
            report.deferred += sum(1 for o in outcomes if o.outcome == "deferred")
            report.skipped += sum(1 for o in outcomes if o.outcome == "skipped")
        await self._progress()
        self.ticks += 1
        if report.submitted or report.budget_stopped:
            log.info(
                "controller.bench.tick",
                campaigns=report.campaigns,
                submitted=report.submitted,
                deferred=report.deferred,
                skipped=report.skipped,
            )
        return report

    async def _node_budget(
        self, campaign: RunnerCampaign, user_busy: set[str], report: TickReport
    ) -> dict[str, int]:
        nodes = await self._control.planned_nodes(campaign.id)
        if not nodes:
            return {}
        # G5c item 0.0 (D255): load and last end from one snapshot, and a
        # job that ended after this tick's sync counts as an end (cool-down)
        load, since = await self._control.node_slots(campaign.id, nodes)
        budget: dict[str, int] = {}
        for node in sorted(nodes):
            if node in user_busy:
                report.excluded[node] = "user_job"
                continue
            live = None if self._sessions is None else self._sessions.get(node)
            if owner_guarded(live) is not None:
                report.excluded[node] = "owner_guard"
                continue
            ended = since.get(node)
            if ended is not None and ended < campaign.cooldown_s:
                report.excluded[node] = "cooldown"
                continue
            free = campaign.max_concurrent_per_node - load.get(node, 0)
            if free <= 0:
                report.excluded[node] = "full"
                continue
            budget[node] = free
        return budget

    async def _progress(self) -> None:
        if self._publish is None:
            return
        now = self._clock()
        for record in await self._store.list_campaigns(limit=50):
            if record.started_at is None:
                continue
            if record.state not in ("active", "paused") and (
                record.id not in self._last_progress
            ):
                continue
            last = self._last_progress.get(record.id)
            if last is not None and now - last < PROGRESS_EVERY_S:
                continue
            status = await self._control.status(
                record.id, record.counts, per_node=record.max_concurrent_per_node
            )
            self._last_progress[record.id] = now
            if record.state not in ("active", "paused"):
                del self._last_progress[record.id]  # one final event, then quiet
            self._publish(
                {
                    "campaign_id": record.id,
                    "state": record.state,
                    "finished_reason": record.finished_reason,
                    "counts": dict(record.counts),
                    "done_percent": status.done_percent,
                    "eta_s": status.eta_s,
                    "infra_failure_rate": status.infra_failure_rate,
                }
            )

    async def run(self) -> None:
        """Until cancelled; supervised (module docstring)."""
        while True:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), self.settings.tick_s)
            self._wake.clear()
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.crashes += 1
                log.exception(
                    "controller.bench.runner_crashed",
                    cause=type(exc).__name__,
                    consecutive_crashes=self.crashes,
                )
                await self._sleep(
                    min(
                        MAX_BACKOFF_S, self.settings.tick_s * 2 ** min(self.crashes, 16)
                    )
                )
            else:
                if self.crashes:
                    log.info("controller.bench.runner_recovered", after=self.crashes)
                self.crashes = 0
