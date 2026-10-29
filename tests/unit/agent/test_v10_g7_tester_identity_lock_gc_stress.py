"""V1.0 G7 tester: test_identity_lock must be clean even under hostile garbage collection.

Reruns ``test_identity_lock.py`` in a child pytest with the cyclic GC firing on every
allocation batch and inside every multiprocessing resource-tracker send, with
unraisable exceptions turned into errors (the CI symptom: 5 ReentrantCallError).
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]


def test_identity_lock_has_no_unraisable_exceptions_under_gc_stress(
    tmp_path: Path,
) -> None:
    (tmp_path / "mfgcstress.py").write_text(
        textwrap.dedent(
            """
            import gc
            import multiprocessing.resource_tracker as rt

            def pytest_configure(config):
                gc.set_threshold(1, 1, 1)
                orig = rt.ResourceTracker._send

                def send(self, *a, **k):
                    gc.collect()
                    return orig(self, *a, **k)

                rt.ResourceTracker._send = send
            """
        ),
        encoding="utf-8",
    )
    env = {**__import__("os").environ, "PYTHONPATH": str(tmp_path)}
    proc = subprocess.run(
        [
            sys.executable, "-m", "pytest", "-p", "mfgcstress", "-q",
            "-p", "no:cacheprovider",
            "-W", "error::pytest.PytestUnraisableExceptionWarning",
            str(REPO / "tests" / "unit" / "agent" / "test_identity_lock.py"),
        ],
        capture_output=True, text=True, env=env, cwd=REPO, timeout=300, check=False,
    )  # fmt: skip
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-1000:]
    assert "passed" in proc.stdout and "Unraisable" not in proc.stdout
