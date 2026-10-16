"""V0.7 G6 deliverable 2: ``compute secret set | list | delete``.

The real :class:`ControllerClient` runs; only the transport is faked.

* ``set`` sends the value as the request body (never in the URL or a header),
  from stdin (one trailing newline removed) or ``--from-file`` (exact bytes);
  there is no option or argument that takes a value; output never shows it;
* bounds and name rules are checked before anything is sent;
* ``list`` prints names only; ``delete`` normalises the name.
"""

from __future__ import annotations

import re
from pathlib import Path

import httpx
from mf_cli_support import TransportFactory
from typer.testing import CliRunner

from meshforge.cli import app as cli_app
from meshforge.common.exit_codes import ExitCode

VALUE = b"cli-secret-value-42"


class Recorder:
    def __init__(self, response: dict[str, object]) -> None:
        self.requests: list[httpx.Request] = []
        self.response = response

    def __call__(self, request: httpx.Request) -> httpx.Response:
        request.read()
        self.requests.append(request)
        return httpx.Response(200, json=self.response)


def test_set_from_stdin(runner: CliRunner, controller: TransportFactory) -> None:
    rec = Recorder({"name": "API_KEY", "created": True})
    controller(rec)
    result = runner.invoke(
        cli_app.app, ["secret", "set", "api_key"], input=VALUE.decode() + "\n"
    )
    assert result.exit_code == 0, result.output
    assert "stored secret API_KEY" in result.output
    (request,) = rec.requests
    assert request.method == "PUT" and request.url.path == "/v1/secrets/API_KEY"
    assert request.content == VALUE  # one trailing newline removed
    assert VALUE.decode() not in str(request.url)
    assert all(VALUE.decode() not in v for v in request.headers.values())
    assert VALUE.decode() not in result.output


def test_set_from_file_is_exact(
    runner: CliRunner, controller: TransportFactory, tmp_path: Path
) -> None:
    rec = Recorder({"name": "K", "created": False})
    controller(rec)
    secret_file = tmp_path / "value.bin"
    secret_file.write_bytes(VALUE + b"\n\n")
    result = runner.invoke(
        cli_app.app, ["secret", "set", "K", "--from-file", str(secret_file)]
    )
    assert result.exit_code == 0, result.output
    assert "replaced secret K" in result.output
    assert rec.requests[0].content == VALUE + b"\n\n"


def test_no_value_from_argv(runner: CliRunner, controller: TransportFactory) -> None:
    rec = Recorder({})
    controller(rec)
    for argv in (
        ["secret", "set", "K", "the-value"],
        ["secret", "set", "K", "--value", "the-value"],
    ):
        result = runner.invoke(cli_app.app, argv)
        assert result.exit_code == ExitCode.USAGE_ERROR, argv
    assert rec.requests == []


def test_bounds_and_names_are_checked_first(
    runner: CliRunner, controller: TransportFactory
) -> None:
    rec = Recorder({})
    controller(rec)
    short = runner.invoke(cli_app.app, ["secret", "set", "K"], input="abc\n")
    assert short.exit_code == ExitCode.USAGE_ERROR and "at least 4" in short.output
    big = runner.invoke(cli_app.app, ["secret", "set", "K"], input="x" * 70000)
    assert big.exit_code == ExitCode.USAGE_ERROR
    bad = runner.invoke(cli_app.app, ["secret", "set", "9X"], input="abcdef\n")
    assert bad.exit_code == ExitCode.USAGE_ERROR
    assert rec.requests == []


def test_list_and_delete(runner: CliRunner, controller: TransportFactory) -> None:
    rec = Recorder(
        {
            "secrets": [
                {"name": "A", "created_at": "t0", "updated_at": "t1"},
                {"name": "B", "created_at": "t0", "updated_at": "t2"},
            ],
            "count": 2,
        }
    )
    controller(rec)
    listed = runner.invoke(cli_app.app, ["secret", "list"])
    # V0.8 G6: a table (NAME / CREATED / UPDATED) instead of "A  updated t1".
    assert listed.exit_code == 0 and "UPDATED" in listed.output
    assert re.search(r"\|A +\| t0 +\| t1 +\|", listed.output), listed.output
    rec.response = {"name": "DB_PASS", "deleted": True}
    gone = runner.invoke(cli_app.app, ["secret", "delete", "db_pass"])
    assert gone.exit_code == 0 and "deleted secret DB_PASS" in gone.output
    assert rec.requests[-1].method == "DELETE"
    assert rec.requests[-1].url.path == "/v1/secrets/DB_PASS"
