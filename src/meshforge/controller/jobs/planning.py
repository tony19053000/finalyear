"""The dispatcher's side of the planner: snapshot in, decision out (V0.6 G2,
D102-D107, D112).

:class:`DispatchPlanner` is called by :meth:`~.execution_store.ExecutionStore.
dispatch_planned` once per queued job, inside the dispatch transaction, with
the tick's :class:`~.queue_reads.FabricRows`. It

1. builds the tick's :class:`~meshforge.controller.planner.ClusterSnapshot`
   **once** (node rows + slot use from the database; liveness, the live
   resource sample and the cache index's trust from this process) and caches
   it on the rows - the store patches it as attempts are created;
2. turns the job into a :class:`~meshforge.controller.planner.PlanRequest`
   (pinned or auto, its Local candidate, its trees, its history);
3. runs :func:`~meshforge.controller.planner.plan` with the job's policy (its
   own, else ``[controller.planner] default_policy``);
4. maps the decision to the store's answer: :class:`Dispatch` (with the
   decision row), :class:`Wait` (the decision row is written only when its
   reasons changed) or :class:`DispatchFailure`.

**Pinned jobs keep V0.4's rules (D65, D103):** node gone (no live or lingering
session) after ``reconnect_grace_s`` -> ``FAILED(node_lost,
placed_node_offline)``; the node row deleted -> ``placed_node_deleted``; the
node stopped accepting jobs -> ``no_feasible_plan`` (``node_no_longer_accepts_
jobs``), stopped being able to pass a GPU -> ``node_can_no_longer_provide_a_
gpu``; any other static rejection -> ``no_feasible_plan`` naming its code; a
live rejection (SUSPECT, DRAINING, busy slots...) -> wait.

**Auto jobs (D104):** a node chosen -> dispatch; every candidate rejected for a
*static* reason (none could ever run it - e.g. its only capable node was
revoked) -> ``FAILED(no_feasible_plan)``; otherwise wait. A **retry** of an
auto job (V0.6 G3, D110) carries the nodes its earlier attempts failed on as
``PlanRequest.excluded`` (:func:`~meshforge.controller.planner.exclusions`);
a pinned job retries on its own node only.

The planner itself stays pure; everything impure is read here, once.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from collections import OrderedDict
from collections.abc import Callable, Sequence
from typing import Final

from pydantic import ValidationError

from meshforge.common.errors import FailureType, InvalidIdError
from meshforge.common.ids import new_attempt_id
from meshforge.common.logging import get_logger
from meshforge.common.manifest import Manifest, ManifestError
from meshforge.controller.jobs.env_health import KnownBadEnvs, env_key
from meshforge.controller.jobs.execution_store import (
    DecisionRecord,
    Dispatch,
    DispatchDecision,
    DispatchFailure,
    Wait,
)
from meshforge.controller.jobs.queue_reads import FabricRows, NodeRow
from meshforge.controller.jobs.spec import (
    choose_gpus,
    start_snapshot,
    start_telemetry,
)
from meshforge.controller.jobs.store import JobRecord
from meshforge.controller.liveness import NodeState
from meshforge.controller.planner import (
    ClusterSnapshot,
    Decision,
    History,
    NodeSnapshot,
    PastFailure,
    PlannerConfig,
    PlanRequest,
    RejectCode,
    TreeNeed,
    exclusions,
    plan,
)
from meshforge.controller.planner.decision_json import candidates_json
from meshforge.controller.planner.inputs import node_snapshot, plan_request
from meshforge.controller.resources import ResourceTracker
from meshforge.controller.session.manager import SessionManager
from meshforge.controller.settings import JobApiSettings, PlannerSettings

__all__ = ["DispatchPlanner", "decision_record"]

log = get_logger(__name__)

_JOB_IS_BAD: Final = (ValidationError, ManifestError, InvalidIdError)
"""Errors that mean the stored job itself is unusable (reviewer N-1): only
these fail the job; any other planning error leaves it queued."""
_ERRORS_LOGGED_MAX: Final = 1024

_GPU_GONE: Final = frozenset(
    {RejectCode.NO_GPU, RejectCode.NO_GPU_RUNTIME, RejectCode.NO_GPU_SLOTS}
)


def _idle(snapshot: ClusterSnapshot) -> ClusterSnapshot:
    """``snapshot`` with no slot, GPU or attempt in use on any node (the shadow
    plan's view, D235)."""
    return dataclasses.replace(
        snapshot,
        nodes=tuple(
            dataclasses.replace(
                n,
                used_cpu_slots=0,
                used_gpu_slots=0,
                live_attempts=0,
                busy_gpus=frozenset(),
            )
            for n in snapshot.nodes
        ),
    )


def decision_record(decision: Decision) -> DecisionRecord:
    return DecisionRecord(
        policy=decision.policy,
        snapshot_digest=decision.snapshot_digest,
        reason_key=decision.reason_key,
        candidates=candidates_json(decision),
        chosen_node_id=None if decision.chosen is None else decision.chosen.node_id,
    )


class DispatchPlanner:
    """See module docstring. Lives in the dispatcher's one task."""

    def __init__(
        self,
        *,
        manager: SessionManager,
        settings: JobApiSettings,
        planner: PlannerSettings,
        clock: Callable[[], float],
        wall_clock: Callable[[], dt.datetime],
        started_at: float,
        resources: ResourceTracker | None = None,
        known_bad_envs: KnownBadEnvs | None = None,
        cache_live: Callable[[str, str], bool] | None = None,
        link_stale_after_s: float = 3600.0,
        min_bw_bytes_s: float = 1024.0,
    ) -> None:
        self._manager = manager
        self._settings = settings
        self._planner = planner
        self._clock = clock
        self._wall = wall_clock
        self._started_at = started_at
        self._resources = resources
        self._known_bad = known_bad_envs
        self._cache_live = cache_live
        self.round_robin_after: str | None = None
        self.planning_errors = 0
        """Reviewer N-1: jobs left queued because planning them raised an
        error that is not the job's fault (the metric; see :meth:`decide`)."""
        self._errors_logged: OrderedDict[tuple[str, str], None] = OrderedDict()
        """D107: the node ``round_robin`` chose last (memory only)."""
        self.config = PlannerConfig(
            default_bw_bytes_s=planner.default_bw_bytes_s,
            default_compute_s=planner.default_compute_s,
            default_env_s=planner.default_env_s,
            max_attempts_per_node=settings.max_attempts_per_node,
            resource_weights=planner.resource_weights,
            link_stale_after_s=link_stale_after_s,
            min_bw_bytes_s=min_bw_bytes_s,
        )

    # ------------------------------------------------------------ snapshot

    def _node(self, row: NodeRow, fabric: FabricRows) -> NodeSnapshot:
        live = self._manager.get(row.node_id)
        state = row.state
        if live is not None:
            state = live.state.value
        elif self._manager.get_lingering(row.node_id) is not None:
            state = NodeState.SUSPECT.value
        sample = (
            None if self._resources is None else self._resources.latest(row.node_id)
        )
        used_cpu, used_gpu = fabric.used_slots(row.node_id)
        cached = fabric.holdings.get(row.node_id, frozenset())
        envs: dict[str, int] = fabric.env_holdings.get(row.node_id, {})
        if live is None or (
            self._cache_live is not None
            and not self._cache_live(row.node_id, live.session_id)
        ):
            cached = frozenset()  # a stale or unknown index is not a measurement
            envs = {}
        cpu_slots, gpu_slots = row.cpu_slots, row.gpu_slots
        if live is not None and live.policy_slots is not None:
            cpu_slots, gpu_slots = live.policy_slots  # V0.7 G5: a live change
        return node_snapshot(
            row.node_id,
            state=state,
            connected=live is not None,
            hardware=row.hardware,
            hardware_scores=row.hardware_scores,
            advertised_cpu_slots=cpu_slots,
            advertised_gpu_slots=gpu_slots,
            used_cpu_slots=used_cpu,
            used_gpu_slots=used_gpu,
            live_attempts=used_cpu + used_gpu,
            cpu_percent=None if sample is None else sample.cpu_percent,
            ram_free_bytes=None
            if sample is None or not sample.ram_total_bytes
            else max(0, sample.ram_total_bytes - sample.ram_used_bytes),
            disk_free_bytes=None
            if sample is None or not sample.disks
            else max(d.free_bytes for d in sample.disks),
            cached=cached,
            busy_gpus=fabric.busy_gpus(row.node_id),
            cached_envs=envs,
            policy=None if live is None else live.policy,
        )

    def snapshot(self, fabric: FabricRows) -> ClusterSnapshot:
        """The tick's snapshot (built once, cached on ``fabric``)."""
        if fabric.snapshot is None:
            fabric.snapshot = ClusterSnapshot(
                nodes=tuple(
                    self._node(row, fabric)
                    for row in sorted(fabric.nodes.values(), key=lambda r: r.node_id)
                ),
                round_robin_after=self.round_robin_after,
                config=self.config,
                links=dict(fabric.links),
                controller_links=dict(fabric.controller_links),
                env_builds=dict(fabric.env_builds),
            )
        return fabric.snapshot

    def _for_job(self, snapshot: ClusterSnapshot, key: str) -> ClusterSnapshot:
        """The snapshot with this job's known-bad env (D116) marked."""
        if self._known_bad is None:
            return snapshot
        bad = {
            n.node_id for n in snapshot.nodes if self._known_bad.is_bad(n.node_id, key)
        }
        if not bad:
            return snapshot
        return dataclasses.replace(
            snapshot,
            nodes=tuple(
                dataclasses.replace(n, known_bad_envs=n.known_bad_envs | {key})
                if n.node_id in bad
                else n
                for n in snapshot.nodes
            ),
        )

    # ------------------------------------------------------------ decide

    def policy_of(self, job: JobRecord) -> str:
        return job.scheduling.policy or self._planner.default_policy

    def decide(self, job: JobRecord, fabric: FabricRows) -> DispatchDecision:
        """The job's decision, isolated per job (G2 fix D-1, reviewer N-1).

        * The **job is bad** - its stored manifest no longer validates, or a
          stored id/manifest value is refused (:data:`_JOB_IS_BAD`) -> that job
          alone is failed ``internal_error`` (it will never plan; submitting
          it again is the fix).
        * **Anything else** is a bug or a transient fault on *our* side, not
          the job's: the job stays ``QUEUED`` (:class:`Wait`, no decision
          row), :attr:`planning_errors` counts it and the first occurrence per
          ``(job, error type)`` is logged with its traceback - so a planner bug
          can never wipe the queue, and the rest of the batch still plans.
        """
        try:
            return self._decide(job, fabric)
        except _JOB_IS_BAD as exc:
            log.error(  # noqa: TRY400 - typed event; stored values stay out of logs
                "controller.jobs.unplannable_job",
                job_id=job.id,
                error_type=type(exc).__name__,
            )
            return DispatchFailure(
                FailureType.INTERNAL_ERROR,
                f"the stored job could not be planned ({type(exc).__name__}); "
                "submit it again",
            )
        except Exception as exc:  # our fault, not the job's: keep it queued
            self.planning_errors += 1
            key = (job.id, type(exc).__name__)
            if key not in self._errors_logged:
                self._errors_logged[key] = None
                while len(self._errors_logged) > _ERRORS_LOGGED_MAX:
                    self._errors_logged.popitem(last=False)
                log.exception(
                    "controller.jobs.planning_error",
                    job_id=job.id,
                    error_type=type(exc).__name__,
                )
            return Wait()

    def _decide(self, job: JobRecord, fabric: FabricRows) -> DispatchDecision:
        manifest = Manifest.model_validate(dict(job.manifest))
        pinned = str(job.placement.get("node_id") or "") or None
        if pinned is not None:
            early = self._pinned_gone(job, pinned, fabric)
            if early is not None:
                return early
        key = env_key(
            job.manifest, tree_id=job.tree_id, bundle_digest=job.bundle_digest
        )
        trees = self._trees(job, fabric)
        plan_job = plan_request(
            manifest,
            job_id=job.id,
            attempt_no=job.attempt_epoch + 1,
            env_key=key,
            trees=trees,
            pinned_node_id=pinned,
            submitter_node_id=job.scheduling.submitter_node_id,
            seeded=job.scheduling.submitter_seeded,
            fingerprint=job.scheduling.fingerprint,
            history=fabric.history.get(job.scheduling.fingerprint or "", History()),
            owner_name=None
            if job.owner_user_id is None
            else fabric.owner_names.get(job.owner_user_id),
        )
        snapshot = self._for_job(self.snapshot(fabric), key)
        past = fabric.past_failures.get(job.id, ())
        if past and pinned is None:
            # D110: a retry of an auto job avoids where it failed (soft, plan.py)
            plan_job = dataclasses.replace(
                plan_job,
                excluded=exclusions(
                    past,
                    snapshot.nodes,
                    now=self._wall(),
                    exclude_s=self._planner.retry_exclude_s,
                ),
            )
        decision = plan(plan_job, snapshot, self.policy_of(job), self._wall())
        record = decision_record(decision)
        if decision.chosen is not None and pinned is not None:
            record = self._with_shadow(record, plan_job, snapshot, job, past)
        if decision.chosen is not None:
            return self._dispatch(
                job, manifest, decision, fabric, record, trees=trees, cluster=snapshot
            )
        if pinned is not None:
            return self._pinned_refused(decision, record)
        if decision.all_static_rejections:
            return DispatchFailure(
                FailureType.NO_FEASIBLE_PLAN,
                "no node of the fabric can run this job (see compute explain)",
                record,
            )
        return Wait(record)

    def _with_shadow(
        self,
        record: DecisionRecord,
        plan_job: PlanRequest,
        snapshot: ClusterSnapshot,
        job: JobRecord,
        past: Sequence[PastFailure] = (),
    ) -> DecisionRecord:
        """V0.9 G3 (D215, fix r1 D235): the node the planner **prefers** for
        this pinned job - the same request unpinned, on the same snapshot with
        **slot occupancy ignored**: every slot (and GPU) of a node is treated
        as free, so a node that is busy right now still wins if it is the
        better place. Everything else is kept: feasibility (capabilities,
        hardware, owner policy, a slot *kind* the node has at all), health
        (state, live session) and the job's retry exclusions (D110, with the
        soft lift rule of ``plan``). So ``planner_node_id`` is a *preference*
        label, not "what would have run this second" (docs/TELEMETRY.md).

        Pure (no reservation, no round-robin pointer moved, no row of its own);
        it only adds the node and its estimate to the dispatched decision. A
        failure here never blocks the pinned dispatch."""
        try:
            request = dataclasses.replace(
                plan_job,
                pinned_node_id=None,
                excluded=exclusions(
                    past,
                    snapshot.nodes,
                    now=self._wall(),
                    exclude_s=self._planner.retry_exclude_s,
                )
                if past
                else {},
            )
            shadow = plan(request, _idle(snapshot), self.policy_of(job), self._wall())
        except Exception as exc:  # the shadow is telemetry, never a condition
            log.exception(
                "controller.jobs.shadow_plan_failed",
                job_id=job.id,
                error_type=type(exc).__name__,
            )
            return record
        chosen = shadow.chosen
        if chosen is None:
            return record
        return dataclasses.replace(
            record,
            shadow_chosen_node_id=chosen.node_id,
            shadow_estimate_s=None
            if chosen.estimate is None
            else round(chosen.estimate.total_s, 6),
        )

    def _pinned_gone(
        self, job: JobRecord, node_id: str, fabric: FabricRows
    ) -> DispatchDecision:
        """V0.4 D65 for a pinned job: its node gone for good fails it; a node
        that may come back (lingering, or the controller just restarted) is
        left to the planner, which records why the job waits."""
        live = self._manager.get(node_id)
        if (
            live is None
            and self._manager.get_lingering(node_id) is None
            and self._clock() - self._started_at >= self._settings.reconnect_grace_s
        ):
            log.info("controller.jobs.pinned_node_gone", job_id=job.id, node_id=node_id)
            return DispatchFailure(FailureType.NODE_LOST, "placed_node_offline")
        if node_id not in fabric.nodes:
            if live is not None:
                return DispatchFailure(FailureType.NODE_LOST, "placed_node_deleted")
            return Wait()
        return None

    @staticmethod
    def _pinned_refused(decision: Decision, record: DecisionRecord) -> DispatchDecision:
        refused = decision.candidates[0].rejection if decision.candidates else None
        if refused is None:  # pragma: no cover - pinned node checked present
            return Wait(record)
        if refused.code is RejectCode.NOT_EXEC:
            return DispatchFailure(
                FailureType.NO_FEASIBLE_PLAN, "node_no_longer_accepts_jobs", record
            )
        if refused.code in _GPU_GONE:
            return DispatchFailure(
                FailureType.NO_FEASIBLE_PLAN, "node_can_no_longer_provide_a_gpu", record
            )
        if refused.static:
            return DispatchFailure(
                FailureType.NO_FEASIBLE_PLAN,
                f"node_rejected_{refused.code.value}",
                record,
            )
        return Wait(record)

    def _trees(self, job: JobRecord, fabric: FabricRows) -> tuple[TreeNeed, ...]:
        inputs = fabric.input_trees.get(job.id, ())
        project = job.tree_id or job.bundle_digest or ""
        return (
            TreeNeed(project, job.scheduling.input_bytes or 0),
            *(TreeNeed(tree, size) for tree, size in inputs),
        )

    def _dispatch(
        self,
        job: JobRecord,
        manifest: Manifest,
        decision: Decision,
        fabric: FabricRows,
        record: DecisionRecord,
        *,
        trees: tuple[TreeNeed, ...] = (),
        cluster: ClusterSnapshot | None = None,
    ) -> Dispatch | Wait:
        chosen = decision.chosen
        if chosen is None or chosen.slot is None:  # pragma: no cover - caller checked
            raise ValueError("dispatch without a chosen candidate")
        row = fabric.nodes[chosen.node_id]
        live = self._manager.get(chosen.node_id)
        gpus = (
            choose_gpus(manifest, row.hardware, fabric.busy_gpus(chosen.node_id))
            if chosen.slot == "gpu"
            else ()
        )
        if chosen.slot == "gpu" and not gpus:  # pragma: no cover - planner checked
            return Wait(record)
        snapshot = start_snapshot(
            hardware=row.hardware,
            hardware_scores=row.hardware_scores,
            node_state=NodeState.ONLINE.value if live is None else live.state.value,
            rtt_ms=None if live is None else live.rtt_ewma_ms,
            sample=None
            if self._resources is None
            else self._resources.latest(chosen.node_id),
            monotonic_now=self._clock(),
            wall_now=self._wall(),
            gpu_indices=gpus,
            owner_name=None
            if job.owner_user_id is None
            else fabric.owner_names.get(job.owner_user_id),
            policy=None if live is None or live.policy is None else live.policy,
            telemetry=start_telemetry(
                node=None if cluster is None else cluster.node(chosen.node_id),
                trees=trees,
                submitter_node_id=job.scheduling.submitter_node_id,
                node_id=chosen.node_id,
                links=fabric.links,
                controller_links=fabric.controller_links,
                clock=None if live is None else live.clock.latest,
            ),
        )
        if decision.policy == "round_robin":
            self.round_robin_after = chosen.node_id
            if fabric.snapshot is not None:
                fabric.snapshot = dataclasses.replace(
                    fabric.snapshot, round_robin_after=chosen.node_id
                )
        return Dispatch(
            attempt_id=str(new_attempt_id()),
            start_snapshot=snapshot,
            node_id=chosen.node_id,
            slot=chosen.slot,
            record=record,
        )
