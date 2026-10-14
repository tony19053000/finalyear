"""Job and attempt state events, published only after their transaction
commits (V0.8 G1, D172).

The job tables have one writer (:mod:`meshforge.controller.jobs.store` /
``execution_store``) and every state change goes through a few helpers there.
Each helper calls :func:`note_job` / :func:`note_attempt` with the session it
writes on; the notes wait in ``session.info`` and are handed to the engine's
sink by a SQLAlchemy ``after_commit`` hook - so the dashboard never sees a
state the database then rolled back (``after_rollback`` discards them), and a
refused report (``WRONG_NODE``, fenced) publishes nothing.

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
from sqlalchemy.orm import Session

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


def _note(db: AsyncSession, change: Change) -> None:
    sync = db.sync_session
    if not wanted(db):
        return
    notes: list[Change] = sync.info.setdefault(_INFO_KEY, [])
    if len(notes) < _MAX_PER_TRANSACTION:
        notes.append(change)


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


@event.listens_for(Session, "after_commit")
def _after_commit(session: Session) -> None:
    notes: list[Change] | None = session.info.pop(_INFO_KEY, None)
    if not notes:
        return
    engine = _engine(session)
    sink = None if engine is None else _SINKS.get(engine)
    if sink is None:
        return
    try:
        sink(notes)
    except Exception as exc:  # noqa: BLE001 - publishing must never fail a commit
        log.warning("controller.events.publish_failed", cause=type(exc).__name__)


@event.listens_for(Session, "after_rollback")
def _after_rollback(session: Session) -> None:
    session.info.pop(_INFO_KEY, None)
