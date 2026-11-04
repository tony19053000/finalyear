"""V1.0 G9-A item 1 (D429) on real sockets: a body cut off mid-stream.

A real loopback HTTP server (:mod:`mf_blob_range_server`) serves a tree's
manifest and chunks with ``Range`` and, once, closes the connection half-way
through a chunk's body - the client sees the same ``RemoteProtocolError`` the
lab saw on Wi-Fi. A real executor stages the tree (real httpx, real CAS,
real resumable parts, real clock).

Checked: the attempt's staging succeeds **within the one attempt** (one
``agent.execution.stage_retry``, ``progressed = true``); the second try asks
only for chunks not verified before the cut (every chunk is served whole
exactly once, the cut one again) and the staging reports the resumed chunks.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from mf_blob_range_server import BlobRangeServer
from structlog.testing import capture_logs

from meshforge.agent.execution.executor import Executor
from meshforge.agent.execution.offer import ExecutionSetup
from meshforge.agent.execution.runtime import (
    RunObserver,
    RunResult,
    RuntimeAvailability,
)
from meshforge.agent.execution.selection import execution_offer
from meshforge.agent.settings import AgentSettings
from meshforge.common.cas import MIN_CHUNK_SIZE
from meshforge.common.ids import new_attempt_id, new_command_id, new_job_id
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.common.proto_codec import attempt_state_from_proto
from meshforge.common.tree import BuiltTree, build_tree

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(60)]

CS = MIN_CHUNK_SIZE
CHUNKS = 12


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in list(os.environ):
        if name.upper().startswith("MESHFORGE_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))


class _Runtime:
    name = "subprocess"

    def __init__(self) -> None:
        self.calls = 0

    async def probe(self) -> RuntimeAvailability:
        return RuntimeAvailability("subprocess", True)

    async def run(
        self, spec: Any, on_output: Any, *, observer: RunObserver | None = None
    ) -> RunResult:
        assert observer is not None
        self.calls += 1
        observer.enter_run()
        observer.leave_run()
        return RunResult(exit_code=0)


def _build(tmp_path: Path, name: str, files: dict[str, bytes]) -> BuiltTree:
    root = tmp_path / name
    root.mkdir()
    for rel, data in files.items():
        (root / rel).write_bytes(data)
    return build_tree(root, chunk_size=CS)


def _blobs(*trees: BuiltTree) -> dict[str, bytes]:
    blobs: dict[str, bytes] = {}
    for built in trees:
        blobs.update({h: p.read_bytes() for h, p in built.sources.items()})
        blobs[built.tree.tree_id.hex] = built.manifest
    return blobs


@pytest_asyncio.fixture
async def project(tmp_path: Path) -> BuiltTree:
    return _build(
        tmp_path,
        "proj",
        {"main.py": b"print(1)\n", "data.bin": os.urandom(CHUNKS * CS)},
    )


def _executor(
    tmp_path: Path, runtime: _Runtime, sent: list[agent_pb2.AttemptStatus]
) -> Executor:
    settings = AgentSettings.load(
        data_dir=tmp_path / "data",
        agent={
            "controller_address": "127.0.0.1:1",
            "execution": {
                "enabled": True,
                "allow_unsandboxed": True,
                "staging_retry_s": 60,
            },
            "fetch": {"per_source": 2},
        },
    )

    async def report(_name: str, payload: agent_pb2.AttemptStatus) -> str:
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
        ExecutionSetup(offer=offer, runtimes={"subprocess": runtime}),  # type: ignore[dict-item]
        node_id="node_x",
        report=report,
        controller_host=lambda: "127.0.0.1",
    )


def _start(
    server: BlobRangeServer, project: BuiltTree, inputs: list[BuiltTree] = ()
) -> agent_pb2.ControllerMessage:
    spec = agent_pb2.ExecutionSpec(
        tree_id=str(project.tree.tree_id),
        blob_url=server.url,
        blob_port=server.port,
        fetch_token="tok",
        manifest_json='{"name":"x","entrypoint":["python","main.py"]}',
        timeout_s=60,
    )
    for index, built in enumerate(inputs):
        spec.inputs.append(
            agent_pb2.InputTree(
                tree_id=str(built.tree.tree_id), mount=f"in{index}", ref=f"ref{index}"
            )
        )
    return agent_pb2.ControllerMessage(
        message_id=str(new_command_id()),
        start_attempt=agent_pb2.StartAttempt(
            job_id=str(new_job_id()),
            attempt_id=str(new_attempt_id()),
            fencing_token=1,
            spec=spec,
        ),
    )


async def _settle(executor: Executor) -> None:
    while True:
        tasks = [a.task for a in executor._attempts.values() if a.task]
        if tasks and all(t.done() for t in tasks):
            return
        await asyncio.sleep(0.01)


def _whole_ranges(server: BlobRangeServer) -> list[tuple[int, int]]:
    return [r.range for r in server.requests if r.range is not None and not r.cut]


async def test_a_body_cut_mid_stream_is_retried_in_the_attempt_and_resumes(
    tmp_path: Path, project: BuiltTree
) -> None:
    server = BlobRangeServer(_blobs(project), cut_range_request=5)
    await server.start()
    runtime, sent = _Runtime(), []
    executor = _executor(tmp_path, runtime, sent)
    try:
        with capture_logs() as logs:
            executor.handle(_start(server, project))
            await asyncio.wait_for(_settle(executor), 30)
    finally:
        await executor.shutdown()
        await server.close()
    final = sent[-1]
    assert attempt_state_from_proto(final.state).value == "done", final.failure
    assert runtime.calls == 1
    retries = [e for e in logs if e["event"] == "agent.execution.stage_retry"]
    assert len(retries) == 1, retries
    assert retries[0]["cause"] == "transfer_error"
    assert retries[0]["progressed"] is True
    cut = [r for r in server.requests if r.cut]
    assert len(cut) == 1
    whole = _whole_ranges(server)
    chunks = {(i * CS, (i + 1) * CS - 1) for i in range(CHUNKS)}
    # every chunk was served whole exactly once - none verified before the
    # cut was asked for again; the cut one was asked again by the retry
    assert sorted(whole) == sorted(chunks)
    cut_at = server.requests.index(cut[0])
    after = {r.range for r in server.requests[cut_at + 1 :] if r.range}
    before = {r.range for r in server.requests[:cut_at] if r.range}
    assert cut[0].range in after
    assert not (after & before)
    staged = [e for e in logs if e["event"] == "agent.cas.staged"]
    assert staged[-1]["chunks_resumed"] >= 4
    assert final.transfers[0].chunks_resumed == staged[-1]["chunks_resumed"]
