"""Independent tester coverage for ``scripts/pytest_exit_guard.py`` (exit-139 fix).

Written by the tester agent against ff7095d, adversarially: it tries inputs the
implementer's own ``tests/unit/test_pytest_exit_guard.py`` does not cover, derived
from the guard's *purpose* (never let a crash hide behind a green summary; never
return 0 when pytest did not), not from its current implementation.

Two of the tests below (``test_ansi_colored_green_summary_is_still_recognised``
and ``test_summary_further_back_than_the_tail_buffer_is_still_found``) currently
**fail** and document real defects; see the tester's report for reproduction and
severity. They are left as normal (not xfail) assertions of the behaviour the
guard's own docstring promises, per instructions to never weaken a test to get
green.
"""

from __future__ import annotations

import importlib.util
import sys
import textwrap
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "pytest_exit_guard.py"
POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("mf_pytest_exit_guard_tester", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


guard = load()


def fake_run(body: str) -> list[str]:
    return [sys.executable, "-c", textwrap.dedent(body)]


# ------------------------------------------------------ E: never return 0 on failure
@pytest.mark.parametrize(
    ("body", "expected_nonzero"),
    [
        # exit 5: "no tests collected"
        ('import sys; print("no tests ran in 0.00s"); sys.exit(5)', 5),
        # KeyboardInterrupt-shaped exit 2, no parseable summary at all
        (
            'import sys; print("!!! KeyboardInterrupt !!!"); sys.exit(2)',
            2,
        ),
        # "passed" appears inside an ordinary log line, real summary is red
        (
            "import sys\n"
            'print("test_foo passed the assertion and kept going")\n'
            'print("1 failed, 3 passed in 0.50s")\n'
            "sys.exit(1)",
            1,
        ),
        # xpassed under xfail_strict: pytest exits 1 even though nothing is
        # literally named "failed"/"error" in the summary line.
        ('import sys; print("1 xpassed in 0.10s"); sys.exit(1)', 1),
    ],
)
def test_guard_never_returns_zero_when_pytest_did_not(
    body: str, expected_nonzero: int
) -> None:
    assert guard.main(command=fake_run(body)) == expected_nonzero


def test_no_tests_collected_exit5_is_reported_as_a_failure(
    capsys: pytest.CaptureFixture[str],
) -> None:
    body = 'import sys; print("no tests ran in 0.00s"); sys.exit(5)'
    assert guard.main(command=fake_run(body)) == 5
    err = capsys.readouterr().err
    assert "pytest failed" in err
    assert "summary is green" not in err  # "no tests ran" must not read as green


# --------------------------------------------------- E: non-UTF-8 bytes, huge output
def test_non_utf8_bytes_do_not_crash_the_guard(
    capfdbinary: pytest.CaptureFixture[bytes],
) -> None:
    # Binary capture: the guard forwards raw bytes (reviewer finding on d9ec26f),
    # which capsys's strict UTF-8 decode cannot represent.
    body = (
        "import sys\n"
        "sys.stdout.buffer.write(b'garbage \\xff\\xfe bytes\\n')\n"
        "sys.stdout.buffer.write(b'3 passed in 0.1s\\n')\n"
        "sys.stdout.buffer.flush()\n"
    )
    assert guard.main(command=fake_run(body)) == 0
    out = capfdbinary.readouterr().out
    assert b"garbage \xff\xfe bytes\n" in out
    assert b"3 passed" in out


def test_a_very_long_run_still_exits_zero_on_success(
    capsys: pytest.CaptureFixture[str],
) -> None:
    body = (
        "import sys\n"
        "for i in range(5000):\n"
        "    print(f'line {i}')\n"
        "print('1 passed in 0.10s')\n"
    )
    assert guard.main(command=fake_run(body)) == 0
    assert "1 passed" in capsys.readouterr().out


# ------------------------------------------------------------- F-adjacent, defect
@POSIX_ONLY
def test_ansi_colored_green_summary_is_still_recognised(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """DEFECT: CI sets ``FORCE_COLOR: "1"`` for every job (.github/workflows/ci.yml),
    and pytest honours that env var even when stdout is piped (verified: `FORCE_COLOR=1
    uv run python scripts/pytest_exit_guard.py ... -q` emits ANSI-wrapped output). The
    summary regex has no ``\\x1b\\[...m`` stripping, so a colored green summary is not
    recognised as green: the exit code is still correctly propagated (not the point
    of this test), but the guard's entire reason for existing - naming a green-summary
    shutdown crash as such - silently does not fire under the exact environment CI
    itself sets.
    """
    death = "os.kill(os.getpid(), signal.SIGSEGV)"
    body = f"""
        import os, signal, sys
        print("\\x1b[32m\\x1b[1m3214 passed\\x1b[0m\\x1b[32m in 51.95s\\x1b[0m")
        sys.stdout.flush()
        {death}
    """
    assert guard.main(command=fake_run(body)) == 139
    err = capsys.readouterr().err
    assert "summary is green but pytest did not exit 0" in err


@POSIX_ONLY
def test_summary_further_back_than_the_tail_buffer_is_still_found(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """DEFECT: a crash handler (Python's ``faulthandler``, common exactly on a
    SIGSEGV, or a noisy atexit hook) can print well past the 50-line tail buffer
    after the summary; the guard then cannot see the summary at all and falls
    back to the generic "pytest failed" message instead of naming the crash.
    """
    death = "os.kill(os.getpid(), signal.SIGSEGV)"
    body = f"""
        import os, signal, sys
        print("3214 passed in 51.95s")
        sys.stdout.flush()
        for i in range(80):
            print(f"post-summary line {{i}}")
        sys.stdout.flush()
        {death}
    """
    assert guard.main(command=fake_run(body)) == 139
    err = capsys.readouterr().err
    assert "summary is green but pytest did not exit 0" in err
