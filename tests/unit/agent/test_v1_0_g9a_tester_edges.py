"""G9-A (tester) edge cases derived from the spec, not the code.

D431 settings bounds + bucket edge cases, D430 setting values, D429 real-clock
behaviour against a dead port (no progress -> transfer_error after the window;
staging_retry_s = 0 -> no retry at all) and raw-socket IP_TOS inheritance.
"""

from __future__ import annotations

import asyncio
import math
import socket
import sys
import time
from pathlib import Path

import pytest

from meshforge.agent.cas import AgentCas
from meshforge.agent.cas.fetcher import BlobSource, FetchTuning
from meshforge.agent.cas.rate import FetchRateLimiter
from meshforge.agent.execution.fetch import FetchError, stage_tree
from meshforge.agent.settings import AgentSettings
from meshforge.common.cas import CasStore
from meshforge.common.dscp import CS1_TOS, mark_listener
from meshforge.common.errors import ConfigError, FailureType

pytestmark = pytest.mark.unit


def _load(tmp_path: Path, **sections: object) -> AgentSettings:
    return AgentSettings.load(
        data_dir=tmp_path / "d",
        agent={"controller_address": "127.0.0.1:1", **sections},
    )


# ------------------------------------------------------------ D431 settings


@pytest.mark.parametrize("bad", [0.5, 0.999, -1, 100_000.1, math.inf, math.nan])
def test_max_rate_mbit_rejects_nonsense(tmp_path: Path, bad: float) -> None:
    with pytest.raises(ConfigError):
        _load(tmp_path, fetch={"max_rate_mbit": bad})


@pytest.mark.parametrize("good", [0, 1, 1.0, 250, 100_000])
def test_max_rate_mbit_accepts_off_and_range(tmp_path: Path, good: float) -> None:
    assert (
        _load(tmp_path, fetch={"max_rate_mbit": good}).agent.fetch.max_rate_mbit == good
    )


def test_defaults_are_off_and_cs1(tmp_path: Path) -> None:
    s = _load(tmp_path)
    assert s.agent.fetch.max_rate_mbit == 0
    assert s.agent.fetch.dscp_bulk == "cs1"
    assert s.agent.peer.dscp_bulk == "cs1"


@pytest.mark.parametrize("bad", ["CS1", "cs2", "", "on", True])
def test_dscp_bulk_only_cs1_or_off(tmp_path: Path, bad: object) -> None:
    with pytest.raises(ConfigError):
        _load(tmp_path, fetch={"dscp_bulk": bad})
    with pytest.raises(ConfigError):
        _load(tmp_path, peer={"dscp_bulk": bad})


@pytest.mark.parametrize("bad", [-1, 3601])
def test_staging_retry_s_bounds(tmp_path: Path, bad: float) -> None:
    with pytest.raises(ConfigError):
        _load(tmp_path, execution={"staging_retry_s": bad})


# --------------------------------------------------------- D431 bucket edges


def test_zero_and_negative_charges_cost_nothing_and_rate_must_be_positive() -> None:
    now = [0.0]
    lim = FetchRateLimiter(1000, burst_bytes=1000, clock=lambda: now[0])
    assert lim.reserve(0) == 0.0 and lim.reserve(-5) == 0.0
    assert lim.charged_bytes == 0
    for bad in (0, -1):
        with pytest.raises(ValueError):
            FetchRateLimiter(bad)


def test_one_piece_bigger_than_the_burst_waits_for_the_excess() -> None:
    now = [0.0]
    lim = FetchRateLimiter(1000, burst_bytes=1000, clock=lambda: now[0])
    assert lim.reserve(5000) == pytest.approx(4.0)  # (5000-1000)/1000


def test_a_clock_going_backwards_never_mints_tokens() -> None:
    now = [100.0]
    lim = FetchRateLimiter(1000, burst_bytes=1000, clock=lambda: now[0])
    lim.reserve(1000)
    now[0] = 50.0
    assert lim.reserve(1000) == pytest.approx(1.0)


def test_long_run_average_equals_the_cap() -> None:
    now = [0.0]
    lim = FetchRateLimiter(10_000, burst_bytes=1000, clock=lambda: now[0])
    total = 0
    for _ in range(1000):
        wait = lim.reserve(500)
        total += 500
        now[0] += wait
    assert total / now[0] == pytest.approx(10_000, rel=0.05)


@pytest.mark.asyncio
async def test_a_cancelled_waiter_keeps_its_charge_and_does_not_break_the_next() -> (
    None
):
    slept: list[float] = []

    async def sleep(s: float) -> None:
        slept.append(s)
        await asyncio.sleep(3600)

    now = [0.0]
    lim = FetchRateLimiter(1000, burst_bytes=1000, clock=lambda: now[0], sleep=sleep)
    task = asyncio.ensure_future(lim.take(3000))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert lim.charged_bytes == 3000
    assert lim.reserve(1) > 0  # the debt is still owed


# ----------------------------------------------- D429 real clock, dead port


def _dead_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = int(s.getsockname()[1])
    s.close()
    return port


async def _stage_dead(tmp_path: Path, retry_s: float) -> tuple[float, FetchError]:
    from meshforge.agent.cas.progress import StagingProgress
    from meshforge.agent.execution.stage_retry import StageRetryWindow

    cas = AgentCas(CasStore(tmp_path / "cas", max_blob_bytes=1 << 30))
    progress = StagingProgress()
    window = StageRetryWindow(retry_s, time.monotonic, progress)
    started = time.monotonic()
    last: FetchError | None = None
    while True:
        try:
            await stage_tree(
                cas,
                tree_id="sha256:" + "ab" * 32,
                sources=[
                    BlobSource(base_url=f"http://127.0.0.1:{_dead_port()}", token="t")
                ],
                dest=tmp_path / "w",
                tuning=FetchTuning(per_source=1),
                progress=progress,
                timeout_s=2,
            )
            raise AssertionError("staged from a dead port")
        except FetchError as exc:
            last = exc
        if not window.retries:
            break
        assert window.failed() is False  # a dead port never makes progress
        try:
            window.check(last)
        except FetchError as final:
            return time.monotonic() - started, final
        await asyncio.sleep(window.backoff())
    return time.monotonic() - started, last


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_no_progress_for_the_window_is_transfer_error_not_forever(
    tmp_path: Path,
) -> None:
    took, err = await _stage_dead(tmp_path, 1.0)
    assert err.failure_type is FailureType.TRANSFER_ERROR
    assert "without progress" in err.message
    assert 0.9 <= took < 10


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_staging_retry_s_zero_reports_the_first_error_immediately(
    tmp_path: Path,
) -> None:
    took, err = await _stage_dead(tmp_path, 0.0)
    assert err.failure_type in (FailureType.TRANSFER_ERROR, FailureType.TIMEOUT)
    assert took < 3


# ------------------------------------------------------ D430 raw sockets


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux only")
@pytest.mark.parametrize(("mode", "want"), [("cs1", CS1_TOS), ("off", 0)])
def test_real_accepted_socket_inherits_listener_tos(mode: str, want: int) -> None:
    assert CS1_TOS == 0x20
    with socket.socket() as srv:
        srv.bind(("127.0.0.1", 0))
        mark_listener(srv, mode, where="tester")  # type: ignore[arg-type]
        srv.listen()
        with socket.create_connection(srv.getsockname()) as cli:
            conn, _ = srv.accept()
            with conn:
                assert conn.getsockopt(socket.IPPROTO_IP, socket.IP_TOS) == want
                assert cli.getsockopt(socket.IPPROTO_IP, socket.IP_TOS) == 0
