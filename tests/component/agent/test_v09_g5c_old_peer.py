"""V0.9 G5c item 0.8 (D252.5): an old controller (no ``StartAttempt.benchmark``)
never gets ``owner_preempted`` (FailureType 24, which it could not decode).

The real executor with the owner guard **on** (owner paused benchmarks): a
StartAttempt without the flag - every attempt of a pre-G5 controller - is not
refused, ``preempt_benchmarks`` does not stop it, and it ends ``done``; no
report it gets carries failure type 24.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

pytest.importorskip("psutil", reason="the agent extra")

from test_v09_g4_conditions_exec import (  # type: ignore[import-not-found]
    GatedRuntime,
    Reports,
    _executor,
    _settle,
    _start,
)
from test_v09_g5b_owner_preempt_exec import (  # type: ignore[import-not-found]
    _final,
    _state,
)

from meshforge.common.errors import FailureType
from meshforge.common.proto_codec import failure_type_to_proto

pytestmark = [pytest.mark.component, pytest.mark.timeout(120)]


@pytest.mark.asyncio
async def test_an_unflagged_attempt_is_never_refused_nor_preempted(
    tmp_path: Path,
) -> None:
    runtime, reports = GatedRuntime(), Reports()
    executor, digest = _executor(tmp_path, runtime, reports, kinds=set())
    executor._owner_guard = lambda: "paused"  # the owner paused benchmarks
    await executor.start()
    old = _start(digest, None)  # benchmark field never set (pre-G5 controller)
    assert old.start_attempt.benchmark is False
    executor.handle(old)
    await asyncio.wait_for(runtime.running.wait(), 30)  # not refused
    assert await executor.preempt_benchmarks("paused") == 0  # not stopped
    runtime.release.set()
    await _settle(executor)
    aid = old.start_attempt.attempt_id
    assert _state(_final(reports, aid)) == "done"
    preempted = failure_type_to_proto(FailureType.OWNER_PREEMPTED)
    assert all(
        s.failure.failure_type != preempted for s in reports.sent if s.attempt_id == aid
    )
    await executor.close()
