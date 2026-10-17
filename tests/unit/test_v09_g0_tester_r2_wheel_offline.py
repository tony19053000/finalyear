"""V0.9 G0 tester round 2: the widened offline markers must still not hide a
real build error."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_PATH = Path(__file__).with_name("test_skeleton_edge_cases.py")
_spec = importlib.util.spec_from_file_location("skel_edge_for_wheel_r2", _PATH)
assert _spec is not None and _spec.loader is not None
SKEL = importlib.util.module_from_spec(_spec)
sys.modules["skel_edge_for_wheel_r2"] = SKEL
_spec.loader.exec_module(SKEL)


@pytest.mark.parametrize(
    "stderr",
    [
        "error: Failed to build `meshforge`\n  Caused by: build hook timed out after 600s",
        "hatchling: custom build hook: subprocess timed out while compiling protos",
        "error: Connection refused by the pre-commit hook script (exit 1)",
    ],
)
@pytest.mark.xfail(
    strict=True,
    reason="DEFECT-R2: bare 'timed out' / 'connection refused' skip the wheel "
    "test for a build-backend or hook failure that has nothing to do with the index",
)
def test_a_build_hook_timeout_is_not_an_unreachable_index(stderr: str) -> None:
    assert not SKEL.network_failure(stderr)


@pytest.mark.parametrize(
    "stderr",
    [
        "error: Failed to parse `pyproject.toml`",
        "error: No space left on device (os error 28)",
        "Traceback: AssertionError: expected wheel to contain meshforge/py.typed",
        "error: unreachable code reached in build script",
        "ValueError: hosts file unreachable_hosts missing",
    ],
)
def test_other_real_errors_still_are_not_offline(stderr: str) -> None:
    assert not SKEL.network_failure(stderr)
