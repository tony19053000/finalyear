# ruff: noqa: F811  (pytest fixtures imported from the peer-fetch test module)
"""V0.5 G4 tester round 1: whole-system checks with a real controller + agents.

* an old V0.4 bundle job still runs (and fetches from the controller only) with
  peer servers switched on;
* a token signed with the controller's OLD key is refused by a holder once the
  controller restarted with a new key (and the new key's tokens work);
* a blob removed behind the agent's cache (not through the CAS) - the job still
  succeeds; the index lie is reported by DEFECT-3 below.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_exec_harness import submit, wait_job
from test_v05_g4_peer_fetch import (  # noqa: F401  (fixtures + helpers)
    _a_holds,
    _http,
    _project,
    _run_on_b,
    _settings,
    _upload,
    agents,
    ctl_link,
    fault_loop,
    settings,
    wired,
)

from meshforge.agent.session.state import AgentState
from meshforge.common.ids import new_attempt_id, new_node_id
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(180)]


async def test_v04_bundle_job_still_runs_with_peers_enabled_controller_only_source(
    wired: ControllerRuntime, agents: tuple[Any, Any], tmp_path: Path
) -> None:
    a, b = agents
    proj = tmp_path / "bundle-proj"
    proj.mkdir()
    (proj / "main.py").write_text(
        "import sys\nopen(sys.argv[1], 'w').write('v04 ok')\n"
    )
    for agent, name in ((a, "a"), (b, "b")):
        out = tmp_path / f"{name}.out"
        async with _http(wired) as http:
            job_id = await submit(
                http,
                proj,
                agent.node_id,
                {"name": "v04", "entrypoint": ["python", "main.py", str(out)]},
            )
            job = await wait_job(http, job_id, timeout_s=60)
        assert job["state"] == "succeeded", job
        assert out.read_text() == "v04 ok"
        stage = agent.executor.last_stage
        if stage is not None:
            assert set(stage.sources) <= {"controller"}, stage.sources
    # A bundle is not a tree: the agents never advertise a tree for it.
    assert wired.cache_index is not None
    for agent in (a, b):
        summary = await wired.cache_index.store.summary(agent.node_id)
        assert summary.get("tree", (0, 0))[0] == 0


async def test_old_key_token_refused_after_controller_key_change(
    wired: ControllerRuntime,
    agents: tuple[Any, Any],
    pg_settings: DatabaseSettings,
    ctl_link: Any,
    tmp_path: Path,
) -> None:
    a, _b = agents
    built, _data = _project(tmp_path / "p")
    await _upload(wired, built)
    await _a_holds(wired, a, built, tmp_path / "a.out")
    assert wired.peer_tokens is not None and a.peer is not None
    member = next(e.digest for e in built.tree.files())
    base = f"http://127.0.0.1:{a.peer.port}/v1/blobs/{member.hex}"

    def issue(rt: ControllerRuntime) -> str:
        assert rt.peer_tokens is not None
        return rt.peer_tokens.issue(
            tree=built.tree.tree_id,
            requester=new_node_id(),  # type: ignore[arg-type]
            holder=a.node_id,
            attempt_id=new_attempt_id(),
        )

    old = issue(wired)

    async def status(tok: str) -> int:
        async with httpx.AsyncClient(trust_env=False, timeout=10) as c:
            r = await c.get(base, headers={"authorization": f"Bearer {tok}"})
            return r.status_code

    assert await status(old) == 200
    grpc_port, http_port = wired.grpc_port, wired.http_port
    key_path = wired.settings.peer_key_path
    await wired.stop()
    key_path.chmod(0o600)
    os.remove(key_path)  # the operator lost / rotated the signing key
    fresh_settings = _settings(
        pg_settings,
        ctl_link,
        grpc_bind=f"127.0.0.1:{grpc_port}",
        http_bind=f"127.0.0.1:{http_port}",
    )
    async with ControllerRuntime.start(fresh_settings) as fresh:
        await a.client.wait_for_state(AgentState.ONLINE, timeout_s=30)
        # Until A's new session installs the new key, A still trusts the old one
        # (it has no other); then the old token must die.
        end = time.monotonic() + 20
        while time.monotonic() < end and await status(old) == 200:  # noqa: ASYNC110
            await asyncio.sleep(0.1)
        assert await status(old) == 403, "an old-key token is still accepted"
        assert await status(issue(fresh)) == 200
        assert fresh.peer_tokens is not None
        assert wired.peer_tokens is not fresh.peer_tokens


async def test_blob_deleted_behind_the_cache_job_still_succeeds(
    wired: ControllerRuntime, agents: tuple[Any, Any], tmp_path: Path
) -> None:
    a, b = agents
    built, data = _project(tmp_path / "p")
    await _upload(wired, built)
    await _a_holds(wired, a, built, tmp_path / "a.out")
    big = next(e.digest for e in built.tree.files() if e.chunks)
    path = a.executor.cas.store.path_for(big)
    path.chmod(0o600)
    path.unlink()  # not through the CAS: no on_change, no delta
    out = tmp_path / "b.out"
    job = await _run_on_b(wired, b, built, out)
    assert job["state"] == "succeeded", job
    assert out.read_text() == hashlib.sha256(data).hexdigest()
    stage = b.executor.last_stage
    assert stage is not None
    # A could not serve the vanished blob; the controller did.
    assert stage.sources["controller"].units >= 1


@pytest.mark.xfail(
    strict=True,
    reason="DEFECT-3: a blob removed behind the agent's CAS stays in the "
    "controller's index across every periodic resnapshot (snapshots are built "
    "from memory, never re-listing the CAS); only an agent restart repairs it",
)
async def test_resnapshot_repairs_an_index_entry_for_a_blob_deleted_behind_the_cache(
    wired: ControllerRuntime, agents: tuple[Any, Any], tmp_path: Path
) -> None:
    a, _b = agents
    built, _data = _project(tmp_path / "p")
    await _upload(wired, built)
    await _a_holds(wired, a, built, tmp_path / "a.out")
    big = next(e.digest for e in built.tree.files() if e.chunks)
    path = a.executor.cas.store.path_for(big)
    path.chmod(0o600)
    path.unlink()
    assert a.cache is not None
    await a.cache.send_snapshot()  # what the periodic resnapshot does
    await asyncio.sleep(1.0)
    assert wired.cache_index is not None
    store = wired.cache_index.store
    assert await store.holders("blob", str(big), among=[a.node_id]) == []
    assert await store.holders("tree", str(built.tree.tree_id), among=[a.node_id]) == []
