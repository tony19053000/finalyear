"""V0.5 G1 round 2 tester: non-UTF-8 / lone-surrogate text in every request field
reachable on the blob and job routes must be a 4xx, never a 500 or a hang."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from mf_jobs_harness import api_client, g2_settings, running_controller, token_of

from meshforge.common.cas import MIN_CHUNK_SIZE
from meshforge.common.hashing import sha256_bytes
from meshforge.common.ids import new_node_id
from meshforge.controller.server import ControllerRuntime

pytestmark = pytest.mark.asyncio

CS = MIN_CHUNK_SIZE
H = "a" * 64


@pytest_asyncio.fixture
async def runtime(pg_settings: object) -> AsyncIterator[ControllerRuntime]:
    settings = g2_settings(pg_settings, blobs={"max_blob_bytes": 4 * 1024 * 1024})  # type: ignore[arg-type]
    async with running_controller(settings) as rt:
        yield rt


def _raw(entries: list[dict[str, Any]]) -> bytes:
    return json.dumps(
        {"v": 1, "chunk_size": CS, "entries": entries},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode()


def _job(**over: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "manifest": {"name": "t", "entrypoint": ["python", "main.py"]},
        "placement": {"node_id": str(new_node_id())},
    }
    body.update(over)
    return body


def _bytes_body(body: dict[str, Any]) -> bytes:
    return json.dumps(body, ensure_ascii=True).encode()


async def test_tree_manifest_paths_and_links_with_surrogates_are_400_at_submit(
    runtime: ControllerRuntime,
) -> None:
    cases = [
        [{"path": "a\ud800b", "sha256": H, "size": 1, "exec": False}],
        [{"path": "\udfff", "link": "x"}],
        [{"path": "l", "link": "\ud800"}],
        [{"path": "d\udc80", "dir": True}],
        [{"path": "ok", "link": "x\udc80y"}],
    ]
    async with api_client(runtime, token=token_of(runtime)) as c:
        for entries in cases:
            raw = _raw(entries)
            d = sha256_bytes(raw)
            assert (await c.put(f"/v1/blobs/{d.hex}", content=raw)).status_code in (
                200,
                201,
            )
            r = await c.post("/v1/jobs", json=_job(tree_id=str(d)))
            assert r.status_code == 400, (entries, r.status_code, r.text)
        # The blob API still answers normally afterwards (no poisoned cache).
        assert (await c.head(f"/v1/blobs/{d.hex}")).status_code == 200


async def test_surrogate_manifest_does_not_break_a_fetch_token_read(
    runtime: ControllerRuntime,
) -> None:
    raw = _raw([{"path": "\ud800", "sha256": H, "size": 1, "exec": False}])
    tid = sha256_bytes(raw)
    assert runtime.fetch_tokens is not None
    tok = runtime.fetch_tokens.mint("attempt_u", [tid], ttl_s=60).get_secret_value()
    async with api_client(runtime, token=token_of(runtime)) as admin:
        await admin.put(f"/v1/blobs/{tid.hex}", content=raw)
    async with api_client(runtime, token=tok) as c:
        assert (await c.get(f"/v1/blobs/{'b' * 64}")).status_code == 401
        assert (await c.get(f"/v1/blobs/{tid.hex}")).status_code == 200


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: b["manifest"].update(name="n\ud800"),
        lambda b: b["manifest"].update(entrypoint=["python", "\udc80.py"]),
        lambda b: b["manifest"].update(env={"base": "python-cpu\ud800"}),
        lambda b: b["manifest"].update(outputs=["o\ud800"]),
        lambda b: b.update(tree_id="sha256:\ud800"),
        lambda b: b.update(bundle_digest="sha256:\udfff" + "a" * 60),
        lambda b: b.update(placement={"node_id": "node_\ud800"}),
        lambda b: b.update(placement={"mode": "\ud800"}),
    ],
)
async def test_job_submit_surrogates_in_any_field_never_500(
    runtime: ControllerRuntime, mutate: Any
) -> None:
    body = _job(tree_id="sha256:" + H)
    mutate(body)
    async with api_client(runtime, token=token_of(runtime)) as c:
        r = await c.post(
            "/v1/jobs",
            content=_bytes_body(body),
            headers={"content-type": "application/json"},
        )
        assert 400 <= r.status_code < 500, (r.status_code, r.text)


@pytest.mark.parametrize(
    "raw",
    [
        b'{"manifest":{"name":"\xff\xfe"},"placement":{}}',
        b'{"tree_id":"sha256:\xc3\x28"}',
        b'{"manifest":{"name":"\xed\xa0\x80"}}',  # UTF-8-encoded surrogate
        b"\xff\xff\xff",
    ],
)
async def test_job_submit_raw_invalid_utf8_bodies_are_4xx(
    runtime: ControllerRuntime, raw: bytes
) -> None:
    async with api_client(runtime, token=token_of(runtime)) as c:
        r = await c.post(
            "/v1/jobs", content=raw, headers={"content-type": "application/json"}
        )
        assert 400 <= r.status_code < 500, (r.status_code, r.text)


@pytest.mark.parametrize(
    "raw",
    [
        b'{"digests":["sha256:\xff"]}',
        b'{"digests":["sha256:\\ud800"]}',
        b'{"digests":["\xed\xa0\x80"]}',
        b"\xff",
    ],
)
async def test_have_with_invalid_utf8_is_400(
    runtime: ControllerRuntime, raw: bytes
) -> None:
    async with api_client(runtime, token=token_of(runtime)) as c:
        r = await c.post("/v1/blobs/have", content=raw)
        assert r.status_code in (400, 422), (r.status_code, r.text)


async def test_non_ascii_urls_and_headers_on_blob_routes_never_500(
    runtime: ControllerRuntime,
) -> None:
    good = {
        "x-meshforge-blob-size": "10",
        "x-meshforge-chunk-size": str(CS),
        "x-meshforge-chunk-sha256": H,
    }
    async with api_client(runtime, token=token_of(runtime)) as c:
        for path in (
            "/v1/blobs/%ff",
            "/v1/blobs/%ed%a0%80",
            "/v1/blobs/" + "%c3%a9" * 32,
            f"/v1/blobs/{H}/parts/%ff",
            f"/v1/blobs/{H}/parts/é",
            "/v1/blobs/😀",
        ):
            for method in ("GET", "HEAD", "PUT", "POST"):
                r = await c.request(method, path, content=b"x", headers=good)
                assert r.status_code < 500, (method, path, r.status_code)
        for bad in (b"\xff" * 64, "é".encode() * 32, b"\xed\xa0\x80" * 21 + b"a"):
            r = await c.put(
                f"/v1/blobs/{H}/parts/0",
                content=b"x" * 10,
                headers=[
                    (k.encode(), v.encode())
                    for k, v in good.items()
                    if k != "x-meshforge-chunk-sha256"
                ]
                + [(b"x-meshforge-chunk-sha256", bad)],
            )
            assert 400 <= r.status_code < 500, (bad[:8], r.status_code)
        for rng in (b"bytes=\xff-", b"bytes=0-\xed\xa0\x80", b"\xff"):
            r = await c.get(f"/v1/blobs/{H}", headers=[(b"range", rng)])
            assert r.status_code < 500, (rng, r.status_code)
        r = await c.get(
            f"/v1/blobs/{H}", headers=[(b"authorization", b"Bearer \xff\xfe")]
        )
        assert r.status_code == 401
        r = await c.get(f"/v1/blobs/{H}", headers=[(b"authorization", b"\xed\xa0\x80")])
        assert r.status_code == 401
