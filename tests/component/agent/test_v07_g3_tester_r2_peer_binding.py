"""V0.7 G3 tester round 2 (D155 T-2) against a real TLS peer server: the
binding holds on every route and under concurrency, not just the happy cases
the implementer pinned."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from test_v07_g3_peer_token_binding import _rig

from meshforge.common.hashing import Digest
from meshforge.common.ids import new_node_id

pytestmark = [pytest.mark.component, pytest.mark.asyncio]


async def test_the_tree_manifest_route_is_bound_too(tmp_path: Path) -> None:
    """``digest == claims.tree`` (the manifest itself) is a separate branch in
    the authorizer - the binding must be in front of it."""
    rig = await _rig(tmp_path)
    b, c = str(new_node_id()), str(new_node_id())
    try:
        path = f"/v1/blobs/{rig.tree.hex}"
        token = rig.token(b)
        assert (await rig.get(c, token, path=path)).status_code == 403
        ok = await rig.get(b, token, path=path)
        assert ok.status_code == 200, ok.text
        assert rig.server.stats.refused.get("wrong_requester") == 1
    finally:
        await rig.server.close()


async def test_a_replayed_stolen_token_is_refused_every_time_and_the_owner_still_works(
    tmp_path: Path,
) -> None:
    rig = await _rig(tmp_path)
    b, c = str(new_node_id()), str(new_node_id())
    try:
        token = rig.token(b)
        for _ in range(10):
            assert (await rig.get(c, token)).status_code == 403
        assert rig.server.stats.refused.get("wrong_requester") == 10
        assert (await rig.get(b, token)).status_code == 200
        assert rig.server.stats.served == 1
    finally:
        await rig.server.close()


async def test_no_authorization_header_is_401_not_403(tmp_path: Path) -> None:
    rig = await _rig(tmp_path)
    c = str(new_node_id())
    try:
        import httpx

        from meshforge.common.fabric_tls import peer_server_name

        async with httpx.AsyncClient(verify=rig.ctx(c), timeout=5) as http:
            r = await http.get(
                f"https://127.0.0.1:{rig.server.port}/v1/blobs/{rig.blob.hex}",
                extensions={"sni_hostname": peer_server_name(rig.holder)},
            )
        assert r.status_code == 401
    finally:
        await rig.server.close()


async def test_interleaved_owners_and_thieves_never_cross_over(tmp_path: Path) -> None:
    """Thirty concurrent requests from three enrolled nodes, each presenting a
    token for itself, for one of the others, or for an unheld tree: only the
    own-token ones are served."""
    rig = await _rig(tmp_path)
    nodes = [str(new_node_id()) for _ in range(3)]
    absent = Digest("cd" * 32)
    try:
        jobs = []
        expected = []
        for i in range(30):
            me = nodes[i % 3]
            owner = nodes[(i + (i // 3) % 3) % 3]
            token = rig.token(owner)
            jobs.append(rig.get(me, token))
            expected.append(200 if owner == me else 403)
        # plus a rightful requester asking for a tree this holder lacks: 404
        jobs.append(rig.get(nodes[0], rig.token(nodes[0], tree=absent)))
        expected.append(404)
        responses = await asyncio.gather(*jobs)
        assert [r.status_code for r in responses] == expected
        assert rig.server.stats.served == expected.count(200)
        assert rig.server.stats.refused.get("wrong_requester") == expected.count(403)
    finally:
        await rig.server.close()


async def test_probe_route_is_bound_under_concurrency(tmp_path: Path) -> None:
    rig = await _rig(tmp_path)
    b, c = str(new_node_id()), str(new_node_id())
    path = "/v1/probe?bytes=100"
    try:
        tok_b = rig.probe(b)
        results = await asyncio.gather(
            *[rig.get(b if i % 2 == 0 else c, tok_b, path=path) for i in range(20)]
        )
        assert [r.status_code for r in results] == [
            200 if i % 2 == 0 else 403 for i in range(20)
        ]
        assert rig.server.stats.refused.get("probe_wrong_requester") == 10
    finally:
        await rig.server.close()
