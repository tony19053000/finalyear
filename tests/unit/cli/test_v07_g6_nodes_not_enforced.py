"""V0.7 G6 item 0 (D161 tester follow-up): ``compute nodes`` shows the owner
caps a node cannot enforce.

* ``GET /v1/nodes`` rows carry ``not_enforced`` from the live session's owner
  policy (null when offline / pre-G5);
* the list's ``NOT ENFORCED`` column shows short field names (``vram, cpu``),
  ``none`` when every cap is enforced, ``-`` when unknown; agent strings pass
  the ``_cell`` gate;
* ``compute nodes NODE_ID`` lists every ``owner_policy`` field, with the full
  ``<field>:<why>`` items.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import io
from types import SimpleNamespace
from typing import Any

import pytest
from rich.console import Console

from meshforge.cli.render import (
    NODE_COLUMNS,
    node_detail_table,
    nodes_table,
    not_enforced_cell,
)
from meshforge.common.owner_policy import OPEN_POLICY

pytestmark = [pytest.mark.unit, pytest.mark.security]

VRAM = "max_vram_bytes:no_runtime_limit"
CPU = "max_cpu_percent:unsandboxed_linux"


def _render(renderable: Any) -> str:
    console = Console(file=io.StringIO(), width=400, color_system=None, no_color=True)
    console.print(renderable)
    buffer = console.file
    assert isinstance(buffer, io.StringIO)
    return buffer.getvalue()


def test_the_cell() -> None:
    assert str(not_enforced_cell([VRAM, CPU])) == "vram, cpu"
    assert str(not_enforced_cell([VRAM, "max_ram_bytes:unsandboxed_gpu_jobs"])) == (
        "vram, ram"
    )
    assert str(not_enforced_cell([])) == "none"
    assert str(not_enforced_cell(None)) == "-"
    assert str(not_enforced_cell("max_vram_bytes")) == "-"  # not a list
    assert str(not_enforced_cell(["\x1b[31mevil:x", 7])) == "[31mevil"  # gated


def test_the_list_has_the_column() -> None:
    assert "NOT ENFORCED" in NODE_COLUMNS and NODE_COLUMNS[-1] == "SCORES"
    out = _render(
        nodes_table(
            {
                "nodes": [
                    {"node_id": "node_a", "hostname": "a", "not_enforced": [VRAM, CPU]},
                    {"node_id": "node_b", "hostname": "b", "not_enforced": None},
                ],
                "count": 2,
            }
        )
    )
    assert "NOT ENFORCED" in out and "vram, cpu" in out


def test_the_detail_lists_the_policy_with_reasons() -> None:
    out = _render(
        node_detail_table(
            {
                "node_id": "node_a",
                "owner_policy": {
                    "accept_jobs": True,
                    "max_cpu_percent": 50,
                    "not_enforced": [VRAM, CPU],
                    "allowed_users_count": 2,
                },
            }
        )
    )
    assert "policy.not_enforced" in out
    assert f"{VRAM}, {CPU}" in out
    assert "policy.max_cpu_percent" in out and "policy.allowed_users_count" in out
    empty = _render(
        node_detail_table({"node_id": "x", "owner_policy": {"not_enforced": []}})
    )
    assert "policy.not_enforced" in empty and "none" in empty


def test_the_api_row_carries_the_live_list() -> None:
    pytest.importorskip("sqlalchemy", reason="needs the controller extra")
    from meshforge.controller.api.shape import node_summary
    from meshforge.controller.liveness import NodeState

    node: Any = SimpleNamespace(
        node_id="node_a",
        hostname="a",
        display_name="",
        os="Linux",
        platform="x86_64",
        state=NodeState.ONLINE,
        last_seen_at=dt.datetime(2026, 10, 12, tzinfo=dt.UTC),
        hardware={},
        hardware_scores=None,
        cert_serial=None,
        cert_fingerprint=None,
        cert_not_after=None,
        cert_public_key_fingerprint=None,
    )
    policy = dataclasses.replace(OPEN_POLICY, not_enforced=(VRAM, CPU))
    live: Any = SimpleNamespace(
        policy=policy,
        last_rtt_ms=None,
        rtt_ewma_ms=None,
        policy_refused=None,
        agent_version="",  # V0.17 G5: the Nodes page's version column
    )
    row = node_summary(node, live=live, sample=None, now=0.0)
    assert row.not_enforced == (VRAM, CPU)
    assert node_summary(node, live=None, sample=None, now=0.0).not_enforced is None
    no_policy: Any = SimpleNamespace(
        policy=None, last_rtt_ms=None, rtt_ewma_ms=None, agent_version=""
    )
    assert node_summary(node, live=no_policy, sample=None, now=0.0).not_enforced is None
