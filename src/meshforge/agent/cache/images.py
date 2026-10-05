"""List this node's environment images for the cache reports (V0.5 D90/D101).

Images ``meshforge-env:<hash>`` carry the label ``meshforge.env_hash`` (G2,
D86). Listing them is one Docker API call with a label filter; blocking, run in
a thread by the reporter every ``[agent.cache] rescan_s``. Key = the image id
(``sha256:<64 hex>``), size = the image's size in bytes.
"""

from __future__ import annotations

import re
from typing import Final

from meshforge.agent.execution.docker_runtime import ENV_IMAGE_REPO, LABEL_ENV_HASH
from meshforge.agent.execution.docker_support import open_client

__all__ = ["list_env_image_hashes", "list_env_images", "remove_env_image"]

_ID_RE: Final = re.compile(r"sha256:[0-9a-f]{64}")
_HASH_RE: Final = re.compile(r"[0-9a-f]{64}")
_MAX_IMAGES: Final = 10_000


def list_env_images(timeout_s: float = 10.0) -> dict[str, int]:
    """``image id -> bytes`` of the env images (raises when Docker is gone)."""
    client = open_client(timeout_s)
    try:
        listed = client.api.images(filters={"label": LABEL_ENV_HASH})
    finally:
        client.close()
    out: dict[str, int] = {}
    for image in listed[:_MAX_IMAGES]:
        image_id = image.get("Id") if isinstance(image, dict) else None
        if isinstance(image_id, str) and _ID_RE.fullmatch(image_id):
            size = image.get("Size", 0)
            out[image_id] = size if isinstance(size, int) and size >= 0 else 0
    return out


def list_env_image_hashes(timeout_s: float = 10.0) -> dict[str, int]:
    """``env hash -> bytes`` of the env images tagged ``meshforge-env:<hash>``
    (V0.5 G5 GC; raises when Docker is gone)."""
    client = open_client(timeout_s)
    try:
        listed = client.api.images(filters={"label": LABEL_ENV_HASH})
    finally:
        client.close()
    out: dict[str, int] = {}
    for image in listed[:_MAX_IMAGES]:
        if not isinstance(image, dict):
            continue
        labels = image.get("Labels") or {}
        digest = labels.get(LABEL_ENV_HASH) if isinstance(labels, dict) else None
        tags = image.get("RepoTags") or []
        if (
            isinstance(digest, str)
            and _HASH_RE.fullmatch(digest)
            and f"{ENV_IMAGE_REPO}:{digest}" in tags
        ):
            size = image.get("Size", 0)
            out[digest] = size if isinstance(size, int) and size >= 0 else 0
    return out


def remove_env_image(digest: str, timeout_s: float = 30.0) -> bool:
    """Remove ``meshforge-env:<digest>`` without force: Docker refuses while any
    container uses it (then False). True = removed or already gone."""
    if not _HASH_RE.fullmatch(digest):
        raise ValueError("not an env hash")
    client = open_client(timeout_s)
    try:
        try:
            client.api.remove_image(f"{ENV_IMAGE_REPO}:{digest}", force=False)
        except Exception as exc:  # noqa: BLE001 - the SDK's errors: NotFound / Conflict
            return type(exc).__name__ == "ImageNotFound"
        return True
    finally:
        client.close()
