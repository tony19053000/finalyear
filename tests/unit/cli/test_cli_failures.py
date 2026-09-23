"""V0.3 G5 unit tests: every way ``compute`` can fail to get an answer (0.3.12).

Each case asserts three things: the exit code (D18 - ``UNAVAILABLE`` only when the
controller could not be reached or timed out), that the message is one readable
line on **stderr**, and that **no traceback** reaches the output. A traceback from
an operator command is a bug, not diagnostics.

The transport is ``httpx.MockTransport`` (the brief's rule for the unit tier), so
the real client, settings and error mapping run with no socket. The genuinely
*real* refused connection - a closed TCP port - is exercised in the component
tier, where a real controller can be stopped.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from mf_cli_support import TransportFactory
from typer.testing import CliRunner

from meshforge.cli.app import app
from meshforge.common.errors import FailureType
from meshforge.common.exit_codes import ExitCode

pytestmark = pytest.mark.unit

NODE_ID = "node_01JQZ00000000000000000000A"
_TRACEBACK_MARKERS = (
    "Traceback",
    "most recent call last",
    'File "',
    "httpx.",
    "ControllerError",
    "site-packages",
)


def assert_no_traceback(output: str) -> None:
    for marker in _TRACEBACK_MARKERS:
        assert marker not in output, f"{marker!r} leaked into the output"


def envelope(code: FailureType, message: str) -> dict[str, Any]:
    return {"error": {"code": code.value, "message": message}}


def raising(exc: Exception) -> Any:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise exc

    return handler


def answering(status: int, *, body: Any = None, text: str | None = None) -> Any:
    def handler(_request: httpx.Request) -> httpx.Response:
        if text is not None:
            return httpx.Response(status, text=text)
        return httpx.Response(status, json=body)

    return handler


# ------------------------------------------------------- cannot reach at all


def test_a_refused_connection_exits_unavailable_with_one_line(
    runner: CliRunner, controller: TransportFactory
) -> None:
    controller(raising(httpx.ConnectError("refused")))
    result = runner.invoke(app, ["nodes"])
    assert result.exit_code == ExitCode.UNAVAILABLE
    assert result.stdout == ""
    assert result.stderr.count("\n") == 1
    assert "cannot reach the controller at http://127.0.0.1:47110" in result.stderr
    assert_no_traceback(result.output)


def test_a_timeout_exits_unavailable_and_names_the_budget(
    runner: CliRunner, controller: TransportFactory
) -> None:
    controller(raising(httpx.ReadTimeout("too slow")))
    result = runner.invoke(app, ["nodes"])
    assert result.exit_code == ExitCode.UNAVAILABLE
    assert "did not answer within 5s" in result.stderr
    assert_no_traceback(result.output)


def test_a_dns_failure_is_also_unavailable(
    runner: CliRunner, controller: TransportFactory
) -> None:
    controller(raising(httpx.ConnectError("Name or service not known")))
    result = runner.invoke(app, ["nodes"])
    assert result.exit_code == ExitCode.UNAVAILABLE
    # The platform's own error text is not reproduced, only the exception type.
    assert "Name or service not known" not in result.stderr


def test_a_protocol_error_is_unavailable_not_a_crash(
    runner: CliRunner, controller: TransportFactory
) -> None:
    controller(raising(httpx.RemoteProtocolError("half-closed")))
    result = runner.invoke(app, ["nodes"])
    assert result.exit_code == ExitCode.UNAVAILABLE
    assert_no_traceback(result.output)


# ------------------------------------------------------------ typed envelopes


def test_a_503_envelope_exits_unavailable_with_the_controllers_safe_message(
    runner: CliRunner, controller: TransportFactory
) -> None:
    """The controller is up but its database is not - still "cannot answer"."""
    controller(
        answering(
            503,
            body=envelope(
                FailureType.NETWORK_ERROR,
                "the controller's database is unavailable; retry shortly",
            ),
        )
    )
    result = runner.invoke(app, ["nodes"])
    assert result.exit_code == ExitCode.UNAVAILABLE
    assert "cannot answer right now" in result.stderr
    assert "database is unavailable" in result.stderr
    assert_no_traceback(result.output)


def test_a_404_on_one_node_exits_failure_not_unavailable(
    runner: CliRunner, controller: TransportFactory
) -> None:
    """A node that does not exist is a *request* problem: retrying will not help."""
    controller(answering(404, body=envelope(FailureType.NOT_FOUND, "no such node")))
    result = runner.invoke(app, ["nodes", NODE_ID])
    assert result.exit_code == ExitCode.FAILURE
    assert "404" in result.stderr
    assert "no such node" in result.stderr
    assert_no_traceback(result.output)


def test_a_400_envelope_exits_failure(
    runner: CliRunner, controller: TransportFactory
) -> None:
    """A *server-side* 400 exits FAILURE.

    The id is a valid one on purpose: since the G5 fix round the CLI refuses a
    malformed NODE_ID itself with ``USAGE_ERROR`` before any request is built
    (``test_cli_g5_fix_round.py``), so a 400 can now only come from the
    controller - e.g. an older/newer controller with a stricter rule.
    """
    controller(
        answering(
            400, body=envelope(FailureType.INVALID_INPUT, "node_id is not canonical")
        )
    )
    result = runner.invoke(app, ["nodes", NODE_ID])
    assert result.exit_code == ExitCode.FAILURE
    assert_no_traceback(result.output)


def test_an_unknown_envelope_code_exits_failure_rather_than_being_trusted(
    runner: CliRunner, controller: TransportFactory
) -> None:
    controller(
        answering(418, body={"error": {"code": "invented_tomorrow", "message": "hmm"}})
    )
    result = runner.invoke(app, ["nodes"])
    assert result.exit_code == ExitCode.FAILURE
    assert_no_traceback(result.output)


def test_an_error_message_is_flattened_and_truncated_before_printing(
    runner: CliRunner, controller: TransportFactory
) -> None:
    """Server text reaches a terminal: no control characters, no screenfuls."""
    hostile = "line one\nline two\x1b[31m" + "x" * 500
    controller(answering(503, body=envelope(FailureType.NETWORK_ERROR, hostile)))
    result = runner.invoke(app, ["nodes"])
    assert result.exit_code == ExitCode.UNAVAILABLE
    assert result.stderr.count("\n") == 1
    assert "\x1b[" not in result.stderr
    assert len(result.stderr) < 400


# -------------------------------------------------------- unusable responses


@pytest.mark.parametrize(
    "handler_kwargs",
    [
        {"text": "<html>nginx</html>"},
        {"text": ""},
        {"body": [1, 2, 3]},
        {"body": "a string"},
    ],
)
def test_a_body_that_is_not_a_json_object_exits_failure_with_a_hint(
    runner: CliRunner, controller: TransportFactory, handler_kwargs: dict[str, Any]
) -> None:
    controller(answering(200, **handler_kwargs))
    result = runner.invoke(app, ["nodes"])
    assert result.exit_code == ExitCode.FAILURE
    assert "really a MeshForge controller" in result.stderr
    assert_no_traceback(result.output)


def test_a_failure_body_that_is_not_json_still_exits_with_the_status(
    runner: CliRunner, controller: TransportFactory
) -> None:
    controller(answering(502, text="Bad Gateway"))
    result = runner.invoke(app, ["nodes"])
    assert result.exit_code == ExitCode.FAILURE
    assert "502" in result.stderr
    assert_no_traceback(result.output)


# ------------------------------------------------------------ compute status


def test_status_reports_an_unreachable_controller_and_still_exits_unavailable(
    runner: CliRunner, controller: TransportFactory
) -> None:
    """``status`` answers "is the fabric up?", so it prints the answer *and* exits
    non-zero for a script."""
    controller(raising(httpx.ConnectError("refused")))
    result = runner.invoke(app, ["status"])
    assert result.exit_code == ExitCode.UNAVAILABLE
    assert "reachable" in result.stdout
    assert "no" in result.stdout
    assert_no_traceback(result.output)


def test_status_json_is_still_one_parseable_object_when_unreachable(
    runner: CliRunner, controller: TransportFactory
) -> None:
    controller(raising(httpx.ConnectError("refused")))
    result = runner.invoke(app, ["status", "--json"])
    assert result.exit_code == ExitCode.UNAVAILABLE
    body = json.loads(result.stdout)
    assert body["reachable"] is False
    assert body["nodes"] is None
    assert "cannot reach the controller" in body["detail"]


# ------------------------------------------------------------------- --watch


def test_ctrl_c_during_watch_exits_zero(
    runner: CliRunner, controller: TransportFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    renders = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal renders
        renders += 1
        return httpx.Response(200, json={"nodes": [], "count": 0})

    def fake_sleep(_seconds: float) -> None:
        if renders >= 2:
            raise KeyboardInterrupt

    controller(handler)
    monkeypatch.setattr("meshforge.cli.app.time.sleep", fake_sleep)
    result = runner.invoke(app, ["nodes", "--watch"])
    assert result.exit_code == ExitCode.OK, result.output
    assert renders == 2
    assert_no_traceback(result.output)


def test_watch_stops_on_the_first_failure_with_the_typed_exit_code(
    runner: CliRunner, controller: TransportFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller(raising(httpx.ConnectError("refused")))
    monkeypatch.setattr(
        "meshforge.cli.app.time.sleep",
        lambda _s: pytest.fail("the loop must not sleep after a failure"),
    )
    result = runner.invoke(app, ["nodes", "--watch"])
    assert result.exit_code == ExitCode.UNAVAILABLE
    assert_no_traceback(result.output)


def test_watch_is_ignored_with_json_so_a_pipe_gets_one_object(
    runner: CliRunner, controller: TransportFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller(answering(200, body={"nodes": [], "count": 0}))
    monkeypatch.setattr(
        "meshforge.cli.app.time.sleep",
        lambda _s: pytest.fail("--json must not loop"),
    )
    result = runner.invoke(app, ["nodes", "--watch", "--json"])
    assert result.exit_code == ExitCode.OK
    assert json.loads(result.stdout) == {"nodes": [], "count": 0}
