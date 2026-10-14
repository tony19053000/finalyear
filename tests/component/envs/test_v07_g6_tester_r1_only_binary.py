"""V0.7 G6 tester r1: can a job's own ``requirements.txt`` switch wheels-only off?

Brief item 0 / THREAT_MODEL WS-I4: "only-binary and its escape hatch ... only the
laptop owner can turn it off, a manifest cannot". The requirements file is the
job author's text, and pip/uv honour option lines inside a requirements file
(``--no-binary``, ``-e``, ``--find-links``, a local source directory, a direct
sdist URL). Each of those must still never run package build code while
``only_binary = true``.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

import pytest

from meshforge.agent.envs.builder import EnvBuilder, EnvsConfig, default_uv_argv
from meshforge.agent.envs.errors import EnvError

pytestmark = [pytest.mark.component, pytest.mark.asyncio]

if not default_uv_argv():
    pytest.skip("the uv wheel is not installed (agent extra)", allow_module_level=True)

import test_v07_g6_only_binary_real as base  # noqa: E402


def _builder(root: Path, wheels: Path) -> EnvBuilder:
    return EnvBuilder(
        EnvsConfig(
            root=root,
            free_disk_floor_bytes=0,
            find_links=wheels,
            no_index=True,
            only_binary=True,
        )
    )


def _local_source_dir(folder: Path, marker: Path) -> Path:
    src = folder / "mfsrc-dir"
    src.mkdir(parents=True)
    (src / "pyproject.toml").write_bytes(base.PYPROJECT)
    (src / "mfback.py").write_text(base.BACKEND.format(marker=str(marker)))
    return src


@pytest.mark.security
@pytest.mark.parametrize(
    "lines",
    [
        "--no-binary :all:\nmfsrc==1.0\n",
        "--no-binary mfsrc\nmfsrc==1.0\n",
        "--no-binary=:all:\n--only-binary=:none:\nmfsrc==1.0\n",
        "--only-binary :none:\nmfsrc==1.0\n",
        "mfsrc==1.0\n--no-binary :all:\n",
    ],
    ids=["no-binary-all", "no-binary-pkg", "none-eq", "only-binary-none", "after"],
)
async def test_option_lines_in_the_job_requirements_cannot_disable_wheels_only(
    tmp_path: Path, lines: str
) -> None:
    wheels = tmp_path / "wheels"
    marker = tmp_path / "marker.txt"
    base._make_sdist(wheels, marker)
    builder = _builder(tmp_path / "envs", wheels)
    with contextlib.suppress(EnvError):  # refused is the right outcome
        await builder.ensure_target(requirements=lines.encode(), deadline=1e18)
    assert not marker.exists(), "package build code ran with only_binary = true"


async def test_a_local_source_directory_or_sdist_path_never_builds(
    tmp_path: Path,
) -> None:
    wheels = tmp_path / "wheels"
    marker = tmp_path / "marker.txt"
    sdist = base._make_sdist(wheels, marker)
    srcdir = _local_source_dir(tmp_path / "proj", marker)
    for line in (
        f"{srcdir}\n",
        f"-e {srcdir}\n",
        f"mfsrc @ file://{sdist}\n",
        f"{sdist}\n",
    ):
        builder = _builder(tmp_path / f"envs-{abs(hash(line))}", wheels)
        with contextlib.suppress(EnvError):
            await builder.ensure_target(requirements=line.encode(), deadline=1e18)
        assert not marker.exists(), f"build code ran for requirement line {line!r}"
