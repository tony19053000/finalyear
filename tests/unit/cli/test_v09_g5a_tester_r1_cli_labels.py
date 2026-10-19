"""V0.9 G5 lane A tester r1: ``compute bench labels`` failure behaviour."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from mf_cli_support import TransportFactory
from typer.testing import CliRunner

from meshforge.cli.app import app

pytestmark = pytest.mark.unit

CAMP = "camp_01J0000000000000000000DDDD"


def _err(status: int, code: str) -> httpx.Response:
    return httpx.Response(
        status, json={"error": {"code": code, "message": "nope", "details": {}}}
    )


@pytest.mark.parametrize(
    ("status", "code"), [(403, "forbidden"), (404, "not_found"), (413, "invalid_input")]
)
def test_error_with_output_file_leaves_no_file_and_exits_nonzero(
    runner: CliRunner,
    controller: TransportFactory,
    tmp_path: Path,
    status: int,
    code: str,
) -> None:
    controller(lambda _r: _err(status, code))
    out = tmp_path / "labels.parquet"
    result = runner.invoke(app, ["bench", "labels", CAMP, "-o", str(out)])
    assert result.exit_code != 0
    assert not out.exists()
    assert list(tmp_path.iterdir()) == []  # no temp or partial leftovers


def test_connection_dropped_mid_download_leaves_no_partial_file(
    runner: CliRunner, controller: TransportFactory, tmp_path: Path
) -> None:
    class Broken(httpx.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            yield b"PAR1" + b"x" * 4096
            raise httpx.ReadError("connection reset")

    controller(lambda _r: httpx.Response(200, stream=Broken()))
    out = tmp_path / "labels.parquet"
    result = runner.invoke(app, ["bench", "labels", CAMP, "-o", str(out)])
    assert result.exit_code != 0
    assert not out.exists(), (
        "a truncated Parquet file must not be left under the final name"
    )


def test_json_mode_prints_the_document_even_for_zero_groups(
    runner: CliRunner, controller: TransportFactory
) -> None:
    doc = {
        "campaign_id": CAMP,
        "labels_version": 1,
        "campaign_state": "draft",
        "rows": 0,
        "counts": {"best": 0, "tie": 0, "insufficient": 0, "pending": 0},
        "groups": [],
    }
    controller(lambda _r: httpx.Response(200, json=doc))
    result = runner.invoke(app, ["bench", "labels", CAMP, "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["groups"] == []
    table = runner.invoke(app, ["bench", "labels", CAMP])
    assert table.exit_code == 0 and "0 groups" in table.stdout


def test_server_error_is_a_clean_failure_not_a_traceback(
    runner: CliRunner, controller: TransportFactory
) -> None:
    controller(lambda _r: _err(503, "unavailable"))
    result = runner.invoke(app, ["bench", "labels", CAMP])
    assert result.exit_code != 0
    assert "Traceback" not in result.output
