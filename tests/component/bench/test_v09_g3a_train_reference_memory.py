"""V0.9 G3 lane A item 0.3 (D228.1): peak memory of the ``ml.cnn_train`` float64
first-loss reference at the corners of the accepted parameter space stays under
512 MB, measured in a real process.

Linux only: the peak is the process's ``VmHWM`` after resetting it
(``/proc/self/clear_refs`` = 5) just before the reference runs, minus ``VmRSS``
at that moment - the allocator's real high-water mark, not an estimate.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("torch")
from meshforge.bench.workloads import get_family
from meshforge.bench.workloads.kernels.ml import (
    TRAIN_REFERENCE_ELEMENTS,
)

pytestmark = [
    pytest.mark.component,
    pytest.mark.timeout(300),
    pytest.mark.skipif(
        not Path("/proc/self/clear_refs").exists(), reason="needs Linux /proc"
    ),
]

LIMIT_BYTES = 512 * 10**6

_MEASURE = r"""
import gc, json, sys, time
from meshforge.bench.workloads.kernels import ml

def kib(key):
    with open("/proc/self/status") as status:
        for line in status:
            if line.startswith(key):
                return int(line.split()[1])
    raise SystemExit("no " + key)

state = ml._train_prepare(json.loads(sys.argv[1]), 3, "cpu")
del state["optimizer"]
gc.collect()
with open("/proc/self/clear_refs", "w") as refs:
    refs.write("5")
base = kib("VmRSS:")
start = time.perf_counter()
loss = ml._first_loss_reference(state)
print(json.dumps({"peak": (kib("VmHWM:") - base) * 1024,
                  "seconds": time.perf_counter() - start, "loss": loss}))
"""

# Every corner has batch * width * image^2 == the cap: the widest, the largest
# image, the largest batch, and the deepest of each.
CORNERS = [
    {"batch": 64, "width": 256, "depth": 2, "image": 32, "steps": 100},
    {"batch": 64, "width": 256, "depth": 12, "image": 32, "steps": 100},
    {"batch": 16, "width": 256, "depth": 12, "image": 64, "steps": 100},
    {"batch": 512, "width": 32, "depth": 12, "image": 32, "steps": 100},
    {"batch": 64, "width": 64, "depth": 12, "image": 64, "steps": 100},
]


@pytest.mark.parametrize(
    "params", CORNERS, ids=lambda p: "b{batch}-w{width}-d{depth}-i{image}".format(**p)
)
def test_reference_peak_at_the_cap_corners_is_under_512_mb(
    params: dict[str, Any],
) -> None:
    assert params["batch"] * params["width"] * params["image"] ** 2 == (
        TRAIN_REFERENCE_ELEMENTS
    )
    get_family("ml.cnn_train").parse_params(params)  # an accepted run
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
    assert result["loss"] > 0
