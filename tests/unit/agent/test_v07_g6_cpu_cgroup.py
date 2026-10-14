"""V0.7 G6 item 0 (D161 tester follow-up): CPU shares for the unsandboxed Linux
runtimes through a delegated cgroup v2, else the typed ``not_enforced`` flag.

Pure logic on a fake cgroup tree (a tmp folder): the probe's typed reasons, the
``cpu.max`` value, job cgroup names, the shim's ``cg=`` argument, the
``not_enforced`` list, and ``build_execution`` wiring. The real kernel
behaviour (a busy loop held to its share) is
``tests/component/agent/test_v07_g6_cpu_cgroup_real.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from meshforge.agent.execution.cpu_cgroup import (
    CpuCgroupProbe,
    CpuCgroups,
    cpu_max,
    probe_cpu_cgroups,
)
from meshforge.agent.policy import not_enforced_caps
from meshforge.agent.process import ProcessLimits, shim_limits
from meshforge.agent.settings import PolicySettings

pytestmark = [pytest.mark.unit, pytest.mark.security]


def _tree(
    tmp_path: Path,
    *,
    own: str = "/user.slice/u.service/agent",
    controllers: str = "cpu memory pids",
    subtree: str = "cpu",
) -> tuple[Path, Path]:
    root = tmp_path / "cg"
    root.mkdir(parents=True)
    (root / "cgroup.controllers").write_text("cpu memory pids io\n")
    leaf = root / own.lstrip("/")
    leaf.mkdir(parents=True)
    (leaf.parent / "cgroup.controllers").write_text(controllers + "\n")
    (leaf.parent / "cgroup.subtree_control").write_text(subtree + "\n")
    self_file = tmp_path / "self_cgroup"
    self_file.write_text(f"0::{own}\n")
    return root, self_file


def test_a_delegated_agent_is_usable(tmp_path: Path) -> None:
    root, self_file = _tree(tmp_path)
    found = probe_cpu_cgroups(root=root, self_file=self_file, platform="linux")
    assert found == CpuCgroupProbe(root / "user.slice" / "u.service")


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"own": "/user.slice/app.slice/term.scope"}, "not_delegated"),
        ({"controllers": "memory pids"}, "no_cpu_controller"),
        ({"subtree": ""}, "cannot_enable_cpu"),  # a regular file never echoes it
        ({"own": "/x;y/agent"}, "not_delegated"),  # unusable in the shim argument
    ],
)
def test_typed_reasons(tmp_path: Path, kwargs: dict[str, str], reason: str) -> None:
    root, self_file = _tree(tmp_path, **kwargs)
    found = probe_cpu_cgroups(root=root, self_file=self_file, platform="linux")
    assert found.parent is None and found.reason == reason


def test_not_linux_and_no_v2(tmp_path: Path) -> None:
    root, self_file = _tree(tmp_path)
    assert probe_cpu_cgroups(
        root=root, self_file=self_file, platform="win32"
    ).reason == ("not_linux")
    (root / "cgroup.controllers").unlink()
    assert probe_cpu_cgroups(
        root=root, self_file=self_file, platform="linux"
    ).reason == ("no_cgroup_v2")
    root2, self2 = _tree(tmp_path / "b")
    self2.write_text("1:name=systemd:/x\n")  # v1 only
    assert probe_cpu_cgroups(root=root2, self_file=self2, platform="linux").reason == (
        "no_cgroup_v2"
    )


def test_a_read_only_parent_is_not_writable(tmp_path: Path) -> None:
    root, self_file = _tree(tmp_path)
    parent = root / "user.slice" / "u.service"
    parent.chmod(0o555)
    try:
        found = probe_cpu_cgroups(root=root, self_file=self_file, platform="linux")
    finally:
        parent.chmod(0o755)
    assert found.reason == "not_writable"


def test_cpu_max() -> None:
    assert cpu_max(1.0) == "100000 100000"
    assert cpu_max(0.25) == "25000 100000"
    assert cpu_max(2.5) == "250000 100000"
    assert cpu_max(0.001) == "1000 100000"  # never below 1 ms
    assert cpu_max(0.123456) == "12346 100000"  # rounded up
    with pytest.raises(ValueError):
        cpu_max(0)


def test_job_cgroups_are_created_with_their_share(tmp_path: Path) -> None:
    cgroups = CpuCgroups(tmp_path)
    path = cgroups.create("ATT_01J0ABCDEF", 0.5)
    assert path == tmp_path / "mf-att_01j0abcdef"
    assert (path / "cpu.max").read_text() == "50000 100000\n"
    with pytest.raises(ValueError):
        cgroups.path_for("../escape")
    with pytest.raises(ValueError):
        cgroups.path_for("a;b")


def test_the_shim_argument_carries_the_cgroup() -> None:
    assert shim_limits(ProcessLimits(tasks=5, cgroup="/sys/fs/cgroup/x/mf-a")) == (
        "nproc=5;cg=/sys/fs/cgroup/x/mf-a"
    )
    with pytest.raises(ValueError):
        shim_limits(ProcessLimits(cgroup="/a;as=1"))


def test_not_enforced_follows_the_cgroup() -> None:
    policy = PolicySettings(accept_jobs=True, allow_unsandboxed=True)
    without = not_enforced_caps(policy, platform="linux")
    with_cg = not_enforced_caps(policy, platform="linux", cpu_cgroup=True)
    assert "max_cpu_percent:unsandboxed_linux" in without
    assert "max_cpu_percent:unsandboxed_linux" not in with_cg
    assert "max_vram_bytes:no_runtime_limit" in with_cg


def test_build_execution_hands_the_cgroups_to_both_runtimes(tmp_path: Path) -> None:
    import asyncio

    from meshforge.agent.execution.offer import build_execution
    from meshforge.agent.settings import AgentSettings

    (tmp_path / "parent").mkdir()
    settings = AgentSettings.load(
        data_dir=tmp_path / "data",
        agent={
            "controller_address": "127.0.0.1:1",
            "policy": {"accept_jobs": True, "allow_unsandboxed": True},
        },
    )

    class NoDocker:
        name = "docker"

        async def probe(self) -> object:
            from meshforge.agent.execution.runtime import RuntimeAvailability

            return RuntimeAvailability("docker", False, reason="no", sandboxed=True)

    async def build(probe: object) -> object:
        return await build_execution(
            settings,
            node_id="node_x",
            host_has_nvidia_gpu=False,
            docker_factory=lambda _c: NoDocker(),  # type: ignore[arg-type,return-value]
            cgroup_probe=probe,  # type: ignore[arg-type]
        )

    setup = asyncio.run(build(lambda: CpuCgroupProbe(tmp_path / "parent")))
    assert setup.cpu_cgroups is not None  # type: ignore[attr-defined]
    for name in ("subprocess", "venv"):
        runtime = setup.runtimes.get(name)  # type: ignore[attr-defined]
        if runtime is not None:
            assert runtime._cgroups is setup.cpu_cgroups  # type: ignore[attr-defined]
    none = asyncio.run(build(lambda: CpuCgroupProbe(None, "not_delegated")))
    assert none.cpu_cgroups is None  # type: ignore[attr-defined]
