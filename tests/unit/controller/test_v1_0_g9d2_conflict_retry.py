"""V1.0 G9-D2 (D440): a PostgreSQL deadlock / serialization failure re-runs
the whole split-store transaction (bounded, jittered, logged, counted); any
other error is not retried; the dispatcher's periodic step (the lease expiry
sweep) survives a store error and runs again on the next tick."""

from __future__ import annotations

import asyncio
import contextlib
from collections import Counter
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy.exc import DBAPIError

from meshforge.controller.jobs.dispatcher import Dispatcher, DispatcherStats
from meshforge.controller.jobs.store import JobStoreError, typed_store_call
from meshforge.controller.split import store as split_store
from meshforge.controller.split.store import ChunkStore
from meshforge.controller.store.conflicts import (
    CONFLICT_SQLSTATES,
    DEFAULT_TRIES,
    retry_conflicts,
    sqlstate_of,
)

pytestmark = pytest.mark.unit


class _DriverError(Exception):
    def __init__(self, sqlstate: str) -> None:
        super().__init__(f"sqlstate {sqlstate}")
        self.sqlstate = sqlstate


def _db_error(sqlstate: str) -> DBAPIError:
    return DBAPIError("SELECT 1", {}, _DriverError(sqlstate))


class _Op:
    """A store-like object whose ``run`` fails ``fails`` times with ``error``."""

    def __init__(self, fails: int, error: BaseException) -> None:
        self.fails = fails
        self.error = error
        self.calls = 0
        self.conflict_retries: Counter[str] = Counter()
        self.slept: list[float] = []

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)


def _decorated(op: _Op, tries: int = DEFAULT_TRIES) -> Any:
    @typed_store_call
    @retry_conflicts("test.op", tries=tries, sleep=op.sleep, jitter=lambda: 0.5)
    async def run(self: _Op) -> str:
        self.calls += 1
        if self.calls <= self.fails:
            raise self.error
        return "ok"

    return run


@pytest.mark.asyncio
@pytest.mark.parametrize("sqlstate", sorted(CONFLICT_SQLSTATES))
async def test_first_try_conflicts_second_succeeds(sqlstate: str) -> None:
    op = _Op(1, _db_error(sqlstate))
    assert await _decorated(op)(op) == "ok"
    assert op.calls == 2
    assert op.conflict_retries == Counter({"test.op": 1})
    assert op.slept == [pytest.approx(0.02)]


@pytest.mark.asyncio
async def test_exhausted_conflicts_raise_as_a_typed_store_error() -> None:
    op = _Op(99, _db_error("40P01"))
    with pytest.raises(JobStoreError) as info:
        await _decorated(op)(op)
    assert isinstance(info.value.__cause__, DBAPIError)
    assert op.calls == DEFAULT_TRIES
    assert op.conflict_retries["test.op"] == DEFAULT_TRIES - 1
    assert op.slept == sorted(op.slept)  # backoff grows
    assert all(0 < s <= 0.5 for s in op.slept)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error", [_db_error("23505"), _db_error("57014"), RuntimeError("bug")]
)
async def test_other_errors_are_never_retried(error: BaseException) -> None:
    op = _Op(1, error)
    with pytest.raises((JobStoreError, RuntimeError)):
        await _decorated(op)(op)
    assert op.calls == 1 and not op.slept and not op.conflict_retries


def test_sqlstate_is_found_through_the_cause_chain() -> None:
    assert sqlstate_of(_db_error("40001")) == "40001"
    outer = RuntimeError("wrapped")
    outer.__cause__ = _db_error("40P01")
    assert sqlstate_of(outer) == "40P01"
    assert sqlstate_of(RuntimeError("no state")) is None


def test_tries_must_be_positive() -> None:
    with pytest.raises(ValueError):
        retry_conflicts("x", tries=0)


# ---------------------------------------------------------- the chunk store


class _Session:
    def begin(self) -> contextlib.AbstractAsyncContextManager[None]:
        @contextlib.asynccontextmanager
        async def tx() -> AsyncIterator[None]:
            yield None

        return tx()


@contextlib.asynccontextmanager
async def _session() -> AsyncIterator[_Session]:
    yield _Session()


@pytest.mark.asyncio
async def test_chunk_store_expire_reruns_its_whole_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []

    async def expire_due(_db: Any, *, now: Any, limit: int) -> Any:
        calls.append(limit)
        if len(calls) == 1:
            raise _db_error("40P01")
        return type("R", (), {"expired": ()})()

    async def no_sleep(_s: float) -> None:
        return None

    monkeypatch.setattr(split_store.wq, "expire_due", expire_due)
    monkeypatch.setattr("meshforge.controller.store.conflicts.asyncio.sleep", no_sleep)
    store = ChunkStore.__new__(ChunkStore)
    store._sessions = _session  # type: ignore[assignment]
    store.conflict_retries = Counter()
    assert await store.expire(now=None) == 0  # type: ignore[arg-type]
    assert len(calls) == 2
    assert store.conflict_retries == Counter({"split.expire": 1})


@pytest.mark.parametrize("op", ["request", "result", "renew", "expire"])
def test_every_writing_chunk_store_op_is_wrapped(op: str) -> None:
    method = getattr(ChunkStore, op)
    inner = method.__wrapped__  # typed_store_call
    assert hasattr(inner, "__wrapped__"), f"{op} lacks retry_conflicts"
    assert inner.__qualname__.endswith(f"ChunkStore.{op}")


# ---------------------------------------------------------- the expiry sweep


class _Loop:
    """Just enough of the dispatcher to run its real ``run`` / ``_retrying``."""

    run = Dispatcher.run
    _retrying = Dispatcher._retrying

    def __init__(self, errors: list[BaseException | None]) -> None:
        self.errors = errors
        self.expire_calls = 0
        self._stopping = False
        self._queue: asyncio.Queue[Any] = asyncio.Queue()
        self._wake = asyncio.Event()
        self._settings = type("S", (), {"dispatch_interval_s": 0.01})()
        self.stats = DispatcherStats()
        self._loop_time = asyncio.get_running_loop().time

    @property
    def stopping(self) -> bool:
        return self._stopping

    def _clock(self) -> float:
        return self._loop_time()

    async def _wait(self, wait_s: float) -> None:
        await asyncio.sleep(wait_s)

    async def _requeue_stranded(self, _: object) -> None:
        return None

    async def _periodic(self, _: object) -> None:
        self.expire_calls += 1
        error = self.errors.pop(0) if self.errors else None
        if not self.errors:
            self._stopping = True
        if error is not None:
            raise error


@pytest.mark.asyncio
async def test_the_expiry_sweep_survives_store_errors_and_runs_again() -> None:
    loop = _Loop(
        [
            JobStoreError("jobs.expire failed: DBAPIError"),  # retried at once
            RuntimeError("an unexpected bug"),  # logged; the next tick runs
            None,
            None,
        ]
    )
    await asyncio.wait_for(loop.run(), 10)
    assert loop.expire_calls == 4
    assert loop.stats.db_retries == 1
