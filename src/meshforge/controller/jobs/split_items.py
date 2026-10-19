"""Split items at submit: resolve, store the list, describe it (V0.10 G1, D260).

``POST /v1/jobs`` calls :func:`resolve_split` for a manifest with ``split:``
while it holds the blob store's GC guard (shared), after the project tree and
the named inputs were checked:

1. a split job needs a **tree** job (``compute run`` sends one) - the items are
   paths of that tree, or of its named inputs;
2. :func:`meshforge.common.split_items.resolve` runs **in a thread** (it walks
   the trees and may read a ``list_file`` / ``lines`` file from the CAS) with
   the ``[controller.split]`` caps;
3. the canonical item list is stored in the CAS as one blob, so every later
   step (planner, lanes, combine, records) reads the same list in the same
   order (checklist 33); its digest, count and bytes go on the job row
   (:class:`~meshforge.controller.jobs.store.SplitRecord`).

Every refusal is :class:`~meshforge.common.manifest.ManifestError`
(``invalid_input`` with the YAML path; ``0 items`` included). Nothing here runs
user code or parses the files beyond counting lines - the controller never
interprets item contents.
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import AsyncIterator, Mapping
from typing import IO

from meshforge.common.hashing import Digest
from meshforge.common.logging import get_logger
from meshforge.common.manifest import Manifest, ManifestError
from meshforge.common.split_items import ResolvedItems, SplitLimits, resolve
from meshforge.common.tree import Tree
from meshforge.controller.blobs import BlobStore
from meshforge.controller.jobs.store import SplitRecord

__all__ = ["resolve_split", "split_record"]

log = get_logger(__name__)


async def _one(data: bytes) -> AsyncIterator[bytes]:
    yield data


def split_record(manifest: Manifest, items: ResolvedItems) -> SplitRecord:
    """What the job row stores for ``items``."""
    spec = manifest.canonical_dict()["split"]
    return SplitRecord(
        spec=spec,
        items_digest=str(items.digest),
        item_count=items.count,
        item_bytes=items.item_bytes,
    )


async def resolve_split(
    manifest: Manifest,
    *,
    project: Tree | None,
    inputs: Mapping[str, Tree],
    blobs: BlobStore,
    limits: SplitLimits,
) -> tuple[SplitRecord, ResolvedItems]:
    """Resolve and store the items of ``manifest.split`` (module docstring).

    Raises :class:`ManifestError` (``invalid_input``); a blob store failure
    keeps its own type (``disk_full``, ``resource_busy``...)."""
    spec = manifest.split
    if spec is None:  # pragma: no cover - the caller checks
        raise ValueError("not a split job")
    if project is None:
        raise ManifestError.one(
            "split", "a split job needs a project tree (compute run sends one)"
        )

    def _open(sha256_hex: str) -> IO[bytes]:
        return blobs.open_path(Digest(sha256_hex)).open("rb")

    # D278: the list's size is capped while it is resolved, never above what
    # the blob store would keep (the check below stays the authority)
    limits = dataclasses.replace(
        limits, max_list_bytes=min(limits.max_list_bytes, blobs.max_blob_bytes)
    )
    items = await asyncio.to_thread(resolve, spec, project, inputs, _open, limits)
    blob = items.blob()
    if len(blob) > blobs.max_blob_bytes:
        raise ManifestError.one(
            "split.over",
            f"the item list is {len(blob)} bytes, more than the controller's blob "
            f"limit of {blobs.max_blob_bytes}",
        )
    digest = items.digest
    await blobs.put(digest, _one(blob), declared_size=len(blob))
    record = split_record(manifest, items)
    log.info(
        "controller.jobs.split_resolved",
        over=items.over,
        items=items.count,
        item_bytes=items.item_bytes,
        items_digest=record.items_digest,
        list_bytes=len(blob),
    )
    return record, items
