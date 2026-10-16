"""V0.6 G4: ``compute network`` rendering and ``compute explain`` term cells.

* rates in decimal units, ages in s/m/h, ``-`` for anything not a number;
* the matrix has one row and column per node seen, ``x`` on the diagonal,
  ``(stale)`` and ``fail:<code>`` marked, control characters flattened;
* the command itself (real client and renderer, faked transport): matrix with
  ages, stale mark, legend, controller links, status line; nothing measured;
  ``--json``; a link with non-numeric fields prints dashes, never a traceback;
* an explain transfer cell names where its bandwidth came from.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from mf_cli_support import TransportFactory
from rich.console import Console
from typer.testing import CliRunner

from meshforge.cli.app import app
from meshforge.cli.explain_commands import _term_cell
from meshforge.cli.network_commands import format_age, format_rate, matrix_table

A = "node_01J0000000000000000000AAAA"
B = "node_01J0000000000000000000BBBB"


def render(table: Any) -> str:
    console = Console(width=250, record=True, color_system=None)
    console.print(table)
    return console.export_text()


def test_format_rate_and_age() -> None:
    assert format_rate(12_345_678) == "12.3 MB/s"
    assert format_rate(2.5e9) == "2.5 GB/s"
    assert format_rate(1500) == "1.5 kB/s"
    assert format_rate(12) == "12 B/s"
    for bad in (None, "x", -1, True):
        assert format_rate(bad) == "-"
    assert (format_age(5), format_age(600), format_age(7300)) == ("5s", "10m", "2.0h")
    assert format_age(None) == "-"


def test_matrix_marks_stale_failures_and_the_diagonal() -> None:
    payload = {
        "links": [
            {
                "src_node_id": A,
                "dst_node_id": B,
                "bw_bytes_s": 40e6,
                "rtt_ms": 3.2,
                "age_s": 30,
                "stale": True,
            }
        ],
        "failures": [{"src_node_id": B, "dst_node_id": A, "error": "http_403\x1b[2J"}],
    }
    text = render(matrix_table(payload))
    assert "AAAA" in text and "BBBB" in text
    assert "40.0 MB/s 3.2ms 30s (stale)" in text
    assert "fail:http_403" in text and "\x1b" not in text
    rows = [line for line in text.splitlines() if "AAAA" in line or "BBBB" in line]
    body = [line for line in rows if "FROM" not in line]
    assert len(body) == 2 and all(" x " in line for line in body)


def test_explain_cells_name_the_bandwidth_source() -> None:
    probe = {
        "seconds": 16.0,
        "source": "measured",
        "detail": {"bw_from": A, "bw_source": "probe"},
    }
    assert _term_cell(probe) == "16.00s (measured, from 0000AAAA)"
    ctl = {"seconds": 2.0, "source": "history", "detail": {"bw_from": "controller"}}
    assert _term_cell(ctl) == "2.00s (history, from controller)"
    env = {"seconds": 0.0, "source": "measured", "detail": {"env_cached": True}}
    assert _term_cell(env) == "0.00s (measured, cached)"
    plain = {"seconds": 1.0, "source": "assumed", "detail": {"bw_source": "assumed"}}
    assert _term_cell(plain) == "1.00s (assumed)"
    stale = {
        "seconds": 8.0,
        "source": "assumed",
        "detail": {"bw_source": "assumed", "stale_links_ignored": [A, B]},
    }
    assert _term_cell(stale) == "8.00s (assumed, 2 stale probe(s) ignored)"
    assert _term_cell(None) == "-"


def test_explain_names_every_source_of_a_per_tree_transfer() -> None:
    """D131: a transfer costed over several sources names each one - the
    holder of the small tree is never credited with the controller's bytes."""
    mixed = {
        "seconds": 100.5,
        "source": "history",
        "detail": {
            "bw_source": "mixed",
            "items": [
                {
                    "bw_source": "transfer_history",
                    "bw_from": "controller",
                    "last_transfer_age_s": 7200.0,
                },
                {"bw_source": "probe", "bw_from": A},
                {"bw_source": "probe", "bw_from": A},
            ],
        },
    }
    assert _term_cell(mixed) == (
        "100.50s (history, from controller (last transfer 2h ago) + 0000AAAA)"
    )
    assumed = {
        "seconds": 4.5,
        "source": "assumed",
        "detail": {
            "bw_source": "mixed",
            "items": [
                {"bw_source": "assumed"},
                {"bw_source": "probe", "bw_from": B},
            ],
        },
    }
    assert _term_cell(assumed) == "4.50s (assumed, from default + 0000BBBB)"
    holders = {
        "seconds": 22.0,
        "source": "measured",
        "detail": {
            "bw_source": "probe",
            "bw_holders": [A, B],
            "items": [
                {"bw_from": A},
                {"bw_from": B},
                {"bw_from": "n3"},
                {"bw_from": "n4"},
            ],
        },
    }
    assert _term_cell(holders) == (
        "22.00s (measured, from 0000AAAA + 0000BBBB + n3 + 1 more)"
    )


def test_explain_shows_the_age_of_controller_history_and_a_clamped_link() -> None:
    old = {
        "seconds": 10.0,
        "source": "history",
        "detail": {"bw_from": "controller", "last_transfer_age_s": 90000.0},
    }
    assert (
        _term_cell(old) == "10.00s (history, from controller (last transfer 25h ago))"
    )
    unknown = {
        "seconds": 10.0,
        "source": "history",
        "detail": {"bw_from": "controller", "last_transfer_age_s": None},
    }
    assert _term_cell(unknown) == "10.00s (history, from controller)"
    floor = {
        "seconds": 1024.0,
        "source": "measured",
        "detail": {"bw_from": A, "bw_clamped": True},
    }
    assert _term_cell(floor) == (
        "1024.00s (measured, from 0000AAAA, at the bandwidth floor)"
    )


def test_explain_cells_survive_hostile_item_lists() -> None:
    for items in ("x", [1, None, "a", {"bw_from": 7}], {"a": 1}, [[]]):
        term = {"seconds": 1.0, "source": "assumed", "detail": {"items": items}}
        assert _term_cell(term) == "1.00s (assumed)"
    seconds_none = {"seconds": None, "source": "measured", "detail": {}}
    assert _term_cell(seconds_none) == "- (measured)"


# ------------------------------------------------- the command, end to end


def _serve(payload: Any, seen: list[str] | None = None) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(f"{request.method} {request.url.path}")
        return httpx.Response(200, json=payload)

    return handler


PROBER = {
    "enabled": True,
    "started": 4,
    "measured": 3,
    "failed": 1,
    "timed_out": 0,
    "dropped": 0,
    "in_flight": [],
    "budget_bytes_per_min": 48 * 1024 * 1024,
    "budget_used_bytes": 12 * 1024 * 1024,
}


def test_compute_network_prints_the_matrix_ages_and_status(
    runner: CliRunner, controller: TransportFactory
) -> None:
    seen: list[str] = []
    payload = {
        "stale_after_s": 3600,
        "links": [
            {
                "src_node_id": A,
                "dst_node_id": B,
                "bw_bytes_s": 40e6,
                "rtt_ms": 3.2,
                "age_s": 30,
                "stale": False,
            },
            {
                "src_node_id": B,
                "dst_node_id": A,
                "bw_bytes_s": 9e6,
                "rtt_ms": 4.0,
                "age_s": 7300,
                "stale": True,
            },
        ],
        "controller_links": [
            {"node_id": A, "bw_bytes_s": 5e6, "samples": 7, "age_s": 12}
        ],
        "failures": [],
        "prober": PROBER,
    }
    controller(_serve(payload, seen))
    result = runner.invoke(app, ["network"])
    assert result.exit_code == 0, result.output
    assert seen == ["GET /v1/network"]
    out = result.output
    assert "40.0 MB/s 3.2ms 30s" in out and "40.0 MB/s 3.2ms 30s (stale)" not in out
    assert "9.0 MB/s 4.0ms 2.0h (stale)" in out
    assert f"0000AAAA = {A}" in out and f"0000BBBB = {B}" in out
    assert "controller links (from transfer history)" in out and "5.0 MB/s" in out
    assert "probes: on - started 4, measured 3, failed 1" in out
    assert "stale after 60m" in out


def test_compute_network_with_nothing_measured_says_so(
    runner: CliRunner, controller: TransportFactory
) -> None:
    controller(
        _serve(
            {
                "links": [],
                "controller_links": [],
                "failures": [],
                "prober": {**PROBER, "enabled": False},
            }
        )
    )
    result = runner.invoke(app, ["network"])
    assert result.exit_code == 0, result.output
    assert "no node pair measured yet" in result.output
    assert "probes: off" in result.output


def test_compute_network_json_is_the_api_answer(
    runner: CliRunner, controller: TransportFactory
) -> None:
    payload = {"links": [], "controller_links": [], "failures": [], "prober": PROBER}
    controller(_serve(payload))
    result = runner.invoke(app, ["network", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {**payload, "schema_version": 1}  # V0.8 G6


@pytest.mark.parametrize("bad", [None, "fast", True, -1, [1], {"x": 1}])
def test_a_link_with_non_numeric_fields_is_dashes_not_a_traceback(
    runner: CliRunner, controller: TransportFactory, bad: Any
) -> None:
    """The controller's answer is input: a cell never raises on its types."""
    link = {
        "src_node_id": A,
        "dst_node_id": B,
        "bw_bytes_s": bad,
        "rtt_ms": bad,
        "age_s": bad,
        "stale": bad,
    }
    controller(_serve({"links": [link, "junk"], "failures": [None], "prober": "nope"}))
    result = runner.invoke(app, ["network"])
    assert result.exit_code == 0, result.output
    assert "Traceback" not in result.output
    assert "0000AAAA" in result.output and "- - -" in result.output
