"""V0.9 G5 lane A: ``compute bench labels`` (real client, faked transport)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from mf_cli_support import TransportFactory
from typer.testing import CliRunner

from meshforge.cli.app import app
from meshforge.cli.bench_label_commands import best_cell, margin_cell

pytestmark = pytest.mark.unit

CAMP = "camp_01J0000000000000000000DDDD"
DOC: dict[str, Any] = {
    "campaign_id": CAMP,
    "labels_version": 1,
    "campaign_state": "done",
    "rows": 15,
    "counts": {"best": 1, "tie": 1, "insufficient": 1, "pending": 0},
    "groups": [
        {
            "group_id": "g1",
            "family": "cpu.sort",
            "condition": "none",
            "label": "best",
            "best_strategy": "mode1",
            "best_node_id": "node_b",
            "margin_pct": 42.123,
            "tied_config_ids": [],
            "reason": None,
            "configs": [],
        },
        {
            "group_id": "g2",
            "family": "cpu.zlib",
            "condition": "cpu_load:2",
            "label": "tie",
            "margin_pct": 1.0,
            "tied_config_ids": ["c1", "c2"],
            "reason": None,
            "configs": [
                {"config_id": "c1", "strategy": "local", "node_id": "node_a"},
                {"config_id": "c2", "strategy": "mode1", "node_id": "node_b"},
            ],
        },
        {
            "group_id": "g3",
            "family": "cpu.sha256",
            "condition": "none",
            "label": "insufficient",
            "margin_pct": None,
            "tied_config_ids": [],
            "reason": "fewer than 3 measured repetitions: c9",
            "configs": [],
        },
    ],
}


def test_the_table_shows_every_group(
    runner: CliRunner, controller: TransportFactory
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=DOC)

    controller(handler)
    result = runner.invoke(app, ["bench", "labels", CAMP])
    assert result.exit_code == 0, result.output
    (request,) = seen
    assert request.url.path == f"/v1/bench/campaigns/{CAMP}/labels"
    out = result.stdout
    for text in ("g1", "mode1 @ node_b", "42.1", "tie: local @ node_a", "fewer than 3"):
        assert text in out, (text, out)
    assert (
        "3 groups: best 1, tie 1, insufficient 1, pending 0 (from 15 run records)"
        in out
    )


def test_a_bad_campaign_id_is_a_usage_error_before_any_request(
    runner: CliRunner, controller: TransportFactory
) -> None:
    controller(lambda _r: pytest.fail("no request expected"))  # type: ignore[arg-type,return-value]
    result = runner.invoke(app, ["bench", "labels", "../etc"])
    assert result.exit_code == 2


def test_parquet_download(
    runner: CliRunner, controller: TransportFactory, tmp_path: Path
) -> None:
    body = b"PAR1" + b"y" * 1000 + b"PAR1"
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            content=body,
            headers={
                "content-type": "application/vnd.apache.parquet",
                "x-meshforge-label-rows": "4",
            },
        )

    controller(handler)
    out = tmp_path / "labels.parquet"
    result = runner.invoke(app, ["bench", "labels", CAMP, "-o", str(out), "--json"])
    assert result.exit_code == 0, result.output
    assert out.read_bytes() == body
    doc = json.loads(result.stdout)
    assert doc["file"] == str(out) and doc["rows"] == 4 and doc["bytes"] == len(body)
    assert doc["labels_version"] == 1
    assert seen[0].url.params["format"] == "parquet"


def test_an_existing_file_is_never_overwritten(
    runner: CliRunner, controller: TransportFactory, tmp_path: Path
) -> None:
    controller(lambda _r: httpx.Response(200, content=b"PAR1"))
    out = tmp_path / "keep.parquet"
    out.write_bytes(b"mine")
    result = runner.invoke(app, ["bench", "labels", CAMP, "-o", str(out)])
    assert result.exit_code == 2
    assert out.read_bytes() == b"mine"


def test_cells() -> None:
    assert margin_cell(None) == "-" and margin_cell(True) == "-"
    assert margin_cell(3) == "3.0"
    assert best_cell({"label": "pending", "reason": None}) == "-"
    assert best_cell({"label": "tie", "tied_config_ids": ["zz"], "configs": []}) == (
        "tie: ? @ zz"
    )
