"""V0.5 G5 fix round 1 (N3): ``compute node gc --all`` and ``node prepare
--all`` with no connected node say so and exit ``UNAVAILABLE`` - the same
contract for both; with connected nodes only those are asked."""

from __future__ import annotations

import httpx
import pytest
from mf_cli_support import TransportFactory
from typer.testing import CliRunner

from meshforge.cli.app import app
from meshforge.common.exit_codes import ExitCode

pytestmark = pytest.mark.unit

A = "node_01JQZ00000000000000000000A"
B = "node_01JQZ00000000000000000000B"


def _nodes(*connected: tuple[str, bool]) -> dict[str, object]:
    return {"nodes": [{"node_id": n, "connected": c} for n, c in connected]}


@pytest.mark.parametrize(
    ("command", "what"),
    [(["node", "gc", "--all"], "run GC on"), (["node", "prepare", "--all"], "prepare")],
)
def test_all_with_no_connected_node_is_unavailable(
    runner: CliRunner,
    controller: TransportFactory,
    command: list[str],
    what: str,
) -> None:
    asked: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        asked.append(f"{request.method} {request.url.path}")
        if request.url.path == "/v1/nodes":
            return httpx.Response(200, json=_nodes((A, False)))
        return httpx.Response(500)

    controller(handle)
    result = runner.invoke(app, command)
    assert result.exit_code == ExitCode.UNAVAILABLE, result.output
    assert f"no connected node to {what}" in result.output
    assert asked == ["GET /v1/nodes"]  # nothing was sent to any node


def test_gc_all_asks_only_connected_nodes(
    runner: CliRunner, controller: TransportFactory
) -> None:
    asked: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        asked.append(f"{request.method} {request.url.path}")
        if request.url.path == "/v1/nodes":
            return httpx.Response(200, json=_nodes((A, True), (B, False)))
        return httpx.Response(202, json={"node_id": A, "requested": True})

    controller(handle)
    result = runner.invoke(app, ["node", "gc", "--all"])
    assert result.exit_code == 0, result.output
    assert f"{A}: GC pass requested" in result.output
    assert asked == ["GET /v1/nodes", f"POST /v1/nodes/{A}/gc"]
