"""V0.5 G2 criterion 5 at component level: real runtimes on cached environments.

* subprocess runtime: requirements go into a cached ``--target`` dir;
* venv runtime: every job runs in a cached uv venv (its own interpreter);
* Docker runtime: requirements go into a committed ``meshforge-env:<hash>`` image
  (skips with the reason without a daemon or the pinned python-cpu image).

The second job with the same requirements must start with **no install** (cache
``hit``, no installer output in its log) and an env phase <= 3 s on Laptop 1 -
printed with ``-s``, "not a benchmark claim". Installs are offline (wheelhouse +
``--no-index``); Docker images created here are removed at teardown.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import secrets
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip("uv", reason="needs the agent extra (the uv wheel)")

from mf_envs_support import make_wheel, offline_builder, slow_uv_argv

from meshforge.agent.execution.plan import workload_env
from meshforge.agent.execution.runtime import (
    EnvRequest,
    RunLimits,
    RunObserver,
    RunResult,
    RunSpec,
    Stream,
)
from meshforge.agent.execution.subprocess_runtime import SubprocessRuntime
from meshforge.agent.execution.venv_runtime import VenvRuntime
from meshforge.common.errors import FailureType
from meshforge.common.ids import new_attempt_id, new_job_id

pytestmark = [pytest.mark.timeout(240)]

ENV_OVERHEAD_BOUND_S = 3.0  # V0.5 criterion 5

READ_ONLY_PROBE = """\
import os, sys, importlib
mod = importlib.import_module(sys.argv[1])
pkg = os.path.dirname(mod.__file__)
blocked = 0
for target in (os.path.join(pkg, "planted.py"), mod.__file__):
    try:
        with open(target, "a") as fh:
            fh.write("# poisoned\\n")
    except OSError:
        blocked += 1
print("BLOCKED", blocked, "VERSION", mod.VERSION, "PREFIX", sys.prefix)
sys.exit(0 if blocked == 2 else 3)
"""


class Log:
    def __init__(self) -> None:
        self.data: dict[Stream, bytearray] = {s: bytearray() for s in Stream}

    async def __call__(self, stream: Stream, data: bytes) -> None:
        self.data[stream].extend(data)

    def text(self) -> str:
        return (
            bytes(self.data[Stream.STDOUT]) + bytes(self.data[Stream.STDERR])
        ).decode("utf-8", "replace")


@pytest.fixture
def package() -> str:
    """A package name unique to this test (so a Docker env image is too)."""
    return f"mfdemo{secrets.token_hex(4)}"


@pytest.fixture
def wheelhouse(tmp_path: Path, package: str) -> Path:
    folder = tmp_path / "wheels"
    make_wheel(folder, package, "1.0")
    return folder


def _job(
    tmp_path: Path,
    runtime: str,
    package: str,
    *,
    requirements: str | None = None,
    argv: tuple[str, ...] | None = None,
    image: str | None = None,
) -> RunSpec:
    attempt_id = new_attempt_id()
    workdir = tmp_path / "work" / attempt_id
    workdir.mkdir(parents=True)
    workdir.chmod(0o700)
    (workdir / "probe.py").write_text(READ_ONLY_PROBE)
    (workdir / "req.txt").write_text(requirements or f"{package}==1.0\n")
    return RunSpec(
        attempt_id=attempt_id,
        argv=argv or ("python", "probe.py", package),
        workdir=workdir,
        env=workload_env(
            runtime=runtime,
            workdir=workdir,
            job_id=str(new_job_id()),
            attempt_id=attempt_id,
        ),
        limits=RunLimits(memory_bytes=512 << 20, pids=256, timeout_s=60),
        image=image,
        env_request=EnvRequest(requirements="req.txt"),
    )


async def _run(runtime: object, spec: RunSpec) -> tuple[RunResult, Log, float]:
    log = Log()
    started = time.monotonic()
    result = await runtime.run(spec, log)  # type: ignore[attr-defined]
    return result, log, time.monotonic() - started


def _report(label: str, cold: RunResult, warm: RunResult) -> None:
    print(  # noqa: T201 - measured, for the handoff (run with -s)
        f"{label}: cold env {cold.env_s:.2f} s ({cold.env_cache}), warm env "
        f"{warm.env_s:.3f} s ({warm.env_cache}) - not a benchmark claim"
    )


# ------------------------------------------------------------- subprocess
@pytest.mark.asyncio
async def test_subprocess_second_job_installs_nothing(
    tmp_path: Path, wheelhouse: Path, package: str
) -> None:
    builder = offline_builder(tmp_path / "envs", wheelhouse)
    runtime = SubprocessRuntime(envs=builder)
    cold, cold_log, _ = await _run(runtime, _job(tmp_path, "subprocess", package))
    assert cold.ok, cold_log.text()
    assert cold.env_cache == "built" and cold.env_hash
    assert "VERSION 1.0" in cold_log.text()
    warm, warm_log, _ = await _run(runtime, _job(tmp_path, "subprocess", package))
    assert warm.ok, warm_log.text()
    assert (warm.env_cache, warm.env_hash) == ("hit", cold.env_hash)
    assert "Installed" not in warm_log.text()  # uv printed nothing: no install
    assert warm.env_s <= ENV_OVERHEAD_BOUND_S
    _report("subprocess", cold, warm)


# ------------------------------------------------------------------- venv
@pytest.mark.asyncio
async def test_venv_probe_is_available_with_the_uv_wheel(tmp_path: Path) -> None:
    builder = offline_builder(tmp_path / "envs", tmp_path)
    found = await VenvRuntime(envs=builder).probe()
    assert found.available and found.name == "venv" and not found.sandboxed
    assert found.version.startswith("uv ")


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows ignores the read-only bit on directories (documented limit)",
)
@pytest.mark.asyncio
async def test_venv_runs_in_its_cached_env_read_only(
    tmp_path: Path, wheelhouse: Path, package: str
) -> None:
    builder = offline_builder(tmp_path / "envs", wheelhouse)
    runtime = VenvRuntime(envs=builder)
    cold, cold_log, _ = await _run(runtime, _job(tmp_path, "venv", package))
    # exit 0 = both writes into the env were refused (the probe exits 3 otherwise)
    assert cold.ok, cold_log.text()
    assert cold.env_cache == "built"
    env_dir = builder.env_dir("venv", cold.env_hash or "")
    assert f"PREFIX {env_dir}" in cold_log.text()  # the venv's interpreter ran it
    assert "BLOCKED 2" in cold_log.text()
    warm, warm_log, _ = await _run(runtime, _job(tmp_path, "venv", package))
    assert warm.ok, warm_log.text()
    assert (warm.env_cache, warm.env_hash) == ("hit", cold.env_hash)
    assert warm.env_s <= ENV_OVERHEAD_BOUND_S
    assert not list(env_dir.rglob("planted.py"))
    _report("venv", cold, warm)


@pytest.mark.asyncio
async def test_venv_without_requirements_still_isolates_the_interpreter(
    tmp_path: Path, wheelhouse: Path, package: str
) -> None:
    runtime = VenvRuntime(envs=offline_builder(tmp_path / "envs", wheelhouse))
    spec = _job(
        tmp_path,
        "venv",
        package,
        argv=(
            "python",
            "-c",
            "import importlib.util as u, sys; print('AGENT', u.find_spec('pydantic'))",
        ),
    )
    spec = dataclasses.replace(spec, env_request=EnvRequest())
    result, log, _ = await _run(runtime, spec)
    assert result.ok, log.text()
    assert "AGENT None" in log.text()  # the agent's packages are not visible


@pytest.mark.asyncio
async def test_failed_env_is_env_error_and_the_workload_never_runs(
    tmp_path: Path, wheelhouse: Path, package: str
) -> None:
    runtime = VenvRuntime(envs=offline_builder(tmp_path / "envs", wheelhouse))
    spec = _job(
        tmp_path,
        "venv",
        package,
        requirements=f"{package}==9.9\n",
        argv=("python", "-c", "open('RAN', 'w').write('x')"),
    )
    result, log, _ = await _run(runtime, spec)
    assert result.failure_type is FailureType.ENV_ERROR
    assert result.reason == "setup_failed"
    assert result.run_s == 0.0 and result.env_s > 0
    assert not (spec.workdir / "RAN").exists()
    assert package in log.text()  # the installer's message is in the job's log


@pytest.mark.asyncio
async def test_rejected_requirements_are_typed(
    tmp_path: Path, wheelhouse: Path, package: str
) -> None:
    runtime = SubprocessRuntime(envs=offline_builder(tmp_path / "envs", wheelhouse))
    spec = _job(tmp_path, "subprocess", package, requirements="-r other.txt\n")
    result, _log, _ = await _run(runtime, spec)
    assert result.failure_type is FailureType.ENV_ERROR
    assert result.reason == "requirements_rejected"


@pytest.mark.asyncio
async def test_cancel_during_a_build_leaves_nothing(
    tmp_path: Path, wheelhouse: Path, package: str
) -> None:
    builder = offline_builder(
        tmp_path / "envs", wheelhouse, uv_argv=slow_uv_argv(tmp_path, 60)
    )
    runtime = SubprocessRuntime(envs=builder)
    observer = RunObserver()
    task = asyncio.create_task(
        runtime.run(_job(tmp_path, "subprocess", package), Log(), observer=observer)
    )
    folder = builder.root / "subprocess"
    for _ in range(600):
        if folder.exists() and list(folder.glob("*.building")):
            break
        await asyncio.sleep(0.05)
    else:
        pytest.fail("the build never started")
    started = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert time.monotonic() - started < 8.0  # grace + slack
    assert observer.cancelled_result().failure_type is FailureType.CANCELLED
    assert list(folder.iterdir()) == []
    # the lock went with the cancelled build: the next one is not blocked
    again = SubprocessRuntime(envs=offline_builder(tmp_path / "envs", wheelhouse))
    result, log, _ = await _run(again, _job(tmp_path, "subprocess", package))
    assert result.ok and result.env_cache == "built", log.text()


# ----------------------------------------------------------------- docker
def _docker_image_local(image: str) -> bool:
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


def _images_for(label: str) -> list[str]:
    """Env images this test's runtime built: the image inherits the build
    container's ``meshforge.agent=<label>`` (unique per test), so this never
    touches another test's or lane's images."""
    from meshforge.agent.execution.docker_support import open_client

    client = open_client(30)
    try:
        return [
            tag
            for image in client.images.list(
                filters={"label": f"meshforge.agent={label}"}
            )
            for tag in image.tags
        ]
    finally:
        client.close()


@pytest.fixture
def env_images() -> Iterator[list[str]]:
    """Env image tags a test created; removed at teardown."""
    created: list[str] = []
    yield created
    from meshforge.agent.execution.docker_support import open_client

    client = open_client(30)
    try:
        for tag in created:
            with contextlib.suppress(Exception):  # already gone
                client.images.remove(tag, force=True)
    finally:
        client.close()


@pytest.mark.docker
@pytest.mark.skipif(sys.platform == "win32", reason="Linux containers on this host")
@pytest.mark.asyncio
async def test_docker_env_image_built_once_then_reused_read_only(
    tmp_path: Path, wheelhouse: Path, package: str, env_images: list[str]
) -> None:
    pytest.importorskip("docker", reason="needs the agent extra (docker SDK)")
    from meshforge.agent.execution.docker_runtime import (
        DockerConfig,
        DockerRuntime,
    )
    from meshforge.agent.settings import PYTHON_CPU_IMAGE

    if not _docker_image_local(PYTHON_CPU_IMAGE):
        pytest.skip(f"{PYTHON_CPU_IMAGE} is not local: docker pull it first")
    builder = offline_builder(tmp_path / "envs", wheelhouse)
    label = f"g2t-{secrets.token_hex(4)}"
    runtime = DockerRuntime(DockerConfig(agent_label=label), envs=builder)
    cold, cold_log, _ = await _run(
        runtime, _job(tmp_path, "docker", package, image=PYTHON_CPU_IMAGE)
    )
    env_images.extend(_images_for(label))
    assert cold.ok, cold_log.text()
    assert cold.env_cache == "built"
    assert "BLOCKED 2" in cold_log.text()  # /env is read-only to the job
    assert "VERSION 1.0" in cold_log.text()
    warm, warm_log, _ = await _run(
        runtime, _job(tmp_path, "docker", package, image=PYTHON_CPU_IMAGE)
    )
    assert warm.ok, warm_log.text()
    assert (warm.env_cache, warm.env_hash) == ("hit", cold.env_hash)
    assert "Successfully installed" not in warm_log.text()
    assert warm.env_s <= ENV_OVERHEAD_BOUND_S
    _report("docker", cold, warm)
    # nothing left behind: no build or run container with this test's label
    from meshforge.agent.execution.docker_support import open_client

    client = open_client(30)
    try:
        left = client.containers.list(
            all=True, filters={"label": f"meshforge.agent={label}"}
        )
        assert left == []
    finally:
        client.close()


@pytest.mark.docker
@pytest.mark.skipif(sys.platform == "win32", reason="Linux containers on this host")
@pytest.mark.asyncio
async def test_docker_failed_build_leaves_no_image(
    tmp_path: Path, wheelhouse: Path, package: str, env_images: list[str]
) -> None:
    pytest.importorskip("docker", reason="needs the agent extra (docker SDK)")
    from meshforge.agent.execution.docker_runtime import (
        ENV_IMAGE_REPO,
        DockerConfig,
        DockerRuntime,
    )
    from meshforge.agent.settings import PYTHON_CPU_IMAGE

    if not _docker_image_local(PYTHON_CPU_IMAGE):
        pytest.skip(f"{PYTHON_CPU_IMAGE} is not local: docker pull it first")
    builder = offline_builder(tmp_path / "envs", wheelhouse)
    label = f"g2t-{secrets.token_hex(4)}"
    runtime = DockerRuntime(DockerConfig(agent_label=label), envs=builder)
    spec = _job(
        tmp_path,
        "docker",
        package,
        requirements=f"{package}==9.9\n",
        image=PYTHON_CPU_IMAGE,
    )
    result, log, _ = await _run(runtime, spec)
    assert result.failure_type is FailureType.ENV_ERROR, log.text()
    assert result.reason == "setup_failed"
    env_images.extend(_images_for(label))
    assert result.env_hash
    assert not _docker_image_local(f"{ENV_IMAGE_REPO}:{result.env_hash}")
