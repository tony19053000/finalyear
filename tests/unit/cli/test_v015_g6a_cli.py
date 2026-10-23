"""V0.15 G6a: ``compute planner replay ID`` and ``compute planner ml
classify``.

* ``replay``: the request path, the human lines (status, model set, chosen
  nodes, differences), server strings scrubbed, exit 0 for any answer and
  with ``--check`` 1 unless ``reproduced``, ``--json`` = the API document;
* ``classify``: exactly one of ``--campaign`` / ``--examples`` (usage error
  before any request); ``--campaign`` POSTs the ids, ``--save-examples``
  keeps the document, the real child runs and the section prints; a
  too-small document says ``too_few_groups`` and prints no table or
  accuracy; ``--examples`` runs offline (no request).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from mf_cli_support import TransportFactory
from typer.testing import CliRunner

from meshforge.cli import app as cli_app
from meshforge.cli.replay_commands import replay_lines

pytestmark = pytest.mark.unit


def _replay(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "decision_id": 42,
        "job_id": "job_x",
        "outcome": "dispatched",
        "policy": "cost_model",
        "status": "reproduced",
        "ml_model_version": "sha256:" + "a" * 64,
        "stored_chosen_node_id": "node_b",
        "replayed_chosen_node_id": "node_b",
        "snapshot_digest_match": True,
        "differences": [],
        "differences_total": 0,
    }
    base.update(over)
    return base


def test_replay_reproduced_is_exit_0(
    runner: CliRunner, controller: TransportFactory
) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url.path}")
        return httpx.Response(200, json=_replay())

    controller(handler)
    result = runner.invoke(cli_app.app, ["planner", "replay", "42", "--check"])
    assert result.exit_code == 0, result.output
    assert seen == ["GET /v1/planner/decisions/42/replay"]
    assert "decision 42 (job job_x, dispatched, policy cost_model): reproduced" in (
        result.stdout
    )
    assert "model set: sha256:" + "a" * 64 in result.stdout


@pytest.mark.parametrize(
    "status", ["differs", "ml_model_unavailable", "no_replay", "replay_unreadable"]
)
def test_replay_anything_else_is_exit_1_with_check(
    runner: CliRunner, controller: TransportFactory, status: str
) -> None:
    doc = _replay(
        status=status,
        differences=["candidates[0].rank: 1 != 2\x1b[31m"],
        differences_total=3,
    )
    controller(lambda _r: httpx.Response(200, json=doc))
    result = runner.invoke(cli_app.app, ["planner", "replay", "42"])
    assert result.exit_code == 0, result.output  # an answer is not a failure
    result = runner.invoke(cli_app.app, ["planner", "replay", "42", "--check"])
    assert result.exit_code == 1, result.output
    assert f": {status} - " in result.stdout
    assert "differences (3):" in result.stdout
    assert "\x1b[31m" not in result.stdout


def test_replay_json_is_the_api_document(
    runner: CliRunner, controller: TransportFactory
) -> None:
    controller(lambda _r: httpx.Response(200, json=_replay()))
    result = runner.invoke(cli_app.app, ["planner", "replay", "42", "--json"])
    assert result.exit_code == 0, result.output
    doc = json.loads(result.stdout)
    doc.pop("schema_version", None)
    assert doc == _replay()


def test_replay_lines_without_a_model() -> None:
    lines = replay_lines(_replay(ml_model_version=None))
    assert lines[1] == "model set: none (no ML)"


def test_replay_refuses_a_bad_id_before_any_request(
    runner: CliRunner, controller: TransportFactory
) -> None:
    seen: list[str] = []
    controller(lambda r: seen.append(r.url.path) or httpx.Response(200, json={}))
    for argv in (["planner", "replay", "0"], ["planner", "replay", "x"]):
        assert runner.invoke(cli_app.app, argv).exit_code == 2
    assert seen == []


def _examples(n: int) -> dict[str, Any]:
    return {
        "examples_version": 1,
        "campaign_ids": ["camp_1"],
        "policy": "cost_model:calibrated:min_time",
        "feature_names": ["x"],
        "counts": {"groups": n, "examples": n},
        "calibrations": {},
        "examples": [
            {
                "campaign_id": "camp_1",
                "group_id": f"g{i}",
                "family": f"fam{i % 2}",
                "fingerprint": None,
                "label": "local",
                "oracle_class": "local",
                "records_used": 1,
                "features": [float(i)],
                "classes": {
                    "local": {
                        "config_id": "c",
                        "status": "counted",
                        "regret_pct": 0.0,
                        "votes": {"c": 1},
                    }
                },
                "cost_model": {
                    "config_id": "c",
                    "status": "counted",
                    "regret_pct": 0.0,
                    "votes": {"c": 1},
                },
            }
            for i in range(n)
        ],
    }


def test_classify_needs_exactly_one_source(
    runner: CliRunner, controller: TransportFactory, tmp_path: Path
) -> None:
    seen: list[str] = []
    controller(lambda r: seen.append(r.url.path) or httpx.Response(200, json={}))
    path = tmp_path / "e.json"
    path.write_text("{}", encoding="utf-8")
    for argv in (
        ["planner", "ml", "classify"],
        ["planner", "ml", "classify", "--campaign", "c", "--examples", str(path)],
        ["planner", "ml", "classify", "--examples", str(tmp_path / "missing.json")],
        [
            "planner",
            "ml",
            "classify",
            "--examples",
            str(path),
            "--save-examples",
            str(tmp_path / "s.json"),
        ],
    ):
        result = runner.invoke(cli_app.app, argv)
        assert result.exit_code == 2, (argv, result.output)
    assert seen == []


def test_classify_fetches_runs_the_child_and_saves(
    runner: CliRunner, controller: TransportFactory, tmp_path: Path
) -> None:
    pytest.importorskip("lightgbm", reason="needs the ml extra")
    bodies: list[Any] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert (request.method, request.url.path) == (
            "POST",
            "/v1/planner/ml/classifier-examples",
        )
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=_examples(4))

    controller(handler)
    saved = tmp_path / "saved.json"
    out = tmp_path / "out"
    result = runner.invoke(
        cli_app.app,
        [
            "planner",
            "ml",
            "classify",
            "--campaign",
            "camp_b",
            "--campaign",
            "camp_a",
            "--save-examples",
            str(saved),
            "--out",
            str(out),
        ],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 0, result.output
    assert bodies == [{"campaign_ids": ["camp_a", "camp_b"]}]
    assert json.loads(saved.read_text(encoding="utf-8")) == _examples(4)
    assert "4 labelled group(s) in 2 family(ies) - too_few_groups" in result.stdout
    assert "accuracy" not in result.stdout
    report = json.loads((out / "classify.json").read_text(encoding="utf-8"))
    assert report["classifier"]["status"] == "too_few_groups"
    assert (out / "classify.md").is_file()


def test_classify_offline_from_a_saved_document(
    runner: CliRunner, controller: TransportFactory, tmp_path: Path
) -> None:
    pytest.importorskip("lightgbm", reason="needs the ml extra")
    seen: list[str] = []
    controller(lambda r: seen.append(r.url.path) or httpx.Response(500))
    path = tmp_path / "e.json"
    path.write_text(json.dumps(_examples(2)), encoding="utf-8")
    result = runner.invoke(
        cli_app.app, ["planner", "ml", "classify", "--examples", str(path), "--json"]
    )
    assert result.exit_code == 0, result.output
    assert seen == []
    doc = json.loads(result.stdout)
    assert doc["classifier"]["status"] == "too_few_groups"
