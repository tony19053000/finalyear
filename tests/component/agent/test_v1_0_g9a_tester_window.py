"""G9-A (tester) D429 on a real executor + real loopback server.

* a staging that keeps making progress outlives ``staging_retry_s`` (a slow
  fetch longer than the window, one mid-body cut) and still succeeds;
* a staging that never progresses fails ``transfer_error`` after the window;
* ``staging_retry_s = 0`` reports the first error with no retry.
"""

from __future__ import annotations

import asyncio
import importlib.util
import time
from pathlib import Path

import pytest
from mf_blob_range_server import BlobRangeServer
from structlog.testing import capture_logs

from meshforge.common.proto_codec import attempt_state_from_proto

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(90)]

_spec = importlib.util.spec_from_file_location(
    "_g9a_mid_body_cut", Path(__file__).with_name("test_v1_0_g9a_mid_body_cut.py")
)
assert _spec and _spec.loader
_base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_base)
CS, CHUNKS = _base.CS, _base.CHUNKS


class _SlowServer(BlobRangeServer):
    """Every Range response is delayed: the whole fetch outlasts the window."""

    delay = 0.4

    async def _one(self, reader, writer):  # type: ignore[no-untyped-def]
        head_ready = await super()._one(reader, writer)
        await asyncio.sleep(self.delay)
        return head_ready


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import os

    for name in list(os.environ):
        if name.upper().startswith("MESHFORGE_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))


def _exec(tmp_path: Path, retry_s: float, runtime, sent):  # type: ignore[no-untyped-def]
    from meshforge.agent.execution.executor import Executor
    from meshforge.agent.execution.offer import ExecutionSetup
    from meshforge.agent.execution.runtime import RuntimeAvailability
    from meshforge.agent.execution.selection import execution_offer
    from meshforge.agent.settings import AgentSettings
    from meshforge.common.ids import new_command_id

    settings = AgentSettings.load(
        data_dir=tmp_path / "data",
        agent={
            "controller_address": "127.0.0.1:1",
            "execution": {
                "enabled": True,
                "allow_unsandboxed": True,
                "staging_retry_s": retry_s,
            },
            "fetch": {"per_source": 1},
        },
    )

    async def report(_n: str, payload):  # type: ignore[no-untyped-def]
        from meshforge.common.proto.meshforge.v1 import agent_pb2

        copy = agent_pb2.AttemptStatus()
        copy.CopyFrom(payload)
        sent.append(copy)
        return str(new_command_id())

    offer = execution_offer(
        enabled=True,
        allow_unsandboxed=True,
        docker=None,
        subprocess=RuntimeAvailability("subprocess", True),
    )
    return Executor(
        settings,
        ExecutionSetup(offer=offer, runtimes={"subprocess": runtime}),
        node_id="node_x",
        report=report,
        controller_host=lambda: "127.0.0.1",
    )


async def test_progress_keeps_a_staging_alive_past_the_window(tmp_path: Path) -> None:
    import os

    project = _base._build(
        tmp_path, "p", {"main.py": b"x\n", "d.bin": os.urandom(CHUNKS * CS)}
    )
    server = _SlowServer(_base._blobs(project), cut_range_request=8)
    await server.start()
    runtime, sent = _base._Runtime(), []
    executor = _exec(tmp_path, 2.0, runtime, sent)
    started = time.monotonic()
    try:
        with capture_logs() as logs:
            executor.handle(_base._start(server, project))
            await asyncio.wait_for(_base._settle(executor), 60)
    finally:
        await executor.shutdown()
        await server.close()
    took = time.monotonic() - started
    assert attempt_state_from_proto(sent[-1].state).value == "done", sent[-1].failure
    assert took > 2.0, "fetch was meant to outlast the 2 s window"
    retries = [e for e in logs if e["event"] == "agent.execution.stage_retry"]
    assert retries and all(e["progressed"] for e in retries)


@pytest.mark.parametrize("retry_s", [1.0, 0.0])
async def test_no_progress_is_transfer_error(tmp_path: Path, retry_s: float) -> None:
    project = _base._build(tmp_path, "p", {"main.py": b"x\n"})
    server = BlobRangeServer(_base._blobs(project))
    await server.start()
    await server.close()  # the port is now dead: nothing can ever progress
    runtime, sent = _base._Runtime(), []
    executor = _exec(tmp_path, retry_s, runtime, sent)
    started = time.monotonic()
    try:
        with capture_logs() as logs:
            executor.handle(_base._start(server, project))
            await asyncio.wait_for(_base._settle(executor), 60)
    finally:
        await executor.shutdown()
    took = time.monotonic() - started
    final = sent[-1]
    assert attempt_state_from_proto(final.state).value == "failed"
    assert "transfer_error" in str(final.failure).lower().replace("_", "_"), (
        final.failure
    )
    assert runtime.calls == 0
    retries = [e for e in logs if e["event"] == "agent.execution.stage_retry"]
    if retry_s == 0:
        assert retries == [] and took < 5
    else:
        assert retries and all(not e["progressed"] for e in retries)
        assert 0.9 <= took < 15
