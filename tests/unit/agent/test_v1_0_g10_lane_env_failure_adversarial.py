"""V1.0 G10 D443 adversarial: which failures end a lane (env phase only)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from test_v1_0_g10_lane_env_failure import (  # type: ignore[import-not-found]
    ENV_HASH,
    _attempt,
    _grant,
    _Host,
    _run,
)

from meshforge.agent.execution.runtime import RunObserver, RunResult, RunSpec
from meshforge.common.errors import FailureType
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.common.states import AttemptState

pytestmark = pytest.mark.unit


class _Rt:
    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.calls = 0

    async def run(
        self, spec: RunSpec, on_output: Any, *, observer: RunObserver
    ) -> RunResult:
        del spec, on_output
        self.calls += 1
        if self.kind == "env_error_after_start":
            # the workload started, then it died with an env_error-typed result
            observer.enter_env()
            observer.enter_run()
            observer.finish()
            return RunResult(
                exit_code=1, failure_type=FailureType.ENV_ERROR, env_hash=ENV_HASH
            )
        if self.kind == "no_env_phase":
            # a runtime that never entered the env phase (e.g. a local failure)
            return RunResult(
                exit_code=1, failure_type=FailureType.INTERNAL_ERROR, reason="x"
            )
        if self.kind == "cancelled_in_env":
            observer.enter_env()
            return observer.cancelled_result()
        raise AssertionError(self.kind)


@pytest.mark.asyncio
async def test_workload_started_then_env_error_type_keeps_chunk_path(
    tmp_path: Path,
) -> None:
    host = _Host(tmp_path, [_grant(0), _grant(1)])
    rt = _Rt("env_error_after_start")
    await _run(host, rt, _attempt(tmp_path))  # type: ignore[arg-type]
    assert rt.calls == 2
    assert len(host.chunk_results) == 2
    ((state, _),) = host.sent
    assert state is AttemptState.DONE


@pytest.mark.asyncio
async def test_failure_without_env_phase_is_a_chunk_failure(tmp_path: Path) -> None:
    host = _Host(tmp_path, [_grant(0), _grant(1)])
    rt = _Rt("no_env_phase")
    await _run(host, rt, _attempt(tmp_path))  # type: ignore[arg-type]
    assert rt.calls == 2
    assert [r.outcome for r in host.chunk_results] == [
        agent_pb2.ChunkResult.OUTCOME_FAILED
    ] * 2


@pytest.mark.asyncio
async def test_cancel_during_env_phase_is_not_an_env_failure(tmp_path: Path) -> None:
    host = _Host(tmp_path, [_grant(0), _grant(1)])
    rt = _Rt("cancelled_in_env")
    attempt = _attempt(tmp_path)
    try:
        await asyncio.wait_for(_run(host, rt, attempt), 5)  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001 - a stop/cancel path may raise; see asserts
        pass
    assert not any(
        s is AttemptState.FAILED
        and kw.get("failure")
        and kw["failure"][0] is FailureType.ENV_ERROR
        for s, kw in host.sent
    ), host.sent
