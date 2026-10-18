"""V0.9 G5 lane B item 3 (D219/D246): the owner guard through the real executor.

Real burner processes (``cpu_load:1``), the real condition runner and marker;
the workload runtime blocks until released. Checked:

* the owner pauses benchmarks (or becomes active) while a **benchmark**
  attempt runs with a condition: ``ConditionRunner.stop_all("owner_pause")``
  runs **before** the attempt is cancelled, the burner is gone, the attempt
  ends ``FAILED(owner_preempted)`` - and a **user** attempt running next to it
  is not touched (checklist 30);
* a benchmark ``StartAttempt`` while the guard is on is refused
  ``owner_preempted`` before anything runs; a user ``StartAttempt`` runs;
* the policy's ``pause_benchmarks`` refuses a benchmark start (agent re-check)
  as ``owner_preempted``, never a user job.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("psutil", reason="the agent extra")

from test_v09_g4_conditions_exec import (  # type: ignore[import-not-found]
    GatedRuntime,
    Reports,
    _burners,
    _executor,
    _no_burners,
    _settle,
    _start,
)

from meshforge.agent.owner_guard import OwnerGuard
from meshforge.common.bench_conditions import ConditionKind
from meshforge.common.owner_policy import OPEN_POLICY, OwnerPolicy
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.common.proto_codec import (
    attempt_state_from_proto,
    failure_type_from_proto,
)

pytestmark = [pytest.mark.component, pytest.mark.timeout(120)]


class TwoRuntimes(GatedRuntime):
    """Every run waits for ``release``; counts runs per attempt workdir."""

    def __init__(self) -> None:
        super().__init__()
        self.started = 0
        self.two = asyncio.Event()

    async def run(self, spec: Any, on_output: Any, *, observer: Any = None) -> Any:
        self.started += 1
        if self.started >= 2:
            self.two.set()
        return await super().run(spec, on_output, observer=observer)


def _final(reports: Reports, attempt_id: str) -> agent_pb2.AttemptStatus:
    mine = [s for s in reports.sent if s.attempt_id == attempt_id]
    return mine[-1]


def _state(status: agent_pb2.AttemptStatus) -> str:
    return attempt_state_from_proto(status.state).value


def _bench(msg: agent_pb2.ControllerMessage) -> agent_pb2.ControllerMessage:
    msg.start_attempt.benchmark = True
    return msg


@pytest.mark.asyncio
async def test_preempt_reverts_conditions_first_then_stops_only_the_bench_attempt(
    tmp_path: Path,
) -> None:
    runtime, reports = TwoRuntimes(), Reports()
    executor, digest = _executor(
        tmp_path, runtime, reports, kinds={ConditionKind.CPU_LOAD}
    )
    await executor.start()
    order: list[str] = []
    real_stop_all = executor.conditions.stop_all

    async def spy_stop_all(reason: str) -> int:
        bench_attempt = executor._attempts[bench.start_attempt.attempt_id]
        # stop_all comes first: the attempt's step is not cancelled yet
        order.append(f"stop_all:{reason}:{bench_attempt.state.value}")
        return await real_stop_all(reason)

    executor.conditions.stop_all = spy_stop_all  # type: ignore[method-assign]
    real_stop = executor._stop

    def spy_stop(attempt: Any, reason: str) -> None:
        order.append(f"stop:{attempt.attempt_id}:{reason}")
        real_stop(attempt, reason)

    executor._stop = spy_stop  # type: ignore[method-assign]

    bench = _bench(_start(digest, "cpu_load:1"))
    user = _start(digest, None)
    executor.handle(bench)
    executor.handle(user)
    await asyncio.wait_for(runtime.two.wait(), 30)
    assert len(_burners()) == 1

    guard = OwnerGuard(policy=None, activity=None, preempt=executor.preempt_benchmarks)
    assert await executor.preempt_benchmarks("paused") == 1
    assert order[0].startswith("stop_all:owner_pause:")
    assert order[1] == f"stop:{bench.start_attempt.attempt_id}:owner_preempted"
    assert len(order) == 2  # the user attempt was never stopped
    await _no_burners()
    for _ in range(200):
        if _state(_final(reports, bench.start_attempt.attempt_id)) == "failed":
            break
        await asyncio.sleep(0.05)
    final = _final(reports, bench.start_attempt.attempt_id)
    assert _state(final) == "failed"
    assert (
        failure_type_from_proto(final.failure.failure_type).value == "owner_preempted"
    )
    assert final.condition.reverted is True and final.condition.verified is False
    # the user job is still running, then finishes normally
    assert _state(_final(reports, user.start_attempt.attempt_id)) == "running"
    runtime.release.set()
    await _settle(executor)
    assert _state(_final(reports, user.start_attempt.attempt_id)) == "done"
    assert guard.blocked() is None
    await executor.close()


@pytest.mark.asyncio
async def test_bench_start_is_refused_while_guarded_and_a_user_start_runs(
    tmp_path: Path,
) -> None:
    runtime, reports = GatedRuntime(), Reports()
    executor, digest = _executor(tmp_path, runtime, reports, kinds=set())
    executor._owner_guard = lambda: "owner_active:idle"
    await executor.start()
    bench = _bench(_start(digest, None))
    executor.handle(bench)
    await _settle(executor)
    final = _final(reports, bench.start_attempt.attempt_id)
    assert _state(final) == "failed"
    assert (
        failure_type_from_proto(final.failure.failure_type).value == "owner_preempted"
    )
    assert runtime.calls == 0
    user = _start(digest, None)
    executor.handle(user)
    await asyncio.wait_for(runtime.running.wait(), 30)
    runtime.release.set()
    await _settle(executor)
    assert _state(_final(reports, user.start_attempt.attempt_id)) == "done"
    await executor.close()


class _Policy:
    def __init__(self, policy: OwnerPolicy) -> None:
        self.policy = policy


@pytest.mark.asyncio
async def test_policy_pause_refuses_a_bench_start_as_owner_preempted(
    tmp_path: Path,
) -> None:
    import dataclasses

    runtime, reports = GatedRuntime(), Reports()
    executor, digest = _executor(tmp_path, runtime, reports, kinds=set())
    executor._policy = _Policy(  # type: ignore[assignment]
        dataclasses.replace(OPEN_POLICY, pause_benchmarks=True)
    )
    await executor.start()
    bench = _bench(_start(digest, None))
    executor.handle(bench)
    await _settle(executor)
    final = _final(reports, bench.start_attempt.attempt_id)
    assert (
        failure_type_from_proto(final.failure.failure_type).value == "owner_preempted"
    )
    assert "policy_bench_paused" in final.failure.message
    user = _start(digest, None)
    executor.handle(user)
    await asyncio.wait_for(runtime.running.wait(), 30)
    runtime.release.set()
    await _settle(executor)
    assert _state(_final(reports, user.start_attempt.attempt_id)) == "done"
    await executor.close()
