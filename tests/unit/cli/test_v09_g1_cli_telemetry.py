"""V0.9 G1 item 5: ``compute telemetry export`` (real client, faked transport)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from mf_cli_support import TransportFactory
from typer.testing import CliRunner

from meshforge.cli.app import app

pytestmark = pytest.mark.unit

BODY = b"PAR1" + b"x" * 300_000 + b"PAR1"


def test_export_writes_the_file_and_passes_the_filters(
    runner: CliRunner, controller: TransportFactory, tmp_path: Path
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            content=BODY,
            headers={
                "content-type": "application/vnd.apache.parquet",
                "x-meshforge-telemetry-rows": "7",
            },
        )

    controller(handler)
    out = tmp_path / "data.parquet"
    result = runner.invoke(
        app,
        [
            "telemetry",
            "export",
            "-o",
            str(out),
            "--campaign",
            "camp_1",
            "--since",
            "2026-10-17T00:00:00+00:00",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    assert out.read_bytes() == BODY
    assert json.loads(result.output) == {
        "file": str(out),
        "bytes": len(BODY),
        "rows": 7,
        "schema_version": 1,
    }
    (request,) = seen
    assert request.url.path == "/v1/telemetry/export"
    assert request.url.params["campaign"] == "camp_1"
    assert request.url.params["since"] == "2026-10-17T00:00:00+00:00"


def test_an_existing_file_is_never_overwritten(
    runner: CliRunner, controller: TransportFactory, tmp_path: Path
) -> None:
    controller(lambda _r: httpx.Response(200, content=BODY))
    out = tmp_path / "keep.parquet"
    out.write_bytes(b"mine")
    result = runner.invoke(app, ["telemetry", "export", "-o", str(out)])
    assert result.exit_code == 2
    assert out.read_bytes() == b"mine"


@pytest.mark.parametrize("since", ["yesterday", "2026-10-17T00:00:00"])
def test_a_bad_since_is_a_usage_error(
    runner: CliRunner, controller: TransportFactory, tmp_path: Path, since: str
) -> None:
    controller(lambda _r: httpx.Response(200, content=BODY))
    result = runner.invoke(
        app, ["telemetry", "export", "-o", str(tmp_path / "x"), "--since", since]
    )
    assert result.exit_code == 2
    assert not (tmp_path / "x").exists()


def test_a_refused_export_leaves_no_file(
    runner: CliRunner, controller: TransportFactory, tmp_path: Path
) -> None:
    controller(
        lambda _r: httpx.Response(
            403, json={"error": {"code": "unauthorized", "message": "admin only"}}
        )
    )
    out = tmp_path / "data.parquet"
    result = runner.invoke(app, ["telemetry", "export", "-o", str(out)])
    assert result.exit_code != 0
    assert "admin only" in result.output
    assert not out.exists()
