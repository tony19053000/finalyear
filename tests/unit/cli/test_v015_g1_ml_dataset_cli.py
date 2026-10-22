"""V0.15 G1: ``compute planner ml dataset`` (real client, faked transport).

* no ``--out``: the summary (lines or ``--json``) from ``?format=summary``;
* ``--out FILE``: the Parquet bytes into a new FILE, the digest header
  checked against the written bytes, then ``FILE.json`` = the summary -
  written only when the summary names the same digest;
* never overwrites FILE or FILE.json; ``--out`` + ``--summary`` is a usage
  error.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from mf_cli_support import TransportFactory
from typer.testing import CliRunner

from meshforge.cli.app import app
from meshforge.cli.planner_commands import ml_dataset_lines

pytestmark = pytest.mark.unit

BODY = b"PAR1" + b"m" * 1000 + b"PAR1"
DIGEST = "sha256:" + hashlib.sha256(BODY).hexdigest()


def _summary(digest: str = DIGEST, **extra: Any) -> dict[str, Any]:
    return {
        "dataset_version": 1,
        "feature_version": 1,
        "digest": digest,
        "bytes": len(BODY),
        "rows": 12,
        "max_runs": 50_000,
        "truncated": False,
        "counts": {
            "runs": 5,
            "runs_with_rows": 4,
            "rows": 12,
            "rows_by_term": {"compute": 4, "chunk_rate": 8},
            "rows_by_family": {"cpu.primes": 12},
            "skipped": {"missing_replay": 1, "measured": 0},
        },
        "dataset_schema": {
            "fingerprint": "f",
            "columns": [],
            "features": [],
            "terms": [],
        },
        **extra,
    }


def _handler(
    seen: list[httpx.Request],
    *,
    digest_header: str | None = DIGEST,
    summary: Any = None,
) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.params.get("format") == "summary":
            return httpx.Response(200, json=summary or _summary())
        headers = {"content-type": "application/vnd.apache.parquet"}
        if digest_header is not None:
            headers["x-meshforge-dataset-digest"] = digest_header
        return httpx.Response(200, content=BODY, headers=headers)

    return handler


def test_summary_lines_name_rows_digest_terms_and_skips(
    runner: CliRunner, controller: TransportFactory
) -> None:
    seen: list[httpx.Request] = []
    controller(_handler(seen))
    result = runner.invoke(app, ["planner", "ml", "dataset"])
    assert result.exit_code == 0, result.output
    assert "12 rows from 5 matched runs (4 with rows)" in result.stdout
    assert DIGEST in result.stdout
    assert "rows per term: compute 4, chunk_rate 8" in result.stdout
    assert "skipped: missing_replay 1" in result.stdout  # zero counts hidden
    (request,) = seen
    assert request.url.path == "/v1/planner/ml/dataset"
    assert request.url.params["format"] == "summary"


def test_truncated_is_said() -> None:
    lines = ml_dataset_lines(_summary(truncated=True, max_runs=10))
    assert any("only the newest 10 runs" in line for line in lines)


def test_out_writes_the_file_and_the_sidecar(
    runner: CliRunner, controller: TransportFactory, tmp_path: Path
) -> None:
    seen: list[httpx.Request] = []
    controller(_handler(seen))
    out = tmp_path / "ds.parquet"
    result = runner.invoke(
        app, ["planner", "ml", "dataset", "--out", str(out), "--json"]
    )
    assert result.exit_code == 0, result.output
    assert out.read_bytes() == BODY
    sidecar = tmp_path / "ds.parquet.json"
    assert json.loads(sidecar.read_text(encoding="utf-8")) == _summary()
    assert sidecar.read_text(encoding="utf-8") == (
        json.dumps(_summary(), sort_keys=True, indent=2) + "\n"
    )
    doc = json.loads(result.stdout)
    assert doc["file"] == str(out) and doc["sidecar"] == str(sidecar)
    assert doc["digest"] == DIGEST and doc["bytes"] == len(BODY) and doc["rows"] == 12
    assert [r.url.params.get("format") for r in seen] == [None, "summary"]


def test_a_corrupt_download_is_removed(
    runner: CliRunner, controller: TransportFactory, tmp_path: Path
) -> None:
    controller(_handler([], digest_header="sha256:" + "0" * 64))
    out = tmp_path / "ds.parquet"
    result = runner.invoke(app, ["planner", "ml", "dataset", "-o", str(out)])
    assert result.exit_code == 1
    assert not out.exists()
    assert "does not match its digest" in result.stderr


def test_a_dataset_that_changed_in_between_keeps_the_file_but_writes_no_sidecar(
    runner: CliRunner, controller: TransportFactory, tmp_path: Path
) -> None:
    controller(_handler([], summary=_summary(digest="sha256:" + "1" * 64)))
    out = tmp_path / "ds.parquet"
    result = runner.invoke(app, ["planner", "ml", "dataset", "-o", str(out)])
    assert result.exit_code == 1
    assert out.read_bytes() == BODY
    assert not (tmp_path / "ds.parquet.json").exists()
    assert "changed between the file and its summary" in result.stderr


@pytest.mark.parametrize("existing", ["ds.parquet", "ds.parquet.json"])
def test_never_overwrites(
    runner: CliRunner, controller: TransportFactory, tmp_path: Path, existing: str
) -> None:
    seen: list[httpx.Request] = []
    controller(_handler(seen))
    (tmp_path / existing).write_text("mine", encoding="utf-8")
    result = runner.invoke(
        app, ["planner", "ml", "dataset", "-o", str(tmp_path / "ds.parquet")]
    )
    assert result.exit_code == 2
    assert (tmp_path / existing).read_text(encoding="utf-8") == "mine"
    assert seen == []


def test_out_and_summary_exclude_each_other(
    runner: CliRunner, controller: TransportFactory, tmp_path: Path
) -> None:
    controller(_handler([]))
    result = runner.invoke(
        app, ["planner", "ml", "dataset", "-o", str(tmp_path / "x"), "--summary"]
    )
    assert result.exit_code == 2
