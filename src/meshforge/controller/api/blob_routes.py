"""``/v1/blobs/{sha256}`` - upload, check and download blobs (brief D48).

``{sha256}`` is the 64-character lower-case hex digest (the ``sha256:`` prefix is
implied). Every call needs a credential (D44/D48): the fabric token, or a
per-attempt fetch token that names this digest (``GET``/``HEAD``) or allows
uploads (``PUT``). See :mod:`meshforge.controller.blobs` for the storage rules.

* ``PUT`` streams the body to disk while hashing it; ``201`` when stored, ``200``
  when it already existed (idempotent), ``400 integrity_error`` when the bytes do
  not hash to the name (nothing kept), ``413`` over the size cap (refused from
  ``Content-Length`` before reading when the client sends one), ``507`` disk full.
* ``HEAD`` answers "do you have it" (``200`` + ``Content-Length`` / ``404``).
* ``GET`` streams the file.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Path, Request, Response, status
from fastapi.responses import FileResponse
from starlette.requests import ClientDisconnect

from meshforge.common.errors import FailureType
from meshforge.common.hashing import Digest
from meshforge.common.logging import get_logger
from meshforge.controller.api.auth import (
    ApiAuth,
    bearer_token,
    get_auth,
    unauthorized,
)
from meshforge.controller.api.errors import ApiError
from meshforge.controller.blobs import (
    BlobError,
    BlobIntegrityError,
    BlobStore,
    BlobTooLargeError,
)

__all__ = ["BLOBS_STATE_KEY", "blob_router", "get_blob_store"]

log = get_logger(__name__)

BLOBS_STATE_KEY: Final = "meshforge_blobs"
_HEX_PATTERN: Final = r"^[0-9a-f]{64}$"
_MAX_CONTENT_LENGTH_CHARS: Final = 20

blob_router = APIRouter(prefix="/v1/blobs", tags=["blobs"])

Sha256 = Annotated[
    str,
    Path(
        min_length=64,
        max_length=64,
        pattern=_HEX_PATTERN,
        description="64 lower-case hex characters (the SHA-256 of the content).",
    ),
]


def get_blob_store(request: Request) -> BlobStore:
    store = getattr(request.app.state, BLOBS_STATE_KEY, None)
    if not isinstance(store, BlobStore):
        raise ApiError(FailureType.CONFIG_ERROR, "the blob store is not configured")
    return store


Store = Annotated[BlobStore, Depends(get_blob_store)]
Auth = Annotated[ApiAuth, Depends(get_auth)]


def _authorize(request: Request, auth: ApiAuth, digest: Digest, *, upload: bool) -> str:
    """Return which credential was used (``fabric``/``fetch``) or raise 401."""
    token = bearer_token(request)
    if auth.verifier is not None and auth.verifier.verify(token):
        return "fabric"
    if auth.fetch_tokens is not None and auth.fetch_tokens.check(
        token, digest, upload=upload
    ):
        return "fetch"
    raise unauthorized()


def _declared_size(request: Request) -> int | None:
    raw = request.headers.get("content-length")
    if raw is None:
        return None
    if len(raw) > _MAX_CONTENT_LENGTH_CHARS or not raw.isdigit():
        raise ApiError.invalid("Content-Length is not a non-negative integer")
    return int(raw)


@blob_router.put(
    "/{sha256}",
    status_code=status.HTTP_201_CREATED,
    summary="Upload a blob (idempotent; verified before it is kept)",
)
async def put_blob(
    request: Request, sha256: Sha256, store: Store, auth: Auth, response: Response
) -> dict[str, object]:
    digest = Digest(sha256)
    credential = _authorize(request, auth, digest, upload=True)
    declared = _declared_size(request)

    async def body() -> AsyncIterator[bytes]:
        async for chunk in request.stream():
            yield chunk

    try:
        result = await store.put(digest, body(), declared_size=declared)
    except BlobTooLargeError as exc:
        raise ApiError(
            FailureType.INVALID_INPUT,
            exc.message,
            http_status=status.HTTP_413_CONTENT_TOO_LARGE,
        ) from None
    except BlobIntegrityError:
        log.warning("controller.blobs.integrity_error", digest=str(digest))
        raise ApiError(
            FailureType.INTEGRITY_ERROR,
            f"the uploaded bytes do not hash to {digest}; nothing was stored",
        ) from None
    except ClientDisconnect:
        log.info("controller.blobs.upload_aborted", digest=str(digest))
        raise ApiError(
            FailureType.TRANSFER_ERROR,
            "the upload was interrupted; nothing was stored",
            http_status=status.HTTP_400_BAD_REQUEST,
        ) from None
    except BlobError as exc:
        raise ApiError(
            exc.failure_type,
            exc.message,
            http_status=status.HTTP_507_INSUFFICIENT_STORAGE
            if exc.failure_type is FailureType.DISK_FULL
            else status.HTTP_400_BAD_REQUEST,
        ) from None
    if not result.created:
        response.status_code = status.HTTP_200_OK
    log.debug(
        "controller.blobs.put",
        digest=str(digest),
        size=result.size,
        created=result.created,
        credential=credential,
    )
    return {"digest": str(digest), "size": result.size, "created": result.created}


@blob_router.head("/{sha256}", summary="Does the controller hold this blob?")
async def head_blob(
    request: Request, sha256: Sha256, store: Store, auth: Auth
) -> Response:
    digest = Digest(sha256)
    _authorize(request, auth, digest, upload=False)
    size = store.size(digest)
    if size is None:
        return Response(status_code=status.HTTP_404_NOT_FOUND)
    return Response(
        status_code=status.HTTP_200_OK,
        headers={"content-length": str(size), "etag": f'"{digest}"'},
    )


@blob_router.get("/{sha256}", summary="Download a blob", response_class=FileResponse)
async def get_blob(
    request: Request, sha256: Sha256, store: Store, auth: Auth
) -> Response:
    digest = Digest(sha256)
    _authorize(request, auth, digest, upload=False)
    if store.size(digest) is None:
        raise ApiError.not_found("blob")
    return FileResponse(
        store.path_for(digest),
        media_type="application/octet-stream",
        headers={"etag": f'"{digest}"'},
    )
