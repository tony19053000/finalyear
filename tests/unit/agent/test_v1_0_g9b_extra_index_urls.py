"""V1.0 G9-B (D436): ``[agent.envs] extra_index_urls`` - the owner adds package
indexes next to PyPI (the PyTorch CUDA wheel index for a venv GPU job).

* shape: http(s), no credentials, at most 4, de-duplicated, not with no_index;
* every installer gets ``--extra-index-url`` per entry: uv for the venv runtime
  and the subprocess ``--target`` dir, pip in the Docker build container;
  wheels-only (``--only-binary :all:``) stays;
* the env hash's ``source`` covers the list, and a config without it keeps the
  hash it had before D436 (no existing env is rebuilt by the upgrade);
* the job's own requirements still cannot add an index under only_binary.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from meshforge.agent.envs import EnvError, normalize_requirements
from meshforge.agent.envs.builder import ONLY_BINARY_OPTIONS, EnvBuilder, EnvsConfig
from meshforge.agent.settings import AgentSettings, EnvsSettings
from meshforge.common.ids import new_attempt_id

pytestmark = [pytest.mark.unit]

TORCH = "https://download.pytorch.org/whl/cu126"


def test_the_setting_shape() -> None:
    assert EnvsSettings().extra_index_urls == []
    ok = EnvsSettings(extra_index_urls=[f" {TORCH} ", TORCH, "http://mirror:8080/s"])
    assert ok.extra_index_urls == [TORCH, "http://mirror:8080/s"]
    for bad in (
        ["https://user:pw@host/simple"],
        ["file:///srv/wheels"],
        ["ftp://h/x"],
        ["https://h/" + "x" * 300],
        [""],
        [f"https://h{i}/s" for i in range(5)],
    ):
        with pytest.raises(ValidationError):
            EnvsSettings(extra_index_urls=bad)


def test_not_with_no_index(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="no_index"):
        EnvsSettings(no_index=True, find_links=tmp_path, extra_index_urls=[TORCH])


def test_from_settings_and_the_installer_options(tmp_path: Path) -> None:
    settings = AgentSettings.model_validate(
        {
            "data_dir": tmp_path / "data",
            "agent": {
                "controller_address": "127.0.0.1:47100",
                "envs": {
                    "index_url": "https://mirror.example/simple",
                    "extra_index_urls": [TORCH, "https://two.example/simple"],
                },
            },
        }
    )
    config = EnvsConfig.from_settings(settings)
    assert config.extra_index_urls == (TORCH, "https://two.example/simple")
    assert config.index_options() == [
        "--index-url",
        "https://mirror.example/simple",
        "--extra-index-url",
        TORCH,
        "--extra-index-url",
        "https://two.example/simple",
    ]


def _old_source_digest(config: EnvsConfig) -> str:
    """The pre-D436 formula (find_links not used here)."""
    if config.index_url is None and config.find_links is None and not config.no_index:
        return ""
    document = {
        "index_url": config.index_url,
        "no_index": config.no_index,
        "find_links": None,
    }
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def test_the_env_hash_source_covers_the_list_and_old_hashes_stay(
    tmp_path: Path,
) -> None:
    plain = EnvsConfig(root=tmp_path)
    mirror = EnvsConfig(root=tmp_path, index_url="https://mirror.example/simple")
    assert plain.source_digest() == "" == _old_source_digest(plain)
    assert mirror.source_digest() == _old_source_digest(mirror) != ""
    torch = EnvsConfig(root=tmp_path, extra_index_urls=(TORCH,))
    both = EnvsConfig(
        root=tmp_path,
        index_url="https://mirror.example/simple",
        extra_index_urls=(TORCH,),
    )
    other = EnvsConfig(root=tmp_path, extra_index_urls=("https://x.example/s",))
    digests = {
        plain.source_digest(),
        mirror.source_digest(),
        torch.source_digest(),
        both.source_digest(),
        other.source_digest(),
    }
    assert len(digests) == 5
    # the owner's order matters to uv (first index wins), so it is in the hash
    swapped = EnvsConfig(root=tmp_path, extra_index_urls=(TORCH, "https://x.e/s"))
    again = EnvsConfig(root=tmp_path, extra_index_urls=("https://x.e/s", TORCH))
    assert swapped.source_digest() != again.source_digest()


class _RecordedError(Exception):
    def __init__(self, argv: list[str]) -> None:
        super().__init__("recorded")
        self.argv = argv


@pytest.mark.parametrize("kind", ["venv", "target"])
def test_uv_installs_carry_every_extra_index_and_stay_wheels_only(
    tmp_path: Path, kind: str
) -> None:
    builder = EnvBuilder(
        EnvsConfig(
            root=tmp_path / "envs",
            free_disk_floor_bytes=0,
            extra_index_urls=(TORCH, "https://two.example/simple"),
        ),
        uv_argv=("uv",),
    )

    async def fake_install(argv: list[str], **_kw: object) -> None:
        if "pip" in argv:
            raise _RecordedError(list(argv))

    async def interpreter(_python: str | None) -> object:
        return SimpleNamespace(
            path=Path("/usr/bin/python3"), base="cpython-3.12", platform="x"
        )

    builder._install = fake_install  # type: ignore[method-assign]
    builder.find_interpreter = interpreter  # type: ignore[method-assign]

    async def run() -> None:
        if kind == "venv":
            await builder.ensure_venv(
                python=None, requirements=b"torch\n", deadline=1e18
            )
        else:
            await builder.ensure_target(requirements=b"torch\n", deadline=1e18)

    with pytest.raises(_RecordedError) as exc:
        asyncio.run(run())
    argv = exc.value.argv
    pairs = [argv[i + 1] for i, a in enumerate(argv) if a == "--extra-index-url"]
    assert pairs == [TORCH, "https://two.example/simple"]
    assert "--index-url" not in argv  # PyPI stays the main index
    i = argv.index("--only-binary")
    assert argv[i : i + 2] == list(ONLY_BINARY_OPTIONS)


def test_the_docker_envbuild_command_carries_them(tmp_path: Path) -> None:
    docker = pytest.importorskip("docker", reason="needs the agent extra (docker SDK)")
    from meshforge.agent.execution.docker_runtime import DockerConfig, DockerRuntime
    from meshforge.agent.execution.runtime import RunLimits, RunSpec

    envs = EnvBuilder(EnvsConfig(root=tmp_path / "envs", extra_index_urls=(TORCH,)))
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
    i = command.index("--extra-index-url")
    assert command[i + 1] == TORCH
    assert command.index("-r") > i and "--only-binary" in command


def test_the_jobs_own_file_still_cannot_add_an_index() -> None:
    with pytest.raises(EnvError) as info:
        normalize_requirements(
            f"--extra-index-url {TORCH}\ntorch\n".encode(), wheels_only=True
        )
    assert info.value.reason == "requirements_rejected"
