"""V0.8 G7 (criterion 1, D176): every CLI command is in the dashboard, or is
explicitly CLI-only with a reason.

The command trees are enumerated from typer at test time (the real ``compute``
app and the ``meshforge-controller user`` group), and compared with the table
in ``tests/e2e/parity.py``. A new command without an entry fails; an entry for
a command that no longer exists fails; a UI entry must name a route the SPA
router has and e2e tests that exist.
"""

from __future__ import annotations

import ast
import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
E2E = ROOT / "tests" / "e2e"
APP_TSX = ROOT / "frontend" / "src" / "app" / "App.tsx"
SETTINGS_TSX = ROOT / "frontend" / "src" / "pages" / "settings" / "SettingsRoutes.tsx"


def _parity() -> ModuleType:
    spec = importlib.util.spec_from_file_location("mf_e2e_parity", E2E / "parity.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve their module
    spec.loader.exec_module(module)
    return module


PARITY = _parity()


def _leaves(command: Any, prefix: tuple[str, ...] = ()) -> list[str]:
    """Every runnable (leaf) command under a click/typer group, space-joined.
    Hidden commands count too (tester r1 T-3): ``hidden=True`` only keeps a
    command out of ``--help``, a user can still run it, so it still needs a
    dashboard mapping or a CLI-only reason."""
    sub = getattr(command, "commands", None)
    if sub is None:
        return [" ".join(prefix)]
    found: list[str] = []
    for name, child in sorted(sub.items()):
        found.extend(_leaves(child, (*prefix, name)))
    return found


def _compute_commands() -> list[str]:
    import typer

    from meshforge.cli.app import app

    return _leaves(typer.main.get_command(app))


def _spa_routes() -> set[str]:
    """Routes of the SPA router: App.tsx (index = "/") + the Settings routes."""
    app = APP_TSX.read_text(encoding="utf-8")
    settings = SETTINGS_TSX.read_text(encoding="utf-8")
    routes = set(re.findall(r'<Route path="([^"*]+)"', app))
    routes = {r if r.startswith("/") else "/" + r for r in routes}
    if re.search(r"<Route index ", app):
        routes.add("/")
    assert 'path="settings/*"' in app
    for sub in re.findall(r'<Route path="([^"*]+)"', settings):
        routes.add("/settings/" + sub)
    if re.search(r"<Route index ", settings):
        routes.add("/settings")
    return routes


def _e2e_test_exists(test_id: str) -> bool:
    """A top-level ``test_`` function of a file directly in ``tests/e2e``. A
    path (``../unit/x.py``, ``sub/x.py``) is rejected (tester r1 T-4): a unit
    test is no proof that the dashboard does the command."""
    file_name, _, function = test_id.partition("::")
    if not re.fullmatch(r"test_[\w]+\.py", file_name):
        return False
    path = E2E / file_name
    if path.resolve().parent != E2E.resolve():
        return False
    if not path.is_file() or not function.startswith("test_"):
        return False
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return any(
        isinstance(node, ast.FunctionDef) and node.name == function
        for node in tree.body
    )


def _check(table: dict[str, Any], commands: list[str], label: str) -> None:
    missing = sorted(set(commands) - set(table))
    assert not missing, (
        f"{label} commands with no dashboard mapping and no CLI-only reason: "
        f"{missing} - add each to tests/e2e/parity.py"
    )
    stale = sorted(set(table) - set(commands))
    assert not stale, (
        f"parity entries for {label} commands that no longer exist: {stale}"
    )


def test_every_compute_command_is_mapped() -> None:
    commands = _compute_commands()
    assert len(commands) >= 25, commands  # the tree really was enumerated
    _check(PARITY.COMPUTE, commands, "compute")


def test_every_controller_user_command_is_mapped() -> None:
    pytest.importorskip("sqlalchemy", reason="needs the controller extra")
    import typer

    from meshforge.controller.app import user_app

    commands = _leaves(typer.main.get_group(user_app))
    assert set(commands) >= {"create", "list"}, commands
    _check(PARITY.CONTROLLER_USER, commands, "meshforge-controller user")


@pytest.mark.parametrize(
    "table", [PARITY.COMPUTE, PARITY.CONTROLLER_USER], ids=["compute", "user"]
)
def test_every_entry_is_a_real_route_with_real_e2e_tests_or_a_reason(
    table: dict[str, Any],
) -> None:
    routes = _spa_routes()
    for command, entry in table.items():
        if isinstance(entry, PARITY.CliOnly):
            assert len(entry.reason.split()) >= 8, (command, "say why, in words")
            continue
        assert isinstance(entry, PARITY.InUi), (command, entry)
        assert entry.route in routes, (command, entry.route, sorted(routes))
        assert entry.where.strip(), command
        assert entry.e2e, (command, "name the e2e test that proves it")
        for test_id in entry.e2e:
            assert _e2e_test_exists(test_id), (command, test_id)


def test_cli_only_is_the_exception() -> None:
    """Criterion 1 is "every Mode 1 operation from the UI and the CLI": the
    CLI-only list stays the short, reasoned one the Lead knows about."""
    cli_only = sorted(
        c for c, e in PARITY.COMPUTE.items() if isinstance(e, PARITY.CliOnly)
    )
    # V0.9 G6: the Benchmarks page (D221) took campaigns, runs, labels, the
    # runner controls, status and the records download. Left CLI-only:
    # local-file previews/creation, the manual driver, the local library.
    assert cli_only == [
        "bench create",  # a local spec file; no browser spec editor in D221
        "bench plan",  # previews a local spec file
        "bench submit",  # the manual driver; the browser starts the runner
        "bench workloads",  # this laptop's library, no controller call
        "data push",
        "fabric join",
        "node gc",
        "node prepare",
        # V0.14 G2: until the Planner page (V0.14 G5, D300) exists
        "planner calibrate",
        "planner calibration",
        "planner eval",  # V0.14 G4: until the Planner page (G5) lists reports
    ]


def test_the_check_catches_a_new_command() -> None:
    """Mutation guard for the table check itself."""
    with pytest.raises(AssertionError, match="no dashboard mapping"):
        _check(PARITY.COMPUTE, [*PARITY.COMPUTE, "brand-new"], "compute")
    with pytest.raises(AssertionError, match="no longer exist"):
        _check(PARITY.COMPUTE, list(PARITY.COMPUTE)[1:], "compute")
    assert not _e2e_test_exists(f"{PARITY.G7}::test_does_not_exist")
    assert "/jobs/:jobId" in _spa_routes()
