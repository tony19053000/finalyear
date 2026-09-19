"""Unit tests for ``lab/spikes/_s3/tables.py``: the transport x size matrices,
the gRPC chunk sweep, the loopback-ceiling and limit-probe blocks and the
generated Q2 verdict.

Two regressions are pinned here (found in the interrupted first session):

* **best chunk**: the sweep rows share the family ``grpc-down`` and the payload
  size of the default-chunk row, so a naive ``(family, size)`` index let a sweep
  row overwrite the default cell, and "best chunk" was taken over *all* pairs
  and links (the fastest link won, not the best chunk) including INVALID rows;
* **parallel wording**: a ratio below x1 was printed as "x0.8 as fast", and the
  verdict mixed rows of different pairs/links into one comparison.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import pytest

from spikes._harness.envelope import make_envelope
from spikes._harness.reps import CaseResult
from spikes._s3 import tables
from spikes._s3.protocol import GIB, KIB, MIB, format_size
from spikes._s3.run import METRICS

FAKE_GIT = {"commit": "abc1234", "dirty": False}


def _host(name: str) -> dict[str, Any]:
    return {"hostname": name, "os": "Linux", "os_release": "6.8", "python": "3.12"}


def case(
    case_id: str,
    *,
    transport: str,
    size: int,
    chunk: int = MIB,
    mbps: Sequence[float],
    direction: str = "download",
    ranged: bool = False,
    parts: int | None = None,
    sweep: bool = False,
    cpu: float = 1.0,
) -> CaseResult:
    attrs: dict[str, Any] = {
        "transport": transport,
        "direction": direction,
        "size_bytes": size,
        "chunk_bytes": chunk,
    }
    if transport == "http":
        attrs["ranged"] = ranged
        if parts is not None:
            attrs["parts"] = parts
    if sweep:
        attrs["sweep"] = True
    samples = [
        {"recv_mbps": m, "recv_mib_s": m / 8.388608, "cpu_ms_per_mib": cpu}
        for m in mbps
    ]
    return CaseResult(
        case_id=case_id,
        label=case_id,
        metrics=dict(METRICS),
        attrs=attrs,
        requested_reps=5,
        requested_warmup=1,
        samples=samples,
    )


def envelope(
    client: str,
    server: str,
    link: str,
    cases: list[CaseResult],
    *,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return make_envelope(
        spike="S3",
        topic="S3-transport",
        link=link,
        params={"server_name": server, "client_name": client},
        cases=cases,
        extra=extra or {},
        git=FAKE_GIT,
        host=_host(client),
        host_label=client,
        now=datetime(2026, 9, 20, tzinfo=UTC),
    )


def five(value: float, spread: float = 1.0) -> list[float]:
    return [value - spread, value, value + spread, value, value]


def full_run(
    *,
    tcp: float = 940.0,
    grpc: float = 900.0,
    http: float = 930.0,
    ranges: float = 945.0,
    size: int = 64 * MIB,
    sweep: dict[int, float] | None = None,
) -> list[CaseResult]:
    label = format_size(size)
    out = [
        case(f"tcp-down-{label}", transport="tcp", size=size, mbps=five(tcp)),
        case(
            f"tcp-up-{label}",
            transport="tcp",
            size=size,
            mbps=five(tcp - 5),
            direction="upload",
        ),
        case(f"grpc-down-{label}", transport="grpc", size=size, mbps=five(grpc)),
        case(
            f"grpc-up-{label}",
            transport="grpc",
            size=size,
            mbps=five(grpc - 5),
            direction="upload",
        ),
        case(f"http-get-{label}", transport="http", size=size, mbps=five(http)),
        case(
            f"http-range4-{label}",
            transport="http",
            size=size,
            mbps=five(ranges),
            ranged=True,
            parts=4,
        ),
    ]
    for chunk, value in (sweep or {}).items():
        out.append(
            case(
                f"grpc-down-{label}-chunk{format_size(chunk)}",
                transport="grpc",
                size=size,
                chunk=chunk,
                mbps=five(value),
                sweep=True,
            )
        )
    return out


# ---------------------------------------------------------------- collect
def test_family_of_and_names() -> None:
    assert tables.family_of({"transport": "tcp", "direction": "download"}) == "tcp-down"
    assert tables.family_of({"transport": "grpc", "direction": "upload"}) == "grpc-up"
    assert tables.family_of({"transport": "http", "ranged": False}) == "http-get"
    assert (
        tables.family_of({"transport": "http", "ranged": True, "parts": 4})
        == "http-range4"
    )
    assert tables.family_name("http-range4") == "HTTP 4 parallel ranges"
    assert tables.family_name("tcp-down") == "raw TCP (server -> client)"
    assert tables.family_name("weird") == "weird"


def test_collect_flattens_rows_with_validity() -> None:
    doc = envelope("A", "B", "wifi", full_run(sweep={256 * KIB: 800.0}))
    rows = tables.collect([doc])
    assert len(rows) == 7
    by_id = {r.case_id: r for r in rows}
    row = by_id["grpc-down-64MiB"]
    assert row.pair == "A -> B" and row.link == "wifi"
    assert row.family == "grpc-down" and row.size == 64 * MIB and row.chunk == MIB
    assert row.n == 5 and row.valid and row.mbps_mean == pytest.approx(900.0)
    assert row.mbps_stdev is not None and row.cpu_mean == pytest.approx(1.0)
    assert by_id["grpc-down-64MiB-chunk256KiB"].sweep is True
    assert not by_id["grpc-down-64MiB"].sweep


def test_collect_marks_short_rows_invalid() -> None:
    doc = envelope(
        "A", "B", "wifi", [case("tcp-down-1MiB", transport="tcp", size=MIB, mbps=[1, 2])]
    )
    (row,) = tables.collect([doc])
    assert row.n == 2 and not row.valid


# --------------------------------------------------------------- matrices
def test_throughput_matrix_never_shows_a_sweep_row_in_the_default_cell() -> None:
    """Regression: the sweep row ``grpc-down-64MiB-chunk64KiB`` sorts after
    ``grpc-down-64MiB`` and used to overwrite its cell."""
    doc = envelope(
        "A", "B", "ethernet", full_run(grpc=900.0, sweep={64 * KIB: 500.0, 4 * MIB: 700.0})
    )
    text = tables.throughput_tables(tables.collect([doc]))
    grpc_line = next(ln for ln in text.splitlines() if "gRPC server-stream" in ln)
    assert "900.0 ±" in grpc_line and "500.0" not in grpc_line
    cpu = tables.cpu_tables(tables.collect([doc]))
    assert "| raw TCP (server -> client) | 1.0 |" in cpu


def test_matrix_prefers_the_row_with_most_valid_reps_on_rerun() -> None:
    first = envelope(
        "A", "B", "wifi", [case("tcp-down-1MiB", transport="tcp", size=MIB, mbps=[999])]
    )
    second = envelope(
        "A", "B", "wifi", [case("tcp-down-1MiB", transport="tcp", size=MIB, mbps=five(500))]
    )
    rows = tables.collect([first, second])
    index = tables._index(rows)
    assert index[("tcp-down", MIB)].mbps_mean == pytest.approx(500.0)
    text = tables.throughput_tables(rows)
    assert "500.0 ±" in text and "999" not in text


def test_matrix_marks_invalid_and_tbd_cells_and_groups_by_pair_and_link() -> None:
    doc_wifi = envelope(
        "A",
        "B",
        "wifi",
        [
            case("tcp-down-1MiB", transport="tcp", size=MIB, mbps=[100, 110]),
            case("tcp-down-8MiB", transport="tcp", size=8 * MIB, mbps=five(200)),
            case("grpc-down-8MiB", transport="grpc", size=8 * MIB, mbps=five(150)),
        ],
    )
    doc_eth = envelope(
        "A", "C", "ethernet", [case("tcp-down-1MiB", transport="tcp", size=MIB, mbps=five(900))]
    )
    text = tables.throughput_tables(tables.collect([doc_wifi, doc_eth]))
    assert "**A -> B · Wi-Fi**" in text and "**A -> C · Ethernet**" in text
    assert "**INVALID (n=2)**" in text
    grpc_line = next(ln for ln in text.splitlines() if "gRPC server-stream" in ln)
    assert grpc_line.startswith("| gRPC server-stream | TBD | 150.0")
    assert tables.throughput_tables([]).endswith("TBD (no results)\n")
    assert tables.cpu_tables([]) == ""


# ------------------------------------------------------------------ sweep
def test_sweep_rows_include_the_default_chunk_row() -> None:
    doc = envelope(
        "A", "B", "wifi", full_run(grpc=900.0, sweep={64 * KIB: 500.0, 4 * MIB: 700.0})
    )
    rows = tables.sweep_rows(tables.collect([doc]))
    assert [r.chunk for r in rows] == [64 * KIB, MIB, 4 * MIB]
    assert [r.sweep for r in rows] == [True, False, True]
    text = tables.sweep_table(tables.collect([doc]))
    assert "payload 64MiB" in text and "| 1MiB | 900.0 ±" in text
    assert tables.sweep_table(tables.collect([envelope("A", "B", "wifi", full_run())])) == ""


def test_best_chunk_is_per_group_and_ignores_invalid_rows() -> None:
    """Regression: the old code took the max over every pair and link, so the
    Ethernet pair's fastest row decided the Wi-Fi 'best chunk' too, and an
    INVALID (n<5) row could win."""
    wifi = envelope(
        "A",
        "B",
        "wifi",
        full_run(grpc=300.0, sweep={256 * KIB: 320.0, 4 * MIB: 250.0})
        + [
            case(
                "grpc-down-64MiB-chunk64KiB",
                transport="grpc",
                size=64 * MIB,
                chunk=64 * KIB,
                mbps=[5000.0],  # n=1: INVALID, must not win
                sweep=True,
            )
        ],
    )
    eth = envelope(
        "A", "C", "ethernet", full_run(grpc=900.0, sweep={256 * KIB: 850.0, 4 * MIB: 950.0})
    )
    groups = tables._groups(tables.collect([wifi, eth]))
    best_wifi = tables.best_chunk(groups[("A -> B", "wifi")])
    best_eth = tables.best_chunk(groups[("A -> C", "ethernet")])
    assert best_wifi is not None and best_wifi.chunk == 256 * KIB
    assert best_eth is not None and best_eth.chunk == 4 * MIB
    verdict = tables.verdict(tables.collect([wifi, eth]))
    wifi_block = verdict[verdict.index("**A -> B · Wi-Fi**") : verdict.index("**A -> C")]
    eth_block = verdict[verdict.index("**A -> C · Ethernet**") :]
    assert "Best gRPC chunk (message) size at 64MiB: **256KiB**" in wifi_block
    assert "64KiB" not in wifi_block.split("Best gRPC chunk")[1].split("\n")[0]
    assert "Best gRPC chunk (message) size at 64MiB: **4MiB**" in eth_block
    assert "exactly gRPC's default 4 MB message limit" in eth_block
    assert "default 4 MB" not in wifi_block


def test_best_chunk_needs_two_valid_chunks_and_flags_ties() -> None:
    only_default = envelope("A", "B", "wifi", full_run())
    assert tables.best_chunk(tables.collect([only_default])) is None
    assert tables.chunk_line(tables.collect([only_default])) is None
    tie = envelope("A", "B", "wifi", full_run(grpc=900.0, sweep={256 * KIB: 880.0}))
    line = tables.chunk_line(tables.collect([tie]))
    assert line is not None
    assert "**1MiB**" in line and "256KiB is within 2%" in line
    clear = envelope("A", "B", "wifi", full_run(grpc=900.0, sweep={256 * KIB: 600.0}))
    line = tables.chunk_line(tables.collect([clear]))
    assert line is not None and "within" not in line and "others: 256KiB 600" in line


# ---------------------------------------------------------------- verdict
def test_pick_excludes_sweep_rows_and_prefers_valid() -> None:
    doc = envelope(
        "A",
        "B",
        "wifi",
        full_run(grpc=900.0, size=64 * MIB, sweep={256 * KIB: 2000.0}),
    )
    rows = tables.collect([doc])
    row = tables.pick(rows, "grpc-down", 64 * MIB)
    assert row is not None and row.mbps_mean == pytest.approx(900.0)
    assert tables.pick(rows, "grpc-down", 8 * MIB) is None


def test_decision_size_needs_all_three_transports_valid() -> None:
    doc = envelope(
        "A",
        "B",
        "wifi",
        full_run(size=8 * MIB)
        + [
            case("tcp-down-64MiB", transport="tcp", size=64 * MIB, mbps=five(900)),
            case("grpc-down-64MiB", transport="grpc", size=64 * MIB, mbps=five(800)),
            case("http-get-64MiB", transport="http", size=64 * MIB, mbps=[850, 860]),
        ],
    )
    # 64MiB has only an INVALID HTTP row, so the decision falls back to 8MiB
    assert tables.decision_size(tables.collect([doc])) == 8 * MIB
    partial = envelope(
        "A", "B", "wifi", [case("tcp-down-1MiB", transport="tcp", size=MIB, mbps=five(1))]
    )
    assert tables.decision_size(tables.collect([partial])) == 0


@pytest.mark.parametrize(
    ("grpc", "http", "grpc_needle", "http_needle"),
    [
        (900.0, 930.0, "**96%** of the raw-TCP baseline", "**99%** of the raw-TCP"),
        (950.0, 700.0, "at or above the baseline", "**74%** of raw TCP - usable"),
        (500.0, 400.0, "**53%** of raw TCP - too expensive", "needs a revision"),
    ],
)
def test_verdict_judges_grpc_and_http_against_raw_tcp(
    grpc: float, http: float, grpc_needle: str, http_needle: str
) -> None:
    doc = envelope("A", "B", "ethernet", full_run(tcp=940.0, grpc=grpc, http=http))
    text = tables.verdict(tables.collect([doc]))
    assert "**A -> B · Ethernet** - at 64MiB" in text
    assert "raw TCP 940 Mbit/s (baseline)" in text
    assert grpc_needle in text and http_needle in text
    assert "ADR-0005" in text and "ADR-0003" in text


@pytest.mark.parametrize(
    ("ranges", "needle", "forbidden"),
    [
        (1300.0, "**x1.40 faster** than a single HTTP GET", "slower"),
        (950.0, "**x1.02** of a single HTTP GET", "faster"),
        (750.0, "**x0.81 - slower** than a single HTTP GET", "as fast"),
    ],
)
def test_verdict_parallel_wording_is_faster_no_gain_or_slower(
    ranges: float, needle: str, forbidden: str
) -> None:
    """Regression: a ratio below one used to read "x0.81 as fast as"."""
    doc = envelope("A", "B", "ethernet", full_run(tcp=940.0, http=930.0, ranges=ranges))
    text = tables.verdict(tables.collect([doc]))
    line = next(ln for ln in text.splitlines() if "HTTP 4 parallel ranges" in ln)
    assert needle in line
    assert forbidden not in line
    assert f"{ranges / 940.0 * 100:.0f}% of raw TCP" in line


def test_verdict_never_mixes_pairs_or_links() -> None:
    """Regression: the old verdict took the best row of every family over all
    envelopes, so a Wi-Fi gRPC row was compared with an Ethernet TCP row."""
    wifi = envelope("A", "B", "wifi", full_run(tcp=300.0, grpc=150.0, http=280.0, ranges=290.0))
    eth = envelope("A", "B", "ethernet", full_run(tcp=940.0, grpc=900.0, http=930.0))
    text = tables.verdict(tables.collect([wifi, eth]))
    # groups are sorted by (pair, link): Ethernet before Wi-Fi
    eth_block = text[text.index("**A -> B · Ethernet**") : text.index("**A -> B · Wi-Fi**")]
    wifi_block = text[text.index("**A -> B · Wi-Fi**") :]
    assert "raw TCP 300 Mbit/s (baseline), gRPC 150" in wifi_block
    assert "**50%** of raw TCP - too expensive" in wifi_block
    assert "raw TCP 940 Mbit/s (baseline), gRPC 900" in eth_block
    assert "**96%**" in eth_block and "**16%**" not in text  # 150/940 never appears


def test_verdict_tbd_paths() -> None:
    assert "TBD: no S3 results" in tables.verdict([])
    doc = envelope(
        "A", "B", "wifi", [case("tcp-down-1MiB", transport="tcp", size=MIB, mbps=five(1))]
    )
    text = tables.verdict(tables.collect([doc]))
    assert "**A -> B · Wi-Fi**: TBD" in text and "n >= 5" in text
    no_ranges = envelope(
        "A",
        "B",
        "wifi",
        [c for c in full_run() if not c.case_id.startswith("http-range")],
    )
    text = tables.verdict(tables.collect([no_ranges]))
    assert "parallel" not in text.split("Read every number")[0].split("ADR-0003)")[1]


def test_verdict_marks_invalid_parallel_row() -> None:
    doc = envelope(
        "A",
        "B",
        "wifi",
        [c for c in full_run() if not c.case_id.startswith("http-range")]
        + [
            case(
                "http-range4-64MiB",
                transport="http",
                size=64 * MIB,
                mbps=[1500.0],
                ranged=True,
                parts=4,
            )
        ],
    )
    text = tables.verdict(tables.collect([doc]))
    line = next(ln for ln in text.splitlines() if "HTTP 4 parallel ranges" in ln)
    assert "**INVALID (n=1)**" in line


# -------------------------------------------------------- ceiling + probe
def test_ceiling_table_renders_ok_and_failed() -> None:
    ok = envelope(
        "A",
        "B",
        "wifi",
        [],
        extra={
            "loopback_ceiling": {
                "ok": True,
                "size_bytes": 64 * MIB,
                "crc_rate_mib_s": 5000.0,
                "per_transport": {
                    "tcp-down": {"n": 3, "mean_mbps": 20000.0, "stdev_mbps": 100.0},
                    "grpc-down": {"n": 0, "mean_mbps": None, "error": "boom"},
                },
            }
        },
    )
    failed = envelope(
        "C",
        "B",
        "wifi",
        [],
        extra={"loopback_ceiling": {"ok": False, "error": "did not start", "size_bytes": 0}},
    )
    text = tables.ceiling_table([ok, failed])
    assert "**A** · payload 64MiB · CRC-32 verification 5000 MiB/s" in text
    assert "| tcp-down | 20000 ± 100 | 3 |" in text
    assert "| grpc-down | failed: boom | 0 |" in text
    assert "- not measured: did not start" in text
    assert tables.ceiling_table([envelope("A", "B", "wifi", [])]) == ""


def test_probe_table_new_and_legacy_shapes() -> None:
    new = envelope(
        "A",
        "B",
        "wifi",
        [],
        extra={
            "grpc_default_limit_probe": {
                "chunk_bytes": 4 * MIB,
                "default_limit_bytes": 4 * MIB,
                "ok": True,
                "at_default_limit": {"ok": True, "failure_type": None, "error": None},
                "over_limit": {
                    "ok": False,
                    "limit_bytes": 4 * MIB - 1,
                    "failure_type": "transport_error",
                    "error": "RESOURCE_EXHAUSTED",
                },
            }
        },
    )
    legacy = envelope(
        "C",
        "B",
        "wifi",
        [],
        extra={
            "grpc_default_limit_probe": {
                "chunk_bytes": 4 * MIB,
                "default_limit_bytes": 4 * MIB,
                "ok": False,
                "failure_type": "grpc_unavailable",
                "error": "no grpc",
            }
        },
    )
    text = tables.probe_table([new, legacy])
    assert "| A -> B | 4MiB | 4194304 (default) | passed |" in text
    assert "4194303 (one byte under) | refused - transport_error: RESOURCE_EXHAUSTED |" in text
    assert "| C -> B | 4MiB | 4194304 (default) | refused - grpc_unavailable: no grpc (unexpected) |" in text
    assert tables.probe_table([envelope("A", "B", "wifi", [])]) == ""


def test_render_tables_is_deterministic_and_ordered() -> None:
    doc = envelope("A", "B", "wifi", full_run(sweep={256 * KIB: 800.0}))
    text = tables.render_tables([doc])
    assert text == tables.render_tables([doc])
    order = [
        text.index("### Throughput"),
        text.index("### gRPC chunk"),
        text.index("### CPU cost"),
        text.index("### Q2 answer"),
    ]
    assert order == sorted(order)
    assert "### Loopback ceiling" not in text  # no ceiling recorded


def test_render_tables_sizes_use_binary_labels() -> None:
    doc = envelope("A", "B", "wifi", full_run(size=2 * GIB))
    assert "| 2GiB |" in tables.throughput_tables(tables.collect([doc]))
