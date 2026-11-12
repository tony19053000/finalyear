"""V1.0 G10-C (D447): a lane's item-list download is a staging fetch like any
other - under the D95/D429 staging retry, never a final ``unauthorized``.

Lab L3 (2026-11-10, job_01M72YDCB1R6V8E3ST71SMVDDW): the split lane was
downloading its item list when the session dropped; the controller had ended
the lane and revoked its fetch token, the download got 401 and the attempt
ended ``FAILED(unauthorized)`` 0.5 s later. The item list was fetched outside
the staging retry loop (``Executor._lane_prepare`` straight after STAGING), so
a refused token or a cut link ended the lane at once - with a non-retryable
type, which after a controller *restart* (D95: the old token is refused until
the controller re-mints it after Reconcile) would fail a lane that the
project staging would have carried through.

Now: a 401 waits for ``RefreshFetchToken`` and retries with the new token; a
cut link is retried with backoff; a refusal nobody refreshes ends
``transfer_error`` (retryable) after ``staging_retry_s`` - never
``unauthorized``.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import httpx
import pytest

from meshforge.agent.execution.executor import Executor
from meshforge.agent.execution.offer import ExecutionSetup
from meshforge.agent.execution.runtime import (
    RunObserver,
    RunResult,
    RuntimeAvailability,
)
from meshforge.agent.execution.selection import execution_offer
from meshforge.agent.settings import AgentSettings
from meshforge.common.hashing import sha256_bytes
from meshforge.common.ids import new_attempt_id, new_command_id, new_job_id
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.common.proto_codec import (
    attempt_state_from_proto,
    failure_type_from_proto,
)
from meshforge.common.tree import BuiltTree, build_tree

pytestmark = pytest.mark.unit

ITEMS = json.dumps(
    {"v": 1, "over": "range", "items": [0, 1, 2, 3], "count": 4},
    separators=(",", ":"),
).encode()
ITEMS_HEX = sha256_bytes(ITEMS).hex


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in list(os.environ):
        if name.upper().startswith("MESHFORGE_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))


class _Runtime:
    name = "subprocess"

    async def probe(self) -> RuntimeAvailability:
        return RuntimeAvailability("subprocess", True)

    async def run(
        self, spec: Any, on_output: Any, *, observer: RunObserver | None = None
    ) -> RunResult:
        del spec, on_output, observer
        return RunResult(exit_code=0)


class _Reports:
    def __init__(self) -> None:
        self.sent: list[agent_pb2.AttemptStatus] = []

    async def __call__(self, _name: str, payload: agent_pb2.AttemptStatus) -> str:
        copy = agent_pb2.AttemptStatus()
        copy.CopyFrom(payload)
        self.sent.append(copy)
        return str(new_command_id())


class _Surface:
    """The controller's blob API: the project tree and the lane's item list.

    ``items_script``: per item-list request (1-based) ``"refuse"`` (401) or
    ``"cut"`` (the connection drops); unlisted requests are served."""

    def __init__(self, built: BuiltTree, items_script: dict[int, str]) -> None:
        self.blobs = {h: p.read_bytes() for h, p in built.sources.items()}
        self.blobs[built.tree.tree_id.hex] = built.manifest
        self.blobs[ITEMS_HEX] = ITEMS
        self.tokens = {"tok"}
        self.items_script = items_script
        self.items_requests: list[str] = []
        """The bearer token of every item-list request, in order."""

    async def handler(self, request: httpx.Request) -> httpx.Response:
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        hex_ = request.url.path.rsplit("/", 1)[-1]
        if hex_ == ITEMS_HEX:
            self.items_requests.append(token)
            action = self.items_script.get(len(self.items_requests))
            if action == "refuse":
                return httpx.Response(401)
            if action == "cut":
                raise httpx.ConnectError("connection reset by peer")
        if token not in self.tokens:
            return httpx.Response(401)
        data = self.blobs.get(hex_)
        if data is None:
            return httpx.Response(404)
        rng = request.headers.get("range")
        if rng is None:
            return httpx.Response(200, stream=httpx.ByteStream(data))
        start, end = (int(x) for x in rng.removeprefix("bytes=").split("-"))
        return httpx.Response(206, stream=httpx.ByteStream(data[start : end + 1]))


def _executor(
    tmp_path: Path, surface: _Surface, *, staging_retry_s: float
) -> tuple[Executor, _Reports]:
    settings = AgentSettings.load(
        data_dir=tmp_path / "data",
        agent={
            "controller_address": "127.0.0.1:1",
            "execution": {
                "enabled": True,
                "allow_unsandboxed": True,
                "grace_s": 0.5,
                "staging_retry_s": staging_retry_s,
            },
        },
    )
    reports = _Reports()
    offer = execution_offer(
        enabled=True,
        allow_unsandboxed=True,
        docker=None,
        subprocess=RuntimeAvailability("subprocess", True),
    )
    executor = Executor(
        settings,
        ExecutionSetup(offer=offer, runtimes={"subprocess": _Runtime()}),  # type: ignore[dict-item]
        node_id="node_x",
        report=reports,
        controller_host=lambda: "127.0.0.1",
    )

    def transport() -> dict[str, Any]:
        return {"transport": httpx.MockTransport(surface.handler)}

    executor._transport = transport  # type: ignore[method-assign]
    return executor, reports


def _tree(tmp_path: Path) -> BuiltTree:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "main.py").write_text("print(1)\n")
    return build_tree(root)


def _start(built: BuiltTree) -> agent_pb2.ControllerMessage:
    return agent_pb2.ControllerMessage(
        message_id=str(new_command_id()),
        start_attempt=agent_pb2.StartAttempt(
            job_id=str(new_job_id()),
            attempt_id=str(new_attempt_id()),
            fencing_token=1,
            spec=agent_pb2.ExecutionSpec(
                tree_id=str(built.tree.tree_id),
                blob_url="http://ctl:47110",
                blob_port=47110,
                fetch_token="tok",
                manifest_json='{"name":"x","entrypoint":["python","main.py"]}',
                timeout_s=60,
            ),
            lane=agent_pb2.LaneSpec(
                items_digest=f"sha256:{ITEMS_HEX}",
                item_count=4,
                max_chunk_output_bytes=1 << 20,
                lease_timeout_s=60,
            ),
        ),
    )


def _states(reports: _Reports) -> list[str]:
    return [attempt_state_from_proto(s.state).value for s in reports.sent]


def _final_failure(reports: _Reports) -> str:
    return failure_type_from_proto(reports.sent[-1].failure.failure_type).value


async def _until(cond: Any, timeout_s: float = 5.0) -> None:
    async def wait() -> None:
        while not cond():  # noqa: ASYNC110 - polling a plain predicate
            await asyncio.sleep(0.01)

    await asyncio.wait_for(wait(), timeout_s)


async def _settle(executor: Executor, timeout_s: float = 10.0) -> None:
    await _until(
        lambda: (
            all(a.task is None or a.task.done() for a in executor._attempts.values())
            and any(a.task for a in executor._attempts.values())
        ),
        timeout_s,
    )


async def _cancel(executor: Executor, msg: agent_pb2.ControllerMessage) -> None:
    start = msg.start_attempt
    executor.handle(
        agent_pb2.ControllerMessage(
            message_id=str(new_command_id()),
            cancel_attempt=agent_pb2.CancelAttempt(
                attempt_id=start.attempt_id,
                fencing_token=start.fencing_token,
                reason="test",
            ),
        )
    )
    await _settle(executor)


@pytest.mark.asyncio
async def test_a_refused_item_list_waits_for_the_refreshed_token(
    tmp_path: Path,
) -> None:
    built = _tree(tmp_path)
    surface = _Surface(built, {1: "refuse"})
    executor, reports = _executor(tmp_path, surface, staging_retry_s=60)
    msg = _start(built)
    executor.handle(msg)
    await _until(lambda: len(surface.items_requests) == 1)
    await asyncio.sleep(0.3)
    # Not ended, not retried with the refused token: waiting for the refresh.
    assert "failed" not in _states(reports), reports.sent[-1].failure.message
    assert surface.items_requests == ["tok"]
    surface.tokens = {"tok2"}  # the controller re-minted after the reconnect
    start = msg.start_attempt
    executor.handle(
        agent_pb2.ControllerMessage(
            message_id=str(new_command_id()),
            refresh_fetch_token=agent_pb2.RefreshFetchToken(
                attempt_id=start.attempt_id,
                fencing_token=start.fencing_token,
                fetch_token="tok2",
                blob_url="http://ctl:47110",
                blob_port=47110,
            ),
        )
    )
    await _until(lambda: len(surface.items_requests) == 2)
    assert surface.items_requests == ["tok", "tok2"]
    await _until(lambda: "running" in _states(reports))  # staged with the new token
    await _cancel(executor, msg)
    assert _final_failure(reports) == "cancelled"


@pytest.mark.asyncio
async def test_a_cut_item_list_download_is_retried(tmp_path: Path) -> None:
    built = _tree(tmp_path)
    surface = _Surface(built, {1: "cut"})
    executor, reports = _executor(tmp_path, surface, staging_retry_s=60)
    msg = _start(built)
    executor.handle(msg)
    await _until(lambda: len(surface.items_requests) == 2)
    await _until(lambda: "running" in _states(reports))
    assert "failed" not in _states(reports)
    await _cancel(executor, msg)


@pytest.mark.asyncio
async def test_a_refusal_nobody_refreshes_ends_retryable_transfer_error(
    tmp_path: Path,
) -> None:
    """The lab case: the controller had already ended the lane. Whatever ends
    it on the agent, it is never a final ``unauthorized``."""
    built = _tree(tmp_path)
    surface = _Surface(built, dict.fromkeys(range(1, 100), "refuse"))
    executor, reports = _executor(tmp_path, surface, staging_retry_s=0.5)
    executor.handle(_start(built))
    await _settle(executor)
    final = reports.sent[-1]
    assert _states(reports)[-1] == "failed"
    assert _final_failure(reports) == "transfer_error", final.failure.message
    assert final.failure.retryable
