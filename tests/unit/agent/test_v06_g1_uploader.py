"""V0.6 G1 / D114: the resumable uploader (:mod:`meshforge.agent.execution.uploader`)
against an in-memory parts API (``httpx.MockTransport``) - no network.

Pinned: a blob of at most one part is one ``PUT``; a larger one sends only the
parts the controller lacks, in the controller's chunk size; ``credited: false``
falls back to a whole ``PUT``; every HTTP answer maps to the documented
``UploadError`` (401 refused + retryable, 403/411/413 final, 5xx retryable,
unreadable answers retryable, another upload under the digest final); the
grant travels only in the ``Authorization`` header. Also ``upload_blob``'s 401
(retryable, refused) vs 403 (final).
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import httpx
import pytest

from meshforge.agent.execution.fetch import UploadError, upload_blob
from meshforge.agent.execution.uploader import UploadProgress, upload_parts
from meshforge.common.cas import MAX_CHUNK_SIZE, MIN_CHUNK_SIZE, chunk_span
from meshforge.common.hashing import sha256_bytes

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

CS = MIN_CHUNK_SIZE
BASE = "http://ctl.test:8471"
TOKEN = "grant-" + "g" * 40


class PartsServer:
    """The controller's parts API, in memory."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.hex = hashlib.sha256(data).hexdigest()
        self.chunk_size = 0
        self.parts: dict[int, bytes] = {}
        self.stored = False
        self.credited = True
        self.requests: list[tuple[str, str]] = []
        self.status_override: dict[tuple[str, str], int] = {}
        self.status_body: Any = None
        self.complete_status: list[int] = []
        self.auth: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append((request.method, path))
        self.auth.append(request.headers.get("authorization", ""))
        assert TOKEN not in str(request.url)
        kind = path.rsplit("/", 2)
        key = (request.method, kind[-1] if not kind[-1].isdigit() else "part")
        if key in self.status_override:
            return httpx.Response(self.status_override[key])
        if request.method == "GET" and path.endswith("/parts"):
            if self.status_body is not None:
                return httpx.Response(200, content=self.status_body)
            if self.stored:
                return httpx.Response(200, json={"complete": True, "size": 0})
            if not self.parts:
                return httpx.Response(404)
            return httpx.Response(
                200,
                json={
                    "size": len(self.data),
                    "chunk_size": self.chunk_size,
                    "received": sorted(self.parts),
                    "complete": False,
                },
            )
        if request.method == "PUT" and "/parts/" in path:
            index = int(path.rsplit("/", 1)[1])
            size = int(request.headers["x-meshforge-blob-size"])
            chunk = int(request.headers["x-meshforge-chunk-size"])
            body = request.content
            assert size == len(self.data)
            assert (
                hashlib.sha256(body).hexdigest()
                == (request.headers["x-meshforge-chunk-sha256"])
            )
            assert int(request.headers["content-length"]) == len(body)
            start, length = chunk_span(index, size, chunk)
            assert body == self.data[start : start + length]
            self.chunk_size = chunk
            self.parts[index] = body
            return httpx.Response(201, json={"stored": True})
        if request.method == "POST" and path.endswith("/complete"):
            if self.complete_status:
                return httpx.Response(self.complete_status.pop(0))
            if self.stored:
                return httpx.Response(200, json={"credited": self.credited})
            assembled = b"".join(self.parts[i] for i in sorted(self.parts))
            if assembled != self.data:
                return httpx.Response(409)
            self.stored = True
            return httpx.Response(201, json={"credited": True})
        if request.method == "PUT":
            body = request.content
            assert hashlib.sha256(body).hexdigest() == self.hex
            self.stored = True
            return httpx.Response(201)
        return httpx.Response(404)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def methods(self) -> list[str]:
        return [m for m, _ in self.requests]


def _file(tmp_path: Path, size: int) -> tuple[Path, bytes]:
    data = os.urandom(size)
    path = tmp_path / "blob.bin"
    path.write_bytes(data)
    return path, data


async def _up(server: PartsServer, path: Path, **kw: Any) -> tuple[int, UploadProgress]:
    progress = UploadProgress()
    transport = server.transport()

    async def put(*args: Any, **kwargs: Any) -> int:
        kwargs["transport"] = transport
        return await upload_blob(*args, **kwargs)

    sent = await upload_parts(
        BASE,
        sha256_bytes(server.data),
        TOKEN,
        path,
        part_bytes=kw.pop("part_bytes", CS),
        transport=transport,
        progress=progress,
        put=put,
        **kw,
    )
    return sent, progress


async def test_a_blob_of_one_part_is_one_put(tmp_path: Path) -> None:
    path, data = _file(tmp_path, CS)  # exactly one part: max for the PUT path
    server = PartsServer(data)
    sent, progress = await _up(server, path)
    assert server.methods() == ["PUT"] and server.stored
    assert sent == CS and progress.whole_puts == 1 and progress.parts_sent == 0
    assert server.auth == [f"Bearer {TOKEN}"]


async def test_a_larger_blob_goes_up_in_parts_and_completes(tmp_path: Path) -> None:
    path, data = _file(tmp_path, CS + 1)  # one part + 1 byte: the parts path
    server = PartsServer(data)
    sent, progress = await _up(server, path)
    assert server.methods() == ["GET", "PUT", "PUT", "POST"]
    assert server.stored and sent == CS + 1
    assert progress.sent_indices == [0, 1] and progress.whole_puts == 0
    assert set(server.auth) == {f"Bearer {TOKEN}"}


async def test_parts_the_controller_holds_are_not_sent_again(tmp_path: Path) -> None:
    path, data = _file(tmp_path, 4 * CS + 3)
    server = PartsServer(data)
    server.chunk_size = CS
    for index in (0, 2):
        start, length = chunk_span(index, len(data), CS)
        server.parts[index] = data[start : start + length]
    sent, progress = await _up(server, path)
    assert progress.sent_indices == [1, 3, 4]
    assert progress.parts_skipped == 2
    assert sent == 2 * CS + 3 and server.stored


async def test_the_controllers_chunk_size_wins_on_resume(tmp_path: Path) -> None:
    path, data = _file(tmp_path, 4 * CS)
    server = PartsServer(data)
    server.chunk_size = 2 * CS  # an earlier run used bigger parts
    server.parts[0] = data[: 2 * CS]
    _, progress = await _up(server, path, part_bytes=CS)
    assert progress.sent_indices == [1]
    assert server.chunk_size == 2 * CS and server.stored


async def test_an_uncredited_complete_proves_the_bytes_with_a_put(
    tmp_path: Path,
) -> None:
    path, data = _file(tmp_path, 2 * CS)
    server = PartsServer(data)
    server.stored, server.credited = True, False  # stored by someone else
    sent, progress = await _up(server, path)
    assert server.methods() == ["GET", "POST", "PUT"]
    assert progress.whole_puts == 1 and progress.parts_sent == 0
    assert sent == 2 * CS


async def test_a_credited_already_stored_blob_sends_nothing(tmp_path: Path) -> None:
    path, data = _file(tmp_path, 2 * CS)
    server = PartsServer(data)
    server.stored = True
    sent, _ = await _up(server, path)
    assert server.methods() == ["GET", "POST"] and sent == 0


@pytest.mark.parametrize(
    ("where", "status", "retryable", "refused"),
    [
        (("GET", "parts"), 401, True, True),
        (("GET", "parts"), 403, False, False),
        (("PUT", "part"), 401, True, True),
        (("PUT", "part"), 413, False, False),
        (("PUT", "part"), 411, False, False),
        (("PUT", "part"), 507, True, False),
        (("PUT", "part"), 503, True, False),
        (("POST", "complete"), 403, False, False),
        (("POST", "complete"), 500, True, False),
    ],
)
async def test_http_answers_map_to_typed_upload_errors(
    tmp_path: Path,
    where: tuple[str, str],
    status: int,
    retryable: bool,
    refused: bool,
) -> None:
    path, data = _file(tmp_path, 2 * CS)
    server = PartsServer(data)
    server.status_override[where] = status
    with pytest.raises(UploadError) as caught:
        await _up(server, path)
    assert caught.value.retryable is retryable
    assert bool(caught.value.details.get("refused")) is refused
    assert caught.value.failure_type.value == "output_error"
    assert TOKEN not in str(caught.value)


async def test_complete_reporting_missing_parts_forever_is_retryable(
    tmp_path: Path,
) -> None:
    path, data = _file(tmp_path, 2 * CS)
    server = PartsServer(data)
    server.complete_status = [409, 404, 400]
    with pytest.raises(UploadError) as caught:
        await _up(server, path)
    assert caught.value.retryable and "missing parts" in caught.value.message
    assert server.methods().count("POST") == 3  # bounded rounds


async def test_complete_missing_once_then_ok_succeeds(tmp_path: Path) -> None:
    path, data = _file(tmp_path, 2 * CS)
    server = PartsServer(data)
    server.complete_status = [409]
    sent, _ = await _up(server, path)
    assert server.stored and sent == 2 * CS


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"[1, 2]",
        json.dumps({"size": 2 * CS, "chunk_size": 1, "received": []}).encode(),
        json.dumps(
            {"size": 2 * CS, "chunk_size": MAX_CHUNK_SIZE + 1, "received": []}
        ).encode(),
        json.dumps(
            {"size": 2 * CS, "chunk_size": CS, "received": [0, 1, 2]}
        ).encode(),  # more than the blob has (max accepted is 2)
        json.dumps({"size": 2 * CS, "chunk_size": CS, "received": ["x"]}).encode(),
        json.dumps({"chunk_size": CS}).encode(),
    ],
)
async def test_an_unreadable_part_status_is_a_retryable_error(
    tmp_path: Path, body: bytes
) -> None:
    path, data = _file(tmp_path, 2 * CS)
    server = PartsServer(data)
    server.status_body = body
    with pytest.raises(UploadError) as caught:
        await _up(server, path)
    assert caught.value.retryable
    assert "unreadable" in caught.value.message


async def test_a_full_received_list_is_accepted(tmp_path: Path) -> None:
    path, data = _file(tmp_path, 2 * CS)
    server = PartsServer(data)
    server.chunk_size = CS
    server.parts = {0: data[:CS], 1: data[CS:]}
    _, progress = await _up(server, path)  # 2 of 2: the max list length
    assert progress.parts_sent == 0 and progress.parts_skipped == 2


async def test_another_upload_under_the_digest_is_final(tmp_path: Path) -> None:
    path, data = _file(tmp_path, 2 * CS)
    server = PartsServer(data)
    server.status_body = json.dumps(
        {"size": 3 * CS, "chunk_size": CS, "received": [], "complete": False}
    ).encode()
    with pytest.raises(UploadError) as caught:
        await _up(server, path)
    assert not caught.value.retryable


async def test_a_connection_error_is_retryable(tmp_path: Path) -> None:
    path, _ = _file(tmp_path, 2 * CS)

    def down(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(UploadError) as caught:
        await upload_parts(
            BASE,
            sha256_bytes(path.read_bytes()),
            TOKEN,
            path,
            part_bytes=CS,
            transport=httpx.MockTransport(down),
        )
    assert caught.value.retryable and "ConnectError" in caught.value.message


async def test_a_file_that_shrank_while_uploading_is_final(tmp_path: Path) -> None:
    path, data = _file(tmp_path, 3 * CS)
    server = PartsServer(data)
    real = server.handler

    def shrink_after_status(request: httpx.Request) -> httpx.Response:
        response = real(request)
        if request.method == "GET":
            path.write_bytes(data[:CS])
        return response

    transport = httpx.MockTransport(shrink_after_status)
    with pytest.raises(UploadError) as caught:
        await upload_parts(
            BASE, sha256_bytes(data), TOKEN, path, part_bytes=CS, transport=transport
        )
    assert not caught.value.retryable and "read the file" in caught.value.message


async def test_part_bytes_out_of_bounds_is_rejected(tmp_path: Path) -> None:
    path, data = _file(tmp_path, 10)
    for bad in (MIN_CHUNK_SIZE - 1, MAX_CHUNK_SIZE + 1):
        with pytest.raises(ValueError, match="part_bytes"):
            await upload_parts(BASE, sha256_bytes(data), TOKEN, path, part_bytes=bad)


@pytest.mark.parametrize(
    ("status", "retryable", "refused"), [(401, True, True), (403, False, False)]
)
async def test_upload_blob_401_waits_for_a_refresh_403_is_final(
    tmp_path: Path, status: int, retryable: bool, refused: bool
) -> None:
    path, data = _file(tmp_path, 100)
    transport = httpx.MockTransport(lambda _r: httpx.Response(status))
    with pytest.raises(UploadError) as caught:
        await upload_blob(BASE, sha256_bytes(data), TOKEN, path, transport=transport)
    assert caught.value.retryable is retryable
    assert bool(caught.value.details.get("refused")) is refused
