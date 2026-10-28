"""V1.0 G4 (D412): the profiling kit's pure helpers (``tests/_support/mf_profile.py``)
- nearest-rank summary, the idle RSS slope that tells a filling bounded store
from a leak, and the py-spy raw-file summary - on hand-built inputs."""

from __future__ import annotations

from pathlib import Path

import pytest
from mf_profile import nearest_rank, pyspy_top, rss_slope, summary

pytestmark = pytest.mark.unit

MIB = 2**20


def test_nearest_rank_and_summary_use_integer_ranks() -> None:
    vals = [float(v) for v in range(1, 101)]
    assert nearest_rank(vals, 50) == 50.0
    assert nearest_rank(vals, 95) == 95.0
    assert nearest_rank([], 50) is None
    got = summary([3.0, 1.0, float("nan"), 2.0])
    assert got["n"] == 3
    assert got["p50"] == 2.0
    assert got["max"] == 3.0
    assert summary([]) == {"n": 0}


def test_rss_slope_flat_after_a_plateau_and_positive_for_a_leak() -> None:
    # warm-up 0-600 s climbs 20 MiB, then flat: the window after 600 s is flat
    plateau = [
        (float(t), int((200 + min(t, 600) / 30) * MIB)) for t in range(0, 3601, 2)
    ]
    flat = rss_slope(plateau, 600.0)
    assert flat["slope_mib_per_h"] == 0.0
    assert flat["first_mib"] == flat["last_mib"] == 220.0
    whole = rss_slope(plateau, 0.0)
    assert whole["slope_mib_per_h"] > 0
    # a leak of 6 MiB/h keeps its slope in every window
    leak = [(float(t), int((200 + t / 600) * MIB)) for t in range(0, 3601, 2)]
    assert rss_slope(leak, 1800.0)["slope_mib_per_h"] == pytest.approx(6.0, abs=0.01)


def test_rss_slope_needs_three_points_in_the_window() -> None:
    assert rss_slope([(0.0, MIB), (1.0, MIB)], 0.0) == {"n": 2}
    assert rss_slope([(t, MIB) for t in (0.0, 1.0, 2.0)], 5.0) == {"n": 0}
    # all at one instant: no variance, slope 0 (no division by zero)
    assert rss_slope([(1.0, MIB)] * 3, 0.0)["slope_mib_per_h"] == 0.0


def test_pyspy_top_drops_idle_samples_and_the_spine(tmp_path: Path) -> None:
    spine = "main (meshforge/controller/app.py:1);run (asyncio/runners.py:118)"
    raw = tmp_path / "pyspy.txt"
    raw.write_text(
        "\n".join(
            [
                f"{spine};_dispatch (meshforge/controller/jobs/dispatch.py:261);"
                "execute (asyncpg/connection.py:354) 6",
                f"{spine};decide (meshforge/controller/jobs/planning.py:486) 4",
                f"{spine};select (selectors.py:468) 90",
                "main (meshforge/agent/app.py:1);fetch (meshforge/agent/x.py:2) 50",
                "garbage line without a count",
            ]
        ),
        "utf-8",
    )
    top = pyspy_top(raw, "meshforge/controller")
    assert top["samples"] == 10  # the 90 idle samples and the agent stack are out
    assert top["self_pct"][0] == ("execute (asyncpg/connection.py:354)", 60.0)
    incl = dict(top["inclusive_pct"])
    assert "main (meshforge/controller/app.py:1)" not in incl  # 100 %: spine
    assert incl["_dispatch (meshforge/controller/jobs/dispatch.py:261)"] == 60.0
    assert [k for k, _ in top["meshforge_pct"]] == [
        "_dispatch (meshforge/controller/jobs/dispatch.py:261)",
        "decide (meshforge/controller/jobs/planning.py:486)",
    ]
    busy_and_idle = pyspy_top(raw, "meshforge/controller", skip_idle=False)
    assert busy_and_idle["samples"] == 100
