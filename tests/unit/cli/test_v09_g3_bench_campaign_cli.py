"""V0.9 G3 lane B item 5: ``compute bench plan|create|campaigns|runs|submit``."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from mf_cli_support import TransportFactory
from typer.testing import CliRunner

from meshforge.bench.campaign import NodeRef, expand, load_spec, runs_json
from meshforge.cli import app as cli_app
from meshforge.cli import bench_campaign_commands
from meshforge.cli.exits import CliExit

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
EXAMPLE = REPO / "examples" / "bench" / "cpu-baseline.yaml"
A, B = "node_01J0AAAAAAAAAAAAAAAAAAAAAA", "node_01J0BBBBBBBBBBBBBBBBBBBBBB"
CAMPAIGN = "camp_01J0000000000000000000DDDD"


def _plan(runner: CliRunner, *extra: str) -> Any:
    return runner.invoke(
        cli_app.app,
        ["bench", "plan", str(EXAMPLE), "--node", A, "--node", B, *extra],
    )


def test_example_spec_is_valid_and_plans_offline(runner: CliRunner) -> None:
    result = _plan(runner, "--json", "--show", "1000")
    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout)
    spec = load_spec(EXAMPLE.read_text())
    expansion = expand(spec, [NodeRef(A), NodeRef(B)])
    assert document["runs"] == len(expansion.runs) == len(document["first"])
    assert json.dumps(document["first"], sort_keys=True, separators=(",", ":")) == (
        runs_json(expansion.runs)
    )
    assert document["nodes"] == [A, B]
    assert document["schema_version"] == 1
    assert document["dropped"] == len(document["dropped_detail"])


def test_plan_is_deterministic_and_prints_the_cost(runner: CliRunner) -> None:
    first, second = _plan(runner, "--json"), _plan(runner, "--json")
    assert first.stdout == second.stdout
    text = _plan(runner, "--show", "3")
    assert text.exit_code == 0, text.output
    assert "runs =" in text.stdout and "h on the reference mid laptop" in text.stdout


def test_plan_spec_errors_are_usage_errors(runner: CliRunner, tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "name: x\nseed: 1\nfamilies: [{family: cpu.nope}]\nbudget: {max_runs: 9}\n"
    )
    result = runner.invoke(cli_app.app, ["bench", "plan", str(bad), "--node", A])
    assert result.exit_code == CliExit.USAGE_ERROR
    assert "unknown workload family" in result.stderr
    over = tmp_path / "over.yaml"
    over.write_text(EXAMPLE.read_text().replace("max_runs: 500", "max_runs: 5"))
    result = runner.invoke(cli_app.app, ["bench", "plan", str(over), "--node", A])
    assert result.exit_code == CliExit.USAGE_ERROR
    assert "more than budget.max_runs 5" in result.stderr
    missing = runner.invoke(cli_app.app, ["bench", "plan", str(tmp_path / "no.yaml")])
    assert missing.exit_code == CliExit.USAGE_ERROR
    bad_node = runner.invoke(
        cli_app.app, ["bench", "plan", str(EXAMPLE), "--node", "x"]
    )
    assert bad_node.exit_code == CliExit.USAGE_ERROR


@pytest.mark.parametrize("value", [".nan", "[.inf]"])
def test_fix1_d2_plan_with_a_non_finite_param_is_a_usage_error(
    runner: CliRunner, tmp_path: Path, value: str
) -> None:
    bad = tmp_path / "nan.yaml"
    bad.write_text(
        "name: x\nseed: 1\nbudget: {max_runs: 9}\non_invalid: drop\n"
        f"families: [{{family: cpu.sort, params: {{n: {value}}}}}]\n"
    )
    result = runner.invoke(cli_app.app, ["bench", "plan", str(bad), "--node", A])
    assert result.exit_code == CliExit.USAGE_ERROR, result.output
    assert "not a finite number" in result.stderr


def test_plan_asks_the_controller_for_nodes_and_skips_revoked(
    runner: CliRunner, controller: TransportFactory
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/nodes"
        return httpx.Response(
            200,
            json={
                "count": 2,
                "nodes": [
                    {"node_id": A, "hostname": "laptop-a", "state": "online"},
                    {"node_id": B, "hostname": "laptop-b", "state": "revoked"},
                ],
            },
        )

    controller(handler)
    result = runner.invoke(cli_app.app, ["bench", "plan", str(EXAMPLE), "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["nodes"] == [A]


@pytest.mark.parametrize(
    "argv",
    [
        ["bench", "runs", "camp_bad"],
        ["bench", "submit", "../x"],
        ["bench", "campaigns", "job_01J0000000000000000000CCCC"],
    ],
)
def test_campaign_ids_are_checked_before_any_request(
    runner: CliRunner, controller: TransportFactory, argv: list[str]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"no request expected: {request.url}")

    controller(handler)
    result = runner.invoke(cli_app.app, argv)
    assert result.exit_code == CliExit.USAGE_ERROR


def _run(run_id: str, seq: int, state: str) -> dict[str, Any]:
    return {"run_id": run_id, "seq": seq, "state": state, "family": "cpu.primes"}


def test_runs_reads_every_page(runner: CliRunner, controller: TransportFactory) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        after = request.url.params["after"]
        seen.append(after)
        if after == "0":
            return httpx.Response(
                200, json={"runs": [_run("r1", 1, "done")], "next_after": 1}
            )
        return httpx.Response(
            200, json={"runs": [_run("r2", 2, "planned")], "next_after": None}
        )

    controller(handler)
    result = runner.invoke(cli_app.app, ["bench", "runs", CAMPAIGN, "--json"])
    assert result.exit_code == 0, result.output
    assert seen == ["0", "1"]
    assert [r["run_id"] for r in json.loads(result.stdout)["runs"]] == ["r1", "r2"]


def test_submit_wait_exits_126_when_a_run_failed(
    runner: CliRunner, controller: TransportFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bench_campaign_commands, "WAIT_POLL_S", 0.0)
    polls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            assert request.url.params["count"] == "2"
            return httpx.Response(
                200,
                json={
                    "campaign_id": CAMPAIGN,
                    "outcomes": [
                        {
                            "run_id": "r1",
                            "seq": 1,
                            "outcome": "submitted",
                            "job_id": "j1",
                        },
                        {
                            "run_id": "r2",
                            "seq": 2,
                            "outcome": "submitted",
                            "job_id": "j2",
                        },
                    ],
                    "submitted": 2,
                    "deferred": 0,
                    "skipped": 0,
                    "counts": {},
                },
            )
        polls["n"] += 1
        late = "submitted" if polls["n"] < 3 else "failed"
        return httpx.Response(
            200,
            json={
                "runs": [_run("r1", 1, "done"), _run("r2", 2, late)],
                "next_after": None,
            },
        )

    controller(handler)
    result = runner.invoke(
        cli_app.app, ["bench", "submit", CAMPAIGN, "-n", "2", "--wait"]
    )
    assert result.exit_code == CliExit.JOB_FAILED, result.output
    assert "1 done, 1 failed" in result.stdout
    assert polls["n"] == 3


def test_submit_prints_deferred_and_skipped_reasons(
    runner: CliRunner, controller: TransportFactory
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "campaign_id": CAMPAIGN,
                "outcomes": [
                    {
                        "run_id": "r1",
                        "seq": 1,
                        "outcome": "deferred",
                        "detail": "offline",
                    },
                    {
                        "run_id": "r2",
                        "seq": 2,
                        "outcome": "skipped",
                        "detail": "no GPU",
                    },
                ],
                "submitted": 0,
                "deferred": 1,
                "skipped": 1,
                "counts": {},
            },
        )

    controller(handler)
    result = runner.invoke(cli_app.app, ["bench", "submit", CAMPAIGN, "-n", "2"])
    assert result.exit_code == 0, result.output
    assert "submitted 0, deferred 1, skipped 1" in result.stdout
    assert "run 1 deferred: offline" in result.stderr
    assert "run 2 skipped: no GPU" in result.stderr
