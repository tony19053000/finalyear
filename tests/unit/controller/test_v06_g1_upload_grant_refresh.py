"""V0.6 G1 / D114, controller side, without a database: after a controller
restart a held attempt gets a fresh upload grant (``RefreshUploadGrant``).

* any held live attempt (ASSIGNED .. UPLOADING) with no live grant in this
  process gets one, with the StartAttempt's quota, its fencing token and where
  this controller serves blobs now; the grant uploads for that attempt only;
* a held attempt that still has a live grant gets nothing;
* no live session: nothing counted, the next Reconcile retries;
* a refresh sent but never acked (the stream broke) is re-minted and re-sent on
  the next Reconcile; once acked it is not - and only the Ack of THAT message
  clears it;
* an attempt whose cancel is pending gets the cancel, not a grant.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from meshforge.common.hashing import sha256_bytes
from meshforge.common.ids import new_attempt_id, new_command_id, new_job_id, new_node_id
from meshforge.common.states import AttemptState
from meshforge.controller.blobs import FetchTokens
from meshforge.controller.jobs.dispatcher import Dispatcher, _Reconcile
from meshforge.controller.session.live import PendingCommand
from meshforge.controller.settings import JobApiSettings
from meshforge.controller.upload_grants import GrantRefusal, UploadGrants

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

DIGEST = "sha256:" + "ab" * 32
NAME = "refresh_upload_grant"
MIB = 1024 * 1024


class FakeManager:
    def __init__(self) -> None:
        self.hooks = SimpleNamespace(
            on_command_acked=None, on_reconcile=None, on_node_offline=None
        )
        self.dispatch = SimpleNamespace(register=lambda *_a: None)
        self.commands: list[tuple[str, str, Any, str]] = []
        self.connected = True

    def defer_ack(self, _p: str) -> None:
        return None

    def send_command(
        self, node_id: str, name: str, payload: Any, **_kw: Any
    ) -> str | None:
        if not self.connected:
            return None
        message_id = str(new_command_id())
        self.commands.append((node_id, name, payload, message_id))
        return message_id

    def named(self, name: str) -> list[tuple[str, str, Any, str]]:
        return [c for c in self.commands if c[1] == name]


class FakeStore:
    def __init__(self, entries: list[Any]) -> None:
        self.entries = entries

    async def live_attempts(self, _node_id: str | None = None) -> list[Any]:
        return self.entries


def _entry(node_id: str, state: AttemptState, *, cancel: bool = False) -> Any:
    job = SimpleNamespace(
        id=str(new_job_id()),
        manifest={"limits": {"timeout": 60}},
        bundle_digest=DIGEST,
        tree_id=None,
        cancel_requested_at=object() if cancel else None,
    )
    attempt = SimpleNamespace(
        id=str(new_attempt_id()),
        job_id=job.id,
        node_id=node_id,
        fencing_token=7,
        state=state,
        start_acked_at=object(),
    )
    return SimpleNamespace(attempt=attempt, job=job)


def _dispatcher(
    entries: list[Any], **settings: Any
) -> tuple[Dispatcher, FakeManager, UploadGrants]:
    manager = FakeManager()
    grants = UploadGrants(clock=lambda: 100.0)
    dispatcher = Dispatcher(
        store=FakeStore(entries),  # type: ignore[arg-type]
        manager=manager,  # type: ignore[arg-type]
        fetch_tokens=FetchTokens(clock=lambda: 100.0),
        settings=JobApiSettings(**settings),
        upload_grants=grants,
        clock=lambda: 0.0,
        blob_port=lambda: 8471,
        blob_url="http://10.0.0.5:8471",
    )
    return dispatcher, manager, grants


def _reconcile(node_id: str, entries: list[Any]) -> _Reconcile:
    live = SimpleNamespace(node_id=node_id, session_id="sess_x", ended=False)
    held = {e.attempt.id: e.attempt.fencing_token for e in entries}
    return _Reconcile(live, held)  # type: ignore[arg-type]


async def _ack(dispatcher: Dispatcher, node: str, entry: Any, message_id: str) -> None:
    live = SimpleNamespace(node_id=node, session_id="sess_x")
    pending = PendingCommand(
        payload=NAME, attempt_id=entry.attempt.id, fencing_token=7
    )
    dispatcher._on_command_acked(live, message_id, pending)  # type: ignore[arg-type]
    await dispatcher._handle(dispatcher._queue.get_nowait())


@pytest.mark.parametrize(
    "state",
    [
        AttemptState.ASSIGNED,
        AttemptState.STAGING,
        AttemptState.ENV_READY,
        AttemptState.RUNNING,
        AttemptState.UPLOADING,
    ],
)
async def test_a_held_attempt_without_a_grant_gets_a_fresh_one(
    state: AttemptState,
) -> None:
    node = str(new_node_id())
    entry = _entry(node, state)
    dispatcher, manager, grants = _dispatcher(
        [entry], max_output_bytes=MIB, max_output_blobs=3
    )
    await dispatcher._handle(_reconcile(node, [entry]))
    ((to, _, payload, _),) = manager.named(NAME)
    assert to == node
    assert payload.attempt_id == entry.attempt.id and payload.fencing_token == 7
    assert payload.blob_url == "http://10.0.0.5:8471" and payload.blob_port == 8471
    assert grants.has(entry.attempt.id)
    assert grants.check(payload.upload_token) == entry.attempt.id
    # the StartAttempt's quota: MIB bytes accepted, MIB + 1 refused
    ok = grants.begin(payload.upload_token, sha256_bytes(b"a"), MIB)
    grants.finish(ok, stored=False)
    with pytest.raises(GrantRefusal) as refused:
        grants.begin(payload.upload_token, sha256_bytes(b"a"), MIB + 1)
    assert refused.value.reason == "byte_quota"
    assert dispatcher.stats.upload_grants_refreshed == 1


async def test_a_live_grant_in_this_process_means_no_refresh() -> None:
    node = str(new_node_id())
    entry = _entry(node, AttemptState.UPLOADING)
    dispatcher, manager, grants = _dispatcher([entry])
    grants.mint(entry.attempt.id, ttl_s=60, max_blobs=3, max_bytes=100)
    await dispatcher._handle(_reconcile(node, [entry]))
    assert manager.named(NAME) == []


async def test_an_expired_or_closed_grant_is_refreshed() -> None:
    node = str(new_node_id())
    entry = _entry(node, AttemptState.UPLOADING)
    dispatcher, manager, grants = _dispatcher([entry])
    grants.mint(entry.attempt.id, ttl_s=60, max_blobs=3, max_bytes=100)
    grants.close(entry.attempt.id, grace_s=30)  # closed: takes no new upload
    await dispatcher._handle(_reconcile(node, [entry]))
    assert len(manager.named(NAME)) == 1


async def test_no_live_session_counts_nothing_and_the_next_reconcile_retries() -> None:
    node = str(new_node_id())
    entry = _entry(node, AttemptState.UPLOADING)
    dispatcher, manager, grants = _dispatcher([entry])
    manager.connected = False
    await dispatcher._handle(_reconcile(node, [entry]))
    assert dispatcher.stats.upload_grants_refreshed == 0
    assert not grants.has(entry.attempt.id)  # minted, not sent: dropped
    manager.connected = True
    await dispatcher._handle(_reconcile(node, [entry]))
    assert len(manager.named(NAME)) == 1


async def test_an_unacked_refresh_is_sent_again_and_an_acked_one_is_not() -> None:
    node = str(new_node_id())
    entry = _entry(node, AttemptState.UPLOADING)
    dispatcher, manager, grants = _dispatcher([entry])
    await dispatcher._handle(_reconcile(node, [entry]))
    ((_, _, first, first_id),) = manager.named(NAME)
    # the stream broke before the agent got it: the next Reconcile re-mints
    await dispatcher._handle(_reconcile(node, [entry]))
    assert len(manager.named(NAME)) == 2
    _, _, second, second_id = manager.named(NAME)[1]
    assert second.upload_token != first.upload_token
    with pytest.raises(GrantRefusal):  # one live grant per attempt
        grants.check(first.upload_token)
    # a late Ack of the FIRST message does not clear the second
    await _ack(dispatcher, node, entry, first_id)
    await dispatcher._handle(_reconcile(node, [entry]))
    assert len(manager.named(NAME)) == 3
    _, _, _third, third_id = manager.named(NAME)[2]
    await _ack(dispatcher, node, entry, third_id)
    await dispatcher._handle(_reconcile(node, [entry]))
    assert len(manager.named(NAME)) == 3
    assert dispatcher._grant_refresh_unacked == {}
    assert second_id != third_id


async def test_an_ended_attempt_forgets_its_unacked_refresh() -> None:
    node = str(new_node_id())
    entry = _entry(node, AttemptState.UPLOADING)
    dispatcher, _manager, _ = _dispatcher([entry])
    await dispatcher._handle(_reconcile(node, [entry]))
    assert entry.attempt.id in dispatcher._grant_refresh_unacked
    dispatcher._end_tokens(entry.attempt.id)
    assert dispatcher._grant_refresh_unacked == {}


async def test_a_cancel_pending_attempt_gets_no_grant() -> None:
    node = str(new_node_id())
    cancelled = _entry(node, AttemptState.RUNNING, cancel=True)
    dispatcher, manager, _ = _dispatcher([cancelled])
    await dispatcher._handle(_reconcile(node, [cancelled]))
    assert manager.named(NAME) == []
    assert [c[1] for c in manager.commands] == ["cancel_attempt"]
