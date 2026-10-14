"""V0.7 G6 item 0 (WS-I4): ``[agent.envs] only_binary`` reaches every installer.

* default on: ``--only-binary :all:`` in the venv / ``--target`` installer
  argv (the real-uv proof is ``tests/component/envs/test_v07_g6_only_binary_real.py``)
  and in the Docker ``envbuild`` container's ``pip install`` command;
* off (the escape hatch): no such option anywhere;
* an install failure in binary-only mode names the escape hatch.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from meshforge.agent.envs.builder import (
    ONLY_BINARY_OPTIONS,
    EnvBuilder,
    EnvsConfig,
    only_binary_hint,
)
from meshforge.agent.settings import AgentSettings
from meshforge.common.ids import new_attempt_id

pytestmark = pytest.mark.unit


def test_the_default_is_wheels_only(tmp_path: Path) -> None:
    settings = AgentSettings.load(
        data_dir=tmp_path, agent={"controller_address": "127.0.0.1:1"}
    )
    assert settings.agent.envs.only_binary is True
    cfg = EnvsConfig.from_settings(settings)
    assert cfg.only_binary is True
    assert cfg.binary_options() == ["--only-binary", ":all:"]
    off = AgentSettings.load(
        data_dir=tmp_path,
        agent={"controller_address": "127.0.0.1:1", "envs": {"only_binary": False}},
    )
    assert EnvsConfig.from_settings(off).binary_options() == []


def test_the_hint_names_the_escape_hatch() -> None:
    assert "only_binary = false" in only_binary_hint(True)
    assert only_binary_hint(False) == ""


class _RecordedError(Exception):
    def __init__(self, argv: list[str]) -> None:
        super().__init__("recorded")
        self.argv = argv


def _recorded_argv(tmp_path: Path, *, only_binary: bool, kind: str) -> list[str]:
    import asyncio

    builder = EnvBuilder(
        EnvsConfig(
            root=tmp_path / "envs", free_disk_floor_bytes=0, only_binary=only_binary
        ),
        uv_argv=("uv",),
    )
    seen: list[list[str]] = []

    async def fake_install(argv: list[str], **_kw: object) -> None:
        seen.append(list(argv))
        if "pip" in argv:
            raise _RecordedError(list(argv))

    builder._install = fake_install  # type: ignore[method-assign]

    async def interpreter(_python: str | None) -> object:
        from types import SimpleNamespace

        return SimpleNamespace(
            path=Path("/usr/bin/python3"), base="cpython-3.12", platform="x"
        )

    builder.find_interpreter = interpreter  # type: ignore[method-assign]

    async def run() -> None:
        if kind == "venv":
            await builder.ensure_venv(
                python=None, requirements=b"x==1\n", deadline=1e18
            )
        else:
            await builder.ensure_target(requirements=b"x==1\n", deadline=1e18)

    with pytest.raises(_RecordedError) as exc:
        asyncio.run(run())
    return exc.value.argv


@pytest.mark.security
@pytest.mark.parametrize("kind", ["venv", "target"])
def test_uv_installs_carry_the_option(tmp_path: Path, kind: str) -> None:
    argv = _recorded_argv(tmp_path, only_binary=True, kind=kind)
    i = argv.index("--only-binary")
    assert argv[i : i + 2] == list(ONLY_BINARY_OPTIONS)
    assert argv.index("-r") > i  # an option, before the requirements file


@pytest.mark.parametrize("kind", ["venv", "target"])
def test_the_escape_hatch_drops_it(tmp_path: Path, kind: str) -> None:
    argv = _recorded_argv(tmp_path / "off", only_binary=False, kind=kind)
    assert "--only-binary" not in argv


@pytest.mark.security
@pytest.mark.parametrize("only_binary", [True, False])
def test_the_docker_envbuild_command(tmp_path: Path, only_binary: bool) -> None:
    docker = pytest.importorskip("docker", reason="needs the agent extra (docker SDK)")
    from meshforge.agent.execution.docker_runtime import DockerConfig, DockerRuntime
    from meshforge.agent.execution.runtime import RunLimits, RunSpec

    envs = EnvBuilder(EnvsConfig(root=tmp_path / "envs", only_binary=only_binary))
    runtime = DockerRuntime(DockerConfig(agent_label="n"), envs=envs)
    spec = RunSpec(
        attempt_id=str(new_attempt_id()),
        image="img",
        argv=("python", "-c", "1"),
        workdir=tmp_path,
        env={},
        limits=RunLimits(timeout_s=10, memory_bytes=64 * 1024 * 1024, pids=64),
    )
    command = runtime._envbuild_kwargs(docker, spec, "python", "d" * 64, tmp_path)[
        "command"
    ]
    if only_binary:
        i = command.index("--only-binary")
        assert command[i : i + 2] == list(ONLY_BINARY_OPTIONS)
        assert command.index("-r") > i
    else:
        assert "--only-binary" not in command
