"""D202 follow-up: every ``meshforge.cli`` module imports cleanly when it is the
**first** ``meshforge.cli`` module a fresh interpreter imports.

Before the fix, ``import meshforge.cli.job_commands`` (and every other
``*_commands`` module) failed with a circular import: the command module pulled
in :mod:`meshforge.cli.app`, which at its end imports the command module back
while it is still half-initialised. The shared helpers now live in the leaf
module :mod:`meshforge.cli.shared`.

Each module is imported in its own subprocess, because inside this pytest process
:mod:`meshforge.cli.app` is already in ``sys.modules`` and would hide the cycle.
The module list is discovered from the package at test time, so a new command
module is covered without editing this file.
"""

from __future__ import annotations

import importlib.util
import pkgutil
import subprocess
import sys

import pytest

pytestmark = pytest.mark.unit


def _cli_modules() -> list[str]:
    spec = importlib.util.find_spec("meshforge.cli")
    assert spec is not None and spec.submodule_search_locations is not None
    names = sorted(
        f"meshforge.cli.{info.name}"
        for info in pkgutil.iter_modules(spec.submodule_search_locations)
    )
    return ["meshforge.cli", *names]


MODULES = _cli_modules()


def test_the_module_list_is_not_vacuous() -> None:
    # The modules that used to break must be in the discovered list.
    for name in ("job_commands", "auth_commands", "node_commands", "shared", "app"):
        assert f"meshforge.cli.{name}" in MODULES


@pytest.mark.parametrize("module", MODULES)
def test_module_imports_first_in_a_fresh_interpreter(module: str) -> None:
    code = (
        "import sys\n"
        f"import {module}\n"
        # The full command tree must still be reachable afterwards, i.e. the
        # first import did not leave a half-initialised module behind.
        "from meshforge.cli.app import app\n"
        "import typer.main\n"
        "names = set(typer.main.get_command(app).commands)\n"
        "missing = {'run', 'jobs', 'logs', 'cancel', 'get', 'nodes', 'status',\n"
        "           'login', 'secret', 'data', 'node', 'fabric'} - names\n"
        "assert not missing, missing\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, f"`import {module}` first failed:\n{result.stderr}"
