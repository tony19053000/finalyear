"""V0.4 G5 D75 (and D73/D74 digest checks), controller side, without a database.

* a fenced / unknown report is **acked** and answered ``CancelAttempt{release}``
  (terminal or not - the controller is done with that attempt);
* a Reconcile entry the controller does not consider live gets the same release;
* a report whose handler fails with a non-database error is retried until
  ``POISON_LIMIT`` failures of that ``(attempt_id, state)``, then acked, once;
* a database error (``JobStoreError``) is never counted as poison.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from meshforge.common.ids import new_attempt_id, new_command_id, new_node_id
from meshforge.common.states import AttemptState
from meshforge.controller.jobs.dispatcher import (
    POISON_LIMIT,
    Dispatcher,
    _Reconcile,
    _Status,
)
from meshforge.controller.jobs.execution_store import (
    StatusKind,
    StatusOutcome,
    StatusReport,
)
from meshforge.controller.jobs.store import JobStoreError
from meshforge.controller.settings import JobApiSettings

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


class FakeManager:
    def __init__(self) -> None:
        self.hooks = SimpleNamespace(
            on_command_acked=None, on_reconcile=None, on_node_offline=None
        )
        self.dispatch = SimpleNamespace(register=lambda *_a: None)
        self.commands: list[tuple[str, str, Any]] = []
        self.acked: list[str] = []

    def defer_ack(self, _p: str) -> None:
        return None

    def send_command(self, node_id: str, name: str, payload: Any, **_kw: Any) -> str:
        self.commands.append((node_id, name, payload))
        return str(new_command_id())

    def ack_message(self, _live: Any, message_id: str) -> bool:
        self.acked.append(message_id)
        return True


class FakeStore:
    def __init__(self) -> None:
        self.outcome = StatusOutcome(StatusKind.FENCED)
        self.raises: BaseException | None = None
        self.calls = 0

    async def apply_status(self, _report: StatusReport) -> StatusOutcome:
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return self.outcome

    async def live_attempts(self, _node_id: str | None = None) -> list[Any]:
        return []

    async def get_attempt(self, _attempt_id: str) -> Any:
        """V0.6 G3: the overlap check (D117) looks the attempt up - unknown."""
        return None


def _dispatcher() -> tuple[Dispatcher, FakeManager, FakeStore]:
    manager, store = FakeManager(), FakeStore()
    dispatcher = Dispatcher(
        store=store,  # type: ignore[arg-type]
        manager=manager,  # type: ignore[arg-type]
        fetch_tokens=SimpleNamespace(),  # type: ignore[arg-type]
        settings=JobApiSettings(),
        clock=lambda: 0.0,
        blob_port=lambda: 1,
    )
    return dispatcher, manager, store


def _item(state: AttemptState, attempt_id: str | None = None) -> _Status:
    live = SimpleNamespace(
        node_id=str(new_node_id()), session_id="sess_x", attempt_status_fenced=0
    )
    report = StatusReport(
        attempt_id=attempt_id or str(new_attempt_id()), fencing_token=2, state=state
    )
    return _Status(live, str(new_command_id()), report)  # type: ignore[arg-type]


@pytest.mark.parametrize("kind", [StatusKind.FENCED, StatusKind.UNKNOWN])
@pytest.mark.parametrize("state", [AttemptState.RUNNING, AttemptState.DONE])
async def test_a_fenced_or_unknown_report_is_acked_and_released(
    kind: StatusKind, state: AttemptState
) -> None:
    dispatcher, manager, store = _dispatcher()
    store.outcome = StatusOutcome(kind)
    item = _item(state)
    await dispatcher._handle(item)
    assert manager.acked == [item.message_id]
    ((node, name, payload),) = manager.commands
    assert node == item.live.node_id and name == "cancel_attempt"
    assert payload.attempt_id == item.report.attempt_id
    assert payload.fencing_token == 2 and payload.release is True
    assert dispatcher.stats.releases_sent == 1


async def test_an_applied_report_is_acked_without_a_release() -> None:
    dispatcher, manager, store = _dispatcher()
    store.outcome = StatusOutcome(StatusKind.DUPLICATE)
    item = _item(AttemptState.DONE)
    await dispatcher._handle(item)
    assert manager.acked == [item.message_id] and manager.commands == []


async def test_a_reconcile_entry_not_live_on_the_controller_is_released() -> None:
    dispatcher, manager, _ = _dispatcher()
    live = SimpleNamespace(node_id=str(new_node_id()), session_id="s", ended=False)
    unknown = str(new_attempt_id())
    await dispatcher._handle(_Reconcile(live, {unknown: 3}))  # type: ignore[arg-type]
    ((_, name, payload),) = manager.commands
    assert name == "cancel_attempt" and payload.attempt_id == unknown
    assert payload.fencing_token == 3 and payload.release is True


async def test_a_poison_report_is_acked_after_the_limit_and_only_then() -> None:
    dispatcher, manager, store = _dispatcher()
    store.raises = KeyError("a handler bug")
    attempt_id = str(new_attempt_id())
    for n in range(1, POISON_LIMIT):
        with pytest.raises(KeyError):
            await dispatcher._handle(_item(AttemptState.DONE, attempt_id))
        assert manager.acked == [], n
    # another state of the same attempt is counted on its own
    with pytest.raises(KeyError):
        await dispatcher._handle(_item(AttemptState.RUNNING, attempt_id))
    last = _item(AttemptState.DONE, attempt_id)
    await dispatcher._handle(last)  # the POISON_LIMIT-th failure: acked, swallowed
    assert manager.acked == [last.message_id]
    assert dispatcher.stats.statuses_poisoned == 1
    # the count restarted: one more failure is not acked again
    with pytest.raises(KeyError):
        await dispatcher._handle(_item(AttemptState.DONE, attempt_id))
    assert manager.acked == [last.message_id]


async def test_a_database_error_is_never_poison() -> None:
    dispatcher, manager, store = _dispatcher()
    store.raises = JobStoreError("db down", details={"cause": "OperationalError"})
    attempt_id = str(new_attempt_id())
    for _ in range(POISON_LIMIT + 2):
        with pytest.raises(JobStoreError):
            await dispatcher._handle(_item(AttemptState.DONE, attempt_id))
    assert manager.acked == [] and dispatcher.stats.statuses_poisoned == 0


# ------------------------------------------------------------------ D73/D74


def _grants_dispatcher() -> tuple[Dispatcher, FakeStore, Any]:
    from meshforge.controller.upload_grants import UploadGrants

    manager, store = FakeManager(), FakeStore()
    grants = UploadGrants(clock=lambda: 0.0)
    dispatcher = Dispatcher(
        store=store,  # type: ignore[arg-type]
        manager=manager,  # type: ignore[arg-type]
        fetch_tokens=SimpleNamespace(),  # type: ignore[arg-type]
        settings=JobApiSettings(),
        upload_grants=grants,
        clock=lambda: 0.0,
        blob_port=lambda: 1,
    )
    return dispatcher, store, grants


def _done(attempt_id: str, digest: str | None) -> StatusReport:
    return StatusReport(
        attempt_id=attempt_id,
        fencing_token=1,
        state=AttemptState.DONE,
        exit_code=0,
        output_digest=digest,
        missing_outputs=("nope/",),
    )


async def test_a_done_digest_uploaded_under_the_grant_is_kept() -> None:
    from meshforge.common.hashing import sha256_bytes

    dispatcher, _, grants = _grants_dispatcher()
    attempt_id = str(new_attempt_id())
    token = grants.mint(attempt_id, ttl_s=60, max_blobs=3, max_bytes=100)
    digest = sha256_bytes(b"outputs")
    grants.finish(
        grants.begin(token.get_secret_value(), digest, 7), stored=True, size=7
    )
    live = SimpleNamespace(node_id="n", session_id="s")
    report = dispatcher._verified(_done(attempt_id, str(digest)), live)  # type: ignore[arg-type]
    assert report.state is AttemptState.DONE and report.output_digest == str(digest)


async def test_a_done_digest_not_uploaded_under_the_grant_fails_output_error() -> None:
    from meshforge.common.errors import FailureType
    from meshforge.common.hashing import sha256_bytes

    dispatcher, _, grants = _grants_dispatcher()
    attempt_id, other = str(new_attempt_id()), str(new_attempt_id())
    grants.mint(attempt_id, ttl_s=60, max_blobs=3, max_bytes=100)
    theirs = grants.mint(other, ttl_s=60, max_blobs=3, max_bytes=100)
    digest = sha256_bytes(b"someone else's")
    # uploaded - but under ANOTHER attempt's grant
    grants.finish(
        grants.begin(theirs.get_secret_value(), digest, 5), stored=True, size=5
    )
    live = SimpleNamespace(node_id="n", session_id="s")
    report = dispatcher._verified(_done(attempt_id, str(digest)), live)  # type: ignore[arg-type]
    assert report.state is AttemptState.FAILED
    assert report.failure_type is FailureType.OUTPUT_ERROR
    assert report.output_digest is None
    # no digest at all (nothing declared / nothing matched): DONE stays DONE
    plain = dispatcher._verified(_done(attempt_id, None), live)  # type: ignore[arg-type]
    assert plain.state is AttemptState.DONE
