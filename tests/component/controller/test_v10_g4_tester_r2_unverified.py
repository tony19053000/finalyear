"""V0.10 G4 tester r2: attack the max_unverified_results fix.

Lanes "lie" by acknowledging an upload (HTTP 201) that stores nothing, then
naming that digest in ChunkResult(done). Every test asserts the job ENDS and
that the controller does not hot-loop.
"""

from __future__ import annotations

import asyncio
import collections
import time
from collections.abc import Callable
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


def _lie(
    monkeypatch: pytest.MonkeyPatch,
    should: Callable[[str, str, int], bool],
) -> collections.Counter[tuple[str, str]]:
    """``should(token, digest_hex, n_th_time)`` -> drop this upload."""
    real = _Upload.put
    seen: collections.Counter[tuple[str, str]] = collections.Counter()

    async def put(self: _Upload, path: str, *, content: bytes) -> httpx.Response:
        key = (self.token, path.rsplit("/", 1)[-1])
        seen[key] += 1
        if should(*key, seen[key]):
            return httpx.Response(201)
        return await real(self, path, content=content)

    monkeypatch.setattr(_Upload, "put", put)
    return seen


async def test_lane_lying_twice_per_chunk_across_many_chunks_still_terminates(
    runtime: ControllerRuntime,  # noqa: F811
    http: httpx.AsyncClient,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(3) no single (lane, chunk) reaches the counted limit (2 lies, then
    honest): the job must still finish, exactly once per chunk, bounded."""
    _lie(monkeypatch, lambda _t, _d, n: n <= 2)  # every lane, every chunk
    async with _lanes(runtime, [0.0, 0.0, 0.0]):
        job_id = await _submit(runtime, http, tmp_path, items=100, chunk=5)
        t0 = time.monotonic()
        job = await _wait_job(runtime, job_id, within=120)
        assert job.state is JobState.SUCCEEDED, (job.failure_type, job.failure_detail)
        await _ledger_check(runtime, job_id, 20)
        stats = runtime.dispatcher.split.stats  # type: ignore[union-attr]
        assert stats.grants <= 20 * 3 * 2 + 20, stats  # <= 3 runs per chunk + slack
        assert stats.committed == 20
        assert time.monotonic() - t0 < 100


async def test_lane_lying_on_every_chunk_forever_single_lane_ends_failed(
    runtime: ControllerRuntime,  # noqa: F811
    http: httpx.AsyncClient,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(3)/(6) one lane left, always lying, many chunks: the cool-down must not
    starve it into a silent hang, and the count must end the job."""
    _lie(monkeypatch, lambda *_: True)
    async with _lanes(runtime, [0.0]):
        job_id = await _submit(runtime, http, tmp_path, items=60, chunk=5)
        job = await _wait_job(runtime, job_id, within=90)
        assert job.state is JobState.FAILED, (job.state, job.failure_type)
        stats = runtime.dispatcher.split.stats  # type: ignore[union-attr]
        assert stats.grants < 200, stats
        assert stats.committed == 0


async def test_last_chunk_cooldown_never_starves_the_only_lane(
    runtime: ControllerRuntime,  # noqa: F811
    http: httpx.AsyncClient,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(6) one lane, every chunk lies once then is honest: the last chunk is
    avoided (cool-down) and must be offered again after wait_retry_s."""
    _lie(monkeypatch, lambda _t, _d, n: n <= 1)
    async with _lanes(runtime, [0.0]):
        job_id = await _submit(runtime, http, tmp_path, items=15, chunk=5)
        job = await _wait_job(runtime, job_id, within=60)
        assert job.state is JobState.SUCCEEDED, (job.failure_type, job.failure_detail)
        await _ledger_check(runtime, job_id, 3)
        assert runtime.dispatcher.split.stats.no_work >= 1  # type: ignore[union-attr]


async def test_alternating_honest_and_lying_results_on_a_mixed_fabric(
    runtime: ControllerRuntime,  # noqa: F811
    http: httpx.AsyncClient,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(2) every lane lies on odd chunks always, is honest on even ones: the
    odd chunks fail for good (counted) -> bounded FAILED, never a loop."""
    from mf_fake_lanes import default_output

    from meshforge.common.hashing import sha256_bytes

    odd = {
        sha256_bytes(default_output(list(range(f, f + 5)))).hex
        for f in range(0, 60, 10)
    }
    _lie(monkeypatch, lambda _t, d, _n: d in odd)
    async with _lanes(runtime, [0.0, 0.0]):
        job_id = await _submit(runtime, http, tmp_path, items=60, chunk=5)
        job = await _wait_job(runtime, job_id, within=90)
        assert job.state is JobState.FAILED, (job.state, job.failure_type)
        assert "chunk_unverified" in (job.failure_detail or "")
        assert runtime.dispatcher.split.stats.grants < 200  # type: ignore[union-attr]


async def test_one_liar_lane_alternating_with_honest_lanes_job_succeeds(
    runtime: ControllerRuntime,  # noqa: F811
    http: httpx.AsyncClient,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(2) one lane lies on every other result, others honest."""
    async with _lanes(runtime, [0.005, 0.005, 0.005]) as lanes:
        job_id = await _submit(runtime, http, tmp_path, items=100, chunk=5)
        for _ in range(300):
            starts = [
                m.start_attempt
                for m in lanes[0].agent.received
                if m.WhichOneof("payload") == "start_attempt"
            ]
            if starts:
                break
            await asyncio.sleep(0.02)
        tok = starts[0].spec.upload_token
        calls = [0]

        def should(t: str, _d: str, _n: int) -> bool:
            if t != tok:
                return False
            calls[0] += 1
            return calls[0] % 2 == 0

        _lie(monkeypatch, should)
        job = await _wait_job(runtime, job_id, within=120)
        assert job.state is JobState.SUCCEEDED, (job.failure_type, job.failure_detail)
        await _ledger_check(runtime, job_id, 20)


async def test_counters_reset_by_controller_restart_stay_bounded_by_restarts(
    runtime: ControllerRuntime,  # noqa: F811
    http: httpx.AsyncClient,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(4) emulate a controller restart (in-memory counters lost) after each of
    the first 3 unverified results; then none: the job still ends FAILED."""
    _lie(monkeypatch, lambda *_: True)
    svc = None
    async with _lanes(runtime, [0.0, 0.0]):
        svc = runtime.dispatcher.split  # type: ignore[union-attr]
        job_id = await _submit(runtime, http, tmp_path, items=10, chunk=5)
        resets = 0
        deadline = asyncio.get_running_loop().time() + 90
        while asyncio.get_running_loop().time() < deadline:
            job = await runtime.jobs.get_job(job_id)  # type: ignore[union-attr]
            assert job is not None
            if job.state in (JobState.FAILED, JobState.SUCCEEDED, JobState.CANCELLED):
                break
            if resets < 3 and svc.stats.not_uploaded > resets:
                svc._unverified.clear()
                svc._cooldown.clear()
                resets += 1
            await asyncio.sleep(0.01)
        assert job.state is JobState.FAILED, (job.state, resets)
        assert svc.stats.grants < 200


@pytest.mark.xfail(
    strict=True,
    reason="D2 (minor): the LRU cap forgets counters of live chunks once more "
    "than _MAX_TRACKED (lane, chunk) pairs cycle (needs > 100k at the real cap)",
)
async def test_counter_cap_eviction_must_not_reset_a_live_job(
    runtime: ControllerRuntime,  # noqa: F811
    http: httpx.AsyncClient,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(5) the cap is patched to 2 to show the mechanism: a lane cycling over
    more chunks than the cap never accumulates a count and loops."""
    import meshforge.controller.split.service as svc_mod

    monkeypatch.setattr(svc_mod, "_MAX_TRACKED", 2)
    _lie(monkeypatch, lambda *_: True)
    async with _lanes(runtime, [0.0]):
        job_id = await _submit(runtime, http, tmp_path, items=40, chunk=5)
        job = await _wait_job(runtime, job_id, within=40)
        assert job.state is JobState.FAILED
