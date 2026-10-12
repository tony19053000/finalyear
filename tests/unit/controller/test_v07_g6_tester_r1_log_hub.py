"""V0.7 G6 tester r1: the controller's LogHub lets go of a job's secret values.

* an attempt that ends (finalize) removes its values from the process-wide
  ``ACTIVE`` set;
* a hub that is closed/stopped while attempts are live must not keep their values
  (and a scrub rule for them) in the process for the rest of its life.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from meshforge.common.secret_redaction import ACTIVE
from meshforge.controller.blobs import BlobStore
from meshforge.controller.jobs.logs import LogHub

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def _hub(tmp_path: Path) -> LogHub:
    blobs = BlobStore(tmp_path / "blobs", max_blob_bytes=10**6)
    blobs.prepare()
    hub = LogHub(tmp_path / "logs", blobs=blobs, max_bytes=10**6)
    hub.prepare()
    return hub


def _holders() -> list[str]:
    return sorted(h for h in ACTIVE._by_holder if h.startswith("logs:att_r1"))


async def test_finalize_removes_the_attempts_values_from_active(tmp_path: Path) -> None:
    hub = _hub(tmp_path)
    hub.open(
        "att_r1_a",
        job_id="job_r1",
        node_id="n",
        token=1,
        secret_values={"K": b"value-r1-aaaa"},
    )
    task = asyncio.create_task(hub.run())
    try:
        assert _holders() == ["logs:att_r1_a"]
        hub.finalize("att_r1_a", None)
        for _ in range(100):
            if not _holders():
                break
            await asyncio.sleep(0.01)
        assert _holders() == []
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.xfail(
    strict=True,
    reason="T-10 (minor): stopping the LogHub writer task (`run()` ends) does not remove the "
    "live attempts' values from ACTIVE: they stay scrubbed-for (and in memory) "
    "for the rest of the process - visible when a controller runtime is "
    "restarted in-process (tests) and a hygiene gap otherwise",
)
async def test_stopping_the_hub_releases_values_of_attempts_still_live(
    tmp_path: Path,
) -> None:
    hub = _hub(tmp_path)
    hub.open(
        "att_r1_b",
        job_id="job_r1",
        node_id="n",
        token=1,
        secret_values={"K": b"value-r1-bbbb"},
    )
    task = asyncio.create_task(hub.run())
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    try:
        assert _holders() == []
    finally:
        ACTIVE.remove("logs:att_r1_b")
