"""V0.15 G6b tester: ``compute planner eval`` polling under hostile servers.

Derived from brief section 5h D378 ("CLI polls with --timeout") and the
lab checklist, which tells the team to run this for up to an hour.
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx
import pytest
from mf_cli_support import TransportFactory
from typer.testing import CliRunner

from meshforge.cli import app as cli_app
from meshforge.cli import planner_commands

pytestmark = pytest.mark.unit

CAMP = "camp_01AAAAAAAAAAAAAAAAAAAAAAAA"


@pytest.fixture(autouse=True)
def _fast_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(planner_commands, "_EVAL_POLL_S", 0.01)


def _running() -> dict[str, Any]:
    return {
        "id": 9,
        "created_at": "2026-10-23T10:00:00Z",
        "campaign_ids": [CAMP],
        "holdout": None,
        "leaked": False,
        "status": "running",
        "reason": None,
        "detail": None,
        "report": None,
    }


def _start_then(get: Any) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202, json={"report_id": 9, "status": "running"})
        return get(request)

    return handler


def test_one_503_during_a_long_poll_does_not_abandon_the_evaluation(
    runner: CliRunner, controller: TransportFactory
) -> None:
    """The lab run polls for up to an hour; the evaluation keeps running on the
    controller, and the API says 503 + Retry-After for a database blip. One
    blip must not turn the whole wait into a failure."""
    calls = [0]

    def get(_r: httpx.Request) -> httpx.Response:
        calls[0] += 1
        if calls[0] == 2:
            return httpx.Response(
                503,
                headers={"Retry-After": "2"},
                json={
                    "error": {
                        "code": "network_error",
                        "message": "the database is unavailable; try again",
                    }
                },
            )
        if calls[0] >= 4:
            return httpx.Response(
                200,
                json={
                    **_running(),
                    "status": "failed",
                    "reason": "crashed",
                    "detail": "x",
                },
            )
        return httpx.Response(200, json=_running())

    controller(_start_then(get))
    result = runner.invoke(cli_app.app, ["planner", "eval", "--campaign", CAMP])
    # expected: the CLI keeps polling past the blip and reaches the failed line
    assert "evaluation report 9 failed: crashed" in result.stdout, (
        calls[0],
        result.output,
    )


def test_a_report_that_vanishes_mid_poll_is_a_clear_error(
    runner: CliRunner, controller: TransportFactory
) -> None:
    calls = [0]

    def get(_r: httpx.Request) -> httpx.Response:
        calls[0] += 1
        if calls[0] == 1:
            return httpx.Response(200, json=_running())
        return httpx.Response(
            404,
            json={
                "error": {
                    "code": "not_found",
                    "message": "no evaluation report 9",
                    "reason": "unknown_eval_report",
                }
            },
        )

    controller(_start_then(get))
    result = runner.invoke(cli_app.app, ["planner", "eval", "--campaign", CAMP])
    assert result.exit_code != 0
    assert "Traceback" not in result.output
    assert "no evaluation report 9" in result.output


def test_an_unknown_status_does_not_spin_forever_or_print_a_report(
    runner: CliRunner, controller: TransportFactory
) -> None:
    def get(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={**_running(), "status": "exploded"})

    controller(_start_then(get))
    result = runner.invoke(cli_app.app, ["planner", "eval", "--campaign", CAMP])
    assert result.exit_code == 1, result.output
    assert "Traceback" not in result.output


@pytest.mark.parametrize(
    "value",
    [
        "nan",
        "-5",
        "0.5",
        "abc",
        "",
    ],
)
def test_nonsense_timeouts_are_usage_errors_not_endless_waits(
    runner: CliRunner, controller: TransportFactory, value: str
) -> None:
    """``--timeout nan`` must not become an unbounded wait (every comparison
    with NaN is false, so the deadline would never fire)."""
    seen = [0]

    def get(_r: httpx.Request) -> httpx.Response:
        seen[0] += 1
        return httpx.Response(200, json=_running())

    controller(_start_then(get))
    t0 = time.monotonic()
    args = ["planner", "eval", "--campaign", CAMP, "--timeout", value]
    if value == "nan":
        # bounded harness: the CLI would spin forever; cap with a poll counter
        def get_bounded(_r: httpx.Request) -> httpx.Response:
            seen[0] += 1
            if seen[0] > 300:
                raise RuntimeError("endless wait")
            return httpx.Response(200, json=_running())

        controller(_start_then(get_bounded))
    result = runner.invoke(cli_app.app, args)
    assert result.exit_code in (1, 2), result.output
    assert seen[0] <= 300, "the CLI polled without bound"
    assert time.monotonic() - t0 < 20


def test_timeout_larger_than_the_default_is_accepted_and_json_failure_exit_1(
    runner: CliRunner, controller: TransportFactory
) -> None:
    def get(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                **_running(),
                "status": "failed",
                "reason": "invalid_input",
                "detail": "no group of family 'x'",
            },
        )

    controller(_start_then(get))
    result = runner.invoke(
        cli_app.app,
        ["planner", "eval", "--campaign", CAMP, "--timeout", "99999", "--json"],
    )
    assert result.exit_code == 1
    assert json.loads(result.stdout)["reason"] == "invalid_input"


def test_hostile_detail_with_control_characters_is_neutralised(
    runner: CliRunner, controller: TransportFactory
) -> None:
    def get(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                **_running(),
                "status": "failed",
                "reason": "crashed",
                "detail": "\x1b[2J\x1b]0;pwned\x07evil",
            },
        )

    controller(_start_then(get))
    result = runner.invoke(cli_app.app, ["planner", "eval", "--campaign", CAMP])
    assert result.exit_code == 1
    assert "\x1b" not in result.stdout
