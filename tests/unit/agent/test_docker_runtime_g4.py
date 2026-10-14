"""V0.4 G4 follow-ups on the Docker runtime (G3 reviewer, D63), without a daemon.

* D63-1: the network mode is decided per container role - setup has the network
  (pip), the run container follows ``RunSpec.network``;
* D63-2: the workdir is owner-only (0o700), writable by the workload's uid, and
  never opened to other users;
* D63-3: only the daemon's own "no GPU driver" wording is ``gpu_unavailable``;
* D63-4: a pull stuck on a stalled registry is interrupted by closing its
  response, not merely flagged.
"""

from __future__ import annotations

import stat
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from meshforge.agent.execution import docker_runtime as dr
from meshforge.agent.execution.docker_runtime import (
    WORKDIR_MODE,
    DockerConfig,
    DockerRuntime,
    prepare_workdir,
)
from meshforge.agent.execution.runtime import RunLimits, RunSpec
from meshforge.common.errors import FailureType
from meshforge.common.ids import new_attempt_id

pytestmark = pytest.mark.unit

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")


def _fake_docker() -> Any:
    def record(**kwargs: Any) -> dict[str, Any]:
        return kwargs

    return SimpleNamespace(
        types=SimpleNamespace(Mount=record, DeviceRequest=record, LogConfig=record)
    )


def _spec(tmp_path: Path, *, network: bool, setup: bool) -> RunSpec:
    return RunSpec(
        attempt_id=new_attempt_id(),
        argv=("python", "main.py"),
        workdir=tmp_path,
        env={"PATH": "/usr/bin"},
        limits=RunLimits(memory_bytes=64 << 20, pids=64, timeout_s=10),
        image="python:3.12-slim",
        setup_argv=("pip", "install", "-r", "requirements.txt") if setup else None,
        network=network,
    )


@pytest.mark.parametrize("network", [True, False])
def test_network_mode_is_set_per_container_role(tmp_path: Path, network: bool) -> None:
    runtime = DockerRuntime(DockerConfig(agent_label="node_x", run_as="1000:1000"))
    spec = _spec(tmp_path, network=network, setup=True)
    setup = runtime._create_kwargs(_fake_docker(), spec, "setup")
    run = runtime._create_kwargs(_fake_docker(), spec, "run")
    assert setup["network_mode"] == "bridge"  # pip needs the network, always
    assert run["network_mode"] == ("bridge" if network else "none")


# ------------------------------------------------------------- permissions


@pytest.mark.security
@posix_only
def test_owner_agent_gets_an_owner_only_workdir(tmp_path: Path) -> None:
    work = tmp_path / "attempt"
    work.mkdir(mode=0o777)
    work.chmod(0o777)
    me = 1000
    prepare_workdir(work, f"{me}:{me}", euid=lambda: me, chown=_no_chown)
    assert stat.S_IMODE(work.stat().st_mode) == WORKDIR_MODE == 0o700


@pytest.mark.security
@posix_only
def test_root_agent_chowns_to_the_workload_user_and_stays_owner_only(
    tmp_path: Path,
) -> None:
    work = tmp_path / "attempt"
    work.mkdir()
    calls: list[tuple[Path, int, int]] = []
    prepare_workdir(
        work,
        "65534:65534",
        euid=lambda: 0,
        chown=lambda p, u, g: calls.append((p, u, g)),
    )
    assert calls == [(work, 65534, 65534)]
    assert stat.S_IMODE(work.stat().st_mode) == 0o700


@pytest.mark.security
@posix_only
def test_a_non_root_agent_never_opens_the_workdir_for_another_uid(
    tmp_path: Path,
) -> None:
    work = tmp_path / "attempt"
    work.mkdir(mode=0o700)
    with pytest.raises(dr._WorkdirError) as info:
        prepare_workdir(work, "4242:4242", euid=lambda: 1000, chown=_no_chown)
    assert info.value.reason == "run_as_not_writable"
    assert stat.S_IMODE(work.stat().st_mode) & 0o077 == 0  # untouched, not widened


def _no_chown(_path: Path, _uid: int, _gid: int) -> None:
    raise AssertionError("a non-root agent must not chown")


# ------------------------------------------------------------- GPU match


class _ApiError(Exception):
    pass


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        (
            'could not select device driver "nvidia" with capabilities: [[gpu]]',
            "gpu_unavailable",
        ),
        ("Unknown or invalid runtime name: nvidia", "gpu_unavailable"),
        # "nvidia" appearing for another reason is not a GPU fault (D63-3)
        ("invalid reference format: nvidia/cuda:bad tag", "container_create_failed"),
        ("mount /home/u/nvidia-projects: no such file", "container_create_failed"),
    ],
)
def test_only_the_daemons_gpu_wording_is_gpu_unavailable(
    text: str, reason: str
) -> None:
    fault = dr._create_fault(_ApiError(text), None, "run")
    assert fault.reason == reason
    expected = (
        FailureType.ENV_ERROR
        if reason == "gpu_unavailable"
        else FailureType.INTERNAL_ERROR
    )
    assert fault.failure_type is expected


# ------------------------------------------------------------- pull stop


class _StalledResponse:
    """A streaming response whose read blocks until it is closed."""

    def __init__(self) -> None:
        self.closed = threading.Event()

    def close(self) -> None:
        self.closed.set()

    def messages(self) -> Any:
        yield {"status": "Pulling fs layer"}
        if not self.closed.wait(10):
            raise AssertionError("the stalled read was never interrupted")
        raise OSError("connection closed")  # what urllib3 raises on a closed socket


def _stream(response: _StalledResponse) -> Any:
    # same shape as APIClient._stream_helper(response, decode=True)
    yield from response.messages()


def test_stopping_a_stalled_pull_closes_its_response() -> None:
    response = _StalledResponse()
    client = SimpleNamespace(
        api=SimpleNamespace(pull=lambda *_a, **_k: _stream(response))
    )
    pull = dr._Pull(client, "python", "sha256:" + "0" * 64)  # type: ignore[arg-type]
    result: list[str | None] = []
    thread = threading.Thread(target=lambda: result.append(pull.run()), daemon=True)
    thread.start()
    time.sleep(0.02)  # the thread is now blocked inside the stalled read
    pull.stop()
    thread.join(2.0)
    assert not thread.is_alive(), "the pull thread is still blocked"
    assert response.closed.is_set()
    assert result == ["cancelled"]


def test_the_sdk_still_exposes_the_pull_response() -> None:
    """Pins the SDK internal ``_Pull`` relies on (``_stream_helper(response)``)."""
    docker = pytest.importorskip("docker", reason="needs the agent extra")
    api = docker.APIClient(base_url="unix:///nonexistent.sock", version="1.43")
    fake = SimpleNamespace(raw=SimpleNamespace(_fp=SimpleNamespace(chunked=True)))
    api._post = lambda *_a, **_k: fake
    api._raise_for_status = lambda _r: None
    stream = api.pull("python", tag="3.12", stream=True, decode=True)
    try:
        assert dr._pull_response(stream) is fake
    finally:
        stream.close()
        api.close()
