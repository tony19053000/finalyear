"""V0.5 G1 criteria 1 and 3 (upload side) against a real controller over HTTP.

``cli.transfer.upload_tree`` (what ``compute run`` calls) uses the synchronous
CLI client, so it runs in a thread while the controller serves on this loop.
The interrupted-upload test SIGKILLs a real uploader process mid-file and counts
the bytes the resumed run sends.
"""

from __future__ import annotations

import asyncio
import os
import sys
import textwrap
import time
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from mf_jobs_harness import api_client, g2_settings, running_controller, token_of
from pydantic import SecretStr

from meshforge.cli.client import ControllerClient
from meshforge.cli.transfer import upload_tree
from meshforge.common.cas import MIN_CHUNK_SIZE
from meshforge.common.hashing import sha256_bytes
from meshforge.common.tree import build_tree
from meshforge.controller.server import ControllerRuntime

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(120)]

CS = MIN_CHUNK_SIZE


@pytest_asyncio.fixture
async def runtime(pg_settings: object) -> AsyncIterator[ControllerRuntime]:
    settings = g2_settings(pg_settings, blobs={"max_blob_bytes": 64 * 1024 * 1024})  # type: ignore[arg-type]
    async with running_controller(settings) as rt:
        yield rt


def _client(runtime: ControllerRuntime) -> ControllerClient:
    return ControllerClient(
        f"http://127.0.0.1:{runtime.http_port}",
        timeout_s=30,
        token=SecretStr(token_of(runtime)),
    )


def _project(root: Path) -> Path:
    root.mkdir()
    (root / "main.py").write_bytes(b"print('hello')\n")
    (root / "lib.py").write_bytes(b"X = 1\n")
    (root / "data.bin").write_bytes(os.urandom(5 * CS + 3))
    return root


async def test_resubmit_uploads_nothing_and_one_change_uploads_one_file(
    runtime: ControllerRuntime, tmp_path: Path
) -> None:
    project = _project(tmp_path / "p")
    client = _client(runtime)
    try:
        first = await asyncio.to_thread(
            upload_tree, client, build_tree(project, chunk_size=CS)
        )
        assert first.uploaded_blobs == 3 and first.skipped_blobs == 0
        again = await asyncio.to_thread(
            upload_tree, client, build_tree(project, chunk_size=CS)
        )
        assert again.uploaded_bytes == 0 and again.uploaded_blobs == 0  # criterion 1
        assert again.skipped_blobs == 3
        (project / "lib.py").write_bytes(b"X = 2\n")
        built = build_tree(project, chunk_size=CS)
        changed = await asyncio.to_thread(upload_tree, client, built)
        assert changed.uploaded_blobs == 1 and changed.skipped_blobs == 2
        assert changed.uploaded_bytes == len(b"X = 2\n") + len(built.manifest)
    finally:
        client.close()
    async with api_client(runtime, token=token_of(runtime)) as http:
        stored = await http.get(f"/v1/blobs/{built.tree.tree_id.hex}")
        assert stored.content == built.manifest
        data = await http.get(
            f"/v1/blobs/{sha256_bytes((project / 'data.bin').read_bytes()).hex}"
        )
        assert data.content == (project / "data.bin").read_bytes()


UPLOADER = """
import sys, time
from pathlib import Path
import httpx
from pydantic import SecretStr
from meshforge.cli.client import ControllerClient
from meshforge.cli.transfer import upload_tree
from meshforge.common.tree import build_tree

class Slow(httpx.HTTPTransport):
    def handle_request(self, request):
        if "/parts/" in request.url.path:
            time.sleep(0.25)
        return super().handle_request(request)

url, token, project, cs = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
client = ControllerClient(url, timeout_s=30, token=SecretStr(token), transport=Slow())
upload_tree(client, build_tree(Path(project), chunk_size=cs))
print("finished")
"""


async def test_killed_upload_resumes_without_resending_completed_parts(
    runtime: ControllerRuntime, tmp_path: Path
) -> None:
    project = tmp_path / "p"
    project.mkdir()
    data = os.urandom(12 * CS)
    (project / "big.bin").write_bytes(data)
    digest = sha256_bytes(data)
    url = f"http://127.0.0.1:{runtime.http_port}"
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        textwrap.dedent(UPLOADER),
        url,
        token_of(runtime),
        str(project),
        str(CS),
        stdout=asyncio.subprocess.DEVNULL,
    )
    received: list[int] = []
    try:
        async with api_client(runtime, token=token_of(runtime)) as http:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                status = await http.get(f"/v1/blobs/{digest.hex}/parts")
                if status.status_code == 200 and len(status.json()["received"]) >= 3:
                    break
                assert child.returncode is None, "the uploader finished before the kill"
                await asyncio.sleep(0.05)
            child.kill()  # mid-file, for real
            await asyncio.wait_for(child.wait(), 10)
            status = await http.get(f"/v1/blobs/{digest.hex}/parts")
            received = status.json()["received"]
            assert 3 <= len(received) < 12
            assert (await http.head(f"/v1/blobs/{digest.hex}")).status_code == 404
    finally:
        if child.returncode is None:
            child.kill()
            await asyncio.wait_for(child.wait(), 10)
    client = _client(runtime)
    try:
        built = build_tree(project, chunk_size=CS)
        resumed = await asyncio.to_thread(upload_tree, client, built)
    finally:
        client.close()
    assert resumed.resumed_parts == len(received)
    # Only the missing parts (plus the small manifest) travelled again.
    assert resumed.uploaded_bytes == (12 - len(received)) * CS + len(built.manifest)
    async with api_client(runtime, token=token_of(runtime)) as http:
        assert (await http.get(f"/v1/blobs/{digest.hex}")).content == data
