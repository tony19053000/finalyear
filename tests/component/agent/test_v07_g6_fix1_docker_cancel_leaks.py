"""V0.7 G6 fix round 1 (gate): cancelling a Docker run at any moment leaks nothing.

Found while running the tester's ``test_docker_cancel_and_timeout_wipe_the_
host_folder`` 30x under load (2 failures): not a wipe race - every assertion
of that test held - but ``ResourceWarning: unclosed <socket ... raddr=/run/
docker.sock>``. A cancel that arrived while a blocking Docker call ran in its
worker thread dropped that call's result: the client (``open_client``), the
attach socket (``attach_socket``) or - worse - the just-created container.

Real daemon, real containers: one run is cancelled at each of many offsets
across client open / create / attach / start; afterwards there is no unclosed
Docker socket (garbage collection raises no ``ResourceWarning``) and no
container with this test's label.
"""

from __future__ import annotations

import asyncio
import gc
import secrets
import sys
import warnings
from pathlib import Path

import pytest

pytest.importorskip("docker", reason="needs the agent extra (docker SDK)")

from meshforge.agent.execution.docker_runtime import DockerConfig, DockerRuntime
from meshforge.agent.execution.docker_support import open_client
from meshforge.agent.execution.plan import workload_env
from meshforge.agent.execution.runtime import RunLimits, RunSpec, Stream
from meshforge.agent.settings import PYTHON_CPU_IMAGE
from meshforge.common.ids import new_attempt_id, new_job_id

pytestmark = [
    pytest.mark.component,
    pytest.mark.docker,
    pytest.mark.asyncio,
    pytest.mark.timeout(300),
    pytest.mark.skipif(sys.platform == "win32", reason="Linux containers on this host"),
]

OFFSETS_S = [0.0, 0.002, 0.005, 0.01, 0.02, 0.03, 0.05, 0.08, 0.12, 0.2, 0.3, 0.5, 0.9]


def _image_ready() -> bool:
    try:
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


def _containers(label: str) -> list[str]:
    client = open_client(30)
    try:
        return [
            c.name
            for c in client.containers.list(
                all=True, filters={"label": f"meshforge.agent={label}"}
            )
        ]
    finally:
        client.close()


def _spec(tmp_path: Path) -> RunSpec:
    attempt_id = new_attempt_id()
    workdir = tmp_path / "work" / attempt_id
    workdir.mkdir(parents=True)
    workdir.chmod(0o700)
    (workdir / "main.py").write_text(
        "import time\nprint('up', flush=True)\ntime.sleep(60)\n"
    )
    return RunSpec(
        attempt_id=attempt_id,
        argv=("python", "main.py"),
        workdir=workdir,
        env=workload_env(
            runtime="docker",
            workdir=workdir,
            job_id=str(new_job_id()),
            attempt_id=attempt_id,
        ),
        limits=RunLimits(memory_bytes=256 << 20, pids=64, timeout_s=60),
        image=PYTHON_CPU_IMAGE,
        grace_s=0.5,
    )


async def _sink(_stream: Stream, _data: bytes) -> None:
    return None


async def test_a_run_cancelled_at_any_moment_leaks_no_socket_and_no_container(
    tmp_path: Path,
) -> None:
    if not await asyncio.to_thread(_image_ready):
        pytest.skip("Docker or the pinned python-cpu image is not available here")
    label = f"g6f1-{secrets.token_hex(4)}"
    runtime = DockerRuntime(DockerConfig(agent_label=label))
    gc.collect()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", ResourceWarning)
        for offset in OFFSETS_S:
            task = asyncio.ensure_future(runtime.run(_spec(tmp_path), _sink))
            await asyncio.sleep(offset)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        # Late releases (a blocking call that returned after the cancel) run
        # in the background: give them their time, then collect.
        for _ in range(100):
            if not getattr(runtime, "_background", None):
                break
            await asyncio.sleep(0.1)
        await asyncio.sleep(0.5)
        for _ in range(3):
            gc.collect()
            await asyncio.sleep(0.05)
    leaked = [str(w.message) for w in caught if "docker.sock" in str(w.message)]
    assert leaked == [], leaked
    left = []
    for _ in range(100):
        left = await asyncio.to_thread(_containers, label)
        if not left:
            break
        await asyncio.sleep(0.1)
    assert left == [], left
