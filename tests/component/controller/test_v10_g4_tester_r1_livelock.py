"""V0.10 G4 tester r1, priority 1: deviation 6 (I6) livelock probe.

A lane whose ``done`` result names a digest it never uploaded is *released*
uncounted (chunk runs again). A buggy or hostile lane that does this forever
must not keep a job alive forever: the job has to END (FAILED) in bounded time,
or at least must not starve the other lanes.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from mf_fake_lanes import _Upload  # type: ignore[attr-defined]
from test_v10_g4_lanes import (  # type: ignore[import-not-found]
    _lanes,
    _ledger_check,
    _submit,
    _wait_job,
    http,  # noqa: F401
    runtime,  # noqa: F401
)

from meshforge.common.states import JobState
from meshforge.controller.server import ControllerRuntime

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(240)]


def _liar(monkeypatch: pytest.MonkeyPatch, only_tokens: set[str] | None = None) -> None:
    real = _Upload.put

    async def put(self: _Upload, path: str, *, content: bytes) -> httpx.Response:
        if only_tokens is None or self.token in only_tokens:
            return httpx.Response(201)  # claims success, stores nothing
        return await real(self, path, content=content)

    monkeypatch.setattr(_Upload, "put", put)


async def test_all_lanes_name_digests_they_never_uploaded_job_must_end(
    runtime: ControllerRuntime,  # noqa: F811
    http: httpx.AsyncClient,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _liar(monkeypatch)
    async with _lanes(runtime, [0.01, 0.01]) as lanes:
        job_id = await _submit(runtime, http, tmp_path, items=20, chunk=5)
        job = await _wait_job(runtime, job_id, within=60)
        assert job.state is JobState.FAILED, (job.state, job.failure_type)
        stats = runtime.dispatcher.split.stats  # type: ignore[union-attr]
        # bounded, and not a hot loop (r1 saw ~65 grants/s): 4 chunks x
        # (2 uncounted + counted) x a few nodes/attempts is far below 100
        assert stats.grants < 100, stats
        assert stats.unverified_failures >= 1, stats
        assert stats.committed == 0
        assert "chunk_unverified" in (job.failure_detail or ""), job.failure_detail
        await asyncio.sleep(2)  # nothing keeps granting after the end
        again = runtime.dispatcher.split.stats.grants  # type: ignore[union-attr]
        assert again <= stats.grants + 2


async def test_one_lying_lane_does_not_stop_honest_lanes_finishing(
    runtime: ControllerRuntime,  # noqa: F811
    http: httpx.AsyncClient,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _lanes(runtime, [0.01, 0.01, 0.01]) as lanes:
        job_id = await _submit(runtime, http, tmp_path, items=60, chunk=5)
        # make the first lane lie once its start is known
        for _ in range(200):
            starts = [
                m.start_attempt
                for m in lanes[0].agent.received
                if m.WhichOneof("payload") == "start_attempt"
            ]
            if starts:
                break
            await asyncio.sleep(0.02)
        _liar(monkeypatch, {starts[0].spec.upload_token})
        job = await _wait_job(runtime, job_id, within=90)
        assert job.state is JobState.SUCCEEDED, (job.failure_type, job.failure_detail)
        await _ledger_check(runtime, job_id, 12)
