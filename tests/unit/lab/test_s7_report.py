"""Unit tests for the S7 cross-laptop report (0.2.11).

One table per metric, one column per laptop: INVALID below the repetition rule,
TBD for a laptop that has no result yet, `err:` when every repetition failed,
and the backends spelled out so a torch number is never read as a fallback one.
"""

from __future__ import annotations

from typing import Any

from spikes._harness.stats import summarize
from spikes._s7 import report


def _case(
    metrics: dict[str, str], values: dict[str, list[float]], **extra: Any
) -> dict[str, Any]:
    return {
        "label": extra.pop("label", "case"),
        "metrics": metrics,
        "samples": [{}] * len(next(iter(values.values()), [])),
        "errors": extra.pop("errors", []),
        "summary": {k: summarize(v).to_json() for k, v in values.items()},
        **extra,
    }


def _envelope(hostname: str, cases: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {
        "spike": "S7",
        "host": {"hostname": hostname, "label": hostname, "os": "Linux"},
        "link": "ethernet",
        "cases": cases,
        "errors": [],
        "extra": {
            "quick": False,
            "capabilities": {"torch": "2.14.0", "numpy": "2.3.1"},
            "gpu": {"available": True, "devices": [{"device_name": "RTX 3050"}]},
            "environment": {"before": {"cpu": {"distinct": ["performance"]}}},
            "on_battery": False,
            "stability": {},
            **extra,
        },
    }


FIVE = [100.0, 101.0, 99.0, 100.0, 100.0]


def test_is_score_keeps_rates_and_hides_durations() -> None:
    assert report.is_score("gflops")
    assert report.is_score("tflops")
    assert report.is_score("copy_gb_s")  # a rate, even though it ends in _s
    assert report.is_score("device_copy_gb_s")
    assert not report.is_score("elapsed_s")
    assert not report.is_score("event_s")
    assert not report.is_score("copy_s")


def test_one_table_per_metric_with_one_column_per_laptop() -> None:
    a = _envelope(
        "hostA", {"cpu-float32-all": _case({"gflops": "GFLOP/s"}, {"gflops": FIVE})}
    )
    b = _envelope(
        "hostB",
        {"cpu-float32-all": _case({"gflops": "GFLOP/s"}, {"gflops": [50.0] * 5})},
    )
    text = report.render_scoreboard([a, b], {"hostA": "L1", "hostB": "L2"})
    assert "#### gflops (GFLOP/s)" in text
    assert "| Case | L1 | L2 |" in text
    assert "| `cpu-float32-all` | 100.00 ± 0.71 | 50.00 ± 0.00 |" in text
    expected = "Ranking: L1 (100.00, `cpu-float32-all`) > L2 (50.00, `cpu-float32-all`)"
    assert expected in text


def test_ranking_names_the_all_cores_case_it_is_based_on() -> None:
    """The gflops ranking is by the best case (``-all``), never the 1t row."""
    a = _envelope(
        "hostA",
        {
            "cpu-float32-1t": _case({"gflops": "GFLOP/s"}, {"gflops": [30.0] * 5}),
            "cpu-float32-all": _case({"gflops": "GFLOP/s"}, {"gflops": [200.0] * 5}),
        },
    )
    b = _envelope(
        "hostB",
        {
            "cpu-float32-1t": _case({"gflops": "GFLOP/s"}, {"gflops": [40.0] * 5}),
            "cpu-float32-all": _case({"gflops": "GFLOP/s"}, {"gflops": [120.0] * 5}),
        },
    )
    text = report.render_scoreboard([a, b], {"hostA": "L1", "hostB": "L2"})
    expected = (
        "Ranking: L1 (200.00, `cpu-float32-all`) > L2 (120.00, `cpu-float32-all`)"
    )
    assert expected in text


def test_a_laptop_without_a_result_file_is_tbd() -> None:
    mem = _case({"triad_gb_s": "GB/s"}, {"triad_gb_s": FIVE})
    a = _envelope("hostA", {"memory-bandwidth": mem})
    text = report.render_scoreboard([a], {"hostA": "L1", "hostD": "L4"})
    assert "| Case | L1 | L4 |" in text
    assert "| `memory-bandwidth` | 100.00 ± 0.71 | TBD |" in text
    assert "no valid result: L4" in text


def test_too_few_repetitions_are_marked_invalid() -> None:
    a = _envelope(
        "hostA", {"gpu-float32": _case({"tflops": "TFLOP/s"}, {"tflops": [8.0, 8.2]})}
    )
    text = report.render_scoreboard([a])
    assert "**INVALID (n=2)**" in text
    assert "Ranking: no valid results" in text


def test_a_case_whose_repetitions_all_failed_shows_its_failure_type() -> None:
    case = _case(
        {"tflops": "TFLOP/s"},
        {"tflops": []},
        errors=[{"failure_type": "cuda_broken", "message": "boom", "rep": 0}],
    )
    cpu = _case({"gflops": "GFLOP/s"}, {"gflops": FIVE})
    a = _envelope("hostA", {"gpu-float32": case, "cpu-float32-1t": cpu})
    text = report.render_scoreboard([a], {"hostA": "L1"})
    assert "| `gpu-float32` | err: cuda_broken |" in text


def test_mixed_backends_are_visible_and_never_averaged() -> None:
    torch_host = _envelope(
        "hostA", {"cpu-float32-all": _case({"gflops": "GFLOP/s"}, {"gflops": FIVE})}
    )
    fallback = _envelope(
        "hostB",
        {
            "cpu-python-1t": _case(
                {"python_mflops": "MFLOP/s"}, {"python_mflops": [30.0] * 5}
            )
        },
        capabilities={"torch": None, "numpy": None},
        gpu={"available": False, "failure_type": "torch_not_installed"},
    )
    text = report.render_scoreboard(
        [torch_host, fallback], {"hostA": "L1", "hostB": "L2"}
    )
    gflops_block = text.split("#### gflops")[1].split("####")[0]
    assert "| `cpu-float32-all` | 100.00 ± 0.71 | TBD |" in gflops_block
    assert "#### python_mflops (MFLOP/s)" in text
    assert "must never be compared with `gflops`" in text
    assert "torch — / numpy —" in text
    assert "none (torch_not_installed)" in text


def test_conditions_table_flags_battery_quick_and_throttling() -> None:
    doc = _envelope(
        "hostA",
        {"cpu-float32-1t": _case({"gflops": "GFLOP/s"}, {"gflops": FIVE})},
        quick=True,
        on_battery=True,
        stability={"cpu-float32-1t": {"thermal_throttling_suspected": True}},
    )
    text = report.render_scoreboard([doc])
    assert "quick (smoke)" in text
    assert "BATTERY — not comparable" in text
    assert "| performance | cpu-float32-1t |" in text


def test_report_without_any_results_says_tbd() -> None:
    text = report.render_scoreboard([], {"hostA": "L1"})
    assert "No S7 result files found" in text
    assert "(no results)" in text


def test_host_names_prefer_labels_then_the_envelope_label() -> None:
    a = _envelope("hostA", {})
    b = _envelope("hostB", {})
    assert report.host_names([a, b], {"hostA": "L1"}) == ["L1", "hostB"]
    assert report.display_name(a, {"hostA": "L1"}) == "L1"
    assert report.display_name(b, None) == "hostB"


def test_cell_formats() -> None:
    assert report.cell(None) == "TBD"
    assert report.cell(None, "cuda_broken") == "err: cuda_broken"
    assert report.cell(summarize([1.0, 1.0, 1.0, 1.0, 1.0])) == "1.00 ± 0.00"
    assert report.cell(summarize([1.0])).startswith("**INVALID (n=1)**")


def test_headline_metrics_come_first() -> None:
    doc = _envelope(
        "hostA",
        {
            "memory-bandwidth": _case(
                {"triad_gb_s": "GB/s", "copy_gb_s": "GB/s"},
                {"triad_gb_s": FIVE, "copy_gb_s": FIVE},
            ),
            "gpu-float32": _case({"tflops": "TFLOP/s"}, {"tflops": FIVE}),
        },
    )
    text = report.render_scoreboard([doc])
    order = [line for line in text.splitlines() if line.startswith("#### ")]
    assert order[0].startswith("#### tflops")
    assert order[1].startswith("#### triad_gb_s")
    assert order[2].startswith("#### copy_gb_s")


def test_unknown_summary_shapes_are_skipped_not_crashed() -> None:
    doc = _envelope("hostA", {"broken": {"metrics": {}, "summary": {"gflops": "??"}}})
    text = report.render_scoreboard([doc])
    assert "Hardware scores per laptop" in text


def test_labels_match_hostnames_case_insensitively() -> None:
    """Windows reports ``LAPTOP-1`` while the file name says ``laptop-1``."""
    a = _envelope(
        "LAPTOP-1", {"cpu-float32-all": _case({"gflops": "x"}, {"gflops": FIVE})}
    )
    assert report.display_name(a, {"laptop-1": "L1"}) == "L1"
    assert report.display_name(a, {"Laptop-1": "L1"}) == "L1"
    assert report.display_name(a, {"other": "L2"}) == "LAPTOP-1"
    text = report.render_scoreboard([a], {"laptop-1": "L1"})
    assert "| Case | L1 |" in text
    assert "LAPTOP-1" not in text.split("### Run conditions")[0]
