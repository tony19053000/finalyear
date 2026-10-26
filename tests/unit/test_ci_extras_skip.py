"""D399: a test module whose import needs a missing optional extra is *skipped*.

The hook lives in ``tests/conftest.py`` (``_ExtrasAwareModule``). The
behaviour cases build a throwaway tree with a copy of the real conftest and run
pytest in a subprocess, like ``test_pytest_tier_policy.py``; a missing extra is
simulated with ``sys.modules[name] = None`` (the import then raises
``ModuleNotFoundError`` with ``name`` set, exactly like an absent package).
"""

from __future__ import annotations

import importlib.util
import re
import tomllib
from pathlib import Path
from types import ModuleType

import pytest
from packaging.requirements import Requirement

pytest_plugins = ["pytester"]
pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFTEST_PATH = REPO_ROOT / "tests" / "conftest.py"
REAL_CONFTEST = CONFTEST_PATH.read_text(encoding="utf-8")

#: Distribution name -> top-level import name, where they differ.
DIST_IMPORT_NAMES = {
    "nvidia-ml-py": "pynvml",
    "scikit-learn": "sklearn",
    "xgboost-cpu": "xgboost",
    "pywin32": "win32service",
}

#: Extras requirements that a plain ``uv sync`` installs anyway (base or dev
#: dependency), so a missing one can never be "an extra is not installed".
ALSO_IN_BASE = {"h11"}  # via httpx (base dependency)


def _load_conftest() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_d399_conftest", CONFTEST_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CONFTEST = _load_conftest()


def _extras_import_names() -> set[str]:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    names: set[str] = set()
    for reqs in data["project"]["optional-dependencies"].values():
        for req in reqs:
            match = re.match(r"[A-Za-z0-9_.-]+", req)
            assert match, req
            marker = Requirement(req).marker
            if marker is not None and not marker.evaluate():
                continue  # e.g. pywin32 off Windows (V0.17 G4): never installed here
            dist = match.group(0).lower()
            names.add(DIST_IMPORT_NAMES.get(dist, dist.replace("-", "_")))
    return names - ALSO_IN_BASE


def test_every_extras_requirement_is_known_to_the_hook() -> None:
    missing = _extras_import_names() - set(CONFTEST.EXTRA_IMPORTS)
    assert missing == set(), (
        f"add {sorted(missing)} to EXTRA_IMPORTS in tests/conftest.py"
    )


def test_missing_extra_recognises_only_top_level_extras_packages() -> None:
    hit = ModuleNotFoundError("No module named 'sqlalchemy'", name="sqlalchemy")
    assert CONFTEST.missing_extra(hit) == ("sqlalchemy", "controller")
    # A missing submodule of an installed extra is a real bug.
    sub = ModuleNotFoundError("x", name="sqlalchemy.nope")
    assert CONFTEST.missing_extra(sub) is None
    # Unknown or first-party modules, other errors, nothing at all: not a skip.
    assert CONFTEST.missing_extra(ModuleNotFoundError("x", name="nope")) is None
    assert CONFTEST.missing_extra(ModuleNotFoundError("x", name="meshforge")) is None
    assert CONFTEST.missing_extra(ModuleNotFoundError("x")) is None
    assert CONFTEST.missing_extra(ImportError("x", name="torch")) is None
    assert CONFTEST.missing_extra(None) is None


def test_first_party_orphans_are_submodules_without_a_loaded_parent() -> None:
    modules: dict[str, object] = {
        "meshforge": object(),
        "meshforge.common": object(),
        "meshforge.agent.x": object(),
        "meshforge.agent.x.y": object(),
        "requests.adapters": object(),
    }
    new = {"meshforge.common", "meshforge.agent.x", "meshforge.agent.x.y"}
    new |= {"requests.adapters", "meshforge.gone"}
    assert CONFTEST.first_party_orphans(new, modules) == [
        "meshforge.agent.x",
        "meshforge.agent.x.y",
    ]


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
    path.write_text(body, encoding="utf-8")


def test_module_needing_a_missing_extra_is_skipped_with_the_reason(
    suite: pytest.Pytester,
) -> None:
    _write(
        suite,
        "unit/test_needs_torch.py",
        "import sys\nsys.modules['torch'] = None\nimport torch\n\n"
        "def test_a():\n    pass\n",
    )
    _write(suite, "unit/test_plain.py", "def test_b():\n    pass\n")
    result = suite.runpytest_subprocess("-m", "unit", "-rs")
    result.assert_outcomes(passed=1, skipped=1)
    result.stdout.fnmatch_lines(["*needs extra gpu (`torch` is not installed*"])


def test_missing_non_extra_module_is_still_a_collection_error(
    suite: pytest.Pytester,
) -> None:
    _write(
        suite,
        "unit/test_broken.py",
        "import definitely_not_a_module_d399\n\ndef test_a():\n    pass\n",
    )
    result = suite.runpytest_subprocess("-m", "unit")
    assert result.ret == pytest.ExitCode.INTERRUPTED
    result.assert_outcomes(errors=1)


def test_missing_submodule_of_an_extras_name_is_still_an_error(
    suite: pytest.Pytester,
) -> None:
    # A stub `numpy` stands in for an installed extra; its missing submodule
    # must not be mistaken for "extra not installed".
    _write(
        suite,
        "unit/test_sub.py",
        "import sys, types\nsys.modules['numpy'] = types.ModuleType('numpy')\n"
        "import numpy.definitely_missing\n\ndef test_a():\n    pass\n",
    )
    result = suite.runpytest_subprocess("-m", "unit")
    result.assert_outcomes(errors=1)
