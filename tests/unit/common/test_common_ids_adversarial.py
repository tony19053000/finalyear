"""Adversarial tests for task 0.1.6 IDs: multiprocessing uniqueness, concurrency, malformed input."""

from __future__ import annotations

import multiprocessing as mp
import os
import threading

import pytest

from meshforge.common.errors import InvalidIdError
from meshforge.common.ids import (
    IdKind,
    id_kind,
    new_job_id,
    parse_id,
    validate_job_id,
    validate_node_id,
)

pytestmark = pytest.mark.unit


def _mp_worker(queue: mp.Queue[list[str]], count: int) -> None:
    queue.put([new_job_id() for _ in range(count)])


def test_ids_unique_across_processes() -> None:
    ctx = mp.get_context("spawn")
    queue: mp.Queue[list[str]] = ctx.Queue()
    procs = [ctx.Process(target=_mp_worker, args=(queue, 500)) for _ in range(4)]
    for p in procs:
        p.start()
    all_ids: list[str] = []
    for _ in procs:
        all_ids.extend(queue.get(timeout=30))
    for p in procs:
        p.join(timeout=10)
        assert p.exitcode == 0
    assert len(all_ids) == len(set(all_ids)), "duplicate ID generated across processes"


def _fork_child_state(queue: mp.Queue[tuple[int, str]]) -> None:
    from meshforge.common import ids

    state = ids._generator._last_ms
    queue.put((state, new_job_id()))


@pytest.mark.skipif(
    not hasattr(os, "register_at_fork"), reason="fork() does not exist on Windows"
)
def test_forked_child_resets_generator_state() -> None:
    parent_before = new_job_id()  # parent now has a non-empty last timestamp
    ctx = mp.get_context("fork")
    queue: mp.Queue[tuple[int, str]] = ctx.Queue()
    proc = ctx.Process(target=_fork_child_state, args=(queue,))
    proc.start()
    child_last_ms, child_id = queue.get(timeout=30)
    proc.join(timeout=10)
    assert proc.exitcode == 0
    assert child_last_ms == -1, "child inherited the parent's generator state"
    parent_after = new_job_id()
    assert len({parent_before, child_id, parent_after}) == 3


def test_ids_unique_and_monotonic_under_thread_contention() -> None:
    ids: list[str] = []
    lock = threading.Lock()

    def worker() -> None:
        local = [new_job_id() for _ in range(2000)]
        with lock:
            ids.extend(local)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(ids) == len(set(ids)), "duplicate ID generated across threads"


def test_generator_monotonic_even_if_clock_appears_to_step_backwards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import time as time_mod

    import meshforge.common.ids as ids_mod

    real_time_ns = time_mod.time_ns
    call_count = {"n": 0}

    def fake_time_ns() -> int:
        call_count["n"] += 1
        # First call: normal time. Second call: pretend the clock jumped
        # backwards by an hour (NTP correction, VM pause/resume, etc).
        if call_count["n"] == 1:
            return real_time_ns()
        return real_time_ns() - 3_600_000_000_000

    monkeypatch.setattr(time_mod, "time_ns", fake_time_ns)  # ids uses time.time_ns

    generator = ids_mod._MonotonicUlidGenerator()
    first = generator.generate()
    second = generator.generate()
    assert bytes(second) > bytes(first), (
        "ID ordering violated by a backwards clock step"
    )


@pytest.mark.parametrize(
    "value",
    [
        "",
        "job_",
        "job",
        "job__01J8Z3K5Q9V6W2X4Y7A0B1C2D3",
        "unknown_01J8Z3K5Q9V6W2X4Y7A0B1C2D3",
        "job_01j8z3k5q9v6w2x4y7a0b1c2d3",  # lower-case ULID part must be rejected
        "job_" + "0" * 26 + "extra",
        "job_" + "I" * 26,  # I/L/O/U excluded from Crockford base32
        "node_01J8Z3K5Q9V6W2X4Y7A0B1C2D3\x00",
        "job_01J8Z3K5Q9V6W2X4Y7A0B1C2D3 ",  # trailing whitespace
        " job_01J8Z3K5Q9V6W2X4Y7A0B1C2D3",  # leading whitespace
        "job_01J8Z3K5Q9V6W2X4Y7A0B1C2D3\n",
        "JOB_01J8Z3K5Q9V6W2X4Y7A0B1C2D3",  # prefix must be lower-case
        "job_ドラゴン01J8Z3K5Q9V6W2X4Y7A0",
    ],
)
def test_malformed_ids_rejected(value: str) -> None:
    with pytest.raises(InvalidIdError):
        parse_id(value)


@pytest.mark.parametrize(
    "value", [None, 123, 1.5, b"job_01J8Z3K5Q9V6W2X4Y7A0B1C2D3", ["x"]]
)
def test_non_string_ids_rejected(value: object) -> None:
    with pytest.raises(InvalidIdError):
        parse_id(value)


def test_wrong_kind_id_rejected_by_typed_validator() -> None:
    a_job_id = new_job_id()
    with pytest.raises(InvalidIdError):
        validate_node_id(a_job_id)


def test_valid_job_id_round_trips_through_typed_validator() -> None:
    job_id = new_job_id()
    validated = validate_job_id(job_id)
    assert validated == job_id
    assert id_kind(job_id) is IdKind.JOB
