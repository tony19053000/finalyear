"""V0.7 G6 tester r1, real Docker: a hostile container looking for secrets.

The file ``/run/secrets/NAME`` is the only place a value exists: not in the
container's environment, command, labels, image history or writable layer
(``docker inspect`` / a filesystem scan from inside), not in the host workdir
or the agent's staged bundle; two concurrent jobs see only their own names; the
job cannot write the mount; the host folder is gone (zeroed) afterwards.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_agent_harness import make_controller_settings
from mf_controller_harness import USERS_ONLY, tolerant_liveness
from mf_exec_harness import ExecAgent, submit, wait_job
from mf_users import bearer, create_user, login

from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.timeout(300),
    pytest.mark.docker,
    pytest.mark.skipif(sys.platform == "win32", reason="Linux containers"),
]

V1 = b"docker-r1-ONE-secret-0123456789"
V2 = b"docker-r1-TWO-secret-9876543210"

HOSTILE = r"""
import json, os, sys, time
mine = sys.argv[1]
me = open("/run/secrets/" + mine, "rb").read()
found = []
for root in ("/", ):
    for dp, dn, fn in os.walk(root):
        if dp.startswith(("/proc", "/sys", "/dev", "/run/secrets")):
            dn[:] = []
            continue
        for f in fn:
            p = os.path.join(dp, f)
            try:
                if os.path.getsize(p) < 5_000_000 and me in open(p, "rb").read():
                    found.append(p)
            except OSError:
                pass
# the other job's name must not exist here, nor anything beside /run/secrets
listing = sorted(os.listdir("/run/secrets"))
try:
    os.listdir("/dev/shm/")
    shm = sorted(os.listdir("/dev/shm/"))
except OSError:
    shm = None
writes = {}
for target in ("/run/secrets/NEW", "/run/secrets/" + mine):
    try:
        open(target, "wb").write(b"x")
        writes[target] = "WRITABLE"
    except OSError:
        writes[target] = "denied"
try:
    os.remove("/run/secrets/" + mine)
    writes["remove"] = "REMOVED"
except OSError:
    writes["remove"] = "denied"
env_hits = [k for k, v in os.environ.items() if me.decode() in v]
json.dump({"found": found, "listing": listing, "shm": shm, "writes": writes,
           "env_hits": env_hits, "cmdline": open("/proc/self/cmdline").read()},
          open("/tmp/report.json", "w"))
print(open("/tmp/report.json").read(), flush=True)
print("." * 400, flush=True)  # the redactor holds back the last bytes (T-3)
open(sys.argv[2], "w").write("up")  # not visible on the host; marker via log only
time.sleep(float(sys.argv[3]))
"""


def _docker_ready() -> bool:
    try:
        from meshforge.agent.execution.docker_support import open_client
        from meshforge.agent.settings import PYTHON_CPU_IMAGE

        client = open_client(10)
    except Exception:  # noqa: BLE001
        return False
    try:
        client.images.get(PYTHON_CPU_IMAGE)
    except Exception:  # noqa: BLE001
        return False
    finally:
        client.close()
    return True


@pytest.fixture
def settings(pg_settings: DatabaseSettings) -> ControllerSettings:
    return make_controller_settings(
        pg_settings,
        auth={**USERS_ONLY, "login_rate_per_minute": 600.0, "login_rate_burst": 100},
        jobs={"dispatch_interval_s": 0.1},
        **tolerant_liveness(),
    )


@pytest_asyncio.fixture
async def alice(
    pg_settings: DatabaseSettings, runtime: ControllerRuntime
) -> AsyncIterator[httpx.AsyncClient]:
    engine = create_engine(pg_settings)
    try:
        await create_user(engine, "alice", "alice-password-1")
    finally:
        await engine.dispose()
    base = f"https://127.0.0.1:{runtime.http_port}"
    async with httpx.AsyncClient(base_url=base, timeout=30) as anon:
        token = await login(anon, "alice", "alice-password-1")
    async with httpx.AsyncClient(
        base_url=base, headers=bearer(token), timeout=30
    ) as client:
        assert (await client.put("/v1/secrets/ONE", content=V1)).is_success
        assert (await client.put("/v1/secrets/TWO", content=V2)).is_success
        yield client


def _inspect_all() -> str:
    ids = subprocess.run(
        ["docker", "ps", "-q"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.split()
    out = ""
    for cid in ids:
        out += subprocess.run(
            ["docker", "inspect", cid],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
    return out


def _tree_bytes(root: Path) -> list[tuple[str, bytes]]:
    items = []
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink():
            try:
                items.append((str(path), path.read_bytes()))
            except OSError:
                continue
    return items


async def _report(alice: httpx.AsyncClient, job_id: str) -> dict[str, object]:
    for _ in range(600):
        logs = await alice.get(f"/v1/jobs/{job_id}/logs", params={"stream": "stdout"})
        for line in logs.content.decode("utf-8", "replace").splitlines():
            if line.startswith("{") and line.endswith("}"):
                return dict(json.loads(line))
        await asyncio.sleep(0.1)
    raise AssertionError("the container never reported")


async def test_two_hostile_containers_find_only_their_own_secret_file(
    alice: httpx.AsyncClient, grpc_target: str, tmp_path: Path
) -> None:
    if not await asyncio.to_thread(_docker_ready):
        pytest.skip("Docker or the pinned python-cpu image is not available here")
    agent_dir = tmp_path / "agent"
    agent = await ExecAgent.start(
        grpc_target,
        agent_dir,
        agent_overrides={
            "policy": {
                "accept_jobs": True,
                "allow_unsandboxed": False,
                "max_cpu_jobs": 2,
            }
        },
        docker=True,
    )
    try:
        base = agent.executor.vault.base
        project = tmp_path / "proj"
        project.mkdir()
        (project / "main.py").write_text(HOSTILE)
        ids = {}
        for name in ("ONE", "TWO"):
            ids[name] = await submit(
                alice,
                project,
                agent.node_id,
                {
                    "name": f"r1-{name}",
                    "entrypoint": ["python", "main.py", name, "/tmp/up", "20"],
                    "secrets": [name],
                    "retry": {"max_attempts": 1},
                },
            )
        reports = {n: await _report(alice, j) for n, j in ids.items()}
        # While both containers are alive: the host side.
        inspected = await asyncio.to_thread(_inspect_all)
        folders = list(base.iterdir())
        assert len(folders) == 2
        host_files = await asyncio.to_thread(_tree_bytes, agent_dir)
        for value in (V1, V2):
            assert value.decode() not in inspected
            assert value.hex() not in inspected
            for path, content in host_files:
                assert value not in content, path
        for name, report in reports.items():
            other = "TWO" if name == "ONE" else "ONE"
            assert report["found"] == [], report["found"]
            assert report["listing"] == [name], report["listing"]
            assert other not in json.dumps(report)
            assert report["env_hits"] == []
            writes = report["writes"]
            assert isinstance(writes, dict) and "WRITABLE" not in writes.values()
            assert writes.get("remove") == "denied"
            assert report["shm"] in (None, []), report["shm"]
        for job_id in ids.values():
            job = await wait_job(alice, job_id, timeout_s=90)
            assert job["state"] == "succeeded", job
        assert list(base.iterdir()) == []
    finally:
        await agent.stop()
