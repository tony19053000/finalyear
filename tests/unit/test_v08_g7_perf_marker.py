"""V0.8 G7: the ``perf`` capability marker (UI performance thresholds).

Skipped by a bare run (it seeds 10 000 jobs), run by ``pytest -m perf`` or
``--run-perf`` - checked on a throwaway tree with the real conftest and ini
options, in a subprocess, like ``test_pytest_tier_policy.py``.
"""

from __future__ import annotations

import pytest
from test_pytest_tier_policy import REAL_CONFTEST, _ini_from_pyproject

pytest_plugins = ["pytester"]
pytestmark = pytest.mark.unit

BODY = (
    "import pytest\n\n"
    "@pytest.mark.perf\n"
    "def test_measured():\n    pass\n\n"
    "def test_plain():\n    pass\n"
)


@pytest.fixture
def suite(pytester: pytest.Pytester) -> pytest.Pytester:
    (pytester.path / "pyproject.toml").write_text(_ini_from_pyproject(), "utf-8")
    tests = pytester.mkdir("tests")
    (tests / "conftest.py").write_text(REAL_CONFTEST, encoding="utf-8")
    (tests / "unit").mkdir()
    (tests / "unit" / "test_p.py").write_text(BODY, encoding="utf-8")
    return pytester


def test_a_bare_run_skips_perf_with_the_way_to_run_it(suite: pytest.Pytester) -> None:
    result = suite.runpytest_subprocess("-rs")
    result.assert_outcomes(passed=1, skipped=1)
    result.stdout.fnmatch_lines(["*perf test skipped*pass --run-perf*"])


@pytest.mark.parametrize(
    "args",
    [("-m", "perf"), ("-m", "perf and unit"), ("--run-perf",)],
    ids=["-m perf", "-m perf and unit", "--run-perf"],
)
def test_selecting_perf_runs_it(suite: pytest.Pytester, args: tuple[str, ...]) -> None:
    result = suite.runpytest_subprocess(*args)
    outcomes = result.parseoutcomes()
    assert outcomes.get("passed", 0) >= 1 and "skipped" not in outcomes, outcomes


def test_not_perf_deselects_and_never_enables_it(suite: pytest.Pytester) -> None:
    result = suite.runpytest_subprocess("-m", "not perf")
    result.assert_outcomes(passed=1, deselected=1)
    result = suite.runpytest_subprocess("-m", "unit and not perf")
    result.assert_outcomes(passed=1, deselected=1)
