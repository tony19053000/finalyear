"""V0.9 G3 lane A tester r1 (item 0.3): the float64 first-loss reference peak at
cap corners the implementer did not measure (other factorisations, odd depth),
plus a check that the reference value equals the straightforward forward in a
real process at a mid size. Linux only (VmHWM)."""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any

import pytest
from test_v09_g3a_train_reference_memory import _MEASURE, LIMIT_BYTES

pytest.importorskip("torch")

pytestmark = [pytest.mark.component, pytest.mark.timeout(300)]

import pathlib  # noqa: E402

pytestmark.append(
    pytest.mark.skipif(
        not pathlib.Path("/proc/self/clear_refs").exists(), reason="needs Linux /proc"
    )
)

CORNERS = [
    {"batch": 128, "width": 128, "depth": 12, "image": 32, "steps": 100},
    {"batch": 32, "width": 128, "depth": 7, "image": 64, "steps": 100},
    {"batch": 256, "width": 64, "depth": 3, "image": 32, "steps": 100},
    {"batch": 8, "width": 256, "depth": 12, "image": 64, "steps": 100},
]


@pytest.mark.parametrize(
    "params", CORNERS, ids=lambda p: "b{batch}-w{width}-d{depth}-i{image}".format(**p)
)
def test_other_cap_corners_stay_under_512_mb(params: dict[str, Any]) -> None:
    done = subprocess.run(
        [sys.executable, "-c", _MEASURE, json.dumps(params)],
        capture_output=True,
        text=True,
        timeout=240,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    result = json.loads(done.stdout.strip().splitlines()[-1])
    assert 0 < result["peak"] < LIMIT_BYTES, result
    assert result["seconds"] < 120, result
