"""V0.6 G1 (roadmap 0.6.10, D116): every classified failure type, caused for real.

* ``oom`` - (a) the kernel OOM killer SIGKILLs a subprocess-runtime workload in
  a memory-limited systemd user scope (``MemoryMax``); (b) a workload whose
  allocation is refused (``RLIMIT_AS`` -> ``MemoryError``), end to end through
  a real controller + agent. The Docker path (container ``OOMKilled``) is
  ``test_docker_sandbox.py::test_the_memory_limit_ends_the_run_as_oom``.
* ``vram_oom`` - a workload that dies printing PyTorch's CUDA out-of-memory
  error (on a CPU-only machine the message is printed by the test workload,
  the only acceptable stand-in per D116), end to end.
* ``disk_full`` - a real 512 KiB tmpfs (a user + mount namespace, no root):
  packing outputs, fetching a bundle and unpacking one into it hit ``ENOSPC``;
  and the env installer reporting it.
* ``env_error`` - a requirements install that cannot succeed (offline
  wheelhouse without that version), end to end: the job fails ``env_error``,
  the terminal report carries the env hash, and the controller now reports
  that node + env as known-bad.
* ``deadline_exceeded`` / ``timeout`` are pre-existing and covered by
  ``test_runtime_contract.py`` (time limit) and ``test_envs_runtimes.py`` /
  the env builder tests (``env_timeout``).
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import textwrap
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_agent_harness import make_controller_settings
from mf_controller_harness import tolerant_liveness
from mf_exec_harness import ExecAgent, submit, wait_job
from mf_jobs_harness import token_of

from meshforge.agent.envs.builder import EnvBuilder, EnvsConfig
from meshforge.agent.envs.errors import EnvError
from meshforge.agent.execution.runtime import RunLimits, RunSpec
from meshforge.agent.execution.subprocess_runtime import (
    SubprocessRuntime,
)
from meshforge.common.errors import FailureType
from meshforge.common.ids import new_attempt_id
from meshforge.controller.jobs.env_health import env_key
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(180)]

LINUX = sys.platform.startswith("linux")
PY = sys.executable


@pytest.fixture
def settings(pg_settings: Any) -> ControllerSettings:
    return make_controller_settings(
        pg_settings,
        jobs={"dispatch_interval_s": 0.1, "backstop_grace_s": 1.0},
        **tolerant_liveness(),
    )


@pytest_asyncio.fixture
async def http(runtime: ControllerRuntime) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        base_url=f"https://127.0.0.1:{runtime.http_port}",
        headers={"authorization": f"Bearer {token_of(runtime)}"},
        timeout=10,
    ) as client:
        yield client


def _project(tmp_path: Path, name: str, main: str, **files: str) -> Path:
    project = tmp_path / name
    project.mkdir()
    (project / "main.py").write_text(main)
    for rel, text in files.items():
        (project / rel).write_text(text)
    return project


# ---------------------------------------------------------------- oom (a)


def _user_scope_ok() -> str | None:
    if not LINUX or shutil.which("systemd-run") is None:
        return "needs Linux with systemd-run"
    probe = subprocess.run(
        ["systemd-run", "--user", "--scope", "-q", "-p", "MemoryMax=64M", "true"],
        capture_output=True,
        timeout=20,
        check=False,
    )
    if probe.returncode != 0:
        return "no systemd user manager with the memory controller delegated"
    return None


async def test_a_kernel_oom_kill_of_a_subprocess_workload_is_oom(
    tmp_path: Path,
) -> None:
    why = _user_scope_ok()
    if why is not None:
        pytest.skip(why)
    env = {
        k: v
        for k in ("PATH", "HOME", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS")
        if (v := os.environ.get(k))
    }
    code = "x = []\nwhile True:\n    x.append(bytearray(16 << 20))\n"
    spec = RunSpec(
        attempt_id=new_attempt_id(),
        argv=(
            "systemd-run",
            "--user",
            "--scope",
            "-q",
            "-p",
            "MemoryMax=96M",
            "-p",
            "MemorySwapMax=0",
            PY,
            "-c",
            code,
        ),
        workdir=tmp_path,
        env=env,
        limits=RunLimits(memory_bytes=96 << 20, pids=64, timeout_s=120),
    )

    async def sink(_stream: object, _data: bytes) -> None:
        return None

    result = await SubprocessRuntime().run(spec, sink)
    assert result.signal == 9, result
    assert result.failure_type is FailureType.OOM, result
    assert result.oom_killed and result.reason == "oom_killed"


# --------------------------------------------- oom (b), vram_oom: end to end

MEMORY_ERROR = textwrap.dedent(
    """
    import resource
    resource.setrlimit(resource.RLIMIT_AS, (400 << 20, 400 << 20))
    hog = []
    while True:
        hog.append(bytearray(32 << 20))
    """
)
CUDA_OOM = textwrap.dedent(
    """
    import sys
    print("training step 1", flush=True)
    sys.stderr.write(
        "Traceback (most recent call last):\\n"
        '  File "train.py", line 7, in <module>\\n'
        "torch.OutOfMemoryError: CUDA out of memory. Tried to allocate 2.00 GiB. "
        "GPU 0 has a total capacity of 3.81 GiB of which 1.10 GiB is free.\\n"
    )
    sys.exit(1)
    """
)


@pytest.mark.skipif(not LINUX, reason="RLIMIT_AS is POSIX (Windows: job object)")
async def test_memory_error_and_cuda_oom_end_to_end(
    http: httpx.AsyncClient, grpc_target: str, data_dir: Path, tmp_path: Path
) -> None:
    agent = await ExecAgent.start(grpc_target, data_dir)
    try:
        oom_job = await submit(
            http,
            _project(tmp_path, "mem", MEMORY_ERROR),
            agent.node_id,
            {"name": "oom", "entrypoint": ["python", "main.py"]},
        )
        job = await wait_job(http, oom_job, timeout_s=60)
        assert job["state"] == "failed" and job["failure_type"] == "oom", job
        vram_job = await submit(
            http,
            _project(tmp_path, "gpu", CUDA_OOM),
            agent.node_id,
            {
                "name": "vram",
                "entrypoint": ["python", "main.py"],
                "retry": {"max_attempts": 2, "backoff_s": 0},
            },
        )
        job = await wait_job(http, vram_job, timeout_s=60)
        assert job["state"] == "failed" and job["failure_type"] == "vram_oom", job
        # vram_oom is retryable (V0.6 D110); a pinned job retries on its own
        # node only, so both attempts hit the same OOM -> retries_exhausted.
        attempts = job["attempts"]
        assert [a["failure_type"] for a in attempts] == ["vram_oom", "vram_oom"]
        assert {a["node_id"] for a in attempts} == {agent.node_id}
        assert "retries_exhausted after 2 attempts" in job["failure_detail"], job
    finally:
        await agent.stop()


# ------------------------------------------------------------- env_error


async def test_env_error_end_to_end_marks_the_node_and_env_known_bad(
    http: httpx.AsyncClient,
    runtime: ControllerRuntime,
    grpc_target: str,
    data_dir: Path,
    tmp_path: Path,
) -> None:
    pytest.importorskip("uv", reason="needs the agent extra (the uv wheel)")
    from mf_envs_support import make_wheel

    wheels = tmp_path / "wheels"
    make_wheel(wheels, "mfbaddemo", "1.0")
    agent = await ExecAgent.start(
        grpc_target,
        data_dir,
        agent_overrides={
            "envs": {
                "find_links": str(wheels),
                "no_index": True,
                "free_disk_floor_mb": 0,
            }
        },
    )
    manifest = {
        "name": "bad-env",
        "entrypoint": ["python", "main.py"],
        "env": {"requirements": "req.txt"},
    }
    try:
        job_id = await submit(
            http,
            _project(
                tmp_path, "env", "print('never')\n", **{"req.txt": "mfbaddemo==9.9\n"}
            ),
            agent.node_id,
            manifest,
        )
        job = await wait_job(http, job_id, timeout_s=90)
    finally:
        await agent.stop()
    assert job["state"] == "failed" and job["failure_type"] == "env_error", job
    assert runtime.known_bad_envs is not None
    entries = runtime.known_bad_envs.for_node(agent.node_id)
    assert len(entries) == 1, entries
    (entry,) = entries
    assert entry.job_id == job_id
    assert entry.env_hash is not None and len(entry.env_hash) == 64
    # the key G2's feasibility will compute from the job row
    assert runtime.known_bad_envs.is_bad(
        agent.node_id,
        env_key(
            manifest, tree_id=job.get("tree_id"), bundle_digest=job["bundle_digest"]
        ),
    )


# ------------------------------------------------------------- disk_full

DISK_CHILD = textwrap.dedent(
    """
    import asyncio, hashlib, json, os, sys
    from pathlib import Path
    import httpx
    from meshforge.agent.execution.fetch import FetchError, fetch_bundle
    from meshforge.agent.execution.outputs import OutputError, collect_outputs
    from meshforge.common.bundle import BundleError, pack_to_file, unpack
    from meshforge.common.hashing import Digest

    full, work = Path(sys.argv[1]), Path(sys.argv[2])
    results = {}
    big = os.urandom(2 << 20)  # 2 MiB of noise: does not compress
    (work / "out").mkdir()
    (work / "out" / "big.bin").write_bytes(big)
    try:
        collect_outputs(work, ("out/",), full / "outputs.bundle", max_bytes=1 << 30)
        results["outputs"] = "stored"
    except OutputError as exc:
        results["outputs"] = exc.failure_type.value

    class Body(httpx.AsyncByteStream):  # a streamed body, like the controller's
        async def __aiter__(self):
            for start in range(0, len(big), 65536):
                yield big[start : start + 65536]

    transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=Body()))
    digest = Digest(hashlib.sha256(big).hexdigest())
    try:
        asyncio.run(
            fetch_bundle(
                "http://controller:1", digest, "tok", full / "b.bundle",
                transport=transport,
            )
        )
        results["fetch"] = "stored"
    except FetchError as exc:
        results["fetch"] = exc.failure_type.value

    packed = pack_to_file(work / "out", work / "p.bundle")
    try:
        unpack(work / "p.bundle", full / "unpacked")
        results["unpack"] = "stored"
    except BundleError as exc:
        results["unpack"] = exc.failure_type.value
    left = sorted(p.name for p in full.iterdir())
    results["left"] = left
    print(json.dumps(results))
    """
)


def _namespace_ok() -> str | None:
    if not LINUX or shutil.which("unshare") is None:
        return "needs Linux with unshare"
    probe = subprocess.run(
        ["unshare", "--user", "--map-root-user", "--mount", "true"],
        capture_output=True,
        timeout=20,
        check=False,
    )
    if probe.returncode != 0:
        return "unprivileged user namespaces are disabled on this machine"
    return None


async def test_enospc_on_a_real_tiny_tmpfs_is_disk_full(tmp_path: Path) -> None:
    why = _namespace_ok()
    if why is not None:
        pytest.skip(why)
    full, work = tmp_path / "full", tmp_path / "work"
    full.mkdir()
    work.mkdir()
    script = tmp_path / "child.py"
    script.write_text(DISK_CHILD)
    shell = (
        'mount -t tmpfs -o size=512k meshforge-test "$1" && exec "$2" "$3" "$1" "$4"'
    )
    done = await asyncio.to_thread(
        subprocess.run,
        [
            "unshare",
            "--user",
            "--map-root-user",
            "--mount",
            "sh",
            "-c",
            shell,
            "sh",
            str(full),
            PY,
            str(script),
            str(work),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    results = json.loads(done.stdout.strip().splitlines()[-1])
    assert results["outputs"] == "disk_full", results
    assert results["fetch"] == "disk_full", results
    assert results["unpack"] == "disk_full", results
    # nothing half-written is left visible (item 14)
    assert results["left"] == [], results


async def test_the_installer_running_out_of_disk_is_disk_full(tmp_path: Path) -> None:
    """The env build: the installer (uv) prints its own ENOSPC message and
    exits non-zero; a real child process stands in for it."""
    (tmp_path / "envs").mkdir()
    builder = EnvBuilder(EnvsConfig(root=tmp_path / "envs", free_disk_floor_bytes=0))
    for text, expected in (
        ("error: Failed to write: No space left on device (os error 28)", "disk_full"),
        ("OSError: [Errno 28] No space left on device", "disk_full"),
        ("ERROR: No matching distribution found for mfdemo==9.9", "setup_failed"),
    ):
        argv = [PY, "-c", f"import sys; print({text!r}, file=sys.stderr); sys.exit(2)"]
        with pytest.raises(EnvError) as err:
            await builder._install(argv, deadline=builder.clock() + 60, on_output=None)
        assert err.value.reason == expected
        if expected == "disk_full":
            assert err.value.failure_type is FailureType.DISK_FULL
        else:
            assert err.value.failure_type is FailureType.ENV_ERROR
