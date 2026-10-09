"""V0.7 G3 deliverable 5 (D147): peer tokens live at most 60 s and are
re-minted while an attempt stages - controller side, without a database.

* the cap: signer, issuer and ``[controller.jobs] peer_token_ttl_s`` refuse
  more than 60 s; the verifier refuses a token whose expiry lies further ahead;
* the dispatcher sends ``RefreshPeerTokens`` to an ASSIGNED/STAGING tree
  attempt every third of the token life (never right after StartAttempt),
  one fresh token per (tree, current holder) incl. named input trees, and
  nothing to RUNNING attempts, bundle jobs or nodes without a session.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

from meshforge.common.hashing import Digest
from meshforge.common.ids import new_attempt_id, new_command_id, new_job_id, new_node_id
from meshforge.common.peer_token import (
    MAX_TTL_S,
    SKEW_S,
    PeerTokenError,
    load_public_key,
    sign_peer_token,
    verify_peer_token,
)
from meshforge.common.states import AttemptState
from meshforge.controller.blobs import FetchTokens
from meshforge.controller.jobs.dispatcher import Dispatcher
from meshforge.controller.jobs.peers import PeerSources
from meshforge.controller.liveness import NodeState
from meshforge.controller.peer_signing import PeerTokenIssuer
from meshforge.controller.settings import JobApiSettings

pytestmark = [pytest.mark.unit]

TREE = "sha256:" + "ab" * 32
INPUT = "sha256:" + "ef" * 32
NOW = 1_800_000_000.0


# ------------------------------------------------------------------ the cap


def test_peer_tokens_are_capped_at_sixty_seconds() -> None:
    key = Ed25519PrivateKey.generate()
    assert MAX_TTL_S == 60
    kwargs: dict[str, Any] = {
        "tree": Digest.parse(TREE),
        "requester": str(new_node_id()),
        "holder": str(new_node_id()),
        "attempt_id": str(new_attempt_id()),
        "now": NOW,
    }
    with pytest.raises(ValueError, match=r"1\.\.60"):
        sign_peer_token(key, ttl_s=61, **kwargs)
    with pytest.raises(ValueError, match=r"1\.\.60"):
        PeerTokenIssuer(key, ttl_s=61)
    assert PeerTokenIssuer(key).ttl_s == 60
    # A token signed "in the future" (exp beyond now + 60 + skew) is refused
    # by the holder even though its signature is good.
    ahead = sign_peer_token(key, ttl_s=60, **{**kwargs, "now": NOW + SKEW_S + 1})
    public = load_public_key(PeerTokenIssuer(key).public_key)
    with pytest.raises(PeerTokenError) as caught:
        verify_peer_token(public, ahead, holder=kwargs["holder"], now=NOW)
    assert caught.value.reason == "too_long"
    within = sign_peer_token(key, ttl_s=60, **{**kwargs, "now": NOW + SKEW_S})
    verify_peer_token(public, within, holder=kwargs["holder"], now=NOW)


def test_settings_default_and_bounds() -> None:
    assert JobApiSettings().peer_token_ttl_s == 60
    for bad in (4, 61, 600):
        with pytest.raises(ValidationError):
            JobApiSettings(peer_token_ttl_s=bad)
    assert JobApiSettings(peer_token_ttl_s=5).peer_token_ttl_s == 5


# --------------------------------------------------------------- the re-mint


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class FakeManager:
    def __init__(self, lives: list[Any]) -> None:
        self.hooks = SimpleNamespace(
            on_command_acked=None, on_reconcile=None, on_node_offline=None
        )
        self.dispatch = SimpleNamespace(register=lambda *_a: None)
        self.commands: list[tuple[str, str, Any]] = []
        self.lives = {live.node_id: live for live in lives}

    def defer_ack(self, _p: str) -> None:
        return None

    def snapshot(self) -> list[Any]:
        return list(self.lives.values())

    def get(self, node_id: str) -> Any:
        return self.lives.get(node_id)

    def send_command(
        self, node_id: str, name: str, payload: Any, **_kw: Any
    ) -> str | None:
        self.commands.append((node_id, name, payload))
        return str(new_command_id())

    def named(self, name: str) -> list[Any]:
        return [p for _n, c, p in self.commands if c == name]


class FakeCache:
    def __init__(self, rows: dict[str, list[tuple[str, float]]]) -> None:
        self.rows = rows

    async def holders(
        self, kind: str, key: str, *, among: Any
    ) -> list[tuple[str, float]]:
        assert kind == "tree"
        return [r for r in self.rows.get(key, []) if r[0] in among]


class FakeInputs:
    def __init__(self, trees: list[str]) -> None:
        self.trees = trees

    async def job_inputs(self, _job_id: str) -> list[Any]:
        return [
            SimpleNamespace(tree_id=t, mount=f"in{i}", ref=f"in{i}@1")
            for i, t in enumerate(self.trees)
        ]


def _live(node_id: str) -> Any:
    return SimpleNamespace(
        node_id=node_id,
        state=NodeState.ONLINE,
        ended=False,
        peer_endpoint="10.0.0.2:47120",
        session_id=f"sess_{node_id}",
    )


def _job(*, tree: bool = True) -> Any:
    return SimpleNamespace(
        id=str(new_job_id()),
        manifest={"name": "j", "entrypoint": ["python", "m.py"], "limits": {}},
        bundle_digest=None if tree else "sha256:" + "cd" * 32,
        tree_id=TREE if tree else None,
        cancel_requested_at=None,
    )


def _attempt(job: Any, node_id: str, state: AttemptState) -> Any:
    return SimpleNamespace(
        id=str(new_attempt_id()),
        job_id=job.id,
        node_id=node_id,
        fencing_token=7,
        state=state,
        start_acked_at=object(),
        start_snapshot={},
    )


class Rig:
    def __init__(self, *, ttl_s: int = 30, inputs: list[str] | None = None) -> None:
        self.clock = Clock()
        self.target = str(new_node_id())
        self.holder = str(new_node_id())
        self.input_holder = str(new_node_id())
        self.manager = FakeManager(
            [_live(self.target), _live(self.holder), _live(self.input_holder)]
        )
        self.key = Ed25519PrivateKey.generate()
        issuer = PeerTokenIssuer(self.key, ttl_s=ttl_s, clock=lambda: NOW)
        cache = FakeCache(
            {TREE: [(self.holder, 1.0)], INPUT: [(self.input_holder, 1.0)]}
        )
        self.peers = PeerSources(
            self.manager,  # type: ignore[arg-type]
            cache,  # type: ignore[arg-type]
            issuer,
            max_sources=4,
            index_live=lambda _n, _s: True,
        )
        self.entries: list[Any] = []
        self.dispatcher = Dispatcher(
            store=SimpleNamespace(live_attempts=self._live_attempts),  # type: ignore[arg-type]
            manager=self.manager,  # type: ignore[arg-type]
            fetch_tokens=FetchTokens(clock=self.clock),
            settings=JobApiSettings(),
            clock=self.clock,
            blob_port=lambda: 8080,
            peers=self.peers,
            inputs=FakeInputs(inputs or []),  # type: ignore[arg-type]
        )

    async def _live_attempts(self, _node: str | None = None) -> list[Any]:
        return self.entries

    def add(self, state: AttemptState, *, tree: bool = True) -> tuple[Any, Any]:
        job = _job(tree=tree)
        attempt = _attempt(job, self.target, state)
        self.entries.append(SimpleNamespace(attempt=attempt, job=job))
        return job, attempt

    async def remint(self) -> list[Any]:
        before = len(self.manager.named("refresh_peer_tokens"))
        await self.dispatcher._remint_peer_tokens(self.entries)
        return self.manager.named("refresh_peer_tokens")[before:]


@pytest.mark.asyncio
async def test_a_staging_attempt_gets_fresh_peer_tokens_every_third_of_their_life() -> (
    None
):
    rig = Rig(ttl_s=30)
    assert rig.peers.remint_interval_s == 10.0
    job, attempt = rig.add(AttemptState.ASSIGNED)
    await rig.dispatcher._send_start(job, attempt, resend=False)
    assert await rig.remint() == []  # just minted with the StartAttempt
    rig.clock.now += 9.9
    assert await rig.remint() == []
    rig.clock.now += 0.2
    (sent,) = await rig.remint()
    assert sent.attempt_id == attempt.id and sent.fencing_token == 7
    (grant,) = sent.tokens
    assert grant.tree_id == TREE and grant.node_id == rig.holder
    public = load_public_key(rig.peers._issuer.public_key)
    claims = verify_peer_token(public, grant.token, holder=rig.holder, now=NOW)
    assert str(claims.tree) == TREE
    assert claims.requester == rig.target and claims.attempt_id == attempt.id
    assert claims.exp == int(NOW) + 30  # never longer than the token life
    attempt.state = AttemptState.STAGING
    rig.clock.now += 10.0
    assert len(await rig.remint()) == 1
    assert rig.dispatcher.stats.peer_tokens_reminted == 2


@pytest.mark.asyncio
async def test_input_trees_are_reminted_with_their_own_holders() -> None:
    rig = Rig(inputs=[INPUT, TREE])
    _unused, attempt = rig.add(AttemptState.STAGING)
    (sent,) = await rig.remint()  # never minted in this process: due at once
    pairs = sorted((g.tree_id, g.node_id) for g in sent.tokens)
    assert pairs == sorted([(TREE, rig.holder), (INPUT, rig.input_holder)])
    assert attempt.id in rig.dispatcher._peer_minted


@pytest.mark.asyncio
async def test_nothing_for_running_bundle_or_disconnected_attempts() -> None:
    rig = Rig()
    _j1, running = rig.add(AttemptState.RUNNING)
    _j2, bundle = rig.add(AttemptState.STAGING, tree=False)
    _j3, gone = rig.add(AttemptState.STAGING)
    gone.node_id = str(new_node_id())  # no live session
    assert await rig.remint() == []
    assert running.id not in rig.dispatcher._peer_minted
    assert bundle.id not in rig.dispatcher._peer_minted
    # An attempt that left staging is forgotten.
    _j4, staging = rig.add(AttemptState.STAGING)
    assert len(await rig.remint()) == 1
    staging.state = AttemptState.RUNNING
    await rig.remint()
    assert staging.id not in rig.dispatcher._peer_minted
