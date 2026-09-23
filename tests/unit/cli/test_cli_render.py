"""V0.3 G5 unit tests: what ``compute`` prints, from canned payloads (0.3.12).

The payload is invented, never read from a controller, so these assertions hold on
any machine. Only the transport is faked (``httpx.MockTransport``): the real
client, the real settings and the real Rich renderers run, in-process.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from mf_cli_support import TransportFactory
from typer.testing import CliRunner

from meshforge.cli.app import app
from meshforge.cli.render import format_age, format_bytes, nodes_table, state_counts
from meshforge.common.exit_codes import ExitCode

pytestmark = pytest.mark.unit

NODE_FULL: dict[str, Any] = {
    "node_id": "node_01JQZ0000000000000000000A",
    "hostname": "laptop-one",
    "os": "linux",
    "platform": "x86_64",
    "state": "online",
    "connected": True,
    "last_seen_at": "2026-01-02T03:04:05+00:00",
    "sample_age_s": 1.25,
    "rtt_ms": 3.5,
    "rtt_ewma_ms": 4.0,
    "cpu_percent": 42.4,
    "ram_used_bytes": 2 * 1024**3,
    "ram_total_bytes": 8 * 1024**3,
    "gpus": [
        {
            "index": 0,
            "util_percent": 55.0,
            "vram_used_bytes": 1024,
            "vram_total_bytes": 4096,
        },
        {
            "index": 1,
            "util_percent": 7.0,
            "vram_used_bytes": 0,
            "vram_total_bytes": 4096,
        },
    ],
}
"""A connected node with two GPUs."""

NODE_EMPTY: dict[str, Any] = {
    "node_id": "node_01JQZ0000000000000000000B",
    "hostname": "laptop-two",
    "os": "windows",
    "platform": "AMD64",
    "state": "offline",
    "connected": False,
    "last_seen_at": "2026-01-02T03:04:05+00:00",
    "sample_age_s": None,
    "rtt_ms": None,
    "rtt_ewma_ms": None,
    "cpu_percent": None,
    "ram_used_bytes": None,
    "ram_total_bytes": None,
    "gpus": [],
}
"""Every optional field null - a node that enrolled and never reported."""

NODES_PAYLOAD: dict[str, Any] = {"nodes": [NODE_FULL, NODE_EMPTY], "count": 2}

DETAIL_PAYLOAD: dict[str, Any] = {
    **NODE_FULL,
    "first_seen_at": "2026-01-01T00:00:00+00:00",
    "session_epoch": 4,
    "last_session_id": "sess_01JQZ0000000000000000000C",
    "agent_version": "0.1.0.dev0",
    "protocol_version": 1,
    "peer_address": "192.168.1.77:51000",
    "hardware": {"cpu_threads": 16, "ram_total_bytes": "8589934592"},
}


def json_handler(payload: dict[str, Any], status: int = 200) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"status": "ok", "sessions": 1})
        return httpx.Response(status, json=payload)

    return handler


# --------------------------------------------------------------- formatters


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "-"),
        ("1024", "-"),
        (-1, "-"),
        (0, "0 B"),
        (512, "512 B"),
        (1024, "1.0 KiB"),
        (2 * 1024**3, "2.0 GiB"),
        (5 * 1024**5, "5.0 PiB"),
    ],
)
def test_format_bytes_never_raises_on_an_unusable_value(
    value: object, expected: str
) -> None:
    assert format_bytes(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "-"),
        (-0.5, "-"),
        ("soon", "-"),
        (0.0, "0.0s"),
        (1.25, "1.2s"),
        (59.9, "59.9s"),
        (90.0, "2m"),
        (7200.0, "2h"),
    ],
)
def test_format_age_handles_null_and_the_unit_boundaries(
    value: object, expected: str
) -> None:
    assert format_age(value) == expected


def test_state_counts_groups_nodes_and_labels_a_missing_state_unknown() -> None:
    payload = {
        "nodes": [
            {"state": "online"},
            {"state": "online"},
            {"state": "offline"},
            {},
            "not-a-node",
        ]
    }
    assert state_counts(payload) == {"offline": 1, "online": 2, "unknown": 1}


def test_a_node_with_an_unexpected_extra_field_still_renders() -> None:
    """An older `compute` against a newer controller must degrade, not crash."""
    table = nodes_table(
        {"nodes": [{**NODE_FULL, "future_field": {"x": 1}}], "count": 1}
    )
    assert table.row_count == 1


def test_a_malformed_payload_renders_an_empty_table_instead_of_raising() -> None:
    for payload in ({}, {"nodes": None}, {"nodes": "oops"}, {"nodes": [1, 2]}):
        assert nodes_table(payload).row_count == 0


# ------------------------------------------------------------ compute nodes


def test_nodes_renders_the_documented_columns_and_both_rows(
    runner: CliRunner, controller: TransportFactory
) -> None:
    controller(json_handler(NODES_PAYLOAD))
    result = runner.invoke(app, ["nodes"])
    assert result.exit_code == ExitCode.OK, result.output
    out = result.stdout
    for column in (
        "NODE",
        "HOSTNAME",
        "STATE",
        "CONN",
        "RTT",
        "CPU%",
        "RAM",
        "GPU",
        "AGE",
    ):
        assert column in out
    # the connected node with two GPUs
    assert "laptop-one" in out
    assert "yes" in out
    assert "0:55% 1:7%" in out
    assert "2.0 GiB/8.0 GiB" in out
    assert "1.2s" in out
    # the node that has never reported: dashes, not "None" or "0"
    assert "laptop-two" in out
    assert "None" not in out


def test_the_table_uses_ascii_only_so_a_legacy_windows_console_can_render_it(
    runner: CliRunner, controller: TransportFactory
) -> None:
    controller(json_handler(NODES_PAYLOAD))
    result = runner.invoke(app, ["nodes"])
    assert result.exit_code == ExitCode.OK
    result.stdout.encode("cp437")  # raises UnicodeEncodeError on box-drawing


def test_there_is_no_ansi_colour_when_stdout_is_not_a_tty(
    runner: CliRunner, controller: TransportFactory
) -> None:
    controller(json_handler(NODES_PAYLOAD))
    result = runner.invoke(app, ["nodes"])
    assert "\x1b[" not in result.stdout


def test_nodes_json_is_one_unindented_line_of_the_raw_payload(
    runner: CliRunner, controller: TransportFactory
) -> None:
    controller(json_handler(NODES_PAYLOAD))
    result = runner.invoke(app, ["nodes", "--json"])
    assert result.exit_code == ExitCode.OK, result.output
    line = result.stdout.strip()
    assert "\n" not in line
    assert line.startswith("{") and line.endswith("}")
    assert ": " not in line.split('"hostname"')[0]  # compact separators
    assert json.loads(line) == NODES_PAYLOAD


def test_nodes_json_passes_an_unknown_field_straight_through(
    runner: CliRunner, controller: TransportFactory
) -> None:
    payload = {"nodes": [{**NODE_FULL, "invented_later": 7}], "count": 1}
    controller(json_handler(payload))
    result = runner.invoke(app, ["nodes", "--json"])
    assert json.loads(result.stdout)["nodes"][0]["invented_later"] == 7


def test_nodes_with_an_id_shows_the_detail_including_hardware(
    runner: CliRunner, controller: TransportFactory
) -> None:
    controller(json_handler(DETAIL_PAYLOAD))
    result = runner.invoke(app, ["nodes", str(DETAIL_PAYLOAD["node_id"])])
    assert result.exit_code == ExitCode.OK, result.output
    out = result.stdout
    assert "hardware.cpu_threads" in out
    assert "16" in out
    assert "192.168.1.77:51000" in out
    assert "0.1.0.dev0" in out


def test_nodes_with_an_id_and_json_prints_the_detail_payload(
    runner: CliRunner, controller: TransportFactory
) -> None:
    controller(json_handler(DETAIL_PAYLOAD))
    result = runner.invoke(app, ["nodes", "node_01JQZ0000000000000000000A", "--json"])
    assert json.loads(result.stdout) == DETAIL_PAYLOAD


def test_bare_nodes_still_lists_even_though_an_id_is_accepted(
    runner: CliRunner, controller: TransportFactory
) -> None:
    """The Typer choice: one command with an optional argument, not a sub-typer."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json=NODES_PAYLOAD)

    controller(handler)
    assert runner.invoke(app, ["nodes"]).exit_code == ExitCode.OK
    assert seen == ["/v1/nodes"]


# ----------------------------------------------------------- compute status


def test_status_reports_the_url_counts_by_state_and_live_sessions(
    runner: CliRunner, controller: TransportFactory
) -> None:
    controller(json_handler(NODES_PAYLOAD))
    result = runner.invoke(app, ["status"])
    assert result.exit_code == ExitCode.OK, result.output
    out = result.stdout
    assert "http://127.0.0.1:47110" in out
    assert "yes" in out
    assert "online" in out and "offline" in out
    assert "live sessions" in out


def test_status_json_carries_reachability_and_the_node_payload(
    runner: CliRunner, controller: TransportFactory
) -> None:
    controller(json_handler(NODES_PAYLOAD))
    result = runner.invoke(app, ["status", "--json"])
    body = json.loads(result.stdout)
    assert body["reachable"] is True
    assert body["detail"] is None
    assert body["live_sessions"] == 1
    assert body["nodes"] == NODES_PAYLOAD


def test_version_still_works_without_touching_the_network(runner: CliRunner) -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == ExitCode.OK
    assert result.stdout.startswith("compute ")
