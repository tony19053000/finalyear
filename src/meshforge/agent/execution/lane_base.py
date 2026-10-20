"""Lane state, constants and the executor surface the lane steps use (V0.10
G4; split out of :mod:`.lane` in fix round 2)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from meshforge.agent.execution.runtime import Stream
from meshforge.common.errors import FailureType
from meshforge.common.hashing import Digest
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.common.split_chunks import ItemList
from meshforge.common.states import AttemptState

if TYPE_CHECKING:
    from meshforge.agent.execution.executor import _Attempt
    from meshforge.agent.execution.logs import AttemptLogs
    from meshforge.agent.execution.plan import AttemptPlan

__all__ = [
    "ASK_TIMEOUT_S",
    "CHUNK_FAILURE_NOT_RETRYABLE",
    "RESULT_ACK_WAIT_S",
    "STAGE_RETRYABLE",
    "LaneHost",
    "LaneState",
    "LaneStopError",
]

ASK_TIMEOUT_S: Final = 15.0
"""No answer to a ChunkRequest this long: ask again (the request is unbuffered)."""
RESULT_ACK_WAIT_S: Final = 30.0
"""How long a drained lane waits for its last results' Acks before it ends."""
STAGE_RETRYABLE: Final = frozenset(
    {FailureType.TRANSFER_ERROR, FailureType.TIMEOUT, FailureType.UNAUTHORIZED}
)
CHUNK_FAILURE_NOT_RETRYABLE: Final = frozenset(
    {
        FailureType.INVALID_INPUT,
        FailureType.UNAUTHORIZED,
        FailureType.CONFIG_ERROR,
        FailureType.NO_FEASIBLE_PLAN,
    }
)
"""D267c: every other chunk failure may succeed on another node - the
controller's queue bounds the tries (``retry.max_attempts``, two nodes)."""


class LaneStopError(Exception):
    """The controller said ``NoWork(stop)``: the lane ends ``cancelled``."""


@dataclass(slots=True)
class LaneState:
    """One lane's state on the agent (``_Attempt.lane``)."""

    spec: agent_pb2.LaneSpec
    items: ItemList | None = None
    by_tree: dict[str | None, frozenset[str]] = field(default_factory=dict)
    """``glob``: item paths per tree (``None`` = the project, else a mount)."""
    inbox: asyncio.Queue[agent_pb2.ControllerMessage] = field(
        default_factory=asyncio.Queue
    )
    held: dict[tuple[int, int], int] = field(default_factory=dict)
    """(item_id, lease_seq) -> chunk index of every lease the lane holds."""
    finished: set[tuple[int, int]] = field(default_factory=set)
    """Leases this lane already ran (a late duplicate grant is ignored)."""
    current: tuple[int, int] | None = None
    cancelled_chunk: tuple[int, int] | None = None
    """``CancelChunk`` asked to stop the running chunk."""
    run_task: asyncio.Future[Any] | None = None
    pending: dict[str, agent_pb2.ChunkResult] = field(default_factory=dict)
    """message_id -> ChunkResult not acked yet (re-sent until acked)."""
    acked: asyncio.Event = field(default_factory=asyncio.Event)
    chunks_run: int = 0
    env_s: float = 0.0
    capped: int | None = None
    """Bytes written when the per-chunk output cap stopped the chunk."""


class LaneHost:
    """What the lane steps use of the executor: its attributes and helpers,
    declared here for the type checker (the executor provides them)."""

    _clock: Callable[[], float]
    _cfg: Any
    _fetch_cfg: Any
    _node_id: str
    _scheme: str
    _attempts: dict[str, _Attempt]
    _report: Callable[[str, Any], Awaitable[str]]
    _resend: Any
    _send_now: Callable[[str, Any], Awaitable[bool]] | None
    _controller_host: Callable[[], str | None]
    cas: Any
    work_root: Path

    if TYPE_CHECKING:

        async def _step[T](self, attempt: _Attempt, awaitable: Awaitable[T]) -> T: ...
        async def _send(
            self,
            attempt: _Attempt,
            state: AttemptState,
            *,
            failure: tuple[FailureType, str, bool] | None = None,
            exit_code: int | None = None,
            signal: int | None = None,
            output_digest: str = "",
            missing: Sequence[str] = (),
            logs: Mapping[Stream, agent_pb2.FinalLog] | None = None,
        ) -> None: ...
        async def _announce_run(self, attempt: _Attempt) -> None: ...
        async def _final_logs(
            self, attempt: _Attempt, plan: AttemptPlan, logs: AttemptLogs
        ) -> Mapping[Stream, agent_pb2.FinalLog]: ...
        async def _upload_retrying(
            self,
            attempt: _Attempt,
            plan: AttemptPlan,
            digest: Digest,
            path: Path,
            *,
            best_effort: bool = False,
        ) -> None: ...
        def _transport(self) -> dict[str, Any]: ...
        def _tuning(self) -> Any: ...
        def _token_lookup(self, attempt: _Attempt, tree_id: str) -> Any: ...
        def _stopped_error(self) -> Exception: ...
        def _current_spec(self, attempt: _Attempt) -> agent_pb2.ExecutionSpec: ...
