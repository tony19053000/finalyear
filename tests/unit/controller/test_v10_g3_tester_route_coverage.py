"""V1.0 G3 tester: is the route inventory really *every* route of the product?

Independent of the implementer's walk: count route decorators in the controller
source with ``ast`` and require the inventory's app to expose at least as many
(method, path-suffix) endpoints; fail if a module defines routes that no mounted
app exposes (a router that the inventory test never mounts is never tested).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="needs the controller extra")

from test_v10_g3_route_inventory import _app, _http_cases, _ws_cases  # type: ignore[import-not-found]

pytestmark = [pytest.mark.unit, pytest.mark.security]

SRC = Path(__file__).resolve().parents[3] / "src" / "meshforge" / "controller"
VERBS = {"get", "post", "put", "patch", "delete", "head", "options", "websocket", "api_route"}


def decorated_routes() -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    for py in sorted(SRC.rglob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for dec in node.decorator_list:
                if (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)
                        and dec.func.attr in VERBS and dec.args
                        and isinstance(dec.args[0], ast.Constant) and isinstance(dec.args[0].value, str)
                        and dec.args[0].value.startswith("/")):
                    out.append((dec.func.attr.upper(), dec.args[0].value, py.relative_to(SRC).as_posix()))
    return out


@pytest.fixture(scope="module")
def inventory(tmp_path_factory: pytest.TempPathFactory) -> set[tuple[str, str]]:
    app = _app(tmp_path_factory.mktemp("tester-routes"))
    got = {(m, t) for m, t, _ in _http_cases(app)}
    got |= {("WEBSOCKET", t) for t, _ in _ws_cases(app)}
    return got


def test_source_finds_a_plausible_number_of_routes() -> None:
    assert len(decorated_routes()) >= 70


def test_every_decorated_route_is_in_the_inventory(inventory: set[tuple[str, str]]) -> None:
    missing = []
    for verb, path, where in decorated_routes():
        if where.startswith("setup/"):
            kind_paths = [p for _, p in inventory]
        else:
            kind_paths = [p for _, p in inventory]
        if not any(p == path or p.endswith(path) for p in kind_paths):
            missing.append((verb, path, where))
    assert not missing, f"routes in source that the inventory app never exposes: {missing}"


def test_inventory_methods_match_source_verbs(inventory: set[tuple[str, str]]) -> None:
    missing = []
    for verb, path, where in decorated_routes():
        if verb == "API_ROUTE":
            continue
        if not any(m in (verb, "WEBSOCKET") and (p == path or p.endswith(path)) for m, p in inventory):
            missing.append((verb, path, where))
    assert not missing, missing


def test_agent_console_is_loopback_only_by_construction() -> None:
    """The agent console app has its own routes; it must never bind non-loopback."""
    console = Path(__file__).resolve().parents[3] / "src" / "meshforge" / "agent" / "console"
    text = "\n".join(p.read_text(encoding="utf-8") for p in console.rglob("*.py"))
    assert "0.0.0.0" not in text and '"::"' not in text
