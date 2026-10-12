"""V0.7 G5 tester round 2: the create-time allow-list (D160) against real Docker.

Every tamper below is applied to the runtime's own create options. Dangerous
ones must be refused (``config_error``) and the workload must never run. The
last block lists tampers INSIDE the options the runtime uses whose effect the
HostConfig check does not compare with the plan (see the xfail reasons).
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("docker", reason="needs the agent extra (docker SDK)")

from mf_runtime_harness import Capture, Target, containers_with_label, new_label

from meshforge.agent.execution.docker_runtime import DockerConfig, DockerRuntime
from meshforge.agent.execution.docker_support import open_client
from meshforge.agent.execution.runtime import RunLimits
from meshforge.agent.settings import PYTHON_CPU_IMAGE
from meshforge.common.errors import FailureType

pytestmark = [pytest.mark.docker, pytest.mark.timeout(120)]

Tamper = Callable[[dict[str, Any]], None]


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


def _set(**fields: Any) -> Tamper:
    def apply(kwargs: dict[str, Any]) -> None:
        kwargs.update(fields)

    return apply


def _secopt(*extra: str) -> Tamper:
    def apply(kwargs: dict[str, Any]) -> None:
        kwargs["security_opt"] = ["no-new-privileges:true", *extra]

    return apply


_ALLOW_ALL = json.dumps({"defaultAction": "SCMP_ACT_ALLOW"})


def _run(
    target: Target,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tamper: Tamper,
    *,
    network: bool = False,
    code: str = "open('/work/started', 'w').write('x')",
) -> Any:
    runtime = target.runtime
    assert isinstance(runtime, DockerRuntime)
    original = runtime._create_kwargs

    def tampered(*args: Any, **kw: Any) -> dict[str, Any]:
        kwargs = original(*args, **kw)
        tamper(kwargs)
        return kwargs

    monkeypatch.setattr(runtime, "_create_kwargs", tampered)
    spec = target.spec(tmp_path, code, network=network)
    return asyncio.run(runtime.run(spec, Capture()))


REFUSED: list[tuple[str, Tamper, bool]] = [
    ("seccomp_allow_all_inline", _secopt(f"seccomp={_ALLOW_ALL}"), False),
    ("seccomp_unconfined", _secopt("seccomp=unconfined"), False),
    ("seccomp_uppercase", _secopt("SECCOMP=unconfined"), False),
    ("seccomp_spaced", _secopt(" seccomp = unconfined "), False),
    ("apparmor_unconfined", _secopt("apparmor=unconfined"), False),
    ("label_disable", _secopt("label=disable"), False),
    ("systempaths_unconfined", _secopt("systempaths=unconfined"), False),
    ("net_host_allowed", _set(network_mode="host"), True),
    ("net_host_denied", _set(network_mode="host"), False),
    ("net_container", _set(network_mode="container:nonexistent"), False),
    ("net_named_bridge", _set(network_mode="bridge2"), True),
    ("pid_host", _set(pid_mode="host"), False),
    ("ipc_host", _set(ipc_mode="host"), False),
    ("uts_host", _set(uts_mode="host"), False),
    ("userns_host", _set(userns_mode="host"), False),
    ("cgroupns_host", _set(cgroupns="host"), False),
    ("mem_bigger", _set(mem_limit="64g", memswap_limit="64g"), False),
    ("mem_bigger_no_swap_change", _set(mem_limit=512 << 20), False),
    ("swap_unlimited", _set(memswap_limit=-1), False),
    ("swap_double", _set(memswap_limit=512 << 20), False),
    ("tmpfs_huge", _set(tmpfs={"/tmp": "rw,nosuid,nodev,size=900g"}), False),
    ("tmpfs_unsized", _set(tmpfs={"/tmp": "rw,nosuid,nodev"}), False),
    ("tmpfs_exec", _set(tmpfs={"/tmp": "rw,nodev,size=16m"}), False),
    (
        "tmpfs_extra",
        _set(tmpfs={"/tmp": "rw,nosuid,nodev,size=16m", "/mnt": "rw"}),
        False,
    ),
    ("tmpfs_slash", _set(tmpfs={"/tmp/": "rw,nosuid,nodev,size=16m"}), False),
    ("sysctl", _set(sysctls={"net.ipv4.ip_unprivileged_port_start": "0"}), False),
    ("ports", _set(ports={"80/tcp": 8080}), True),
    ("group_add", _set(group_add=["0"]), False),
    ("devices", _set(devices=["/dev/null:/dev/x:rwm"]), False),
    ("privileged", _set(privileged=True), False),
    ("cap_add", _set(cap_add=["SYS_ADMIN"]), False),
    ("cap_drop_empty", _set(cap_drop=[]), False),
    ("oom_disable", _set(oom_kill_disable=True), False),
    ("volumes_from", _set(volumes_from=["x"]), False),
    (
        "extra_bind_mount",
        _set(volumes={"/etc": {"bind": "/hostetc", "mode": "rw"}}),
        False,
    ),
    ("pids_unlimited", _set(pids_limit=-1), False),
    ("writable_root", _set(read_only=False), False),
    ("cpuset", _set(cpuset_cpus="0"), False),
    ("security_opt_dup", _secopt("no-new-privileges:false"), False),
]


@pytest.mark.parametrize(
    ("tamper", "network"),
    [pytest.param(t, n, id=name) for name, t, n in REFUSED],
)
def test_a_tampered_create_is_refused_and_never_started(
    target: Target,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tamper: Tamper,
    network: bool,
) -> None:
    result = _run(target, tmp_path, monkeypatch, tamper, network=network)
    assert not (tmp_path / "started").exists(), "the tampered container ran"
    # CONFIG_ERROR from our assertion, or INTERNAL_ERROR when the daemon itself
    # refused the create; either way the workload never ran
    assert result.failure_type in (
        FailureType.CONFIG_ERROR,
        FailureType.INTERNAL_ERROR,
    ), result


def test_the_untampered_container_still_runs_with_and_without_a_network(
    target: Target, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for network in (False, True):
        sub = tmp_path / str(network)
        sub.mkdir()
        result = _run(target, sub, monkeypatch, lambda _k: None, network=network)
        assert result.failure_type is None, result
        assert (sub / "started").exists()


def test_a_setup_container_with_its_network_still_starts(
    target: Target, tmp_path: Path
) -> None:
    runtime = target.runtime
    spec = target.spec(
        tmp_path,
        "open('/work/ran', 'w').write('x')",
        setup_code="open('/work/setup', 'w').write('x')",
    )
    result = asyncio.run(runtime.run(spec, Capture()))
    assert result.failure_type is None, result
    assert (tmp_path / "setup").exists() and (tmp_path / "ran").exists()


# Tampers within the options the runtime itself uses, whose value the HostConfig
# check does not compare with the plan (D160 lists Memory/MemorySwap/tmpfs only;
# T-10). Observed and not asserted: ``shm_size`` (memory-backed, so inside the
# memory cgroup), ``hostname``, ``user`` (every capability is dropped).


def test_a_pids_limit_above_the_plan_is_refused(
    target: Target, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _run(target, tmp_path, monkeypatch, _set(pids_limit=50_000))
    assert not (tmp_path / "started").exists(), "the tampered container ran"
    assert result.failure_type is FailureType.CONFIG_ERROR, result


def test_a_cpu_cap_above_the_plan_is_refused(
    target: Target, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = target.runtime
    assert isinstance(runtime, DockerRuntime)
    original = runtime._create_kwargs

    def tampered(*args: Any, **kw: Any) -> dict[str, Any]:
        kwargs = original(*args, **kw)
        assert kwargs["nano_cpus"] == 500_000_000
        kwargs["nano_cpus"] = 4_000_000_000  # under the host CPUs, so Docker accepts it
        return kwargs

    monkeypatch.setattr(runtime, "_create_kwargs", tampered)
    spec = dataclasses.replace(
        target.spec(tmp_path, "open('/work/started', 'w').write('x')"),
        limits=RunLimits(memory_bytes=256 << 20, pids=128, timeout_s=60.0, cpus=0.5),
    )
    result = asyncio.run(runtime.run(spec, Capture()))
    assert not (tmp_path / "started").exists(), "the tampered container ran"
    assert result.failure_type is FailureType.CONFIG_ERROR, result
