"""Unit tests for S1 matrix aggregation (docs/LAB.md §2 tables): row = sender,
receiver view, mean ± stdev, INVALID for n < 5, TBD for missing pairs, Wi-Fi and
Ethernet never mixed, pooling of runs, honesty notes."""

# ruff: noqa: RUF001 - the en dash is docs/LAB.md's pair separator

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from spikes._harness.envelope import make_envelope
from spikes._harness.reps import CaseResult
from spikes._s1.matrix import cell, order_hosts, render_matrix

NOW = datetime(2026, 9, 20, 9, 0, tzinfo=UTC)


def s1_env(
    client: str,
    server: str,
    link: str,
    *,
    up: list[float],
    down: list[float],
    rtt: list[float] | None = None,
    streams: int = 1,
    sender_factor: float = 1.0,
    ceiling: float | None = None,
    spike: str = "S1",
) -> dict[str, Any]:
    def tcp(direction: str, values: list[float]) -> CaseResult:
        sender, receiver = (
            (client, server) if direction == "upload" else (server, client)
        )
        return CaseResult(
            case_id=f"tcp-{direction}-x{streams}",
            label=f"TCP x{streams} {sender} → {receiver}",
            metrics={"receiver_mbps": "Mbit/s", "sender_mbps": "Mbit/s"},
            attrs={
                "kind": "tcp",
                "direction": direction,
                "streams": streams,
                "sender": sender,
                "receiver": receiver,
            },
            samples=[
                {"receiver_mbps": v, "sender_mbps": v * sender_factor} for v in values
            ],
        )

    rtts = rtt if rtt is not None else [1.0] * 5
    udp = CaseResult(
        case_id="udp-rtt",
        label="UDP",
        metrics={"rtt_mean_ms": "ms", "jitter_ms": "ms", "loss_pct": "%"},
        attrs={"kind": "udp", "client": client, "server": server},
        samples=[{"rtt_mean_ms": r, "jitter_ms": 0.1, "loss_pct": 0.0} for r in rtts],
    )
    return make_envelope(
        spike=spike,
        topic="S1-network",
        link=link,
        params={"client_name": client, "server_name": server},
        cases=[udp, tcp("upload", up), tcp("download", down)],
        extra={"loopback_ceiling": {"ok": True, "receiver_mbps": ceiling}}
        if ceiling
        else {},
        git={"commit": "abc", "dirty": False},
        host={"hostname": f"os-{client}"},
        host_label=client,
        now=NOW,
    )


def section(md: str, title: str) -> str:
    """The markdown between ``#### <title>`` and the next ``#### ``."""
    start = md.index(f"#### {title}\n")
    end = md.find("\n#### ", start + 1)
    return md[start : end if end >= 0 else len(md)]


def row(md: str, first_cell: str, table_index: int = 0) -> list[str]:
    """Cells of the ``table_index``-th table row starting with ``| first_cell |``."""
    rows = [ln for ln in md.splitlines() if ln.startswith(f"| {first_cell} |")]
    return [c.strip() for c in rows[table_index].strip("|").split("|")]


def test_cell_formats() -> None:
    assert cell(None) == "TBD"
    assert cell([]) == "TBD"
    assert cell([1, 2, 3, 4]) == "INVALID (n=4)"
    assert cell([100.0] * 5) == "100.0 ± 0.0 (n=5)"
    assert cell([50, 60, 70, 80, 90]) == "70.0 ± 15.8 (n=5)"


def test_row_is_sender_and_both_directions_filled() -> None:
    env = s1_env("A", "B", "ethernet", up=[100.0] * 5, down=[50, 60, 70, 80, 90])
    eth = section(render_matrix([env]), "Ethernet")
    assert row(eth, "A") == ["A", "—", "100.0 ± 0.0 (n=5)"]  # A sends to B (upload)
    assert row(eth, "B") == ["B", "70.0 ± 15.8 (n=5)", "—"]  # B sends to A (download)
    assert row(eth, "A–B") == ["A–B", "1.00 ± 0.00 (n=5)", "0.100", "0.00"]


def test_invalid_and_tbd_cells_with_labels() -> None:
    env = s1_env("A", "B", "wifi", up=[1, 2, 3], down=[5.0] * 5, rtt=[1.0, 1.0])
    md = render_matrix([env], {"A": "L1", "B": "L2", "C": "L3", "D": "L4"})
    wifi = section(md, "Wi-Fi")
    assert row(wifi, "L1") == ["L1", "—", "INVALID (n=3)", "TBD", "TBD"]
    assert row(wifi, "L2") == ["L2", "5.0 ± 0.0 (n=5)", "—", "TBD", "TBD"]
    assert row(wifi, "L3") == ["L3", "TBD", "TBD", "—", "TBD"]
    assert row(wifi, "L1–L2")[1:] == ["INVALID (n=2)", "INVALID (n=2)", "INVALID (n=2)"]
    assert row(wifi, "L3–L4")[1:] == ["TBD", "TBD", "TBD"]
    assert "Host labels: L1 = A, L2 = B, L3 = C, L4 = D." in md
    # every one of the 6 pairs of 4 laptops is listed
    assert sum(1 for ln in wifi.splitlines() if ln.startswith("| L") and "–" in ln) == 6


def test_wifi_and_ethernet_are_never_mixed() -> None:
    wifi_env = s1_env("A", "B", "wifi", up=[100.0] * 5, down=[100.0] * 5, rtt=[3.0] * 5)
    eth_env = s1_env("A", "B", "ethernet", up=[900.0] * 5, down=[900.0] * 5)
    md = render_matrix([wifi_env, eth_env])
    wifi, eth = section(md, "Wi-Fi"), section(md, "Ethernet")
    assert "100.0 ± 0.0" in wifi and "900.0" not in wifi
    assert "900.0 ± 0.0" in eth and "100.0" not in eth
    assert row(wifi, "A–B")[1] == "3.00 ± 0.00 (n=5)"
    assert row(eth, "A–B")[1] == "1.00 ± 0.00 (n=5)"


def test_runs_in_both_directions_are_pooled() -> None:
    # A measured B, and B measured A: A->B comes from A's upload and B's download.
    a_run = s1_env("A", "B", "ethernet", up=[100.0] * 5, down=[200.0] * 5)
    b_run = s1_env("B", "A", "ethernet", up=[200.0] * 5, down=[100.0] * 5)
    eth = section(render_matrix([a_run, b_run]), "Ethernet")
    assert row(eth, "A")[2] == "100.0 ± 0.0 (n=10)"
    assert row(eth, "B")[1] == "200.0 ± 0.0 (n=10)"
    assert row(eth, "A–B")[1] == "1.00 ± 0.00 (n=10)"


def test_one_table_per_stream_count() -> None:
    one = s1_env("A", "B", "ethernet", up=[100.0] * 5, down=[100.0] * 5)
    four = s1_env("A", "B", "ethernet", up=[400.0] * 5, down=[400.0] * 5, streams=4)
    eth = section(render_matrix([one, four]), "Ethernet")
    assert "TCP, 1 stream, row = sender" in eth
    assert "TCP, 4 parallel streams, row = sender" in eth
    assert row(eth, "A", 0)[2] == "100.0 ± 0.0 (n=5)"
    assert row(eth, "A", 1)[2] == "400.0 ± 0.0 (n=5)"


def test_sender_receiver_difference_and_ceiling_are_reported() -> None:
    env = s1_env(
        "A",
        "B",
        "ethernet",
        up=[100.0] * 5,
        down=[100.0] * 5,
        sender_factor=1.2,
        ceiling=150.0,
    )
    eth = section(render_matrix([env]), "Ethernet")
    assert "| A→B x1 | 100.0 | 120.0 | +20.0% |" in eth
    assert "loopback ceiling (150.0 Mbit/s)" in eth


def test_views_agree_note_and_no_ceiling() -> None:
    env = s1_env("A", "B", "ethernet", up=[100.0] * 5, down=[100.0] * 5)
    md = render_matrix([env])
    assert "agree within 5%" in section(md, "Ethernet")
    assert "Not measured" in md
    assert "iperf3 not used" in md


def test_other_spikes_are_ignored() -> None:
    env = s1_env("A", "B", "ethernet", up=[100.0] * 5, down=[100.0] * 5, spike="S3")
    md = render_matrix([env])
    assert "100.0" not in md


def test_order_hosts_labels_first_naturally() -> None:
    labels = {"h10": "L10", "h2": "L2", "h1": "L1"}
    assert order_hosts(["zeta", "h2", "h10", "alpha"], labels) == [
        "h1",
        "h2",
        "h10",
        "alpha",
        "zeta",
    ]
