"""V0.5 G1 tester: hostile have/need, parts, Range, tokens and manifests over real
HTTP + PostgreSQL. Derived from D82/D83 and success criteria 3 and 4."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections.abc import AsyncIterator
from pathlib import Path
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


@pytest_asyncio.fixture
async def runtime(pg_settings: object) -> AsyncIterator[ControllerRuntime]:
    settings = g2_settings(pg_settings, blobs={"max_blob_bytes": 4 * 1024 * 1024})  # type: ignore[arg-type]
    async with running_controller(settings) as rt:
        yield rt


def _leftovers(root: Path, keep: str) -> list[Path]:
    return [
        p
        for p in root.rglob("*")
        if p.is_file() and p.name != keep and "tmp" in p.parts
    ]


def _hdr(size: int, chunk: bytes, cs: int = CS) -> dict[str, str]:
    return {
        "x-meshforge-blob-size": str(size),
        "x-meshforge-chunk-size": str(cs),
        "x-meshforge-chunk-sha256": hashlib.sha256(chunk).hexdigest(),
    }


def _chunks(data: bytes) -> list[bytes]:
    return [data[i : i + CS] for i in range(0, len(data), CS)] or [b""]


# ------------------------------------------------------------------ have/need


@pytest.mark.parametrize(
    "digests",
    [
        ["SHA256:" + "a" * 64],
        ["sha256:" + "A" * 64],
        ["sha256:" + "a" * 63],
        ["sha256:" + "a" * 65],
        ["a" * 64],
        [""],
        [None],
        [5],
        [["sha256:" + "a" * 64]],
        ["sha256:" + "a" * 64 + "\n"],
        ["sha256:" + "é" * 64],
        ["sha256:" + "a" * 64, "garbage"],
    ],
)
async def test_have_refuses_garbage_digests_without_500(
    runtime: ControllerRuntime, digests: list[Any]
) -> None:
    async with api_client(runtime, token=token_of(runtime)) as c:
        r = await c.post("/v1/blobs/have", json={"digests": digests})
        assert r.status_code == 400, (digests, r.status_code, r.text)


@pytest.mark.parametrize(
    "raw", [b"", b"null", b"[]", b'{"digests":null}', b'{"digests":"x"}', b"{" * 5000]
)
async def test_have_refuses_garbage_bodies(
    runtime: ControllerRuntime, raw: bytes
) -> None:
    async with api_client(runtime, token=token_of(runtime)) as c:
        r = await c.post("/v1/blobs/have", content=raw)
        assert r.status_code in (400, 422), (raw[:20], r.status_code, r.text)


async def test_have_duplicates_and_empty_list(runtime: ControllerRuntime) -> None:
    d = str(sha256_bytes(b"nope"))
    async with api_client(runtime, token=token_of(runtime)) as c:
        assert (await c.post("/v1/blobs/have", json={"digests": []})).json() == {
            "missing": []
        }
        r = await c.post("/v1/blobs/have", json={"digests": [d, d, d]})
        assert r.status_code == 200 and set(r.json()["missing"]) == {d}


async def test_have_body_exactly_at_cap_is_not_a_413_for_size_alone(
    runtime: ControllerRuntime,
) -> None:
    body = b'{"digests": []' + b" " * (1024 * 1024 - len(b'{"digests": []}')) + b"}"
    assert len(body) == 1024 * 1024
    async with api_client(runtime, token=token_of(runtime)) as c:
        r = await c.post("/v1/blobs/have", content=body)
        assert r.status_code == 200, r.text
        r = await c.post("/v1/blobs/have", content=body + b" ")
        assert r.status_code == 413


async def test_have_after_corrupting_a_stored_blob_still_lists_it_present(
    runtime: ControllerRuntime,
) -> None:
    """Documented gap (no controller scrub): have trusts presence. The agent's
    verification is the safety net (test_v05_tree_exec corrupt-blob test)."""
    data = b"hello world"
    d = sha256_bytes(data)
    async with api_client(runtime, token=token_of(runtime)) as c:
        await c.put(f"/v1/blobs/{d.hex}", content=data)
        assert (await c.post("/v1/blobs/have", json={"digests": [str(d)]})).json() == {
            "missing": []
        }


# ---------------------------------------------------------------------- parts


async def test_parts_out_of_order_duplicates_then_complete(
    runtime: ControllerRuntime,
) -> None:
    data = os.urandom(5 * CS + 7)
    d = sha256_bytes(data)
    ch = _chunks(data)
    base = f"/v1/blobs/{d.hex}"
    async with api_client(runtime, token=token_of(runtime)) as c:
        for i in (5, 3, 0, 4, 1, 3, 5):
            r = await c.put(
                f"{base}/parts/{i}", content=ch[i], headers=_hdr(len(data), ch[i])
            )
            assert r.status_code in (200, 201), (i, r.text)
        assert (await c.post(f"{base}/complete")).status_code == 409
        r = await c.put(
            f"{base}/parts/2", content=ch[2], headers=_hdr(len(data), ch[2])
        )
        assert r.status_code == 201
        done = await c.post(f"{base}/complete")
        assert done.status_code == 201, done.text
        assert (await c.get(base)).content == data
        # Completing again is idempotent.
        assert (await c.post(f"{base}/complete")).status_code == 200


async def test_duplicate_part_with_different_bytes_cannot_overwrite_a_good_one(
    runtime: ControllerRuntime,
) -> None:
    data = os.urandom(2 * CS)
    d = sha256_bytes(data)
    ch = _chunks(data)
    base = f"/v1/blobs/{d.hex}"
    evil = os.urandom(CS)
    async with api_client(runtime, token=token_of(runtime)) as c:
        assert (
            await c.put(
                f"{base}/parts/0", content=ch[0], headers=_hdr(len(data), ch[0])
            )
        ).status_code == 201
        # Self-consistent (header matches the evil bytes) but a different chunk 0.
        r = await c.put(f"{base}/parts/0", content=evil, headers=_hdr(len(data), evil))
        await c.put(f"{base}/parts/1", content=ch[1], headers=_hdr(len(data), ch[1]))
        done = await c.post(f"{base}/complete")
        # Either the evil overwrite was ignored (201, correct blob) or the whole
        # hash caught it (400) - the stored blob must never be wrong.
        if done.status_code == 201:
            assert (await c.get(base)).content == data
        else:
            assert done.status_code == 400, (r.status_code, done.text)
            assert (await c.get(base)).status_code == 404


async def test_complete_with_wrong_whole_digest_stores_nothing(
    runtime: ControllerRuntime,
) -> None:
    """Every part matches its own header digest, but the blob's name is a lie."""
    data = os.urandom(2 * CS)
    liar = sha256_bytes(b"something else entirely")
    ch = _chunks(data)
    base = f"/v1/blobs/{liar.hex}"
    async with api_client(runtime, token=token_of(runtime)) as c:
        for i in range(2):
            assert (
                await c.put(
                    f"{base}/parts/{i}", content=ch[i], headers=_hdr(len(data), ch[i])
                )
            ).status_code == 201
        done = await c.post(f"{base}/complete")
        assert (
            done.status_code == 400
            and done.json()["error"]["code"] == "integrity_error"
        )
        assert (await c.get(base)).status_code == 404
        assert (await c.head(base)).status_code == 404
        assert (
            await c.post("/v1/blobs/have", json={"digests": [str(liar)]})
        ).json() == {"missing": [str(liar)]}
        # "the upload starts over": a retry of complete must not 500.
        again = await c.post(f"{base}/complete")
        assert again.status_code in (404, 409, 400), again.text


async def test_parts_reshaping_mid_upload_never_stores_a_wrong_blob(
    runtime: ControllerRuntime,
) -> None:
    data = os.urandom(3 * CS)
    d = sha256_bytes(data)
    ch = _chunks(data)
    base = f"/v1/blobs/{d.hex}"
    async with api_client(runtime, token=token_of(runtime)) as c:
        assert (
            await c.put(
                f"{base}/parts/0", content=ch[0], headers=_hdr(len(data), ch[0])
            )
        ).status_code == 201
        # same digest, different declared size
        r = await c.put(
            f"{base}/parts/1", content=ch[1], headers=_hdr(len(data) + CS, ch[1])
        )
        assert r.status_code < 500, r.text
        # same digest, different chunk size (2*CS) with a body of 2*CS
        big = data[: 2 * CS]
        r = await c.put(
            f"{base}/parts/0", content=big, headers=_hdr(len(data), big, 2 * CS)
        )
        assert r.status_code < 500, r.text
        # Finish honestly: the final blob must be right or absent.
        for i in range(3):
            await c.put(
                f"{base}/parts/{i}", content=ch[i], headers=_hdr(len(data), ch[i])
            )
        done = await c.post(f"{base}/complete")
        assert done.status_code < 500, done.text
        got = await c.get(base)
        assert got.status_code == 404 or got.content == data


async def test_part_shape_edge_cases(runtime: ControllerRuntime) -> None:
    async with api_client(runtime, token=token_of(runtime)) as c:
        zero = sha256_bytes(b"")
        r = await c.put(
            f"/v1/blobs/{zero.hex}/parts/0", content=b"", headers=_hdr(0, b"")
        )
        assert r.status_code < 500, r.text
        # size over the cap (4 MiB in this fixture): max+1
        d = sha256_bytes(b"x")
        r = await c.put(
            f"/v1/blobs/{d.hex}/parts/0",
            content=b"x" * CS,
            headers=_hdr(4 * 1024 * 1024 + 1, b"x" * CS),
        )
        assert r.status_code == 413
        # index far out of range, negative, not a number
        for idx in ("-1", "99999999999", "abc", "1048576"):
            r = await c.put(
                f"/v1/blobs/{d.hex}/parts/{idx}",
                content=b"x",
                headers=_hdr(1, b"x"),
            )
            assert r.status_code in (400, 404, 422), (idx, r.status_code)

        # missing Content-Length (chunked body)
        async def gen() -> AsyncIterator[bytes]:
            yield b"x" * CS

        r = await c.put(
            f"/v1/blobs/{d.hex}/parts/0", content=gen(), headers=_hdr(2 * CS, b"x" * CS)
        )
        assert r.status_code in (400, 411), r.status_code
        # non-hex chunk digest, upper-case digest, missing headers
        h = _hdr(CS, b"x" * CS) | {"x-meshforge-chunk-sha256": "A" * 64}
        assert (
            await c.put(f"/v1/blobs/{d.hex}/parts/0", content=b"x" * CS, headers=h)
        ).status_code == 400
        assert (
            await c.put(f"/v1/blobs/{d.hex}/parts/0", content=b"x" * CS)
        ).status_code == 400
        # complete / status for something never started
        assert (await c.post(f"/v1/blobs/{d.hex}/complete")).status_code == 404
        assert (await c.get(f"/v1/blobs/{d.hex}/parts")).status_code == 404


async def test_concurrent_identical_whole_uploads(runtime: ControllerRuntime) -> None:
    data = os.urandom(1 << 20)
    d = sha256_bytes(data)
    async with api_client(runtime, token=token_of(runtime)) as c:
        rs = await asyncio.gather(
            *[c.put(f"/v1/blobs/{d.hex}", content=data) for _ in range(10)]
        )
        assert all(r.status_code in (200, 201, 503) for r in rs), [
            r.status_code for r in rs
        ]
        assert any(r.status_code in (200, 201) for r in rs)
        assert (await c.get(f"/v1/blobs/{d.hex}")).content == data
    cas_root = runtime.blobs.cas.root  # type: ignore[union-attr]
    leftovers = await asyncio.to_thread(_leftovers, Path(cas_root), d.hex)
    assert not leftovers, leftovers


async def test_concurrent_part_uploads_same_and_different_and_complete(
    runtime: ControllerRuntime,
) -> None:
    data = os.urandom(6 * CS + 1)
    d = sha256_bytes(data)
    ch = _chunks(data)
    base = f"/v1/blobs/{d.hex}"
    async with api_client(runtime, token=token_of(runtime)) as c:
        jobs = [
            c.put(f"{base}/parts/{i}", content=ch[i], headers=_hdr(len(data), ch[i]))
            for i in range(len(ch))
            for _ in range(3)
        ]
        rs = await asyncio.gather(*jobs)
        assert all(r.status_code in (200, 201, 503) for r in rs), [
            r.status_code for r in rs
        ]
        for i in range(len(ch)):  # retry any 503
            await c.put(
                f"{base}/parts/{i}", content=ch[i], headers=_hdr(len(data), ch[i])
            )
        done = await asyncio.gather(*[c.post(f"{base}/complete") for _ in range(4)])
        # A duplicate complete racing the winner may see 404 or 409 (the sidecar is gone
        # a moment before the size is visible): a minor defect, reported. Once
        # the winner is done, complete must be an idempotent 200.
        assert all(r.status_code in (200, 201) for r in done), [r.text for r in done]
        assert any(r.status_code == 201 for r in done)
        assert (await c.post(f"{base}/complete")).status_code == 200
        assert (await c.get(base)).content == data


# ---------------------------------------------------------------------- Range


async def test_range_requests_never_500_and_never_wrong(
    runtime: ControllerRuntime,
) -> None:
    data = os.urandom(1000)
    d = sha256_bytes(data)
    async with api_client(runtime, token=token_of(runtime)) as c:
        await c.put(f"/v1/blobs/{d.hex}", content=data)
        url = f"/v1/blobs/{d.hex}"
        r = await c.get(url, headers={"range": "bytes=1000-1010"})
        assert r.status_code == 416, r.status_code
        r = await c.get(url, headers={"range": "bytes=5000-"})
        assert r.status_code == 416
        r = await c.get(url, headers={"range": "bytes=990-5000"})
        assert r.status_code == 206 and r.content == data[990:]
        r = await c.get(url, headers={"range": "bytes=-10"})
        assert r.status_code == 206 and r.content == data[-10:]
        r = await c.get(url, headers={"range": "bytes=0-0"})
        assert r.status_code == 206 and r.content == data[:1]
        r = await c.get(url, headers={"range": "bytes=999-999"})
        assert r.status_code == 206 and r.content == data[999:]
        for bad in (
            "bytes=5-2",
            "bytes=a-b",
            "items=0-1",
            "bytes=",
            "bytes=0-1,5-9",
            "bytes=-0",
            "bytes=99999999999999999999-",
        ):
            r = await c.get(url, headers={"range": bad})
            assert r.status_code in (200, 206, 400, 416), (bad, r.status_code)
            if r.status_code == 206:
                assert len(r.content) <= len(data)
            if r.status_code == 200:
                assert r.content == data
        # a Range on an absent blob is a 404, not a 416/500
        r = await c.get(f"/v1/blobs/{'0' * 64}", headers={"range": "bytes=0-1"})
        assert r.status_code == 404


async def test_range_on_empty_blob(runtime: ControllerRuntime) -> None:
    d = sha256_bytes(b"")
    async with api_client(runtime, token=token_of(runtime)) as c:
        await c.put(f"/v1/blobs/{d.hex}", content=b"")
        r = await c.get(f"/v1/blobs/{d.hex}", headers={"range": "bytes=0-0"})
        assert r.status_code in (200, 416), r.status_code


# --------------------------------------------------------------------- tokens


async def test_fetch_token_cannot_touch_any_v05_write_endpoint(
    runtime: ControllerRuntime,
) -> None:
    data = os.urandom(2 * CS)
    d = sha256_bytes(data)
    assert runtime.fetch_tokens is not None
    tok = runtime.fetch_tokens.mint("attempt_x", [d], ttl_s=60).get_secret_value()
    ch = _chunks(data)
    async with api_client(runtime, token=token_of(runtime)) as admin:
        await admin.put(f"/v1/blobs/{d.hex}", content=data)
    async with api_client(runtime, token=tok) as c:
        base = f"/v1/blobs/{d.hex}"
        assert (await c.get(base, headers={"range": "bytes=0-9"})).status_code == 206
        assert (await c.post("/v1/blobs/have", json={"digests": []})).status_code == 401
        assert (
            await c.put(
                f"{base}/parts/0", content=ch[0], headers=_hdr(len(data), ch[0])
            )
        ).status_code == 401
        assert (await c.get(f"{base}/parts")).status_code == 401
        assert (await c.post(f"{base}/complete")).status_code == 401
        assert (await c.put(base, content=data)).status_code == 401
        assert (await c.post("/v1/jobs", json={})).status_code in (401, 403)
    async with api_client(runtime, token="garbage") as c:
        assert (await c.get(f"/v1/blobs/{d.hex}")).status_code == 401


async def test_token_for_a_bundle_blob_does_not_widen_to_other_blobs(
    runtime: ControllerRuntime,
) -> None:
    """A token for a non-tree blob (even valid JSON that names other blobs, but is
    not a canonical manifest) must not grant its 'members'."""
    other = b"victim data"
    od = sha256_bytes(other)
    fake = json.dumps(
        {
            "v": 1,
            "chunk_size": CS,
            "entries": [
                {"path": "a", "sha256": od.hex, "size": len(other), "exec": False}
            ],
        }
    ).encode()  # not canonical (spaces)
    fd = sha256_bytes(fake)
    assert runtime.fetch_tokens is not None
    tok = runtime.fetch_tokens.mint("attempt_y", [fd], ttl_s=60).get_secret_value()
    async with api_client(runtime, token=token_of(runtime)) as admin:
        await admin.put(f"/v1/blobs/{od.hex}", content=other)
        await admin.put(f"/v1/blobs/{fd.hex}", content=fake)
    async with api_client(runtime, token=tok) as c:
        assert (await c.get(f"/v1/blobs/{fd.hex}")).status_code == 200
        assert (await c.get(f"/v1/blobs/{od.hex}")).status_code == 401


async def test_expired_and_revoked_fetch_tokens_are_refused(
    runtime: ControllerRuntime,
) -> None:
    data = b"abc"
    d = sha256_bytes(data)
    assert runtime.fetch_tokens is not None
    tok = runtime.fetch_tokens.mint("attempt_z", [d], ttl_s=60).get_secret_value()
    async with api_client(runtime, token=token_of(runtime)) as admin:
        await admin.put(f"/v1/blobs/{d.hex}", content=data)
    runtime.fetch_tokens.revoke("attempt_z")
    async with api_client(runtime, token=tok) as c:
        assert (await c.get(f"/v1/blobs/{d.hex}")).status_code == 401


# ------------------------------------------------------------ hostile manifests


def _sub(tree_id: str) -> dict[str, object]:
    return {
        "manifest": {"name": "t", "entrypoint": ["python", "main.py"]},
        "tree_id": tree_id,
        "placement": {"node_id": str(new_node_id())},
    }


async def _put_blob(c: Any, data: bytes) -> str:
    d = sha256_bytes(data)
    r = await c.put(f"/v1/blobs/{d.hex}", content=data)
    assert r.status_code in (200, 201), r.text
    return str(d)


def _m(entries: list[dict[str, Any]], cs: int = CS, ascii_: bool = False) -> bytes:
    return json.dumps(
        {"v": 1, "chunk_size": cs, "entries": entries},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=ascii_,
    ).encode()


@pytest.mark.parametrize(
    "entries",
    [
        [{"path": "../x", "sha256": "a" * 64, "size": 1, "exec": False}],
        [{"path": "/etc/passwd", "sha256": "a" * 64, "size": 1, "exec": False}],
        [{"path": "a", "sha256": "a" * 64, "size": 1, "exec": False}] * 2,
        [{"path": "l", "link": "../../../etc"}],
        [
            {
                "path": "a",
                "sha256": "a" * 64,
                "size": CS + 1,
                "exec": False,
                "chunks": ["a" * 64],
            }
        ],
    ],
)
async def test_submit_hostile_manifest_is_a_400_never_a_500(
    runtime: ControllerRuntime, entries: list[dict[str, Any]]
) -> None:
    async with api_client(runtime, token=token_of(runtime)) as c:
        tid = await _put_blob(c, _m(entries))
        r = await c.post("/v1/jobs", json=_sub(tid))
        assert r.status_code == 400, r.text


async def test_submit_manifest_with_lone_surrogate_path_is_a_400_never_a_500(
    runtime: ControllerRuntime,
) -> None:
    raw = _m(
        [{"path": "\ud800", "sha256": "a" * 64, "size": 1, "exec": False}], ascii_=True
    )
    async with api_client(runtime, token=token_of(runtime)) as c:
        tid = await _put_blob(c, raw)
        r = await c.post("/v1/jobs", json=_sub(tid))
        assert r.status_code == 400, (r.status_code, r.text)


async def test_fetch_token_for_a_surrogate_manifest_does_not_crash_the_blob_api(
    runtime: ControllerRuntime,
) -> None:
    raw = _m(
        [{"path": "\ud800", "sha256": "a" * 64, "size": 1, "exec": False}], ascii_=True
    )
    tid = sha256_bytes(raw)
    assert runtime.fetch_tokens is not None
    tok = runtime.fetch_tokens.mint("attempt_s", [tid], ttl_s=60).get_secret_value()
    async with api_client(runtime, token=token_of(runtime)) as admin:
        await admin.put(f"/v1/blobs/{tid.hex}", content=raw)
    async with api_client(runtime, token=tok) as c:
        r = await c.get(f"/v1/blobs/{'b' * 64}")
        assert r.status_code == 401, r.status_code
        assert (await c.get(f"/v1/blobs/{tid.hex}")).status_code == 200


async def test_submit_with_a_corrupted_stored_manifest_is_refused_cleanly(
    runtime: ControllerRuntime,
) -> None:
    good = _m(
        [
            {
                "path": "main.py",
                "sha256": sha256_bytes(b"x").hex,
                "size": 1,
                "exec": False,
            }
        ]
    )
    async with api_client(runtime, token=token_of(runtime)) as c:
        tid = await _put_blob(c, good)
        await _put_blob(c, b"x")
        assert runtime.blobs is not None
        p = runtime.blobs.path_for(sha256_bytes(good))
        p.chmod(0o600)
        p.write_bytes(good.replace(b"main.py", b"main.pz"))  # same size
        runtime.blobs._trees.clear()  # type: ignore[attr-defined]
        r = await c.post("/v1/jobs", json=_sub(tid))
        assert 400 <= r.status_code < 500, (r.status_code, r.text)


async def test_submit_tree_id_of_a_non_json_blob_and_of_a_huge_manifest(
    runtime: ControllerRuntime,
) -> None:
    async with api_client(runtime, token=token_of(runtime)) as c:
        tid = await _put_blob(c, os.urandom(2000))
        r = await c.post("/v1/jobs", json=_sub(tid))
        assert r.status_code == 400, r.text
        entries = [
            {"path": f"f{i:07d}", "sha256": "a" * 64, "size": 1, "exec": False}
            for i in range(200_000)
        ]
        huge = _m(entries)
        if len(huge) <= 4 * 1024 * 1024:
            tid = await _put_blob(c, huge)
            r = await c.post("/v1/jobs", json=_sub(tid))
            assert 400 <= r.status_code < 500, r.status_code
