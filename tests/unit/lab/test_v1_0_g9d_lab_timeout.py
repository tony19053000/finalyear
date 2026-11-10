"""V1.0 G9-D (D437): ``mode3_check build`` gives a lab job a time limit its
slowest laptop can meet. A split job's ``limits.timeout`` bounds each chunk
and a "don't split" run is one chunk with every item, so the benchmark's
15 min (``JOB_TIMEOUT``) killed the lab's ``mc`` on a slower laptop."""

from __future__ import annotations

from pathlib import Path

import pytest

from meshforge.bench.workloads import get_family, materialise
from meshforge.common.manifest import MAX_TIMEOUT_S
from mode3_check import LAB_TIMEOUT_FACTOR, lab_timeout_s, main

pytestmark = pytest.mark.unit


def test_lab_timeout_is_ten_times_the_hint_with_a_floor_and_a_cap() -> None:
    assert lab_timeout_s(0.5) == 900
    assert lab_timeout_s(530.6) == 5340  # 5306 s up to whole minutes
    assert lab_timeout_s(1e9) == MAX_TIMEOUT_S


@pytest.mark.extras("agent")
def test_built_mc_folder_carries_the_lab_time_limit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    before = materialise.JOB_TIMEOUT
    dest = tmp_path / "mc"
    args = ["build", "split.montecarlo_range", str(dest)]
    args += ["--param", "items=5000", "--param", "samples=5000000", "--seed", "10"]
    assert main(args) == 0
    assert before == materialise.JOB_TIMEOUT  # restored for everything else
    from meshforge.common.manifest import load_manifest

    manifest = load_manifest(dest)
    family = get_family("split.montecarlo_range")
    hint = family.expected_runtime_s({"items": 5000, "samples": 5000000})
    assert manifest.limits.timeout == lab_timeout_s(hint)
    assert manifest.limits.timeout >= LAB_TIMEOUT_FACTOR * hint > 900
    out = capsys.readouterr().out
    assert f"time limit (limits.timeout, per chunk): {lab_timeout_s(hint)} s" in out
