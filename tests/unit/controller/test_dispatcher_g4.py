"""V0.5 G4 (D89/D95/D98/D100) dispatcher pieces without a database:

* the fetch-token scope is explicit (tree job -> the tree, bundle job -> the
  bundle) and a RefreshFetchToken's scope equals the StartAttempt's;
* a tree job's sources are the controller first, then the peers; the refresh
  carries the whole list re-minted;
* PeerSources lists only ONLINE holders with an endpoint, never the target,
  newest index row first, at most max_sources, and falls back to none when the
  index cannot be read.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from meshforge.common.hashing import Digest
from meshforge.common.ids import new_attempt_id, new_command_id, new_job_id, new_node_id
from meshforge.common.peer_token import load_public_key, verify_peer_token
from meshforge.common.states import AttemptRole, AttemptState
from meshforge.controller.blobs import FetchTokens
from meshforge.controller.cache_index import CacheIndex, NodeCacheState, Phase
from meshforge.controller.jobs.dispatcher import Dispatcher, _Reconcile, fetch_scope
from meshforge.controller.jobs.peers import PeerSources
from meshforge.controller.liveness import NodeState
from meshforge.controller.peer_signing import PeerTokenIssuer
from meshforge.controller.settings import JobApiSettings

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

TREE = "sha256:" + "ab" * 32
BUNDLE = "sha256:" + "cd" * 32
NOW = 1_800_000_000.0


class FakeManager:
    def __init__(self, lives: list[Any] | None = None) -> None:
        self.hooks = SimpleNamespace(
            on_command_acked=None, on_reconcile=None, on_node_offline=None
        )
        self.dispatch = SimpleNamespace(register=lambda *_a: None)
        self.commands: list[tuple[str, str, Any]] = []
        self.lives = lives or []

    def defer_ack(self, _p: str) -> None:
        return None

    def snapshot(self) -> list[Any]:
        return list(self.lives)

    def send_command(
        self, node_id: str, name: str, payload: Any, **_kw: Any
    ) -> str | None:
        self.commands.append((node_id, name, payload))
        return str(new_command_id())


class FakeStore:
    def __init__(self, entries: list[Any]) -> None:
        self.entries = entries

    async def live_attempts(self, _node_id: str | None = None) -> list[Any]:
        return self.entries


class FakeCache:
    def __init__(self, rows: list[tuple[str, float]], fail: bool = False) -> None:
        self.rows = rows
        self.fail = fail
        self.asked: list[tuple[str, str, list[str]]] = []

    async def holders(
        self, kind: str, key: str, *, among: Any
    ) -> list[tuple[str, float]]:
        self.asked.append((kind, key, sorted(among)))
        if self.fail:
            raise OSError("database down")
        return [r for r in self.rows if r[0] in among]


def _live(
    node_id: str,
    *,
    endpoint: str = "10.0.0.2:47120",
    state: NodeState = NodeState.ONLINE,
) -> Any:
    return SimpleNamespace(
        node_id=node_id,
        state=state,
        ended=False,
        peer_endpoint=endpoint,
        session_id=f"sess_{node_id}",
    )


def _all_live(_node_id: str, _session_id: str) -> bool:
    return True


def _job(*, tree: bool) -> Any:
    return SimpleNamespace(
        id=str(new_job_id()),
        manifest={
            "name": "j",
            "entrypoint": ["python", "m.py"],
            "limits": {"timeout": 60},
        },
        bundle_digest=None if tree else BUNDLE,
        tree_id=TREE if tree else None,
        cancel_requested_at=None,
        split=None,
    )


def _attempt(job: Any, node_id: str, state: AttemptState) -> Any:
    return SimpleNamespace(
        id=str(new_attempt_id()),
        job_id=job.id,
        node_id=node_id,
        fencing_token=7,
        role=AttemptRole.RUN,  # V0.10 G4: never inferred
        state=state,
        start_acked_at=object(),
        start_snapshot={},
    )


def _issuer() -> tuple[PeerTokenIssuer, bytes]:
    issuer = PeerTokenIssuer(Ed25519PrivateKey.generate(), clock=lambda: NOW)
    return issuer, issuer.public_key


def _dispatcher(
    entries: list[Any], manager: FakeManager, peers: PeerSources | None = None
) -> tuple[Dispatcher, FetchTokens]:
    tokens = FetchTokens(clock=lambda: 100.0)
    dispatcher = Dispatcher(
        store=FakeStore(entries),  # type: ignore[arg-type]
        manager=manager,  # type: ignore[arg-type]
        fetch_tokens=tokens,
        settings=JobApiSettings(),
        clock=lambda: 0.0,
        blob_port=lambda: 8080,
        peers=peers,
    )
    return dispatcher, tokens


async def test_fetch_scope_is_explicit() -> None:
    assert fetch_scope(_job(tree=True)) == [Digest.parse(TREE)]
    assert fetch_scope(_job(tree=False)) == [Digest.parse(BUNDLE)]
    neither = _job(tree=False)
    neither.bundle_digest = None
    with pytest.raises(ValueError, match="neither"):
        fetch_scope(neither)


@pytest.mark.parametrize("tree", [True, False])
async def test_refresh_scope_equals_start_scope(tree: bool) -> None:
    node = str(new_node_id())
    job = _job(tree=tree)
    attempt = _attempt(job, node, AttemptState.STAGING)
    entry = SimpleNamespace(attempt=attempt, job=job)
    manager = FakeManager()
    dispatcher, tokens = _dispatcher([entry], manager)
    await dispatcher._send_start(job, attempt, resend=False)
    (_n, name, start) = manager.commands[-1]
    assert name == "start_attempt"
    start_grant = tokens.grant(start.spec.fetch_token)
    assert start_grant is not None
    # Explicit tree path: no reuse of bundle_digest for the manifest.
    assert start.spec.tree_id == (TREE if tree else "")
    assert start.spec.bundle_digest == ("" if tree else BUNDLE)
    tokens.revoke(attempt.id)  # "the controller restarted"
    live = SimpleNamespace(node_id=node, session_id="sess_x", ended=False)
    await dispatcher._handle(_Reconcile(live, {attempt.id: 7}))  # type: ignore[arg-type]
    (_n, name, refresh) = manager.commands[-1]
    assert name == "refresh_fetch_token"
    refresh_grant = tokens.grant(refresh.fetch_token)
    assert refresh_grant is not None
    assert refresh_grant.digests == start_grant.digests
    assert refresh_grant.attempt_id == start_grant.attempt_id == attempt.id
    if tree:
        assert [s.node_id for s in refresh.sources] == [""]
        assert refresh.sources[0].token == refresh.fetch_token
    else:
        assert list(refresh.sources) == []


async def test_tree_sources_controller_first_then_peers_and_refresh_reminted() -> None:
    target = str(new_node_id())
    holder_a, holder_b = str(new_node_id()), str(new_node_id())
    manager = FakeManager(
        [_live(target), _live(holder_a), _live(holder_b, endpoint="10.0.0.3:47120")]
    )
    issuer, raw = _issuer()
    cache = FakeCache([(holder_a, 10.0), (holder_b, 20.0), (target, 30.0)])
    peers = PeerSources(manager, cache, issuer, max_sources=4, index_live=_all_live)  # type: ignore[arg-type]
    job = _job(tree=True)
    attempt = _attempt(job, target, AttemptState.ASSIGNED)
    dispatcher, _tokens = _dispatcher(
        [SimpleNamespace(attempt=attempt, job=job)], manager, peers
    )
    await dispatcher._send_start(job, attempt, resend=False)
    spec = manager.commands[-1][2].spec
    assert [s.node_id for s in spec.sources] == ["", holder_b, holder_a]  # newest first
    assert spec.sources[0].token == spec.fetch_token
    assert spec.sources[1].url == "https://10.0.0.3:47120"  # V0.7 G3: TLS peers
    key = load_public_key(raw)
    claims = verify_peer_token(key, spec.sources[1].token, holder=holder_b, now=NOW)
    assert str(claims.tree) == TREE and claims.requester == target
    assert claims.attempt_id == attempt.id
    assert target not in cache.asked[0][2]  # never the target itself


async def test_peer_sources_filters_and_bounds() -> None:
    target = str(new_node_id())
    online = [str(new_node_id()) for _ in range(6)]
    suspect, no_endpoint = str(new_node_id()), str(new_node_id())
    lives = [_live(n) for n in online]
    lives.append(_live(suspect, state=NodeState.SUSPECT))
    lives.append(_live(no_endpoint, endpoint=""))
    rows = [(n, float(i)) for i, n in enumerate([*online, suspect, no_endpoint])]
    issuer, _raw = _issuer()
    cache = FakeCache(rows)
    peers = PeerSources(
        FakeManager(lives), cache, issuer, max_sources=4, index_live=_all_live
    )  # type: ignore[arg-type]
    job = _job(tree=True)
    listed = await peers.for_attempt(job, _attempt(job, target, AttemptState.ASSIGNED))
    assert [s.node_id for s in listed] == list(reversed(online))[:4]
    assert suspect not in cache.asked[0][2] and no_endpoint not in cache.asked[0][2]
    # A bundle job, max 0, nobody online: no lookup at all.
    none = PeerSources(
        FakeManager(lives), cache, issuer, max_sources=0, index_live=_all_live
    )  # type: ignore[arg-type]
    assert (
        await none.for_attempt(job, _attempt(job, target, AttemptState.ASSIGNED)) == []
    )
    bundle = _job(tree=False)
    assert (
        await peers.for_attempt(bundle, _attempt(bundle, target, AttemptState.ASSIGNED))
        == []
    )
    assert len(cache.asked) == 1


async def test_peer_lookup_failure_falls_back_to_the_controller() -> None:
    target, holder = str(new_node_id()), str(new_node_id())
    manager = FakeManager([_live(holder)])
    issuer, _raw = _issuer()
    peers = PeerSources(
        manager,
        FakeCache([(holder, 1.0)], fail=True),  # type: ignore[arg-type]
        issuer,
        max_sources=4,
        index_live=_all_live,
    )
    job = _job(tree=True)
    attempt = _attempt(job, target, AttemptState.ASSIGNED)
    dispatcher, _tokens = _dispatcher([], manager, peers)
    await dispatcher._send_start(job, attempt, resend=False)
    spec = manager.commands[-1][2].spec
    assert [s.node_id for s in spec.sources] == [""]
    assert peers.lookup_failures == 1


async def test_only_nodes_with_a_live_index_are_listed() -> None:
    """G4 reviewer carry-over 1: rows of a node whose index is not live for its
    current session (stale after a gap, or not yet re-snapshotted after a
    controller restart) never make it a peer source."""
    target = str(new_node_id())
    live_node, stale_node, old_session = (str(new_node_id()) for _ in range(3))
    lives = [_live(live_node), _live(stale_node), _live(old_session)]
    index = CacheIndex(None)  # type: ignore[arg-type]
    for live in lives:
        index._states[live.node_id] = NodeCacheState(live.session_id, Phase.LIVE)
    index._states[stale_node].phase = Phase.STALE
    index._states[old_session].session_id = "sess_before_reconnect"
    cache = FakeCache([(n, 1.0) for n in (live_node, stale_node, old_session)])
    issuer, _raw = _issuer()
    peers = PeerSources(
        FakeManager(lives),
        cache,  # type: ignore[arg-type]
        issuer,
        max_sources=4,
        index_live=index.is_live,
    )
    job = _job(tree=True)
    listed = await peers.for_attempt(job, _attempt(job, target, AttemptState.ASSIGNED))
    assert [s.node_id for s in listed] == [live_node]
    assert cache.asked[0][2] == [live_node]
    # A controller restart: no state at all -> nobody is listed, no lookup.
    fresh = PeerSources(
        FakeManager(lives),
        cache,  # type: ignore[arg-type]
        issuer,
        max_sources=4,
        index_live=CacheIndex(None).is_live,  # type: ignore[arg-type]
    )
    assert (
        await fresh.for_attempt(job, _attempt(job, target, AttemptState.ASSIGNED)) == []
    )
    assert len(cache.asked) == 1
