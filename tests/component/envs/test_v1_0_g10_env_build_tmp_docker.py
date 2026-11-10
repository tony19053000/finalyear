"""V1.0 G10 BUG-1 (D442, lab L1 G9-B c): a Docker env build ran out of space.

The ``envbuild`` container mounted ``/tmp`` as the job's tmpfs
(``[agent.execution] tmp_size_mb``, 512 MB) and pip unpacks every wheel into
``$TMPDIR`` before it moves it into ``--target /env``: torch (554 MB wheel)
failed ``[Errno 28] No space left on device`` with 21 GB free on the disk.

Here the tmpfs is 16 MB and the wheel unpacks to ~40 MB: the build must
succeed (its temp files are on the container layer, i.e. the Docker disk),
the job's ``/tmp`` stays the sized tmpfs, and the committed env image does not
hand the build's temp folder to the job (``TMPDIR`` is ``/tmp`` again: the
build folder is on the run container's read-only root filesystem).
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import secrets
import sys
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip("docker", reason="needs the agent extra (docker SDK)")

from mf_envs_support import offline_builder

from meshforge.agent.execution.plan import workload_env
from meshforge.agent.execution.runtime import (
    EnvRequest,
    RunLimits,
    RunResult,
    RunSpec,
    Stream,
)
from meshforge.common.ids import new_attempt_id, new_job_id

pytestmark = [
    pytest.mark.docker,
    pytest.mark.timeout(300),
    pytest.mark.skipif(sys.platform == "win32", reason="Linux containers on this host"),
]

TMPFS_MB = 16
PAYLOAD_BYTES = 40 << 20  # unpacked: well over the tmpfs, far under max_env_bytes

PROBE = """\
import os, sys, tempfile
import {pkg}
size = os.path.getsize(os.path.join(os.path.dirname({pkg}.__file__), "blob.bin"))
print("TMPDIR", os.environ.get("TMPDIR"), "GETTEMP", tempfile.gettempdir())
with tempfile.NamedTemporaryFile() as fh:
    fh.write(b"x")
print("BLOB", size, "OK")
"""


def _record(name: str, data: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
    return f"{name},sha256={digest.decode()},{len(data)}"


def _big_wheel(folder: Path, name: str) -> None:
    """A pure-Python wheel whose package holds ``blob.bin`` of
    :data:`PAYLOAD_BYTES` random (incompressible) bytes, stored uncompressed."""
    folder.mkdir(parents=True, exist_ok=True)
    dist = f"{name}-1.0.dist-info"
    blob = secrets.token_bytes(PAYLOAD_BYTES)
    files = {
        f"{name}/__init__.py": b'VERSION = "1.0"\n',
        f"{name}/blob.bin": blob,
        f"{dist}/METADATA": f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0\n".encode(),
        f"{dist}/WHEEL": (
            b"Wheel-Version: 1.0\nGenerator: mf-test\nRoot-Is-Purelib: true\n"
            b"Tag: py3-none-any\n"
        ),
    }
    record = [_record(n, d) for n, d in files.items()] + [f"{dist}/RECORD,,"]
    files[f"{dist}/RECORD"] = ("\n".join(record) + "\n").encode()
    with zipfile.ZipFile(folder / f"{name}-1.0-py3-none-any.whl", "w") as zf:
        for item, data in files.items():
            zf.writestr(item, data, compress_type=zipfile.ZIP_STORED)


def _image_local(image: str) -> bool:
    from meshforge.agent.execution.docker_support import open_client

    try:
        client = open_client(10)
    except Exception:  # noqa: BLE001
        return False
    try:
        client.images.get(image)
    except Exception:  # noqa: BLE001
        return False
    finally:
        client.close()
    return True


@pytest.fixture
def env_tags() -> Iterator[list[str]]:
    created: list[str] = []
    yield created
    from meshforge.agent.execution.docker_support import open_client

    client = open_client(30)
    try:
        for tag in created:
            with contextlib.suppress(Exception):
                client.images.remove(tag, force=True)
    finally:
        client.close()


class _Log:
    def __init__(self) -> None:
        self.data = bytearray()

    async def __call__(self, stream: Stream, data: bytes) -> None:
        del stream
        self.data.extend(data)

    def text(self) -> str:
        return self.data.decode("utf-8", "replace")


def _spec(tmp_path: Path, pkg: str, image: str) -> RunSpec:
    attempt_id = new_attempt_id()
    workdir = tmp_path / "work" / attempt_id
    workdir.mkdir(parents=True)
    workdir.chmod(0o700)
    (workdir / "probe.py").write_text(PROBE.format(pkg=pkg))
    (workdir / "req.txt").write_text(f"{pkg}==1.0\n")
    return RunSpec(
        attempt_id=attempt_id,
        argv=("python", "probe.py"),
        workdir=workdir,
        env=workload_env(
            runtime="docker",
            workdir=workdir,
            job_id=str(new_job_id()),
            attempt_id=attempt_id,
        ),
        limits=RunLimits(memory_bytes=512 << 20, pids=256, timeout_s=120),
        image=image,
        env_request=EnvRequest(requirements="req.txt"),
    )


@pytest.mark.asyncio
async def test_a_wheel_larger_than_the_tmpfs_builds_on_the_docker_disk(
    tmp_path: Path, env_tags: list[str]
) -> None:
    from meshforge.agent.execution.docker_runtime import (
        ENV_IMAGE_REPO,
        DockerConfig,
        DockerRuntime,
    )
    from meshforge.agent.settings import PYTHON_CPU_IMAGE

    if not _image_local(PYTHON_CPU_IMAGE):
        pytest.skip(f"{PYTHON_CPU_IMAGE} is not local: docker pull it first")
    pkg = f"mfbig{secrets.token_hex(4)}"
    wheels = tmp_path / "wheels"
    _big_wheel(wheels, pkg)
    runtime = DockerRuntime(
        DockerConfig(agent_label=f"g10-{secrets.token_hex(4)}", tmp_size_mb=TMPFS_MB),
        envs=offline_builder(tmp_path / "envs", wheels),
    )
    log = _Log()
    result: RunResult = await runtime.run(_spec(tmp_path, pkg, PYTHON_CPU_IMAGE), log)
    if result.env_hash:
        env_tags.append(f"{ENV_IMAGE_REPO}:{result.env_hash}")
    text = log.text()
    assert "No space left on device" not in text, text[-2000:]
    assert result.ok, (result.failure_type, result.reason, result.detail, text[-2000:])
    assert result.env_cache == "built"
    assert f"BLOB {PAYLOAD_BYTES} OK" in text
    # the job's temp folder is its own sized tmpfs, never the build's folder
    assert "GETTEMP /tmp" in text, text[-2000:]

    from meshforge.agent.execution.docker_support import open_client

    client = open_client(30)
    try:
        config = client.images.get(f"{ENV_IMAGE_REPO}:{result.env_hash}").attrs[
            "Config"
        ]
    finally:
        client.close()
    env = dict(item.partition("=")[::2] for item in config.get("Env") or [])
    assert env.get("TMPDIR", "/tmp") == "/tmp"


@pytest.mark.asyncio
async def test_a_setup_step_that_runs_out_of_disk_is_disk_full(tmp_path: Path) -> None:
    """D447: a real ENOSPC inside a Docker setup step (its 16 MB ``/tmp``) is
    ``disk_full`` - a condition of the node - never ``env_error``."""
    from meshforge.agent.execution.docker_runtime import DockerConfig, DockerRuntime
    from meshforge.agent.settings import PYTHON_CPU_IMAGE
    from meshforge.common.errors import FailureType

    if not _image_local(PYTHON_CPU_IMAGE):
        pytest.skip(f"{PYTHON_CPU_IMAGE} is not local: docker pull it first")
    runtime = DockerRuntime(
        DockerConfig(agent_label=f"g10-{secrets.token_hex(4)}", tmp_size_mb=TMPFS_MB)
    )
    attempt_id = new_attempt_id()
    workdir = tmp_path / "work" / attempt_id
    workdir.mkdir(parents=True)
    workdir.chmod(0o700)
    fill = "open('/tmp/fill', 'wb').write(b'0' * (40 << 20))"  # > the 16 MB tmpfs
    spec = RunSpec(
        attempt_id=attempt_id,
        argv=("python", "-c", "print('never')"),
        workdir=workdir,
        env=workload_env(
            runtime="docker",
            workdir=workdir,
            job_id=str(new_job_id()),
            attempt_id=attempt_id,
        ),
        limits=RunLimits(memory_bytes=512 << 20, pids=256, timeout_s=60),
        image=PYTHON_CPU_IMAGE,
        setup_argv=("python", "-c", fill),
    )
    log = _Log()
    result = await runtime.run(spec, log)
    assert "No space left on device" in log.text(), log.text()[-2000:]
    assert result.failure_type is FailureType.DISK_FULL, (result.reason, result.detail)
    assert result.reason == "disk_full"
    assert "never" not in log.text()  # the workload never ran
