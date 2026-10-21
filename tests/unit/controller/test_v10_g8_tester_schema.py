"""V0.10 G8 tester (independent): schema v3, chunks.parquet determinism,
SiteTimesCache bounds, campaign forced strategies, stderr tail cleaning.
"""

# ruff: noqa: S311, DTZ001
from __future__ import annotations

import asyncio
import datetime as dt
import io
import random
from typing import Any

import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")
pytest.importorskip("pyarrow", reason="needs the controller extra")

import pyarrow.parquet as pq

import meshforge.controller.split.speculation as spec_mod
from meshforge.bench.campaign import CampaignSpecError, NodeRef, expand, parse_spec
from meshforge.controller.split.records import clean_tail
from meshforge.controller.split.speculation import SiteTimesCache
from meshforge.controller.store.models.telemetry import STDERR_TAIL_CHARS
from meshforge.controller.telemetry import schema
from meshforge.controller.telemetry.export import (
    chunk_parquet_bytes,
    chunks_table,
    parquet_bytes,
)

pytestmark = pytest.mark.unit

T0 = dt.datetime(2026, 10, 1, 12, 0, 0, tzinfo=dt.UTC)


# --------------------------------------------------------------- schema v3


def test_schema_version_is_3_and_published_fingerprints_are_frozen() -> None:
    assert schema.SCHEMA_VERSION == 3
    # v1 and v2 entries are literal values published by V0.9: never edited.
    assert schema.SCHEMA_FINGERPRINTS[1] == (
        "69e443de70cbb8bbd25fb04662510910c649b812ad16000ede6d092948df723a"
    )
    assert schema.SCHEMA_FINGERPRINTS[2] == (
        "6cdab4d3e019c82bf7f1f8d08cfc6b8543a234b642bdc68c1c8f3135d2c02882"
    )
    assert set(schema.SCHEMA_FINGERPRINTS) == {1, 2, 3}
    assert schema.fingerprint() == schema.SCHEMA_FINGERPRINTS[3]
    assert schema.fingerprint(schema.CHUNK_COLUMNS) == schema.CHUNK_FINGERPRINTS[3]


def test_v3_run_columns_are_additive_and_nullable() -> None:
    names = [c.name for c in schema.COLUMNS]
    assert len(names) == len(set(names))
    by = {c.name: c for c in schema.COLUMNS}
    for new in (
        "attempt_role",
        "planner_strategy",
        "planner_node_set",
        "split_node_set",
        "split_items",
        "split_chunk_items",
        "split_chunks",
        "split_predicted_s",
        "split_makespan_ms",
        "split_job_ms",
        "split_combine_ms",
        "split_ideal_bound_s",
    ):
        assert new in by, new
        assert by[new].nullable, f"{new} must be nullable (v1/v2 rows keep nulls)"


def test_chunk_columns_never_export_the_stderr_tail_and_are_unique() -> None:
    names = schema.CHUNK_COLUMN_NAMES
    assert len(names) == len(set(names))
    assert "stderr_tail" not in names
    assert set(schema.CHUNK_OUTCOMES) == {
        "committed",
        "lost",
        "failed",
        "superseded",
        "released",
        "expired",
    }
    nullable = {c.name for c in schema.CHUNK_COLUMNS if c.nullable}
    for must in ("run_ms", "stage_ms", "env_ms", "upload_ms", "exit_code", "ended_at"):
        assert must in nullable, f"{must}: a superseded/expired row has no measurement"


# ------------------------------------------------------- chunks.parquet


def _row(job: str, idx: int, seq: int, **kw: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "schema_version": 3,
        "job_id": job,
        "chunk_index": idx,
        "lease_seq": seq,
        "lane_attempt_id": f"{job}-l{seq}",
        "node_id": f"n{seq}",
        "first_item": idx * 4,
        "items": 4,
        "speculative": seq > 1,
        "won": seq == 1,
        "outcome": "committed" if seq == 1 else "superseded",
        "failure_type": None,
        "failure_class": "ok" if seq == 1 else "unavailable",
        "exit_code": 0 if seq == 1 else None,
        "result_digest": None,
        "bytes_in": 10,
        "bytes_out": 20,
        "stage_ms": 1,
        "env_ms": 2,
        "run_ms": 3,
        "upload_ms": 4,
        "leased_at": T0,
        "ended_at": T0 + dt.timedelta(seconds=1),
        "recorded_at": T0 + dt.timedelta(seconds=2),
    }
    row.update(kw)
    return row


def test_chunk_parquet_is_byte_identical_for_any_input_order() -> None:
    rows = [_row("jobB", i, s) for i in range(7) for s in (1, 2)] + [
        _row("jobA", i, 1) for i in range(5)
    ]
    ref = chunk_parquet_bytes(rows)
    rng = random.Random(1)
    for _ in range(5):
        shuffled = list(rows)
        rng.shuffle(shuffled)
        assert chunk_parquet_bytes(shuffled) == ref
        assert chunk_parquet_bytes(iter(shuffled)) == ref
    table = pq.read_table(io.BytesIO(ref))
    keys = list(
        zip(
            table["job_id"].to_pylist(),
            table["chunk_index"].to_pylist(),
            table["lease_seq"].to_pylist(),
            strict=True,
        )
    )
    assert keys == sorted(keys)
    assert table.column_names == list(schema.CHUNK_COLUMN_NAMES)
    assert table.schema.metadata[b"meshforge.telemetry.schema_version"] == b"3"


def test_chunk_parquet_empty_and_nulls_and_extra_keys() -> None:
    empty = chunk_parquet_bytes([])
    t = pq.read_table(io.BytesIO(empty))
    assert t.num_rows == 0 and t.column_names == list(schema.CHUNK_COLUMN_NAMES)
    assert chunk_parquet_bytes([]) == empty
    nulls = _row(
        "j",
        0,
        2,
        run_ms=None,
        stage_ms=None,
        exit_code=None,
        ended_at=None,
        stderr_tail="SECRET-ish",
    )
    out = chunk_parquet_bytes([nulls])
    back = pq.read_table(io.BytesIO(out)).to_pylist()[0]
    assert back["run_ms"] is None and back["ended_at"] is None
    assert "stderr_tail" not in back  # operational, never exported
    assert b"SECRET-ish" not in out


def test_chunk_parquet_rejects_naive_timestamps_and_null_required() -> None:
    with pytest.raises(ValueError):
        chunks_table([_row("j", 0, 1, leased_at=dt.datetime(2026, 1, 1))])
    with pytest.raises(ValueError):
        chunks_table([_row("j", 0, 1, outcome=None)])


def test_runs_parquet_still_deterministic_and_v3() -> None:
    a = parquet_bytes([])
    assert a == parquet_bytes([])
    t = pq.read_table(io.BytesIO(a))
    assert t.schema.metadata[b"meshforge.telemetry.schema_version"] == b"3"


# ------------------------------------------------------- stderr tail


def test_clean_tail_bounds_encoding_and_nul() -> None:
    assert clean_tail(b"", 100) is None
    assert clean_tail(b"abc", 0) is None
    assert clean_tail(b"\x00\x00\x00", 10) is None  # only NULs -> nothing
    assert clean_tail(b"a\x00b", 10) == "ab"
    assert clean_tail(b"hello world", 5) == "world"
    # invalid UTF-8 never raises and never leaves bytes
    out = clean_tail(b"ok \xff\xfe bad \x80 end", 100)
    assert out is not None and out.endswith("end") and isinstance(out, str)
    # multibyte char cut by the limit: no leading replacement char
    data = ("é" * 50).encode()  # 100 bytes
    for limit in (1, 2, 3, 5, 51, 99):
        text = clean_tail(data, limit)
        assert text is None or not text.startswith("�"), (limit, text)
        assert text is None or len(text.encode()) <= limit
    # emoji (4 byte) cut at every offset
    emoji = ("a😀" * 20).encode()
    for limit in range(1, 30):
        text = clean_tail(emoji, limit)
        assert text is None or not text.startswith("�")
    # huge input is bounded by the column limit regardless of the configured limit
    big = b"x" * (STDERR_TAIL_CHARS * 4)
    got = clean_tail(big, STDERR_TAIL_CHARS * 8)
    assert got is not None and len(got) <= STDERR_TAIL_CHARS
    assert clean_tail(b"x" * 10_000, 16_384) == "x" * 10_000
    # the tail is the END, not the start
    assert clean_tail(b"START" + b"m" * 100 + b"END", 10) == "m" * 7 + "END"
    # control chars / ANSI survive as text (JSON-safe escape is the API's job)
    assert clean_tail(b"\x1b[31mred\x1b[0m", 100) == "\x1b[31mred\x1b[0m"


def test_clean_tail_windows_line_endings_and_utf16_garbage() -> None:
    assert clean_tail(b"a\r\nb\r\n", 100) == "a\r\nb\r\n"
    utf16 = "traceback é".encode("utf-16")
    assert isinstance(clean_tail(utf16, 100), str | None)


# ------------------------------------------------------- SiteTimesCache


class _Clock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def counting(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls: list[int] = []

    async def fake(db: Any, queue_id: int) -> dict[str, Any]:
        calls.append(queue_id)
        return {}

    monkeypatch.setattr(spec_mod, "_history", fake)
    return calls


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_cache_one_read_per_commit_and_ttl(counting: list[int]) -> None:
    clock = _Clock()
    cache = SiteTimesCache(ttl_s=5.0, entries=4, clock=clock)
    for _ in range(100):  # 100 idle-lane requests, nothing committed
        _run(cache.history(None, 1, 3))  # type: ignore[arg-type]
    assert len(counting) == 1
    _run(cache.history(None, 1, 4))  # a commit
    assert len(counting) == 2
    clock.t += 5.01  # ttl passes
    _run(cache.history(None, 1, 4))
    assert len(counting) == 3
    clock.t += 5.0  # exactly ttl: still valid (strictly greater expires)
    _run(cache.history(None, 1, 4))
    assert len(counting) == 3


def test_cache_bounded_lru_and_disabled_and_validation(counting: list[int]) -> None:
    cache = SiteTimesCache(ttl_s=60.0, entries=3, clock=_Clock())
    for q in range(1000):
        _run(cache.history(None, q, 1))  # type: ignore[arg-type]
        assert len(cache) <= 3
    assert len(counting) == 1000
    # the most recent three are still hits
    before = len(counting)
    for q in (997, 998, 999):
        _run(cache.history(None, q, 1))  # type: ignore[arg-type]
    assert len(counting) == before
    _run(cache.history(None, 0, 1))  # evicted -> read again
    assert len(counting) == before + 1

    off = SiteTimesCache(ttl_s=0, entries=3)
    counting.clear()
    for _ in range(5):
        _run(off.history(None, 1, 1))  # type: ignore[arg-type]
    assert len(counting) == 5 and len(off) == 0

    unknown = SiteTimesCache(ttl_s=60, entries=3)
    counting.clear()
    for _ in range(3):
        _run(unknown.history(None, 1, None))  # type: ignore[arg-type]
    assert len(counting) == 3  # no committed count -> never cached

    for bad in (-1.0, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            SiteTimesCache(ttl_s=bad, entries=3)
    with pytest.raises(ValueError):
        SiteTimesCache(ttl_s=1, entries=0)


def test_cache_returns_copies_not_shared_mutable_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake(db: Any, queue_id: int) -> dict[str, Any]:
        return {"n1": object()}

    monkeypatch.setattr(spec_mod, "_history", fake)
    cache = SiteTimesCache(ttl_s=60, entries=3)
    first, _ = _run(cache.history(None, 1, 1))  # type: ignore[arg-type]
    first.clear()  # a caller mutating its result
    second, _ = _run(cache.history(None, 1, 1))  # type: ignore[arg-type]
    assert "n1" in second


# ------------------------------------------------------- campaign forcing

NODES = [NodeRef("n1", "a"), NodeRef("n2", "b"), NodeRef("n3", "c")]


def _spec(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "name": "t",
        "seed": 1,
        "families": [{"family": "split.csv_aggregate"}],
        "strategies": ["mode1", "mode3"],
        "origin": "a",
        "repetitions": 3,
        "budget": {"max_runs": 1000},
    }
    base.update(kw)
    return base


def test_forced_strategy_expansion_is_deterministic_and_covers_every_rep() -> None:
    spec = parse_spec(_spec())
    one = expand(spec, NODES).runs
    two = expand(spec, NODES).runs
    assert [r.config_id for r in one] == [r.config_id for r in two]
    for reps in range(1, 4):
        in_rep = [r for r in one if r.repetition == reps and r.strategy == "mode3"]
        assert len(in_rep) == 1 and in_rep[0].node_set == ("n1", "n2", "n3")
    ids = {r.config_id for r in one}
    assert len({r.config_id for r in one if r.strategy == "mode3"}) == 1
    assert len(ids) == 1 + 2  # mode3 + mode1 on n2, n3
    # a mode3 set is order-insensitive: the same set, same config id
    a = expand(parse_spec(_spec(mode3_sets=[["a", "b", "c"]])), NODES).runs
    b = expand(parse_spec(_spec(mode3_sets=[["c", "a", "b"]])), NODES).runs
    assert {r.config_id for r in a if r.strategy == "mode3"} == {
        r.config_id for r in b if r.strategy == "mode3"
    }


def test_mode3_rejections() -> None:
    with pytest.raises(CampaignSpecError):
        parse_spec(_spec(families=[{"family": "cpu.primes"}]))
    with pytest.raises(CampaignSpecError):
        parse_spec(_spec(mode3_sets=[["a"]]))  # a set needs >= 2 (schema)
    with pytest.raises(CampaignSpecError):
        parse_spec(_spec(mode3_sets=[]))
    with pytest.raises(CampaignSpecError):
        parse_spec(_spec(mode3_sets="a,b"))
    with pytest.raises(CampaignSpecError):
        expand(parse_spec(_spec(mode3_sets=[["a", "zzz"]])), NODES)
    with pytest.raises(CampaignSpecError):
        parse_spec(_spec(conditions=["none", "cpu_load:50"]))
