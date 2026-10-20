"""Fake Mode 3 lanes over a real gRPC session (V0.10 G4 component tests).

:class:`FakeLane` drives a :class:`mf_session_client.FakeAgent` the way a real
agent's lane runner does - the wire protocol only, no staging or processes:

* every command is acked (``StartAttempt`` included, so Reconcile does not
  re-send it);
* a ``StartAttempt`` with ``lane`` -> ``STAGING``, ``ENV_READY``, ``RUNNING``,
  then the item list is downloaded with the lane's fetch token (checks the
  lane's fetch scope) and verified;
* pull loop: ``ChunkRequest`` -> ``ChunkGrant`` -> per chunk: the slice digest
  is checked against the item list, the "work" (``output_of(items)``) is
  uploaded under the lane's upload grant (``PUT /v1/blobs``, the lane node's
  mTLS certificate) and reported ``ChunkResult(done)``; a chunk in ``fail``
  is reported ``failed`` (``user_error``, retryable); ``NoWork(wait)`` waits,
  ``drained`` ends the lane ``DONE``, ``stop`` ends it ``FAILED(cancelled)``;
* a ``CancelAttempt`` stops the lane (``FAILED(cancelled)``); ``kill()`` drops
  the stream with no Goodbye (the node goes SUSPECT -> OFFLINE).
* V0.10 G5: a ``StartAttempt`` with ``combine`` -> ``STAGING``, the parts
  index and every part downloaded with the attempt's fetch token (checks its
  scope) and verified, ``ENV_READY``, ``RUNNING``, ``UPLOADING``, the output
  (``combine_of(parts)``: the part blobs concatenated in chunk order) uploaded
  under its grant, ``DONE`` with its digest; ``combine_fail`` reports a
  failure instead, ``combine_hold`` never finishes (a node to kill).

``speed_s`` is the time one chunk "takes"; ``ledger`` records every chunk this
lane ran ((chunk_index, lease_seq)).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx
from mf_jobs_harness import api_client
from mf_session_client import FakeAgent

from meshforge.common.errors import FailureType
from meshforge.common.hashing import sha256_bytes
from meshforge.common.ids import new_command_id
from meshforge.common.proto.meshforge.v1 import agent_pb2, common_pb2
from meshforge.common.proto_codec import attempt_state_to_proto, failure_type_to_proto
from meshforge.common.split_chunks import (
    ChunkSpan,
    ItemList,
    chunk_items_digest,
    parse_item_list,
    parse_parts_index,
)
from meshforge.common.states import AttemptState

__all__ = ["FakeLane", "default_output"]


def _bearer(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


@dataclass
class _Upload:
    client: httpx.AsyncClient
    token: str

    async def put(self, path: str, *, content: bytes) -> httpx.Response:
        return await self.client.put(path, content=content, headers=_bearer(self.token))


def default_output(items: Sequence[str | int]) -> bytes:
    """A deterministic chunk output: the items, one per line, upper-cased."""
    return "".join(f"{str(i).upper()}\n" for i in items).encode()


@dataclass
class FakeLane:
    agent: FakeAgent
    runtime: Any
    speed_s: float = 0.0
    fail: set[int] = field(default_factory=set)
    """Chunk indices this lane reports failed."""
    output_of: Callable[[Sequence[str | int]], bytes] = default_output
    ledger: list[tuple[int, int]] = field(default_factory=list)
    results_sent: list[str] = field(default_factory=list)
    hold_results: bool = False
    """True = run chunks but never send their results (a hung lane)."""
    combine_of: Callable[[Sequence[bytes]], bytes] = b"".join
    """V0.10 G5: the fake combine's output from the part blobs (chunk order)."""
    combine_fail: tuple[FailureType, str, bool] | None = None
    """V0.10 G5: report the combine failed (type, detail, retryable)."""
    combine_hold: bool = False
    """V0.10 G5: the combine stages, then waits - until a ``CancelAttempt``
    (-> ``FAILED(cancelled)``) or the node is killed."""
    delay_first_result_s: float = 0.0
    """V0.10 G5: the first chunk's result is sent this much later (past its
    lease's visibility timeout: a late, second result of that chunk)."""
    _delayed: bool = False
    combines: list[str] = field(default_factory=list)
    """Combine attempts this node started (attempt ids)."""
    combine_parts: dict[str, list[bytes]] = field(default_factory=dict)
    """attempt_id -> the part blobs its combine downloaded (chunk order)."""
    _seen: int = 0
    _tasks: list[asyncio.Task[None]] = field(default_factory=list)
    _inbox: dict[str, asyncio.Queue[agent_pb2.ControllerMessage]] = field(
        default_factory=dict
    )
    _pump: asyncio.Task[None] | None = None
    killed: bool = False
    _client: httpx.AsyncClient | None = None
    _client_cm: Any = None
    stopped: dict[str, str] = field(default_factory=dict)
    """attempt_id -> how the lane ended (done / cancelled / stop)."""

    # ------------------------------------------------------------ wiring

    def start(self) -> None:
        self._pump = asyncio.create_task(self._pump_loop(), name="fake-lane-pump")

    async def _write(self, **payload: Any) -> str:
        msg = agent_pb2.AgentMessage(
            message_id=new_command_id(), session_id=self.agent.session_id, **payload
        )
        with contextlib.suppress(Exception):
            await self.agent.write(msg)
        return msg.message_id

    async def _ack(self, msg: agent_pb2.ControllerMessage) -> None:
        await self._write(ack=agent_pb2.Ack(acked_message_id=msg.message_id))

    async def _pump_loop(self) -> None:
        while not self.agent.ended.is_set():
            got = self.agent.received[self._seen :]
            self._seen += len(got)
            for msg in got:
                which = msg.WhichOneof("payload")
                if which in ("ack", "ping", "hello_ack", None):
                    continue
                await self._ack(msg)
                if which == "start_attempt" and msg.start_attempt.HasField("combine"):
                    if msg.start_attempt.attempt_id in self._inbox:
                        continue
                    self._inbox[msg.start_attempt.attempt_id] = asyncio.Queue()
                    self._tasks.append(
                        asyncio.create_task(self._combine(msg.start_attempt))
                    )
                    continue
                if which == "start_attempt" and msg.start_attempt.HasField("lane"):
                    if msg.start_attempt.attempt_id in self._inbox:
                        continue  # a re-sent start: already running
                    self._inbox[msg.start_attempt.attempt_id] = asyncio.Queue()
                    self._tasks.append(
                        asyncio.create_task(self._lane(msg.start_attempt))
                    )
                    continue
                inner = getattr(msg, which)
                box = self._inbox.get(getattr(inner, "attempt_id", ""))
                if box is not None:
                    await box.put(msg)
            await asyncio.sleep(0.01)

    async def kill(self) -> None:
        """Drop the stream with no Goodbye (like kill -9 of the agent)."""
        self.killed = True
        for task in self._tasks:
            task.cancel()
        if self._pump is not None:
            self._pump.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.agent.abort()

    async def _http(self) -> httpx.AsyncClient:
        """One HTTPS client per fake node (its mTLS certificate), closed by
        :meth:`close` - never left to a cancelled task to close."""
        if self._client is None:
            self._client_cm = api_client(self.runtime, node_id=self.agent.node_id)
            self._client = await self._client_cm.__aenter__()
        return self._client

    async def close(self) -> None:
        # Lanes that are ending (a status write, an HTTP client closing) get a
        # moment to finish: a cancel mid-request can leave a socket unclosed.
        live = [t for t in self._tasks if not t.done()]
        if live:
            await asyncio.wait(live, timeout=2.0)
        for task in self._tasks:
            task.cancel()
        if self._pump is not None:
            self._pump.cancel()
        await asyncio.gather(
            *self._tasks, *([self._pump] if self._pump else []), return_exceptions=True
        )
        if self._client_cm is not None:
            await self._client_cm.__aexit__(None, None, None)
            self._client_cm = self._client = None

    # ------------------------------------------------------------ one lane

    async def _status(
        self,
        start: agent_pb2.StartAttempt,
        state: AttemptState,
        failure: tuple[FailureType, str] | None = None,
        *,
        retryable: bool = False,
        output_digest: str = "",
    ) -> None:
        status = agent_pb2.AttemptStatus(
            job_id=start.job_id,
            attempt_id=start.attempt_id,
            fencing_token=start.fencing_token,
            state=attempt_state_to_proto(state),
            runtime="subprocess",
            output_digest=output_digest,
        )
        if failure is not None:
            status.failure.CopyFrom(
                common_pb2.Failure(
                    failure_type=failure_type_to_proto(failure[0]),
                    message=failure[1],
                    retryable=retryable,
                )
            )
        if state in (AttemptState.DONE, AttemptState.FAILED):
            status.phases.run_s = 0.25
        await self._write(attempt_status=status)

    # ------------------------------------------------------------ combine

    async def _combine(self, start: agent_pb2.StartAttempt) -> None:
        """V0.10 G5: the wire side of a combine (module docstring)."""
        self.combines.append(start.attempt_id)
        await self._status(start, AttemptState.STAGING)
        client = await self._http()
        token = _bearer(start.spec.fetch_token)
        index_hex = start.combine.parts_digest.removeprefix("sha256:")
        got = await client.get(f"/v1/blobs/{index_hex}", headers=token)
        assert got.status_code == 200, got.text
        assert str(sha256_bytes(got.content)) == start.combine.parts_digest
        index = parse_parts_index(got.content)
        assert len(index.chunks) == start.combine.chunk_count
        parts: list[bytes] = []
        for _first, _count, digest in index.chunks:
            part = await client.get(
                f"/v1/blobs/{digest.removeprefix('sha256:')}", headers=token
            )
            assert part.status_code == 200, part.text
            assert str(sha256_bytes(part.content)) == digest
            parts.append(part.content)
        self.combine_parts[start.attempt_id] = parts
        if self.combine_hold:
            box = self._inbox[start.attempt_id]
            await self._next(box, ("cancel_attempt",))
            await self._status(
                start, AttemptState.FAILED, (FailureType.CANCELLED, "cancelled")
            )
            return
        for state in (AttemptState.ENV_READY, AttemptState.RUNNING):
            await self._status(start, state)
        if self.combine_fail is not None:
            kind, detail, retryable = self.combine_fail
            await self._status(
                start, AttemptState.FAILED, (kind, detail), retryable=retryable
            )
            return
        await self._status(start, AttemptState.UPLOADING)
        data = self.combine_of(parts)
        digest = sha256_bytes(data)
        # not through _Upload (tests patch it to make LANES lie about uploads)
        put = await client.put(
            f"/v1/blobs/{digest.hex}",
            content=data,
            headers=_bearer(start.spec.upload_token),
        )
        assert put.status_code in (200, 201), put.text
        await self._status(start, AttemptState.DONE, output_digest=str(digest))

    async def _lane(self, start: agent_pb2.StartAttempt) -> None:
        attempt_id = start.attempt_id
        box = self._inbox[attempt_id]
        for state in (
            AttemptState.STAGING,
            AttemptState.ENV_READY,
            AttemptState.RUNNING,
        ):
            await self._status(start, state)
        client = await self._http()
        hex_ = start.lane.items_digest.removeprefix("sha256:")
        got = await client.get(
            f"/v1/blobs/{hex_}", headers=_bearer(start.spec.fetch_token)
        )
        assert got.status_code == 200, got.text
        assert str(sha256_bytes(got.content)) == start.lane.items_digest
        items = parse_item_list(got.content)
        up = _Upload(client, start.spec.upload_token)
        held: list[agent_pb2.ChunkLeaseRef] = []
        while True:
            await self._write(
                chunk_request=agent_pb2.ChunkRequest(
                    job_id=start.job_id,
                    attempt_id=attempt_id,
                    fencing_token=start.fencing_token,
                    want=1,
                    held=held,
                )
            )
            reply = await self._next(box, ("chunk_grant", "no_work", "cancel_attempt"))
            which = reply.WhichOneof("payload")
            if which == "cancel_attempt":
                self.stopped[attempt_id] = "cancelled"
                await self._status(
                    start, AttemptState.FAILED, (FailureType.CANCELLED, "cancelled")
                )
                return
            if which == "no_work":
                reason = reply.no_work.reason
                if reason == agent_pb2.NoWork.REASON_WAIT:
                    await asyncio.sleep(max(0.05, reply.no_work.retry_after_s))
                    continue
                if reason == agent_pb2.NoWork.REASON_DRAINED:
                    self.stopped[attempt_id] = "done"
                    await self._status(start, AttemptState.DONE)
                    return
                self.stopped[attempt_id] = "stop"
                await self._status(
                    start, AttemptState.FAILED, (FailureType.CANCELLED, "stopped")
                )
                return
            for chunk in reply.chunk_grant.chunks:
                ref = agent_pb2.ChunkLeaseRef(
                    item_id=chunk.item_id, lease_seq=chunk.lease_seq
                )
                held.append(ref)
                await self._run_chunk(start, up, items, chunk, box)
                held.remove(ref)

    async def _next(
        self, box: asyncio.Queue[agent_pb2.ControllerMessage], kinds: tuple[str, ...]
    ) -> agent_pb2.ControllerMessage:
        while True:
            msg = await box.get()
            if msg.WhichOneof("payload") in kinds:
                return msg

    async def _run_chunk(
        self,
        start: agent_pb2.StartAttempt,
        up: _Upload,
        items: ItemList,
        chunk: agent_pb2.GrantedChunk,
        box: asyncio.Queue[agent_pb2.ControllerMessage],
    ) -> None:
        span = ChunkSpan(chunk.first_item, chunk.item_count)
        mine = items.chunk(span)
        assert str(chunk_items_digest(mine)) == chunk.items_digest
        if self.speed_s:
            await asyncio.sleep(self.speed_s)
        self.ledger.append((chunk.chunk_index, chunk.lease_seq))
        if self.hold_results:
            await asyncio.Event().wait()
        if self.delay_first_result_s and not self._delayed:
            self._delayed = True
            await asyncio.sleep(self.delay_first_result_s)
        result = agent_pb2.ChunkResult(
            job_id=start.job_id,
            attempt_id=start.attempt_id,
            fencing_token=start.fencing_token,
            chunk_index=chunk.chunk_index,
            item_id=chunk.item_id,
            lease_seq=chunk.lease_seq,
        )
        if chunk.chunk_index in self.fail:
            result.outcome = agent_pb2.ChunkResult.OUTCOME_FAILED
            result.exit_code = 3
            result.failure.CopyFrom(
                common_pb2.Failure(
                    failure_type=failure_type_to_proto(FailureType.USER_ERROR),
                    message=f"exit 3 on chunk {chunk.chunk_index}",
                    retryable=True,
                )
            )
        else:
            data = self.output_of(mine)
            digest = sha256_bytes(data)
            put = await up.put(f"/v1/blobs/{digest.hex}", content=data)
            assert put.status_code in (200, 201), put.text
            result.outcome = agent_pb2.ChunkResult.OUTCOME_DONE
            result.result_digest = str(digest)
            result.result_bytes = len(data)
            result.exit_code = 0
        self.results_sent.append(await self._write(chunk_result=result))


def expect_json(resp: httpx.Response) -> Any:
    assert resp.status_code == 200, resp.text
    return json.loads(resp.text)
