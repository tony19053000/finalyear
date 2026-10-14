"""V0.7 G5 part 2 (roadmap 0.7.9, D143): a container that is not the sandbox is
never started.

The runtime's own options are tampered with (as a bug, a plugin or a hostile
change could): an extra Docker-socket mount, ``seccomp=unconfined``, a lost
``pids_limit``, a writable root filesystem, a network the plan did not allow.
Docker really creates each container; the check on its inspected
``HostConfig`` must refuse it - ``config_error`` / ``sandbox_violation`` - and
remove it **unstarted** (the workload's marker file never appears) with nothing
left behind. Real Docker; skips without it or without the pinned image.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("docker", reason="needs the agent extra (docker SDK)")

from mf_runtime_harness import Capture, Target, containers_with_label, new_label

from meshforge.agent.execution.docker_runtime import DockerConfig, DockerRuntime
from meshforge.agent.execution.docker_support import open_client, probe_docker
from meshforge.agent.settings import PYTHON_CPU_IMAGE
from meshforge.common.errors import FailureType

pytestmark = [pytest.mark.docker, pytest.mark.timeout(120), pytest.mark.security]


def _image_local(image: str) -> bool:
    client = open_client(10)
    try:
        client.images.get(image)
    except Exception:  # noqa: BLE001
        return False
    finally:
        client.close()
    return True


@pytest.fixture
def target() -> Iterator[Target]:
    if not _image_local(PYTHON_CPU_IMAGE):
        pytest.skip(f"{PYTHON_CPU_IMAGE} is not local: docker pull it first")
    label = new_label()
    yield Target(
        DockerRuntime(DockerConfig(agent_label=label)),
        "docker",
        PYTHON_CPU_IMAGE,
        label,
        "python",
    )
    assert containers_with_label(label) == [], "a container survived the test"


def _socket_mount(kwargs: dict[str, Any]) -> None:
    import docker

    kwargs["mounts"] = [
        *kwargs["mounts"],
        docker.types.Mount(
            target="/var/run/docker.sock",
            source="/var/run/docker.sock",
            type="bind",
            read_only=True,
        ),
    ]


def _host_path_mount(kwargs: dict[str, Any]) -> None:
    import docker

    kwargs["mounts"] = [
        *kwargs["mounts"],
        docker.types.Mount(target="/host-etc", source="/etc", type="bind"),
    ]


def _unconfined(kwargs: dict[str, Any]) -> None:
    kwargs["security_opt"] = [*kwargs["security_opt"], "seccomp=unconfined"]


def _no_pids_limit(kwargs: dict[str, Any]) -> None:
    del kwargs["pids_limit"]


def _writable_root(kwargs: dict[str, Any]) -> None:
    kwargs["read_only"] = False


def _network(kwargs: dict[str, Any]) -> None:
    kwargs["network_mode"] = "bridge"


def _cap_add(kwargs: dict[str, Any]) -> None:
    kwargs["cap_add"] = ["SYS_PTRACE"]


@pytest.mark.parametrize(
    ("tamper", "what"),
    [
        (_socket_mount, "mounts"),
        (_host_path_mount, "mounts"),
        (_unconfined, "seccomp"),
        (_no_pids_limit, "pids_limit"),
        (_writable_root, "read_only"),
        (_network, "network"),
        (_cap_add, "capabilities"),
    ],
)
def test_a_tampered_container_is_never_started(
    target: Target,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tamper: Callable[[dict[str, Any]], None],
    what: str,
) -> None:
    runtime = target.runtime
    assert isinstance(runtime, DockerRuntime)
    original = runtime._create_kwargs

    def tampered(*args: Any, **kw: Any) -> dict[str, Any]:
        kwargs = original(*args, **kw)
        tamper(kwargs)
        return kwargs

    monkeypatch.setattr(runtime, "_create_kwargs", tampered)
    spec = target.spec(tmp_path, "open('/work/started', 'w').write('x')")
    result = asyncio.run(runtime.run(spec, Capture()))
    assert result.failure_type is FailureType.CONFIG_ERROR
    assert result.reason == "sandbox_violation"
    assert f"({what}:" in result.detail
    assert not (tmp_path / "started").exists(), "the tampered container ran"


def test_the_untampered_container_passes_the_check(
    target: Target, tmp_path: Path
) -> None:
    spec = target.spec(tmp_path, "open('/work/started', 'w').write('x')")
    result = asyncio.run(target.runtime.run(spec, Capture()))
    assert result.ok, result
    assert (tmp_path / "started").read_text() == "x"


def test_this_daemon_confines_with_seccomp() -> None:
    """The probe's seccomp rule on the real daemon: available here."""
    found = probe_docker(
        timeout_s=10,
        host_has_nvidia_gpu=False,
        gpu_probe_image=None,
        agent_label=new_label(),
    )
    assert found.available, found.reason
