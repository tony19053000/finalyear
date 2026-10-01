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
file only (criterion 1). Every part is re-hashed on its way out and checked against
the manifest: a file edited between hashing and sending is reported, never sent
under the wrong name. Errors are :class:`~meshforge.cli.client.ControllerError`
(``compute`` turns them into exit codes) or :class:`TransferError`.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import httpx

from meshforge.cli.client import ControllerClient
from meshforge.common.cas import chunk_span
from meshforge.common.errors import FailureType, MeshForgeError
from meshforge.common.hashing import Digest
from meshforge.common.tree import BuiltTree, TreeEntry

__all__ = ["HAVE_BATCH", "TransferError", "UploadReport", "upload_tree"]

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
