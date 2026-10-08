"""V0.3 G5 reviewer fix round B2: agent text cannot drive the operator's terminal.

gRPC enrolment is unauthenticated in V0.3 and the controller validates
``Hello.hostname`` / ``os`` / ``platform`` / ``hardware`` for **length only**, so
any host on the LAN can enrol with a crafted hostname. ``compute nodes`` then
prints it on every operator's terminal - an OSC sequence rewrites the window
title, an SGR sequence can colour another node's row into invisibility, and a
``\\r`` or cursor move can rewrite the table above. These tests assert the raw
control characters are **absent from the rendered bytes**, not that the output
"looks fine".

Rich markup is the second vector: ``[bold red]`` in a hostname must be printed
literally. ``_cell`` returns a :class:`rich.text.Text`, which Rich never parses
as markup whatever the console's own ``markup`` setting is.

``--json`` is deliberately excluded: it prints the API payload for scripting, and
``json.dumps`` already escapes a control character to ``\\u001b`` - the bytes on
the terminal are safe without the renderer touching them.
"""

from __future__ import annotations

import io
import json
from typing import Any

import pytest
from mf_cli_support import TransportFactory
from rich.console import Console
from typer.testing import CliRunner

from meshforge.cli.app import app
from meshforge.cli.render import (
    MAX_CELL_LENGTH,
    node_detail_table,
    nodes_table,
    status_table,
)
from meshforge.common.exit_codes import ExitCode
from meshforge.common.ids import new_node_id

pytestmark = pytest.mark.unit

NODE_ID = str(new_node_id())
"""A syntactically valid id: `compute nodes <id>` validates it before requesting."""

HOSTILE: dict[str, str] = {
    "esc": "lap\x1btop",
    "osc_title": "lap\x1b]0;PWNED\x07top",
    "sgr_hide": "lap\x1b[8mtop",
    "carriage_return": "lap\rtop",
    "newline": "lap\ntop",
    "nul": "lap\x00top",
    "cursor_up": "lap\x1b[2Atop",
    "rich_markup": "[bold red]lap[/]top",
    "backspace": "lap\btop",
    "vertical_tab": "lap\vtop",
}
"""Every value flattens to something containing ``lap`` and ``top``, so a test can
assert the payload was *rendered* and not silently dropped."""

CONTROL_CHARS = ("\x1b", "\x07", "\r", "\n", "\x00", "\b", "\v")

_FIELDS = ["hostname", "state", "node_id"]
"""The columns `nodes_table` actually renders (`os`/`platform` are detail-only)."""


def _render(renderable: Any, *, ansi: bool = False) -> str:
    """Render with markup *on* - the hostile case, not the easy one.

    ``compute`` itself uses ``markup=False``, but a renderable must be safe on its
    own: the V0.8 dashboard and any future command may print it through a console
    configured differently.

    ``ansi=False`` (the default) means Rich emits **no** escape sequence of its
    own, so any ESC found in the output can only have come from the payload -
    which is what makes the "no control characters" assertion meaningful rather
    than a check against Rich's own styling.
    """
    console = Console(
        file=io.StringIO(),
        # Both dimensions: with only `width`, Rich falls back to 80x25 under the
        # `TERM=dumb` the CLI conftest sets, and a folded cell would hide a
        # payload from the assertions below.
        width=400,
        height=50,
        force_terminal=ansi,
        color_system="truecolor" if ansi else None,
        no_color=not ansi,
        markup=True,
        highlight=True,
        emoji=True,
        legacy_windows=False,
    )
    console.print(renderable)
    buffer = console.file
    assert isinstance(buffer, io.StringIO)
    return buffer.getvalue()


def _assert_no_control_characters(rendered: str, *, payload: str) -> None:
    for char in CONTROL_CHARS:
        if char == "\n":
            continue  # Rich ends every table line with one; it is not from us
        assert char not in rendered, (
            f"{char!r} from {payload!r} reached the terminal: {rendered!r}"
        )
    # The terminal styling Rich itself emits is fine; a payload-sourced escape is
    # not. Since no cell may contain ESC at all, the check above covers both.
    assert "PWNED" not in rendered or "\x1b]0;" not in rendered


# ---------------------------------------------------------------- the table


@pytest.mark.parametrize("field", _FIELDS)
@pytest.mark.parametrize("name", list(HOSTILE))
def test_no_hostile_string_in_a_table_cell_reaches_the_terminal(
    field: str, name: str
) -> None:
    payload = HOSTILE[name]
    node = {"node_id": NODE_ID, "hostname": "safe", "state": "online", field: payload}
    rendered = _render(nodes_table({"nodes": [node], "count": 1}))
    _assert_no_control_characters(rendered, payload=payload)
    assert "lap" in rendered  # the row was rendered, not dropped


def test_rich_markup_in_a_hostname_is_not_interpreted_as_style() -> None:
    """``[bold red]`` must print literally; ``Text`` is the mechanism that does it."""
    node = {"node_id": NODE_ID, "hostname": "[bold red]danger[/bold red]"}
    rendered = _render(nodes_table({"nodes": [node], "count": 1}), ansi=True)
    assert "[bold red]danger" in rendered  # printed literally
    assert "\x1b[1;31m" not in rendered  # bold red was never applied


def test_a_very_long_cell_is_truncated_so_it_cannot_flood_the_terminal() -> None:
    node = {"node_id": NODE_ID, "hostname": "h" * (MAX_CELL_LENGTH * 40)}
    rendered = _render(nodes_table({"nodes": [node], "count": 1}))
    assert "h" * (MAX_CELL_LENGTH + 1) not in rendered
    assert "..." in rendered


# --------------------------------------------------------------- the detail


@pytest.mark.parametrize("field", ["hostname", "os", "platform", "peer_address"])
@pytest.mark.parametrize("name", list(HOSTILE))
def test_no_hostile_string_in_the_detail_view_reaches_the_terminal(
    field: str, name: str
) -> None:
    payload = HOSTILE[name]
    rendered = _render(node_detail_table({"node_id": NODE_ID, field: payload}))
    _assert_no_control_characters(rendered, payload=payload)


@pytest.mark.parametrize("name", list(HOSTILE))
def test_no_hostile_hardware_value_or_key_reaches_the_terminal(name: str) -> None:
    """``hardware`` is stored as the agent sent it, keys included (D13)."""
    payload = HOSTILE[name]
    rendered = _render(
        node_detail_table(
            {
                "node_id": NODE_ID,
                "hardware": {"cpu_model": payload, payload: "8", "nested": [payload]},
            }
        )
    )
    _assert_no_control_characters(rendered, payload=payload)


def test_a_hardware_value_that_is_not_a_string_still_renders() -> None:
    table = node_detail_table(
        {"node_id": NODE_ID, "hardware": {"cpu_threads": 8, "gpus": None}}
    )
    rendered = _render(table)
    assert "hardware.cpu_threads" in rendered
    assert "8" in rendered


# ------------------------------------------------- end to end through the CLI


def _handler(payload: dict[str, Any]) -> Any:
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"status": "ok", "sessions": 1})
        return httpx.Response(200, json=payload)

    return handler


@pytest.mark.parametrize("name", list(HOSTILE))
def test_compute_nodes_prints_no_control_character_from_the_payload(
    runner: CliRunner, controller: TransportFactory, name: str
) -> None:
    payload = HOSTILE[name]
    controller(
        _handler(
            {
                "nodes": [{"node_id": NODE_ID, "hostname": payload, "os": payload}],
                "count": 1,
            }
        )
    )
    result = runner.invoke(app, ["nodes"])
    assert result.exit_code == ExitCode.OK, result.output
    _assert_no_control_characters(result.stdout, payload=payload)


@pytest.mark.parametrize("name", list(HOSTILE))
def test_compute_nodes_detail_prints_no_control_character_from_the_payload(
    runner: CliRunner, controller: TransportFactory, name: str
) -> None:
    payload = HOSTILE[name]
    controller(
        _handler(
            {
                "node_id": NODE_ID,
                "hostname": payload,
                "platform": payload,
                "hardware": {"cpu_model": payload},
            }
        )
    )
    result = runner.invoke(app, ["nodes", NODE_ID])
    assert result.exit_code == ExitCode.OK, result.output
    _assert_no_control_characters(result.stdout, payload=payload)


@pytest.mark.parametrize("name", list(HOSTILE))
def test_compute_status_prints_no_control_character_from_the_payload(
    runner: CliRunner, controller: TransportFactory, name: str
) -> None:
    payload = HOSTILE[name]
    controller(
        _handler(
            {"nodes": [{"node_id": NODE_ID, "state": payload}], "count": 1},
        )
    )
    result = runner.invoke(app, ["status"])
    assert result.exit_code == ExitCode.OK, result.output
    _assert_no_control_characters(result.stdout, payload=payload)


def test_a_hostile_state_value_cannot_forge_a_state_count_row() -> None:
    """``state_counts`` feeds ``status_table`` row labels, so it is a sink too."""
    rendered = _render(
        status_table(
            controller_url="https://127.0.0.1:47110",
            reachable=True,
            nodes={"nodes": [{"state": "onl\x1b[31mine"}], "count": 1},
        )
    )
    _assert_no_control_characters(rendered, payload="onl\x1b[31mine")


# ----------------------------------------------------------------- --json


@pytest.mark.parametrize("name", list(HOSTILE))
def test_json_output_escapes_control_characters_instead_of_emitting_them(
    runner: CliRunner, controller: TransportFactory, name: str
) -> None:
    """``--json`` passes the payload through verbatim, which is correct: JSON
    string escaping turns ESC into ``\\u001b``, so nothing reaches the terminal as
    a control character and a consumer still sees the exact value."""
    payload = HOSTILE[name]
    body = {"nodes": [{"node_id": NODE_ID, "hostname": payload}], "count": 1}
    controller(_handler(body))
    result = runner.invoke(app, ["nodes", "--json"])
    assert result.exit_code == ExitCode.OK, result.output
    line = result.stdout.strip()
    for char in CONTROL_CHARS:
        assert char not in line
    assert json.loads(line)["nodes"][0]["hostname"] == payload  # value preserved
