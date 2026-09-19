"""Adversarial boundary tests for ``lab/spikes/_s3/tables.py`` (task 0.2.7, S3).

Independent of the implementer's own tests: these attack the *exact* threshold
values the spike's own report (``lab/spikes/S3.md``) and the module docstring
claim, rather than reading the code and restating what it does. Every threshold
is imported from ``tables``/`` _harness`` (never hard-coded here), so a future
change to a constant does not silently desync these tests from the source of
truth - only the *behaviour* at the boundary is asserted.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from spikes._harness import MIN_VALID_REPS
from spikes._harness.envelope import make_envelope
from spikes._harness.reps import CaseResult
from spikes._s3 import tables
from spikes._s3.protocol import MIB, format_size
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


def n_reps(value: float, n: int, spread: float = 1.0) -> list[float]:
    """Exactly ``n`` samples whose **mean is exactly** ``value`` (offsets are
    symmetric around ``(n - 1) / 2``, so the sum of offsets is exactly zero
    regardless of parity - unlike an ``i - n / 2`` spread, which is only
    zero-sum for even ``n`` and silently shifts the mean for odd ``n``)."""
    return [value + spread * (i - (n - 1) / 2) for i in range(n)]


def full_run(
    *,
    tcp: float = 1000.0,
    grpc: float = 900.0,
    http: float = 900.0,
    ranges: float = 900.0,
    size: int = 64 * MIB,
) -> list[CaseResult]:
    label = format_size(size)
    return [
        case(f"tcp-down-{label}", transport="tcp", size=size, mbps=n_reps(tcp, 5)),
        case(f"grpc-down-{label}", transport="grpc", size=size, mbps=n_reps(grpc, 5)),
        case(f"http-get-{label}", transport="http", size=size, mbps=n_reps(http, 5)),
        case(
            f"http-range4-{label}",
            transport="http",
            size=size,
            mbps=n_reps(ranges, 5),
            ranged=True,
            parts=4,
        ),
    ]


# --------------------------------------------------------- GOOD_PCT / POOR_PCT
def test_good_pct_boundary_is_inclusive_at_exactly_85_percent() -> None:
    """Doc: "≥ 85% ... overhead acceptable"; code: ``share >= GOOD_PCT``."""
    tcp = 1000.0
    grpc = tables.GOOD_PCT / 100.0 * tcp  # exactly 85% of tcp
    doc = envelope("A", "B", "ethernet", full_run(tcp=tcp, grpc=grpc, http=grpc))
    text = tables.verdict(tables.collect([doc]))
    assert "**85%** of the raw-TCP baseline" in text
    assert "the overhead is acceptable" in text
    assert "too expensive" not in text and "usable, but" not in text


def test_good_pct_boundary_just_under_85_percent_is_usable_not_acceptable() -> None:
    tcp = 1000.0
    grpc = (tables.GOOD_PCT - 0.06) / 100.0 * tcp  # rounds to 85% display but < 85.0
    doc = envelope("A", "B", "ethernet", full_run(tcp=tcp, grpc=grpc, http=grpc))
    text = tables.verdict(tables.collect([doc]))
    assert "usable, but the cost is real" in text
    assert "the overhead is acceptable" not in text


def test_poor_pct_boundary_is_inclusive_at_exactly_60_percent() -> None:
    """Doc: "60-85% = usable"; code: ``share >= POOR_PCT`` (the elif branch)."""
    tcp = 1000.0
    grpc = tables.POOR_PCT / 100.0 * tcp  # exactly 60%
    doc = envelope("A", "B", "ethernet", full_run(tcp=tcp, grpc=grpc, http=grpc))
    text = tables.verdict(tables.collect([doc]))
    assert "**60%** of raw TCP - usable" in text
    assert "too expensive" not in text


def test_poor_pct_boundary_just_under_60_percent_is_too_expensive() -> None:
    tcp = 1000.0
    grpc = (tables.POOR_PCT - 0.06) / 100.0 * tcp
    doc = envelope("A", "B", "ethernet", full_run(tcp=tcp, grpc=grpc, http=grpc))
    text = tables.verdict(tables.collect([doc]))
    assert "too expensive; ADR-0005 needs a revision" in text


# ---------------------------------------------------- parallel gain/loss band
def test_parallel_gain_boundary_is_inclusive_at_exactly_x1_10() -> None:
    """Doc: "gain above x1.10"; code: ``speedup >= PARALLEL_GAIN_X`` (inclusive)."""
    http = 1000.0
    ranges = tables.PARALLEL_GAIN_X * http  # exactly x1.10
    doc = envelope("A", "B", "ethernet", full_run(http=http, ranges=ranges))
    text = tables.verdict(tables.collect([doc]))
    line = next(ln for ln in text.splitlines() if "HTTP 4 parallel ranges" in ln)
    assert "x1.10 faster" in line
    assert "no measurable gain" not in line


def test_parallel_gain_boundary_just_under_x1_10_is_no_measurable_gain() -> None:
    http = 1000.0
    ranges = tables.PARALLEL_GAIN_X * http - 0.5
    doc = envelope("A", "B", "ethernet", full_run(http=http, ranges=ranges))
    text = tables.verdict(tables.collect([doc]))
    line = next(ln for ln in text.splitlines() if "HTTP 4 parallel ranges" in ln)
    assert "no measurable gain" in line
    assert "faster" not in line


def test_parallel_loss_boundary_at_exactly_x0_90_is_still_no_measurable_gain() -> None:
    """The "no measurable gain" band is the half-open interval [x0.90, x1.10):
    S3.md counts a loss as strictly **below** x0.90, so exactly x0.90 is the
    last ratio that still reads "no measurable gain", while exactly x1.10
    already reads "faster" (pinned by
    ``test_parallel_gain_boundary_is_inclusive_at_exactly_x1_10``).
    """
    http = 1000.0
    ranges = tables.PARALLEL_LOSS_X * http  # exactly x0.90
    doc = envelope("A", "B", "ethernet", full_run(http=http, ranges=ranges))
    text = tables.verdict(tables.collect([doc]))
    line = next(ln for ln in text.splitlines() if "HTTP 4 parallel ranges" in ln)
    assert "no measurable gain" in line
    assert "slower" not in line


def test_parallel_loss_boundary_just_above_x0_90_is_no_measurable_gain() -> None:
    http = 1000.0
    ranges = tables.PARALLEL_LOSS_X * http + 0.5
    doc = envelope("A", "B", "ethernet", full_run(http=http, ranges=ranges))
    text = tables.verdict(tables.collect([doc]))
    line = next(ln for ln in text.splitlines() if "HTTP 4 parallel ranges" in ln)
    assert "no measurable gain" in line
    assert "slower" not in line


# ------------------------------------------------------------ chunk tie band
def test_chunk_tie_boundary_is_inclusive_at_mathematically_exact_5_percent() -> None:
    """A mathematically-exact 5% gap is a tie, as S3.md and the code comment
    promise. ``gap`` is ``100 * (1 - runner/best)`` in floating point, so
    best=1000 / runner=950 evaluates to ``5.000000000000004`` - one ULP above
    5.0. ``CHUNK_TIE_EPS`` absorbs exactly that representation error, so the
    documented inclusive boundary really holds and the report does not call two
    tied chunks a clear winner.
    """
    size = 64 * MIB
    label = format_size(size)
    best_mbps = 1000.0
    runner_mbps = 950.0  # mathematically exactly a 5.0% gap from 1000.0
    rows = [
        case(
            f"grpc-down-{label}-chunk1MiB",
            transport="grpc",
            size=size,
            chunk=MIB,
            mbps=n_reps(best_mbps, 5),
            sweep=True,
        ),
        case(
            f"grpc-down-{label}-chunk4MiB",
            transport="grpc",
            size=size,
            chunk=4 * MIB,
            mbps=n_reps(runner_mbps, 5),
            sweep=True,
        ),
    ]
    doc = envelope("A", "B", "ethernet", rows)
    text = tables.chunk_line(tables.collect([doc])) or ""
    assert "not distinguishable" in text
    assert "**1MiB**" in text


def test_chunk_tie_boundary_just_over_5_percent_is_a_clear_winner() -> None:
    size = 64 * MIB
    label = format_size(size)
    best_mbps = 1000.0
    runner_mbps = best_mbps * (1 - tables.CHUNK_TIE_PCT / 100.0) - 1.0  # > 5% gap
    rows = [
        case(
            f"grpc-down-{label}-chunk1MiB",
            transport="grpc",
            size=size,
            chunk=MIB,
            mbps=n_reps(best_mbps, 5),
            sweep=True,
        ),
        case(
            f"grpc-down-{label}-chunk4MiB",
            transport="grpc",
            size=size,
            chunk=4 * MIB,
            mbps=n_reps(runner_mbps, 5),
            sweep=True,
        ),
    ]
    doc = envelope("A", "B", "ethernet", rows)
    text = tables.chunk_line(tables.collect([doc])) or ""
    assert "not distinguishable" not in text


# --------------------------------------------- MIN_VALID_REPS n=5 boundary
def test_row_validity_boundary_n_equals_min_valid_reps_is_valid() -> None:
    size = 64 * MIB
    label = format_size(size)
    rows = tables.collect(
        [
            envelope(
                "A",
                "B",
                "ethernet",
                [
                    case(
                        f"tcp-down-{label}",
                        transport="tcp",
                        size=size,
                        mbps=n_reps(900.0, MIN_VALID_REPS),
                    )
                ],
            )
        ]
    )
    (row,) = rows
    assert row.n == MIN_VALID_REPS and row.valid is True


def test_row_validity_boundary_n_one_below_min_valid_reps_is_invalid() -> None:
    size = 64 * MIB
    label = format_size(size)
    rows = tables.collect(
        [
            envelope(
                "A",
                "B",
                "ethernet",
                [
                    case(
                        f"tcp-down-{label}",
                        transport="tcp",
                        size=size,
                        mbps=n_reps(900.0, MIN_VALID_REPS - 1),
                    )
                ],
            )
        ]
    )
    (row,) = rows
    assert row.n == MIN_VALID_REPS - 1 and row.valid is False


# ---------------------------------- decision_size must never see http-range
def test_decision_size_ignores_http_range_even_when_it_dominates() -> None:
    """S3.md claims ``decision_size`` picks the largest size where tcp/grpc/http
    (single-stream) are all valid, and the parallel line is purely informational.
    Attack that claim: make the range family gigantic, valid, and present only
    at a size where the single-stream trio is NOT all valid - the huge, valid
    range row must never smuggle that size into the decision.
    """
    small = 8 * MIB
    huge_only_size = 512 * MIB
    small_label, huge_label = format_size(small), format_size(huge_only_size)
    rows = [
        *full_run(size=small),
        # only http-range4 (no tcp/grpc/http-get) is valid at the huge size,
        # and it reports an outrageous throughput that would tempt a naive
        # "biggest observed valid size" rule.
        case(
            f"http-range4-{huge_label}",
            transport="http",
            size=huge_only_size,
            mbps=n_reps(50_000.0, 5),
            ranged=True,
            parts=4,
        ),
    ]
    doc = envelope("A", "B", "ethernet", rows)
    collected = tables.collect([doc])
    assert tables.decision_size(collected) == small
    text = tables.verdict(collected)
    assert f"at {small_label}" in text
    assert huge_label not in text.split("Read every number")[0]


def test_http_range_at_1mib_never_appears_in_a_decision_size() -> None:
    """S3.md §5: ``http-range4`` at 1 MiB is "noisy by construction" and must
    never drive a decision. Even a spectacular (fabricated) 1 MiB range result
    must not move ``decision_size`` off the size where tcp/grpc/http are valid.
    """
    size = 64 * MIB
    rows = [
        *full_run(size=size),
        case(
            "http-range4-1MiB",
            transport="http",
            size=1 * MIB,
            mbps=n_reps(999_999.0, 5),
            ranged=True,
            parts=4,
        ),
    ]
    doc = envelope("A", "B", "ethernet", rows)
    collected = tables.collect([doc])
    assert tables.decision_size(collected) == size


# ------------------------------------------- ADR-0005 line names its chunk
def _sweep(size: int, *, per_chunk: dict[int, float]) -> list[CaseResult]:
    label = format_size(size)
    return [
        case(
            f"grpc-down-{label}-chunk{format_size(chunk)}",
            transport="grpc",
            size=size,
            chunk=chunk,
            mbps=n_reps(mbps, 5),
            sweep=True,
        )
        for chunk, mbps in per_chunk.items()
    ]


def test_adr_0005_line_always_states_the_chunk_it_was_measured_at() -> None:
    """The ADR-0005 judgement reads the *default-chunk* row. A reader who is
    about to revise an ADR must be told which message size produced the number,
    otherwise a chunk-size penalty is indistinguishable from a gRPC penalty.
    """
    doc = envelope("A", "B", "ethernet", full_run(tcp=1000.0, grpc=900.0))
    line = next(
        ln
        for ln in tables.verdict(tables.collect([doc])).splitlines()
        if "ADR-0005" in ln
    )
    assert f"Measured at chunk {format_size(MIB)}." in line


def test_adr_0005_penalty_is_qualified_when_the_sweep_found_a_faster_chunk() -> None:
    """A gRPC row that reads "too expensive" at the default chunk, while the
    sweep already showed a materially faster chunk, must not send the team off
    to revise ADR-0005: the verdict has to say the message size is implicated
    and that the run should be repeated at that chunk first.
    """
    size = 64 * MIB
    rows = full_run(tcp=1000.0, grpc=500.0, size=size)  # 50% -> "too expensive"
    rows += _sweep(size, per_chunk={MIB: 500.0, 256 * 1024: 700.0})
    doc = envelope("A", "B", "ethernet", rows)
    line = next(
        ln
        for ln in tables.verdict(tables.collect([doc])).splitlines()
        if "ADR-0005" in ln
    )
    assert "too expensive" in line
    assert "256KiB" in line and "40.0% faster" in line
    assert "not gRPC" in line and "re-measure at that chunk" in line


def test_adr_0005_penalty_is_not_qualified_when_the_default_chunk_is_best() -> None:
    """The counterpart: when the sweep confirms the default chunk is the best
    one, the caveat must stay out of the way - a warning that fires always is
    a warning nobody reads.
    """
    size = 64 * MIB
    rows = full_run(tcp=1000.0, grpc=500.0, size=size)
    rows += _sweep(size, per_chunk={MIB: 500.0, 256 * 1024: 480.0})
    doc = envelope("A", "B", "ethernet", rows)
    line = next(
        ln
        for ln in tables.verdict(tables.collect([doc])).splitlines()
        if "ADR-0005" in ln
    )
    assert "too expensive" in line
    assert f"Measured at chunk {format_size(MIB)}." in line
    assert "re-measure at that chunk" not in line


def test_adr_0005_caveat_ignores_a_sweep_gain_inside_the_tie_band() -> None:
    """A runner-up inside CHUNK_TIE_PCT is "not distinguishable at this n", so
    it must not be quoted as a reason to doubt the ADR judgement either. Pinned
    *at* the threshold, not comfortably inside it: the caveat and chunk_line's
    tie sentence share one normalisation precisely so that a chunk called "not
    distinguishable" is never also called "faster" in the same block.
    """
    size = 64 * MIB
    rows = full_run(tcp=1000.0, grpc=500.0, size=size)
    # exactly at the threshold: a gap of CHUNK_TIE_PCT *below the best*, the
    # same normalisation chunk_line uses, so the two sentences in one verdict
    # block can never disagree about this pair of chunks
    best = 500.0 / (1 - tables.CHUNK_TIE_PCT / 100.0)
    rows += _sweep(size, per_chunk={MIB: 500.0, 256 * 1024: best})
    doc = envelope("A", "B", "ethernet", rows)
    line = next(
        ln
        for ln in tables.verdict(tables.collect([doc])).splitlines()
        if "ADR-0005" in ln
    )
    assert "re-measure at that chunk" not in line


# ----------------------------------------------- partial loopback ceiling
def _ceiling(per_transport: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {
        "ok": True,
        "size_bytes": 64 * MIB,
        "crc_rate_mib_s": 6000.0,
        "per_transport": per_transport,
    }


def test_ceiling_table_marks_a_run_that_failed_part_way_as_incomplete() -> None:
    """A ceiling that dies on rep 3 of 4 keeps the samples it already took, so
    ``mean_mbps`` is a real number computed from one or two reps while ``error``
    is set. Rendering that mean bare would read as a confident *low* ceiling -
    and the verdict tells the reader that a LAN result near the ceiling is
    tool-limited. The failure must survive into the table.
    """
    doc = envelope(
        "A",
        "B",
        "ethernet",
        full_run(),
        extra={
            "loopback_ceiling": _ceiling(
                {
                    "tcp-down": {
                        "n": 1,
                        "mean_mbps": 900.0,
                        "stdev_mbps": 0.0,
                        "max_mbps": 900.0,
                        "error": "transport_error: peer went away",
                    }
                }
            )
        },
    )
    table = tables.ceiling_table([doc])
    row = next(ln for ln in table.splitlines() if ln.startswith("| tcp-down"))
    assert "900" in row
    assert "incomplete" in row and "peer went away" in row


def test_ceiling_table_renders_a_clean_run_without_an_incomplete_marker() -> None:
    doc = envelope(
        "A",
        "B",
        "ethernet",
        full_run(),
        extra={
            "loopback_ceiling": _ceiling(
                {
                    "tcp-down": {
                        "n": 3,
                        "mean_mbps": 900.0,
                        "stdev_mbps": 10.0,
                        "max_mbps": 910.0,
                        "error": None,
                    }
                }
            )
        },
    )
    row = next(
        ln for ln in tables.ceiling_table([doc]).splitlines() if ln.startswith("| tcp")
    )
    assert "900" in row and "incomplete" not in row
