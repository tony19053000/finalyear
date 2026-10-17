"""V0.8 G7 fix round 1 (T-1): the leak verdict does not depend on test order.

Tester r1 showed a test sorted after the gate (or a ``-k`` subset without the
gate) was never judged. Here a real pytest run (a subprocess, the real
``tests/e2e/conftest.py``) gets one throw-away test file whose test plants a
credential shape in the session ``leak_watch``: the run must exit non-zero
with the leak named - with the gate file left out entirely, and with a clean
test before and after it. The planted file is removed afterwards.
"""

from __future__ import annotations

import os
import secrets
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip(
    "playwright", reason="needs the dev dependency group (pytest-playwright)"
)

ROOT = Path(__file__).resolve().parents[2]
E2E = Path(__file__).resolve().parent

PLANTED = """
import pytest

pytestmark = pytest.mark.e2e


def test_a_clean_one(leak_watch) -> None:
    leak_watch.places.append(("clean", "nothing here"))


def test_late_leak(leak_watch) -> None:
    leak_watch.places.append(("late test: localStorage", "mfat_" + "A" * 30))


def test_z_after(leak_watch) -> None:
    pass
"""


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=300,
        env=env,
        check=False,
    )


@pytest.fixture
def planted() -> Iterator[Path]:
    path = E2E / f"test_zzzz_tmp_late_leak_{secrets.token_hex(4)}.py"
    path.write_text(PLANTED, "utf-8")
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)


def test_a_leak_fails_the_run_whatever_runs_and_in_whatever_order(
    planted: Path,
) -> None:
    rel = str(planted.relative_to(ROOT))
    for selection in (
        [rel],  # the gate file is not in the run at all
        [rel, "-k", "late_leak"],  # a -k subset of exactly the leaking test
        [rel, "-p", "no:randomly"],  # leak between two clean tests
    ):
        done = _run(*selection)
        output = done.stdout + done.stderr
        assert done.returncode != 0, (selection, output[-3000:])
        assert "credentials reached the browser" in output, output[-3000:]
        assert "late test: localStorage: holds a mfat... shape" in output
        assert "A" * 30 not in output  # the value itself is never printed


def test_a_clean_subset_passes(planted: Path) -> None:
    rel = str(planted.relative_to(ROOT))
    done = _run(rel, "-k", "clean_one")
    assert done.returncode == 0, (done.stdout + done.stderr)[-3000:]
