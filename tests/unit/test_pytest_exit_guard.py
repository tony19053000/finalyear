"""Unit tests for ``scripts/pytest_exit_guard.py``.

The guard exists because ``pytest -m unit`` once printed ``3214 passed`` and then
exited 139 (a segfault at interpreter shutdown), and three review rounds read the
summary instead of the exit code. So the central case here is exactly that shape:
a run whose summary is green and whose process then dies.
"""

from __future__ import annotations

import importlib.util
import signal
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
    """Import the script by path: ``scripts/`` is not an importable package."""
    spec = importlib.util.spec_from_file_location("mf_pytest_exit_guard", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


guard = load()


def fake_run(body: str) -> list[str]:
    """A command that runs ``body`` as a Python program (stands in for pytest)."""
    return [sys.executable, "-c", textwrap.dedent(body)]


GREEN_THEN = """
    import os, signal, sys
    print("....")
    print("3214 passed, 2 skipped, 2 xfailed in 51.95s")
    sys.stdout.flush()
    {death}
"""


# ------------------------------------------------------------- summary parsing
@pytest.mark.parametrize(
    "line",
    [
        "3214 passed, 2 skipped, 2 xfailed in 51.95s",
        "========== 3 passed in 0.12s ==========",
        "2 skipped, 2 xfailed in 126.06s (0:02:06)",
        "5 passed, 7 deselected, 1 warning in 1.00s",
    ],
)
def test_green_summaries_are_recognised(line: str) -> None:
    assert guard.summary_line(["noise", line, ""]) == line.strip()
    assert guard.is_green(line)


@pytest.mark.parametrize(
    "line",
    [
        "4 failed, 3210 passed, 2 skipped in 42.95s",
        "3214 errors in 1.0s",
        "=== 1 passed, 1 error in 0.50s ===",
    ],
)
def test_red_summaries_are_not_green(line: str) -> None:
    assert guard.summary_line([line]) == line.strip()
    assert not guard.is_green(line)


def test_output_without_a_summary_has_none() -> None:
    assert guard.summary_line(["collected 0 items", "no tests ran in 0.01s"]) is None
    assert not guard.is_green("no tests ran in 0.01s")


def test_the_last_summary_wins() -> None:
    lines = ["1 failed in 0.1s", "log line", "2 passed in 0.2s"]
    assert guard.summary_line(lines) == "2 passed in 0.2s"


# --------------------------------------------------------------- exit statuses
def test_a_signal_death_maps_to_the_shell_convention() -> None:
    status, how = guard.exit_status(-int(signal.SIGSEGV))
    assert status == 128 + int(signal.SIGSEGV)
    assert "SIGSEGV" in how


def test_a_windows_access_violation_is_named() -> None:
    status, how = guard.exit_status(0xC0000005)
    # Signed 32-bit form: sys.exit rejects values >= 2**31 on Windows, and this
    # one exits with the same 0xC0000005 code there.
    assert status == 0xC0000005 - 2**32
    assert status & 0xFFFFFFFF == 0xC0000005
    assert "access violation" in how


def test_a_plain_exit_status_passes_through() -> None:
    assert guard.exit_status(1) == (1, "exit status 1")


# ------------------------------------------------------------------ end to end
@POSIX_ONLY
def test_green_summary_then_segfault_fails_with_139(
    capsys: pytest.CaptureFixture[str],
) -> None:
    death = "os.kill(os.getpid(), signal.SIGSEGV)"
    assert guard.main(command=fake_run(GREEN_THEN.format(death=death))) == 139
    out, err = capsys.readouterr()
    assert "3214 passed" in out  # the output is passed through untouched
    assert "summary is green but pytest did not exit 0" in err
    assert "SIGSEGV" in err


def test_green_summary_then_nonzero_exit_fails_with_that_status(
    capsys: pytest.CaptureFixture[str],
) -> None:
    death = "os._exit(3)"
    assert guard.main(command=fake_run(GREEN_THEN.format(death=death))) == 3
    assert "summary is green but pytest did not exit 0" in capsys.readouterr().err


def test_a_red_run_fails_without_the_green_summary_diagnosis(
    capsys: pytest.CaptureFixture[str],
) -> None:
    body = 'print("1 failed, 2 passed in 0.10s"); raise SystemExit(1)'
    assert guard.main(command=fake_run(body)) == 1
    err = capsys.readouterr().err
    assert "pytest failed (exit status 1, exit 1)" in err
    assert "summary is green" not in err


def test_a_clean_run_exits_zero_and_says_nothing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert guard.main(command=fake_run('print("2 passed in 0.10s")')) == 0
    out, err = capsys.readouterr()
    assert "2 passed" in out
    assert err == ""


@POSIX_ONLY
def test_a_real_pytest_run_that_crashes_at_shutdown_is_caught(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The real bug's shape with a real pytest: every test passes, the summary
    # is printed, and the process then dies of SIGSEGV on its way out.
    (tmp_path / "test_crash_at_exit.py").write_text(
        textwrap.dedent(
            """
            import atexit, os, signal

            atexit.register(lambda: os.kill(os.getpid(), signal.SIGSEGV))

            def test_ok():
                assert True
            """
        ),
        encoding="utf-8",
    )
    argv = [
        str(tmp_path / "test_crash_at_exit.py"),
        "-q",
        "-p",
        "no:cacheprovider",
        "--rootdir",
        str(tmp_path),
        "-c",
        str(tmp_path / "none.ini"),
    ]
    (tmp_path / "none.ini").write_text("[pytest]\n", encoding="utf-8")
    assert guard.main(argv) == 139
    out, err = capsys.readouterr()
    assert "1 passed" in out
    assert "summary is green but pytest did not exit 0" in err


def test_a_run_failed_by_the_half_import_check_is_not_blamed_on_a_crash(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # tests/conftest.py fails the session after every test passed, so the
    # summary is green and the exit is 1; the diagnosis must name the real cause.
    body = """
        print("half-imported package(s) left in the pytest process: torch. ...")
        print("3 passed in 0.10s")
        raise SystemExit(1)
    """
    assert guard.main(command=fake_run(body)) == 1
    err = capsys.readouterr().err
    assert "found a half-imported package" in err
    assert "crash while the" not in err


def test_output_bytes_are_passed_through_undecoded(
    capfdbinary: pytest.CaptureFixture[bytes],
) -> None:
    # Not UTF-8 and not cp1252-encodable once decoded: re-encoding text would
    # raise on a non-UTF-8 Windows console, so the guard forwards raw bytes.
    body = """
        import sys
        sys.stdout.buffer.write(b"caf\\xe9 \\xff\\n1 passed in 0.01s\\n")
    """
    assert guard.main(command=fake_run(body)) == 0
    assert b"caf\xe9 \xff\n" in capfdbinary.readouterr().out


class _InterruptedStream:
    def __iter__(self) -> _InterruptedStream:
        return self

    def __next__(self) -> bytes:
        raise KeyboardInterrupt


class _InterruptedPopen:
    """Popen stand-in whose output stream raises Ctrl-C mid-run."""

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self.stdout = _InterruptedStream()
        self.returncode: int | None = None
        self.waited = False

    def __enter__(self) -> _InterruptedPopen:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def wait(self) -> int:
        self.waited = True
        self.returncode = 2
        return 2


def test_ctrl_c_waits_for_pytest_and_exits_130(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    made: list[_InterruptedPopen] = []

    def popen(*args: object, **kwargs: object) -> _InterruptedPopen:
        made.append(_InterruptedPopen(*args, **kwargs))
        return made[-1]

    monkeypatch.setattr(guard.subprocess, "Popen", popen)
    assert guard.main(["-q"]) == 130
    assert made[0].waited
    assert "interrupted" in capsys.readouterr().err
