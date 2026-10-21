"""V0.14 G2: ``compute planner calibrate`` / ``compute planner calibration``.

The human views: a created version (status, fit inputs, held-out errors, the
rejection reason, the per-term factor table with key, level, factor, n and
median), "nothing written" with the reason, what the planner uses now;
``--version`` reaches the query; server strings are scrubbed; an error is
one stderr line with the documented exit code.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from mf_cli_support import TransportFactory
from typer.testing import CliRunner

from meshforge.cli import app as cli_app

pytestmark = pytest.mark.unit

NODE = "node_01AAAAAAAAAAAAAAAAAAAAAAAA"


def _version(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "version": 12,
        "created_at": "2026-10-21T10:00:00Z",
        "status": "active",
        "holdout_error_new": 0.1234,
        "holdout_error_active": 0.2,
        "reason": None,
        "fit_inputs": {
            "pairs": 40,
            "held_out": 10,
            "window": 200,
            "k": 5.0,
            "clamp": [0.2, 5.0],
            "active_version": 11,
        },
        "readable": True,
        "factors": [
            {
                "term": "compute",
                "key_level": "global",
                "node_id": None,
                "family": None,
                "factor": 1.3,
                "n": 40,
                "median": 1.4,
            },
            {
                "term": "compute",
                "key_level": "node_family",
                "node_id": NODE,
                "family": "matmul",
                "factor": 1.6,
                "n": 6,
                "median": 1.8,
            },
        ],
        "families": {},
    }
    base.update(over)
    return base


IN_USE = {"mode": "auto", "pinned_version": None, "in_use_version": 12}


def test_calibrate_prints_the_new_version_and_its_factors(
    runner: CliRunner, controller: TransportFactory
) -> None:
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        return httpx.Response(
            200,
            json={
                "matched_runs": 50,
                "created": _version(),
                "skipped": None,
                "superseded_version": 11,
                "in_use": IN_USE,
            },
        )

    controller(handler)
    result = runner.invoke(cli_app.app, ["planner", "calibrate"])
    assert result.exit_code == 0, result.output
    assert seen == [("POST", "/v1/planner/calibrate")]
    out = result.stdout
    assert "supersedes version 11" in out
    assert "version 12: active" in out
    assert "fitted on 40 runs, 10 held out" in out
    assert "new 0.1234 vs active 0.2000 (v11)" in out
    assert "x 1.600" in out and "AAAAAAAA / matmul" in out and "node_family" in out
    assert "planner uses: version 12 (auto)" in out


def test_calibrate_says_why_nothing_was_written(
    runner: CliRunner, controller: TransportFactory
) -> None:
    controller(
        lambda _r: httpx.Response(
            200,
            json={
                "matched_runs": 1,
                "created": None,
                "skipped": "too_few_runs",
                "superseded_version": None,
                "in_use": {
                    "mode": "off",
                    "pinned_version": None,
                    "in_use_version": None,
                },
            },
        )
    )
    result = runner.invoke(cli_app.app, ["planner", "calibrate"])
    assert result.exit_code == 0, result.output
    assert "nothing written: too_few_runs (1 matched runs)" in result.stdout
    assert "calibration = off" in result.stdout


def test_a_rejected_version_shows_its_reason(
    runner: CliRunner, controller: TransportFactory
) -> None:
    rejected = _version(
        status="rejected",
        reason="holdout_error 0.3 > 1.05 x active 0.2\x1b[31m",
    )
    controller(
        lambda _r: httpx.Response(
            200,
            json={
                "matched_runs": 50,
                "created": rejected,
                "skipped": None,
                "superseded_version": None,
                "in_use": IN_USE,
            },
        )
    )
    result = runner.invoke(cli_app.app, ["planner", "calibrate"])
    assert result.exit_code == 0, result.output
    assert "rejected: holdout_error 0.3 > 1.05 x active 0.2" in result.stdout
    assert "\x1b" not in result.stdout


def test_calibration_with_a_version_and_without_any(
    runner: CliRunner, controller: TransportFactory
) -> None:
    seen: list[str] = []
    answers = iter(
        [
            {
                "in_use": {"mode": "pinned", "pinned_version": 3, "in_use_version": 3},
                "active_version": 12,
                "calibration": _version(version=3, status="superseded"),
                "term_phase": {"compute": "run"},
                "not_calibrated": {"queue": "measured", "tail": "no_phase"},
                "recent": [],
            },
            {
                "in_use": {
                    "mode": "auto",
                    "pinned_version": None,
                    "in_use_version": None,
                },
                "active_version": None,
                "calibration": None,
                "term_phase": {},
                "not_calibrated": {},
                "recent": [],
            },
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json=next(answers))

    controller(handler)
    result = runner.invoke(cli_app.app, ["planner", "calibration", "--version", "3"])
    assert result.exit_code == 0, result.output
    assert seen[0].endswith("/v1/planner/calibration?version=3")
    assert "version 3: superseded" in result.stdout
    assert "never scaled: queue (measured), tail (no_phase)" in result.stdout
    assert "planner uses: pinned version 3" in result.stdout
    result = runner.invoke(cli_app.app, ["planner", "calibration"])
    assert result.exit_code == 0, result.output
    assert seen[1].endswith("/v1/planner/calibration")
    assert "no active calibration version" in result.stdout
    assert "no calibration yet" in result.stdout


def test_errors_are_one_stderr_line_with_the_exit_code(
    runner: CliRunner, controller: TransportFactory
) -> None:
    controller(
        lambda _r: httpx.Response(
            403, json={"error": {"code": "unauthorized", "message": "admin only"}}
        )
    )
    result = runner.invoke(cli_app.app, ["planner", "calibrate"])
    assert result.exit_code == 3
    assert result.stdout == ""
    result = runner.invoke(cli_app.app, ["planner", "calibration", "--version", "0"])
    assert result.exit_code == 2  # usage: --version >= 1


def test_explain_shows_a_calibrated_term_with_its_factor() -> None:
    from meshforge.cli.explain_commands import _term_cell

    term = {
        "seconds": 15.0,
        "source": "history",
        "detail": {
            "samples": 5,
            "calibration": {
                "factor": 1.5,
                "n": 14,
                "key_level": "node",
                "version": 3,
                "raw_s": 10.0,
            },
        },
    }
    assert _term_cell(term) == "15.00s (history, x1.50 calibrated (n=14, node))"
    plain = {"seconds": 1.0, "source": "assumed", "detail": {}}
    assert _term_cell(plain) == "1.00s (assumed)"
    odd = {
        "seconds": 1.0,
        "source": "history",
        "detail": {"calibration": {"factor": "x"}},
    }
    assert _term_cell(odd) == "1.00s (history)"
