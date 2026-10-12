"""V0.7 G5 tester round 2: odd-shaped HostConfig values against the allow-list."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from meshforge.agent.execution.sandbox_check import (
    CREATE_OPTIONS,
    ExpectedLimits,
    ExpectedMount,
    SandboxViolationError,
    check_create_options,
    check_host_config,
)

pytestmark = pytest.mark.unit

WORK = "/data/work/att_x"
MEM = 67108864
TMP = "rw,nosuid,nodev,size=512m"
PLAN = ExpectedLimits(memory_bytes=MEM, tmp_options=TMP)
REAL: dict[str, Any] = {
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
    "MaskedPaths": ["/proc/acpi"],
    "ReadonlyPaths": ["/proc/sysrq-trigger"],
}
MOUNTS = [ExpectedMount(WORK, "/work", False)]


def _bad(**changes: Any) -> None:
    host = copy.deepcopy(REAL)
    host.update(changes)
    with pytest.raises(SandboxViolationError):
        check_host_config(
            host, role="run", mounts=MOUNTS, network_allowed=False, limits=PLAN
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"NetworkMode": "HOST"},
        {"NetworkMode": "host "},
        {"NetworkMode": "container:abc"},
        {"NetworkMode": "Container:abc"},
        {"NetworkMode": ""},
        {"NetworkMode": None},
        {"NetworkMode": ["none"]},
        {"NetworkMode": "none\x00host"},
        {"PidMode": "HOST"},
        {"PidMode": "container:x"},
        {"PidMode": " host"},
        {"IpcMode": "HOST"},
        {"IpcMode": "container:x"},
        {"UTSMode": "host"},
        {"UsernsMode": "host"},
        {"CgroupnsMode": "host"},
        {"CgroupnsMode": "HOST"},
        {"Memory": str(MEM), "MemorySwap": str(MEM)},
        {"Memory": True, "MemorySwap": True},
        {"Memory": MEM + 1, "MemorySwap": MEM + 1},
        {"MemorySwap": MEM * 2},
        {"MemorySwap": 0},
        {"MemorySwap": None},
        {"MemorySwap": -1},
        {"Memory": 0, "MemorySwap": 0},
        {"Tmpfs": None},
        {"Tmpfs": {}},
        {"Tmpfs": [("/tmp", TMP)]},
        {"Tmpfs": {"/tmp": TMP + ",size=900g"}},
        {"Tmpfs": {"/tmp": "rw,nosuid,nodev,size=512m,exec"}},
        {"Tmpfs": {"/tmp": "size=512m"}},
        {"Tmpfs": {"/tmp": TMP, "/dev/shm": "rw,size=900g"}},
        {"Tmpfs": {"/tmp": TMP, "/": "rw"}},
        {"Tmpfs": {"/TMP": TMP}},
        {"Tmpfs": {"/tmp": 512}},
        {"SecurityOpt": ["no-new-privileges:true", "seccomp=/tmp/p.json"]},
        {"SecurityOpt": ["seccomp=unconfined", "no-new-privileges:true"]},
        {"SecurityOpt": ["no-new-privileges:true", "seccomp:unconfined"]},
        {"SecurityOpt": ["no-new-privileges:true", "SeCcOmP=unconfined"]},
        {"SecurityOpt": ["no-new-privileges:true", ""]},
        {"SecurityOpt": ["no-new-privileges:true", "no-new-privileges:false"]},
        {"SecurityOpt": "no-new-privileges:true"},
        {"SecurityOpt": []},
        {"Sysctls": {"a": "b"}},
        {"PortBindings": {"80/tcp": [{"HostPort": "1"}]}},
        {"GroupAdd": ["0"]},
        {"OomKillDisable": True},
        {"Privileged": None},
        {"CapAdd": ["NET_RAW"]},
        {"CapDrop": ["NET_RAW"]},
        {"Devices": [{"PathOnHost": "/dev/kvm"}]},
        {"PidsLimit": 0},
        {"PidsLimit": -1},
        {"PidsLimit": True},
        {"PidsLimit": None},
        {"MaskedPaths": []},
        {"MaskedPaths": None},
        {"ReadonlyPaths": ["/proc/bus"]},
        {"ReadonlyRootfs": None},
        {"Mounts": []},
        {"Mounts": [{"Type": "bind", "Source": WORK, "Target": "/work/"}]},
        {"Mounts": [{"Type": "bind", "Source": WORK + "/", "Target": "/work"}]},
        {"Mounts": [{"Type": "volume", "Source": "v", "Target": "/work"}]},
        {"Mounts": [{"Type": "tmpfs", "Target": "/work"}]},
        {"Mounts": [*REAL["Mounts"], {"Type": "bind", "Source": "/", "Target": "/h"}]},
        {"Binds": ["/:/h"]},
        {"VolumesFrom": ["other"]},
    ],
)
def test_odd_host_config_values_are_refused(changes: dict[str, Any]) -> None:
    _bad(**changes)


def test_the_real_shape_passes() -> None:
    check_host_config(
        copy.deepcopy(REAL),
        role="run",
        mounts=MOUNTS,
        network_allowed=False,
        limits=PLAN,
    )


@pytest.mark.parametrize(
    "extra",
    [
        "privileged",
        "cap_add",
        "pid_mode",
        "ipc_mode",
        "uts_mode",
        "userns_mode",
        "sysctls",
        "ports",
        "group_add",
        "devices",
        "volumes",
        "volumes_from",
        "oom_kill_disable",
        "cpuset_cpus",
        "ulimits",
        "dns",
        "extra_hosts",
        "links",
        "runtime",
        "security_opts",
        "cgroup_parent",
        "storage_opt",
        "mem_swappiness",
        "oom_score_adj",
        "ipc",
        "pid",
        "",
        "Command",
    ],
)
def test_a_create_option_outside_the_allow_list_is_refused(extra: str) -> None:
    options = dict.fromkeys(CREATE_OPTIONS)
    check_create_options(options)
    options[extra] = True
    with pytest.raises(SandboxViolationError):
        check_create_options(options)


def test_a_gpu_device_request_on_a_cpu_plan_is_refused() -> None:
    _bad(
        DeviceRequests=[
            {"Driver": "nvidia", "Count": -1, "Capabilities": [["gpu"]]},
        ]
    )


# T-10 fix (Lead): PidsLimit, NanoCpus and DeviceRequests match the plan.
def _check_t10(limits: ExpectedLimits, **changes: Any) -> None:
    host = copy.deepcopy(REAL)
    host.update(changes)
    check_host_config(
        host, role="run", mounts=MOUNTS, network_allowed=False, limits=limits
    )


T10_PLAN = ExpectedLimits(MEM, TMP, pids=64, nano_cpus=500_000_000)
GPU_PLAN = ExpectedLimits(MEM, TMP, pids=64, nano_cpus=500_000_000, gpu_ids=("1",))


def test_the_planned_values_pass() -> None:
    _check_t10(T10_PLAN, NanoCpus=500_000_000)
    _check_t10(
        GPU_PLAN,
        NanoCpus=500_000_000,
        DeviceRequests=[{"Count": 0, "DeviceIDs": ["1"]}],
    )


@pytest.mark.parametrize(
    ("limits", "changes"),
    [
        (T10_PLAN, {"NanoCpus": 500_000_000, "PidsLimit": 65}),
        (T10_PLAN, {"NanoCpus": 4_000_000_000}),
        (T10_PLAN, {"NanoCpus": 0}),
        (ExpectedLimits(MEM, TMP, nano_cpus=0), {"NanoCpus": 1}),
        (T10_PLAN, {"NanoCpus": 500_000_000, "DeviceRequests": [{"DeviceIDs": ["0"]}]}),
        (GPU_PLAN, {"NanoCpus": 500_000_000, "DeviceRequests": [{"Count": -1}]}),
        (
            GPU_PLAN,
            {"NanoCpus": 500_000_000, "DeviceRequests": [{"DeviceIDs": ["0", "1"]}]},
        ),
        (GPU_PLAN, {"NanoCpus": 500_000_000, "DeviceRequests": []}),
    ],
)
def test_a_value_off_the_plan_is_refused(
    limits: ExpectedLimits, changes: dict[str, Any]
) -> None:
    with pytest.raises(SandboxViolationError):
        _check_t10(limits, **changes)
