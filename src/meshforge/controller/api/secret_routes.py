"""``/v1/secrets``: a user's job secrets (V0.7 G6, roadmap 0.7.10, D144).

* ``PUT /v1/secrets/{name}`` - body = the raw value (any bytes,
  ``application/octet-stream``, 4 B .. 64 KiB) -> ``{name, created}``. The body
  is read with a hard cap and never logged or echoed; a bad name is refused
  without echoing it.
* ``GET /v1/secrets`` - the caller's secrets: names and timestamps only.
* ``DELETE /v1/secrets/{name}`` - remove one; unknown -> 404.

Every route acts on **the authenticated user's own** secrets: there is no
admin view of another user's secrets and **no endpoint ever returns a value**
(values leave the controller only inside ``StartAttempt`` over mTLS). The
legacy fabric token has no user: 400 ``legacy_token``.
"""

from __future__ import annotations

from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Request

from meshforge.common.errors import FailureType
from meshforge.common.secret_names import (
    MAX_SECRET_BYTES,
    SecretNameError,
    SecretValueError,
    normalise_secret_name,
)
from meshforge.controller.api.auth import CurrentUser, request_source
from meshforge.controller.api.errors import ApiError
from meshforge.controller.auth.principal import Principal
from meshforge.controller.secret_box import SecretKeyError
from meshforge.controller.secret_store import (
    SecretLimitError,
    SecretNotFoundError,
    SecretStore,
    SecretStoreError,
)

__all__ = ["SECRETS_STATE_KEY", "secret_router"]

SECRETS_STATE_KEY: Final = "meshforge_secrets"
_MAX_PATH_NAME_CHARS: Final = 256

secret_router = APIRouter(prefix="/v1/secrets", tags=["secrets"])


def get_store(request: Request) -> SecretStore:
    store: SecretStore | None = getattr(request.app.state, SECRETS_STATE_KEY, None)
    if store is None:
        raise ApiError(FailureType.CONFIG_ERROR, "secrets are not configured")
    return store


Store = Annotated[SecretStore, Depends(get_store)]


def _owner(user: Principal) -> str:
    if user.user_id is None:
        raise ApiError(
            FailureType.INVALID_INPUT,
            "the legacy fabric token has no user and no secrets; run `compute login`",
            extra={"reason": "legacy_token"},
        )
    return user.user_id


_BAD_NAME: Final = (
    "a secret name is 1-64 characters of A-Z, 0-9 and _ (ASCII), not starting "
    "with a digit"
)


def _name(raw: str) -> str:
    if len(raw) > _MAX_PATH_NAME_CHARS:
        raise ApiError.invalid(_BAD_NAME)
    try:
        return normalise_secret_name(raw)
    except SecretNameError:
        raise ApiError.invalid(_BAD_NAME) from None


async def _value(request: Request) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None and (
        not declared.isdigit() or int(declared) > MAX_SECRET_BYTES
    ):
        raise ApiError.invalid(f"a secret value is at most {MAX_SECRET_BYTES} bytes")
    data = bytearray()
    async for chunk in request.stream():
        data += chunk
        if len(data) > MAX_SECRET_BYTES:
            raise ApiError.invalid(
                f"a secret value is at most {MAX_SECRET_BYTES} bytes"
            )
    return bytes(data)


def _unavailable() -> ApiError:
    return ApiError(
        FailureType.NETWORK_ERROR,
        "the secret store is unavailable; retry",
        headers={"Retry-After": "2"},
    )


def _key_problem() -> ApiError:
    # The message names a file path on the controller: log it, never return it.
    return ApiError(
        FailureType.CONFIG_ERROR,
        "the controller's secrets master key is unusable (see its log)",
    )


@secret_router.put("/{name}", summary="Store or replace one of your secrets")
async def put_secret(
    name: str, request: Request, user: CurrentUser, store: Store
) -> dict[str, Any]:
    owner = _owner(user)
    checked = _name(name)
    value = await _value(request)
    try:
        created = await store.put(
            owner,
            checked,
            value,
            actor=user.name,
            source=request_source(request),
        )
    except SecretValueError as exc:
        raise ApiError.invalid(exc.message) from None
    except SecretLimitError as exc:
        raise ApiError(FailureType.CONFLICT, exc.message) from None
    except SecretStoreError:
        raise _unavailable() from None
    except SecretKeyError:
        raise _key_problem() from None
    return {"name": checked, "created": created}


@secret_router.get("", summary="Your secrets: names and timestamps, never values")
async def list_secrets(user: CurrentUser, store: Store) -> dict[str, Any]:
    owner = _owner(user)
    try:
        items = await store.list(owner)
    except SecretStoreError:
        raise _unavailable() from None
    return {
        "secrets": [
            {
                "name": item.name,
                "created_at": item.created_at.isoformat(),
                "updated_at": item.updated_at.isoformat(),
            }
            for item in items
        ],
        "count": len(items),
    }


@secret_router.delete("/{name}", summary="Delete one of your secrets")
async def delete_secret(
    name: str, request: Request, user: CurrentUser, store: Store
) -> dict[str, Any]:
    owner = _owner(user)
    checked = _name(name)
    try:
        await store.delete(
            owner, checked, actor=user.name, source=request_source(request)
        )
    except SecretNotFoundError:
        raise ApiError.not_found("secret") from None
    except SecretStoreError:
        raise _unavailable() from None
    return {"name": checked, "deleted": True}
