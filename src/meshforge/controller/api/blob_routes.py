"""``/v1/blobs/{sha256}`` - upload, check and download blobs (brief D48).

``{sha256}`` is the 64-character lower-case hex digest (the ``sha256:`` prefix is
implied). Every call needs a credential (D44/D48/D73): the fabric token; a
per-attempt fetch token that names this digest (``GET``/``HEAD`` only - fetch
tokens never upload); or, for ``PUT`` only, an attempt's upload grant, within
its quota (:mod:`meshforge.controller.upload_grants`: ``401`` unknown/expired,
``403`` the attempt ended, ``411`` no ``Content-Length``, ``413`` over the
blob or byte quota). See :mod:`meshforge.controller.blobs` for the storage rules.

* ``PUT`` streams the body to disk while hashing it; ``201`` when *this request*
  wrote the blob (concurrent identical uploads may each get ``201`` - never a
  uniqueness signal), ``200`` when it was already present as the upload started
  (idempotent), ``400 integrity_error`` when the bytes do
  not hash to the name (nothing kept), ``413`` over the size cap (refused from
  ``Content-Length`` before reading when the client sends one), ``503``
  ``resource_busy`` when every upload slot stayed taken, ``507`` disk full or
  below the free-disk floor (insufficient storage).
* ``HEAD`` answers "do you have it" (``200`` + ``Content-Length`` / ``404``).
* ``GET`` streams the file; a ``Range: bytes=a-b`` request is answered ``206``
  with that slice (resumable, chunked fetches - V0.5 D80).

**V0.5 data plane (D82), fabric token only:**

* ``POST /v1/blobs/have`` ``{"digests": ["sha256:<hex>", ...]}`` (at most
  :data:`~meshforge.controller.blobs.MAX_HAVE_DIGESTS`, body at most 1 MiB) ->
  ``{"missing": [...]}``: upload only those.
* ``PUT /v1/blobs/{sha256}/parts/{index}`` with ``X-MeshForge-Blob-Size``,
  ``X-MeshForge-Chunk-Size`` and ``X-MeshForge-Chunk-Sha256`` (the part's own
  digest, from the tree manifest) and an exact ``Content-Length``: the part is
  verified and written at its offset (``201`` stored, ``200`` already there or the
  whole blob already stored; ``400 integrity_error`` for a corrupt part - nothing
  kept).
* ``GET /v1/blobs/{sha256}/parts`` -> ``{size, chunk_size, received, complete}``
  (``404`` when no part upload is in progress): what a resumed upload can skip.
* ``POST /v1/blobs/{sha256}/complete`` hashes the assembled parts and stores the
  blob (``201``/``200``; ``409`` parts missing; ``400 integrity_error`` - the
  upload starts over).

**Fetch-token scope (V0.5 D83):** a fetch token reads the digests it was minted
for and, when one of them is a stored tree manifest, every blob that tree lists.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Path, Request, Response, status
from fastapi.responses import FileResponse
from starlette.requests import ClientDisconnect

from meshforge.common.cas import MAX_CHUNK_SIZE, MIN_CHUNK_SIZE, chunk_span
from meshforge.common.errors import FailureType
from meshforge.common.hashing import Digest
from meshforge.common.logging import get_logger
from meshforge.controller.api.auth import (
    ApiAuth,
    FabricTokenRequired,
    bearer_token,
    get_auth,
    unauthorized,
)
from meshforge.controller.api.errors import ApiError
from meshforge.controller.blobs import (
    MAX_HAVE_DIGESTS,
    BlobError,
    BlobIntegrityError,
    BlobNotFoundError,
    BlobStore,
    BlobTooLargeError,
)
from meshforge.controller.upload_grants import GrantRefusal, UploadTicket

__all__ = ["BLOBS_STATE_KEY", "blob_router", "get_blob_store"]

log = get_logger(__name__)

BLOBS_STATE_KEY: Final = "meshforge_blobs"
_HEX_PATTERN: Final = r"^[0-9a-f]{64}$"
_MAX_CONTENT_LENGTH_CHARS: Final = 20
MAX_HAVE_BODY_BYTES: Final = 1024 * 1024
_DIGEST_RE: Final = re.compile(r"sha256:[0-9a-f]{64}")
_HEX_RE: Final = re.compile(r"[0-9a-f]{64}")
MAX_PART_INDEX: Final = 1 << 20

_BLOB_ERROR_STATUS: Final = {
    FailureType.DISK_FULL: status.HTTP_507_INSUFFICIENT_STORAGE,
    FailureType.RESOURCE_BUSY: status.HTTP_503_SERVICE_UNAVAILABLE,
    FailureType.CONFLICT: status.HTTP_409_CONFLICT,
    FailureType.NOT_FOUND: status.HTTP_404_NOT_FOUND,
}

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


_GRANT_STATUS: Final = {
    "unknown": status.HTTP_401_UNAUTHORIZED,
    "expired": status.HTTP_401_UNAUTHORIZED,
    "closed": status.HTTP_403_FORBIDDEN,
    "length_required": status.HTTP_411_LENGTH_REQUIRED,
    "blob_quota": status.HTTP_413_CONTENT_TOO_LARGE,
    "byte_quota": status.HTTP_413_CONTENT_TOO_LARGE,
}


async def _authorize(
    request: Request, auth: ApiAuth, store: BlobStore, digest: Digest
) -> str:
    """Reads: ``fabric`` or ``fetch`` (its digests, or a tree's members), else 401."""
    token = bearer_token(request)
    if auth.verifier is not None and auth.verifier.verify(token):
        return "fabric"
    grant = None if auth.fetch_tokens is None else auth.fetch_tokens.grant(token)
    if grant is None:
        raise unauthorized()
    if digest.hex in grant.digests:
        return "fetch"
    for root in grant.digests:
        if digest.hex in await store.tree_members(Digest(root)):
            return "fetch"
    raise unauthorized()


def _authorize_upload(
    request: Request, auth: ApiAuth, digest: Digest, declared: int | None
) -> UploadTicket | None:
    """Uploads: ``None`` for the fabric token, else a reserved grant ticket."""
    token = bearer_token(request)
    if auth.verifier is not None and auth.verifier.verify(token):
        return None
    grants = auth.upload_grants
    if token is None or grants is None:
        raise unauthorized()
    try:
        return grants.begin(token, digest, declared)
    except GrantRefusal as refusal:
        if refusal.reason == "unknown":
            # Not a grant at all: an unauthenticated request - no log line, so
            # random tokens cannot flood the log.
            raise unauthorized() from None
        log.warning(
            "controller.blobs.upload_refused",
            digest=str(digest),
            reason=refusal.reason,
        )
        if refusal.reason == "expired":
            raise unauthorized() from None
        raise ApiError(
            FailureType.UNAUTHORIZED
            if refusal.reason == "closed"
            else FailureType.INVALID_INPUT,
            refusal.message,
            http_status=_GRANT_STATUS[refusal.reason],
        ) from None


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
    declared = _declared_size(request)
    ticket = _authorize_upload(request, auth, digest, declared)
    credential = "fabric" if ticket is None else "grant"

    async def body() -> AsyncIterator[bytes]:
        async for chunk in request.stream():
            yield chunk

    stored = False
    try:
        result = await store.put(digest, body(), declared_size=declared)
        stored = True
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
            http_status=_BLOB_ERROR_STATUS.get(
                exc.failure_type, status.HTTP_400_BAD_REQUEST
            ),
            headers={"Retry-After": "5"}
            if exc.failure_type is FailureType.RESOURCE_BUSY
            else None,
        ) from None
    finally:
        if ticket is not None and auth.upload_grants is not None:
            auth.upload_grants.finish(
                ticket, stored=stored, size=result.size if stored else 0
            )
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
    await _authorize(request, auth, store, digest)
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
    await _authorize(request, auth, store, digest)
    if store.size(digest) is None:
        raise ApiError.not_found("blob")
    return FileResponse(
        store.path_for(digest),
        media_type="application/octet-stream",
        headers={"etag": f'"{digest}"'},
    )


# ================================================================ V0.5 (D82)


def _blob_error(exc: BlobError) -> ApiError:
    if isinstance(exc, BlobTooLargeError):
        return ApiError(
            FailureType.INVALID_INPUT,
            exc.message,
            http_status=status.HTTP_413_CONTENT_TOO_LARGE,
        )
    return ApiError(
        exc.failure_type,
        exc.message,
        http_status=_BLOB_ERROR_STATUS.get(
            exc.failure_type, status.HTTP_400_BAD_REQUEST
        ),
        headers={"Retry-After": "5"}
        if exc.failure_type is FailureType.RESOURCE_BUSY
        else None,
    )


async def _bounded_body(request: Request, limit: int) -> bytes:
    declared = _declared_size(request)
    if declared is not None and declared > limit:
        raise ApiError(
            FailureType.INVALID_INPUT,
            f"request body larger than {limit} bytes",
            http_status=status.HTTP_413_CONTENT_TOO_LARGE,
        )
    data = bytearray()
    async for chunk in request.stream():
        data += chunk
        if len(data) > limit:
            raise ApiError(
                FailureType.INVALID_INPUT,
                f"request body larger than {limit} bytes",
                http_status=status.HTTP_413_CONTENT_TOO_LARGE,
            )
    return bytes(data)


@blob_router.post("/have", summary="Which of these blobs are missing? (have/need)")
async def have_blobs(
    request: Request, _auth: FabricTokenRequired, store: Store
) -> dict[str, list[str]]:
    raw = await _bounded_body(request, MAX_HAVE_BODY_BYTES)
    try:
        body: Any = json.loads(raw)
    except (ValueError, RecursionError):
        raise ApiError.invalid("the request body is not valid JSON") from None
    digests = body.get("digests") if isinstance(body, dict) else None
    if (
        not isinstance(body, dict)
        or set(body) != {"digests"}
        or not isinstance(digests, list)
    ):
        raise ApiError.invalid('the body must be {"digests": ["sha256:<hex>", ...]}')
    if len(digests) > MAX_HAVE_DIGESTS:
        raise ApiError(
            FailureType.INVALID_INPUT,
            f"at most {MAX_HAVE_DIGESTS} digests per call",
            http_status=status.HTTP_413_CONTENT_TOO_LARGE,
        )
    if not all(isinstance(d, str) and _DIGEST_RE.fullmatch(d) for d in digests):
        raise ApiError.invalid("every digest must be sha256:<64 lower-case hex>")
    asked = [Digest.parse(d) for d in digests]
    missing = store.missing(asked)
    if store.used is not None:
        absent = {d.hex for d in missing}
        await store.used([d for d in asked if d.hex not in absent])
    return {"missing": [str(d) for d in missing]}


def _int_header(request: Request, name: str) -> int:
    raw = request.headers.get(name)
    if raw is None or len(raw) > _MAX_CONTENT_LENGTH_CHARS or not raw.isdigit():
        raise ApiError.invalid(f"{name} must be a non-negative integer")
    return int(raw)


PartIndex = Annotated[int, Path(ge=0, lt=MAX_PART_INDEX)]


@blob_router.put(
    "/{sha256}/parts/{index}",
    status_code=status.HTTP_201_CREATED,
    summary="Upload one verified part of a large blob (resumable)",
)
async def put_part(
    request: Request,
    response: Response,
    sha256: Sha256,
    index: PartIndex,
    _auth: FabricTokenRequired,
    store: Store,
) -> dict[str, object]:
    digest = Digest(sha256)
    size = _int_header(request, "x-meshforge-blob-size")
    chunk_size = _int_header(request, "x-meshforge-chunk-size")
    chunk_hex = request.headers.get("x-meshforge-chunk-sha256", "")
    if not _HEX_RE.fullmatch(chunk_hex):
        raise ApiError.invalid("X-MeshForge-Chunk-Sha256 must be 64 lower-case hex")
    if not MIN_CHUNK_SIZE <= chunk_size <= MAX_CHUNK_SIZE:
        raise ApiError.invalid(
            f"X-MeshForge-Chunk-Size must be in [{MIN_CHUNK_SIZE}, {MAX_CHUNK_SIZE}]"
        )
    if size > store.max_blob_bytes:
        raise ApiError(
            FailureType.INVALID_INPUT,
            f"blob larger than the {store.max_blob_bytes}-byte limit",
            http_status=status.HTTP_413_CONTENT_TOO_LARGE,
        )
    try:
        _start, length = chunk_span(index, size, chunk_size)
    except ValueError as exc:
        raise ApiError.invalid(str(exc)) from None
    declared = _declared_size(request)
    if declared is None:
        raise ApiError(
            FailureType.INVALID_INPUT,
            "a part upload needs Content-Length",
            http_status=status.HTTP_411_LENGTH_REQUIRED,
        )
    if declared != length:
        raise ApiError.invalid(f"part {index} must be exactly {length} bytes")

    async def body() -> AsyncIterator[bytes]:
        async for chunk in request.stream():
            yield chunk

    try:
        stored = await store.put_part(
            digest,
            index,
            size=size,
            chunk_size=chunk_size,
            chunk_hex=chunk_hex,
            body=body(),
        )
    except BlobIntegrityError:
        log.warning(
            "controller.blobs.part_integrity_error", digest=str(digest), index=index
        )
        raise ApiError(
            FailureType.INTEGRITY_ERROR,
            f"part {index} does not hash to its declared digest; nothing was kept",
        ) from None
    except ClientDisconnect:
        raise ApiError(
            FailureType.TRANSFER_ERROR,
            "the part upload was interrupted; send it again",
        ) from None
    except BlobError as exc:
        raise _blob_error(exc) from None
    if not stored:
        response.status_code = status.HTTP_200_OK
    return {"digest": str(digest), "index": index, "stored": stored}


@blob_router.get("/{sha256}/parts", summary="Parts already received (resume)")
async def part_status(
    sha256: Sha256, _auth: FabricTokenRequired, store: Store
) -> dict[str, object]:
    digest = Digest(sha256)
    found = await store.part_status(digest)
    if found is None:
        raise ApiError.not_found("part upload")
    return {
        "digest": str(digest),
        "size": found.size,
        "chunk_size": found.chunk_size,
        "received": list(found.received),
        "complete": found.complete,
    }


@blob_router.post(
    "/{sha256}/complete",
    status_code=status.HTTP_201_CREATED,
    summary="Verify the assembled parts and store the blob",
)
async def complete_parts(
    response: Response, sha256: Sha256, _auth: FabricTokenRequired, store: Store
) -> dict[str, object]:
    digest = Digest(sha256)
    try:
        result = await store.complete_parts(digest)
    except BlobIntegrityError as exc:
        raise ApiError(FailureType.INTEGRITY_ERROR, exc.message) from None
    except BlobNotFoundError:
        raise ApiError.not_found("part upload") from None
    except BlobError as exc:
        raise _blob_error(exc) from None
    if not result.created:
        response.status_code = status.HTTP_200_OK
    return {"digest": str(digest), "size": result.size, "created": result.created}
