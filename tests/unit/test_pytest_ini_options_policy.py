"""Tester (task 0.1.8): coverage for `[tool.pytest.ini_options]` behaviour that
`tests/unit/test_pytest_tier_policy.py` does not already exercise: `timeout`,
`filterwarnings = ["error"]`, `xfail_strict`, and `asyncio_mode = "strict"`.

Same technique as `test_pytest_tier_policy.py`: build a throwaway suite (real
`pyproject.toml` `[tool.pytest.ini_options]` + real `tests/conftest.py`) and run
pytest in a subprocess, so the *actual* configured behaviour is checked, not a
reimplementation of it.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

import pytest

pytest_plugins = ["pytester"]
pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_CONFTEST = (REPO_ROOT / "tests" / "conftest.py").read_text(encoding="utf-8")


def _ini_from_pyproject() -> str:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    opts = data["tool"]["pytest"]["ini_options"]
    lines = ["[tool.pytest.ini_options]"]
    for key, value in opts.items():
        if isinstance(value, list):
            items = ", ".join(f'"{v}"' for v in value)
            lines.append(f"{key} = [{items}]")
        elif isinstance(value, bool):
            lines.append(f"{key} = {str(value).lower()}")
        elif isinstance(value, int):
            lines.append(f"{key} = {value}")
        else:
            lines.append(f'{key} = "{value}"')
    return "\n".join(lines) + "\n"


@pytest.fixture
def suite(pytester: pytest.Pytester) -> pytest.Pytester:
    (pytester.path / "pyproject.toml").write_text(_ini_from_pyproject(), "utf-8")
    tests = pytester.mkdir("tests")
    (tests / "conftest.py").write_text(REAL_CONFTEST, encoding="utf-8")
    return pytester


def _write(suite: pytest.Pytester, rel: str, body: str) -> None:
    path = suite.path / "tests" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("import pytest\n\n" + body, encoding="utf-8")


def test_hanging_test_is_killed_by_the_configured_timeout(
    suite: pytest.Pytester,
) -> None:
    """A test that never returns must be force-failed, not hang the suite.

    Uses a per-test `@pytest.mark.timeout(2)` override (the suite-wide default is
    300s, far too slow for a test) to prove `pytest-timeout` is wired up through
    the real ini options, not merely installed.
    """
    _write(
        suite,
        "unit/test_hang.py",
        "import time\n\n@pytest.mark.timeout(2)\ndef test_a():\n    time.sleep(120)\n",
    )
    result = suite.runpytest_subprocess(timeout=30)
    if sys.platform == "win32":
        # No SIGALRM: pytest-timeout's thread method dumps the stacks and ends
        # the whole process (no summary line), which still fails the run.
        assert result.ret != 0
        assert "Timeout" in result.stdout.str() + result.stderr.str()
        return
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*Timeout*>2.0s*"])


def test_new_warning_fails_the_run(suite: pytest.Pytester) -> None:
    """`filterwarnings = ["error"]` must turn any warning (e.g. a stdlib
    DeprecationWarning raised by test code) into a test failure with a clear
    message, not a silently-passing warning line.
    """
    _write(
        suite,
        "unit/test_warns.py",
        "import warnings\n\n"
        "def test_a():\n"
        "    warnings.warn('this is deprecated', DeprecationWarning, stacklevel=1)\n",
    )
    result = suite.runpytest_subprocess()
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*DeprecationWarning: this is deprecated*"])


def test_xfail_strict_turns_unexpected_pass_into_failure(
    suite: pytest.Pytester,
) -> None:
    """`xfail_strict = true`: a test marked `xfail` that actually passes (XPASS)
    must fail the suite, not report as a harmless "xpassed".
    """
    _write(
        suite,
        "unit/test_xfail.py",
        "@pytest.mark.xfail(reason='should stay broken')\n"
        "def test_a():\n"
        "    assert True\n",
    )
    result = suite.runpytest_subprocess()
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*XPASS*strict*"])


def test_asyncio_strict_mode_does_not_silently_skip_unmarked_coroutine(
    suite: pytest.Pytester,
) -> None:
    """`asyncio_mode = "strict"`: an `async def` test without an asyncio marker
    must not silently "pass" without running its body; pytest-asyncio strict mode
    warns (and, with `filterwarnings=error`, that becomes a failure) instead of
    quietly returning a coroutine object.
    """
    _write(
        suite,
        "unit/test_async.py",
        "async def test_a():\n"
        "    raise AssertionError('this must never be reported as passing')\n",
    )
    result = suite.runpytest_subprocess()
    assert result.ret != 0
    outcomes = result.parseoutcomes()
    assert outcomes.get("passed", 0) == 0


def test_marked_asyncio_test_runs_for_real(suite: pytest.Pytester) -> None:
    """The converse: a properly marked `@pytest.mark.asyncio` test does run its
    coroutine body (so the strict-mode failure above is not itself a false
    positive from asyncio tests being globally broken).
    """
    _write(
        suite,
        "unit/test_async_ok.py",
        "import pytest_asyncio  # noqa: F401\n\n"
        "@pytest.mark.asyncio\n"
        "async def test_a():\n"
        "    assert True\n",
    )
    suite.runpytest_subprocess().assert_outcomes(passed=1)


def test_deep_nested_directory_still_resolves_tier_from_first_component(
    suite: pytest.Pytester,
) -> None:
    """Tier derivation is `Path.relative_to(tests_dir).parts[0]`, which is
    OS-path-separator agnostic (pathlib on the running OS builds `parts`
    correctly for both POSIX and Windows paths) -- a deeply nested test file
    still resolves to its top-level tier directory.
    """
    _write(suite, "unit/a/b/c/test_deep.py", "def test_a():\n    pass\n")
    result = suite.runpytest_subprocess("-m", "unit")
    result.assert_outcomes(passed=1)
