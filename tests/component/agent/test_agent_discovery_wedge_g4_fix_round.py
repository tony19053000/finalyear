"""V0.3 G4 fix round (H1, priority 3): does a wedged discovery really still
enroll the node with ``basic_hardware()``, end to end through a real session?

``discover_hardware`` itself is unit/component-tested for its own bounded
return value; this drives the full ``serve()`` path against a real controller
and asserts the node that shows up in the registry has the *fallback* shape
(no GPUs field populated beyond the empty default, capabilities from
``basic_hardware()``), and that it shows up within the documented budget
(``agent.hardware.timeout_s`` + ``DISCOVERY_GRACE_S``), not after some much
longer default.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from pathlib import Path

import pytest
from mf_agent_harness import make_agent_settings
from mf_session_client import poll_until

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from meshforge.agent.hardware.summary import HardwareProvider
from meshforge.agent.identity import NodeIdentity
from meshforge.agent.runtime import DISCOVERY_GRACE_S, serve
from meshforge.controller.registry import Registry
from meshforge.controller.server import ControllerRuntime

pytestmark = [pytest.mark.component, pytest.mark.docker]

HARDWARE_TIMEOUT_S = 0.2
BUDGET_S = HARDWARE_TIMEOUT_S + DISCOVERY_GRACE_S


@pytest.mark.asyncio
async def test_a_permanently_wedged_probe_still_enrols_with_basic_hardware(
    monkeypatch: pytest.MonkeyPatch,
    runtime: ControllerRuntime,
    registry: Registry,
    grpc_target: str,
    identity: NodeIdentity,
    data_dir: Path,
) -> None:
    release = threading.Event()

    def wedge(self: HardwareProvider) -> None:
        release.wait(30.0)
        raise AssertionError("test cleanup failed to release the wedge in time")

    monkeypatch.setattr(HardwareProvider, "get", wedge)

    settings = make_agent_settings(
        data_dir, grpc_target, hardware={"timeout_s": HARDWARE_TIMEOUT_S}
    )
    node_id = str(identity.node_id)
    started = time.monotonic()
    task = asyncio.create_task(serve(settings, identity=identity))
    try:

        async def enrolled() -> bool:
            return await registry.get_node(node_id) is not None

        await poll_until(enrolled, timeout_s=BUDGET_S + 10.0)
        elapsed = time.monotonic() - started
        assert elapsed < BUDGET_S + 8.0, (
            "enrolment should follow shortly after the discovery budget expires, "
            f"not require some much larger hidden timeout (took {elapsed:.2f}s "
            f"against a {BUDGET_S:.2f}s discovery budget)"
        )

        node = await registry.get_node(node_id)
        assert node is not None
        hardware = node.hardware
        # basic_hardware()'s documented shape: no GPUs, CPU/RAM from the stdlib.
        assert hardware.get("gpus", []) == []
        assert hardware["capabilities"][0] == "v0.3"
        assert int(hardware["cpu_threads"]) >= 0
    finally:
        release.set()  # let the abandoned probe thread exit
        task.cancel()
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(task, 10.0)
