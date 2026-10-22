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

**V0.14 G3.** The job's objective (D295) is ``placement.objective`` (POST
/v1/jobs) > the manifest's ``objective:`` > ``[controller.planner]
default_objective``; each node carries its reliability over
``risk_window_days`` (D296, read once per tick by :func:`~.queue_reads.
read_fabric`); the planner configuration carries the risk prior, the
``prefer_local`` margin, the ``link_infeasible`` floor and turns dominance
pruning on (D294). A decision slower than ``decision_latency_budget_ms`` is
logged and counted (:attr:`DispatchPlanner.decisions_over_budget`).

The planner itself stays pure; everything impure is read here, once.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from typing import Final

from pydantic import ValidationError

from meshforge.common.capabilities import EXEC_SPLIT_CAPABILITY
from meshforge.common.errors import FailureType, InvalidIdError
from meshforge.common.ids import new_attempt_id
from meshforge.common.logging import get_logger
from meshforge.common.manifest import OBJECTIVES, Manifest, ManifestError, SplitSpec
from meshforge.controller.jobs.env_health import KnownBadEnvs, env_key
from meshforge.controller.jobs.execution_store import (
    CombineDecision,
    CombineStart,
    DecisionRecord,
    Dispatch,
    DispatchDecision,
    DispatchFailure,
    DispatchLanes,
    LaneStart,
    Wait,
)
from meshforge.controller.jobs.queue_reads import (
    FabricRows,
    NodeRow,
    is_benchmark_key,
)
from meshforge.controller.jobs.spec import (
    choose_gpus,
    combine_manifest,
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
    SplitConfig,
    TreeNeed,
    exclusions,
    plan,
)

# V0.14 G2 (D293): the tick's calibration
from meshforge.controller.planner.calibration import Calibration, project_calibration
from meshforge.controller.planner.decision_json import candidates_json
from meshforge.controller.planner.inputs import node_snapshot, plan_request, split_info

# V0.15 G4 (D336): the tick's ML model (None = none)
from meshforge.controller.planner.predictor import MlInput
from meshforge.controller.planner.replay import (
    REPLAY_VERSION,
    bounded_replay,
    idle_snapshot,
    project,
)
from meshforge.controller.planner.types import (
    MlUse,
    Objective,
    Reliability,
    RiskConfig,
)
from meshforge.controller.resources import ResourceTracker
from meshforge.controller.session.manager import SessionManager
from meshforge.controller.settings import (
    JobApiSettings,
    PlannerSettings,
    SplitSettings,
)
from meshforge.workqueue.core import QueuePolicy

__all__ = ["DispatchPlanner", "decision_record", "with_decision_ms"]

log = get_logger(__name__)

_JOB_IS_BAD: Final = (ValidationError, ManifestError, InvalidIdError)
"""Errors that mean the stored job itself is unusable (reviewer N-1): only
these fail the job; any other planning error leaves it queued."""
_ERRORS_LOGGED_MAX: Final = 1024

ML_ERROR_LOG_EVERY_S: Final = 60.0
"""V0.15 G4 (D363): a failing predictor is logged at most once a minute (every
failure is counted in :attr:`DispatchPlanner.ml_predictor_errors`)."""

DEFAULT_OBJECTIVE: Final = "min_time"
"""V0.14 D295: what a decision whose request named no objective ranked by
(``plan_decision.objective``; e.g. a combine's placement)."""

_GPU_GONE: Final = frozenset(
    {RejectCode.NO_GPU, RejectCode.NO_GPU_RUNTIME, RejectCode.NO_GPU_SLOTS}
)


MAX_FORCED_SET: Final = 64
"""Most nodes a stored ``placement.node_set`` may name (a campaign set is a
handful of laptops; anything else is not a set this controller wrote)."""


def forced_set(job: JobRecord) -> tuple[str, ...]:
    """V0.10 G8 (D271): a campaign's forced ``mode3`` run's node set
    (``placement.node_set``, written only by the benchmark submitter) -
    sorted, deduplicated; () = not forced (or a malformed value: never
    trusted into a set)."""
    raw = job.placement.get("node_set")
    if not isinstance(raw, list) or not raw or len(raw) > MAX_FORCED_SET:
        return ()
    if not all(isinstance(n, str) and n for n in raw):
        return ()
    return tuple(sorted(set(raw)))


def _idle(snapshot: ClusterSnapshot) -> ClusterSnapshot:
    """``snapshot`` with no slot, GPU or attempt in use on any node (the shadow
    plan's view, D235): :func:`~meshforge.controller.planner.replay.
    idle_snapshot`, the one a shadow replay rebuilds it with."""
    return idle_snapshot(snapshot)


def _without_draining(
    snapshot: ClusterSnapshot,
    lanes: Mapping[str, tuple[int, int]],
    *,
    gpu: bool = False,
) -> ClusterSnapshot:
    """V0.10 G5: ``snapshot`` with the slots of a split job's draining lanes
    free - its combine takes their place (every chunk is committed). Only
    lanes of the combine's own slot kind are freed (fix r1 N2): a combine
    needs no GPU (``gpu=False``), and a draining GPU lane frees no CPU slot -
    it still counts in the node's live attempts until it ends."""
    if not lanes:
        return snapshot
    nodes = []
    for node in snapshot.nodes:
        cpu, gpus = lanes.get(node.node_id, (0, 0))
        if gpu and gpus:
            node = dataclasses.replace(
                node,
                used_gpu_slots=max(0, node.used_gpu_slots - gpus),
                live_attempts=max(0, node.live_attempts - gpus),
            )
        elif not gpu and cpu:
            node = dataclasses.replace(
                node,
                used_cpu_slots=max(0, node.used_cpu_slots - cpu),
                live_attempts=max(0, node.live_attempts - cpu),
            )
        nodes.append(node)
    return dataclasses.replace(snapshot, nodes=tuple(nodes))


def decision_record(decision: Decision) -> DecisionRecord:
    return DecisionRecord(
        policy=decision.policy,
        snapshot_digest=decision.snapshot_digest,
        reason_key=decision.reason_key,
        candidates=candidates_json(decision),
        chosen_node_id=None if decision.chosen is None else decision.chosen.node_id,
        objective=decision.objective or DEFAULT_OBJECTIVE,  # V0.14 G3
    )


def with_decision_ms(outcome: DispatchDecision, took_ms: float) -> DispatchDecision:
    """V0.14 G6 (D324): ``outcome`` with ``took_ms`` (ms, 3 decimals, never
    negative) on its decision row; an outcome without a row is returned
    as is (a :class:`Wait` without a record writes nothing)."""
    if outcome is None or outcome.record is None:
        return outcome
    record = dataclasses.replace(
        outcome.record, decision_ms=round(max(0.0, took_ms), 3)
    )
    return dataclasses.replace(outcome, record=record)


# --- V0.14 G3 (D295) -------------------------------------------------------------


def job_objective(job: JobRecord, manifest: Manifest, default: str) -> Objective:
    """``placement.objective`` (POST /v1/jobs, validated there) > the
    manifest's ``objective:`` > ``default``. A stored placement value that is
    not an objective (never written by this controller) is ignored."""
    asked = job.placement.get("objective")
    for value in (asked, manifest.objective, default):
        if isinstance(value, str) and value in OBJECTIVES:
            return value  # type: ignore[return-value]  # checked against OBJECTIVES
    return DEFAULT_OBJECTIVE


# --- end V0.14 G3 ------------------------------------------------------------------


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
        node_paused: Callable[[str], bool] | None = None,
        split: SplitSettings | None = None,
    ) -> None:
        self._split = split or SplitSettings()
        """V0.10 G4: ``[controller.split]`` (chunk caps, lease timeout)."""
        self._node_paused = node_paused
        """V0.9 G4 (D217): an ONLINE node paused by an ``unavailable:T``
        benchmark condition is planned as DRAINING (a live rejection: nothing
        new starts there, a pinned job waits, the shadow plan avoids it)."""
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
        self.replays_over_cap = 0
        """V0.14 D292: decisions stored without a replay because it was larger
        than ``[controller.planner] replay_max_bytes``."""
        self.replay_failures = 0
        """V0.14 D292: decisions stored without a replay because building it
        raised (a bug: logged with its traceback, never fails the dispatch)."""
        self.decisions_over_budget = 0
        """V0.14 G3 (D294): decisions slower than ``[controller.planner]
        decision_latency_budget_ms``."""
        # ------------------------------------------------ V0.14 G2 (D293)
        self.calibration: Calibration | None = None
        """The calibration this tick plans with (the dispatcher sets it once
        per tick from :class:`~meshforge.controller.calibration_store.
        CalibrationSource`); None = uncalibrated. Its version is written as
        ``plan_decision.calibration_version`` and its factors (cut to the
        job) as the replay's ``calibration`` block."""
        # ------------------------------------------------ V0.15 G4 (D336)
        self.ml: MlInput | None = None
        """The ML model this tick plans with (the dispatcher sets it once per
        tick from its ``ModelSource``); None = no model (V0.14 decisions
        exactly). Its version goes onto every candidate row and the replay."""
        self.ml_predictor_errors = 0
        """D363: decisions whose predictor failed (they fell back to
        calibration)."""
        self._ml_error_logged_at: float | None = None
        self.config = PlannerConfig(
            default_bw_bytes_s=planner.default_bw_bytes_s,
            default_compute_s=planner.default_compute_s,
            default_env_s=planner.default_env_s,
            max_attempts_per_node=settings.max_attempts_per_node,
            resource_weights=planner.resource_weights,
            link_stale_after_s=link_stale_after_s,
            min_bw_bytes_s=min_bw_bytes_s,
            split=SplitConfig(  # V0.10 G4: [controller.split] -> the planner (D275)
                split_margin=self._split.split_margin,
                target_chunk_s=self._split.target_chunk_s,
                max_overhead_frac=self._split.max_overhead_frac,
                min_chunks_per_node=self._split.min_chunks_per_node,
                max_chunks=self._split.max_chunks,
                chunk_overhead_s=self._split.chunk_overhead_s,
                tail_frac=self._split.tail_frac,
            ),
            # V0.14 G3 (D294-D296)
            risk=RiskConfig(
                prior=planner.risk_prior,
                # a lost lane's chunk comes back when its lease expires
                lease_release_s=self._split.visibility_timeout_s,
            ),
            prefer_local_margin=planner.prefer_local_margin,
            min_useful_bw_bytes_s=planner.min_useful_bw_bytes_s,
            min_useful_bytes=planner.min_useful_bytes,
            prune_dominated=True,
        )

    # ------------------------------------------------------------ snapshot

    def _node(self, row: NodeRow, fabric: FabricRows) -> NodeSnapshot:
        live = self._manager.get(row.node_id)
        state = row.state
        if live is not None:
            state = live.state.value
        elif self._manager.get_lingering(row.node_id) is not None:
            state = NodeState.SUSPECT.value
        if (
            state == NodeState.ONLINE.value
            and self._node_paused is not None
            and self._node_paused(row.node_id)
        ):
            state = NodeState.DRAINING.value
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
        built = node_snapshot(
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
            owner_active=bool(getattr(live, "owner_active", False)),
        )
        return dataclasses.replace(built, reliability=self._reliability(row, fabric))

    def _reliability(self, row: NodeRow, fabric: FabricRows) -> Reliability | None:
        """V0.14 G3 (D296): the node's ended attempts over the risk window
        (the tick's one query); None = risk off (``risk_window_days`` 0)."""
        window = self._planner.risk_window_days
        if window <= 0.0:
            return None
        failures, total = fabric.reliability.get(row.node_id, (0, 0))
        return Reliability(failures=failures, total=total, window_days=window)

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
        started = time.perf_counter()
        outcome = self._decide_isolated(job, fabric)
        # V0.14 G6 (D324): the wall time goes onto the row the store writes
        return self._timed(job, outcome, (time.perf_counter() - started) * 1000.0)

    def _decide_isolated(self, job: JobRecord, fabric: FabricRows) -> DispatchDecision:
        """:meth:`decide` without the timing (its docstring)."""
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

    def _timed(
        self, job: JobRecord, outcome: DispatchDecision, took_ms: float
    ) -> DispatchDecision:
        """V0.14 G3 (D294, criterion 3): count a decision over the budget, and
        log it once per job. V0.14 G6 (D324): the outcome's decision row (if
        it carries one) gets ``decision_ms`` - every outcome that writes a
        row, whole job or split."""
        budget = self._planner.decision_latency_budget_ms
        if took_ms > budget:
            self.decisions_over_budget += 1
            if self._first_time((job.id, "over_budget")):
                log.warning(
                    "controller.jobs.decision_over_budget",
                    job_id=job.id,
                    decision_ms=round(took_ms, 3),
                    budget_ms=budget,
                )
        return with_decision_ms(outcome, took_ms)

    def _decide(self, job: JobRecord, fabric: FabricRows) -> DispatchDecision:
        manifest = Manifest.model_validate(dict(job.manifest))
        if manifest.split is not None or job.split is not None:
            # V0.10 G4 (D259): a split job runs as lanes - never whole.
            return self._decide_split(job, manifest, fabric)
        pinned = str(job.placement.get("node_id") or "") or None
        if pinned is not None:
            early = self._pinned_gone(job, pinned, fabric)
            if early is not None:
                return early
        trees, plan_job, snapshot, past = self._request(job, manifest, fabric, pinned)
        now = self._wall()
        view = project(plan_job, snapshot)  # V0.14 G1: what the replay stores
        calibration = self._job_calibration(plan_job, view)  # V0.14 G2
        decision = plan(
            plan_job,
            view,
            self.policy_of(job),
            now,
            calibration=calibration,
            ml=self.ml,  # V0.15 G4 (D336)
        )
        self._note_ml(job, decision.ml)  # V0.15 G4 (D363)
        record = self._calibrated(decision_record(decision), calibration)
        shadow: PlanRequest | None = None
        if (
            decision.chosen is not None
            and pinned is not None
            and is_benchmark_key(job.idempotency_key)
        ):
            # D215 + G4 item 0.8 (D239.4): only a campaign's forced run needs the
            # would-have-chosen label; a user's pinned job skips the extra plan
            record, shadow = self._with_shadow(
                record, plan_job, view, job, past, now, calibration=calibration
            )
        outcome = self._whole_outcome(
            job, manifest, decision, fabric, record, trees, snapshot, pinned
        )
        # V0.14 G3 (G1 review N1): the replay only for a row that is written
        return self._attach_replay(
            outcome,
            job,
            fabric,
            plan_job,
            view,
            now,
            shadow,
            calibration=calibration,  # V0.14 G2
            ml=decision.ml,  # V0.15 G4 (D364)
        )

    def _whole_outcome(
        self,
        job: JobRecord,
        manifest: Manifest,
        decision: Decision,
        fabric: FabricRows,
        record: DecisionRecord,
        trees: tuple[TreeNeed, ...],
        snapshot: ClusterSnapshot,
        pinned: str | None,
    ) -> DispatchDecision:
        """A whole job's decision as the store's answer (without the replay)."""
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

    def _request(
        self,
        job: JobRecord,
        manifest: Manifest,
        fabric: FabricRows,
        pinned: str | None,
    ) -> tuple[
        tuple[TreeNeed, ...], PlanRequest, ClusterSnapshot, Sequence[PastFailure]
    ]:
        """The job's planner request and the tick's snapshot for it (one place
        for Mode 1 and split jobs; V0.10 G4 extracted it from ``_decide``)."""
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
            # V0.9 G5 (D246): a bench job waits while the owner guard is on
            benchmark=is_benchmark_key(job.idempotency_key),
            # V0.10 G3/G4 (D265/D267e): a split job's items - the remainder
            # once its chunk queue exists (a re-plan runs only what is left)
            split=None
            if job.split is None
            else split_info(
                manifest,
                item_count=fabric.split_left.get(job.id, job.split.item_count),
                item_bytes=job.split.item_bytes,
                project_tree=job.tree_id,
                input_trees=fabric.input_mounts.get(job.id, {}),
            ),
            # V0.10 G8 (D271): a campaign's forced mode3 run
            node_set=forced_set(job),
        )
        # V0.14 G3 (D295): the objective the job ranks by
        plan_job = dataclasses.replace(
            plan_job,
            objective=job_objective(job, manifest, self._planner.default_objective),
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
        return trees, plan_job, snapshot, past

    def _decide_split(
        self, job: JobRecord, manifest: Manifest, fabric: FabricRows
    ) -> DispatchDecision:
        """V0.10 G4 (D259): the planner's Mode 3 plan (``Decision.split``, G3)
        becomes one lane per node of its set - each with its own slot, GPUs
        and start snapshot - over the job's chunk queue. "Don't split" is one
        lane on the same path (D269). A node whose agent cannot run a lane is
        rejected by the planner (``no_split_support``, static)."""
        spec = manifest.split
        if spec is None or job.split is None or not job.tree_id:
            # the job row and its manifest disagree: a stored-data fault
            return DispatchFailure(
                FailureType.INTERNAL_ERROR,
                "the stored split job has no resolved items; submit it again",
            )
        pinned = str(job.placement.get("node_id") or "") or None
        forced = forced_set(job)
        for node_id in [*([pinned] if pinned is not None else []), *forced]:
            early = self._pinned_gone(job, node_id, fabric)
            if early is not None:
                return early
        trees, plan_job, snapshot, past = self._request(job, manifest, fabric, pinned)
        now = self._wall()
        view = project(plan_job, snapshot)  # V0.14 G1: what the replay stores
        calibration = self._job_calibration(plan_job, view)  # V0.14 G2
        decision = plan(
            plan_job,
            view,
            self.policy_of(job),
            now,
            calibration=calibration,
            ml=self.ml,  # V0.15 G4 (D336)
        )
        self._note_ml(job, decision.ml)  # V0.15 G4 (D363)
        record = self._calibrated(decision_record(decision), calibration)
        shadow: PlanRequest | None = None
        if (
            decision.chosen is not None
            and (pinned is not None or forced)
            and is_benchmark_key(job.idempotency_key)
        ):
            # D215 extended (V0.10 G8): the would-have-chosen plan of a
            # campaign's forced split run - a node, or a Mode 3 set
            record, shadow = self._with_shadow(
                record, plan_job, view, job, past, now, calibration=calibration
            )
        outcome = self._split_outcome(
            job,
            manifest,
            spec,
            decision,
            fabric,
            record,
            trees,
            snapshot,
            pinned,
            forced,
        )
        # V0.14 G3 (G1 review N1): the replay only for a row that is written
        return self._attach_replay(
            outcome,
            job,
            fabric,
            plan_job,
            view,
            now,
            shadow,
            calibration=calibration,  # V0.14 G2
            ml=decision.ml,  # V0.15 G4 (D364)
        )

    def _split_outcome(
        self,
        job: JobRecord,
        manifest: Manifest,
        spec: SplitSpec,
        decision: Decision,
        fabric: FabricRows,
        record: DecisionRecord,
        trees: tuple[TreeNeed, ...],
        snapshot: ClusterSnapshot,
        pinned: str | None,
        forced: tuple[str, ...],
    ) -> DispatchDecision:
        """A split job's decision as the store's answer (without the replay)."""
        lanes = decision.split if decision.chosen is not None else None
        if lanes is None or not lanes.lanes:
            if forced:
                return self._forced_refused(decision, record)
            if pinned is not None:
                return self._pinned_refused(decision, record)
            if decision.all_static_rejections:
                return DispatchFailure(
                    FailureType.NO_FEASIBLE_PLAN,
                    "no node of the fabric can run this job (see compute explain)",
                    record,
                )
            return Wait(record)
        starts: list[LaneStart] = []
        for lane in lanes.lanes:
            started = self._start(
                job, manifest, lane.node_id, lane.slot, fabric, trees, snapshot
            )
            if started is not None:
                starts.append(started)
        if not starts:  # pragma: no cover - the planner checked the GPUs
            return Wait(record)
        log.info(
            "controller.split.lanes_planned",
            job_id=job.id,
            lanes=len(starts),
            chunk_items=lanes.chunk_items,
            chunks=lanes.chunks,
            split=lanes.split,
            predicted_s=round(lanes.predicted_makespan_s, 3),
        )
        return DispatchLanes(
            lanes=tuple(starts),
            chunk_items=lanes.chunk_items,
            queue_policy=self.queue_policy(job, spec),
            record=dataclasses.replace(record, chosen_node_id=starts[0].node_id),
        )

    # ------------------------------------------------------------ combine

    def decide_combine(self, job: JobRecord, fabric: FabricRows) -> CombineDecision:
        """V0.10 G5 (D268/D283 pin 2): where a split job's combine runs, or
        why it waits. Errors are ours, not the job's: it waits (counted and
        logged once, like :meth:`decide`)."""
        try:
            return self._decide_combine(job, fabric)
        except Exception as exc:  # our fault: keep it waiting
            self.planning_errors += 1
            key = (job.id, f"combine:{type(exc).__name__}")
            if key not in self._errors_logged:
                self._errors_logged[key] = None
                while len(self._errors_logged) > _ERRORS_LOGGED_MAX:
                    self._errors_logged.popitem(last=False)
                log.exception(
                    "controller.split.combine_planning_error",
                    job_id=job.id,
                    error_type=type(exc).__name__,
                )
            return Wait()

    def _decide_combine(self, job: JobRecord, fabric: FabricRows) -> CombineDecision:
        """Feasible = the planner's own rules (liveness, owner policy, slots,
        runtime) for the job **without its GPU need**
        (:func:`~.spec.combine_manifest`) on nodes that speak Mode 3
        (``exec-split``); then the submitter's node, else the node holding
        the most committed chunk results, else the planner's best. A failed
        combine's node is avoided (softly, like a retry). Nothing feasible:
        the job waits; no node that could ever run it: it fails
        ``no_feasible_plan``."""
        manifest = combine_manifest(Manifest.model_validate(dict(job.manifest)))
        pinned = str(job.placement.get("node_id") or "") or None
        script = (
            manifest.split is not None and manifest.split.combine.script is not None
        )
        key = env_key(
            job.manifest, tree_id=job.tree_id, bundle_digest=job.bundle_digest
        )
        # a built-in reads only the parts; a script needs the project + env
        trees = self._trees(job, fabric) if script else ()
        request = plan_request(
            manifest,
            job_id=job.id,
            attempt_no=job.attempt_epoch + 1,
            env_key=key,
            trees=trees,
            pinned_node_id=pinned,
            submitter_node_id=job.scheduling.submitter_node_id,
            seeded=job.scheduling.submitter_seeded,
            owner_name=None
            if job.owner_user_id is None
            else fabric.owner_names.get(job.owner_user_id),
            benchmark=is_benchmark_key(job.idempotency_key),
        )
        cluster = _without_draining(
            self.snapshot(fabric), fabric.combine_lanes.get(job.id, {})
        )
        if script:
            cluster = self._for_job(cluster, key)
        past = fabric.combine_failures.get(job.id, ())
        if past and pinned is None:
            request = dataclasses.replace(
                request,
                excluded=exclusions(
                    past,
                    cluster.nodes,
                    now=self._wall(),
                    exclude_s=self._planner.retry_exclude_s,
                ),
            )
        decision = plan(request, cluster, self.policy_of(job), self._wall())

        def speaks_mode3(node_id: str) -> bool:
            row = fabric.nodes.get(node_id)
            caps = () if row is None else row.hardware.get("capabilities") or ()
            return EXEC_SPLIT_CAPABILITY in caps

        able = [c for c in decision.candidates if speaks_mode3(c.node_id)]
        feasible = sorted(
            (c for c in able if c.feasible and c.slot is not None),
            key=lambda c: (c.rank if c.rank is not None else 1 << 30, c.node_id),
        )
        if not feasible:
            if all(c.rejection is not None and c.rejection.static for c in able):
                return DispatchFailure(
                    FailureType.NO_FEASIBLE_PLAN,
                    "no node of the fabric can run this split job's combine "
                    "(see compute explain)",
                )
            return Wait()
        sites = fabric.combine_sites.get(job.id, {})
        submitter = job.scheduling.submitter_node_id
        by_id = {c.node_id: c for c in feasible}
        if submitter is not None and submitter in by_id:
            chosen, reason = by_id[submitter], "submitter"
        else:
            chosen = min(feasible, key=lambda c: -sites.get(c.node_id, 0))
            reason = "most_results" if sites.get(chosen.node_id) else "planner"
        row = fabric.nodes[chosen.node_id]
        return CombineStart(
            attempt_id=str(new_attempt_id()),
            node_id=chosen.node_id,
            slot=chosen.slot or "cpu",
            start_snapshot=self._start_snapshot(
                job, chosen.node_id, row, (), fabric, trees, cluster
            ),
            reason=reason,
        )

    def queue_policy(self, job: JobRecord, spec: SplitSpec) -> QueuePolicy:
        """D267: a chunk is tried at most ``retry.max_attempts`` times and fails
        the job once it failed on ``max_failed_nodes`` different nodes; leases
        expire after ``visibility_timeout_s`` unless renewed."""
        return QueuePolicy(
            visibility_timeout_s=self._split.visibility_timeout_s,
            max_attempts=max(1, job.scheduling.max_attempts),
            max_failed_sites=self._split.max_failed_nodes,
            count_expiry=False,
            speculative_max_frac=(
                self._split.speculative_max_frac if spec.speculative else 0.0
            ),
        )

    def _start(
        self,
        job: JobRecord,
        manifest: Manifest,
        node_id: str,
        slot: str,
        fabric: FabricRows,
        trees: tuple[TreeNeed, ...],
        cluster: ClusterSnapshot,
    ) -> LaneStart | None:
        """One lane's attempt id, slot and start snapshot (like
        :meth:`_dispatch` for a Mode 1 attempt)."""
        row = fabric.nodes.get(node_id)
        if row is None:
            return None
        gpus = (
            choose_gpus(manifest, row.hardware, fabric.busy_gpus(node_id))
            if slot == "gpu"
            else ()
        )
        if slot == "gpu" and not gpus:
            return None
        return LaneStart(
            attempt_id=str(new_attempt_id()),
            node_id=node_id,
            slot=slot,
            start_snapshot=self._start_snapshot(
                job, node_id, row, gpus, fabric, trees, cluster
            ),
        )

    def _start_snapshot(
        self,
        job: JobRecord,
        node_id: str,
        row: NodeRow,
        gpus: tuple[int, ...],
        fabric: FabricRows,
        trees: tuple[TreeNeed, ...],
        cluster: ClusterSnapshot | None,
    ) -> dict[str, object]:
        live = self._manager.get(node_id)
        return dict(
            start_snapshot(
                hardware=row.hardware,
                hardware_scores=row.hardware_scores,
                node_state=NodeState.ONLINE.value if live is None else live.state.value,
                rtt_ms=None if live is None else live.rtt_ewma_ms,
                sample=None
                if self._resources is None
                else self._resources.latest(node_id),
                monotonic_now=self._clock(),
                wall_now=self._wall(),
                gpu_indices=gpus,
                owner_name=None
                if job.owner_user_id is None
                else fabric.owner_names.get(job.owner_user_id),
                policy=None if live is None or live.policy is None else live.policy,
                telemetry=start_telemetry(
                    node=None if cluster is None else cluster.node(node_id),
                    trees=trees,
                    submitter_node_id=job.scheduling.submitter_node_id,
                    node_id=node_id,
                    links=fabric.links,
                    controller_links=fabric.controller_links,
                    clock=None if live is None else live.clock.latest,
                ),
            )
        )

    def _with_shadow(
        self,
        record: DecisionRecord,
        plan_job: PlanRequest,
        snapshot: ClusterSnapshot,
        job: JobRecord,
        past: Sequence[PastFailure],
        now: dt.datetime,
        *,
        calibration: Calibration | None = None,
    ) -> tuple[DecisionRecord, PlanRequest | None]:
        """V0.9 G3 (D215, fix r1 D235): the node the planner **prefers** for
        this pinned **benchmark** job (G4 item 0.8: a user's pinned job has
        no shadow - its record's ``planner_node_id`` is null) - the same
        request unpinned, on the same snapshot with **slot occupancy
        ignored**: every slot (and GPU) of a node is treated as free, so a
        node that is busy right now still wins if it is the better place.
        Everything else is kept: feasibility (capabilities, hardware, owner
        policy, a slot *kind* the node has at all), health (state, live
        session) and the job's retry exclusions (D110, with the soft lift rule
        of ``plan``). So ``planner_node_id`` is a *preference*
        label, not "what would have run this second" (docs/TELEMETRY.md).

        Pure (no reservation, no round-robin pointer moved, no row of its own);
        it only adds the node and its estimate to the dispatched decision. A
        failure here never blocks the pinned dispatch. V0.14 G1 (D292): it
        plans at the dispatch's ``now`` and also returns its request, which
        the row's replay stores (``replay.shadow_from_replay`` re-runs it);
        None = it failed."""
        try:
            request = dataclasses.replace(
                plan_job,
                pinned_node_id=None,
                node_set=(),  # V0.10 G8: a forced set is not a preference
                # the owner guard is a moment, like slot occupancy: the
                # preference label ignores it (V0.9 G5)
                benchmark=False,
                excluded=exclusions(
                    past,
                    snapshot.nodes,
                    now=now,
                    exclude_s=self._planner.retry_exclude_s,
                )
                if past
                else {},
            )
            shadow = plan(
                request,
                _idle(snapshot),
                self.policy_of(job),
                now,
                calibration=calibration,  # V0.14 G2: as the decision itself
                ml=self.ml,  # V0.15 G4: as the decision itself
            )
        except Exception as exc:  # the shadow is telemetry, never a condition
            log.exception(
                "controller.jobs.shadow_plan_failed",
                job_id=job.id,
                error_type=type(exc).__name__,
            )
            return record, None
        chosen = shadow.chosen
        if chosen is None:
            return record, request
        split = shadow.split
        return (
            dataclasses.replace(
                record,
                shadow_chosen_node_id=chosen.node_id,
                shadow_estimate_s=None
                if chosen.estimate is None
                else round(chosen.estimate.total_s, 6),
                # V0.10 G8: a split job's preference may be a Mode 3 set
                shadow_node_set=split.node_ids
                if split is not None and split.split
                else (),
            ),
            request,
        )

    def _attach_replay(
        self,
        outcome: DispatchDecision,
        job: JobRecord,
        fabric: FabricRows,
        plan_job: PlanRequest,
        snapshot: ClusterSnapshot,
        now: dt.datetime,
        shadow: PlanRequest | None,
        *,
        calibration: Calibration | None = None,
        ml: MlUse | None = None,
    ) -> DispatchDecision:
        """V0.14 G3 (G1 review N1): ``outcome`` with its record's replay - built
        only when the store will write the row: always for a dispatch or a
        failure, for a :class:`Wait` only when its reason key differs from
        the job's latest decision (D104; the store's own rule). A waiting job
        re-planned every tick with the same reasons builds no replay."""
        record = None if outcome is None else outcome.record
        if outcome is None or record is None:
            return outcome
        if (
            isinstance(outcome, Wait)
            and fabric.last_reason.get(job.id) == record.reason_key
        ):
            return outcome
        with_replay = self._with_replay(
            record,
            job,
            plan_job,
            snapshot,
            now,
            shadow,
            calibration=calibration,
            ml=ml,
        )
        return dataclasses.replace(outcome, record=with_replay)

    def _with_replay(
        self,
        record: DecisionRecord,
        job: JobRecord,
        plan_job: PlanRequest,
        snapshot: ClusterSnapshot,
        now: dt.datetime,
        shadow: PlanRequest | None,
        *,
        calibration: Calibration | None = None,
        ml: MlUse | None = None,
    ) -> DecisionRecord:
        """V0.14 G1 (D292): the record with its replay snapshot - or without
        one, counted and logged, when it is over ``replay_max_bytes`` or could
        not be built. The replay explains the decision; it is never a
        condition of the dispatch."""
        try:
            replay, size = bounded_replay(
                plan_job,
                snapshot,
                now,
                max_bytes=self._planner.replay_max_bytes,
                shadow=shadow,
                calibration=calibration,  # V0.14 G2 (D307): already cut to the job
                ml=ml,  # V0.15 G4 (D364): the model the decision used
            )
        except Exception as exc:  # a bug in the codec: keep dispatching
            self.replay_failures += 1
            log.exception(
                "controller.jobs.replay_failed",
                job_id=job.id,
                error_type=type(exc).__name__,
            )
            return record
        if replay is None:
            self.replays_over_cap += 1
            # V0.14 G3 (G1 review N1): logged once per (job, reason key)
            if self._first_time((job.id, f"over_cap:{record.reason_key}")):
                log.warning(
                    "controller.jobs.replay_over_cap",
                    job_id=job.id,
                    replay_bytes=size,
                    replay_max_bytes=self._planner.replay_max_bytes,
                )
            return record
        return dataclasses.replace(record, replay=replay, replay_version=REPLAY_VERSION)

    def _first_time(self, key: tuple[str, str]) -> bool:
        """True the first time ``key`` is seen (bounded memory, shared with the
        planning-error log)."""
        if key in self._errors_logged:
            return False
        self._errors_logged[key] = None
        while len(self._errors_logged) > _ERRORS_LOGGED_MAX:
            self._errors_logged.popitem(last=False)
        return True

    def _note_ml(self, job: JobRecord, ml: MlUse | None) -> None:
        """V0.15 G4 (D363): a decision whose predictor failed is counted, and
        logged at most once per :data:`ML_ERROR_LOG_EVERY_S` (a broken model
        would otherwise log on every decision of every tick)."""
        if ml is None or ml.error is None:
            return
        self.ml_predictor_errors += 1
        now = self._clock()
        last = self._ml_error_logged_at
        if last is not None and now - last < ML_ERROR_LOG_EVERY_S:
            return
        self._ml_error_logged_at = now
        log.warning(
            "controller.jobs.ml_predictor_error",
            job_id=job.id,
            ml_model_version=ml.version,
            error_type=ml.error,
            errors_total=self.ml_predictor_errors,
        )

    def _job_calibration(
        self, plan_job: PlanRequest, view: ClusterSnapshot
    ) -> Calibration | None:
        """V0.14 G2 (D293/D307): the tick's calibration cut to what this job
        can look up - the job is planned **and** replayed with exactly this,
        so the stored digest and the replay agree (like the snapshot's
        projection, G1)."""
        if self.calibration is None:
            return None
        return project_calibration(self.calibration, plan_job, view)

    @staticmethod
    def _calibrated(
        record: DecisionRecord, calibration: Calibration | None
    ) -> DecisionRecord:
        """V0.14 G2 (D293): the record names the calibration it was planned
        with (``plan_decision.calibration_version``; None = uncalibrated)."""
        if calibration is None:
            return record
        return dataclasses.replace(record, calibration_version=calibration.version)

    @staticmethod
    def _forced_refused(decision: Decision, record: DecisionRecord) -> DispatchDecision:
        """V0.10 G8: a forced mode3 run whose set cannot run now. A node of
        the set refused for good (static) fails it - the campaign skips or
        retries the run; otherwise it waits like a pinned job."""
        refused = [
            c
            for c in decision.candidates
            if c.mode != "split" and c.rejection is not None and c.rejection.static
        ]
        if refused:
            first = refused[0]
            assert first.rejection is not None  # noqa: S101 - filtered above
            return DispatchFailure(
                FailureType.NO_FEASIBLE_PLAN,
                f"node_rejected_{first.rejection.code.value}",
                record,
            )
        return Wait(record)

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
        gpus = (
            choose_gpus(manifest, row.hardware, fabric.busy_gpus(chosen.node_id))
            if chosen.slot == "gpu"
            else ()
        )
        if chosen.slot == "gpu" and not gpus:  # pragma: no cover - planner checked
            return Wait(record)
        snapshot = self._start_snapshot(
            job, chosen.node_id, row, gpus, fabric, trees, cluster
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
