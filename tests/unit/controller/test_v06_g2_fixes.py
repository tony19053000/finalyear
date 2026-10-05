"""V0.6 G2 fix round 1: one GPU per job (D-2) and a job that cannot be planned
is failed on its own (D-1). The database side of both (and D-3, serialised
dispatchers) is covered by the tester's component tests."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Any

from meshforge.common.errors import FailureType
from meshforge.common.ids import new_job_id, new_node_id
from meshforge.common.manifest import Manifest
from meshforge.common.states import JobState
from meshforge.controller.jobs.execution_store import Dispatch, DispatchFailure, Wait
from meshforge.controller.jobs.planning import DispatchPlanner
from meshforge.controller.jobs.queue_reads import FabricRows, NodeRow
from meshforge.controller.jobs.spec import choose_gpus
from meshforge.controller.jobs.store import JobRecord
from meshforge.controller.liveness import NodeState
from meshforge.controller.planner import RejectCode
from meshforge.controller.planner.feasibility import choose_slot, live_rejection
from meshforge.controller.planner.inputs import node_snapshot
from meshforge.controller.planner.types import PlanRequest, TreeNeed
from meshforge.controller.settings import JobApiSettings, PlannerSettings

GIB = 1024**3
CAPS = ["exec", "exec-subprocess", "exec-gpu"]
GPUS = [{"vram_bytes": str(4 * GIB)}, {"index": 1, "vram_bytes": str(8 * GIB)}]
"""Index 0 left out on purpose: protobuf JSON drops a zero field."""


def _manifest(gpu: str = "required") -> Manifest:
    return Manifest.model_validate(
        {"name": "d", "entrypoint": ["python", "m.py"], "resources": {"gpu": gpu}}
    )


def _request(gpu: str = "required", vram_gb: float | None = None) -> PlanRequest:
    return PlanRequest(
        job_id="job",
        attempt_no=1,
        gpu=gpu,  # type: ignore[arg-type]
        vram_bytes=None if vram_gb is None else int(vram_gb * GIB),
        ram_bytes=None,
        cpus=None,
        base="python-cpu",
        env_key="e",
        trees=(TreeNeed("t", 0),),
    )


def _node(busy: tuple[int, ...] = (), gpu_slots: int = 2) -> Any:
    return node_snapshot(
        "n",
        state="online",
        connected=True,
        hardware={"capabilities": CAPS, "gpus": GPUS},
        advertised_cpu_slots=1,
        advertised_gpu_slots=gpu_slots,
        used_gpu_slots=len(busy),
        live_attempts=len(busy),
        busy_gpus=busy,
    )


def test_choose_gpus_skips_gpus_live_attempts_hold() -> None:
    hardware = {"gpus": GPUS}
    assert choose_gpus(_manifest(), hardware) == (1,)  # the largest
    assert choose_gpus(_manifest(), hardware, {1}) == (0,)
    assert choose_gpus(_manifest(), hardware, {0, 1}) == ()
    assert choose_gpus(_manifest("none"), hardware) == ()


def test_a_gpu_slot_is_free_only_while_a_gpu_is() -> None:
    assert _node().free_gpu_slots == 2
    assert _node(busy=(1,)).free_gpu_slots == 1
    assert choose_slot(_request(), _node(busy=(1,))) == "gpu"
    # owner allows 3 GPU jobs on 2 GPUs: still one job per GPU
    full = _node(busy=(0, 1), gpu_slots=3)
    assert full.free_gpu_slots == 0
    assert choose_slot(_request(), full) is None
    refused = live_rejection(_request(), full, max_attempts_per_node=8, local=False)
    assert refused is not None and refused.code is RejectCode.SLOTS_FULL
    # `preferred` falls back to its CPU slot
    assert choose_slot(_request("preferred"), full) == "cpu"


def test_a_free_gpu_too_small_for_the_job_is_not_a_free_slot() -> None:
    small_free = _node(busy=(1,))  # the 8 GiB GPU is held, 4 GiB free
    assert choose_slot(_request(vram_gb=6), small_free) is None
    assert choose_slot(_request(vram_gb=4), small_free) == "gpu"


# ------------------------------------------------------------ dispatch planner


class _Manager:
    def __init__(self, node_id: str) -> None:
        self.live = {
            node_id: SimpleNamespace(
                state=NodeState.ONLINE, rtt_ewma_ms=None, session_id="s"
            )
        }

    def get(self, node_id: str) -> Any:
        return self.live.get(node_id)

    def get_lingering(self, _node_id: str) -> Any:
        return None


def _planner(node_id: str) -> DispatchPlanner:
    return DispatchPlanner(
        manager=_Manager(node_id),  # type: ignore[arg-type]
        settings=JobApiSettings(),
        planner=PlannerSettings(),
        clock=lambda: 1000.0,
        wall_clock=lambda: dt.datetime.now(dt.UTC),
        started_at=1000.0,
    )


def _job(manifest: dict[str, Any]) -> JobRecord:
    now = dt.datetime.now(dt.UTC)
    return JobRecord(
        id=str(new_job_id()),
        name="demo",
        state=JobState.QUEUED,
        manifest=manifest,
        bundle_digest="sha256:" + "a" * 64,
        placement={"mode": "auto", "node_id": None},
        idempotency_key=None,
        request_hash="h" * 64,
        attempt_epoch=0,
        failure_type=None,
        failure_detail=None,
        created_at=now,
        updated_at=now,
    )


def _fabric(node_id: str, held: set[int] | None = None) -> FabricRows:
    return FabricRows(
        nodes={
            node_id: NodeRow(
                node_id=node_id,
                state="online",
                hardware={"capabilities": CAPS, "gpus": GPUS},
                hardware_scores=None,
                cpu_slots=1,
                gpu_slots=2,
            )
        },
        used={node_id: [0, len(held or ())]},
        gpus_held={node_id: set(held or ())},
    )


GPU_JOB = {
    "name": "d",
    "entrypoint": ["python", "m.py"],
    "resources": {"gpu": "required"},
}


def test_two_gpu_jobs_in_one_tick_get_two_gpus_and_a_third_waits() -> None:
    node = str(new_node_id())
    planner, fabric = _planner(node), _fabric(node)
    got = []
    for _ in range(2):
        decision = planner.decide(_job(GPU_JOB), fabric)
        assert isinstance(decision, Dispatch) and decision.slot == "gpu"
        got.append(decision.start_snapshot["gpu_indices"])
        fabric.took(node, "gpu", decision.start_snapshot["gpu_indices"])
    assert sorted(got) == [[0], [1]]
    assert isinstance(planner.decide(_job(GPU_JOB), fabric), Wait)


def test_gpus_held_by_attempts_from_the_database_are_skipped() -> None:
    node = str(new_node_id())
    decision = _planner(node).decide(_job(GPU_JOB), _fabric(node, held={1}))
    assert isinstance(decision, Dispatch)
    assert decision.start_snapshot["gpu_indices"] == [0]


def test_a_job_whose_stored_manifest_no_longer_validates_is_failed_alone() -> None:
    node = str(new_node_id())
    planner, fabric = _planner(node), _fabric(node)
    corrupt = planner.decide(_job({"name": 5}), fabric)
    assert isinstance(corrupt, DispatchFailure)
    assert corrupt.failure_type is FailureType.INTERNAL_ERROR
    assert "ValidationError" in corrupt.detail and corrupt.record is None
    # the planner is still usable for the next job of the batch
    assert isinstance(planner.decide(_job(GPU_JOB), fabric), Dispatch)


def test_any_exception_while_planning_one_job_is_that_job_s_failure() -> None:
    node = str(new_node_id())
    planner = _planner(node)
    fabric = _fabric(node)

    class Boom(FabricRows):
        def busy_gpus(self, node_id: str) -> frozenset[int]:
            raise KeyError(node_id)

    boom = Boom(nodes=fabric.nodes)
    failed = planner.decide(_job(GPU_JOB), boom)
    assert isinstance(failed, DispatchFailure)
    assert failed.failure_type is FailureType.INTERNAL_ERROR
    assert "KeyError" in failed.detail
