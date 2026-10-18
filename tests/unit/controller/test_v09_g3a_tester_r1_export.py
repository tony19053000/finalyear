"""V0.9 G3 lane A tester r1 (item 0.7, D230.1): the export across real row-group
boundaries, empty, cap and cap + 1 through the route, and secrets."""

from __future__ import annotations

import io
from typing import Any

import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")
pytest.importorskip("pyarrow", reason="needs the controller extra")

import pyarrow.parquet as pq
from test_v09_g1_run_record import SNAPSHOT, T0, _records, _source

from meshforge.controller.telemetry import export
from meshforge.controller.telemetry.record import build_record
from meshforge.controller.telemetry.schema import COLUMNS

pytestmark = pytest.mark.unit


def _many(n: int) -> list[dict[str, Any]]:
    base = _records(1)[0]
    return [{**base, "attempt_id": f"att_{i:07d}"} for i in range(n)]


def _old_way(records: list[dict[str, Any]]) -> bytes:
    buffer = io.BytesIO()
    export.write_parquet(export.records_table(records), buffer)
    return buffer.getvalue()


@pytest.mark.parametrize("count", [0, 1, 65_535, 65_536, 65_537, 131_073], ids=str)
def test_real_row_group_size_boundaries(count: int) -> None:
    records = _many(count)
    first = export.parquet_bytes(records)
    assert first == export.parquet_bytes(list(reversed(records)))
    assert first == _old_way(records)
    meta = pq.read_metadata(io.BytesIO(first))
    assert meta.num_rows == count
    assert meta.num_row_groups == max(1, -(-count // export.ROW_GROUP_SIZE))
    sizes = [meta.row_group(i).num_rows for i in range(meta.num_row_groups)]
    assert all(s == export.ROW_GROUP_SIZE for s in sizes[:-1])
    if count:
        ids = pq.read_table(io.BytesIO(first), columns=["attempt_id"])
        got = ids.column("attempt_id").to_pylist()
        assert got[0] == "att_0000000" and got[-1] == f"att_{count - 1:07d}"
        assert got == sorted(got)


def test_empty_export_is_a_valid_file_with_the_full_schema() -> None:
    data = export.parquet_bytes([])
    assert data[:4] == b"PAR1" and data[-4:] == b"PAR1"
    table = pq.read_table(io.BytesIO(data))
    assert table.num_rows == 0
    assert table.schema.names == [c.name for c in COLUMNS]
    assert table.schema.equals(export.arrow_schema())
    assert export.parquet_bytes(iter([])) == data  # a generator works too


def test_write_records_returns_the_count_and_accepts_a_generator() -> None:
    records = _records(5)
    sink = io.BytesIO()
    assert export.write_records((r for r in records), sink) == 5
    assert sink.getvalue() == export.parquet_bytes(records)


def test_values_survive_the_round_trip_at_a_group_edge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records = _records(5)
    one_group = pq.read_table(io.BytesIO(export.parquet_bytes(records))).to_pylist()
    monkeypatch.setattr(export, "ROW_GROUP_SIZE", 2)
    grouped = pq.read_table(io.BytesIO(export.parquet_bytes(records))).to_pylist()
    assert grouped == one_group and len(grouped) == 5


# ----------------------------------------------------------------- the route


def _app(
    monkeypatch: pytest.MonkeyPatch, rows_in_db: int, max_rows: int
) -> tuple[Any, list[int]]:
    from fastapi import FastAPI

    from meshforge.controller.api import telemetry_routes as routes
    from meshforge.controller.api.auth import require_admin
    from meshforge.controller.api.errors import install_error_handlers

    seen: list[int] = []
    records = _many(rows_in_db)

    async def fake_fetch(engine: Any, *, limit: int, **_: Any) -> list[Any]:
        seen.append(limit)
        return records[:limit]  # what SQL LIMIT does

    monkeypatch.setattr(export, "fetch_records", fake_fetch)
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(routes.telemetry_router)
    app.dependency_overrides[require_admin] = lambda: type(
        "P", (), {"user_id": "u", "is_admin": True}
    )()
    setattr(
        app.state,
        routes.TELEMETRY_STATE_KEY,
        routes.TelemetryExport(engine=None, max_rows=max_rows),  # type: ignore[arg-type]
    )
    return app, seen


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("in_db", "status"), [(0, 200), (9, 200), (10, 200), (11, 413)]
)
async def test_the_cap_is_inclusive_and_cap_plus_one_is_413(
    monkeypatch: pytest.MonkeyPatch, in_db: int, status: int
) -> None:
    import httpx

    app, seen = _app(monkeypatch, in_db, max_rows=10)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        reply = await client.get("/v1/telemetry/export")
    assert reply.status_code == status, reply.text
    assert seen == [11]  # asks for cap + 1 to know it overflowed
    if status == 200:
        assert reply.headers["X-MeshForge-Telemetry-Rows"] == str(in_db)
        assert int(reply.headers["Content-Length"]) == len(reply.content)
        assert pq.read_metadata(io.BytesIO(reply.content)).num_rows == in_db
        assert reply.content == export.parquet_bytes(_many(in_db))
    else:
        assert "narrow the export" in reply.text and "10" in reply.text


# --------------------------------------------------------------------- secrets

SENTINEL = "S3CR3T-sentinel-0123456789"


def _planted(workload_params_extra: dict[str, Any]) -> list[dict[str, Any]]:
    snapshot = {
        **SNAPSHOT,
        "secrets": {"TOKEN": SENTINEL},
        "node": {**SNAPSHOT["node"], "note": SENTINEL},
    }
    source = _source(
        start_snapshot=snapshot,
        manifest={
            "env": {"base": "python-cpu", "vars": {"API": SENTINEL}},
            "resources": {"gpu": "none"},
            "secrets": {"API_KEY": SENTINEL},
            "name": "job",
        },
        placement={"mode": "offload", "node_id": "node_b", "note": SENTINEL},
        workload={
            "family": "cpu.matmul",
            "params": {
                "n": 512,
                "repeats": 3,
                "dtype": "float64",
                **workload_params_extra,
            },
            "seed": 7,
            "status": "ok",
            "device": "cpu",
            "compute_ns": 5,
            "note": SENTINEL,
        },
    )
    record = build_record(source)
    record["recorded_at"] = T0
    return [record]


def _assert_clean(records: list[dict[str, Any]]) -> None:
    data = export.parquet_bytes(records)
    assert SENTINEL not in repr(pq.read_table(io.BytesIO(data)).to_pylist())
    assert SENTINEL.encode() not in data


def test_a_secret_planted_in_manifest_placement_snapshot_never_reaches_the_file() -> (
    None
):
    _assert_clean(_planted({}))


def test_defect_unknown_keys_in_reported_workload_params_are_stored() -> None:
    _assert_clean(_planted({"token": SENTINEL}))


# ------------------------------------------------------------- tester r2 (D236)


def _wl(family: Any, params: Any) -> dict[str, Any]:
    from meshforge.controller.telemetry.record import _workload

    return _workload({"family": family, "params": params, "seed": 1})


def test_r2_only_the_family_own_keys_are_kept() -> None:
    out = _wl("cpu.matmul", {"n": 4, "token": SENTINEL, "size_mb": 9, "batch": 2})
    assert out["workload_params"] == {"n": 4}  # size_mb/batch belong to other families
    assert out["p_n"] == 4 and out["p_size_mb"] is None and out["p_batch"] is None
    assert SENTINEL not in repr(out)


@pytest.mark.parametrize(
    "family",
    [None, 5, ["cpu.matmul"], "", "cpu.nope", "CPU.MATMUL", "cpu.matmul\x00", {}],
    ids=repr,
)
def test_r2_an_unknown_or_malformed_family_gives_null_params(family: Any) -> None:
    out = _wl(family, {"n": 4, "token": SENTINEL})
    assert out["workload_params"] is None
    assert all(out[c] is None for c in out if c.startswith("p_"))
    assert SENTINEL not in repr(out)


@pytest.mark.parametrize("params", [None, [], "x", 5, [{"n": 1}]], ids=repr)
def test_r2_params_that_are_not_an_object_give_null(params: Any) -> None:
    assert _wl("cpu.matmul", params)["workload_params"] is None


def test_r2_non_string_keys_are_dropped() -> None:
    out = _wl("cpu.matmul", {1: "x", None: "y", ("n",): 3, "n": 7})
    assert out["workload_params"] == {"n": 7}


def test_r2_hostile_values_under_a_real_key_never_break_the_record() -> None:
    import json

    deep = json.loads("[" * 200 + "]" * 200)
    for value in (
        deep,
        {"a": deep},
        "x" * 100_000,
        "a\x00b",
        "\ud800",
        2**70,
        {"token": SENTINEL},
    ):
        out = _wl("cpu.matmul", {"dtype": value, "n": value})
        assert out["p_dtype"] is None or isinstance(out["p_dtype"], str)
        assert out["p_n"] is None or isinstance(out["p_n"], int)
        if out["p_dtype"] is not None:
            assert len(out["p_dtype"]) <= 64
        _assert_clean_roundtrip(out)


def _assert_clean_roundtrip(out: dict[str, Any]) -> None:
    import json

    params = out["workload_params"]
    if params is not None:  # a kept document must be storable JSON text
        json.dumps(params, allow_nan=False)
        assert "\x00" not in json.dumps(params)
