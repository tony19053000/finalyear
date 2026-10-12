"""V0.4 G3 tester round 1: `[agent.execution]` bounds (brief D45/D51, roadmap
0.4.17 item checklist 9 - "max accepted AND max+1 rejected" for every numeric),
plus the two off-by-default opt-ins and the digest-pin / run_as validators.

Pure model construction - no Docker, no network; runs on a CPU-only Windows
laptop after a plain `uv sync` (checklist item 2).
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from meshforge.agent.settings import (
    CUDA_PYTORCH_IMAGE,
    PYTHON_CPU_IMAGE,
    ExecutionImages,
    ExecutionSettings,
)


def test_execution_is_disabled_by_default() -> None:
    settings = ExecutionSettings()
    # V0.7 G5 (D142): deprecated keys, unset by default; the opt-ins live in
    # [agent.policy] (off by default, test_v07_g5_policy_settings.py).
    assert settings.enabled is None
    assert settings.allow_unsandboxed is None


@pytest.mark.parametrize(
    ("field", "max_value", "over_value"),
    [
        ("grace_s", 3.0, 3.0000001),
        ("default_timeout_s", 604_800.0, 604_800.0000001),
        ("max_timeout_s", 604_800.0, 604_800.0000001),
        ("default_memory_mb", 1_048_576, 1_048_577),
        ("default_cpus", 1024, 1024.0000001),
        ("default_pids", 65_536, 65_537),
        ("tmp_size_mb", 65_536, 65_537),
        ("shm_size_mb", 65_536, 65_537),
        ("env_timeout_s", 7200.0, 7200.0000001),
        ("docker_timeout_s", 300.0, 300.0000001),
    ],
)
def test_max_accepted_and_max_plus_one_rejected(
    field: str, max_value: float, over_value: float
) -> None:
    kwargs: dict[str, Any] = (
        {"max_timeout_s": 604_800.0} if field == "default_timeout_s" else {}
    )
    accepted = ExecutionSettings(**{**kwargs, field: max_value})
    assert getattr(accepted, field) == max_value
    with pytest.raises(ValidationError):
        ExecutionSettings(**{**kwargs, field: over_value})


@pytest.mark.parametrize(
    ("field", "min_value", "under_value"),
    [
        ("default_memory_mb", 64, 63),
        ("default_pids", 16, 15),
        ("tmp_size_mb", 16, 15),
        ("shm_size_mb", 16, 15),
        ("docker_timeout_s", 1.0, 0.9999999),
    ],
)
def test_min_accepted_and_min_minus_one_rejected(
    field: str, min_value: float, under_value: float
) -> None:
    kwargs: dict[str, Any] = {field: min_value}
    accepted = ExecutionSettings(**kwargs)
    assert getattr(accepted, field) == min_value
    bad_kwargs: dict[str, Any] = {field: under_value}
    with pytest.raises(ValidationError):
        ExecutionSettings(**bad_kwargs)


def test_grace_s_zero_and_negative_rejected() -> None:
    with pytest.raises(ValidationError):
        ExecutionSettings(grace_s=0)
    with pytest.raises(ValidationError):
        ExecutionSettings(grace_s=-1)


def test_default_timeout_over_max_timeout_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ExecutionSettings(default_timeout_s=100.0, max_timeout_s=50.0)


@pytest.mark.parametrize("run_as", ["0:1000", "1000:0", "0:0", "root", "1000", "1000:"])
def test_run_as_rejects_root_and_malformed_values(run_as: str) -> None:
    with pytest.raises(ValidationError):
        ExecutionSettings(run_as=run_as)


@pytest.mark.parametrize("run_as", ["auto", "1000:1000"])
def test_run_as_accepts_auto_and_nonroot_numeric(run_as: str) -> None:
    assert ExecutionSettings(run_as=run_as).run_as == run_as


@pytest.mark.parametrize(
    "image",
    [
        "python:3.12-slim",  # tag only, no digest
        "python:3.12-slim@sha256:" + "g" * 64,  # not hex
        "python:3.12-slim@sha256:" + "a" * 63,  # too short
        "PYTHON:3.12-SLIM@sha256:" + "a" * 64,  # uppercase
        "",
    ],
)
def test_images_reject_anything_not_digest_pinned(image: str) -> None:
    with pytest.raises(ValidationError):
        ExecutionImages(python_cpu=image)


def test_default_images_are_digest_pinned_and_match_the_documented_constants() -> None:
    images = ExecutionImages()
    assert images.python_cpu == PYTHON_CPU_IMAGE
    assert images.cuda_pytorch == CUDA_PYTORCH_IMAGE
    assert "@sha256:" in images.python_cpu
    assert "@sha256:" in images.cuda_pytorch
