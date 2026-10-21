"""V0.10 TD2 tester: a job-written benchmark report (``results/workload.json``)
with hostile keys / values reaches an operator only cleaned - through a real
controller, agent and PostgreSQL: the job API view, the stored JSONB and the
Parquet export. Clean params must arrive byte-for-byte (as parsed JSON) and
non-string values must be unchanged."""

# ruff: noqa: F811
from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")
pytest.importorskip("pyarrow", reason="needs the controller extra")

from mf_exec_harness import ExecAgent, submit, wait_job
from sqlalchemy import text
from test_v07_g6_tester_r1_jobs import (  # noqa: F401
    alice,
    alice_token,
    settings,
)

from meshforge.controller.server import ControllerRuntime
from meshforge.controller.store.engine import create_engine
from meshforge.controller.telemetry.export import fetch_records, parquet_bytes

pytestmark = [
    pytest.mark.component,
    pytest.mark.security,
    pytest.mark.asyncio,
    pytest.mark.timeout(300),
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX"),
]

CODE = """\
import json, os
os.makedirs("results", exist_ok=True)
params = json.load(open("params.json", encoding="utf-8"))
json.dump({"schema_version": 1, "family": "cpu.matmul", "status": "ok",
           "params": params, "seed": 1, "device": "cpu", "compute_ns": 10},
          open("results/workload.json", "w", encoding="utf-8"), ensure_ascii=True)
"""

BAD = "\x1b]0;owned\x07\x1b[2J\x1b[31mred\x1b[0m\u202eexe.txt\u200b\r\nnext\x9b1m"
BAD_CLEAN = "red" + "exe.txt" + "\nnext"
HOSTILE: dict[str, Any] = {
    "n": 3,
    "ratio": 0.5,
    "flag": True,
    "none": None,
    "ints": [1, 2, 3],
    "dtype": BAD,
    "\x1b[31mkey\u202e": "v",
    "nested": {"\u200bdeep": ["\x1b[2Jlist", {"\u202ek": BAD}], "k": 7},
}
CLEAN: dict[str, Any] = {
    "n": 200000,
    "dtype": "float32",
    "name": "\u65e5\u672c\u8a9e \u00e9\u00e8 tab\there",
    "nested": {"a": [1, 2.5, None, True, {"b": "c"}], "k": "v"},
    "empty": {},
    "list": [],
}


def _expected_hostile() -> dict[str, Any]:
    return {
        "n": 3,
        "ratio": 0.5,
        "flag": True,
        "none": None,
        "ints": [1, 2, 3],
        "dtype": BAD_CLEAN,
        "key": "v",
        "nested": {"deep": ["list", {"k": BAD_CLEAN}], "k": 7},
    }


def _deep(depth: int) -> Any:
    out: Any = "\x1b[31mleaf\u202e"
    for _ in range(depth):
        out = {"d\u200b": out}
    return out


def _flatten_strings(doc: Any) -> list[str]:
    out: list[str] = []
    stack = [doc]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            out.extend(item)
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return out


def _display_safe(value: str) -> bool:
    return not any(
        (ord(c) < 0x20 and c not in "\n\t")
        or 0x7F <= ord(c) <= 0x9F
        or c in "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
        or c in "\u200b\u200c\u200d\u200e\u200f\u2060\ufeff\u061c"
        for c in value
    )


async def _run(
    alice: httpx.AsyncClient,
    grpc_target: str,
    tmp_path: Path,
    pg_settings: Any,
    params: Any,
) -> tuple[dict[str, Any], dict[str, Any], str]:
    """(job view, parquet workload_params parsed, stored JSONB text)."""
    agent = await ExecAgent.start(
        grpc_target,
        tmp_path / "agent",
        agent_overrides={"policy": {"accept_jobs": True, "allow_unsandboxed": True}},
    )
    try:
        project = tmp_path / "proj"
        project.mkdir()
        (project / "main.py").write_text(CODE)
        (project / "params.json").write_text(json.dumps(params))
        job_id = await submit(
            alice,
            project,
            agent.node_id,
            {
                "name": "hostile-report",
                "entrypoint": ["python", "main.py"],
                "retry": {"max_attempts": 1},
            },
        )
        job = await wait_job(alice, job_id, timeout_s=60)
        assert job["state"] == "succeeded", job
    finally:
        await agent.stop()
    engine = create_engine(pg_settings)
    try:
        import asyncio

        rows: list[dict[str, Any]] = []
        for _ in range(100):
            rows = await fetch_records(engine)
            if rows:
                break
            await asyncio.sleep(0.2)
        assert rows, "no run record"
        async with engine.connect() as conn:
            stored = await conn.scalar(text("SELECT workload::text FROM attempt"))
        import pyarrow.parquet as pq

        table = pq.read_table(io.BytesIO(parquet_bytes(rows)))
        cells = [p for p in table.column("workload_params").to_pylist() if p]
        assert cells, "no workload_params in the export"
        return job, json.loads(cells[0]), str(stored)
    finally:
        await engine.dispose()


async def test_hostile_report_params_are_cleaned_in_api_db_and_parquet(
    alice: httpx.AsyncClient,
    grpc_target: str,
    runtime: ControllerRuntime,
    tmp_path: Path,
    pg_settings: Any,
) -> None:
    job, exported, stored = await _run(
        alice, grpc_target, tmp_path, pg_settings, HOSTILE
    )
    # the job API view does not carry the report; it must still carry no raw
    # control from the (cleaned) attempt document
    api = (await alice.get(f"/v1/jobs/{job['job_id']}")).json()
    assert all(_display_safe(s) for s in _flatten_strings(api))
    want = _expected_hostile()
    stored_doc = json.loads(stored)["params"]
    api_params = stored_doc  # the attempt's stored report document
    # the export keeps only the registered family's keys (dtype, n)
    assert exported == {"dtype": BAD_CLEAN, "n": 3}, exported
    stored_params = json.loads(stored)["params"]
    assert stored_params == want
    for doc in (api_params, exported, stored_params):
        assert all(_display_safe(s) for s in _flatten_strings(doc)), doc
    # nothing readable disappeared: the visible words survive
    blob = json.dumps(api_params)
    for word in ("red", "exe.txt", "next", "list"):
        assert word in blob, word
    # types untouched
    assert api_params["n"] == 3 and api_params["flag"] is True
    assert api_params["none"] is None and api_params["ints"] == [1, 2, 3]
    assert api_params["ratio"] == 0.5


async def test_clean_report_params_arrive_unchanged(
    alice: httpx.AsyncClient,
    grpc_target: str,
    runtime: ControllerRuntime,
    tmp_path: Path,
    pg_settings: Any,
) -> None:
    _job, exported, stored = await _run(
        alice, grpc_target, tmp_path, pg_settings, CLEAN
    )
    assert json.loads(stored)["params"] == CLEAN
    assert exported == {"dtype": "float32", "n": 200000}, exported
    assert json.loads(stored)["params"] == CLEAN


async def test_deeply_nested_hostile_params_do_not_lose_the_report(
    alice: httpx.AsyncClient,
    grpc_target: str,
    runtime: ControllerRuntime,
    tmp_path: Path,
    pg_settings: Any,
) -> None:
    """Depth 60 (below JSON/JSONB limits): cleaned all the way down, and the
    attempt still reports success - cleaning must not turn into a refusal."""
    _job, _exported, stored = await _run(
        alice, grpc_target, tmp_path, pg_settings, {"deep": _deep(60), "n": 1}
    )
    node: Any = json.loads(stored)["params"]["deep"]
    for _ in range(60):
        assert list(node) == ["d"], node
        node = node["d"]
    assert node == "leaf"
