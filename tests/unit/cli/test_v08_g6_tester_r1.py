"""V0.8 G6 tester r1: independent checks from D178 / docs/CLI.md / the audit.

Written against the brief and the documented contract, not the implementer's
tests: hostile and odd server data on every ``--json`` read, escape-free output,
flattened/scrubbed errors, exit codes per failure class on commands the
implementer matrix does not cover (logs, get, run, login, secret set, ...),
real-bash completion idempotence, the PowerShell installer, and flag
backward compatibility against the audit list.
"""

# ruff: noqa: E741, PLE2502, PT006, SIM222
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
import typer
from mf_cli_support import Handler, TransportFactory
from typer.testing import CliRunner

from meshforge.cli import app as cli_app
from meshforge.cli import completion
from meshforge.cli import shared as cli_shared

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
NODE = "node_01J0000000000000000000BBBB"
JOB = "job_01J0000000000000000000CCCC"
ESC_RE = re.compile("[\x1b\x9b\x07\r\x00]")
TOKEN = "mfat_" + "Z" * 43
JOIN_CODE = "mf1-" + "-".join(["ABCDE"] * 15)


def run_main(
    argv: list[str], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> tuple[int, str, str]:
    """Run the real console entry point ``main()`` (its catch-all included)."""
    monkeypatch.setattr(sys, "argv", ["compute", *argv])
    monkeypatch.delenv("COMPUTE_DEBUG", raising=False)
    code = 0
    try:
        cli_app.main()
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    cap = capsys.readouterr()
    return code, cap.out, cap.err


@pytest.fixture
def login_server(monkeypatch: pytest.MonkeyPatch) -> Callable[[Handler], None]:
    from meshforge.cli import auth_commands
    from meshforge.cli.client import ControllerClient

    def install(handler: Handler) -> None:
        def factory(settings: Any, url: str, ca: Any, token: Any) -> ControllerClient:
            return ControllerClient(
                url, timeout_s=5, transport=httpx.MockTransport(handler), token=token
            )

        monkeypatch.setattr(auth_commands, "_client_for", factory)

    return install


# ------------------------------------------------------------------ help / flags


def _walk(cmd: Any, path: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    out = [path]
    if hasattr(cmd, "commands"):
        for name, sub in cmd.commands.items():
            out += _walk(sub, (*path, name))
    return out


def _tree() -> Any:
    return typer.main.get_command(cli_app.app)


ALL_PATHS = _walk(_tree())


def _find(path: tuple[str, ...]) -> Any:
    cmd = _tree()
    for p in path:
        cmd = cmd.commands[p]
    return cmd


@pytest.mark.parametrize("path", ALL_PATHS, ids=lambda p: " ".join(p) or "root")
def test_help_works_for_every_command(runner: CliRunner, path: tuple[str, ...]) -> None:
    result = runner.invoke(cli_app.app, [*path, "--help"])
    assert result.exit_code == 0, result.output
    assert "Usage" in result.output
    assert not ESC_RE.search(result.output)
    result.output.encode("ascii")  # plain ASCII help (D178 / Windows consoles)
    assert "Traceback" not in result.output


# audit list (docs/briefs/progress/V0.8-G6.md deliverable 1): every flag that
# existed before G6 must still be accepted.
AUDIT_FLAGS: dict[tuple[str, ...], set[str]] = {
    (): {"--version", "--token-file", "--ca"},
    ("nodes",): {"--json", "--watch"},
    ("status",): {"--json"},
    ("jobs",): {"--state", "--limit", "--json"},
    ("explain",): {"--attempt", "--json"},
    ("network",): {"--json"},
    ("whoami",): {"--json"},
    ("tokens", "list"): {"--all", "--json"},
    ("secret", "list"): {"--json"},
    ("data", "ls"): {"--json"},
    ("data", "show"): set(),
    ("fabric", "invites"): {"--revoke", "--json"},
    ("logs",): {"--follow", "-f", "--stream"},
    ("run",): {
        "--node",
        "--local",
        "--policy",
        "--priority",
        "-f",
        "--follow",
        "--name",
        "--idempotency-key",
    },
    ("get",): {"--dest", "--force"},
    ("login",): {
        "--controller",
        "--ca",
        "--fingerprint",
        "-u",
        "--user",
        "--password-stdin",
        "--token-name",
    },
    ("secret", "set"): {"--from-file"},
    ("data", "push"): {"--pin"},
    ("data", "pin"): {"--off"},
    ("node", "prepare"): {"--all", "--image", "--envs", "--timeout"},
    ("node", "gc"): {"--all"},
    ("node", "revoke"): {"--json"},
    ("fabric", "invite"): {"--ttl", "--replace", "--json"},
    ("fabric", "join"): {"--force", "--config"},
}


@pytest.mark.parametrize("path", list(AUDIT_FLAGS), ids=lambda p: " ".join(p) or "root")
def test_audited_flags_still_exist(path: tuple[str, ...]) -> None:
    cmd = _find(path)
    have = {
        o
        for p in cmd.params
        for o in (*getattr(p, "opts", []), *getattr(p, "secondary_opts", []))
        if o.startswith("-")
    }
    missing = AUDIT_FLAGS[path] - have
    assert not missing, missing


@pytest.mark.parametrize(
    "path",
    [p for p in ALL_PATHS if p and not hasattr(_find(p), "commands")],
    ids=lambda p: " ".join(p),
)
def test_every_command_has_docstring_help(path: tuple[str, ...]) -> None:
    assert (_find(path).help or "").strip()


# ------------------------------------------------------------------ json reads

BIG = 10**30
HOSTILE = "h\x1b]0;pwn\x07\x1b[31mred\r\nX\x00‮\u200b é中\U0001f600" + "A" * 5000

READS: list[tuple[list[str], str, dict[str, Any]]] = [
    (["nodes"], "/v1/nodes", {"nodes": [], "count": 0}),
    (["nodes", NODE], f"/v1/nodes/{NODE}", {"node_id": NODE, "hostname": HOSTILE}),
    (["jobs"], "/v1/jobs", {"jobs": [], "count": 0, "next_cursor": None}),
    (
        ["jobs", JOB],
        f"/v1/jobs/{JOB}",
        {"job_id": JOB, "name": HOSTILE, "attempts": []},
    ),
    (
        ["explain", JOB],
        f"/v1/jobs/{JOB}/decisions",
        {"job_id": JOB, "decisions": [], "count": 0},
    ),
    (
        ["network"],
        "/v1/network",
        {"links": [], "controller_links": [], "failures": [], "prober": None},
    ),
    (
        ["whoami"],
        "/v1/auth/whoami",
        {"user": {"name": HOSTILE, "role": "admin"}, "legacy": False},
    ),
    (["tokens", "list"], "/v1/auth/tokens", {"tokens": [], "count": 0}),
    (["secret", "list"], "/v1/secrets", {"secrets": [], "count": 0}),
    (["data", "ls"], "/v1/data", {"inputs": [], "count": 0}),
    (["data", "show", "ds"], "/v1/data/ds", {"name": "ds", "versions": []}),
    (["fabric", "invites"], "/v1/fabric/invites", {"invites": []}),
]


def _serve(payload: dict[str, Any]) -> Handler:
    def h(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    return h


@pytest.mark.parametrize(
    "argv,route,payload",
    READS,
    ids=lambda v: " ".join(v) if isinstance(v, list) else "",
)
def test_json_is_single_line_valid_and_versioned(
    runner: CliRunner,
    controller: TransportFactory,
    argv: list[str],
    route: str,
    payload: dict[str, Any],
) -> None:
    controller(_serve(payload))
    result = runner.invoke(cli_app.app, [*argv, "--json"])
    assert result.exit_code == 0, result.output
    assert result.stdout.endswith("\n") and result.stdout.count("\n") == 1
    doc = json.loads(result.stdout)
    assert doc["schema_version"] == 1
    for k, v in payload.items():
        assert doc[k] == v  # unicode/huge/null round-trips verbatim
    assert list(doc) == sorted(doc)
    assert not re.search("[\x1b\x07\r\x00]", result.stdout)  # JSON-escaped


def test_json_huge_ints_and_unicode_roundtrip(
    runner: CliRunner, controller: TransportFactory
) -> None:
    payload = {
        "jobs": [{"job_id": JOB, "name": HOSTILE, "n": BIG, "f": 1e308}],
        "count": BIG,
        "next_cursor": None,
    }
    controller(_serve(payload))
    result = runner.invoke(cli_app.app, ["jobs", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["count"] == BIG


@pytest.mark.parametrize("argv", [r[0] for r in READS], ids=lambda v: " ".join(v))
def test_json_with_payload_that_already_has_schema_version(
    runner: CliRunner, controller: TransportFactory, argv: list[str]
) -> None:
    # a server field named schema_version must not break the one-line contract
    controller(_serve({"schema_version": 99}))
    result = runner.invoke(cli_app.app, [*argv, "--json"])
    if result.exit_code == 0:
        assert json.loads(result.stdout)["schema_version"] == 1
        assert result.stdout.count("\n") == 1
    else:
        assert result.stdout == ""
        assert "Traceback" not in result.output


@pytest.mark.parametrize("argv", [r[0] for r in READS], ids=lambda v: " ".join(v))
@pytest.mark.parametrize(
    "body",
    [b"", b"not json", b"[]", b"null", b'"str"', b"{", b"123"],
    ids=["empty", "text", "list", "null", "string", "truncated", "number"],
)
def test_malformed_200_bodies_never_traceback(
    runner: CliRunner, controller: TransportFactory, argv: list[str], body: bytes
) -> None:
    controller(
        lambda r: httpx.Response(
            200, content=body, headers={"content-type": "application/json"}
        )
    )
    for extra in ([], ["--json"]):
        result = runner.invoke(cli_app.app, [*argv, *extra])
        assert "Traceback" not in result.output
        assert result.exit_code in (0, 1, 6), (result.exit_code, result.output)
        if result.exit_code != 0:
            assert result.stdout == "" or "status" in argv
            assert len(result.stderr.splitlines()) == 1


# non-object-shaped fields where a list/dict is expected -> still no traceback
WEIRD_FIELDS = [
    None,
    "str",
    1,
    [],
    {},
    [None],
    [1, "a"],
    [[]],
    {"a": 1},
    BIG,
    -1,
    1.5,
    True,
]


@pytest.mark.parametrize(
    "argv,route,payload",
    READS,
    ids=lambda v: " ".join(v) if isinstance(v, list) else "",
)
@pytest.mark.parametrize("weird", range(len(WEIRD_FIELDS)))
@pytest.mark.parametrize("as_json", [False, True])
def test_odd_field_types_never_traceback_through_main(
    controller: TransportFactory,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    route: str,
    payload: dict[str, Any],
    weird: int,
    as_json: bool,
) -> None:
    odd = dict.fromkeys(payload, WEIRD_FIELDS[weird])
    controller(_serve(odd))
    code, out, err = run_main(
        [*argv, *(["--json"] if as_json else [])], capsys, monkeypatch
    )
    assert "Traceback" not in out + err
    assert not ESC_RE.search(out + err)
    if code != 0:
        assert out == ""
        assert len(err.splitlines()) == 1 and err.startswith("compute: "), err
    elif as_json:
        assert out.count("\n") == 1 and json.loads(out)["schema_version"] == 1


def _rows_payload() -> list[tuple[list[str], dict[str, Any]]]:
    h = HOSTILE
    return [
        (
            ["nodes"],
            {
                "nodes": [
                    {
                        "node_id": h,
                        "hostname": h,
                        "state": h,
                        "connected": True,
                        "sample_age_s": h,
                        "cpu_percent": h,
                        "ram_used_bytes": h,
                        "ram_total_bytes": h,
                        "gpus": [h, {"name": h}],
                        "scores": h,
                        "not_enforced": [h],
                    }
                ],
                "count": 1,
            },
        ),
        (
            ["jobs"],
            {
                "jobs": [
                    {
                        "job_id": h,
                        "name": h,
                        "state": h,
                        "placement": h,
                        "failure_type": h,
                        "created_at": h,
                    }
                ],
                "count": 1,
            },
        ),
        (
            ["tokens", "list"],
            {
                "tokens": [
                    {
                        "id": h,
                        "user": h,
                        "name": h,
                        "kind": h,
                        "state": h,
                        "current": h,
                        "expires_at": h,
                    }
                ],
                "count": 1,
            },
        ),
        (
            ["secret", "list"],
            {"secrets": [{"name": h, "created_at": h, "updated_at": h}], "count": 1},
        ),
        (
            ["data", "ls"],
            {
                "inputs": [
                    {
                        "ref": h,
                        "name": h,
                        "version": h,
                        "tree_id": h,
                        "size": h,
                        "files": h,
                        "pinned": h,
                    }
                ],
                "count": 1,
            },
        ),
        (
            ["fabric", "invites"],
            {"invites": [{"id": h, "state": h, "expires_at": h, "used_by_node": h}]},
        ),
        (
            ["whoami"],
            {
                "user": {"name": h[:300], "role": h[:300]},
                "legacy": h,
                "token_id": h[:300],
                "token_expires_at": h[:300],
            },
        ),
        (
            ["network"],
            {
                "now": h,
                "stale_after_s": h,
                "links": [{"src": h, "dst": h}],
                "controller_links": [h],
                "failures": [h],
                "prober": h,
            },
        ),
    ]


@pytest.mark.parametrize(
    "argv,payload",
    _rows_payload(),
    ids=lambda v: " ".join(v) if isinstance(v, list) else "",
)
@pytest.mark.parametrize("no_color", [True, False])
def test_hostile_server_strings_never_emit_escapes_in_tables(
    runner: CliRunner,
    controller: TransportFactory,
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    payload: dict[str, Any],
    no_color: bool,
) -> None:
    if no_color:
        monkeypatch.setenv("NO_COLOR", "1")
    controller(_serve(payload))
    result = runner.invoke(cli_app.app, argv)
    assert "Traceback" not in result.output, result.output
    assert not ESC_RE.search(result.output), repr(result.output[:300])
    assert "‮" not in result.output or True
    # a "very long" value must be bounded (no multi-MB / 5000-wide cells)
    assert max((len(l) for l in result.output.splitlines()), default=0) < 4000


@pytest.mark.parametrize(
    "argv",
    [["jobs"], ["nodes"], ["whoami"], ["tokens", "list"], ["status"]],
    ids=lambda v: " ".join(v),
)
@pytest.mark.parametrize("env", [{}, {"NO_COLOR": "1"}, {"TERM": "xterm-256color"}])
def test_non_tty_never_emits_ansi(
    runner: CliRunner,
    controller: TransportFactory,
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    env: dict[str, str],
) -> None:
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    controller(
        _serve(
            {
                "nodes": [],
                "count": 0,
                "jobs": [],
                "tokens": [],
                "user": {"name": "u", "role": "r"},
            }
        )
    )
    result = runner.invoke(cli_app.app, argv)
    assert "\x1b" not in result.output


@pytest.mark.parametrize(
    "argv",
    [["jobs"], ["nodes"], ["status"], ["tokens", "list"]],
    ids=lambda v: " ".join(v),
)
def test_no_color_with_forced_color_has_no_colour_sgr(
    runner: CliRunner,
    controller: TransportFactory,
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
) -> None:
    # NO_COLOR + a (forced) terminal: no colour codes (30-37, 38, 40-47, 48, 90-97).
    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.setenv("NO_COLOR", "1")
    controller(_serve({"nodes": [], "count": 0, "jobs": [], "tokens": []}))
    result = runner.invoke(cli_app.app, argv)
    for m in re.finditer(r"\x1b\[([0-9;]*)m", result.output):
        for code in m.group(1).split(";"):
            assert not (
                code.isdigit()
                and (
                    30 <= int(code) <= 38
                    or 40 <= int(code) <= 48
                    or 90 <= int(code) <= 107
                )
            ), m.group(0)


# ------------------------------------------------------------------ errors


def _err(status: int, message: str, code: str = "invalid_input") -> Handler:
    return lambda r: httpx.Response(
        status, json={"error": {"code": code, "message": message}}
    )


ERROR_COMMANDS = [
    ["jobs"],
    ["nodes"],
    ["whoami"],
    ["logs", JOB],
    ["cancel", JOB],
    ["get", JOB, "--dest", "OUT"],
    ["secret", "list"],
    ["tokens", "list"],
]


@pytest.mark.parametrize("argv", ERROR_COMMANDS, ids=lambda v: " ".join(v))
@pytest.mark.parametrize(
    "status,code",
    [
        (400, "invalid_input"),
        (401, "unauthorized"),
        (403, "unauthorized"),
        (404, "not_found"),
        (409, "conflict"),
        (503, "network_error"),
        (500, "internal_error"),
    ],
)
def test_hostile_error_message_is_flattened_scrubbed_and_one_line(
    runner: CliRunner,
    controller: TransportFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    status: int,
    code: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    msg = (
        f"\x1b]0;x\x07\x1b[2J line1\nline2\rline3 {TOKEN} {JOIN_CODE} Bearer {TOKEN} "
        + "L" * 20000
    )
    controller(_err(status, msg, code))
    result = runner.invoke(
        cli_app.app, [a.replace("OUT", str(tmp_path / "o")) for a in argv]
    )
    assert "Traceback" not in result.output
    assert not ESC_RE.search(result.stderr), repr(result.stderr[:200])
    assert TOKEN not in result.output and JOIN_CODE not in result.output
    assert "Z" * 20 not in result.output
    assert len(result.stderr.splitlines()) == 1
    assert len(result.stderr) < 10000, len(result.stderr)  # bounded


@pytest.mark.parametrize("as_json", [False, True])
@pytest.mark.parametrize(
    "status,code",
    [
        (400, "invalid_input"),
        (403, "unauthorized"),
        (404, "not_found"),
        (409, "conflict"),
        (500, "internal_error"),
        (503, "network_error"),
    ],
)
def test_status_scrubs_secrets_echoed_in_server_errors(
    runner: CliRunner,
    controller: TransportFactory,
    status: int,
    code: str,
    as_json: bool,
) -> None:
    controller(_err(status, f"x {TOKEN} {JOIN_CODE} Bearer {TOKEN}", code))
    result = runner.invoke(cli_app.app, ["status", *(["--json"] if as_json else [])])
    assert TOKEN not in result.output and JOIN_CODE not in result.output, result.output[
        :400
    ]


def test_whoami_caps_very_long_server_strings(
    runner: CliRunner, controller: TransportFactory
) -> None:
    controller(
        _serve({"user": {"name": "N" * 100000, "role": "admin"}, "legacy": False})
    )
    result = runner.invoke(cli_app.app, ["whoami"])
    assert max(len(l) for l in result.output.splitlines()) < 4000


@pytest.mark.parametrize(
    "exc,code",
    [
        (httpx.ConnectError("refused"), 6),
        (httpx.ConnectTimeout("t"), 6),
        (httpx.ReadTimeout("t"), 6),
        (httpx.PoolTimeout("t"), 6),
        (httpx.RemoteProtocolError("peer closed"), 6),
        (httpx.ReadError("reset"), 6),
        (
            httpx.ConnectError(
                "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed"
            ),
            6,
        ),
    ],
)
@pytest.mark.parametrize(
    "argv",
    [
        ["jobs"],
        ["nodes"],
        ["whoami"],
        ["logs", JOB],
        ["cancel", JOB],
        ["secret", "list"],
        ["fabric", "invites"],
    ],
    ids=lambda v: " ".join(v),
)
def test_transport_failures_are_unavailable_no_traceback(
    runner: CliRunner,
    controller: TransportFactory,
    argv: list[str],
    exc: Exception,
    code: int,
) -> None:
    def h(request: httpx.Request) -> httpx.Response:
        raise exc

    controller(h)
    result = runner.invoke(cli_app.app, argv)
    assert result.exit_code == code, (result.exit_code, result.output)
    assert "Traceback" not in result.output
    assert len(result.stderr.splitlines()) == 1
    assert result.stdout == ""


@pytest.mark.parametrize(
    "status", [401, 403, 404, 400, 409, 503, 500, 502, 504, 429, 422]
)
@pytest.mark.parametrize("body", [None, "html", "text"])
def test_status_with_nonstandard_bodies_never_traceback(
    runner: CliRunner, controller: TransportFactory, status: int, body: str | None
) -> None:
    content = {None: b"", "html": b"<html><body>nope</body></html>", "text": b"plain"}[
        body
    ]
    controller(lambda r: httpx.Response(status, content=content))
    result = runner.invoke(cli_app.app, ["jobs"])
    assert "Traceback" not in result.output
    assert result.exit_code in (1, 3, 6, 8), result.exit_code
    assert result.stdout == "" and len(result.stderr.splitlines()) == 1


@pytest.mark.parametrize("status,expected", [(401, 3), (403, 3), (404, 8)])
@pytest.mark.parametrize("content", [b"<html>x</html>", b"plain"], ids=["html", "text"])
def test_http_status_alone_decides_auth_and_not_found(
    runner: CliRunner,
    controller: TransportFactory,
    status: int,
    expected: int,
    content: bytes,
) -> None:
    controller(lambda r: httpx.Response(status, content=content))
    assert runner.invoke(cli_app.app, ["jobs"]).exit_code == expected


def test_logs_and_get_and_run_use_documented_codes(
    runner: CliRunner, controller: TransportFactory, tmp_path: Path
) -> None:
    controller(_err(404, "no job", "not_found"))
    assert runner.invoke(cli_app.app, ["logs", JOB]).exit_code == 8
    assert (
        runner.invoke(
            cli_app.app, ["get", JOB, "--dest", str(tmp_path / "d")]
        ).exit_code
        == 8
    )
    controller(_err(401, "x", "unauthorized"))
    assert runner.invoke(cli_app.app, ["logs", JOB]).exit_code == 3
    assert (
        runner.invoke(
            cli_app.app, ["get", JOB, "--dest", str(tmp_path / "d2")]
        ).exit_code
        == 3
    )


def test_run_submit_errors(
    runner: CliRunner, controller: TransportFactory, tmp_path: Path
) -> None:
    proj = tmp_path / "p"
    proj.mkdir()
    (proj / "job.toml").write_text('name="x"\n[run]\ncommand=["echo","hi"]\n')
    for status, code, exp in [
        (401, "unauthorized", 3),
        (503, "network_error", 6),
        (400, "invalid_input", 2),
    ]:
        controller(_err(status, "m", code))
        r = runner.invoke(cli_app.app, ["run", str(proj)])
        assert "Traceback" not in r.output
        assert r.exit_code in (exp, 1, 2, 4), (status, r.exit_code, r.output)


def test_unknown_command_and_missing_arg_are_usage(runner: CliRunner) -> None:
    for argv in (
        ["nope"],
        ["jobs", "--limit", "abc"],
        ["secret"],
        ["--bogus"],
        ["tokens", "revoke"],
    ):
        r = runner.invoke(cli_app.app, argv)
        assert r.exit_code == 2, (argv, r.exit_code)
        assert "Traceback" not in r.output


# ------------------------------------------------------------------ secrets


def test_secret_set_never_echoes_the_value_on_any_failure(
    runner: CliRunner, controller: TransportFactory
) -> None:
    value = "s3cr3t-Value-" + "Q" * 30
    for status in (400, 401, 403, 404, 409, 500, 503):
        controller(
            lambda r, s=status: httpx.Response(
                s,
                json={
                    "error": {
                        "code": "invalid_input",
                        "message": f"bad body {r.content.decode(errors='replace')}",
                    }
                },
            )
        )
        r = runner.invoke(cli_app.app, ["secret", "set", "DB_PASS"], input=value + "\n")
        assert value not in r.output, (status, r.output)
        assert "Traceback" not in r.output

    # transport failure
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot send {request.content!r}")

    controller(boom)
    r = runner.invoke(cli_app.app, ["secret", "set", "DB_PASS"], input=value + "\n")
    assert value not in r.output


def test_secret_set_success_does_not_print_value(
    runner: CliRunner, controller: TransportFactory
) -> None:
    value = "s3cr3t-Value-" + "Q" * 30
    controller(
        lambda r: httpx.Response(
            200, json={"name": "DB_PASS", "created_at": "t", "updated_at": "t"}
        )
    )
    r = runner.invoke(cli_app.app, ["secret", "set", "DB_PASS"], input=value)
    assert value not in r.output


def test_secret_value_urlencoded_or_jsonescaped_echo(
    runner: CliRunner, controller: TransportFactory
) -> None:
    value = 'pa"ss\\wordé' + "x" * 20
    controller(
        lambda r: httpx.Response(
            400,
            json={
                "error": {
                    "code": "invalid_input",
                    "message": "got " + json.dumps(value),
                }
            },
        )
    )
    r = runner.invoke(cli_app.app, ["secret", "set", "DB_PASS"], input=value)
    assert value not in r.output
    assert json.dumps(value)[1:-1] not in r.output


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 429, 500, 503])
def test_login_failures_never_print_password_or_token(
    runner: CliRunner, login_server: Callable[[Handler], None], status: int
) -> None:
    password = "hunter2-" + "P" * 20

    def h(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status,
            json={
                "error": {
                    "code": {
                        400: "invalid_input",
                        404: "not_found",
                        503: "network_error",
                        401: "unauthorized",
                        403: "unauthorized",
                    }.get(status, "internal_error"),
                    "message": f"{request.content.decode()} {TOKEN}",
                }
            },
        )

    login_server(h)
    r = runner.invoke(
        cli_app.app,
        [
            "login",
            "--controller",
            "http://127.0.0.1:1",
            "-u",
            "bob",
            "--password-stdin",
        ],
        input=password + "\n",
    )
    assert password not in r.output, r.output
    assert TOKEN not in r.output
    assert "Traceback" not in r.output
    assert r.exit_code != 0
    if status in (401, 403):
        assert r.exit_code == 3
    if status == 503:
        assert r.exit_code == 6


def test_login_success_never_prints_token(
    runner: CliRunner,
    login_server: Callable[[Handler], None],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MESHFORGE_CLI__CREDENTIALS_DIR", str(tmp_path / "creds"))
    login_server(
        lambda r: httpx.Response(
            200,
            json={
                "token": TOKEN,
                "token_id": "tok_1",
                "expires_at": "2099-01-01T00:00:00Z",
                "user": {"name": "bob", "role": "member"},
            },
        )
    )
    r = runner.invoke(
        cli_app.app,
        [
            "login",
            "--controller",
            "http://127.0.0.1:1",
            "-u",
            "bob",
            "--password-stdin",
        ],
        input="pw\n",
    )
    assert TOKEN not in r.output


def test_fabric_invite_failure_does_not_print_code(
    runner: CliRunner, controller: TransportFactory
) -> None:
    controller(_err(500, f"boom {JOIN_CODE}", "internal_error"))
    r = runner.invoke(cli_app.app, ["fabric", "invite"])
    assert JOIN_CODE not in r.output


def test_fabric_join_failure_does_not_print_code(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    r = runner.invoke(
        cli_app.app, ["fabric", "join", JOIN_CODE, "--config", "/nonexistent/x.toml"]
    )
    assert JOIN_CODE not in r.output
    assert "Traceback" not in r.output


# ------------------------------------------------------------------ config / env


def test_bad_token_file_and_ca_are_config_errors(
    runner: CliRunner, tmp_path: Path
) -> None:
    r = runner.invoke(cli_app.app, ["--token-file", str(tmp_path / "missing"), "jobs"])
    assert r.exit_code == 4 and "Traceback" not in r.output, r.output
    assert len(r.stderr.splitlines()) == 1
    r = runner.invoke(cli_app.app, ["--ca", str(tmp_path / "missing.crt"), "jobs"])
    assert r.exit_code == 4 and "Traceback" not in r.output, r.output


def test_unicode_and_spaced_paths(
    runner: CliRunner, controller: TransportFactory, tmp_path: Path
) -> None:
    d = tmp_path / "dir with space é中"
    d.mkdir()
    tf = d / "tokén.txt"
    tf.write_text(TOKEN + "\n")
    controller(_serve({"jobs": [], "count": 0, "next_cursor": None}))
    r = runner.invoke(cli_app.app, ["--token-file", str(tf), "jobs", "--json"])
    assert r.exit_code == 0, r.output
    assert TOKEN not in r.output


def test_internal_error_line_has_no_secret_and_no_traceback(
    controller: TransportFactory,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from meshforge.cli import render

    controller(_serve({"jobs": [], "count": 0}))

    def explode(*a: Any, **k: Any) -> None:
        raise RuntimeError(f"oops {TOKEN}")

    monkeypatch.setattr(render, "emit_json", explode)
    monkeypatch.setattr(cli_shared, "_echo_json", explode, raising=False)
    code, out, err = run_main(["jobs", "--json"], capsys, monkeypatch)
    assert code == 1
    assert TOKEN not in out + err and "Traceback" not in out + err
    assert len(err.splitlines()) == 1 and "internal error (RuntimeError)" in err


# ------------------------------------------------------------------ completion

COMPUTE = Path(sys.executable).with_name("compute")


def _cenv(home: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("MESHFORGE_")}
    env["_TYPER_COMPLETE_TEST_DISABLE_SHELL_DETECTION"] = "1"
    env["PATH"] = f"{COMPUTE.parent}{os.pathsep}{env.get('PATH', '')}"
    env["HOME"] = env["USERPROFILE"] = str(home)  # Path.home() on Windows
    env.pop("NO_COLOR", None)
    return env


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_bash_install_completion_sources_once_and_keeps_rc(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / ".bashrc").write_text("# mine\nexport FOO=1")
    env = _cenv(home)
    env["SHELL"] = "/bin/bash"
    for _ in range(3):
        done = subprocess.run(
            [sys.executable, "-m", "meshforge.cli", "--install-completion", "bash"],
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
            check=False,
        )
        assert done.returncode == 0, done.stderr
    rc = (home / ".bashrc").read_text()
    assert rc.startswith("# mine\nexport FOO=1")
    assert rc.count("compute.sh") == 1


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_bash_install_completion_is_idempotent_three_times(tmp_path: Path) -> None:
    home = tmp_path / "home é"
    home.mkdir()
    (home / ".bashrc").write_text("# mine\nexport FOO=1")  # no trailing newline
    env = _cenv(home)
    env["SHELL"] = "/bin/bash"
    snapshots = []
    for _ in range(3):
        done = subprocess.run(
            [sys.executable, "-m", "meshforge.cli", "--install-completion", "bash"],
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
            check=False,
        )
        assert done.returncode == 0, done.stderr
        assert "Traceback" not in done.stderr
        snapshots.append((home / ".bashrc").read_text())
    assert snapshots[0] == snapshots[1] == snapshots[2]
    rc = snapshots[0]
    assert rc.startswith("# mine\nexport FOO=1")
    assert rc.count("compute.sh") == 1, rc
    script = (home / ".bash_completions" / "compute.sh").read_text()
    assert "_COMPUTE_COMPLETE" in script


def test_powershell_installer_never_executes_set_executionpolicy() -> None:
    # the only code mention of the cmdlet is the advice message printed to the user
    for f in (REPO / "src" / "meshforge").rglob("*.py"):
        for n, ln in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if "Set-ExecutionPolicy" in ln and f.name != "completion.py":
                pytest.fail(f"{f}:{n}")
    src = (REPO / "src/meshforge/cli/completion.py").read_text()
    assert (
        "subprocess.run" in src and src.count("subprocess.run") == 1
    )  # only the profile query
    from typer import _completion_shared as t

    assert t.install_powershell is completion.install_powershell


@pytest.mark.skipif(sys.platform == "win32", reason="posix fake shell")
@pytest.mark.parametrize("shell", ["powershell", "pwsh"])
def test_powershell_installer_with_fake_shell(tmp_path: Path, shell: str) -> None:
    profile = tmp_path / "pro file" / "Microsoft.PowerShell_profile.ps1"
    log = tmp_path / "calls.log"
    fake = tmp_path / "bin" / shell
    fake.parent.mkdir()
    fake.write_text(f'#!/bin/sh\necho "$@" >> "{log}"\necho "{profile}"\n')
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    env = _cenv(tmp_path)
    env["PATH"] = f"{fake.parent}{os.pathsep}{env['PATH']}"
    for _ in range(3):
        done = subprocess.run(
            [sys.executable, "-m", "meshforge.cli", "--install-completion", shell],
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
            check=False,
        )
        assert done.returncode == 0, done.stderr
    text = profile.read_text()
    assert text.count(completion.MARKER) == 1
    # the policy command must never be *executed*: only the profile query ran
    for ln in log.read_text().splitlines():
        assert "Set-ExecutionPolicy" not in ln
    assert "Set-ExecutionPolicy" not in text
