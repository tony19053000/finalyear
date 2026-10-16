"""G7-B (0.3.4, D38) item 5: hardware scores in ``compute nodes``.

The compact ``SCORES`` column (measured / pending / none), the full table of
``compute nodes <id>``, ``--json`` passing ``scores`` through untouched, and the
``_cell()`` gate on every agent-supplied string (backend names, reasons, details,
metric names, run id). Canned payloads only: nothing depends on this machine.
"""

from __future__ import annotations

import copy
import io
import json
from typing import Any

import pytest
from mf_cli_support import TransportFactory
from rich.console import Console
from typer.testing import CliRunner

from meshforge.cli.app import app
from meshforge.cli.render import (
    NODE_COLUMNS,
    SCORE_COLUMNS,
    nodes_table,
    scores_cell,
    scores_table,
)
from meshforge.common.exit_codes import ExitCode

pytestmark = pytest.mark.unit

NODE_ID = "node_01JQZ00000000000000000000A"

MEASURED: dict[str, Any] = {
    "status": "measured",
    "run_id": "score_01JQZ00000000000000000000A",
    "measured_at": "2026-09-24T12:00:00.123456+00:00",
    "score_version": 1,
    "conditions": {
        "on_battery": False,
        "cpu_load_percent_before": 3.5,
        "background_load_high": False,
        "priority_lowered": True,
        "budget_s": 90.0,
        "duration_s": 31.25,
        "budget_exhausted": False,
    },
    "metrics": [
        {
            "name": "cpu_fp32_gflops_1t",
            "gpu_index": None,
            "status": "ok",
            "unit": "GFLOP/s",
            "mean": 97.3,
            "stdev": 0.97,
            "n": 5,
            "backend": "numpy",
            "backend_version": "2.5.3",
            "threads_effective": 1,
            "lower_bound": False,
            "throttling_suspected": False,
            "reason": None,
            "detail": "",
        },
        {
            "name": "cpu_fp32_gflops_all",
            "gpu_index": None,
            "status": "ok",
            "unit": "GFLOP/s",
            "mean": 312.4,
            "stdev": 4.1,
            "n": 7,
            "backend": "numpy",
            "backend_version": "2.5.3",
            "threads_effective": 8,
            "lower_bound": False,
            "throttling_suspected": True,
            "reason": None,
            "detail": "",
        },
        {
            "name": "mem_triad_gb_s",
            "gpu_index": None,
            "status": "ok",
            "unit": "GB/s",
            "mean": 19.29,
            "stdev": 0.2,
            "n": 5,
            "backend": "numpy",
            "backend_version": "2.5.3",
            "threads_effective": 1,
            "lower_bound": True,
            "throttling_suspected": None,
            "reason": None,
            "detail": "",
        },
        {
            "name": "gpu_fp32_tflops",
            "gpu_index": 0,
            "status": "ok",
            "unit": "TFLOP/s",
            "mean": 8.123,
            "stdev": 0.05,
            "n": 5,
            "backend": "torch",
            "backend_version": "2.9.0",
            "threads_effective": None,
            "lower_bound": False,
            "throttling_suspected": False,
            "reason": None,
            "detail": "",
        },
        {
            "name": "gpu_fp32_tflops",
            "gpu_index": 1,
            "status": "failed",
            "unit": "TFLOP/s",
            "mean": None,
            "stdev": None,
            "n": 0,
            "backend": "torch",
            "backend_version": "2.9.0",
            "threads_effective": None,
            "lower_bound": False,
            "throttling_suspected": None,
            "reason": "device_mismatch",
            "detail": "torch reports another device",
        },
    ],
}


def _node(scores: dict[str, Any] | None) -> dict[str, Any]:
    node: dict[str, Any] = {"node_id": NODE_ID, "hostname": "h", "state": "online"}
    if scores is not None:
        node["scores"] = scores
    return node


def _render(renderable: Any) -> str:
    console = Console(
        file=io.StringIO(),
        width=400,
        height=80,
        force_terminal=False,
        color_system=None,
        no_color=True,
        markup=True,
        highlight=True,
        legacy_windows=False,
    )
    console.print(renderable)
    buffer = console.file
    assert isinstance(buffer, io.StringIO)
    return buffer.getvalue()


# ----------------------------------------------------------- compact column


def test_the_compact_cell_for_measured_pending_and_none() -> None:
    assert str(scores_cell(_node(MEASURED))) == "CPU 312 GF, GPU0 8.1 TF, GPU1 n/a TF"
    assert str(scores_cell(_node({"status": "pending"}))) == "pending"
    assert str(scores_cell(_node({"status": "none"}))) == "-"
    assert str(scores_cell(_node(None))) == "-"  # an older controller
    assert str(scores_cell(_node({"status": "measured", "metrics": "x"}))) == (
        "measured"
    )


def test_a_lower_bound_prints_as_such_and_not_ok_is_never_a_number() -> None:
    scores = copy.deepcopy(MEASURED)
    scores["metrics"] = [dict(scores["metrics"][1], lower_bound=True)]
    assert str(scores_cell(_node(scores))) == "CPU >=312 GF"
    scores["metrics"] = [
        dict(scores["metrics"][0], status="unavailable", mean=0.0, reason="x")
    ]
    assert str(scores_cell(_node(scores))) == "CPU n/a GF"


def test_the_nodes_table_has_a_scores_column() -> None:
    assert NODE_COLUMNS[-1] == "SCORES"
    out = _render(nodes_table({"nodes": [_node(MEASURED)], "count": 1}))
    assert "SCORES" in out and "CPU 312 GF, GPU0 8.1 TF" in out


# -------------------------------------------------------------- full table


def test_the_full_table_lists_every_metric_with_its_conditions() -> None:
    out = _render(scores_table({"scores": MEASURED}))
    for column in SCORE_COLUMNS:
        assert column in out
    assert "hardware scores (measured)" in out
    assert "cpu_fp32_gflops_all" in out and "312" in out and "4.1" in out
    assert ">=19.3" in out and "lower bound" in out
    assert "throttling?" in out
    assert "device_mismatch: torch reports another device" in out
    assert "numpy 2.5.3" in out
    assert "on battery: no" in out and "priority lowered: yes" in out
    assert "took 31.2 s of 90 s budget" in out
    assert "score_01JQZ00000000000000000000A" in out
    out.encode("cp437")  # ASCII only, like every other table


@pytest.mark.parametrize(
    ("status", "caption"),
    [
        ("pending", "not measured yet"),
        ("none", "does not measure"),
        ("weird", "sent no scores"),
    ],
)
def test_the_full_table_without_scores_says_why(status: str, caption: str) -> None:
    out = _render(scores_table({"scores": {"status": status}}))
    assert caption in out
    assert "n/a" not in out and "GFLOP" not in out


HOSTILE = "np\x1b]0;PWNED\x07\x1b[8m[bold red]x[/]\r\nlie"


@pytest.mark.parametrize(
    "field", ["backend", "backend_version", "reason", "detail", "name", "unit"]
)
def test_a_hostile_agent_string_never_reaches_the_terminal(field: str) -> None:
    scores = copy.deepcopy(MEASURED)
    metric = scores["metrics"][4]
    metric[field] = HOSTILE
    scores["run_id"] = HOSTILE
    out = _render(scores_table({"scores": scores}))
    for char in ("\x1b", "\x07", "\r"):
        assert char not in out, field
    assert "[bold red]x[/]" in out  # printed literally, never as markup
    cell = _render(nodes_table({"nodes": [_node(scores)], "count": 1}))
    assert "\x1b" not in cell


# ------------------------------------------------------------------ the CLI


def _handler(payload: dict[str, Any]) -> Any:
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    return handler


def test_compute_nodes_id_prints_the_score_table(
    runner: CliRunner, controller: TransportFactory
) -> None:
    controller(_handler({**_node(MEASURED), "hardware": {}}))
    result = runner.invoke(app, ["nodes", NODE_ID])
    assert result.exit_code == ExitCode.OK, result.output
    assert "hardware scores (measured)" in result.stdout
    assert "gpu_fp32_tflops" in result.stdout


def test_json_passes_scores_through_verbatim(
    runner: CliRunner, controller: TransportFactory
) -> None:
    payload = {"nodes": [_node(MEASURED)], "count": 1}
    controller(_handler(payload))
    result = runner.invoke(app, ["nodes", "--json"])
    assert result.exit_code == ExitCode.OK
    assert json.loads(result.stdout) == {**payload, "schema_version": 1}  # V0.8 G6
    controller(_handler(_node(MEASURED)))
    detail = runner.invoke(app, ["nodes", NODE_ID, "--json"])
    assert json.loads(detail.stdout)["scores"] == MEASURED
