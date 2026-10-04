"""List this node's environment images for the cache reports (V0.5 D90/D101).

Images ``meshforge-env:<hash>`` carry the label ``meshforge.env_hash`` (G2,
D86). Listing them is one Docker API call with a label filter; blocking, run in
a thread by the reporter every ``[agent.cache] rescan_s``. Key = the image id
(``sha256:<64 hex>``), size = the image's size in bytes.
"""

from __future__ import annotations

import re
from typing import Final

from meshforge.agent.execution.docker_runtime import LABEL_ENV_HASH
from meshforge.agent.execution.docker_support import open_client

__all__ = ["list_env_images"]

_ID_RE: Final = re.compile(r"sha256:[0-9a-f]{64}")
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
