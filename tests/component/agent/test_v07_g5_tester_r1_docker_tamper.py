"""V0.7 G5 tester round 1: real containers created with options the sandbox
assertion does not read (it must refuse them and never start the workload).

Complements ``test_v07_g5_sandbox_assert.py`` (which tampers with the fields the
assertion does read). Real Docker; skips without it or without the pinned image.
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
from meshforge.agent.execution.docker_support import open_client
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


def _swap(kwargs: dict[str, Any]) -> None:
    kwargs["memswap_limit"] = -1


def _cgroupns(kwargs: dict[str, Any]) -> None:
    kwargs["cgroupns"] = "host"


def _sysctl(kwargs: dict[str, Any]) -> None:
    kwargs["sysctls"] = {"net.ipv4.ip_unprivileged_port_start": "0"}


def _extra_tmpfs(kwargs: dict[str, Any]) -> None:
    kwargs["tmpfs"] = {**kwargs["tmpfs"], "/mnt": "rw,exec"}


def _no_oom_kill(kwargs: dict[str, Any]) -> None:
    kwargs["oom_kill_disable"] = True


@pytest.mark.parametrize(
    "tamper", [_swap, _cgroupns, _sysctl, _extra_tmpfs, _no_oom_kill]
)
def test_a_container_with_an_unread_option_is_never_started(
    target: Target,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tamper: Callable[[dict[str, Any]], None],
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
    assert result.failure_type is FailureType.CONFIG_ERROR, result
    assert not (tmp_path / "started").exists(), "the tampered container ran"
