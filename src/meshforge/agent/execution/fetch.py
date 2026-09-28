"""Fetch an attempt's bundle from the controller's blob store (V0.4 G4, D48/D67).

``GET <blob base>/v1/blobs/<hex>`` with the attempt's **fetch token** (never the
fabric token: an agent does not hold it, ADR-0014), streamed to a part file while
it is hashed and counted. Only a body whose SHA-256 is the requested digest is
renamed into place; anything else deletes the part file and raises
:class:`FetchError` with a typed failure:

* ``unauthorized`` - the token was refused (401/403: expired, revoked, wrong);
* ``not_found`` - the controller does not have the blob (404);
* ``integrity_error`` - the bytes do not hash to the digest (a corrupt or hostile
  body): nothing is kept;
* ``invalid_input`` - the body is larger than ``max_bytes``;
* ``transfer_error`` - connection refused/reset, other HTTP statuses, a body that
  ended early;
* ``timeout`` - connect/read timed out.

The token travels only in the ``Authorization`` header and is never logged.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import os
import secrets
from pathlib import Path
from typing import Final

import httpx

from meshforge.common.errors import FailureType, MeshForgeError
from meshforge.common.hashing import Digest
from meshforge.common.logging import get_logger

__all__ = ["DEFAULT_MAX_BUNDLE_BYTES", "FetchError", "blob_base_url", "fetch_bundle"]

log = get_logger(__name__)

DEFAULT_MAX_BUNDLE_BYTES: Final = 2 * 1024**3
"""The controller's default ``max_blob_bytes``: a bigger body is not a bundle."""
_CHUNK: Final = 1024 * 1024


class FetchError(MeshForgeError):
    default_failure_type = FailureType.TRANSFER_ERROR


def blob_base_url(blob_url: str, controller_host: str | None, blob_port: int) -> str:
    """``ExecutionSpec.blob_url`` if set, else ``http://<dialled host>:<blob_port>``."""
    if blob_url:
        return blob_url.rstrip("/")
    if not controller_host or not 0 < blob_port < 65536:
        raise FetchError(
            "no blob URL: the controller sent none and this agent has no dialled host",
            failure_type=FailureType.CONFIG_ERROR,
        )
    host = f"[{controller_host}]" if ":" in controller_host else controller_host
    return f"http://{host}:{blob_port}"


async def fetch_bundle(
    base_url: str,
    digest: Digest,
    token: str,
    dest: Path,
    *,
    max_bytes: int = DEFAULT_MAX_BUNDLE_BYTES,
    timeout_s: float = 60.0,
    transport: httpx.AsyncBaseTransport | None = None,
) -> int:
    """Download ``digest`` to ``dest`` (verified); returns its size. See module doc."""
    part = dest.with_name(f"{dest.name}.{secrets.token_hex(6)}.part")
    hasher = hashlib.sha256()
    size = 0
    url = f"{base_url}/v1/blobs/{digest.hex}"
    try:
        async with (
            httpx.AsyncClient(
                timeout=httpx.Timeout(timeout_s), transport=transport, trust_env=False
            ) as client,
            client.stream(
                "GET", url, headers={"authorization": f"Bearer {token}"}
            ) as response,
        ):
            _check_status(response.status_code)
            declared = response.headers.get("content-length")
            if (
                declared is not None
                and declared.isdigit()
                and int(declared) > max_bytes
            ):
                raise FetchError(
                    f"bundle larger than {max_bytes} bytes",
                    failure_type=FailureType.INVALID_INPUT,
                )
            with part.open("xb") as fh:
                async for chunk in response.aiter_raw(_CHUNK):
                    size += len(chunk)
                    if size > max_bytes:
                        raise FetchError(
                            f"bundle larger than {max_bytes} bytes",
                            failure_type=FailureType.INVALID_INPUT,
                        )
                    hasher.update(chunk)
                    fh.write(chunk)
                fh.flush()
                os.fsync(fh.fileno())
        if not hmac.compare_digest(hasher.hexdigest(), digest.hex):
            raise FetchError(
                "the fetched bundle does not match its digest; nothing was kept",
                failure_type=FailureType.INTEGRITY_ERROR,
            )
        os.replace(part, dest)
    except httpx.TimeoutException:
        raise FetchError(
            "timed out fetching the bundle", failure_type=FailureType.TIMEOUT
        ) from None
    except httpx.HTTPError as exc:
        raise FetchError(f"bundle fetch failed: {type(exc).__name__}") from None
    finally:
        with contextlib.suppress(OSError):
            part.unlink()
    return size


def _check_status(status: int) -> None:
    if status == 200:
        return
    if status in (401, 403):
        raise FetchError(
            "the controller refused this attempt's fetch token",
            failure_type=FailureType.UNAUTHORIZED,
        )
    if status == 404:
        raise FetchError(
            "the controller does not have this bundle",
            failure_type=FailureType.NOT_FOUND,
        )
    raise FetchError(f"bundle fetch answered HTTP {status}")
