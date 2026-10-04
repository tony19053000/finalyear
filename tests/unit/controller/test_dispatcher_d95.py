"""V0.5 D95, controller side, without a database: after a controller restart
a held attempt that may still be fetching gets a fresh fetch token.

* a held ASSIGNED/STAGING attempt with no live token in this process is sent
  ``RefreshFetchToken`` with a new token scoped like the original (the job's
  bundle; a tree job's scope is covered by tests/unit/controller/
  test_dispatcher_g4.py);
* the old token stays refused (it was never persisted, and a refresh revokes
  whatever the attempt held);
* a held attempt that still has a live token, or is past fetching
  (ENV_READY / RUNNING / UPLOADING), gets nothing.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from meshforge.common.hashing import Digest
from meshforge.common.ids import new_attempt_id, new_command_id, new_job_id, new_node_id
from meshforge.common.states import AttemptState
from meshforge.controller.blobs import FetchTokens
from meshforge.controller.jobs.dispatcher import Dispatcher, _Reconcile
from meshforge.controller.settings import JobApiSettings

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

DIGEST = "sha256:" + "ab" * 32
OTHER = Digest("cd" * 32)


class FakeManager:
    def __init__(self) -> None:
        self.hooks = SimpleNamespace(
            on_command_acked=None, on_reconcile=None, on_node_offline=None
        )
        self.dispatch = SimpleNamespace(register=lambda *_a: None)
        self.commands: list[tuple[str, str, Any]] = []
        self.connected = True

    def defer_ack(self, _p: str) -> None:
        return None

    def send_command(
        self, node_id: str, name: str, payload: Any, **_kw: Any
    ) -> str | None:
        if not self.connected:
            return None
        self.commands.append((node_id, name, payload))
        return str(new_command_id())


class FakeStore:
    def __init__(self, entries: list[Any]) -> None:
        self.entries = entries

    async def live_attempts(self, _node_id: str | None = None) -> list[Any]:
        return self.entries


def _entry(node_id: str, state: AttemptState) -> Any:
    job = SimpleNamespace(
        id=str(new_job_id()),
        manifest={"limits": {"timeout": 60}},
        bundle_digest=DIGEST,
        tree_id=None,
        cancel_requested_at=None,
    )
    attempt = SimpleNamespace(
        id=str(new_attempt_id()),
        job_id=job.id,
        node_id=node_id,
        fencing_token=4,
        state=state,
        start_acked_at=object(),
    )
    return SimpleNamespace(attempt=attempt, job=job)


def _dispatcher(entries: list[Any]) -> tuple[Dispatcher, FakeManager, FetchTokens]:
    manager = FakeManager()
    tokens = FetchTokens(clock=lambda: 100.0)
    dispatcher = Dispatcher(
        store=FakeStore(entries),  # type: ignore[arg-type]
        manager=manager,  # type: ignore[arg-type]
        fetch_tokens=tokens,
        settings=JobApiSettings(),
        clock=lambda: 0.0,
        blob_port=lambda: 1,
    )
    return dispatcher, manager, tokens


def _reconcile(node_id: str, entries: list[Any]) -> _Reconcile:
    live = SimpleNamespace(node_id=node_id, session_id="sess_x", ended=False)
    held = {e.attempt.id: e.attempt.fencing_token for e in entries}
    return _Reconcile(live, held)  # type: ignore[arg-type]


@pytest.mark.parametrize("state", [AttemptState.ASSIGNED, AttemptState.STAGING])
async def test_a_held_fetching_attempt_without_a_token_gets_a_fresh_one(
    state: AttemptState,
) -> None:
    node = str(new_node_id())
    entry = _entry(node, state)
    dispatcher, manager, tokens = _dispatcher([entry])
    await dispatcher._handle(_reconcile(node, [entry]))
    ((to, name, payload),) = manager.commands
    assert to == node and name == "refresh_fetch_token"
    assert payload.attempt_id == entry.attempt.id and payload.fencing_token == 4
    assert payload.blob_port == 1  # where this controller serves blobs now
    assert tokens.check(payload.fetch_token, Digest.parse(DIGEST))
    assert not tokens.check(payload.fetch_token, OTHER)  # same scope, no wider
    grant = tokens.grant(payload.fetch_token)
    assert grant is not None and grant.attempt_id == entry.attempt.id
    assert dispatcher.stats.fetch_tokens_refreshed == 1
    # a second Reconcile (another blip) while that token lives: nothing new
    await dispatcher._handle(_reconcile(node, [entry]))
    assert len(manager.commands) == 1


async def test_a_refresh_revokes_any_older_token_of_the_attempt() -> None:
    node = str(new_node_id())
    entry = _entry(node, AttemptState.STAGING)
    dispatcher, manager, tokens = _dispatcher([entry])
    old = tokens.mint("att_old_process", [Digest.parse(DIGEST)], ttl_s=60)
    # a token minted by "the old process" is simply unknown to this one
    fresh = FetchTokens(clock=lambda: 100.0)
    assert not fresh.check(old.get_secret_value(), Digest.parse(DIGEST))
    stale = tokens.mint(entry.attempt.id, [Digest.parse(DIGEST)], ttl_s=1)
    tokens.expire_after(entry.attempt.id, 0)  # expired: has() is False
    await dispatcher._handle(_reconcile(node, [entry]))
    ((_, _, payload),) = manager.commands
    assert not tokens.check(stale.get_secret_value(), Digest.parse(DIGEST))
    assert tokens.check(payload.fetch_token, Digest.parse(DIGEST))


@pytest.mark.parametrize(
    "state", [AttemptState.ENV_READY, AttemptState.RUNNING, AttemptState.UPLOADING]
)
async def test_an_attempt_past_fetching_gets_no_token(state: AttemptState) -> None:
    node = str(new_node_id())
    entry = _entry(node, state)
    dispatcher, manager, _ = _dispatcher([entry])
    await dispatcher._handle(_reconcile(node, [entry]))
    assert manager.commands == []


async def test_a_live_token_in_this_process_means_no_refresh() -> None:
    node = str(new_node_id())
    entry = _entry(node, AttemptState.STAGING)
    dispatcher, manager, tokens = _dispatcher([entry])
    tokens.mint(entry.attempt.id, [Digest.parse(DIGEST)], ttl_s=60)
    await dispatcher._handle(_reconcile(node, [entry]))
    assert manager.commands == []


async def test_no_live_session_counts_nothing_and_the_next_reconcile_retries() -> None:
    node = str(new_node_id())
    entry = _entry(node, AttemptState.STAGING)
    dispatcher, manager, _ = _dispatcher([entry])
    manager.connected = False
    await dispatcher._handle(_reconcile(node, [entry]))
    assert dispatcher.stats.fetch_tokens_refreshed == 0
    manager.connected = True
    await dispatcher._handle(_reconcile(node, [entry]))
    ((_, name, _),) = manager.commands
    assert name == "refresh_fetch_token"
