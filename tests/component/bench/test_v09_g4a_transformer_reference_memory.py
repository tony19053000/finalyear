"""V0.9 G4 lane A item 0.1 (D233.1): peak memory of the ``ml.transformer_infer``
float64 reference at the largest accepted shape stays under 512 MB, measured in
a real process (it was ~4.9 GB: a whole float64 model and 4 rows of attention
scores at once).

Linux only: the peak is the process's ``VmHWM`` after resetting it
(``/proc/self/clear_refs`` = 5) just before the reference is built, minus
``VmRSS`` at that moment - the timed model (float32, here on the CPU; on CUDA
in a real job) is already resident, as in a real check. The reference handles
one row at a time, so one row at each corner measures its peak (a real check
recomputes >= 4 rows one after the other: the same peak, 4x the time).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("torch")
from meshforge.bench.workloads.kernels.runner import PARAM_LIMITS

pytestmark = [
    pytest.mark.component,
    pytest.mark.timeout(400),
    pytest.mark.skipif(
        not Path("/proc/self/clear_refs").exists(), reason="needs Linux /proc"
    ),
]

LIMIT_BYTES = 512 * 10**6

_MEASURE = r"""
import gc, json, sys, time
import torch
from meshforge.bench.workloads.kernels import ml

def kib(key):
    with open("/proc/self/status") as status:
        for line in status:
            if line.startswith(key):
                return int(line.split()[1])
    raise SystemExit("no " + key)

params = json.loads(sys.argv[1])
state = ml._transformer_prepare(params, 3, "cpu")
tokens = state["xs"][0][:1].cpu()
gc.collect()
with open("/proc/self/clear_refs", "w") as refs:
    refs.write("5")
base = kib("VmRSS:")
start = time.perf_counter()
reference = ml._transformer_reference(state, state["model"])
assert callable(reference), reference
out = reference(tokens)
print(json.dumps({"peak": (kib("VmHWM:") - base) * 1024,
                  "seconds": time.perf_counter() - start,
                  "finite": bool(torch.isfinite(out).all()),
                  "shape": list(out.shape)}))
"""

_MAX = PARAM_LIMITS["ml.transformer_infer"]
CORNERS = [
    # the largest of everything (one row: seq_len 2048, d_model 1024, 24 layers)
    {
        "batch": 1,
        "seq_len": _MAX["seq_len"],
        "width": _MAX["width"],
        "depth": _MAX["depth"],
        "batches": 1,
    },
    # widest with the shortest sequence (weights dominate)
    {"batch": 1, "seq_len": 16, "width": _MAX["width"], "depth": 1, "batches": 1},
]


@pytest.mark.parametrize(
    "params", CORNERS, ids=lambda p: "n{seq_len}-w{width}-d{depth}".format(**p)
)
def test_reference_peak_at_the_largest_shapes_is_under_512_mb(
    params: dict[str, Any],
) -> None:
    done = subprocess.run(
        [sys.executable, "-c", _MEASURE, json.dumps(params)],
        capture_output=True,
        text=True,
        timeout=360,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    result = json.loads(done.stdout.strip().splitlines()[-1])
    assert 0 < result["peak"] < LIMIT_BYTES, result
    assert result["finite"] is True
    assert result["shape"] == [1, params["seq_len"], params["width"]]
