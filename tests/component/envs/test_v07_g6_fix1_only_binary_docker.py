"""V0.7 G6 fix round 1 (D162, T-5): wheels-only on the **Docker pip** build.

Tester r1 showed the bypass on the uv path only. The Docker env build runs the
base image's own ``pip install --only-binary :all: -r requirements.txt``, and
real pip (25.0, python:3.12-slim) honours the job's file over that option:

* ``--no-binary mfsrc`` in the file -> pip builds the sdist (its build code runs);
* ``mfsrc @ https://.../mfsrc-1.0.tar.gz`` -> pip builds it too (a direct
  reference never goes through the wheel filter) - found in this round.

Real Docker daemon, real pip, offline wheelhouse holding an sdist whose in-tree
backend builds without the network:

* default (``only_binary = true``): both files are refused typed
  ``env_error`` / ``requirements_rejected`` naming the line, **before** any
  build container exists; no env image is left;
* positive control (``only_binary = false``, the owner's escape hatch): the
  same option line really makes this pip build and install the sdist - so the
  refusal above is what keeps build code from running;
* ``--hash`` (the one allowed option) installs a wheel on a wheels-only node.
"""

from __future__ import annotations

import contextlib
import hashlib
import secrets
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import test_v07_g6_only_binary_real as base
from mf_envs_support import make_wheel

from meshforge.agent.envs.builder import EnvBuilder, EnvsConfig
from meshforge.agent.execution.plan import workload_env
from meshforge.agent.execution.runtime import (
    EnvRequest,
    RunLimits,
    RunResult,
    RunSpec,
    Stream,
)
from meshforge.common.errors import FailureType
from meshforge.common.ids import new_attempt_id, new_job_id

pytestmark = [
    pytest.mark.component,
    pytest.mark.docker,
    pytest.mark.asyncio,
    pytest.mark.timeout(300),
    pytest.mark.skipif(sys.platform == "win32", reason="Linux containers on this host"),
]

pytest.importorskip("docker", reason="needs the agent extra (docker SDK)")


class Log:
    def __init__(self) -> None:
        self.data = bytearray()

    async def __call__(self, stream: Stream, data: bytes) -> None:
        self.data.extend(data)

    def text(self) -> str:
        return bytes(self.data).decode("utf-8", "replace")


def _open():  # type: ignore[no-untyped-def]
    from meshforge.agent.execution.docker_support import open_client

    return open_client(30)


@pytest.fixture
def image() -> str:
    from meshforge.agent.settings import PYTHON_CPU_IMAGE

    try:
        client = _open()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no Docker daemon: {type(exc).__name__}")
    try:
        client.images.get(PYTHON_CPU_IMAGE)
    except Exception:  # noqa: BLE001
        pytest.skip(f"{PYTHON_CPU_IMAGE} is not local: docker pull it first")
    finally:
        client.close()
    return PYTHON_CPU_IMAGE


@pytest.fixture
def package() -> str:
    """A wheel name unique to this test: it makes the wheelhouse, and so the
    env hash and the env image, unique too."""
    return f"mfdemo{secrets.token_hex(4)}"


@pytest.fixture
def wheelhouse(tmp_path: Path, package: str) -> Path:
    folder = tmp_path / "wheels"
    make_wheel(folder, package, "1.0")
    # The backend's marker is written inside the build container (its /tmp).
    base._make_sdist(folder, Path("/tmp/mf-build-marker.txt"))
    for path in folder.iterdir():
        path.chmod(0o644)
    folder.chmod(0o755)
    return folder


@pytest.fixture
def label() -> Iterator[str]:
    made = f"g6f1-{secrets.token_hex(4)}"
    yield made
    client = _open()
    try:
        for container in client.containers.list(
            all=True, filters={"label": f"meshforge.agent={made}"}
        ):
            with contextlib.suppress(Exception):
                container.remove(force=True)
    finally:
        client.close()


@pytest.fixture
def env_images() -> Iterator[list[str]]:
    hashes: list[str] = []
    yield hashes
    client = _open()
    try:
        for env_hash in hashes:
            for found in client.images.list(
                filters={"label": f"meshforge.env_hash={env_hash}"}
            ):
                for tag in found.tags:
                    with contextlib.suppress(Exception):
                        client.images.remove(tag, force=True)
    finally:
        client.close()


def _runtime(tmp_path: Path, wheelhouse: Path, label: str, *, only_binary: bool):  # type: ignore[no-untyped-def]
    from meshforge.agent.execution.docker_runtime import DockerConfig, DockerRuntime

    builder = EnvBuilder(
        EnvsConfig(
            root=tmp_path / f"envs-{only_binary}",
            free_disk_floor_bytes=0,
            find_links=wheelhouse,
            no_index=True,
            only_binary=only_binary,
        )
    )
    return DockerRuntime(DockerConfig(agent_label=label), envs=builder)


def _job(tmp_path: Path, image: str, requirements: str, module: str) -> RunSpec:
    attempt_id = new_attempt_id()
    workdir = tmp_path / "work" / attempt_id
    workdir.mkdir(parents=True)
    workdir.chmod(0o700)
    (workdir / "req.txt").write_text(requirements)
    (workdir / "main.py").write_text(
        f"import {module} as m\n"
        "print('IMPORTED', getattr(m, 'SOURCE_BUILT', None), "
        "getattr(m, 'VERSION', None))\n"
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
        limits=RunLimits(memory_bytes=512 << 20, pids=256, timeout_s=120),
        image=image,
        env_request=EnvRequest(requirements="req.txt"),
    )


async def _run(runtime: object, spec: RunSpec) -> tuple[RunResult, Log]:
    log = Log()
    result = await runtime.run(spec, log)  # type: ignore[attr-defined]
    return result, log


def _containers(label: str) -> list[str]:
    client = _open()
    try:
        return [
            c.name
            for c in client.containers.list(
                all=True, filters={"label": f"meshforge.agent={label}"}
            )
        ]
    finally:
        client.close()


def _env_image_count(env_hash: str | None) -> int:
    if not env_hash:
        return 0
    client = _open()
    try:
        return len(
            client.images.list(filters={"label": f"meshforge.env_hash={env_hash}"})
        )
    finally:
        client.close()


@pytest.mark.parametrize(
    ("requirements", "names"),
    [
        ("--no-binary mfsrc\nmfsrc==1.0\n", "'--no-binary mfsrc'"),
        ("mfsrc==1.0\n--no-binary=:all:\n", "'--no-binary=:all:'"),
        ("mfsrc==1.0 --config-settings=a=b\n", "'--config-settings'"),
        (
            "mfsrc @ https://files.example.org/mfsrc-1.0.tar.gz\n",
            "mfsrc-1.0.tar.gz",
        ),
    ],
    ids=["no-binary-pkg", "no-binary-all-after", "per-req-option", "sdist-url"],
)
async def test_docker_pip_refuses_what_could_build_from_source(
    tmp_path: Path,
    image: str,
    wheelhouse: Path,
    label: str,
    env_images: list[str],
    requirements: str,
    names: str,
) -> None:
    runtime = _runtime(tmp_path, wheelhouse, label, only_binary=True)
    started = time.monotonic()
    result, log = await _run(runtime, _job(tmp_path, image, requirements, "mfsrc"))
    env_images.append(result.env_hash or "")
    assert result.failure_type is FailureType.ENV_ERROR, (result, log.text())
    assert result.reason == "requirements_rejected", (result, log.text())
    detail = f"{result.detail} {log.text()}"
    assert names in detail and "requirements line" in detail, detail
    assert "only_binary = true" in detail
    assert "IMPORTED" not in log.text()  # the workload never ran
    assert "Building wheel" not in log.text() and "Successfully" not in log.text()
    assert _containers(label) == []  # refused before any build container
    assert _env_image_count(result.env_hash) == 0
    assert time.monotonic() - started < 60


async def test_positive_control_this_pip_builds_the_sdist_when_the_owner_allows(
    tmp_path: Path, image: str, wheelhouse: Path, label: str, env_images: list[str]
) -> None:
    """The same file on a node whose owner set ``only_binary = false``: real pip
    builds the sdist. So on the default node the refusal is the control."""
    runtime = _runtime(tmp_path, wheelhouse, label, only_binary=False)
    result, log = await _run(
        runtime,
        _job(tmp_path, image, "--no-binary mfsrc\nmfsrc==1.0\n", "mfsrc"),
    )
    env_images.append(result.env_hash or "")
    assert result.ok, (result, log.text())
    assert "IMPORTED True" in log.text(), log.text()  # built from source
    assert _containers(label) == []


async def test_a_hash_pinned_wheel_installs_on_a_wheels_only_node(
    tmp_path: Path,
    image: str,
    wheelhouse: Path,
    package: str,
    label: str,
    env_images: list[str],
) -> None:
    wheel = wheelhouse / f"{package}-1.0-py3-none-any.whl"
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    runtime = _runtime(tmp_path, wheelhouse, label, only_binary=True)
    result, log = await _run(
        runtime,
        _job(tmp_path, image, f"{package}==1.0 --hash=sha256:{digest}\n", package),
    )
    env_images.append(result.env_hash or "")
    assert result.ok, (result, log.text())
    assert "IMPORTED None 1.0" in log.text(), log.text()
    assert result.env_cache == "built"
