"""V0.5 G1 (D82/D83) over real HTTP + PostgreSQL: have/need, resumable parts,
Range reads, tree-scoped fetch tokens and job submission by ``tree_id``."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from mf_jobs_harness import api_client, g2_settings, running_controller, token_of

from meshforge.common.cas import MIN_CHUNK_SIZE
from meshforge.common.hashing import Digest, sha256_bytes
from meshforge.common.ids import new_node_id
from meshforge.common.tree import build_tree
from meshforge.controller.blobs import MAX_HAVE_DIGESTS
from meshforge.controller.server import ControllerRuntime

pytestmark = pytest.mark.asyncio

CS = MIN_CHUNK_SIZE


@pytest_asyncio.fixture
async def runtime(pg_settings: object) -> AsyncIterator[ControllerRuntime]:
    settings = g2_settings(pg_settings, blobs={"max_blob_bytes": 16 * 1024 * 1024})  # type: ignore[arg-type]
    async with running_controller(settings) as rt:
        yield rt


def _part_headers(size: int, chunk: bytes) -> dict[str, str]:
    return {
        "x-meshforge-blob-size": str(size),
        "x-meshforge-chunk-size": str(CS),
        "x-meshforge-chunk-sha256": hashlib.sha256(chunk).hexdigest(),
    }


async def test_have_need_answers_missing_only(runtime: ControllerRuntime) -> None:
    present = os.urandom(100)
    digest = sha256_bytes(present)
    absent = sha256_bytes(b"absent")
    async with api_client(runtime, token=token_of(runtime)) as client:
        await client.put(f"/v1/blobs/{digest.hex}", content=present)
        got = await client.post(
            "/v1/blobs/have", json={"digests": [str(digest), str(absent)]}
        )
        assert got.status_code == 200, got.text
        assert got.json() == {"missing": [str(absent)]}


async def test_have_need_bounds_and_auth(runtime: ControllerRuntime) -> None:
    many = [f"sha256:{i:064x}" for i in range(MAX_HAVE_DIGESTS + 1)]
    async with api_client(runtime, token=token_of(runtime)) as client:
        ok = await client.post("/v1/blobs/have", json={"digests": many[:-1]})
        assert ok.status_code == 200 and len(ok.json()["missing"]) == MAX_HAVE_DIGESTS
        over = await client.post("/v1/blobs/have", json={"digests": many})
        assert over.status_code == 413
        bad = await client.post("/v1/blobs/have", json={"digests": ["sha256:XYZ"]})
        assert bad.status_code == 400
        extra = await client.post("/v1/blobs/have", json={"digests": [], "x": 1})
        assert extra.status_code == 400
        huge = await client.post(
            "/v1/blobs/have", content=b'{"digests": [' + b" " * (1024 * 1024) + b"]}"
        )
        assert huge.status_code == 413
    async with api_client(runtime) as anonymous:
        refused = await anonymous.post("/v1/blobs/have", json={"digests": []})
        assert refused.status_code == 401


async def test_parts_resume_complete_and_range_reads(
    runtime: ControllerRuntime,
) -> None:
    data = os.urandom(3 * CS + 5)
    digest = sha256_bytes(data)
    chunks = [data[i : i + CS] for i in range(0, len(data), CS)]
    base = f"/v1/blobs/{digest.hex}"
    async with api_client(runtime, token=token_of(runtime)) as client:
        assert (await client.get(f"{base}/parts")).status_code == 404
        for i in (0, 2):
            put = await client.put(
                f"{base}/parts/{i}",
                content=chunks[i],
                headers=_part_headers(len(data), chunks[i]),
            )
            assert put.status_code == 201, put.text
        again = await client.put(
            f"{base}/parts/0",
            content=chunks[0],
            headers=_part_headers(len(data), chunks[0]),
        )
        assert again.status_code == 200 and again.json()["stored"] is False
        status = (await client.get(f"{base}/parts")).json()
        assert status["received"] == [0, 2] and status["complete"] is False
        early = await client.post(f"{base}/complete")
        assert early.status_code == 409
        for i in (1, 3):
            await client.put(
                f"{base}/parts/{i}",
                content=chunks[i],
                headers=_part_headers(len(data), chunks[i]),
            )
        done = await client.post(f"{base}/complete")
        assert done.status_code == 201, done.text
        assert (await client.get(base)).content == data
        ranged = await client.get(base, headers={"range": f"bytes={CS}-{2 * CS - 1}"})
        assert ranged.status_code == 206
        assert ranged.content == chunks[1]
        # Once stored, a part upload is a no-op.
        late = await client.put(
            f"{base}/parts/1",
            content=chunks[1],
            headers=_part_headers(len(data), chunks[1]),
        )
        assert late.status_code == 200


async def test_corrupt_or_misshapen_parts_are_refused(
    runtime: ControllerRuntime,
) -> None:
    data = os.urandom(2 * CS)
    digest = sha256_bytes(data)
    chunk = data[:CS]
    corrupt = bytes([chunk[0] ^ 0xFF]) + chunk[1:]
    base = f"/v1/blobs/{digest.hex}/parts"
    async with api_client(runtime, token=token_of(runtime)) as client:
        bad = await client.put(
            f"{base}/0", content=corrupt, headers=_part_headers(len(data), chunk)
        )
        assert (
            bad.status_code == 400 and bad.json()["error"]["code"] == "integrity_error"
        )
        short = await client.put(
            f"{base}/0", content=chunk[:-1], headers=_part_headers(len(data), chunk)
        )
        assert short.status_code == 400
        out_of_range = await client.put(
            f"{base}/2", content=chunk, headers=_part_headers(len(data), chunk)
        )
        assert out_of_range.status_code == 400
        headers = _part_headers(len(data), chunk) | {"x-meshforge-chunk-size": "1"}
        tiny = await client.put(f"{base}/0", content=chunk, headers=headers)
        assert tiny.status_code == 400
        status = await client.get(f"/v1/blobs/{digest.hex}/parts")
        assert status.status_code == 404 or status.json()["received"] == []
    async with api_client(runtime) as anonymous:
        refused = await anonymous.put(
            f"{base}/0", content=chunk, headers=_part_headers(len(data), chunk)
        )
        assert refused.status_code == 401


def _project(root: Path, files: dict[str, bytes]) -> Path:
    root.mkdir()
    for name, data in files.items():
        (root / name).write_bytes(data)
    return root


async def _upload_tree(client: object, root: Path) -> Digest:
    built = build_tree(root)
    for hex_, path in built.sources.items():
        resp = await client.put(f"/v1/blobs/{hex_}", content=path.read_bytes())  # type: ignore[attr-defined]
        assert resp.status_code in (200, 201)
    resp = await client.put(
        f"/v1/blobs/{built.tree.tree_id.hex}", content=built.manifest
    )  # type: ignore[attr-defined]
    assert resp.status_code in (200, 201)
    return built.tree.tree_id


async def test_fetch_token_scope_is_the_blobs_of_its_tree(
    runtime: ControllerRuntime, tmp_path: Path
) -> None:
    async with api_client(runtime, token=token_of(runtime)) as client:
        tree_a = await _upload_tree(
            client, _project(tmp_path / "a", {"x.txt": b"in A", "y.txt": b"both"})
        )
        tree_b = await _upload_tree(
            client, _project(tmp_path / "b", {"z.txt": b"only in B", "y.txt": b"both"})
        )
    assert runtime.fetch_tokens is not None
    token = runtime.fetch_tokens.mint("attempt_t", [tree_a], ttl_s=60)
    async with api_client(runtime, token=token.get_secret_value()) as agent:
        assert (await agent.get(f"/v1/blobs/{tree_a.hex}")).status_code == 200
        member = sha256_bytes(b"in A")
        got = await agent.get(f"/v1/blobs/{member.hex}")
        assert got.status_code == 200 and got.content == b"in A"
        shared = sha256_bytes(b"both")
        assert (await agent.get(f"/v1/blobs/{shared.hex}")).status_code == 200
        only_b = sha256_bytes(b"only in B")
        assert (await agent.get(f"/v1/blobs/{only_b.hex}")).status_code == 401
        assert (await agent.get(f"/v1/blobs/{tree_b.hex}")).status_code == 401
        assert (await agent.head(f"/v1/blobs/{only_b.hex}")).status_code == 401
        # Download-only (D73): a fetch token never uploads, even a member.
        put = await agent.put(f"/v1/blobs/{member.hex}", content=b"in A")
        assert put.status_code == 401


def _submission(tree_id: str | None, bundle: str | None = None) -> dict[str, object]:
    body: dict[str, object] = {
        "manifest": {"name": "t", "entrypoint": ["python", "main.py"]},
        "placement": {"node_id": str(new_node_id())},
    }
    if tree_id is not None:
        body["tree_id"] = tree_id
    if bundle is not None:
        body["bundle_digest"] = bundle
    return body


async def test_submit_by_tree_checks_the_manifest_and_its_blobs(
    runtime: ControllerRuntime, tmp_path: Path
) -> None:
    project = _project(tmp_path / "p", {"main.py": b"print(1)\n", "d.bin": b"data"})
    built = build_tree(project)
    tree_id = built.tree.tree_id
    async with api_client(runtime, token=token_of(runtime)) as client:
        absent = await client.post("/v1/jobs", json=_submission(str(tree_id)))
        assert absent.status_code == 409 and "not in the blob store" in absent.text
        await client.put(f"/v1/blobs/{tree_id.hex}", content=built.manifest)
        await client.put(
            f"/v1/blobs/{sha256_bytes(b'print(1)' + bytes([10])).hex}",
            content=b"print(1)\n",
        )
        missing = await client.post("/v1/jobs", json=_submission(str(tree_id)))
        assert missing.status_code == 409, missing.text
        assert str(sha256_bytes(b"data")) in missing.json()["error"]["message"]
        await client.put(f"/v1/blobs/{sha256_bytes(b'data').hex}", content=b"data")
        complete = await client.post("/v1/jobs", json=_submission(str(tree_id)))
        # Every blob is there: the request reaches placement (an unknown node).
        assert complete.status_code == 404, complete.text
        garbage = json.dumps({"v": 1}).encode()
        await client.put(f"/v1/blobs/{sha256_bytes(garbage).hex}", content=garbage)
        invalid = await client.post(
            "/v1/jobs", json=_submission(str(sha256_bytes(garbage)))
        )
        assert invalid.status_code == 400, invalid.text
        both = await client.post(
            "/v1/jobs", json=_submission(str(tree_id), bundle=str(tree_id))
        )
        assert both.status_code == 400
        neither = await client.post("/v1/jobs", json=_submission(None))
        assert neither.status_code == 400
