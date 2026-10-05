"""V0.6 G4: ``compute network`` rendering and ``compute explain`` term cells.

* rates in decimal units, ages in s/m/h, ``-`` for anything not a number;
* the matrix has one row and column per node seen, ``x`` on the diagonal,
  ``(stale)`` and ``fail:<code>`` marked, control characters flattened;
* an explain transfer cell names where its bandwidth came from.
"""

from __future__ import annotations

from typing import Any

from rich.console import Console

import meshforge.cli.app  # noqa: F401 - the command modules import it back
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
    assert _term_cell(None) == "-"
