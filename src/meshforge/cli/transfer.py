"""Upload a project as a tree: have/need, parts, resume (V0.5 G1, D81/D82).

``compute run`` builds the project's tree manifest (:func:`~meshforge.common.tree.
build_tree`: the same ignore rules and path refusals as bundles) and
:func:`upload_tree` sends only what the controller lacks:

1. ``POST /v1/blobs/have`` with every blob digest the tree names plus the manifest
   itself (batched at the server's per-call cap) -> the missing ones;
2. each missing **one-chunk** file is one ``PUT /v1/blobs/{hex}``;
3. each missing **chunked** file is sent part by part
   (``PUT /v1/blobs/{hex}/parts/{i}`` with the chunk's digest from the manifest),
   skipping the parts ``GET /v1/blobs/{hex}/parts`` says already arrived - so an
   interrupted upload resumes without re-sending completed chunks (criterion 3) -
   then ``POST /v1/blobs/{hex}/complete``;
4. the manifest goes last (once its blobs are in).

An unchanged project therefore uploads **0 bytes**; one changed file uploads that
file only (criterion 1). Every part is re-hashed on its way out and checked
against the manifest: a file edited between hashing and sending is reported,
never sent under the wrong name. Errors are
:class:`~meshforge.cli.client.ControllerError` (``compute`` turns them into exit
codes) or :class:`TransferError`.

**Local seeding (V0.6 D109)** - :func:`seed_local`: when this laptop runs an
enrolled agent, the same blobs (and the tree manifest) are also put into that
agent's cache (``<data_dir>/cas``) with :class:`~meshforge.common.cas.CasStore`
- write, verify against the manifest's digest, rename; the agent's own rules -
and recorded in its GC index (``<data_dir>/gc``) as just used, so the agent's
GC treats them like blobs it fetched itself. ``compute run`` then submits
``seeded: true`` and the planner counts no download for the Local candidate.
The agent finds them with its normal cache lookup (no protocol change). Any
reason not to seed (no cache here, a cache another OS user owns (V0.7 G5,
D145e), not writable, larger than ``[cli]
seed_max_bytes``, the free-disk floor, a file changed while copied) skips
seeding with that reason (``compute run`` prints it on stderr) - never an
error: the job still runs, the agent just downloads what it needs.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import httpx

from meshforge.cli.client import ControllerClient
from meshforge.common.cas import CasError, CasStore, chunk_span
from meshforge.common.errors import FailureType, MeshForgeError
from meshforge.common.gc_index import GcIndex
from meshforge.common.hashing import Digest
from meshforge.common.local_node import AGENT_CAS_DIR_NAME, AGENT_GC_DIR_NAME
from meshforge.common.tree import BuiltTree, TreeEntry

__all__ = [
    "HAVE_BATCH",
    "SeedReport",
    "TransferError",
    "UploadReport",
    "seed_local",
    "upload_tree",
]

HAVE_BATCH: Final = 10_000
"""Digests per have/need call (the controller's ``MAX_HAVE_DIGESTS``)."""
_TRANSFER_TIMEOUT_S: Final = 300.0
_READ: Final = 1024 * 1024


class TransferError(MeshForgeError):
    default_failure_type = FailureType.TRANSFER_ERROR


@dataclass(slots=True)
class UploadReport:
    tree_id: Digest
    files: int = 0
    """File entries in the tree."""
    uploaded_blobs: int = 0
    uploaded_bytes: int = 0
    """Payload bytes this run sent (file contents and the manifest)."""
    skipped_blobs: int = 0
    skipped_bytes: int = 0
    resumed_parts: int = 0
    """Parts an earlier, interrupted run had already delivered."""


Progress = Callable[[str], None]


def _check(client: ControllerClient, response: httpx.Response, path: str) -> Any:
    """The decoded JSON body; an error status raises the typed ControllerError."""
    return client._decode(response, path)


def _missing(client: ControllerClient, digests: list[Digest]) -> set[str]:
    out: set[str] = set()
    for start in range(0, len(digests), HAVE_BATCH):
        batch = [str(d) for d in digests[start : start + HAVE_BATCH]]
        body = client._request("POST", "/v1/blobs/have", json_body={"digests": batch})
        listed = body.get("missing")
        if not isinstance(listed, list):
            raise TransferError("the controller's have/need answer has no list")
        out.update(str(d) for d in listed)
    return out


def _received(client: ControllerClient, hex_: str) -> tuple[set[int], bool]:
    path = f"/v1/blobs/{hex_}/parts"
    response = client._send("GET", path)
    if response.status_code == httpx.codes.NOT_FOUND:
        return set(), False
    body = _check(client, response, path)
    received = body.get("received") or []
    return {int(i) for i in received if isinstance(i, int)}, bool(body.get("complete"))


def _read_part(path: Path, start: int, length: int) -> bytes:
    with path.open("rb") as fh:
        fh.seek(start)
        data = fh.read(length)
    if len(data) != length:
        raise TransferError(f"{path.name} changed while it was uploaded; run again")
    return data


def _upload_parts(
    client: ControllerClient,
    entry: TreeEntry,
    path: Path,
    chunk_size: int,
    report: UploadReport,
) -> None:
    hex_ = entry.sha256
    received, complete = _received(client, hex_)
    if complete:
        return
    report.resumed_parts += len(received)
    for index, expected in enumerate(entry.chunks):
        if index in received:
            continue
        start, length = chunk_span(index, entry.size, chunk_size)
        data = _read_part(path, start, length)
        if not hmac.compare_digest(hashlib.sha256(data).hexdigest(), expected):
            raise TransferError(
                f"{entry.path} changed after it was hashed; run again",
                failure_type=FailureType.INTEGRITY_ERROR,
            )
        route = f"/v1/blobs/{hex_}/parts/{index}"
        response = client._send(
            "PUT",
            route,
            content=data,
            headers={
                "content-type": "application/octet-stream",
                "x-meshforge-blob-size": str(entry.size),
                "x-meshforge-chunk-size": str(chunk_size),
                "x-meshforge-chunk-sha256": expected,
            },
            timeout=_TRANSFER_TIMEOUT_S,
        )
        _check(client, response, route)
        report.uploaded_bytes += length
    client._request("POST", f"/v1/blobs/{hex_}/complete", timeout=_TRANSFER_TIMEOUT_S)


def upload_tree(
    client: ControllerClient, built: BuiltTree, *, progress: Progress | None = None
) -> UploadReport:
    """Send what the controller lacks of ``built`` (see module docstring)."""
    tree = built.tree
    report = UploadReport(tree_id=tree.tree_id, files=sum(1 for _ in tree.files()))
    blobs: dict[str, TreeEntry] = {}
    for entry in tree.files():
        blobs.setdefault(entry.sha256, entry)
    digests = [Digest(h) for h in blobs] + [tree.tree_id]
    missing = _missing(client, digests)
    for hex_, entry in blobs.items():
        if f"sha256:{hex_}" not in missing:
            report.skipped_blobs += 1
            report.skipped_bytes += entry.size
            continue
        path = built.sources[hex_]
        if progress is not None:
            progress(entry.path)
        if entry.chunks:
            _upload_parts(client, entry, path, tree.chunk_size, report)
        else:
            client.put_blob(hex_, path)
            report.uploaded_bytes += entry.size
        report.uploaded_blobs += 1
    if str(tree.tree_id) in missing:
        client._request(
            "PUT",
            f"/v1/blobs/{tree.tree_id.hex}",
            content=built.manifest,
            headers={"content-type": "application/octet-stream"},
            timeout=_TRANSFER_TIMEOUT_S,
        )
        report.uploaded_bytes += len(built.manifest)
    return report


# ------------------------------------------------------------------ seeding


@dataclass(slots=True)
class SeedReport:
    """What :func:`seed_local` did (V0.6 D109)."""

    seeded: bool
    reason: str | None = None
    """Why not (``no_cache``, ``other_user``, ``not_writable``, ``over_quota``,
    ``disk_full``, ``changed``, ``io_error``) - or a GC-index note when seeded
    anyway."""
    blobs_written: int = 0
    bytes_written: int = 0
    blobs_present: int = 0


def _skip(reason: str) -> SeedReport:
    return SeedReport(seeded=False, reason=reason)


def seed_local(
    built: BuiltTree,
    data_dir: Path,
    *,
    max_bytes: int,
    free_disk_floor_bytes: int,
) -> SeedReport:
    """Put ``built``'s blobs and manifest into the local agent's cache (module
    docstring). Blocking; never raises for an expected failure."""
    tree = built.tree
    root = data_dir / AGENT_CAS_DIR_NAME
    if not (root / "sha256").is_dir() or not (root / "tmp").is_dir():
        return _skip("no_cache")  # no agent has prepared a cache here
    if not _agents_own_cache(root):
        return _skip("other_user")
    if not os.access(root / "tmp", os.W_OK) or not os.access(root / "sha256", os.W_OK):
        return _skip("not_writable")
    blobs = tree.blob_sizes()
    total = sum(blobs.values()) + len(built.manifest)
    if total > max_bytes:
        return _skip("over_quota")
    store = CasStore(
        root,
        max_blob_bytes=max(total, 1),
        free_disk_floor_bytes=free_disk_floor_bytes,
    )
    report = SeedReport(seeded=True)
    items: list[tuple[str, int]] = []
    try:
        for hex_, size in blobs.items():
            digest = Digest(hex_)
            items.append((hex_, size))
            if store.has(digest):
                report.blobs_present += 1
                continue
            if store.put_file(built.sources[hex_], digest).created:
                report.blobs_written += 1
                report.bytes_written += size
        if not store.has(tree.tree_id):
            store.put_bytes(built.manifest, tree.tree_id)
            report.blobs_written += 1
            report.bytes_written += len(built.manifest)
        items.append((tree.tree_id.hex, len(built.manifest)))
    except CasError as exc:
        reason = (
            "disk_full"
            if exc.failure_type is FailureType.DISK_FULL
            else "changed"
            if exc.failure_type is FailureType.INTEGRITY_ERROR
            else "io_error"
        )
        return _skip(reason)
    except OSError as exc:
        return _skip("not_writable" if isinstance(exc, PermissionError) else "io_error")
    report.reason = _record_use(data_dir / AGENT_GC_DIR_NAME, items)
    return report


def _agents_own_cache(root: Path) -> bool:
    """V0.7 G5 (D145e): seed only a cache **this** OS user owns. An agent
    running as another user (a service account; or this CLI run as root)
    could not manage - or would have to trust - files written by us, so the
    CAS folders must be real directories (no symlink) owned by our uid.
    Windows has no uid: the per-user data folder's ACL decides there."""
    geteuid = getattr(os, "geteuid", None)
    if geteuid is None:  # pragma: no cover - Windows
        return True
    me = geteuid()
    for path in (root, root / "sha256", root / "tmp"):
        try:
            info = path.lstat()
        except OSError:
            return False
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != me:
            return False
    return True


def _record_use(gc_dir: Path, items: list[tuple[str, int]]) -> str | None:
    """Mark the seeded blobs as just used in the agent's GC index (its next
    disk reconcile would add them anyway; this keeps them out of the next
    eviction). None = recorded; else a note (seeding still counts)."""
    if not gc_dir.is_dir():
        return "no_gc_index"
    index = GcIndex(gc_dir, busy_timeout_s=5.0)
    try:
        index.open()
        for key, size in items:
            index.upsert("blob", key, size)
    except Exception:  # noqa: BLE001 - a note, never a failure
        return "gc_index_unavailable"
    finally:
        index.close()
    return None
