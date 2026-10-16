"""V0.8 G6 (D178) deliverables 4 + 5: one exit-code mapping, tested per command
class; every error is one line saying what / why / what to do; no traceback;
no secret on any output.

Command classes: list reads, single-object reads, writes. Each meets the same
failure matrix (401, 403, 404, 400, 409, 503, a non-JSON 500, connection
refused, timeout) and must exit with the documented code, print nothing on
stdout and exactly one ``compute: ...`` line on stderr.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from mf_cli_support import Handler
from pydantic import SecretStr
from typer.testing import CliRunner

from meshforge.cli import app as cli_app
from meshforge.cli import shared as cli_shared
from meshforge.cli.client import ControllerClient, ControllerError
from meshforge.cli.exits import CliExit, exit_code_for, job_exit_code
from meshforge.cli.settings import CliSettings
from meshforge.common.errors import FailureType
from meshforge.common.exit_codes import ExitCode

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
NODE = "node_01J0000000000000000000BBBB"
JOB = "job_01J0000000000000000000CCCC"
INVITE = "inv_01J0000000000000000000AAAA"
TOKEN_ID = "tok_01J0000000000000000000DDDD"
TOKEN = "mfat_" + "Q" * 43
_TRACEBACK = ("Traceback", 'File "', "httpx.", "ControllerError", "site-packages")

LIST_READS = [
    ["jobs"],
    ["tokens", "list"],
    ["secret", "list"],
    ["data", "ls"],
    ["network"],
    ["whoami"],
    ["fabric", "invites"],
    ["nodes"],
]
ONE_READS = [
    ["nodes", NODE],
    ["jobs", JOB],
    ["explain", JOB],
    ["data", "show", "ds"],
]
WRITES = [
    ["cancel", JOB],
    ["secret", "delete", "DB_PASS"],
    ["tokens", "revoke", TOKEN_ID],
    ["data", "rm", "ds@1"],
    ["data", "pin", "ds@1"],
    ["node", "revoke", NODE],
    ["node", "gc", NODE],
    ["fabric", "invites", "--revoke", INVITE],
    ["fabric", "invite"],
]
COMMANDS = [
    pytest.param(c, id=f"{cls}:{' '.join(c)}")
    for cls, group in (("list", LIST_READS), ("one", ONE_READS), ("write", WRITES))
    for c in group
]


def _error(status: int, code: str, message: str) -> Handler:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status, json={"error": {"code": code, "message": message}}
        )

    return handler


def _raise(exc: Exception) -> Handler:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise exc

    return handler


def _html_500(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(500, text="<html>Bad Gateway</html>")


# (failure, handler, exit code, a "what to do" fragment the line must contain)
FAILURES: list[tuple[str, Handler, int, str]] = [
    ("401", _error(401, "unauthorized", "x"), CliExit.AUTH, "run `compute login`"),
    (
        "403",
        _error(403, "unauthorized", "admins only"),
        CliExit.AUTH,
        "ask a fabric admin",
    ),
    (
        "404",
        _error(404, "not_found", "no such thing"),
        CliExit.NOT_FOUND,
        "check the id",
    ),
    (
        "400",
        _error(400, "invalid_input", "bad value"),
        CliExit.USAGE_ERROR,
        "bad value",
    ),
    (
        "409",
        _error(409, "conflict", "in use by a job"),
        CliExit.FAILURE,
        "in use by a job",
    ),
    ("503", _error(503, "network_error", "db down"), CliExit.UNAVAILABLE, "retry"),
    ("500-html", _html_500, CliExit.FAILURE, "HTTP 500"),
    (
        "refused",
        _raise(httpx.ConnectError("refused")),
        CliExit.UNAVAILABLE,
        "`compute status`",
    ),
    ("timeout", _raise(httpx.ReadTimeout("slow")), CliExit.UNAVAILABLE, "timeout_s"),
]


@pytest.fixture
def with_token(monkeypatch: pytest.MonkeyPatch) -> Callable[[Handler], None]:
    """Like the ``controller`` fixture, but the client sends a real bearer token
    (so a server that echoes the request can be checked for leaks)."""
    clients: list[ControllerClient] = []

    def install(handler: Handler) -> None:
        def factory(settings: CliSettings) -> ControllerClient:
            client = ControllerClient(
                settings.cli.controller_url,
                timeout_s=settings.cli.timeout_s,
                transport=httpx.MockTransport(handler),
                token=SecretStr(TOKEN),
            )
            clients.append(client)
            return client

        monkeypatch.setattr(cli_shared, "_client", factory)

    yield install  # type: ignore[misc]
    for client in clients:
        client.close()


def _one_error_line(result: Any) -> str:
    assert result.stdout == "", result.stdout
    lines = result.stderr.splitlines()
    assert len(lines) == 1, result.stderr
    assert lines[0].startswith("compute: ")
    for marker in _TRACEBACK:
        assert marker not in result.output, marker
    return lines[0]


@pytest.mark.parametrize("argv", COMMANDS)
@pytest.mark.parametrize(
    ("failure", "handler", "code", "advice"),
    FAILURES,
    ids=[f[0] for f in FAILURES],
)
def test_every_command_class_maps_every_failure(
    runner: CliRunner,
    with_token: Callable[[Handler], None],
    argv: list[str],
    failure: str,
    handler: Handler,
    code: int,
    advice: str,
) -> None:
    with_token(handler)
    result = runner.invoke(cli_app.app, argv)
    assert result.exit_code == code, (failure, result.output)
    line = _one_error_line(result)
    assert advice in line, line
    assert TOKEN not in result.output


@pytest.mark.parametrize("argv", COMMANDS)
def test_a_server_echoing_the_bearer_token_is_scrubbed(
    runner: CliRunner, with_token: Callable[[Handler], None], argv: list[str]
) -> None:
    def echo(request: httpx.Request) -> httpx.Response:
        header = request.headers.get("authorization", "")
        return httpx.Response(
            400,
            json={"error": {"code": "invalid_input", "message": f"got {header}"}},
        )

    with_token(echo)
    result = runner.invoke(cli_app.app, argv)
    assert result.exit_code == CliExit.USAGE_ERROR
    assert TOKEN not in result.output and "Q" * 16 not in result.output
    assert "Bearer" in result.stderr  # the line is there, the token is not


def test_secret_set_scrubs_its_value_from_an_echoing_error(
    runner: CliRunner, with_token: Callable[[Handler], None]
) -> None:
    value = "hunter2-correct-horse"

    def echo(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        return httpx.Response(
            409, json={"error": {"code": "conflict", "message": f"refused {body}"}}
        )

    with_token(echo)
    result = runner.invoke(cli_app.app, ["secret", "set", "DB_PASS"], input=value)
    assert result.exit_code == CliExit.FAILURE
    assert value not in result.output
    assert "[secret:DB_PASS]" in result.stderr
    from meshforge.common.secret_redaction import ACTIVE

    assert not ACTIVE, "the value must leave the registry after the command"


def test_login_never_prints_the_token(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from meshforge.cli import auth_commands

    issued = "mfat_" + "Z" * 43

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "token": issued,
                "token_id": TOKEN_ID,
                "user": {"name": "alice", "role": "admin"},
                "expires_at": "2026-12-01T00:00:00+00:00",
            },
        )

    def client_for(
        settings: CliSettings, url: str, _ca: object, _token: object
    ) -> ControllerClient:
        return ControllerClient(
            url, timeout_s=1.0, transport=httpx.MockTransport(handler)
        )

    monkeypatch.setattr(auth_commands, "_client_for", client_for)
    monkeypatch.setenv("MESHFORGE_CLI__CREDENTIALS_DIR", str(tmp_path / "creds"))
    result = runner.invoke(
        cli_app.app,
        [
            "login",
            "--controller",
            "http://127.0.0.1:47110",
            "-u",
            "alice",
            "--password-stdin",
        ],
        input="pw-pw-pw-pw\n",
    )
    assert result.exit_code == 0, result.output
    assert issued not in result.output and "Z" * 16 not in result.output
    assert "pw-pw-pw-pw" not in result.output


# -------------------------------------------------------------- local classes


@pytest.mark.parametrize(
    "argv",
    [
        ["nodes", "../x"],
        ["jobs", "nope"],
        ["cancel", "nope"],
        ["tokens", "revoke", "x"],
        ["data", "rm", "noversion"],
        ["secret", "set", "bad name!"],
        ["run", "--node", NODE, "--local"],
        ["jobs", "--state", "sideways"],
        ["nodes", "--no-such-flag"],
        ["logs", JOB, "--stream", "both-ish"],
    ],
)
def test_usage_errors_exit_2_before_any_request(
    runner: CliRunner, with_token: Callable[[Handler], None], argv: list[str]
) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json={})

    with_token(handler)
    result = runner.invoke(cli_app.app, argv)
    assert result.exit_code == CliExit.USAGE_ERROR, result.output
    assert calls == []
    assert "Traceback" not in result.output


def test_bad_configuration_exits_4(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MESHFORGE_CLI__TIMEOUT_S", "not-a-number")
    result = runner.invoke(cli_app.app, ["nodes"])
    assert result.exit_code == CliExit.CONFIG_ERROR
    _one_error_line(result)


def test_a_missing_token_file_exits_4(runner: CliRunner, tmp_path: Path) -> None:
    result = runner.invoke(
        cli_app.app, ["--token-file", str(tmp_path / "absent"), "nodes"]
    )
    assert result.exit_code == CliExit.CONFIG_ERROR
    _one_error_line(result)


# -------------------------------------------------------------- the table


def test_the_cli_table_agrees_with_the_shared_exit_codes() -> None:
    """Every name both enums have means the same number; the only reuse is 3
    (NOT_IMPLEMENTED there, which no program returns) as AUTH."""
    shared = {m.name: m.value for m in ExitCode}
    for member in CliExit:
        if member.name in shared:
            assert member.value == shared[member.name], member
        elif member.value in shared.values():
            assert (member.name, member.value) == ("AUTH", 3), member
    src = REPO / "src" / "meshforge"
    users = [
        p
        for p in src.rglob("*.py")
        if "ExitCode.NOT_IMPLEMENTED" in p.read_text(encoding="utf-8")
        and p.name != "exit_codes.py"
    ]
    assert users == [], "3 is AUTH for compute; nothing may return NOT_IMPLEMENTED"


@pytest.mark.parametrize(
    ("failure_type", "code"),
    [
        (FailureType.NETWORK_ERROR, CliExit.UNAVAILABLE),
        (FailureType.TIMEOUT, CliExit.UNAVAILABLE),
        (FailureType.UNAUTHORIZED, CliExit.AUTH),
        (FailureType.NOT_FOUND, CliExit.NOT_FOUND),
        (FailureType.INVALID_INPUT, CliExit.USAGE_ERROR),
        (FailureType.CONFIG_ERROR, CliExit.CONFIG_ERROR),
        (FailureType.CONFLICT, CliExit.FAILURE),
        (FailureType.INTERNAL_ERROR, CliExit.FAILURE),
        (FailureType.RESOURCE_BUSY, CliExit.FAILURE),
        (FailureType.NO_FEASIBLE_PLAN, CliExit.FAILURE),
    ],
)
def test_the_one_mapping(failure_type: FailureType, code: CliExit) -> None:
    assert exit_code_for(ControllerError("x", failure_type=failure_type)) is code


def test_every_failure_type_maps_to_a_documented_code() -> None:
    documented = set(CliExit)
    for failure_type in FailureType:
        assert exit_code_for(ControllerError("x", failure_type=failure_type)) in (
            documented
        )


def test_run_follow_outcomes_are_unchanged() -> None:
    """D76 is kept: 0 / workload 1-125 / 126 / 128."""
    assert job_exit_code("succeeded", None, 0) == 0
    assert job_exit_code("failed", "user_error", 3) == 3
    assert job_exit_code("failed", "user_error", 300) == 125
    assert job_exit_code("failed", "user_error", None) == 125
    assert job_exit_code("failed", "node_lost", None) == CliExit.JOB_FAILED == 126
    assert job_exit_code("cancelled", "cancelled", None) == CliExit.JOB_CANCELLED


def test_docs_exit_table_matches_the_enum() -> None:
    text = (REPO / "docs" / "CLI.md").read_text(encoding="utf-8")
    section = text.split("## 4. Exit codes", 1)[1].split("\n## 5.", 1)[0]
    rows = dict(re.findall(r"^\| (\d+) \| ([A-Z_]+) \|", section, flags=re.M))
    documented = {name: int(code) for code, name in rows.items()}
    returned = {m.name: m.value for m in CliExit if m.name not in {"CONFLICT", "BUSY"}}
    assert documented == returned


def test_json_failure_documents_are_not_printed(
    runner: CliRunner, with_token: Callable[[Handler], None]
) -> None:
    with_token(_error(404, "not_found", "gone"))
    result = runner.invoke(cli_app.app, ["whoami", "--json"])
    assert result.exit_code == CliExit.NOT_FOUND
    assert result.stdout == ""
    with pytest.raises(json.JSONDecodeError):
        json.loads(result.stdout)
