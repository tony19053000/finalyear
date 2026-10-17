"""V0.9 G0 tester round 1: the wheel packaging test must skip ONLY for
"the index is unreachable", never hide a real build error, and recognise the
usual shapes of "unreachable"."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_PATH = Path(__file__).with_name("test_skeleton_edge_cases.py")
_spec = importlib.util.spec_from_file_location("skel_edge_for_wheel_r1", _PATH)
assert _spec is not None and _spec.loader is not None
SKEL = importlib.util.module_from_spec(_spec)
sys.modules["skel_edge_for_wheel_r1"] = SKEL
_spec.loader.exec_module(SKEL)


@pytest.mark.parametrize(
    "stderr",
    [
        "error: Failed to parse `pyproject.toml`",
        "Failed to build `meshforge`; build backend failed: ModuleNotFoundError",
        "error: Failed to determine build requirements: invalid version specifier hatchling>=",
        "hatchling: ValueError: Unable to determine which files to ship",
        "error: Permission denied (os error 13)",
        "error: No space left on device (os error 28)",
        "TypeError: unsupported operand",
        "Traceback (most recent call last): ... KeyError: 'name'",
        "error: dnserrors module raised AssertionError",  # substring trap
    ],
)
def test_real_build_errors_are_never_classified_as_offline(stderr: str) -> None:
    assert not SKEL.network_failure(stderr)


def test_classification_is_case_insensitive() -> None:
    assert SKEL.network_failure("DNS ERROR: FAILED TO LOOKUP ADDRESS INFORMATION")


@pytest.mark.parametrize(
    "stderr",
    [
        "error: Request failed after 3 retries\n  Caused by: operation timed out",
        "error sending request for url (https://pypi.org/simple/hatchling/)\n"
        "  Caused by: connection refused",
        "Caused by: No route to host (os error 113)",
        "A socket operation was attempted to an unreachable network. (os error 10051)",
    ],
)
def test_other_common_unreachable_index_shapes_are_recognised(stderr: str) -> None:
    # DEFECT-7 fixed in G0 fix round 1 (dynamic xfail removed).
    assert SKEL.network_failure(stderr)


def test_build_wheel_runs_offline_retry_only_after_a_network_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fake ``uv``: a non-network failure must fail at once, with no --offline retry."""
    import subprocess

    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 2, "", "error: Failed to parse")

    monkeypatch.setattr(SKEL.subprocess, "run", fake_run)
    with pytest.raises(AssertionError, match="Failed to parse"):
        SKEL.build_wheel(tmp_path / "dist")
    assert len(calls) == 1 and "--offline" not in calls[0]


def test_build_wheel_offline_retry_can_succeed_from_the_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    out = tmp_path / "dist"
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        if "--offline" in cmd:
            out.mkdir(parents=True, exist_ok=True)
            (out / "meshforge-0.0.0-py3-none-any.whl").write_bytes(b"x")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return subprocess.CompletedProcess(cmd, 2, "", "dns error: failed to lookup")

    monkeypatch.setattr(SKEL.subprocess, "run", fake_run)
    wheel = SKEL.build_wheel(out)
    assert wheel.name.startswith("meshforge-") and len(calls) == 2


def test_offline_retry_failing_with_a_real_error_fails_not_skips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    def fake_run(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        if "--offline" in cmd:
            return subprocess.CompletedProcess(cmd, 2, "", "error: Failed to parse")
        return subprocess.CompletedProcess(cmd, 2, "", "dns error")

    monkeypatch.setattr(SKEL.subprocess, "run", fake_run)
    with pytest.raises(AssertionError):
        SKEL.build_wheel(tmp_path / "dist")
