"""Job and attempt state events, published only after their transaction
commits (V0.8 G1, D172).

The job tables have one writer (:mod:`meshforge.controller.jobs.store` /
``execution_store``) and every state change goes through a few helpers there.
Each helper calls :func:`note_job` / :func:`note_attempt` with the session it
writes on; the notes wait in ``session.info`` and are handed to the engine's
sink only when the OUTERMOST transaction commits - so the dashboard never sees
a state the database then rolled back, and a refused report (``WRONG_NODE``,
fenced) publishes nothing.

Savepoints (``begin_nested``, e.g. ``execution_store._record_decision``):
SQLAlchemy fires ``after_commit`` when a SAVEPOINT is *released* and
``after_rollback`` when one is rolled back, so each note remembers the
savepoint it was made in (its "frame"; ``None`` = the outer transaction).
Releasing a savepoint hands its notes to the enclosing frame; rolling one back
(or ending it any other way) discards only its own notes; the outer commit
publishes what is left; the outer rollback or close discards everything.
A note made with no transaction open begins the root transaction (as
autobegin would on the next statement), so ``rollback()`` discards it (T-5).

The sink is registered per engine (:func:`attach`): tests and tools that use
the stores without a bus publish nothing and pay nothing.
"""

from __future__ import annotations

import weakref
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlalchemy.orm import Session, SessionTransaction

from meshforge.common.logging import get_logger

__all__ = ["Change", "attach", "detach", "note_attempt", "note_job", "wanted"]

log = get_logger(__name__)

_INFO_KEY: Final = "meshforge_changes"
_MAX_PER_TRANSACTION: Final = 10_000


@dataclass(frozen=True, slots=True)
class Change:
    kind: str
    """``job`` or ``attempt``."""
    data: dict[str, Any]
    owner_user_id: str | None


Sink = Callable[[list[Change]], None]
_SINKS: weakref.WeakKeyDictionary[Engine, Sink] = weakref.WeakKeyDictionary()


def attach(engine: AsyncEngine, sink: Sink) -> None:
    """Publish the committed changes of sessions on ``engine`` to ``sink``."""
    _SINKS[engine.sync_engine] = sink


def detach(engine: AsyncEngine) -> None:
    _SINKS.pop(engine.sync_engine, None)


def _engine(session: Session) -> Engine | None:
    bind = session.get_bind()
    return bind if isinstance(bind, Engine) else getattr(bind, "engine", None)


def wanted(db: AsyncSession) -> bool:
    """Does anyone listen to this session's engine? (Skip the work if not.)"""
    engine = _engine(db.sync_session)
    return engine is not None and engine in _SINKS


_Frame = SessionTransaction | None
"""The savepoint a note was made in; ``None`` = the outermost transaction."""
_Notes = list[tuple[_Frame, Change]]


def _note(db: AsyncSession, change: Change) -> None:
    sync = db.sync_session
    if not wanted(db):
        return
    if not sync.in_transaction():
        # T-5 (D184): a note must belong to a transaction. With none open,
        # ``rollback()`` is a no-op (no ``after_rollback``, no
        # ``after_transaction_end``), so the note would leak into whatever
        # transaction commits next. Begin the root transaction now - exactly
        # what autobegin does on the next statement (no I/O: the connection
        # is taken lazily) - so the note lives and dies with it.
        sync.begin()
    notes: _Notes = sync.info.setdefault(_INFO_KEY, [])
    if len(notes) < _MAX_PER_TRANSACTION:
        notes.append((sync.get_nested_transaction(), change))


def note_job(
    db: AsyncSession, job_id: str, state: str, *, owner_user_id: str | None
) -> None:
    _note(db, Change("job", {"job_id": job_id, "state": state}, owner_user_id))


def note_attempt(
    db: AsyncSession,
    *,
    attempt_id: str,
    job_id: str,
    node_id: str | None,
    state: str,
    owner_user_id: str | None,
) -> None:
    _note(
        db,
        Change(
            "attempt",
            {
                "attempt_id": attempt_id,
                "job_id": job_id,
                "node_id": node_id,
                "state": state,
            },
            owner_user_id,
        ),
    )


def _enclosing_frame(transaction: SessionTransaction) -> _Frame:
    """The savepoint (or ``None`` = outermost) that encloses ``transaction``."""
    parent = transaction.parent
    while parent is not None and not parent.nested:
        parent = parent.parent
    return parent


def _drop_frame(session: Session, frame: SessionTransaction) -> None:
    notes: _Notes | None = session.info.get(_INFO_KEY)
    if notes:
        notes[:] = [(f, c) for f, c in notes if f is not frame]


@event.listens_for(Session, "after_commit")
def _after_commit(session: Session) -> None:
    released = session.get_nested_transaction()
    if released is not None:
        # A SAVEPOINT was released, not a commit: its notes now belong to the
        # enclosing frame and wait for the outermost commit (T-3).
        notes: _Notes | None = session.info.get(_INFO_KEY)
        if notes:
            parent = _enclosing_frame(released)
            notes[:] = [(parent if f is released else f, c) for f, c in notes]
        return
    _publish(session, session.info.pop(_INFO_KEY, None))


def _publish(session: Session, notes: _Notes | None) -> None:
    if not notes:
        return
    engine = _engine(session)
    sink = None if engine is None else _SINKS.get(engine)
    if sink is None:
        return
    try:
        sink([change for _, change in notes])
    except Exception as exc:  # noqa: BLE001 - publishing must never fail a commit
        log.warning("controller.events.publish_failed", cause=type(exc).__name__)


@event.listens_for(Session, "after_rollback")
def _after_rollback(session: Session) -> None:
    rolled_back = session.get_nested_transaction()
    if rolled_back is not None:
        _drop_frame(session, rolled_back)  # only this savepoint's notes (T-2)
    else:
        session.info.pop(_INFO_KEY, None)


@event.listens_for(Session, "after_transaction_end")
def _after_transaction_end(session: Session, transaction: SessionTransaction) -> None:
    if transaction.nested:
        # Released savepoints moved their notes on in after_commit; anything
        # still in this frame was never released (rolled back or closed).
        _drop_frame(session, transaction)
    elif transaction.parent is None:
        # The outermost transaction ended: published already if it committed;
        # otherwise (rollback, close without commit) nothing may leak into the
        # session's next transaction.
        session.info.pop(_INFO_KEY, None)
