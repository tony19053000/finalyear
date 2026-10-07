"""V0.6 G4 tester round 1: ``compute network`` / ``compute explain`` under a
hostile or broken controller answer - terminal escapes, odd types, controller
down, ``--json`` stability, and a non-numeric ``score`` in a stored decision."""

from __future__ import annotations

import json
import math
from typing import Any

import httpx
import pytest
from mf_cli_support import TransportFactory
from typer.testing import CliRunner

from meshforge.cli.app import app
from meshforge.cli.explain_commands import _candidates_table
from meshforge.cli.network_commands import (
    format_age,
    format_rate,
    format_rtt,
    matrix_table,
)
from meshforge.common.exit_codes import ExitCode

A = "node_01J0000000000000000000AAAA"
BIDI = chr(0x202E)
EVIL = "\x1b]0;pwned\x07\x1b[2J\x1b[31m\x9b31m\r" + BIDI + "\x00evil"

PROBER = {
    "enabled": True,
    "started": 1,
    "measured": 1,
    "failed": 0,
    "timed_out": 0,
    "dropped": 0,
    "in_flight": [],
    "budget_bytes_per_min": 1,
    "budget_used_bytes": 1,
}


def serve(payload: Any) -> Any:
    return lambda request: httpx.Response(200, json=payload)


def assert_clean(text: str) -> None:
    for bad in ("\x1b", "\x07", "\x9b", "\x00", BIDI, "\r"):
        assert bad not in text, repr(bad)


def test_hostile_strings_never_reach_the_terminal(
    runner: CliRunner, controller: TransportFactory
) -> None:
    link = {
        "src_node_id": EVIL + A,
        "dst_node_id": A + EVIL,
        "bw_bytes_s": 1e6,
        "rtt_ms": 1.0,
        "age_s": 1,
        "stale": False,
        "last_failure": {"error": EVIL},
    }
    payload = {
        "stale_after_s": 3600,
        "links": [link],
        "controller_links": [
            {"node_id": EVIL, "bw_bytes_s": 1e6, "samples": EVIL, "age_s": 1}
        ],
        "failures": [{"src_node_id": EVIL, "dst_node_id": A, "error": EVIL}],
        "prober": {**PROBER, "started": EVIL, "enabled": EVIL},
    }
    controller(serve(payload))
    result = runner.invoke(app, ["network"])
    assert result.exit_code == 0, result.output
    assert_clean(result.output)


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf, 1e308, 5e-324, 0])
def test_formatters_accept_any_float(value: float) -> None:
    for fmt in (format_rate, format_age, format_rtt):
        assert isinstance(fmt(value), str)


def test_matrix_table_survives_every_junk_shape() -> None:
    for junk in (
        {},
        {"links": None},
        {"links": [[], 3, None, "x"], "failures": "no"},
        {"links": [{"src_node_id": None, "dst_node_id": 5}]},
        {"links": [{"src_node_id": A, "dst_node_id": A}]},  # self pair
        {"failures": [{"src_node_id": A}]},
    ):
        matrix_table(junk)


def test_json_output_is_stable_and_sorted(
    runner: CliRunner, controller: TransportFactory
) -> None:
    payload = {"b": 1, "links": [], "a": {"z": 1, "y": 2}, "prober": PROBER}
    controller(serve(payload))
    first = runner.invoke(app, ["network", "--json"])
    second = runner.invoke(app, ["network", "--json"])
    assert first.exit_code == 0 and first.output == second.output
    assert json.loads(first.output) == payload
    keys = list(json.loads(first.output))
    assert keys == sorted(keys) and first.output.index('"a"') < first.output.index(
        '"b"'
    )


def test_controller_down_is_unavailable_not_a_traceback(
    runner: CliRunner, controller: TransportFactory
) -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    controller(down)
    for args in (["network"], ["network", "--json"]):
        result = runner.invoke(app, args)
        assert result.exit_code == int(ExitCode.UNAVAILABLE), result.output
        assert "Traceback" not in result.output


@pytest.mark.parametrize("status", [401, 403, 404, 500, 503])
def test_http_errors_exit_non_zero_without_a_traceback(
    runner: CliRunner, controller: TransportFactory, status: int
) -> None:
    controller(lambda request: httpx.Response(status, json={"detail": EVIL}))
    result = runner.invoke(app, ["network"])
    assert result.exit_code != 0
    assert "Traceback" not in result.output
    assert_clean(result.output)


def test_a_non_json_answer_is_not_a_traceback(
    runner: CliRunner, controller: TransportFactory
) -> None:
    controller(lambda request: httpx.Response(200, text="<html>proxy</html>"))
    result = runner.invoke(app, ["network"])
    assert result.exit_code != 0 and "Traceback" not in result.output


def test_an_empty_fabric_says_so(
    runner: CliRunner, controller: TransportFactory
) -> None:
    controller(
        serve({"links": [], "controller_links": [], "failures": [], "prober": PROBER})
    )
    result = runner.invoke(app, ["network"])
    assert result.exit_code == 0 and "no node pair measured yet" in result.output


@pytest.mark.parametrize("score", ["fast", [1], {"a": 1}])
def test_explain_survives_a_non_numeric_score(score: Any) -> None:
    decision = {
        "id": "1",
        "attempt_no": 1,
        "outcome": "placed",
        "policy": "cost_model",
        "candidates": [
            {"rank": 1, "node_id": A, "mode": "offload", "slot": "cpu", "score": score}
        ],
    }
    _candidates_table(decision)
