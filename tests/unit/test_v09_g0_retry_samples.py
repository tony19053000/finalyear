"""V0.9 G0 item 3 (D210 item 5, T-8b): job.state latency numbers exclude a
retry's re-entered states and count them separately."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from mf_ui_perf import load_ui_perf

pytestmark = pytest.mark.unit

PERF = load_ui_perf("ui_perf_v09_g0_retry")
T0 = 1_760_000_000_000.0  # ms


def _tr(*steps: tuple[str, float]) -> list[dict[str, Any]]:
    return [
        {
            "to_state": s,
            "at": dt.datetime.fromtimestamp((T0 + at) / 1000, dt.UTC).isoformat(),
        }
        for s, at in steps
    ]


def test_a_retry_is_counted_not_timed() -> None:
    s = PERF.JobStateSamples()
    s.add(
        "j",
        _tr(
            ("queued", 0),
            ("running", 100),
            ("queued", 1_000),  # retry
            ("running", 2_000),  # retry
            ("succeeded", 3_000),
        ),
        {"queued": T0 + 10, "running": T0 + 130, "succeeded": T0 + 3_050},
    )
    assert s.lags == pytest.approx([10.0, 30.0, 50.0])
    assert s.terminal_lags == pytest.approx([50.0])
    assert s.revisited == 2 and s.retried_jobs == 1
    assert s.missed == [] and s.superseded == 0


def test_a_job_without_a_retry_is_unchanged() -> None:
    s = PERF.JobStateSamples()
    s.add(
        "j",
        _tr(("queued", 0), ("running", 100), ("succeeded", 200)),
        {"queued": T0 + 5, "running": T0 + 105, "succeeded": T0 + 205},
    )
    assert s.lags == pytest.approx([5.0, 5.0, 5.0])
    assert s.revisited == 0 and s.retried_jobs == 0


def test_a_re_entry_never_supersedes_an_unshown_state() -> None:
    """``assigned`` was never shown. The page's one ``running`` time (600) is
    the display of the FIRST ``running`` (shown late) - the re-entered
    ``running`` must not stand in for ``assigned``; the next first-visit state
    shown (``failed``) does: 1010 - 500, not 600 - 500."""
    s = PERF.JobStateSamples()
    s.add(
        "j",
        _tr(("running", 0), ("assigned", 500), ("running", 900), ("failed", 1_000)),
        {"running": T0 + 600, "failed": T0 + 1_010},
    )
    assert s.missed == []
    assert s.superseded == 1 and s.revisited == 1
    assert s.lags == pytest.approx([600.0, 510.0, 10.0])


def test_a_state_seen_only_by_its_re_entry_is_missed_if_nothing_later() -> None:
    s = PERF.JobStateSamples()
    s.add(
        "j",
        _tr(("queued", 0), ("assigned", 100), ("queued", 200)),
        {"queued": T0 + 250},  # shown once, after the re-entry happened
    )
    # queued (first visit) is timed to its one display; assigned has no later
    # first-visit display: missed, never superseded by the re-entry.
    assert s.missed == ["j:assigned"]
    assert s.lags == pytest.approx([250.0])
    assert s.revisited == 1


def test_retry_counts_add_up_over_jobs() -> None:
    s = PERF.JobStateSamples()
    for job in ("a", "b"):
        s.add(job, _tr(("queued", 0), ("queued", 10)), {"queued": T0 + 1})
    s.add("c", _tr(("queued", 0)), {"queued": T0 + 1})
    assert s.revisited == 2 and s.retried_jobs == 2
    assert s.lags == pytest.approx([1.0, 1.0, 1.0])
