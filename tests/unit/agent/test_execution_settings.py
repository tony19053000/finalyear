"""V0.4 G3 (D45/D51): ``[agent.execution]`` defaults and every bound (max accepted,
max+1 rejected)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from meshforge.agent.settings import (
    CUDA_PYTORCH_IMAGE,
    GRACE_BOUND_S,
    PYTHON_CPU_IMAGE,
    AgentSettings,
    ExecutionImages,
    ExecutionSettings,
)

pytestmark = pytest.mark.unit

DIGEST = "@sha256:" + "a" * 64


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in list(os.environ):
        if name.upper().startswith("MESHFORGE_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))


def test_execution_is_off_and_sandboxed_by_default() -> None:
    agent = AgentSettings.load().agent
    e = agent.execution
    # V0.7 G5 (D142): the opt-ins moved to [agent.policy]; the old keys are
    # unset (None) unless a configuration still uses them.
    assert e.enabled is None and e.allow_unsandboxed is None
    assert agent.policy.accept_jobs is False
    assert agent.policy.allow_unsandboxed is False
    assert agent.policy.allow_network is False
    assert e.grace_s == GRACE_BOUND_S == 3.0
    assert e.images.python_cpu == PYTHON_CPU_IMAGE
    assert e.images.cuda_pytorch == CUDA_PYTORCH_IMAGE
    assert e.run_as == "auto"
    assert e.workdir_root is None


def test_environment_can_enable_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MESHFORGE_AGENT__EXECUTION__ENABLED", "true")
    monkeypatch.setenv("MESHFORGE_AGENT__EXECUTION__GRACE_S", "1.5")
    e = AgentSettings.load().agent.execution
    assert e.enabled is True and e.grace_s == 1.5


@pytest.mark.parametrize("image", [PYTHON_CPU_IMAGE, CUDA_PYTORCH_IMAGE])
def test_default_images_are_digest_pinned(image: str) -> None:
    name, _, digest = image.partition("@sha256:")
    assert name and len(digest) == 64 and int(digest, 16) >= 0


@pytest.mark.parametrize(
    "image",
    [
        "python:3.12-slim",  # tag only: can move
        "python@sha256:" + "a" * 63,  # short digest
        "python@sha256:" + "A" * 64,  # uppercase
        "Python:3.12" + DIGEST,  # uppercase name
        "python:3.12 " + DIGEST,  # whitespace
        "",
        "x" * 202 + DIGEST,  # over the length bound
    ],
)
def test_unpinned_or_malformed_images_are_refused(image: str) -> None:
    with pytest.raises(ValidationError):
        ExecutionImages(python_cpu=image)


def test_image_length_bound_edge() -> None:
    ExecutionImages(python_cpu="x" * 201 + DIGEST)  # max accepted
    with pytest.raises(ValidationError):
        ExecutionImages(python_cpu="x" * 202 + DIGEST)  # max + 1


# (field, max accepted, first rejected above, min accepted, first rejected below)
BOUNDS: list[tuple[str, Any, Any, Any, Any]] = [
    ("grace_s", 3.0, 3.0001, 0.001, 0.0),
    ("default_memory_mb", 1_048_576, 1_048_577, 64, 63),
    ("default_pids", 65_536, 65_537, 16, 15),
    ("tmp_size_mb", 65_536, 65_537, 16, 15),
    ("shm_size_mb", 65_536, 65_537, 16, 15),
    ("env_timeout_s", 7200.0, 7200.001, 0.001, 0.0),
    ("docker_timeout_s", 300.0, 300.001, 1.0, 0.999),
    ("max_timeout_s", 604_800.0, 604_800.001, 3600.0, 0.0),
    ("default_cpus", 1024, 1024.001, 0.01, 0.0),
]


@pytest.mark.parametrize(("field", "hi", "too_hi", "lo", "too_lo"), BOUNDS)
def test_numeric_bounds(field: str, hi: Any, too_hi: Any, lo: Any, too_lo: Any) -> None:
    extra: dict[str, Any] = {}
    if field == "max_timeout_s":
        extra["default_timeout_s"] = 1.0
    ExecutionSettings(**{field: hi, **extra})
    ExecutionSettings(**{field: lo, **extra})
    with pytest.raises(ValidationError):
        ExecutionSettings(**{field: too_hi, **extra})
    with pytest.raises(ValidationError):
        ExecutionSettings(**{field: too_lo, **extra})


def test_default_timeout_must_fit_under_the_max() -> None:
    ExecutionSettings(default_timeout_s=100.0, max_timeout_s=100.0)
    with pytest.raises(ValidationError, match="default_timeout_s"):
        ExecutionSettings(default_timeout_s=100.001, max_timeout_s=100.0)


@pytest.mark.parametrize("value", ["auto", "1000:1000", "65534:65534", " 5:7 "])
def test_run_as_accepts_auto_and_non_root(value: str) -> None:
    assert ExecutionSettings(run_as=value).run_as == value.strip()


@pytest.mark.parametrize(
    "value",
    [
        "0:0",
        "0:1000",
        "1000:0",
        "root",
        "1000",
        "1000:",
        "-1:1",
        "1:1:1",
        "12345678901:1",
    ],
)
def test_run_as_refuses_root_and_garbage(value: str) -> None:
    with pytest.raises(ValidationError):
        ExecutionSettings(run_as=value)


def test_unknown_keys_are_errors() -> None:
    with pytest.raises(ValidationError):
        ExecutionSettings(enable=True)  # type: ignore[call-arg]
