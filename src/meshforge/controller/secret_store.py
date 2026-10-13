"""Job secrets in PostgreSQL (V0.7 G6, roadmap 0.7.10, D144).

:class:`SecretStore` is the only code that reads or writes ``secret`` rows:

* :meth:`SecretStore.put` seals the value (:class:`~.secret_box.SecretBox`,
  AES-256-GCM bound to owner + name) and upserts it; an ``audit_event``
  ``secret_set`` row (name only) is written in the same transaction;
* :meth:`SecretStore.list` returns names and timestamps - **never** a value or
  ciphertext; :meth:`SecretStore.delete` removes one (audit ``secret_deleted``);
* :meth:`SecretStore.resolve` opens the named secrets of **one owner** for a
  job's start (deliverable 3/4): a name the owner does not have is reported
  missing, never looked up for anyone else.

The master key is loaded (or created) on first use, off the event loop. A
database failure is :class:`SecretStoreError` (``network_error``, retryable);
a key problem is :class:`~.secret_box.SecretKeyError` (``config_error``).
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from sqlalchemy import delete, func, literal_column, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from meshforge.common.errors import ExitCode, FailureType, MeshForgeError
from meshforge.common.logging import get_logger
from meshforge.common.secret_names import check_secret_value, normalise_secret_name
from meshforge.controller.enrollment.invites import audit
from meshforge.controller.secret_box import SealedSecret, SecretBox
from meshforge.controller.store.engine import create_session_factory
from meshforge.controller.store.models.enrollment import AuditAction
from meshforge.controller.store.models.secret import GCM_TAG_BYTES, Secret

__all__ = [
    "MAX_SECRETS_PER_USER",
    "SecretInfo",
    "SecretLimitError",
    "SecretNotFoundError",
    "SecretStore",
    "SecretStoreError",
    "SecretsMissingError",
    "SecretsTooLargeError",
    "secret_names",
]

log = get_logger(__name__)

MAX_SECRETS_PER_USER: Final = 256
"""Most secrets one user may store (bounds a list and a job's resolve)."""
_OWNER_LOCK: Final = text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))")
"""Serialises one owner's ``put`` calls until their transaction ends."""


class SecretStoreError(MeshForgeError):
    """The database refused or lost a secret read or write (retryable)."""

    default_failure_type = FailureType.NETWORK_ERROR
    exit_code = ExitCode.UNAVAILABLE


class SecretNotFoundError(MeshForgeError):
    default_failure_type = FailureType.NOT_FOUND


class SecretLimitError(MeshForgeError):
    default_failure_type = FailureType.RESOURCE_BUSY


class SecretsMissingError(MeshForgeError):
    """A job names secrets its owner does not have (``secret_missing:<NAME>``)."""

    default_failure_type = FailureType.INVALID_INPUT

    def __init__(self, names: Iterable[str]) -> None:
        self.names = tuple(sorted(names))
        super().__init__(
            ", ".join(f"secret_missing:{name}" for name in self.names),
            details={"missing": list(self.names)},
        )


class SecretsTooLargeError(MeshForgeError):
    """A job's secrets together exceed ``[controller.jobs]
    max_job_secret_bytes`` (D162; ``secret_bytes_over_limit``)."""

    default_failure_type = FailureType.INVALID_INPUT

    def __init__(self, total_bytes: int, max_bytes: int) -> None:
        self.total_bytes = total_bytes
        self.max_bytes = max_bytes
        super().__init__(
            f"secret_bytes_over_limit: the job's secrets total {total_bytes} "
            f"bytes; at most {max_bytes} per job "
            "([controller.jobs] max_job_secret_bytes)",
            details={"total_bytes": total_bytes, "max_bytes": max_bytes},
        )


def secret_names(manifest: Mapping[str, Any]) -> list[str]:
    """The secret names a stored (canonical) manifest asks for (V0.7 G6);
    malformed entries are ignored here - the manifest was validated at submit."""
    entries = manifest.get("secrets")
    if not isinstance(entries, list):
        return []
    names: list[str] = []
    for entry in entries:
        name = entry.get("name") if isinstance(entry, Mapping) else entry
        if isinstance(name, str):
            names.append(name)
    return names


@dataclass(frozen=True, slots=True)
class SecretInfo:
    """What a listing shows: never the value."""

    name: str
    created_at: dt.datetime
    updated_at: dt.datetime


def _check_total(total_bytes: int, max_total_bytes: int | None) -> None:
    if max_total_bytes is not None and total_bytes > max_total_bytes:
        raise SecretsTooLargeError(total_bytes, max_total_bytes)


class SecretStore:
    """Secrets of every user (module docstring)."""

    def __init__(self, engine: AsyncEngine, key_path: Path) -> None:
        self._sessions = create_session_factory(engine)
        self._key_path = key_path
        self._box: SecretBox | None = None
        self._box_lock = asyncio.Lock()

    async def box(self) -> SecretBox:
        """The master key's box, loaded (or created) once, off the loop."""
        if self._box is None:
            async with self._box_lock:
                if self._box is None:
                    self._box = await asyncio.to_thread(SecretBox.at, self._key_path)
        return self._box

    async def put(
        self,
        owner_user_id: str,
        name: str,
        value: bytes,
        *,
        actor: str,
        source: str | None = None,
    ) -> bool:
        """Store or replace ``name``; True when it was new."""
        name = normalise_secret_name(name)
        value = check_secret_value(value)
        sealed = (await self.box()).seal(value, owner_user_id=owner_user_id, name=name)
        try:
            async with self._sessions() as db, db.begin():
                # D162 (T-1): one writer per owner at a time, in the database
                # (a transaction-scoped advisory lock): the count below and
                # the insert are then one step for every controller process,
                # so the limit is exact and `created` is true exactly once.
                await db.execute(
                    _OWNER_LOCK, {"key": f"meshforge.secret:{owner_user_id}"}
                )
                exists = await db.scalar(
                    select(func.count())
                    .select_from(Secret)
                    .where(Secret.owner_user_id == owner_user_id, Secret.name == name)
                )
                if not exists:
                    count = await db.scalar(
                        select(func.count())
                        .select_from(Secret)
                        .where(Secret.owner_user_id == owner_user_id)
                    )
                    if (count or 0) >= MAX_SECRETS_PER_USER:
                        raise SecretLimitError(
                            f"at most {MAX_SECRETS_PER_USER} secrets per user"
                        )
                statement = pg_insert(Secret).values(
                    owner_user_id=owner_user_id,
                    name=name,
                    ciphertext=sealed.ciphertext,
                    nonce=sealed.nonce,
                    key_id=sealed.key_id,
                )
                # `xmax = 0` only for a row this statement inserted (PostgreSQL):
                # the database, not the read above, says whether it was new.
                created = bool(
                    await db.scalar(
                        statement.on_conflict_do_update(
                            index_elements=[Secret.owner_user_id, Secret.name],
                            set_={
                                "ciphertext": statement.excluded.ciphertext,
                                "nonce": statement.excluded.nonce,
                                "key_id": statement.excluded.key_id,
                                "updated_at": func.now(),
                            },
                        ).returning(literal_column("(xmax = 0)"))
                    )
                )
                await audit(
                    db,
                    AuditAction.SECRET_SET,
                    actor=actor,
                    source=source,
                    details={"owner_user_id": owner_user_id, "name": name},
                )
        except SQLAlchemyError as exc:
            raise SecretStoreError(
                f"cannot store the secret: {type(exc).__name__}"
            ) from None
        log.info("controller.secrets.set", owner_user_id=owner_user_id, name=name)
        return created

    async def list(self, owner_user_id: str) -> list[SecretInfo]:
        try:
            async with self._sessions() as db:
                rows = await db.execute(
                    select(Secret.name, Secret.created_at, Secret.updated_at)
                    .where(Secret.owner_user_id == owner_user_id)
                    .order_by(Secret.name)
                    .limit(MAX_SECRETS_PER_USER)
                )
                return [SecretInfo(n, c, u) for n, c, u in rows.all()]
        except SQLAlchemyError as exc:
            raise SecretStoreError(
                f"cannot list secrets: {type(exc).__name__}"
            ) from None

    async def delete(
        self,
        owner_user_id: str,
        name: str,
        *,
        actor: str,
        source: str | None = None,
    ) -> None:
        name = normalise_secret_name(name)
        try:
            async with self._sessions() as db, db.begin():
                result = await db.execute(
                    delete(Secret)
                    .where(Secret.owner_user_id == owner_user_id, Secret.name == name)
                    .returning(Secret.name)
                )
                if result.scalar_one_or_none() is None:
                    raise SecretNotFoundError(f"no secret {name}")
                await audit(
                    db,
                    AuditAction.SECRET_DELETED,
                    actor=actor,
                    source=source,
                    details={"owner_user_id": owner_user_id, "name": name},
                )
        except SQLAlchemyError as exc:
            raise SecretStoreError(
                f"cannot delete the secret: {type(exc).__name__}"
            ) from None
        log.info("controller.secrets.deleted", owner_user_id=owner_user_id, name=name)

    async def check_present(
        self,
        owner_user_id: str | None,
        names: Iterable[str],
        *,
        max_total_bytes: int | None = None,
    ) -> None:
        """Submit-time check (no decryption): :class:`SecretsMissingError`
        unless ``owner_user_id`` has every one of ``names``; then
        :class:`SecretsTooLargeError` if their values together exceed
        ``max_total_bytes`` (D162; sizes from the ciphertext lengths)."""
        wanted = sorted({normalise_secret_name(n) for n in names})
        if not wanted:
            return
        if owner_user_id is None:
            raise SecretsMissingError(wanted)
        try:
            async with self._sessions() as db:
                rows = await db.execute(
                    select(Secret.name, func.octet_length(Secret.ciphertext)).where(
                        Secret.owner_user_id == owner_user_id,
                        Secret.name.in_(wanted),
                    )
                )
                found = {name: int(size) - GCM_TAG_BYTES for name, size in rows.all()}
        except SQLAlchemyError as exc:
            raise SecretStoreError(
                f"cannot read secrets: {type(exc).__name__}"
            ) from None
        missing = [n for n in wanted if n not in found]
        if missing:
            raise SecretsMissingError(missing)
        _check_total(sum(found.values()), max_total_bytes)

    async def resolve(
        self,
        owner_user_id: str | None,
        names: Iterable[str],
        *,
        max_total_bytes: int | None = None,
    ) -> dict[str, bytes]:
        """The values of ``names`` for ``owner_user_id`` (a job's owner).
        Raises :class:`SecretsMissingError` naming every absent one (a job
        without an owner has none), :class:`SecretsTooLargeError` past
        ``max_total_bytes`` (D162; before anything is decrypted).
        Integrity/key failures raise typed."""
        wanted = sorted({normalise_secret_name(n) for n in names})
        if not wanted:
            return {}
        if owner_user_id is None:
            raise SecretsMissingError(wanted)
        try:
            async with self._sessions() as db:
                rows = (
                    await db.execute(
                        select(Secret).where(
                            Secret.owner_user_id == owner_user_id,
                            Secret.name.in_(wanted),
                        )
                    )
                ).scalars()
                found: Mapping[str, Secret] = {row.name: row for row in rows}
        except SQLAlchemyError as exc:
            raise SecretStoreError(
                f"cannot read secrets: {type(exc).__name__}"
            ) from None
        missing = [n for n in wanted if n not in found]
        if missing:
            raise SecretsMissingError(missing)
        _check_total(
            sum(len(row.ciphertext) - GCM_TAG_BYTES for row in found.values()),
            max_total_bytes,
        )
        box = await self.box()
        return {
            name: box.open(
                SealedSecret(row.ciphertext, row.nonce, row.key_id),
                owner_user_id=owner_user_id,
                name=name,
            )
            for name, row in found.items()
        }
