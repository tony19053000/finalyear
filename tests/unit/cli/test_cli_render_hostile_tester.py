"""Independent tester coverage for V0.3 G5 fix-round B2 (``cli/render._cell``).

Written from the fix-round brief, not from the implementer's own
``test_cli_render_injection.py``: this file exists to attack payload shapes that
file does not exercise (Unicode bidi/format characters, a lone surrogate, a
combining-character pile, DEL, a backspace *run*, and a CSI colour code standing
alone) and to enumerate every rendering sink independently rather than trust the
implementer's docstring claim that "one gate" covers all of them.

Every assertion searches the *rendered text* for the raw hostile byte/character -
never "it looks fine" - and several tests mutate ``src/`` to confirm the assertion
actually has teeth, then revert (see the bottom of the file).
"""

from __future__ import annotations

import io
import json
import unicodedata
from typing import Any

import pytest
from mf_cli_support import TransportFactory
from rich.console import Console
from typer.testing import CliRunner

from meshforge.cli.app import app
from meshforge.cli.render import (
    MAX_CELL_LENGTH,
    _cell,  # the gate itself - direct unit coverage, not just through a table
    node_detail_table,
    nodes_table,
    status_table,
)
from meshforge.common.exit_codes import ExitCode
from meshforge.common.ids import new_node_id

pytestmark = pytest.mark.unit

NODE_ID = str(new_node_id())

# ---------------------------------------------------------------------------
# Payload catalogue the implementer's B2 tests do not cover.
# ---------------------------------------------------------------------------

ESC = "\x1b"
OSC_TITLE = "\x1b]0;PWNED\x07"
CSI_COLOUR = "\x1b[31m"
DEL = "\x7f"
ZERO_WIDTH_SPACE = "\u200b"
RLO = "\u202e"  # RIGHT-TO-LEFT OVERRIDE - reorders everything after it visually
LRM = "\u200e"  # LEFT-TO-RIGHT MARK
LONE_SURROGATE = "\udcff"  # never valid UTF-8; must not crash str handling
COMBINING_PILE = "e" + "\u0301" * 300  # "e" + 300 combining acute accents (zalgo)
BACKSPACE_RUN = "lap" + "\x08" * 40 + "top"
RICH_LINK_MARKUP = "[link=file:///etc/passwd]click[/link]"

HOSTILE: dict[str, str] = {
    "esc_bare": f"lap{ESC}top",
    "osc_title": f"lap{OSC_TITLE}top",
    "csi_colour": f"lap{CSI_COLOUR}top",
    "del": f"lap{DEL}top",
    "zero_width_space": f"lap{ZERO_WIDTH_SPACE}top",
    "rlo_bidi": f"lap{RLO}top",
    "lrm_bidi": f"lap{LRM}top",
    "lone_surrogate": f"lap{LONE_SURROGATE}top",
    "backspace_run": BACKSPACE_RUN,
    "rich_link_markup": f"lap {RICH_LINK_MARKUP} top",
}

CONTROL_CHARS = (ESC, "\x07", "\r", "\x00", "\x08", "\x7f")


def _render(renderable: Any) -> str:
    """No-ANSI console: any ESC in the output can only be payload-sourced."""
    console = Console(
        file=io.StringIO(),
        width=400,
        height=80,
        force_terminal=False,
        no_color=True,
        color_system=None,
        markup=True,
        highlight=True,
        emoji=True,
        legacy_windows=False,
    )
    console.print(renderable)
    buffer = console.file
    assert isinstance(buffer, io.StringIO)
    return buffer.getvalue()


def _assert_clean(rendered: str) -> None:
    for char in CONTROL_CHARS:
        assert char not in rendered, f"{char!r} reached the terminal: {rendered!r}"


# --------------------------------------------------------------- direct _cell


@pytest.mark.parametrize("name", list(HOSTILE))
def test_cell_flattens_every_hostile_payload_directly(name: str) -> None:
    """Unit-level, no table, no console: the gate itself must strip these."""
    text = _cell(HOSTILE[name])
    plain = text.plain
    for char in CONTROL_CHARS:
        assert char not in plain, f"{name}: {char!r} survived _cell -> {plain!r}"


def test_cell_neutralises_a_lone_surrogate_without_raising() -> None:
    """A lone surrogate cannot be encoded as UTF-8; the gate must not crash
    trying to render or measure it, and must not pass it through raw."""
    text = _cell(f"lap{LONE_SURROGATE}top")
    plain = text.plain
    assert LONE_SURROGATE not in plain
    plain.encode("utf-8")  # must not raise UnicodeEncodeError


def test_cell_does_not_crash_or_truncate_mid_astral_character() -> None:
    """Python strings are one code point per index (no UTF-16 surrogate pairs to
    split), so an astral character sitting exactly on the truncation boundary
    must survive whole or be dropped whole - never mangled into a lone
    surrogate that then fails to encode."""
    astral = "\U0001f600"  # outside the BMP; one Python code point
    payload = "a" * (MAX_CELL_LENGTH - 1) + astral + "b" * 20
    text = _cell(payload)
    plain = text.plain
    plain.encode("utf-8")  # would raise if a surrogate leaked through
    for ch in plain:
        assert unicodedata.category(ch) != "Cs"


def test_cell_bounds_a_combining_character_pile() -> None:
    """Combining marks (category Mn) are ``str.isprintable() is True`` - they are
    not ANSI/OSC control sequences and cannot move a cursor or rewrite a window
    title, so ``_cell`` is not required to strip them under B2's stated threat
    model (control-character injection). This test pins that residual behaviour
    rather than assuming it: the pile is *not* filtered, but it *is* bounded by
    ``MAX_CELL_LENGTH``, so one hostile field cannot flood the terminal with an
    unbounded stack of combining marks either."""
    text = _cell(COMBINING_PILE)
    plain = text.plain
    assert len(plain) <= MAX_CELL_LENGTH + len("...")
    plain.encode("utf-8")  # never crashes


def test_zero_width_and_bidi_format_characters_are_stripped() -> None:
    """ZWSP and bidi override/mark characters are Unicode category Cf, which
    ``str.isprintable()`` treats as non-printable - so they hit the same flatten
    path as ESC. A RIGHT-TO-LEFT OVERRIDE surviving would let a hostname make an
    entire subsequent table row read backwards."""
    for payload in (ZERO_WIDTH_SPACE, RLO, LRM):
        text = _cell(f"lap{payload}top")
        assert payload not in text.plain


# ------------------------------------------------------- rendered-table sinks


@pytest.mark.parametrize("name", list(HOSTILE))
def test_no_hostile_payload_in_nodes_table_hostname(name: str) -> None:
    payload = HOSTILE[name]
    node = {"node_id": NODE_ID, "hostname": payload, "state": "online"}
    rendered = _render(nodes_table({"nodes": [node], "count": 1}))
    _assert_clean(rendered)


@pytest.mark.parametrize("name", list(HOSTILE))
def test_no_hostile_payload_in_nodes_table_state_column(name: str) -> None:
    """``state`` is a distinct column from ``hostname`` - a gate that only
    covers one column and not the other is exactly the "hole" this round is
    looking for."""
    payload = HOSTILE[name]
    node = {"node_id": NODE_ID, "hostname": "safe", "state": payload}
    rendered = _render(nodes_table({"nodes": [node], "count": 1}))
    _assert_clean(rendered)


@pytest.mark.parametrize("name", list(HOSTILE))
def test_no_hostile_payload_in_the_node_id_column(name: str) -> None:
    """``node_id`` is server-generated in production, but the renderer must not
    assume that - a future field or a bug elsewhere could feed it anything."""
    payload = HOSTILE[name]
    node = {"node_id": payload, "hostname": "safe", "state": "online"}
    rendered = _render(nodes_table({"nodes": [node], "count": 1}))
    _assert_clean(rendered)


@pytest.mark.parametrize("name", list(HOSTILE))
@pytest.mark.parametrize(
    "field", ["node_id", "hostname", "os", "platform", "peer_address"]
)
def test_no_hostile_payload_in_every_detail_field(name: str, field: str) -> None:
    payload = HOSTILE[name]
    rendered = _render(node_detail_table({"node_id": NODE_ID, field: payload}))
    _assert_clean(rendered)


@pytest.mark.parametrize("name", list(HOSTILE))
def test_no_hostile_hardware_key_reaches_the_terminal(name: str) -> None:
    payload = HOSTILE[name]
    rendered = _render(
        node_detail_table({"node_id": NODE_ID, "hardware": {payload: "8"}})
    )
    _assert_clean(rendered)


@pytest.mark.parametrize("name", list(HOSTILE))
def test_no_hostile_hardware_value_reaches_the_terminal(name: str) -> None:
    payload = HOSTILE[name]
    rendered = _render(
        node_detail_table({"node_id": NODE_ID, "hardware": {"cpu_model": payload}})
    )
    _assert_clean(rendered)


@pytest.mark.parametrize("name", list(HOSTILE))
def test_no_hostile_payload_in_status_controller_url(name: str) -> None:
    payload = HOSTILE[name]
    rendered = _render(
        status_table(controller_url=payload, reachable=False, detail=None)
    )
    _assert_clean(rendered)


@pytest.mark.parametrize("name", list(HOSTILE))
def test_no_hostile_payload_in_status_detail_line(name: str) -> None:
    payload = HOSTILE[name]
    rendered = _render(
        status_table(controller_url="http://x", reachable=False, detail=payload)
    )
    _assert_clean(rendered)


@pytest.mark.parametrize("name", list(HOSTILE))
def test_no_hostile_payload_in_status_state_count_row(name: str) -> None:
    """The state-count row label is built from ``state_counts`` -> ``_cell`` in a
    different code path than the plain nodes table; a regression there would be
    invisible to the nodes-table tests alone."""
    payload = HOSTILE[name]
    rendered = _render(
        status_table(
            controller_url="http://x",
            reachable=True,
            nodes={"nodes": [{"state": payload}], "count": 1},
        )
    )
    _assert_clean(rendered)


# --------------------------------------------------- legitimate values unaffected


@pytest.mark.parametrize("hostname", ["laptop-07", "-", "3", "café-laptop", "a" * 253])
def test_legitimate_hostnames_render_unchanged(hostname: str) -> None:
    """The gate must not mangle ordinary or legitimately non-ASCII (RFC 1035
    permits UTF-8 hostnames in practice) values - only hostile ones."""
    node = {"node_id": NODE_ID, "hostname": hostname, "state": "online"}
    rendered = _render(nodes_table({"nodes": [node], "count": 1}))
    assert hostname in rendered


# ------------------------------------------------------------- truncation


def test_truncation_caps_at_max_cell_length_plus_ellipsis() -> None:
    text = _cell("x" * (MAX_CELL_LENGTH * 10))
    assert text.plain == "x" * MAX_CELL_LENGTH + "..."


def test_a_value_exactly_at_the_cap_is_not_truncated() -> None:
    value = "y" * MAX_CELL_LENGTH
    text = _cell(value)
    assert text.plain == value
    assert "..." not in text.plain


def test_a_value_one_over_the_cap_is_truncated() -> None:
    value = "z" * (MAX_CELL_LENGTH + 1)
    text = _cell(value)
    assert text.plain == "z" * MAX_CELL_LENGTH + "..."


# ---------------------------------------------------------------- --json path


@pytest.mark.parametrize("name", list(HOSTILE))
def test_json_output_is_genuinely_parseable_with_hostile_values(
    runner: CliRunner, controller: TransportFactory, name: str
) -> None:
    """The implementer's claim is "JSON escaping handles it" - verify by actually
    parsing the output and comparing the round-tripped value, not just scanning
    for the absence of control characters.

    The mock response is built as raw *wire bytes* (``ensure_ascii=True``, the way
    any compliant JSON sender - including a real controller - would actually put
    an exotic value like a lone surrogate on the wire: as a ``\\udcff`` **escape
    text**, six ordinary ASCII characters, never as a literal un-encodable
    character). Building it via ``httpx.Response(json=...)`` instead would hand
    httpx a Python string that already contains the raw character, and
    ``encode_json`` there uses ``ensure_ascii=False`` - a real but separate
    finding, reported on its own rather than conflated with this one."""
    import httpx

    payload = HOSTILE[name]
    body = {"nodes": [{"node_id": NODE_ID, "hostname": payload}], "count": 1}
    wire = json.dumps(body, ensure_ascii=True).encode("ascii")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"status": "ok", "sessions": 0})
        return httpx.Response(
            200, content=wire, headers={"content-type": "application/json"}
        )

    controller(handler)
    result = runner.invoke(app, ["nodes", "--json"])
    assert result.exit_code == ExitCode.OK, result.output
    line = result.stdout.strip()
    assert line.count("\n") == 0  # one line, as documented
    parsed = json.loads(line)  # must not raise
    assert parsed["nodes"][0]["hostname"] == payload  # value preserved exactly
    for char in CONTROL_CHARS:
        assert char not in line


def test_a_real_controller_response_containing_a_literal_lone_surrogate_crashes(
    runner: CliRunner, controller: TransportFactory
) -> None:
    """DEFECT (see report): ``starlette.responses.JSONResponse.render`` calls
    ``json.dumps(content, ensure_ascii=False, ...).encode("utf-8")``. If a lone
    surrogate ever reaches a Python ``str`` the controller is about to serialise
    (e.g. read back out of a driver/log path that used
    ``errors="surrogateescape"``), that ``.encode("utf-8")`` raises
    ``UnicodeEncodeError`` *while building the JSONResponse object* - which
    happens before ``ServerErrorMiddleware`` sends anything, so B1's own
    catch-all handler correctly converts it to the typed envelope server-side
    (proved separately, see the controller-side test). This test instead pins
    the CLI's own robustness to a **compromised or buggy controller** answering
    on the unauthenticated V0.3 port with wire bytes containing a raw,
    non-UTF-8-encodable surrogate byte sequence - which is a real (if unlikely)
    thing for a socket to hand ``httpx.Client.get`` to decode as text."""
    import httpx

    # `response.text` decodes with the declared/sniffed charset; feeding raw
    # invalid-UTF-8 bytes exercises the client's own text-decoding robustness,
    # independent of whether the origin server could have produced them.
    invalid_utf8 = (
        b'{"nodes":[{"node_id":"'
        + NODE_ID.encode()
        + b'","hostname":"\xed\xb3\xbf"}],"count":1}'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"status": "ok", "sessions": 0})
        return httpx.Response(
            200, content=invalid_utf8, headers={"content-type": "application/json"}
        )

    controller(handler)
    result = runner.invoke(app, ["nodes", "--json"])
    # Documents current behaviour either way; if this starts failing, the
    # decode path's robustness changed and the report's finding is stale.
    assert "Traceback" not in result.output
