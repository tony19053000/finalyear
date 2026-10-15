"""Independent tester coverage for V0.3 G5 fix round N2.

``compute nodes NODE_ID`` must reject anything that is not a canonical
``node_<ULID>`` **before building a request** - the pre-fix bug was httpx
collapsing ``../healthz`` into ``GET /v1/healthz``. The implementer's own
``test_cli_g5_fix_round.py`` already covers dot-segment/query-string escapes;
this file adds the payload shapes the fix-round brief calls out that file does
not exercise: a 10 KB id, NUL/newline embedded in the id, a Crockford-invalid
ULID alphabet character (I/L/O/U), and a valid-looking-but-wrong-length ULID
distinct from the "one character short" case already covered.

Every test also asserts **no HTTP request was built at all** - the ``controller``
fixture installs a handler that raises if it is ever called, which is a stronger
check than "the recorded list of URLs is empty".
"""

from __future__ import annotations

import httpx
import pytest
from mf_cli_support import TransportFactory
from typer.testing import CliRunner

from meshforge.cli.app import app
from meshforge.common.exit_codes import ExitCode
from meshforge.common.ids import new_node_id

pytestmark = pytest.mark.unit

VALID_NODE_ID = str(new_node_id())
_, _BODY = VALID_NODE_ID.split("_")


def _never_called(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"no HTTP request should have been built, got {request.url}")


@pytest.fixture
def no_network(controller: TransportFactory) -> None:
    controller(_never_called)


INVALID_IDS = [
    pytest.param("x" * 10_240, id="10kb_id"),
    pytest.param("node_" + "\x00" + _BODY[1:], id="embedded_nul"),
    pytest.param("node_" + "\n" + _BODY[1:], id="embedded_newline"),
    pytest.param("node_" + "I" + _BODY[1:], id="ulid_alphabet_I"),
    pytest.param("node_" + "L" + _BODY[1:], id="ulid_alphabet_L"),
    pytest.param("node_" + "O" + _BODY[1:], id="ulid_alphabet_O"),
    pytest.param("node_" + "U" + _BODY[1:], id="ulid_alphabet_U"),
    pytest.param("node_" + _BODY + "AAAAA", id="valid_looking_but_5_chars_too_long"),
    pytest.param("node_" + _BODY[:20], id="valid_looking_but_6_chars_too_short"),
    pytest.param("job_" + _BODY, id="wrong_kind_prefix"),
    pytest.param("node" + _BODY, id="missing_separator"),
]


@pytest.mark.parametrize("node_id", INVALID_IDS)
def test_an_invalid_node_id_is_rejected_before_any_request_is_built(
    runner: CliRunner, no_network: None, node_id: str
) -> None:
    result = runner.invoke(app, ["nodes", node_id])
    assert result.exit_code == ExitCode.USAGE_ERROR, result.output
    assert "Traceback" not in result.output


@pytest.mark.parametrize("node_id", INVALID_IDS)
def test_the_invalid_value_itself_is_never_echoed(
    runner: CliRunner, no_network: None, node_id: str
) -> None:
    """argv reaches a terminal too; a 10 KB id echoed back would itself be a
    terminal-flooding vector distinct from B2's escape-sequence one."""
    result = runner.invoke(app, ["nodes", node_id])
    # Not `node_id not in output`: a one- or two-character invalid value would
    # make that vacuous by occurring in the error text by chance. A value is
    # only "echoed" if a distinctive run of it survives, so test the longest
    # distinctive slice we have.
    distinctive = node_id[:40]
    if len(distinctive) >= 8:
        assert distinctive not in result.output
    assert len(result.output) < 500  # one short line, not proportional to input


def test_a_syntactically_correct_but_never_enrolled_node_id_is_still_sent(
    runner: CliRunner, controller: TransportFactory
) -> None:
    """N2 validates *shape*, not existence - the 404 for "no such node" is the
    server's job, so a well-formed id must still reach the network."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"status": "ok", "sessions": 0})
        return httpx.Response(
            404, json={"error": {"code": "not_found", "message": "no such node"}}
        )

    controller(handler)
    result = runner.invoke(app, ["nodes", VALID_NODE_ID])
    assert seen == [f"https://127.0.0.1:47110/v1/nodes/{VALID_NODE_ID}"]
    assert result.exit_code == 8, result.output  # V0.8 G6: NOT_FOUND (was FAILURE)


# ------------------------------------------------------- mutation: has teeth?


def test_mutation_a_validator_that_accepts_everything_is_caught(
    runner: CliRunner, no_network: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulates N2 regressing to "no validation at all": patches
    ``_checked_node_id`` to pass every value through unchanged, the way the
    pre-fix code effectively did, and confirms the suite would have failed.
    This does not touch ``src/`` - it monkeypatches the imported symbol for the
    duration of this one test only."""
    import meshforge.cli.app as app_module

    monkeypatch.setattr(app_module, "_checked_node_id", lambda value: value)
    # `no_network` would now legitimately fire for a would-be-valid-looking value,
    # proving the earlier tests are not vacuously true: with validation bypassed,
    # the request reaches the transport and the "never called" handler fires.
    result = runner.invoke(app, ["nodes", "../healthz"])
    assert isinstance(result.exception, AssertionError)
    assert "no HTTP request should" in str(result.exception)
