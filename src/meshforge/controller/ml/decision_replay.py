"""Re-plan one stored decision and say whether it reproduces (V0.15 G6a; the
G5 debt "``replay_decision`` has no production caller"; D364, Q23,
ADR-0020 "replayable planner").

``GET /v1/planner/decisions/{id}/replay`` and ``compute planner replay ID``
call :meth:`DecisionReplayer.replay`:

1. read the ``plan_decision`` row (its replay, policy, stored candidates,
   snapshot digest, chosen node);
2. in a worker thread, :func:`~.replay.replay_decision` with the replay's own
   calibration block and - for a decision planned with an ML model set -
   **that** set, loaded by version through the
   :class:`~.registry_loader.RegistryLoader` (``blocking``: the loader runs
   on the controller's event loop, the planning in the thread);
3. compare (:func:`compare`): the re-planned candidates as the dispatcher
   stores them (:func:`~meshforge.controller.planner.decision_json.
   candidates_json`) against the stored JSON, field for field, and the
   snapshot digest.

Statuses (:data:`REPLAY_STATUSES`):

* ``reproduced`` - the same digest and the same candidate rows, byte for
  byte after a JSON round trip;
* ``differs`` - re-planned, but something differs (the first
  :data:`MAX_DIFFERENCES` differing paths are listed);
* ``ml_model_unavailable`` - the decision used a model set that can no
  longer be loaded (gone, unreadable artefact, other feature version):
  **not** re-planned with calibration instead (Q23 - that would claim to
  reproduce a decision it does not);
* ``no_replay`` - the row stored no replay (before V0.14, or too large);
* ``replay_unreadable`` - the stored replay cannot be decoded.

A database failure raises :class:`DecisionReplayError` (retryable).
:func:`compare` and :func:`replay_stored` are pure.
"""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from meshforge.common.errors import ExitCode, FailureType, MeshForgeError
from meshforge.controller.ml.registry import MlRegistryError
from meshforge.controller.ml.registry_loader import RegistryLoader
from meshforge.controller.ml.replay import (
    ML_MODEL_UNAVAILABLE,
    ReplayResult,
    replay_decision,
)
from meshforge.controller.planner.decision_json import candidates_json
from meshforge.controller.planner.predictor import MlInput
from meshforge.controller.planner.replay import ReplayError, ml_from_replay
from meshforge.controller.store.engine import create_session_factory
from meshforge.controller.store.models.plan import PlanDecision

__all__ = [
    "DIFFERS",
    "MAX_DIFFERENCES",
    "NO_REPLAY",
    "REPLAY_STATUSES",
    "REPLAY_UNREADABLE",
    "REPRODUCED",
    "DecisionReplay",
    "DecisionReplayError",
    "DecisionReplayer",
    "StoredDecision",
    "compare",
    "json_differences",
    "read_decision",
    "replay_stored",
]

REPRODUCED: Final = "reproduced"
DIFFERS: Final = "differs"
NO_REPLAY: Final = "no_replay"
REPLAY_UNREADABLE: Final = "replay_unreadable"
REPLAY_STATUSES: Final = (
    REPRODUCED,
    DIFFERS,
    ML_MODEL_UNAVAILABLE,
    NO_REPLAY,
    REPLAY_UNREADABLE,
)
MAX_DIFFERENCES: Final = 20
"""Differing paths listed at most (the first ones, in document order)."""
_MAX_VALUE_CHARS: Final = 80
_DB_ERRORS: Final = (SQLAlchemyError, OSError, TimeoutError)


class DecisionReplayError(MeshForgeError):
    """The database refused or lost the read (retryable)."""

    default_failure_type = FailureType.NETWORK_ERROR
    exit_code = ExitCode.UNAVAILABLE


@dataclass(frozen=True, slots=True)
class StoredDecision:
    """One ``plan_decision`` row, as far as a replay needs it."""

    decision_id: int
    job_id: str
    outcome: str
    policy: str
    chosen_node_id: str | None
    snapshot_digest: str
    candidates: Sequence[Mapping[str, Any]]
    replay: Mapping[str, Any] | None


@dataclass(frozen=True, slots=True)
class DecisionReplay:
    """What one replay found (module docstring)."""

    decision_id: int
    job_id: str
    outcome: str
    policy: str
    status: str
    """One of :data:`REPLAY_STATUSES`."""
    ml_model_version: str | None
    """The model set the stored decision was planned with (None = none)."""
    stored_chosen_node_id: str | None
    replayed_chosen_node_id: str | None
    snapshot_digest_match: bool | None
    """None = not re-planned."""
    differences: tuple[str, ...] = ()
    """``path: stored != replayed`` (at most :data:`MAX_DIFFERENCES`)."""
    differences_total: int = 0


# ------------------------------------------------------------------ pure


def _normal(value: Any) -> Any:
    """``value`` as JSONB gives it back (tuples -> lists, keys -> str); a
    non-finite float (never stored, D131) stays itself."""
    return json.loads(json.dumps(value, allow_nan=True))


def _short(value: Any) -> str:
    text = json.dumps(value, sort_keys=True, allow_nan=True)
    return (
        text if len(text) <= _MAX_VALUE_CHARS else text[: _MAX_VALUE_CHARS - 3] + "..."
    )


def _same_number(a: Any, b: Any) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    if isinstance(a, int | float) and isinstance(b, int | float):
        if isinstance(a, float) and isinstance(b, float) and math.isnan(a):
            return math.isnan(b)
        return a == b
    return False


def json_differences(stored: Any, replayed: Any, path: str = "") -> list[str]:
    """Every path where two JSON documents differ, in document order (dict
    keys sorted). Numbers compare by value (``1`` == ``1.0``: JSONB keeps
    no int/float distinction), everything else exactly."""
    out: list[str] = []
    if isinstance(stored, Mapping) and isinstance(replayed, Mapping):
        for key in sorted(set(stored) | set(replayed), key=str):
            where = f"{path}.{key}" if path else str(key)
            if key not in stored:
                out.append(f"{where}: missing != {_short(replayed[key])}")
            elif key not in replayed:
                out.append(f"{where}: {_short(stored[key])} != missing")
            else:
                out.extend(json_differences(stored[key], replayed[key], where))
        return out
    if isinstance(stored, list) and isinstance(replayed, list):
        for index in range(max(len(stored), len(replayed))):
            where = f"{path}[{index}]"
            if index >= len(stored):
                out.append(f"{where}: missing != {_short(replayed[index])}")
            elif index >= len(replayed):
                out.append(f"{where}: {_short(stored[index])} != missing")
            else:
                out.extend(json_differences(stored[index], replayed[index], where))
        return out
    if stored == replayed and type(stored) is type(replayed):
        return out
    if _same_number(stored, replayed):
        return out
    out.append(f"{path or '<root>'}: {_short(stored)} != {_short(replayed)}")
    return out


def _version(stored: StoredDecision) -> str | None:
    if stored.replay is None:
        return None
    try:
        used = ml_from_replay(stored.replay)
    except ReplayError:
        return None
    return None if used is None else used.version


def _unplanned(
    stored: StoredDecision, status: str, ml_model_version: str | None
) -> DecisionReplay:
    return DecisionReplay(
        decision_id=stored.decision_id,
        job_id=stored.job_id,
        outcome=stored.outcome,
        policy=stored.policy,
        status=status,
        ml_model_version=ml_model_version,
        stored_chosen_node_id=stored.chosen_node_id,
        replayed_chosen_node_id=None,
        snapshot_digest_match=None,
    )


def compare(stored: StoredDecision, result: ReplayResult) -> DecisionReplay:
    """The verdict on one re-planned decision (module docstring)."""
    decision = result.decision
    if decision is None:
        return _unplanned(stored, result.status, result.ml_model_version)
    found = json_differences(
        {"candidates": _normal(list(stored.candidates))},
        {"candidates": _normal(candidates_json(decision))},
    )
    digest_same = decision.snapshot_digest == stored.snapshot_digest
    if not digest_same:
        found.insert(
            0,
            f"snapshot_digest: {stored.snapshot_digest} != {decision.snapshot_digest}",
        )
    return DecisionReplay(
        decision_id=stored.decision_id,
        job_id=stored.job_id,
        outcome=stored.outcome,
        policy=stored.policy,
        status=REPRODUCED if not found else DIFFERS,
        ml_model_version=result.ml_model_version,
        stored_chosen_node_id=stored.chosen_node_id,
        replayed_chosen_node_id=None
        if decision.chosen is None
        else decision.chosen.node_id,
        snapshot_digest_match=digest_same,
        differences=tuple(found[:MAX_DIFFERENCES]),
        differences_total=len(found),
    )


def replay_stored(
    stored: StoredDecision, load: Callable[[str], MlInput | None]
) -> DecisionReplay:
    """Re-plan ``stored`` (``load`` = a model set by version, None when it
    cannot be loaded) and compare (module docstring)."""
    if stored.replay is None:
        return _unplanned(stored, NO_REPLAY, None)
    try:
        result = replay_decision(stored.replay, stored.policy, load=load)
    except (ReplayError, ValueError, KeyError, TypeError, ArithmeticError):
        return _unplanned(stored, REPLAY_UNREADABLE, _version(stored))
    return compare(stored, result)


# ------------------------------------------------------------------ I/O


async def read_decision(engine: AsyncEngine, decision_id: int) -> StoredDecision | None:
    """The row, or None when there is no decision ``decision_id``.
    :class:`DecisionReplayError` when the database fails."""
    query = select(
        PlanDecision.id,
        PlanDecision.job_id,
        PlanDecision.outcome,
        PlanDecision.policy,
        PlanDecision.chosen_node_id,
        PlanDecision.snapshot_digest,
        PlanDecision.candidates,
        PlanDecision.replay,
    ).where(PlanDecision.id == decision_id)
    try:
        async with create_session_factory(engine)() as db:
            row = (await db.execute(query)).first()
    except _DB_ERRORS as exc:
        raise DecisionReplayError(
            "the decision read failed in the database",
            details={"cause": type(exc).__name__},
        ) from exc
    if row is None:
        return None
    candidates = row[6] if isinstance(row[6], list) else []
    replay = row[7] if isinstance(row[7], Mapping) else None
    return StoredDecision(
        decision_id=int(row[0]),
        job_id=str(row[1]),
        outcome=str(row[2]),
        policy=str(row[3]),
        chosen_node_id=None if row[4] is None else str(row[4]),
        snapshot_digest=str(row[5]),
        candidates=candidates,
        replay=replay,
    )


class DecisionReplayer:
    """The production caller of :func:`~.replay.replay_decision` (module
    docstring). One per controller."""

    def __init__(self, engine: AsyncEngine, loader: RegistryLoader) -> None:
        self._engine = engine
        self._loader = loader

    async def replay(self, decision_id: int) -> DecisionReplay | None:
        """None = no such decision. :class:`DecisionReplayError` when the
        database fails (the row read or a model set read)."""
        stored = await read_decision(self._engine, decision_id)
        if stored is None:
            return None
        load = self._loader.blocking(asyncio.get_running_loop())
        try:
            return await asyncio.to_thread(replay_stored, stored, load)
        except MlRegistryError as exc:
            raise DecisionReplayError(
                "the model set read failed in the database",
                details={"cause": exc.details.get("cause")},
            ) from exc
