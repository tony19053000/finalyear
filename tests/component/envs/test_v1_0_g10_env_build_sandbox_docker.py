"""V1.0 G10 BUG-1 (D442) adversarial: the build temp folder is not a way out.

After the build the job runs on a read-only root filesystem: the build folder
must be empty, unwritable to the job, never its TMPDIR; a failing build is a
typed ``env_error`` and leaves no env image behind.
"""

from __future__ import annotations

import secrets
import sys
from pathlib import Path

import pytest

pytest.importorskip("docker", reason="needs the agent extra (docker SDK)")

from mf_envs_support import offline_builder
from test_v1_0_g10_env_build_tmp_docker import (  # type: ignore[import-not-found]
    TMPFS_MB,
    _big_wheel,
    _image_local,
    _Log,
    _spec,
    env_tags,  # noqa: F401 - fixture
)

from meshforge.agent.execution.runtime import RunResult
from meshforge.common.errors import FailureType

pytestmark = [
    pytest.mark.docker,
    pytest.mark.timeout(300),
    pytest.mark.skipif(sys.platform == "win32", reason="Linux containers on this host"),
]

SANDBOX_PROBE = """\
import os, tempfile
import {pkg}
b = "/meshforge-build-tmp"
print("TMPDIR", os.environ.get("TMPDIR"))
print("BUILD_LISTING", sorted(os.listdir(b)) if os.path.isdir(b) else "absent")
for target in (b + "/escape", "/escape", "/env/escape", "/usr/escape"):
    try:
        open(target, "w").close()
        print("WROTE", target)
    except OSError as exc:
        print("DENIED", target, exc.errno)
print("TEMPDIR", tempfile.gettempdir())
"""


@pytest.mark.asyncio
async def test_job_cannot_use_or_write_the_build_folder(
    tmp_path: Path,
    env_tags: list[str],  # noqa: F811
) -> None:
    from meshforge.agent.execution.docker_runtime import (
        ENV_IMAGE_REPO,
        DockerConfig,
        DockerRuntime,
    )
    from meshforge.agent.settings import PYTHON_CPU_IMAGE

    if not _image_local(PYTHON_CPU_IMAGE):
        pytest.skip(f"{PYTHON_CPU_IMAGE} is not local")
    pkg = f"mfsb{secrets.token_hex(4)}"
    _big_wheel(tmp_path / "wheels", pkg)
    runtime = DockerRuntime(
        DockerConfig(agent_label=f"g10s-{secrets.token_hex(4)}", tmp_size_mb=TMPFS_MB),
        envs=offline_builder(tmp_path / "envs", tmp_path / "wheels"),
    )
    spec = _spec(tmp_path, pkg, PYTHON_CPU_IMAGE)
    (spec.workdir / "probe.py").write_text(SANDBOX_PROBE.format(pkg=pkg))
    log = _Log()
    result: RunResult = await runtime.run(spec, log)
    if result.env_hash:
        env_tags.append(f"{ENV_IMAGE_REPO}:{result.env_hash}")
    text = log.text()
    assert result.ok, (result.failure_type, result.reason, text[-1500:])
    assert "TMPDIR /tmp\n" in text, text
    assert "TEMPDIR /tmp" in text
    assert "WROTE" not in text, text  # read-only root: no escape / no persistence
    assert text.count("DENIED") == 4, text
    # leftovers from pip would leak into every job: the folder must be empty
    assert "BUILD_LISTING []" in text, text


@pytest.mark.asyncio
async def test_a_failing_build_is_env_error_and_saves_no_image(
    tmp_path: Path,
    env_tags: list[str],  # noqa: F811
) -> None:
    from meshforge.agent.execution.docker_runtime import DockerConfig, DockerRuntime
    from meshforge.agent.execution.docker_support import open_client
    from meshforge.agent.settings import PYTHON_CPU_IMAGE

    if not _image_local(PYTHON_CPU_IMAGE):
        pytest.skip(f"{PYTHON_CPU_IMAGE} is not local")
    pkg = f"mfnone{secrets.token_hex(4)}"  # no wheel for it -> pip fails offline
    (tmp_path / "wheels").mkdir()
    label = f"g10f-{secrets.token_hex(4)}"
    runtime = DockerRuntime(
        DockerConfig(agent_label=label, tmp_size_mb=TMPFS_MB),
        envs=offline_builder(tmp_path / "envs", tmp_path / "wheels"),
    )
    log = _Log()
    result = await runtime.run(_spec(tmp_path, pkg, PYTHON_CPU_IMAGE), log)
    assert not result.ok
    assert result.failure_type is FailureType.ENV_ERROR, (
        result.reason,
        log.text()[-800:],
    )
    client = open_client(30)
    try:
        assert not client.containers.list(
            all=True, filters={"label": f"meshforge.agent={label}"}
        )  # build container cleaned up
    finally:
        client.close()
