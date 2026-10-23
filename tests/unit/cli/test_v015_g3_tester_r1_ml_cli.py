"""V0.15 G3 tester r1 - ``compute planner ml models|train|pin|unpin`` under
hostile server answers and exit-code rules (CLI.md: 0 ok, 1 failure, 2
usage, 3 auth, 4 not found, 5/6 unavailable).
"""

from __future__ import annotations

import json

import httpx
import pytest
from mf_cli_support import TransportFactory
from typer.testing import CliRunner

from meshforge.cli import app as cli_app
from meshforge.cli import ml_model_commands

pytestmark = pytest.mark.unit

V = "sha256:" + "a" * 64


def _err(status: int, kind: str, message: str = "m") -> httpx.Response:
    return httpx.Response(
        status,
        json={"error": {"failure_type": kind, "message": message, "retryable": False}},
    )


@pytest.mark.parametrize(
    ("status", "kind", "expected"),
    [
        (401, "unauthorized", 3),
        (403, "forbidden", 3),
        (404, "not_found", None),  # CLI.md decides: 4 or 1; must be non-zero
        (409, "conflict", 1),
    ],
)
@pytest.mark.parametrize(
    "argv",
    [
        ["planner", "ml", "pin", V],
        ["planner", "ml", "unpin", V],
        ["planner", "ml", "train"],
        ["planner", "ml", "train", "--wait"],
        ["planner", "ml", "models"],
        ["planner", "ml", "models", "--set", V],
    ],
)
def test_error_statuses_map_to_exit_codes_without_a_traceback(
    runner: CliRunner,
    controller: TransportFactory,
    status: int,
    kind: str,
    expected: int | None,
    argv: list[str],
) -> None:
    controller(lambda _r: _err(status, kind, "refused \x1b[31mred"))
    result = runner.invoke(cli_app.app, argv)
    assert result.exit_code != 0, result.output
    if expected is not None:
        assert result.exit_code == expected, (argv, result.output)
    assert "Traceback" not in result.output
    assert "\x1b[31m" not in result.output  # server strings are scrubbed
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_server_down_is_a_clean_unavailable_exit(
    runner: CliRunner, controller: TransportFactory
) -> None:
    def refuse(_r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    controller(refuse)
    for argv in (
        ["planner", "ml", "models"],
        ["planner", "ml", "train"],
        ["planner", "ml", "pin", V],
    ):
        result = runner.invoke(cli_app.app, argv)
        assert result.exit_code not in (0, 2), (argv, result.output)
        assert "Traceback" not in result.output
        assert result.exception is None or isinstance(result.exception, SystemExit)


def test_503_with_retry_after_is_unavailable_not_a_crash(
    runner: CliRunner, controller: TransportFactory
) -> None:
    controller(
        lambda _r: httpx.Response(
            503,
            headers={"Retry-After": "2"},
            json={
                "error": {
                    "failure_type": "network_error",
                    "message": "the database is unavailable; try again",
                    "retryable": True,
                }
            },
        )
    )
    result = runner.invoke(cli_app.app, ["planner", "ml", "models"])
    assert result.exit_code not in (0, 2)
    assert "Traceback" not in result.output


HOSTILE = [
    {},
    [],
    "text",
    None,
    {"sets": None},
    {"sets": "x"},
    {"sets": [1, "x", None, [], {"members": 5}]},
    {"sets": [{"set_version": 5, "members": [None, 1, {"metrics": "x"}]}]},
    {"in_use": "x", "retrain": [], "sets": []},
    {
        "in_use": {"mode": {"a": 1}, "in_use_set": 5},
        "retrain": {"last": "x"},
        "sets": [],
    },
    {"retrain": {"available": False, "missing": "sklearn"}, "sets": []},
    {"retrain": {"running": True, "running_since": 7}, "sets": []},
    {"retrain": {"last": {"error": ["x"], "detail": {"a": 1}}}, "sets": []},
    {"retrain": {"last": {"created_set": 3, "status": None}}, "sets": []},
    {"sets": [{"members": [{"metrics": {"rows": float("1e999")}}]}]},
]


@pytest.mark.parametrize("body", range(len(HOSTILE)))
def test_hostile_models_documents_never_traceback(
    runner: CliRunner, controller: TransportFactory, body: int
) -> None:
    doc = HOSTILE[body]
    controller(lambda _r: httpx.Response(200, content=json.dumps(doc)))
    result = runner.invoke(cli_app.app, ["planner", "ml", "models"])
    assert "Traceback" not in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit), repr(
        result.exception
    )
    result = runner.invoke(cli_app.app, ["planner", "ml", "models", "--json"])
    assert result.exception is None or isinstance(result.exception, SystemExit), repr(
        result.exception
    )


@pytest.mark.parametrize("body", range(len(HOSTILE)))
def test_hostile_set_and_pin_documents_never_traceback(
    runner: CliRunner, controller: TransportFactory, body: int
) -> None:
    doc = HOSTILE[body]
    controller(lambda _r: httpx.Response(200, content=json.dumps(doc)))
    for argv in (
        ["planner", "ml", "models", "--set", V],
        ["planner", "ml", "pin", V],
        ["planner", "ml", "unpin", V],
    ):
        result = runner.invoke(cli_app.app, argv)
        assert result.exception is None or isinstance(result.exception, SystemExit), (
            argv,
            repr(result.exception),
        )


def test_non_json_and_empty_bodies(
    runner: CliRunner, controller: TransportFactory
) -> None:
    for content in (b"", b"<html>proxy error</html>", b"\xff\xfe", b"{"):
        controller(lambda _r, c=content: httpx.Response(200, content=c))
        for argv in (["planner", "ml", "models"], ["planner", "ml", "train"]):
            result = runner.invoke(cli_app.app, argv)
            assert result.exit_code != 0, (content, argv, result.output)
            assert "Traceback" not in result.output
            assert result.exception is None or isinstance(result.exception, SystemExit)


@pytest.mark.parametrize("limit", ["0", "-3", "201", "abc", "1.5"])
def test_models_limit_is_validated_before_any_request(
    runner: CliRunner, controller: TransportFactory, limit: str
) -> None:
    seen: list[str] = []
    controller(lambda r: seen.append(r.url.path) or httpx.Response(200, json={}))
    result = runner.invoke(cli_app.app, ["planner", "ml", "models", "--limit", limit])
    assert result.exit_code == 2
    assert seen == []


@pytest.mark.parametrize("timeout", ["0", "0.5", "-1", "x"])
def test_train_timeout_is_validated(
    runner: CliRunner, controller: TransportFactory, timeout: str
) -> None:
    seen: list[str] = []
    controller(lambda r: seen.append(r.url.path) or httpx.Response(200, json={}))
    result = runner.invoke(
        cli_app.app, ["planner", "ml", "train", "--wait", "--timeout", timeout]
    )
    assert result.exit_code == 2
    assert seen == []


def test_train_wait_that_never_finishes_times_out_with_exit_1(
    runner: CliRunner, controller: TransportFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ml_model_commands, "_POLL_S", 0.01)

    def handler(request: httpx.Request) -> httpx.Response:
        state = {
            "available": True,
            "missing": [],
            "running": True,
            "running_since": "2026-10-22T10:00:00Z",
            "retrains": 0,
            "failures": 0,
            "last": None,
            "interval_s": 86400.0,
        }
        if request.method == "POST":
            return httpx.Response(200, json={"started": True, "retrain": state})
        return httpx.Response(200, json=state)

    controller(handler)
    result = runner.invoke(
        cli_app.app, ["planner", "ml", "train", "--wait", "--timeout", "1"]
    )
    assert result.exit_code == 1, result.output
    assert "did not finish" in result.output
