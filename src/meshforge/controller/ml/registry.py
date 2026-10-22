"""The ML model registry in PostgreSQL + the CAS blob store (V0.15 G3, D339,
D358; tables ``ml_model_set`` / ``ml_model``, migration 0034). I/O.

:class:`ModelRegistry` is the tables' one writer and their reads. The
artefacts live in the controller's CAS blob store, named by their digest
(= the member ``version``): integrity is the name.

* :meth:`~ModelRegistry.store` - one trained set (the ``fit`` child's
  ``set.json`` + artefact bytes): artefacts first into the CAS (verified
  against their names, idempotent), then **one transaction under an
  advisory lock**: a set already stored (same ``set_version``) is not stored
  again (``same_set``); if what serves (pinned, else active) is no longer
  the set the gate judged against, nothing is written (``reference_changed``
  - the next retrain judges against the new one); else the set is inserted
  ``active`` (the active one -> ``superseded``) or ``rejected`` with the
  gate's reason. The partial unique indexes keep **at most one active and
  one pinned** set whatever two writers do.
* :meth:`~ModelRegistry.pin` / :meth:`~ModelRegistry.unpin` - the operator's
  override and rollback (``compute planner ml pin|unpin``): pin refuses a
  ``rejected`` set and one whose artefacts are gone or unreadable; the
  previously pinned set -> ``superseded``. Unpin -> ``active`` when no set is
  active, else ``superseded``.
* :meth:`~ModelRegistry.serving` - pinned, else active (what ``ml = "auto"``
  plans with); :meth:`~ModelRegistry.load` - a set's members as a
  :class:`~.serve.LoadedSet` (artefacts read and decoded in a worker
  thread).

A database that fails raises :class:`MlRegistryError` (``network_error``,
retryable). No row ever changes except a set's ``status`` (+
``status_changed_at``).
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import func, select, text, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from meshforge.common.cas import CasError, CasStore
from meshforge.common.errors import ExitCode, FailureType, MeshForgeError
from meshforge.common.hashing import Digest
from meshforge.common.logging import get_logger
from meshforge.controller.ml.artefact import ArtefactError, digest
from meshforge.controller.ml.serve import LoadedModel, LoadedSet, load_model
from meshforge.controller.store.engine import create_session_factory
from meshforge.controller.store.models.ml import (
    MAX_ML_REASON_CHARS,
    MlModel,
    MlModelSet,
)

__all__ = [
    "MlRegistryError",
    "ModelRegistry",
    "PinError",
    "SetMember",
    "SetRow",
    "StoreResult",
    "protected_artefacts",
]

log = get_logger(__name__)

_LOCK: Final = text(
    "SELECT pg_advisory_xact_lock(hashtextextended('meshforge.ml_model_set', 0))"
)
_DB_ERRORS: Final = (SQLAlchemyError, OSError, TimeoutError)
MAX_RECENT: Final = 200


class MlRegistryError(MeshForgeError):
    """The database refused or lost a registry read/write (retryable)."""

    default_failure_type = FailureType.NETWORK_ERROR
    exit_code = ExitCode.UNAVAILABLE


class PinError(MeshForgeError):
    """A pin / unpin the registry refuses (no such set, rejected, artefacts
    gone, not pinned)."""

    default_failure_type = FailureType.CONFLICT


@dataclass(frozen=True, slots=True)
class SetMember:
    term: str
    version: str
    algorithm: str
    feature_version: int
    params: dict[str, Any]
    metrics: dict[str, Any]
    reason: str | None
    artefact_digest: str | None
    artefact_bytes: int | None

    @property
    def learned(self) -> bool:
        return self.algorithm != "none"


@dataclass(frozen=True, slots=True)
class SetRow:
    set_version: str
    created_at: dt.datetime
    status_changed_at: dt.datetime
    status: str
    reason: str | None
    gate: dict[str, Any]
    dataset_digest: str
    feature_version: int
    inputs: dict[str, Any]
    members: tuple[SetMember, ...]


@dataclass(frozen=True, slots=True)
class StoreResult:
    row: SetRow | None
    skipped: str | None = None
    """``same_set`` | ``reference_changed`` (nothing written)."""
    superseded: str | None = None


def _member(item: MlModel) -> SetMember:
    return SetMember(
        term=item.term,
        version=item.version,
        algorithm=item.algorithm,
        feature_version=int(item.feature_version),
        params=dict(item.params),
        metrics=dict(item.metrics),
        reason=item.reason,
        artefact_digest=item.artefact_digest,
        artefact_bytes=None
        if item.artefact_bytes is None
        else int(item.artefact_bytes),
    )


def _row(item: MlModelSet, members: Sequence[MlModel]) -> SetRow:
    return SetRow(
        set_version=item.set_version,
        created_at=item.created_at,
        status_changed_at=item.status_changed_at,
        status=item.status,
        reason=item.reason,
        gate=dict(item.gate),
        dataset_digest=item.dataset_digest,
        feature_version=int(item.feature_version),
        inputs=dict(item.inputs),
        members=tuple(_member(m) for m in sorted(members, key=lambda m: m.term)),
    )


def _reason(value: str | None) -> str | None:
    return None if value is None else value[:MAX_ML_REASON_CHARS]


class ModelRegistry:
    """See module docstring."""

    def __init__(self, engine: AsyncEngine, cas: CasStore) -> None:
        self._engine = engine
        self._cas = cas
        self._sessions: async_sessionmaker[AsyncSession] = create_session_factory(
            engine
        )

    # ------------------------------------------------------------------ reads

    async def _members(
        self, db: AsyncSession, versions: Sequence[str]
    ) -> dict[str, list[MlModel]]:
        out: dict[str, list[MlModel]] = {v: [] for v in versions}
        if versions:
            for item in await db.scalars(
                select(MlModel).where(MlModel.set_version.in_(list(versions)))
            ):
                out[item.set_version].append(item)
        return out

    async def _rows(self, db: AsyncSession, query: Any) -> list[SetRow]:
        sets = list(await db.scalars(query))
        members = await self._members(db, [s.set_version for s in sets])
        return [_row(s, members[s.set_version]) for s in sets]

    async def _read(self, query: Any) -> list[SetRow]:
        try:
            async with self._sessions() as db:
                return await self._rows(db, query)
        except _DB_ERRORS as exc:
            raise MlRegistryError(
                "the model registry read failed in the database",
                details={"cause": type(exc).__name__},
            ) from exc

    async def get(self, set_version: str) -> SetRow | None:
        rows = await self._read(
            select(MlModelSet).where(MlModelSet.set_version == set_version)
        )
        return rows[0] if rows else None

    async def recent(self, limit: int = 20) -> list[SetRow]:
        return await self._read(
            select(MlModelSet)
            .order_by(MlModelSet.created_at.desc(), MlModelSet.set_version)
            .limit(max(1, min(limit, MAX_RECENT)))
        )

    async def newest(self) -> SetRow | None:
        rows = await self.recent(1)
        return rows[0] if rows else None

    async def serving_version(self) -> tuple[str | None, str | None]:
        """``(kind, set_version)`` of what ``ml = "auto"`` serves: the
        ``pinned`` set, else the ``active`` one, else ``(None, None)``. One
        cheap query (the per-tick read)."""
        try:
            async with self._sessions() as db:
                rows = (
                    await db.execute(
                        select(MlModelSet.status, MlModelSet.set_version).where(
                            MlModelSet.status.in_(("pinned", "active"))
                        )
                    )
                ).all()
        except _DB_ERRORS as exc:
            raise MlRegistryError(
                "the model registry read failed in the database",
                details={"cause": type(exc).__name__},
            ) from exc
        by = {str(status): str(version) for status, version in rows}
        for kind in ("pinned", "active"):
            if kind in by:
                return kind, by[kind]
        return None, None

    async def serving(self) -> tuple[str | None, SetRow | None]:
        kind, version = await self.serving_version()
        if version is None:
            return None, None
        return kind, await self.get(version)

    # ------------------------------------------------------------------ CAS

    def _read_blob(self, artefact_digest: str) -> bytes:
        with self._cas.open_read(Digest.parse(artefact_digest)) as fh:
            return fh.read()

    async def artefact(self, member: SetMember) -> bytes:
        """The member's artefact bytes; :class:`~.artefact.ArtefactError`
        (``artefact_missing``) when the blob is gone."""
        if member.artefact_digest is None:
            raise ArtefactError(
                f"{member.term} has no learned model",
                details={"reason": "no_artefact"},
            )
        try:
            return await asyncio.to_thread(self._read_blob, member.artefact_digest)
        except (CasError, OSError, ValueError):
            raise ArtefactError(
                f"the {member.term} artefact is not in the blob store",
                details={"reason": "artefact_missing", "term": member.term},
            ) from None

    async def load(self, row: SetRow) -> LoadedSet:
        """Every member of ``row`` loaded (:func:`~.serve.load_model` in a
        worker thread); :class:`~.artefact.ArtefactError` if any learned
        member cannot be (a set serves whole or not at all)."""
        models: dict[str, LoadedModel] = {}
        without: dict[str, str] = {}
        for member in row.members:
            if not member.learned:
                without[member.term] = member.reason or "no_learned_model"
                continue
            data = await self.artefact(member)
            models[member.term] = await asyncio.to_thread(
                load_model, data, expected_version=member.version
            )
        return LoadedSet(
            set_version=row.set_version,
            feature_version=row.feature_version,
            models=models,
            without_model=without,
        )

    def _put(self, data: bytes, version: str) -> None:
        self._cas.put_bytes(data, Digest.parse(version))

    # ------------------------------------------------------------------ writes

    async def store(
        self,
        doc: Mapping[str, Any],
        artefacts: Mapping[str, bytes],
        *,
        reference_version: str | None,
        inputs: Mapping[str, Any],
    ) -> StoreResult:
        """Store one trained set (module docstring). ``doc`` = the child's
        ``set.json``; ``artefacts`` = version -> bytes of every learned
        member (each must hash to its version - ``ValueError`` otherwise);
        ``reference_version`` = the set the gate judged against (None =
        the calibration)."""
        members = list(doc["members"])
        for member in members:
            if member["algorithm"] == "none":
                continue
            data = artefacts.get(member["version"])
            if data is None or digest(data) != member["version"]:
                raise ValueError(f"the {member['term']} artefact does not match")
        try:
            for member in members:
                if member["algorithm"] != "none":
                    await asyncio.to_thread(
                        self._put, artefacts[member["version"]], member["version"]
                    )
        except CasError as exc:
            raise MlRegistryError(
                "the model artefacts could not be stored in the blob store",
                details={"cause": type(exc).__name__},
            ) from exc
        accepted = bool(doc.get("accepted"))
        try:
            async with self._sessions() as db, db.begin():
                await db.execute(_LOCK)
                existing = await db.get(MlModelSet, doc["set_version"])
                if existing is not None:
                    return StoreResult(row=None, skipped="same_set")
                serving = (
                    await db.execute(
                        select(MlModelSet.status, MlModelSet.set_version).where(
                            MlModelSet.status.in_(("pinned", "active"))
                        )
                    )
                ).all()
                by = {str(s): str(v) for s, v in serving}
                now_serving = by.get("pinned", by.get("active"))
                if now_serving != reference_version:
                    return StoreResult(row=None, skipped="reference_changed")
                superseded = None
                if accepted and "active" in by:
                    superseded = by["active"]
                    await db.execute(
                        update(MlModelSet)
                        .where(MlModelSet.set_version == superseded)
                        .values(status="superseded", status_changed_at=func.now())
                    )
                item = MlModelSet(
                    set_version=doc["set_version"],
                    status="active" if accepted else "rejected",
                    reason=_reason(doc.get("reason")),
                    gate=dict(doc.get("gate") or {}),
                    dataset_digest=doc["dataset_digest"],
                    feature_version=int(doc["feature_version"]),
                    inputs={**inputs, "settings": doc.get("settings", {})},
                )
                db.add(item)
                await db.flush()
                rows = []
                for member in members:
                    learned = member["algorithm"] != "none"
                    rows.append(
                        MlModel(
                            set_version=doc["set_version"],
                            term=member["term"],
                            version=member["version"],
                            algorithm=member["algorithm"],
                            feature_version=int(doc["feature_version"]),
                            dataset_digest=doc["dataset_digest"],
                            params=dict(member.get("params") or {}),
                            metrics=dict(member.get("metrics") or {}),
                            reason=_reason(member.get("reason")),
                            artefact_digest=member["version"] if learned else None,
                            artefact_bytes=len(artefacts[member["version"]])
                            if learned
                            else None,
                        )
                    )
                db.add_all(rows)
                await db.flush()
                await db.refresh(item)
                return StoreResult(row=_row(item, rows), superseded=superseded)
        except _DB_ERRORS as exc:
            raise MlRegistryError(
                "the model set could not be stored in the database",
                details={"cause": type(exc).__name__},
            ) from exc

    async def _check_loadable(self, row: SetRow) -> None:
        try:
            await self.load(row)
        except ArtefactError as exc:
            raise PinError(
                f"model set {row.set_version} cannot be loaded "
                f"({exc.details.get('reason')}); it cannot be pinned",
                details={"reason": exc.details.get("reason")},
            ) from None

    async def pin(self, set_version: str) -> SetRow:
        """Pin ``set_version`` (module docstring); :class:`PinError` when
        refused."""
        row = await self.get(set_version)
        if row is None:
            raise PinError(
                f"no model set {set_version}", failure_type=FailureType.NOT_FOUND
            )
        if row.status == "rejected":
            raise PinError(
                f"model set {set_version} was rejected by the activation gate "
                f"({row.reason}); pin an active or superseded set"
            )
        await self._check_loadable(row)
        try:
            async with self._sessions() as db, db.begin():
                await db.execute(_LOCK)
                current = await db.get(MlModelSet, set_version)
                if current is None or current.status == "rejected":
                    raise PinError(f"model set {set_version} cannot be pinned")
                if current.status != "pinned":
                    await db.execute(
                        update(MlModelSet)
                        .where(MlModelSet.status == "pinned")
                        .values(status="superseded", status_changed_at=func.now())
                    )
                    current.status = "pinned"
                    current.status_changed_at = func.now()
        except _DB_ERRORS as exc:
            raise MlRegistryError(
                "the pin failed in the database",
                details={"cause": type(exc).__name__},
            ) from exc
        pinned = await self.get(set_version)
        assert pinned is not None  # noqa: S101 - just written, rows never deleted
        return pinned

    async def unpin(self, set_version: str) -> SetRow:
        """Unpin ``set_version`` (module docstring)."""
        try:
            async with self._sessions() as db, db.begin():
                await db.execute(_LOCK)
                current = await db.get(MlModelSet, set_version)
                if current is None:
                    raise PinError(
                        f"no model set {set_version}",
                        failure_type=FailureType.NOT_FOUND,
                    )
                if current.status != "pinned":
                    raise PinError(f"model set {set_version} is not pinned")
                active = await db.scalar(
                    select(func.count())
                    .select_from(MlModelSet)
                    .where(MlModelSet.status == "active")
                )
                current.status = "superseded" if active else "active"
                current.status_changed_at = func.now()
        except _DB_ERRORS as exc:
            raise MlRegistryError(
                "the unpin failed in the database",
                details={"cause": type(exc).__name__},
            ) from exc
        row = await self.get(set_version)
        assert row is not None  # noqa: S101 - just written, rows never deleted
        return row


async def protected_artefacts(db: AsyncSession, keep_superseded: int) -> set[str]:
    """Artefact hex digests the blob GC must keep (:mod:`meshforge.controller.
    gc`): every member of the active and pinned sets and of the
    ``keep_superseded`` newest superseded sets (the rollback window). A
    rejected set's artefacts may be collected (a rejected set is never
    loaded); so may those of older superseded sets (pinning one then fails
    with ``artefact_missing``)."""
    keep = list(
        await db.scalars(
            select(MlModelSet.set_version).where(
                MlModelSet.status.in_(("active", "pinned"))
            )
        )
    )
    if keep_superseded > 0:
        keep += list(
            await db.scalars(
                select(MlModelSet.set_version)
                .where(MlModelSet.status == "superseded")
                .order_by(MlModelSet.status_changed_at.desc(), MlModelSet.set_version)
                .limit(keep_superseded)
            )
        )
    if not keep:
        return set()
    digests = await db.scalars(
        select(MlModel.artefact_digest).where(
            MlModel.set_version.in_(keep), MlModel.artefact_digest.is_not(None)
        )
    )
    return {d.split(":", 1)[1] for d in digests if d}
