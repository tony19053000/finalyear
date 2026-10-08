"""V0.3 G5 reviewer fix round: CLI argument validation (N2) and import cost (N7).

* **N2** - ``compute nodes ../healthz`` used to issue ``GET /v1/healthz`` (httpx
  collapses dot segments) and print a row of dashes, and ``compute nodes
  'x?limit=9'`` silently dropped the suffix into a query string. Both are usage
  errors and must never leave the process.
* **N7** - ``compute --version`` is ADR-0010's base-install check. Importing
  ``meshforge.cli.app`` must not drag in ``httpx`` (or the client/renderer), so
  the two commands import them on their first line instead.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Any

import httpx
import pytest
from mf_cli_support import TransportFactory
from typer.testing import CliRunner

import meshforge
from meshforge.cli.app import app
from meshforge.common.exit_codes import ExitCode
from meshforge.common.ids import new_node_id

pytestmark = pytest.mark.unit

VALID_NODE_ID = str(new_node_id())


def _recording_handler(seen: list[str]) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"status": "ok", "sessions": 0})
        return httpx.Response(200, json={"node_id": VALID_NODE_ID})

    return handler


# ------------------------------------------------------------------ N2


@pytest.mark.parametrize(
    "node_id",
    [
        "../healthz",  # httpx would collapse this to GET /v1/healthz
        "x?limit=9",  # the suffix would become a query string
        "//evil.example/v1/nodes",  # would change the host
        "node_not-a-ulid",
        "",
        " ",
        "node_01JQZ0000000000000000000A",  # one character short of a ULID
        VALID_NODE_ID + "extra",
    ],
)
def test_a_non_canonical_node_id_is_a_usage_error_and_never_sent(
    runner: CliRunner, controller: TransportFactory, node_id: str
) -> None:
    seen: list[str] = []
    controller(_recording_handler(seen))
    result = runner.invoke(app, ["nodes", node_id])
    assert result.exit_code == ExitCode.USAGE_ERROR, result.output
    assert seen == []  # no request was built at all
    assert "NODE_ID is not a canonical node_<ULID>" in result.output
    assert "Traceback" not in result.output


def test_the_usage_error_is_one_line_and_does_not_echo_the_value(
    runner: CliRunner, controller: TransportFactory
) -> None:
    """argv reaches a terminal too, so the offending value is not echoed back."""
    seen: list[str] = []
    controller(_recording_handler(seen))
    result = runner.invoke(app, ["nodes", "\x1b]0;PWNED\x07"])
    assert result.exit_code == ExitCode.USAGE_ERROR
    lines = [line for line in result.output.splitlines() if line.strip()]
    assert len(lines) == 1
    assert "\x1b" not in result.output
    assert "PWNED" not in result.output


def test_a_valid_node_id_is_still_requested_verbatim(
    runner: CliRunner, controller: TransportFactory
) -> None:
    seen: list[str] = []
    controller(_recording_handler(seen))
    result = runner.invoke(app, ["nodes", VALID_NODE_ID, "--json"])
    assert result.exit_code == ExitCode.OK, result.output
    assert seen == [f"https://127.0.0.1:47110/v1/nodes/{VALID_NODE_ID}"]


def test_the_bare_list_command_is_unaffected_by_the_validation(
    runner: CliRunner, controller: TransportFactory
) -> None:
    seen: list[str] = []
    controller(_recording_handler(seen))
    result = runner.invoke(app, ["nodes", "--json"])
    assert result.exit_code == ExitCode.OK, result.output
    assert seen == ["https://127.0.0.1:47110/v1/nodes"]


# ------------------------------------------------------------------ N7


def test_version_still_works_in_process(runner: CliRunner) -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == ExitCode.OK
    assert result.output.strip() == f"compute {meshforge.__version__}"


def test_importing_the_cli_app_does_not_import_httpx() -> None:
    """ADR-0010: ``compute --version`` must stay cheap on a base install.

    Asserted on the *import*, not on a wall-clock duration, so the test cannot
    flake on a slow or loaded machine. ``httpx`` stands in for the whole client
    import chain (it is the expensive part).
    """
    script = (
        "import sys; import meshforge.cli.app; "
        "print(int(any(m == 'httpx' or m.startswith('httpx.') for m in sys.modules)))"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    assert result.stdout.strip() == "0", result.stdout


def test_running_version_as_a_subprocess_does_not_import_httpx() -> None:
    """The whole command path, not just the module: ``-X importtime`` names every
    module the process imported, so a re-introduced module-level import shows up
    here even if it lands somewhere other than ``app.py``."""
    result = subprocess.run(
        [sys.executable, "-X", "importtime", "-m", "meshforge.cli", "--version"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == ExitCode.OK, result.stderr
    imported = [
        line.rsplit("|", 1)[-1].strip() for line in result.stderr.splitlines()[1:]
    ]
    assert "httpx" not in imported, "httpx is imported by `compute --version` again"
