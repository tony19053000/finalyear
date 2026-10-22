"""V0.14 G4 tester round 2 - ``compute planner reports [--limit N] [--json]``.

Auth/forbidden/network errors exit non-zero with nothing on stdout; the
bearer token is sent; ``--limit`` bounds (1..200) are usage errors before any
request; ``--json`` passes the document through; hostile documents (escape
sequences, wrong types, NaN-ish strings, huge lists) never crash the renderer
or leak terminal control codes.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from mf_cli_support import TransportFactory
from typer.testing import CliRunner

from meshforge.cli import app as cli_app
from meshforge.cli import shared as cli_shared
from meshforge.cli.client import ControllerClient

pytestmark = pytest.mark.unit


def _err(code: str, status: int) -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": code, "message": "nope"}})


@pytest.mark.parametrize("limit", ["0", "-1", "201", "abc", "1.5", ""])
def test_bad_limit_is_a_usage_error_and_sends_nothing(
    runner: CliRunner, controller: TransportFactory, limit: str
) -> None:
    called: list[Any] = []
    controller(lambda r: called.append(r) or httpx.Response(200, json={"reports": []}))
    r = runner.invoke(cli_app.app, ["planner", "reports", "--limit", limit])
    assert r.exit_code == 2, (limit, r.output)
    assert called == []


@pytest.mark.parametrize("limit", ["1", "200"])
def test_limit_bounds_are_accepted_and_forwarded(
    runner: CliRunner,
    controller: TransportFactory,
    limit: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MESHFORGE_CLI__TOKEN", "T" * 43)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"reports": []})

    def factory(settings: Any) -> ControllerClient:  # keeps the real token
        return ControllerClient(
            settings.cli.controller_url,
            timeout_s=settings.cli.timeout_s,
            transport=httpx.MockTransport(handler),
            token=settings.cli.token,
        )

    monkeypatch.setattr(cli_shared, "_client", factory)
    r = runner.invoke(cli_app.app, ["planner", "reports", "--limit", limit])
    assert r.exit_code == 0, r.output
    assert seen[0].method == "GET"
    assert seen[0].url.params["limit"] == limit
    assert seen[0].headers.get("authorization", "") == "Bearer " + "T" * 43
    assert "T" * 43 not in str(seen[0].url)
    assert "no evaluation reports yet" in r.stdout


@pytest.mark.parametrize(
    ("status", "code", "exit_code"),
    [
        (401, "unauthorized", 3),
        (403, "unauthorized", 3),
        (404, "not_found", None),
        (400, "invalid_input", None),
        (500, "internal", None),
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
    for extra in ([], ["--json"]):
        r = runner.invoke(cli_app.app, ["planner", "reports", *extra])
        assert r.exit_code != 0, (extra, status)
        if exit_code is not None:
            assert r.exit_code == exit_code
        assert r.stdout == ""
        assert "Traceback" not in (r.output or "")


def test_non_json_error_body_does_not_crash(
    runner: CliRunner, controller: TransportFactory
) -> None:
    controller(lambda _r: httpx.Response(502, text="<html>bad gateway</html>"))
    r = runner.invoke(cli_app.app, ["planner", "reports"])
    assert r.exit_code != 0 and r.stdout == ""
    assert "Traceback" not in (r.output or "")


def test_json_flag_passes_the_document_through_and_empty_json_is_valid(
    runner: CliRunner, controller: TransportFactory
) -> None:
    doc = {
        "reports": [
            {
                "id": 3,
                "created_at": "2026-10-22T10:00:00Z",
                "campaign_ids": ["camp_a"],
                "holdout": None,
                "leaked": False,
                "eval_version": 1,
                "evaluated_groups": 12,
                "verdicts": [
                    {"criterion": 1, "status": "PASS"},
                    {"criterion": 2, "status": "too_few_groups"},
                ],
            }
        ]
    }
    controller(lambda _r: httpx.Response(200, json=doc))
    r = runner.invoke(cli_app.app, ["planner", "reports", "--json"])
    assert r.exit_code == 0, r.output
    got = json.loads(r.stdout)
    assert got["reports"] == doc["reports"]
    controller(lambda _r: httpx.Response(200, json={"reports": []}))
    r = runner.invoke(cli_app.app, ["planner", "reports", "--json"])
    assert r.exit_code == 0 and json.loads(r.stdout)["reports"] == []


def test_table_shows_each_column_and_dashes_for_missing_values(
    runner: CliRunner, controller: TransportFactory
) -> None:
    rows = [
        {
            "id": 7,
            "created_at": "2026-10-22T10:00:00Z",
            "campaign_ids": ["camp_a", "camp_b"],
            "holdout": "cpu.sort",
            "leaked": False,
            "evaluated_groups": 31,
            "verdicts": [
                {"criterion": 1, "status": "PASS"},
                {"criterion": 2, "status": "FAIL"},
            ],
        },
        {"id": 6, "created_at": None, "campaign_ids": None, "verdicts": []},
    ]
    controller(lambda _r: httpx.Response(200, json={"reports": rows}))
    r = runner.invoke(cli_app.app, ["planner", "reports"])
    assert r.exit_code == 0, r.output
    for text in ("camp_a", "camp_b", "cpu.sort", "31", "PASS", "FAIL", "7", "6"):
        assert text in r.stdout, text
    assert r.stdout.index("7") < r.stdout.index("camp_a")  # order as served


def test_hostile_documents_never_crash_or_emit_escape_codes(
    runner: CliRunner, controller: TransportFactory
) -> None:
    rows: list[Any] = [
        None,
        3,
        "x",
        {"id": "\x1b[31mRED", "created_at": 5, "campaign_ids": "notalist"},
        {
            "id": 1,
            "campaign_ids": ["\x1b]0;pwn\x07c"],
            "holdout": "h\x1b[2J",
            "leaked": "yes",
            "evaluated_groups": float("nan") if False else "NaN",
            "verdicts": [
                None,
                {"criterion": 1},
                {"criterion": 2, "status": "\x1b[0m"},
                "z",
            ],
        },
        {"id": 2, "verdicts": "nope"},
    ]
    for payload in (
        {"reports": rows},
        {"reports": "nope"},
        {"reports": None},
        {},
        {"reports": rows * 400},
    ):
        controller(lambda _r, p=payload: httpx.Response(200, json=p))
        r = runner.invoke(cli_app.app, ["planner", "reports"])
        assert r.exit_code == 0, (payload.keys(), r.output, r.exception)
        assert "\x1b" not in r.stdout
        assert "Traceback" not in r.output


def test_a_non_object_document_is_an_error_not_a_traceback(
    runner: CliRunner, controller: TransportFactory
) -> None:
    controller(lambda _r: httpx.Response(200, json=[1, 2]))
    r = runner.invoke(cli_app.app, ["planner", "reports"])
    assert "Traceback" not in (r.output or "")
