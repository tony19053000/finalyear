"""V0.7 G5 part 2 (roadmap 0.7.9, D143): the sandbox asserted on what Docker
created, and the daemon's seccomp default.

The good ``HostConfig`` below is what ``docker inspect`` returns for a
container created with the runtime's options (Docker 29, runc); every
assertion of :func:`check_host_config` is broken once and must be caught with
its own code.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from meshforge.agent.execution.sandbox_check import (
    ExpectedMount,
    SandboxViolationError,
    check_host_config,
    daemon_seccomp_problem,
)

pytestmark = pytest.mark.unit

WORK = "/home/me/.local/share/meshforge/work/att_x"
GOOD: dict[str, Any] = {
    "Binds": None,
    "NetworkMode": "none",
    "CapAdd": None,
    "CapDrop": ["ALL"],
    "IpcMode": "private",
    "PidMode": "",
    "Privileged": False,
    "ReadonlyRootfs": True,
    "SecurityOpt": ["no-new-privileges:true"],
    "Tmpfs": {"/tmp": "rw,nosuid,nodev,size=512m"},
    "UTSMode": "",
    "UsernsMode": "",
    "Memory": 67108864,
    "MemorySwap": 67108864,  # fix r1 (D160): asserted equal to Memory
    "CgroupnsMode": "private",
    "Sysctls": None,
    "PortBindings": {},
    "GroupAdd": None,
    "OomKillDisable": False,
    "PidsLimit": 64,
    "Devices": [],
    "Mounts": [{"Type": "bind", "Source": WORK, "Target": "/work"}],
    "MaskedPaths": ["/proc/acpi", "/proc/kcore"],
    "ReadonlyPaths": ["/proc/bus", "/proc/sys", "/proc/sysrq-trigger"],
    "VolumesFrom": None,
}
RUN_MOUNTS = [ExpectedMount(WORK, "/work", False)]


def _check(host: dict[str, Any], *, role: str = "run", network: bool = False) -> None:
    check_host_config(host, role=role, mounts=RUN_MOUNTS, network_allowed=network)


def test_the_real_hardened_config_passes() -> None:
    _check(GOOD)
    _check({**GOOD, "NetworkMode": "bridge"}, network=True)
    _check({**GOOD, "ReadonlyRootfs": False}, role="envbuild")


def _broken(**changes: Any) -> dict[str, Any]:
    host = copy.deepcopy(GOOD)
    host.update(changes)
    return host


@pytest.mark.parametrize(
    ("host", "what"),
    [
        (_broken(Privileged=True), "privileged"),
        (_broken(Privileged=None), "privileged"),
        (_broken(CapAdd=["SYS_ADMIN"]), "capabilities"),
        (_broken(CapDrop=["NET_RAW"]), "capabilities"),
        (_broken(CapDrop=None), "capabilities"),
        (_broken(SecurityOpt=[]), "no_new_privileges"),
        (
            _broken(SecurityOpt=["no-new-privileges:true", "seccomp=unconfined"]),
            "seccomp",
        ),
        (
            _broken(SecurityOpt=["no-new-privileges:true", "apparmor=unconfined"]),
            "seccomp",
        ),
        (
            _broken(SecurityOpt=["no-new-privileges:true", "label=disable"]),
            "seccomp",
        ),
        (_broken(MaskedPaths=[]), "proc"),
        (_broken(ReadonlyPaths=["/proc/bus"]), "proc"),
        (_broken(PidMode="host"), "namespaces"),
        (_broken(IpcMode="host"), "namespaces"),
        (_broken(UTSMode="host"), "namespaces"),
        (_broken(UsernsMode="host"), "namespaces"),
        (_broken(Devices=[{"PathOnHost": "/dev/sda"}]), "devices"),
        (_broken(PidsLimit=0), "pids_limit"),
        (_broken(PidsLimit=None), "pids_limit"),
        (_broken(Memory=0), "memory_limit"),
        (_broken(Tmpfs={}), "tmpfs"),
        (_broken(Tmpfs={"/tmp": "rw,nosuid"}), "tmpfs"),
        (_broken(Binds=["/var/run/docker.sock:/var/run/docker.sock"]), "mounts"),
        (_broken(VolumesFrom=["other"]), "mounts"),
        (
            _broken(
                Mounts=[
                    {"Type": "bind", "Source": WORK, "Target": "/work"},
                    {
                        "Type": "bind",
                        "Source": "/var/run/docker.sock",
                        "Target": "/var/run/docker.sock",
                    },
                ]
            ),
            "mounts",
        ),
        (
            _broken(Mounts=[{"Type": "bind", "Source": "/home/me", "Target": "/work"}]),
            "mounts",
        ),
        (
            _broken(
                Mounts=[
                    {
                        "Type": "bind",
                        "Source": WORK,
                        "Target": "/work",
                        "ReadOnly": True,
                    }
                ]
            ),
            "mounts",
        ),
        (
            _broken(Mounts=[{"Type": "volume", "Source": "v", "Target": "/work"}]),
            "mounts",
        ),
        (_broken(Mounts=[]), "mounts"),
        (_broken(ReadonlyRootfs=False), "read_only"),
        (_broken(NetworkMode="bridge"), "network"),
        (_broken(NetworkMode="host"), "network"),
    ],
)
def test_every_gap_is_refused_with_its_code(host: dict[str, Any], what: str) -> None:
    with pytest.raises(SandboxViolationError) as exc:
        _check(host)
    assert exc.value.what == what


def test_no_host_config_is_a_violation() -> None:
    with pytest.raises(SandboxViolationError) as exc:
        check_host_config(
            None,  # type: ignore[arg-type]
            role="run",
            mounts=RUN_MOUNTS,
            network_allowed=False,
        )
    assert exc.value.what == "inspect"


def test_envbuild_may_have_its_network_and_layer_but_not_other_gaps() -> None:
    staging = [ExpectedMount("/data/envs/staging/h", "/req", True)]
    host = _broken(
        ReadonlyRootfs=False,
        NetworkMode="bridge",
        Mounts=[
            {
                "Type": "bind",
                "Source": "/data/envs/staging/h",
                "Target": "/req",
                "ReadOnly": True,
            }
        ],
    )
    check_host_config(host, role="envbuild", mounts=staging, network_allowed=False)
    host["Privileged"] = True
    with pytest.raises(SandboxViolationError):
        check_host_config(host, role="envbuild", mounts=staging, network_allowed=False)


@pytest.mark.parametrize(
    ("options", "problem"),
    [
        (["name=apparmor", "name=seccomp,profile=builtin", "name=cgroupns"], None),
        (["name=seccomp,profile=default"], None),
        (["name=seccomp,profile=builtin", "name=rootless"], None),
        (["name=apparmor"], "docker_seccomp_off"),
        ([], "docker_seccomp_off"),
        (None, "docker_seccomp_off"),
        (["name=seccomp,profile=unconfined"], "docker_seccomp_unconfined"),
    ],
)
def test_the_daemon_must_confine_with_seccomp(
    options: list[str] | None, problem: str | None
) -> None:
    assert daemon_seccomp_problem({"SecurityOptions": options}) == problem


class _FakeClient:
    def __init__(self, options: list[str]) -> None:
        self.options = options

    def ping(self) -> bool:
        return True

    def version(self) -> dict[str, str]:
        return {"Version": "29.0"}

    def info(self) -> dict[str, Any]:
        return {"SecurityOptions": self.options, "Runtimes": {}}

    def close(self) -> None:
        return None


@pytest.mark.parametrize(
    ("options", "available", "reason"),
    [
        (["name=seccomp,profile=builtin"], True, ""),
        (["name=apparmor"], False, "docker_seccomp_off"),
        (["name=seccomp,profile=unconfined"], False, "docker_seccomp_unconfined"),
    ],
)
def test_the_probe_refuses_a_daemon_without_seccomp(
    monkeypatch: pytest.MonkeyPatch, options: list[str], available: bool, reason: str
) -> None:
    pytest.importorskip("docker", reason="needs the agent extra (docker SDK)")
    from meshforge.agent.execution import docker_support

    monkeypatch.setattr(docker_support, "open_client", lambda _t: _FakeClient(options))
    found = docker_support.probe_docker(
        timeout_s=1, host_has_nvidia_gpu=False, gpu_probe_image=None, agent_label="n"
    )
    assert (found.available, found.reason) == (available, reason)
