"""Roadmap 0.1.8: the suite-wide pytest policy in ``tests/conftest.py`` really holds.

Each case builds a throwaway tree (``tests/<tier>/test_x.py`` + a copy of the real
conftest + the real ``[tool.pytest.ini_options]``) and runs pytest in a subprocess,
so the policy is checked exactly as a developer or CI would hit it.
"""

from __future__ import annotations

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


def test_marker_is_applied_from_directory(suite: pytest.Pytester) -> None:
    _write(suite, "unit/test_a.py", "def test_a():\n    pass\n")
    _write(suite, "component/test_b.py", "def test_b():\n    pass\n")
    result = suite.runpytest_subprocess("-m", "unit")
    result.assert_outcomes(passed=1, deselected=1)
    result = suite.runpytest_subprocess("-m", "component")
    result.assert_outcomes(passed=1, deselected=1)


def test_matching_explicit_pytestmark_is_accepted(suite: pytest.Pytester) -> None:
    _write(
        suite,
        "unit/test_a.py",
        "pytestmark = pytest.mark.unit\n\ndef test_a():\n    pass\n",
    )
    suite.runpytest_subprocess().assert_outcomes(passed=1)


def test_mismatched_tier_marker_fails_collection(suite: pytest.Pytester) -> None:
    _write(
        suite,
        "component/test_a.py",
        "pytestmark = pytest.mark.unit\n\ndef test_a():\n    pass\n",
    )
    result = suite.runpytest_subprocess()
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    assert (
        "tier markers ['unit'] do not match directory tier 'component'"
        in result.stderr.str()
    )


def test_two_tier_markers_fail_collection(suite: pytest.Pytester) -> None:
    _write(
        suite,
        "unit/test_a.py",
        "pytestmark = pytest.mark.unit\n\n"
        "@pytest.mark.component\ndef test_a():\n    pass\n",
    )
    result = suite.runpytest_subprocess()
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    assert "tier markers ['component', 'unit'] do not match" in result.stderr.str()


def test_test_outside_tier_directory_fails_collection(suite: pytest.Pytester) -> None:
    _write(suite, "test_loose.py", "def test_a():\n    pass\n")
    _write(suite, "misc/test_other.py", "def test_b():\n    pass\n")
    result = suite.runpytest_subprocess()
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(
        ["*test_loose.py::test_a: not inside a tier directory*"]
    )
    result.stderr.fnmatch_lines(["*test_other.py::test_b: not inside a tier*"])


def test_unknown_marker_is_an_error(suite: pytest.Pytester) -> None:
    _write(suite, "unit/test_a.py", "@pytest.mark.unti\ndef test_a():\n    pass\n")
    result = suite.runpytest_subprocess()
    assert result.ret != pytest.ExitCode.OK
    result.stdout.fnmatch_lines(["*'unti' not found in `markers`*"])


@pytest.mark.parametrize("tier", ["lab", "soak", "chaos"])
def test_opt_in_tiers_skip_by_default(suite: pytest.Pytester, tier: str) -> None:
    _write(suite, f"{tier}/test_a.py", "def test_a():\n    pass\n")
    result = suite.runpytest_subprocess("-rs")
    result.assert_outcomes(skipped=1)
    result.stdout.fnmatch_lines([f"*pass --run-{tier} to run it*"])


@pytest.mark.parametrize("tier", ["lab", "soak", "chaos"])
def test_opt_in_flag_runs_the_tier(suite: pytest.Pytester, tier: str) -> None:
    _write(suite, f"{tier}/test_a.py", "def test_a():\n    pass\n")
    suite.runpytest_subprocess(f"--run-{tier}").assert_outcomes(passed=1)


def test_slow_marker_skips_by_default(suite: pytest.Pytester) -> None:
    _write(suite, "component/test_a.py", "@pytest.mark.slow\ndef test_a():\n    pass\n")
    result = suite.runpytest_subprocess("-rs")
    result.assert_outcomes(skipped=1)
    result.stdout.fnmatch_lines(["*slow test skipped*pass --run-slow to run it*"])


@pytest.mark.parametrize("tier", ["unit", "component", "cluster"])
def test_run_slow_runs_it_under_its_tier_filter(
    suite: pytest.Pytester, tier: str
) -> None:
    """`slow` is a capability: it combines with any tier and `-m <tier>`."""
    _write(
        suite,
        f"{tier}/test_a.py",
        "@pytest.mark.slow\ndef test_a():\n    pass\n\ndef test_b():\n    pass\n",
    )
    suite.runpytest_subprocess("--run-slow", "-m", tier).assert_outcomes(passed=2)
    suite.runpytest_subprocess("--run-slow", "-m", f"{tier} and slow").assert_outcomes(
        passed=1, deselected=1
    )


def test_slow_directory_is_not_a_tier(suite: pytest.Pytester) -> None:
    _write(suite, "slow/test_a.py", "def test_a():\n    pass\n")
    result = suite.runpytest_subprocess()
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(["*test_a.py::test_a: not inside a tier directory*"])


def _stub_package(
    suite: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, name: str, body: str
) -> None:
    """Put a fake top-level package ahead of site-packages for the subprocess."""
    stub = suite.path / "stub"
    (stub / name).mkdir(parents=True)
    (stub / name / "__init__.py").write_text(body, encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", str(stub))


def _torch_stub(cuda: bool) -> str:
    return (
        "class cuda:\n    @staticmethod\n    def is_available():\n"
        f"        return {cuda}\n"
    )


def test_gpu_directory_is_not_a_tier(suite: pytest.Pytester) -> None:
    """`gpu` is a capability marker, so tests/gpu/ is not a valid tier directory."""
    _write(suite, "gpu/test_a.py", "def test_a():\n    pass\n")
    result = suite.runpytest_subprocess()
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(["*test_a.py::test_a: not inside a tier directory*"])


@pytest.mark.parametrize("tier", ["component", "cluster"])
def test_gpu_marker_combines_with_a_tier(
    suite: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, tier: str
) -> None:
    """Mode 2/4 tests are `component`/`cluster` + `gpu`: accepted, and they run
    under their tier's `-m` filter when --run-gpu is given and CUDA is present."""
    _stub_package(suite, monkeypatch, "torch", _torch_stub(cuda=True))
    _write(suite, f"{tier}/test_a.py", "@pytest.mark.gpu\ndef test_a():\n    pass\n")
    suite.runpytest_subprocess("--run-gpu", "-m", tier).assert_outcomes(passed=1)
    suite.runpytest_subprocess("--run-gpu", "-m", f"{tier} and gpu").assert_outcomes(
        passed=1
    )


def test_gpu_marker_skips_by_default(
    suite: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Even with CUDA "present", gpu tests are opt-in.
    _stub_package(suite, monkeypatch, "torch", _torch_stub(cuda=True))
    _write(suite, "component/test_a.py", "@pytest.mark.gpu\ndef test_a():\n    pass\n")
    result = suite.runpytest_subprocess("-rs")
    result.assert_outcomes(skipped=1)
    result.stdout.fnmatch_lines(["*gpu test skipped*pass --run-gpu to run it*"])


def test_run_gpu_without_cuda_skips_with_reason(
    suite: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A stub `torch` that reports no CUDA keeps this deterministic on GPU laptops.
    _stub_package(suite, monkeypatch, "torch", _torch_stub(cuda=False))
    _write(suite, "component/test_a.py", "@pytest.mark.gpu\ndef test_a():\n    pass\n")
    result = suite.runpytest_subprocess("--run-gpu", "-rs")
    result.assert_outcomes(skipped=1)
    result.stdout.fnmatch_lines(["*torch reports no CUDA device*"])


def test_run_gpu_without_torch_skips_with_reason(
    suite: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_package(suite, monkeypatch, "torch", "raise ImportError('stub: no torch')\n")
    _write(suite, "cluster/test_a.py", "@pytest.mark.gpu\ndef test_a():\n    pass\n")
    result = suite.runpytest_subprocess("--run-gpu", "-rs")
    result.assert_outcomes(skipped=1)
    result.stdout.fnmatch_lines(["*torch is not installed*"])


def test_docker_marker_skips_with_reason_when_daemon_unreachable(
    suite: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DOCKER_HOST", "tcp://127.0.0.1:1")
    _write(
        suite,
        "component/test_a.py",
        "@pytest.mark.docker\ndef test_a():\n    pass\n\n"
        "def test_b(docker_available):\n    pass\n\n"
        "def test_c():\n    pass\n",
    )
    result = suite.runpytest_subprocess("-rs")
    result.assert_outcomes(passed=1, skipped=2)
    result.stdout.fnmatch_lines(
        ["*Docker is not available: no Docker daemon answered*"]
    )
    result.stdout.no_fnmatch_line("*package is not installed*")


def test_docker_marker_skips_with_distinct_reason_when_package_missing(
    suite: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_package(
        suite, monkeypatch, "docker", "raise ImportError('stub: no docker')\n"
    )
    _write(
        suite,
        "component/test_a.py",
        "@pytest.mark.docker\ndef test_a():\n    pass\n\n"
        "def test_b(docker_available):\n    pass\n",
    )
    result = suite.runpytest_subprocess("-rs")
    result.assert_outcomes(skipped=2)
    result.stdout.fnmatch_lines(["*`docker` Python package is not installed*"])
    result.stdout.no_fnmatch_line("*no Docker daemon answered*")
