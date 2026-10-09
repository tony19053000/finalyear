"""V0.7 G3 deliverable 5 (D147), agent side without a network.

* ``RefreshPeerTokens`` is applied only to its (attempt_id, fencing_token)
  while the attempt lives; malformed grants are dropped one by one; the number
  of (tree, holder) pairs an attempt keeps is bounded;
* a :class:`~meshforge.agent.cas.sources.Source` asks for the newest token at
  every request - a peer's re-minted one, never for the controller.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest

from meshforge.agent.cas.sources import BlobSource, Source, SourceStats
from meshforge.agent.execution import executor as executor_module
from meshforge.agent.execution.executor import MAX_PEER_TOKENS, Executor
from meshforge.agent.execution.offer import ExecutionSetup
from meshforge.agent.execution.runtime import RuntimeAvailability
from meshforge.agent.execution.selection import execution_offer
from meshforge.agent.settings import AgentSettings
from meshforge.common.ids import new_attempt_id, new_command_id, new_job_id, new_node_id
from meshforge.common.proto.meshforge.v1 import agent_pb2

pytestmark = pytest.mark.unit

TREE = "sha256:" + "ab" * 32
OTHER = "sha256:" + "cd" * 32


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


async def _hang(*_a: Any, **_k: Any) -> int:
    await asyncio.Event().wait()
    return 0


def _executor(tmp_path: Path) -> Executor:
    settings = AgentSettings.load(
        data_dir=tmp_path / "data",
        agent={
            "controller_address": "127.0.0.1:1",
            "execution": {"enabled": True, "allow_unsandboxed": True},
        },
    )
    offer = execution_offer(
        enabled=True,
        allow_unsandboxed=True,
        docker=None,
        subprocess=RuntimeAvailability("subprocess", True),
    )

    async def report(_name: str, _payload: Any) -> str:
        return str(new_command_id())

    return Executor(
        settings,
        ExecutionSetup(offer=offer, runtimes={"subprocess": _Runtime()}),  # type: ignore[dict-item]
        node_id="node_x",
        report=report,
        controller_host=lambda: "127.0.0.1",
        fetch=_hang,
    )


def _start(attempt_id: str) -> agent_pb2.ControllerMessage:
    return agent_pb2.ControllerMessage(
        message_id=str(new_command_id()),
        start_attempt=agent_pb2.StartAttempt(
            job_id=str(new_job_id()),
            attempt_id=attempt_id,
            fencing_token=3,
            spec=agent_pb2.ExecutionSpec(
                bundle_digest="sha256:" + "11" * 32,
                blob_port=47110,
                fetch_token="tok",
                manifest_json='{"name":"x","entrypoint":["python","main.py"]}',
                timeout_s=60,
            ),
        ),
    )


def _refresh(
    attempt_id: str, grants: list[tuple[str, str, str]], fencing: int = 3
) -> agent_pb2.ControllerMessage:
    return agent_pb2.ControllerMessage(
        message_id=str(new_command_id()),
        refresh_peer_tokens=agent_pb2.RefreshPeerTokens(
            attempt_id=attempt_id,
            fencing_token=fencing,
            tokens=[
                agent_pb2.PeerTokenGrant(tree_id=t, node_id=n, token=tok)
                for t, n, tok in grants
            ],
        ),
    )


@pytest.mark.asyncio
async def test_refresh_applies_to_its_attempt_only_and_drops_bad_grants(
    tmp_path: Path,
) -> None:
    executor = _executor(tmp_path)
    attempt_id = str(new_attempt_id())
    executor.handle(_start(attempt_id))
    await asyncio.sleep(0.05)
    attempt = executor._attempts[attempt_id]
    holder = str(new_node_id())
    executor.handle(_refresh(attempt_id, [(TREE, holder, "t1")], fencing=4))
    executor.handle(_refresh(str(new_attempt_id()), [(TREE, holder, "t1")]))
    assert attempt.peer_tokens == {}
    executor.handle(
        _refresh(
            attempt_id,
            [
                (TREE, holder, "t1"),
                ("sha256:xyz", holder, "t2"),  # malformed tree
                (OTHER, "not-a-node", "t3"),  # malformed node id
                (OTHER, holder, "bad token"),  # whitespace
                (OTHER, holder, ""),  # empty
            ],
        )
    )
    assert attempt.peer_tokens == {("ab" * 32, holder): "t1"}
    executor.handle(_refresh(attempt_id, [(TREE, holder, "t1-newer")]))
    lookup = executor._token_lookup(attempt, TREE)
    assert lookup(BlobSource("https://h:1", "listed", holder)) == "t1-newer"
    assert lookup(BlobSource("https://h:1", "listed", str(new_node_id()))) is None
    assert (
        executor._token_lookup(attempt, OTHER)(
            BlobSource("https://h:1", "listed", holder)
        )
        is None
    )
    await executor.shutdown()


@pytest.mark.asyncio
async def test_the_pairs_an_attempt_keeps_are_bounded(tmp_path: Path) -> None:
    executor = _executor(tmp_path)
    attempt_id = str(new_attempt_id())
    executor.handle(_start(attempt_id))
    await asyncio.sleep(0.05)
    attempt = executor._attempts[attempt_id]
    many = [(TREE, str(new_node_id()), f"t{i}") for i in range(MAX_PEER_TOKENS + 10)]
    executor.handle(_refresh(attempt_id, many[: MAX_PEER_TOKENS // 2]))
    executor.handle(_refresh(attempt_id, many[MAX_PEER_TOKENS // 2 :]))
    assert len(attempt.peer_tokens) == MAX_PEER_TOKENS
    # A known pair is still renewed when the book is full.
    first = many[0]
    executor.handle(_refresh(attempt_id, [(first[0], first[1], "renewed")]))
    assert attempt.peer_tokens[("ab" * 32, first[1])] == "renewed"
    await executor.shutdown()


def test_a_source_asks_for_the_newest_token_at_every_request() -> None:
    holder = str(new_node_id())
    book: dict[str, str] = {}

    def lookup(src: BlobSource) -> str | None:
        return book.get(src.node_id)

    peer = Source(
        BlobSource("https://p:1", "listed", holder), SourceStats(), None, lookup
    )
    controller = Source(
        BlobSource("https://c:1", "fetch", ""), SourceStats(), None, lookup
    )
    assert peer.headers()["authorization"] == "Bearer listed"
    book[holder] = "reminted"
    book[""] = "never-for-the-controller"
    assert peer.headers({"range": "bytes=0-1"}) == {
        "authorization": "Bearer reminted",
        "range": "bytes=0-1",
    }
    assert controller.headers()["authorization"] == "Bearer fetch"
    assert "reminted" not in repr(peer.src)
    assert executor_module.MAX_PEER_TOKENS >= 16
