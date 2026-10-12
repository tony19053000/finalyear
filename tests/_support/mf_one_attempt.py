"""Run one attempt through the real :class:`Executor` with a given runtime
(not a test module).

The bundle is a real packed project "fetched" by copying it; reports are
collected, never acked (so the workdir stays until the executor closes). Used
by component tests that need the agent's own rules around a real runtime
(e.g. the output cap around a real container).
"""

from __future__ import annotations

import asyncio
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from meshforge.agent.execution.executor import Executor
from meshforge.agent.execution.offer import ExecutionSetup
from meshforge.agent.execution.runtime import RuntimeAvailability
from meshforge.agent.execution.selection import execution_offer
from meshforge.agent.settings import AgentSettings
from meshforge.common.bundle import pack_to_file
from meshforge.common.errors import FailureType
from meshforge.common.ids import new_attempt_id, new_command_id, new_job_id
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.common.proto_codec import (
    attempt_state_from_proto,
    failure_type_from_proto,
)


@dataclass(frozen=True)
class Final:
    state: str
    failure_type: FailureType | None
    message: str


def _final(status: agent_pb2.AttemptStatus) -> Final:
    failure = (
        failure_type_from_proto(status.failure.failure_type)
        if status.HasField("failure")
        else None
    )
    return Final(
        attempt_state_from_proto(status.state).value, failure, status.failure.message
    )


def run_one_attempt(
    tmp_path: Path,
    runtime: Any,
    *,
    entrypoint: list[str],
    execution: dict[str, Any] | None = None,
    timeout_s: float = 120.0,
) -> tuple[Final, int]:
    """``(final status, bytes left in the workdir)`` of one attempt run by
    ``runtime`` (registered under its ``name``)."""
    project = tmp_path / "proj"
    project.mkdir()
    (project / "main.py").write_text("print(1)\n")
    bundle = tmp_path / "src.bundle"
    digest = str(pack_to_file(project, bundle).digest)

    async def fake_fetch(_base: str, _digest: Any, _token: str, dest: Path) -> int:
        shutil.copyfile(bundle, dest)
        return dest.stat().st_size  # noqa: ASYNC240 - a test fake

    sandboxed = runtime.name == "docker"
    settings = AgentSettings.load(
        data_dir=tmp_path / "data",
        agent={
            "controller_address": "127.0.0.1:1",
            "policy": {"accept_jobs": True, "allow_unsandboxed": not sandboxed},
            "execution": {"grace_s": 1.0, **(execution or {})},
        },
    )
    available = RuntimeAvailability(runtime.name, True, sandboxed=sandboxed)
    setup = ExecutionSetup(
        offer=execution_offer(
            enabled=True,
            allow_unsandboxed=not sandboxed,
            docker=available if sandboxed else None,
            subprocess=None if sandboxed else available,
        ),
        runtimes={runtime.name: runtime},
    )
    sent: list[agent_pb2.AttemptStatus] = []
    ended = asyncio.Event()

    async def report(_name: str, payload: agent_pb2.AttemptStatus) -> str:
        copy = agent_pb2.AttemptStatus()
        copy.CopyFrom(payload)
        sent.append(copy)
        if attempt_state_from_proto(copy.state).value in ("done", "failed"):
            ended.set()
        return str(new_command_id())

    attempt_id = str(new_attempt_id())
    msg = agent_pb2.ControllerMessage(
        message_id=str(new_command_id()),
        start_attempt=agent_pb2.StartAttempt(
            job_id=str(new_job_id()),
            attempt_id=attempt_id,
            fencing_token=1,
            spec=agent_pb2.ExecutionSpec(
                bundle_digest=digest,
                blob_port=47110,
                fetch_token="tok",
                manifest_json=json.dumps({"name": "x", "entrypoint": entrypoint}),
                timeout_s=int(timeout_s),
            ),
        ),
    )

    async def scenario() -> tuple[Final, int]:
        executor = Executor(
            settings,
            setup,
            node_id="node_x",
            report=report,
            controller_host=lambda: "127.0.0.1",
            fetch=fake_fetch,
        )
        try:
            executor.handle(msg)
            await asyncio.wait_for(ended.wait(), timeout_s)
            workdir = executor.work_root / attempt_id
            used = sum(p.stat().st_size for p in workdir.rglob("*") if p.is_file())
            return _final(sent[-1]), used
        finally:
            await executor.close()

    return asyncio.run(scenario())
