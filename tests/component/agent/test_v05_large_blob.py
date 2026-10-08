"""V0.5 criterion 2 prerequisite (review B2): one file over 5 GB end to end.

A sparse 5 GiB + 3 MiB file (a few non-zero bytes so the content is not trivial)
goes through the real path with the **default** limits: tree build -> have/need
-> resumable part upload to a real controller -> agent chunked Range fetch into
its CAS -> materialise with hash-while-copy. Writes ~15 GB to disk (controller
CAS, agent CAS, workdir), so it is ``slow`` (run with ``--run-slow``).
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from mf_agent_harness import make_controller_settings
from mf_jobs_harness import running_controller, token_of
from pydantic import SecretStr

from meshforge.agent.cas import AgentCas
from meshforge.agent.cas.fetcher import BlobSource
from meshforge.agent.execution.fetch import stage_tree
from meshforge.cli.client import ControllerClient
from meshforge.cli.transfer import upload_tree
from meshforge.common.cas import DEFAULT_CHUNK_SIZE
from meshforge.common.tree import build_tree
from meshforge.controller.server import ControllerRuntime

pytestmark = [pytest.mark.asyncio, pytest.mark.slow, pytest.mark.timeout(1800)]

SIZE = 5 * 1024**3 + 3 * 1024 * 1024
MARKS = (0, 1234567, 2 * 1024**3 + 5, SIZE - 1)
NEEDED_FREE = 18 * 1024**3


@pytest_asyncio.fixture
async def runtime(pg_settings: Any) -> AsyncIterator[ControllerRuntime]:
    # Default [controller.blobs]: proves the 64 GiB default admits the file.
    async with running_controller(make_controller_settings(pg_settings)) as rt:
        yield rt


def _sparse(path: Path) -> None:
    with path.open("wb") as fh:
        fh.truncate(SIZE)
        for offset in MARKS:
            fh.seek(offset)
            fh.write(b"\x5a")


async def test_a_file_over_5_gb_round_trips_with_default_limits(
    runtime: ControllerRuntime, tmp_path: Path
) -> None:
    free = os.statvfs(tmp_path).f_bavail * os.statvfs(tmp_path).f_frsize
    if free < NEEDED_FREE:
        pytest.skip(f"needs {NEEDED_FREE} bytes free, {free} available")
    project = tmp_path / "p"
    project.mkdir()
    _sparse(project / "big.bin")
    started = time.monotonic()
    built = await asyncio.to_thread(build_tree, project)
    (entry,) = built.tree.files()
    assert entry.size == SIZE and len(entry.chunks) == -(-SIZE // DEFAULT_CHUNK_SIZE)
    client = ControllerClient(
        f"https://127.0.0.1:{runtime.http_port}",
        timeout_s=60,
        token=SecretStr(token_of(runtime)),
    )
    try:
        report = await asyncio.to_thread(upload_tree, client, built)
    finally:
        client.close()
    uploaded = time.monotonic()
    assert report.uploaded_bytes == SIZE + len(built.manifest)
    assert runtime.blobs is not None and runtime.blobs.size(entry.digest) == SIZE
    assert runtime.fetch_tokens is not None
    token = runtime.fetch_tokens.mint("attempt_big", [built.tree.tree_id], ttl_s=1800)
    cas = AgentCas.at(tmp_path / "agent")
    staged = await stage_tree(
        cas,
        tree_id=str(built.tree.tree_id),
        sources=[
            BlobSource(
                f"https://127.0.0.1:{runtime.http_port}", token.get_secret_value()
            )
        ],
        dest=tmp_path / "work",
    )
    done = time.monotonic()
    assert staged.blobs_fetched == 1 and staged.bytes_fetched >= SIZE
    out = tmp_path / "work" / "big.bin"
    assert out.stat().st_size == SIZE  # bytes were verified while copying (D84)
    with out.open("rb") as fh:
        for offset in MARKS:
            fh.seek(offset)
            assert fh.read(1) == b"\x5a"
    print(  # noqa: T201 - the timing is part of the evidence (not a benchmark)
        f"\nLARGE hash {uploaded - started:.1f}s incl. upload; "
        f"fetch+copy {done - uploaded:.1f}s; total {done - started:.1f}s"
    )
