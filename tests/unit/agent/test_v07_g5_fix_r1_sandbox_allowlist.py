"""V0.7 G5 fix round 1 (D160, T-3): the create-time sandbox check is an allow-list.

Every field that can widen the box must hold a value the runtime itself sets.
Each case below breaks one field of a real ``docker inspect`` ``HostConfig``
(Docker 29, runc, cgroup v2) and must be refused with its own code; the real
config (with the planned limits) must pass for every role.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from meshforge.agent.execution.sandbox_check import (
    ExpectedLimits,
    ExpectedMount,
    SandboxViolationError,
    check_host_config,
)

pytestmark = pytest.mark.unit

WORK = "/data/work/att_x"
MEM = 67108864
TMP = "rw,nosuid,nodev,size=512m"
PLAN = ExpectedLimits(memory_bytes=MEM, tmp_options=TMP)
REAL: dict[str, Any] = {
    "Binds": None,
    "NetworkMode": "none",
    "CapAdd": None,
    "CapDrop": ["ALL"],
    "IpcMode": "private",
    "PidMode": "",
    "UTSMode": "",
    "UsernsMode": "",
    "CgroupnsMode": "private",
    "Privileged": False,
    "ReadonlyRootfs": True,
    "SecurityOpt": ["no-new-privileges:true"],
    "Tmpfs": {"/tmp": TMP},
    "Memory": MEM,
    "MemorySwap": MEM,
    "OomKillDisable": False,
    "Sysctls": None,
    "PortBindings": {},
    "GroupAdd": None,
    "PidsLimit": 64,
    "Devices": [],
    "Mounts": [{"Type": "bind", "Source": WORK, "Target": "/work"}],
    "MaskedPaths": ["/proc/acpi", "/proc/kcore"],
    "ReadonlyPaths": ["/proc/bus", "/proc/sys", "/proc/sysrq-trigger"],
    "VolumesFrom": None,
}
MOUNTS = [ExpectedMount(WORK, "/work", False)]


def _check(
    host: dict[str, Any],
    *,
    role: str = "run",
    network: bool = False,
    limits: ExpectedLimits | None = PLAN,
) -> None:
    check_host_config(
        host, role=role, mounts=MOUNTS, network_allowed=network, limits=limits
    )


def _with(**changes: Any) -> dict[str, Any]:
    host = copy.deepcopy(REAL)
    host.update(changes)
    return host


@pytest.mark.parametrize("role", ["run", "setup"])
@pytest.mark.parametrize("network", ["none", "bridge", "default"])
def test_the_real_config_passes_for_every_role_and_network(
    role: str, network: str
) -> None:
    _check(_with(NetworkMode=network), role=role, network=True)
    _check(_with(NetworkMode=network, ReadonlyRootfs=False), role="envbuild")


@pytest.mark.parametrize(
    "changes",
    [
        {"IpcMode": "shareable"},
        {"IpcMode": "none"},
        {"IpcMode": None},
        {"CgroupnsMode": ""},  # a daemon that does not report it
        {"CgroupnsMode": None},
        {"Sysctls": {}},
        {"PortBindings": None},
        {"GroupAdd": []},
        {"OomKillDisable": None},
        {"SecurityOpt": ["no-new-privileges"]},
    ],
)
def test_the_other_harmless_spellings_pass(changes: dict[str, Any]) -> None:
    _check(_with(**changes))


@pytest.mark.parametrize(
    ("changes", "what"),
    [
        # seccomp: only the daemon default; any seccomp= / other confinement option
        (
            {
                "SecurityOpt": [
                    "no-new-privileges:true",
                    'seccomp={"defaultAction":"SCMP_ACT_ALLOW"}',
                ]
            },
            "seccomp",
        ),
        ({"SecurityOpt": ["no-new-privileges:true", "seccomp=/p.json"]}, "seccomp"),
        ({"SecurityOpt": ["no-new-privileges:true", "seccomp:x"]}, "seccomp"),
        ({"SecurityOpt": ["no-new-privileges:true", "apparmor=other"]}, "seccomp"),
        ({"SecurityOpt": ["no-new-privileges:true", "label=type:x"]}, "seccomp"),
        (
            {"SecurityOpt": ["no-new-privileges:true", "no-new-privileges:false"]},
            "seccomp",
        ),
        # namespaces
        ({"PidMode": "container:abc"}, "namespaces"),
        ({"PidMode": "host"}, "namespaces"),
        ({"IpcMode": "container:abc"}, "namespaces"),
        ({"IpcMode": "host"}, "namespaces"),
        ({"UTSMode": "host"}, "namespaces"),
        ({"UsernsMode": "host"}, "namespaces"),
        ({"CgroupnsMode": "host"}, "namespaces"),
        ({"CgroupnsMode": 1}, "namespaces"),
        # memory
        ({"Memory": MEM * 2, "MemorySwap": MEM * 2}, "memory_limit"),
        ({"Memory": 1 << 50, "MemorySwap": 1 << 50}, "memory_limit"),
        ({"MemorySwap": -1}, "memory_limit"),
        ({"MemorySwap": MEM * 2}, "memory_limit"),
        ({"MemorySwap": None}, "memory_limit"),
        ({"Memory": True}, "memory_limit"),
        ({"OomKillDisable": True}, "memory_limit"),
        # tmpfs
        ({"Tmpfs": {"/tmp": "rw,nosuid,nodev,size=900g"}}, "tmpfs"),
        ({"Tmpfs": {"/tmp": "rw,exec,nodev,size=512m"}}, "tmpfs"),
        ({"Tmpfs": {"/tmp": TMP, "/mnt": "rw,exec"}}, "tmpfs"),
        ({"Tmpfs": {"/var/tmp": TMP}}, "tmpfs"),
        # host config that widens the box
        ({"Sysctls": {"net.ipv4.ip_forward": "1"}}, "host_config"),
        ({"PortBindings": {"80/tcp": [{"HostPort": "8080"}]}}, "host_config"),
        ({"GroupAdd": ["0"]}, "host_config"),
        # network: none or the bridge only, whatever the plan allowed
        ({"NetworkMode": "host"}, "network"),
        ({"NetworkMode": "container:abc"}, "network"),
        ({"NetworkMode": "mynet"}, "network"),
        ({"NetworkMode": None}, "network"),
    ],
)
@pytest.mark.parametrize("network_allowed", [False, True])
def test_each_field_outside_the_allow_list_is_refused_with_its_code(
    changes: dict[str, Any], what: str, network_allowed: bool
) -> None:
    with pytest.raises(SandboxViolationError) as exc:
        _check(_with(**changes), network=network_allowed)
    assert exc.value.what == what


@pytest.mark.parametrize("role", ["setup", "envbuild"])
def test_the_allow_list_holds_for_setup_and_envbuild_too(role: str) -> None:
    for changes in (
        {"NetworkMode": "host"},
        {"CgroupnsMode": "host"},
        {"MemorySwap": -1},
        {"Tmpfs": {"/tmp": TMP, "/x": "rw"}},
    ):
        host = _with(ReadonlyRootfs=False, **changes)
        with pytest.raises(SandboxViolationError):
            _check(host, role=role, network=True)


def test_without_planned_limits_only_the_shape_is_checked() -> None:
    """The runtime always passes the plan; without it a larger but internally
    consistent box passes, a swap-unlimited or extra-tmpfs one does not."""
    _check(_with(Memory=MEM * 4, MemorySwap=MEM * 4), limits=None)
    _check(_with(Tmpfs={"/tmp": "rw,size=1m"}), limits=None)
    for changes in (
        {"MemorySwap": -1},
        {"Tmpfs": {"/tmp": "rw"}},
        {"Tmpfs": {"/tmp": TMP, "/x": "rw"}},
    ):
        with pytest.raises(SandboxViolationError):
            _check(_with(**changes), limits=None)


def test_the_runtime_asserts_against_the_plan_not_its_own_kwargs() -> None:
    """A changed create kwarg must not move the bar: the expected limits come
    from ``spec.limits`` and the runtime's configured ``/tmp`` options."""
    pytest.importorskip("docker", reason="needs the agent extra (docker SDK)")
    from pathlib import Path

    from meshforge.agent.execution import docker_runtime
    from meshforge.agent.execution.docker_runtime import DockerConfig, DockerRuntime
    from meshforge.agent.execution.runtime import RunLimits, RunSpec
    from meshforge.common.ids import new_attempt_id

    runtime = DockerRuntime(DockerConfig(agent_label="n", tmp_size_mb=64))
    spec = RunSpec(
        attempt_id=str(new_attempt_id()),
        image="img",
        argv=("python", "-c", "1"),
        workdir=Path(WORK),
        env={},
        limits=RunLimits(timeout_s=10, memory_bytes=MEM, pids=64),
    )
    seen: dict[str, Any] = {}

    def fake_check(host: Any, **kw: Any) -> None:
        seen.update(kw)

    class _C:
        attrs: dict[str, Any] = {"HostConfig": {}}  # noqa: RUF012 - a fake

    original = docker_runtime.check_host_config
    docker_runtime.check_host_config = fake_check  # type: ignore[assignment]
    try:
        runtime._assert_sandbox(
            _C(),  # type: ignore[arg-type]
            spec,
            "run",
            {"mem_limit": 1 << 40, "tmpfs": {"/tmp": "rw,size=900g"}},
        )
    finally:
        docker_runtime.check_host_config = original  # type: ignore[assignment]
    assert seen["limits"] == ExpectedLimits(MEM, "rw,nosuid,nodev,size=64m")


# ------------------------------------------------------------------ create options


@pytest.mark.parametrize(
    "extra",
    ["oom_kill_disable", "privileged", "sysctls", "ports", "volumes", "cgroup_parent"],
)
def test_a_create_option_outside_the_runtimes_own_set_is_refused(extra: str) -> None:
    """Some options are not visible in ``HostConfig`` (cgroup v2 drops
    ``oom_kill_disable``), so the request itself is checked too."""
    from meshforge.agent.execution.sandbox_check import check_create_options

    with pytest.raises(SandboxViolationError) as exc:
        check_create_options({"command": ["x"], extra: True})
    assert exc.value.what == "create_options"


def test_every_option_the_runtime_really_uses_is_allowed(tmp_path: Any) -> None:
    pytest.importorskip("docker", reason="needs the agent extra (docker SDK)")
    import docker

    from meshforge.agent.envs.builder import EnvBuilder, EnvsConfig
    from meshforge.agent.execution.docker_runtime import DockerConfig, DockerRuntime
    from meshforge.agent.execution.runtime import RunLimits, RunSpec
    from meshforge.agent.execution.sandbox_check import check_create_options
    from meshforge.common.ids import new_attempt_id

    envs = EnvBuilder(EnvsConfig(root=tmp_path / "envs"))
    runtime = DockerRuntime(DockerConfig(agent_label="n"), envs=envs)
    spec = RunSpec(
        attempt_id=str(new_attempt_id()),
        image="img",
        argv=("python", "-c", "1"),
        workdir=tmp_path,
        env={},
        limits=RunLimits(timeout_s=10, memory_bytes=MEM, pids=64, cpus=1.5),
        gpu_indices=(0,),
        setup_argv=("pip", "install"),
    )
    for role in ("run", "setup"):
        kwargs = runtime._create_kwargs(docker, spec, role)
        check_create_options(kwargs)
        assert kwargs["cgroupns"] == "private"
    build = runtime._envbuild_kwargs(docker, spec, "base", "h" * 64, tmp_path)
    check_create_options(build)
    assert build["cgroupns"] == "private"
