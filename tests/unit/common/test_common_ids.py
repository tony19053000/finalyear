"""Task 0.1.6: typed ULID-based identifiers."""

import re
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated

import pytest
from pydantic import AfterValidator, BaseModel, ValidationError

from meshforge.common import ids
from meshforge.common.errors import FailureType, InvalidIdError
from meshforge.common.ids import (
    IdKind,
    id_kind,
    id_timestamp,
    new_attempt_id,
    new_chunk_id,
    new_command_id,
    new_id,
    new_job_id,
    new_node_id,
    new_session_id,
    parse_id,
    validate_job_id,
    validate_node_id,
)

pytestmark = pytest.mark.unit

ID_RE = re.compile(r"^[a-z]+_[0-9A-HJKMNP-TV-Z]{26}$")


@pytest.mark.parametrize(
    ("factory", "prefix"),
    [
        (new_job_id, "job_"),
        (new_attempt_id, "att_"),
        (new_node_id, "node_"),
        (new_session_id, "sess_"),
        (new_chunk_id, "chunk_"),
        (new_command_id, "cmd_"),
    ],
)
def test_typed_factories_have_prefix_and_shape(
    factory: Callable[[], str], prefix: str
) -> None:
    value = factory()
    assert value.startswith(prefix)
    assert ID_RE.fullmatch(value)
    assert id_kind(value).value == prefix[:-1]


def test_ids_are_unique_and_strictly_increasing_in_process() -> None:
    values = [new_job_id() for _ in range(20_000)]
    assert values == sorted(values)
    assert len(set(values)) == len(values)


def test_concurrent_generation_is_unique() -> None:
    results: list[list[str]] = []

    def worker() -> None:
        local = [new_id(IdKind.ATTEMPT) for _ in range(2_000)]
        results.append(local)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    flat = [v for chunk in results for v in chunk]
    assert len(set(flat)) == len(flat) == 16_000
    for chunk in results:  # each thread observes increasing IDs
        assert chunk == sorted(chunk)


def test_monotonic_even_if_clock_goes_backwards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gen = ids._MonotonicUlidGenerator()
    clock = iter(
        [5_000_000_000_000, 4_000_000_000_000, 4_000_000_000_000, 6_000_000_000_000]
    )
    monkeypatch.setattr(time, "time_ns", lambda: next(clock))
    produced = [gen.generate() for _ in range(4)]
    assert produced == sorted(produced)
    assert len(set(produced)) == 4
    assert produced[1].milliseconds == produced[0].milliseconds  # held, not rewound


def test_randomness_overflow_advances_timestamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gen = ids._MonotonicUlidGenerator()
    monkeypatch.setattr(time, "time_ns", lambda: 1_000_000_000_000)
    first = gen.generate()
    gen._last_rand = ids._MAX_RANDOMNESS
    second = gen.generate()
    assert second > first
    assert second.milliseconds == first.milliseconds + 1


def test_timestamp_is_recent_utc() -> None:
    ts = id_timestamp(new_job_id())
    assert ts.tzinfo is not None
    assert abs((datetime.now(UTC) - ts).total_seconds()) < 5


def test_parse_round_trip() -> None:
    value = new_session_id()
    kind, ulid = parse_id(value, IdKind.SESSION)
    assert kind is IdKind.SESSION
    assert f"sess_{ulid}" == value


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "job",
        "job_",
        "01J8Z3K5Q9V6W2X4Y7A0B1C2D3",  # missing prefix
        "blob_01J8Z3K5Q9V6W2X4Y7A0B1C2D3",  # unknown prefix
        "job_01J8Z3K5Q9V6W2X4Y7A0B1C2D",  # 25 chars
        "job_01J8Z3K5Q9V6W2X4Y7A0B1C2D3X",  # 27 chars
        "job_01j8z3k5q9v6w2x4y7a0b1c2d3",  # not canonical case
        "job_01J8Z3K5Q9V6W2X4Y7A0B1C2D!",  # invalid character
        "job_81J8Z3K5Q9V6W2X4Y7A0B1C2D3",  # timestamp overflow (> 7ZZZ...)
        "JOB_01J8Z3K5Q9V6W2X4Y7A0B1C2D3",
    ],
)
def test_parse_rejects_malformed(bad: str) -> None:
    with pytest.raises(InvalidIdError):
        parse_id(bad)


def test_parse_rejects_wrong_kind_and_non_strings() -> None:
    job = new_job_id()
    with pytest.raises(InvalidIdError, match="expected a 'node' ID"):
        validate_node_id(job)
    with pytest.raises(InvalidIdError) as info:
        parse_id(123)
    assert info.value.failure_type is FailureType.INVALID_INPUT
    assert validate_job_id(job) == job


class _Job(BaseModel):
    id: Annotated[str, AfterValidator(validate_job_id)]


def test_validators_integrate_with_pydantic() -> None:
    job = new_job_id()
    assert _Job(id=job).id == job
    with pytest.raises(ValidationError):
        _Job(id=new_node_id())
