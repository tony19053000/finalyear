"""Who is calling the REST API (V0.7 G4, D141): the result of authentication.

A :class:`Principal` is either a **user** (an ``api_token`` row whose user is
enabled) or - only with ``[controller.auth] legacy_fabric_token = true`` - the
**legacy fabric token**, which acts as an admin with no user id (D141: one
release of overlap, removed in V0.8). Authorization reads only this object:
:meth:`Principal.may_see_job` is the one owner rule every job route applies.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from meshforge.controller.store.models.auth import UserRole

__all__ = ["LEGACY_ACTOR", "LEGACY_PRINCIPAL", "Principal"]

LEGACY_ACTOR: Final = "rest:fabric-token"
"""The audit ``actor`` of a call made with the legacy fabric token (as in G2/G3)."""


@dataclass(frozen=True, slots=True)
class Principal:
    """An authenticated REST caller. Never holds the token itself."""

    user_id: str | None
    """``usr_...``; ``None`` only for the legacy fabric token."""
    name: str
    role: UserRole
    token_id: str | None = None
    """The ``api_token`` the call used (``tok_...``)."""
    legacy: bool = False

    @property
    def is_admin(self) -> bool:
        return self.role is UserRole.ADMIN

    @property
    def actor(self) -> str:
        """The audit ``actor`` string: ``user:<id>`` or the legacy marker."""
        return LEGACY_ACTOR if self.user_id is None else f"user:{self.user_id}"

    def may_see_job(self, owner_user_id: str | None) -> bool:
        """D141: an admin sees every job, a user only the jobs they own (a job
        with no owner - before migration 0018, or legacy-token - is admin-only)."""
        if self.is_admin:
            return True
        return owner_user_id is not None and owner_user_id == self.user_id


LEGACY_PRINCIPAL: Final = Principal(
    user_id=None, name="fabric-token", role=UserRole.ADMIN, legacy=True
)
