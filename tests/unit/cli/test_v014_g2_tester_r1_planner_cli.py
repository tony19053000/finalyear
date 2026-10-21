"""V0.14 G2 tester round 1 - ``compute planner calibrate|calibration``:
--json passthrough, error exit codes, hostile/odd server documents."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from mf_cli_support import TransportFactory
from typer.testing import CliRunner

from meshforge.cli import app as cli_app

pytestmark = pytest.mark.unit


def _err(code: str, status: int) -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": code, "message": "nope"}})


def test_json_flag_prints_the_api_document_for_both_commands(
    runner: CliRunner, controller: TransportFactory
) -> None:
    doc = {
        "matched_runs": 0,
        "created": None,
        "skipped": "too_few_runs",
        "superseded_version": None,
        "in_use": {"mode": "auto", "pinned_version": None, "in_use_version": None},
    }
    controller(lambda _r: httpx.Response(200, json=doc))
    r = runner.invoke(cli_app.app, ["planner", "calibrate", "--json"])
    assert r.exit_code == 0, r.output
    assert {
        k: v for k, v in json.loads(r.stdout).items() if k != "schema_version"
    } == doc
    view = {
        "in_use": doc["in_use"],
        "active_version": None,
        "calibration": None,
        "term_phase": {},
        "not_calibrated": {},
        "recent": [],
    }
    controller(lambda _r: httpx.Response(200, json=view))
    r = runner.invoke(cli_app.app, ["planner", "calibration", "--json"])
    assert r.exit_code == 0, r.output
    assert {
        k: v for k, v in json.loads(r.stdout).items() if k != "schema_version"
    } == view


@pytest.mark.parametrize(
    ("status", "code", "exit_code"),
    [
        (401, "unauthorized", 3),
        (403, "unauthorized", 3),
        (404, "not_found", None),
        (503, "network_error", None),
    ],
)
def test_error_responses_exit_nonzero_with_nothing_on_stdout(
    runner: CliRunner,
    controller: TransportFactory,
    status: int,
    code: str,
    exit_code: int | None,
) -> None:
    controller(lambda _r: _err(code, status))
    for args in (
        ["planner", "calibrate"],
        ["planner", "calibration", "--version", "5"],
    ):
        r = runner.invoke(cli_app.app, args)
        assert r.exit_code != 0, (args, status)
        if exit_code is not None:
            assert r.exit_code == exit_code
        assert r.stdout == ""
        assert "Traceback" not in (r.output or "")


def test_bad_version_values_are_usage_errors(
    runner: CliRunner, controller: TransportFactory
) -> None:
    called: list[Any] = []
    controller(lambda r: called.append(r) or httpx.Response(200, json={}))
    for v in ("0", "-2", "abc", "1.5", ""):
        r = runner.invoke(cli_app.app, ["planner", "calibration", "--version", v])
        assert r.exit_code == 2, (v, r.output)
    assert called == []


@pytest.mark.parametrize(
    "version",
    [
        5,
        "\x1b[31mV",  # T2 (fixed in G2 fix round 1): the title is scrubbed
    ],
)
def test_odd_documents_do_not_crash_the_renderer(
    runner: CliRunner, controller: TransportFactory, version: Any
) -> None:
    hostile = {
        "version": version,
        "status": None,
        "created_at": 5,
        "holdout_error_new": "x",
        "holdout_error_active": True,
        "reason": "r\x1b]0;title\x07",
        "fit_inputs": [],
        "readable": False,
        "factors": [
            None,
            3,
            {
                "term": "\x1b[2Jcompute",
                "key_level": "node_family",
                "node_id": None,
                "family": "f\x1b[0m",
                "factor": "NaN",
                "n": None,
                "median": [],
            },
        ],
    }
    controller(
        lambda _r: httpx.Response(
            200,
            json={
                "matched_runs": None,
                "created": hostile,
                "skipped": None,
                "superseded_version": "x",
                "in_use": 3,
            },
        )
    )
    r = runner.invoke(cli_app.app, ["planner", "calibrate"])
    assert r.exit_code == 0, r.output
    assert "\x1b" not in r.stdout and "\x07" not in r.stdout
    controller(
        lambda _r: httpx.Response(
            200,
            json={
                "in_use": None,
                "active_version": None,
                "calibration": hostile,
                "term_phase": None,
                "not_calibrated": {"\x1b[1mq": "\x1b[0mm"},
                "recent": None,
            },
        )
    )
    r = runner.invoke(cli_app.app, ["planner", "calibration"])
    assert r.exit_code == 0, r.output
    assert "\x1b" not in r.stdout


def test_planner_without_subcommand_shows_help(runner: CliRunner) -> None:
    r = runner.invoke(cli_app.app, ["planner"])
    assert "calibrate" in r.output and "calibration" in r.output
