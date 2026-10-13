# ruff: noqa: T201  # tester test file: seeded RNG, report prints, re-imported fixtures
"""V0.7 G6 tester r2 (T-8): in a REAL separate process the event loop keeps
ticking while the matcher for the largest allowed job is built the way the
controller (dispatch._matcher) and the agent (Executor: to_thread) build it."""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

pytestmark = pytest.mark.unit

SCRIPT = textwrap.dedent(
    """
    import asyncio, os, time, resource, json
    from meshforge.common.secret_redaction import SecretMatcher
    from meshforge.controller.jobs import dispatch

    async def ticker(stop, lags):
        last = time.perf_counter()
        while not stop.is_set():
            await asyncio.sleep(0.002)
            now = time.perf_counter()
            lags.append(now - last - 0.002)
            last = now

    async def main():
        secrets = {f"S{i:02d}": os.urandom(8192) for i in range(32)}   # 256 KiB
        shapes = {"random": secrets,
                  "periodic": {"P": b"ab" * (128 * 1024)},
                  "repeated": {f"R{i}": bytes([65 + i]) * 8192 for i in range(32)}}
        out = {}
        for label, sec in shapes.items():
            for how in ("agent", "controller"):
                stop, lags = asyncio.Event(), []
                t = asyncio.create_task(ticker(stop, lags))
                await asyncio.sleep(0.05)
                t0 = time.perf_counter()
                if how == "agent":
                    m = await asyncio.to_thread(SecretMatcher, sec)
                else:
                    m = await dispatch._matcher(sec)
                build = time.perf_counter() - t0
                await asyncio.sleep(0.05)
                stop.set(); await t
                out[f"{label}/{how}"] = [round(build, 3), round(max(lags) * 1000, 1)]
        hwm = [l for l in open("/proc/self/status") if l.startswith("VmHWM")][0]
        out["maxrss_mb"] = int(hwm.split()[1]) / 1024  # not ru_maxrss: it is inherited across exec
        print("RESULT" + json.dumps(out))

    asyncio.run(main())
    """
)


def test_the_loop_keeps_ticking_while_the_largest_matcher_builds() -> None:
    pytest.importorskip("sqlalchemy", reason="needs the controller extra")
    done = subprocess.run(
        [sys.executable, "-c", SCRIPT],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    import json

    result = json.loads(
        next(x for x in done.stdout.splitlines() if x.startswith("RESULT"))[6:]
    )
    print(result)
    assert result.pop("maxrss_mb") < 400
    for key, (build_s, worst_lag_ms) in result.items():
        assert build_s < 1.0, (key, build_s)
        assert worst_lag_ms < 100, (key, worst_lag_ms)
