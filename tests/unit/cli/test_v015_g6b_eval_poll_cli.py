"""V0.15 G6b (D378): ``compute planner eval`` starts, then polls.

* POST answers 202 ``{report_id, status: running}``; the CLI polls ``GET
  /v1/planner/eval-reports/{id}`` until it is no longer ``running`` and
  prints the report (stdout) - the "started" line goes to stderr;
* a ``failed`` report prints its reason and detail and exits 1 (``--json``
  prints the record, still exit 1);
* ``--timeout S`` bounds the wait: over it, exit 1 with "did not finish
  within S s" - and the fake answers ``done`` after many polls, so a CLI
  that ignored ``--timeout`` would exit 0 (mutation check);
* ``--md`` fetches the Markdown of the finished report;
* a 409 ``eval_already_running`` is one stderr line, never a traceback;
* the request says ``background: true`` (a controller refuses one without).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from mf_cli_support import TransportFactory
from mf_eval_world import fabric, group, records
from typer.testing import CliRunner

from meshforge.cli import app as cli_app
from meshforge.cli import planner_commands
from meshforge.common.ml_eval import EVAL_WAIT_DEFAULT_S
from meshforge.controller.planner_eval.core import evaluate
from meshforge.controller.planner_eval.model import EvalConfig, default_policies

pytestmark = pytest.mark.unit

CAMP = "camp_01AAAAAAAAAAAAAAAAAAAAAAAA"
PATH = "/v1/planner/eval-reports"


@pytest.fixture(autouse=True)
def _fast_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(planner_commands, "_EVAL_POLL_S", 0.01)


def _done(report_id: int = 9) -> dict[str, Any]:
    request, snapshot = fabric({"node_a": 100, "node_b": 10, "node_c": 50})
    g = group("g1", {"node_a": 300, "node_b": 100, "node_c": 200})
    report = evaluate(
        [g],
        records(g, request, snapshot),
        {},
        default_policies(),
        EvalConfig(bootstrap_resamples=50),
        campaign_ids=[CAMP],
    )
    return {
        "id": report_id,
        "created_at": "2026-10-23T10:00:00Z",
        "campaign_ids": [CAMP],
        "holdout": None,
        "leaked": False,
        "status": "done",
        "reason": None,
        "detail": None,
        "report": report,
    }


def _state(status: str, report_id: int = 9, **kw: Any) -> dict[str, Any]:
    return {
        "id": report_id,
        "created_at": "2026-10-23T10:00:00Z",
        "campaign_ids": [CAMP],
        "holdout": None,
        "leaked": False,
        "status": status,
        "reason": kw.get("reason"),
        "detail": kw.get("detail"),
        "report": None,
    }


def test_202_then_poll_until_done(
    runner: CliRunner, controller: TransportFactory
) -> None:
    seen: list[tuple[str, str, Any]] = []
    polls = iter([_state("running"), _state("running"), _done()])

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        seen.append((request.method, request.url.path, body))
        if request.method == "POST":
            return httpx.Response(202, json={"report_id": 9, "status": "running"})
        return httpx.Response(200, json=next(polls))

    controller(handler)
    result = runner.invoke(cli_app.app, ["planner", "eval", "--campaign", CAMP])
    assert result.exit_code == 0, result.output
    assert seen[0] == (
        "POST",
        PATH,
        {"campaign_ids": [CAMP], "allow_leak": False, "background": True},
    )
    assert [s[:2] for s in seen[1:]] == [("GET", f"{PATH}/9")] * 3
    assert "report 9: campaigns " + CAMP in result.stdout
    assert "criterion 1: too_few_groups" in result.stdout
    assert "evaluation report 9 started" in result.stderr
    assert "started" not in result.stdout


def test_a_failed_report_prints_its_reason_and_exits_1(
    runner: CliRunner, controller: TransportFactory
) -> None:
    failed = _state(
        "failed",
        reason="controller_restart",
        detail="the controller stopped while this evaluation ran; run it again",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202, json={"report_id": 9, "status": "running"})
        return httpx.Response(200, json=failed)

    controller(handler)
    result = runner.invoke(cli_app.app, ["planner", "eval", "--campaign", CAMP])
    assert result.exit_code == 1, result.output
    assert (
        "evaluation report 9 failed: controller_restart - the controller stopped"
        in result.stdout
    )
    as_json = runner.invoke(
        cli_app.app, ["planner", "eval", "--campaign", CAMP, "--json"]
    )
    assert as_json.exit_code == 1
    assert json.loads(as_json.stdout)["reason"] == "controller_restart"


def test_timeout_flag_bounds_the_wait(
    runner: CliRunner, controller: TransportFactory
) -> None:
    gets = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202, json={"report_id": 9, "status": "running"})
        gets[0] += 1
        # a CLI that ignored --timeout would see done after 400 polls (~4 s)
        return httpx.Response(200, json=_done() if gets[0] > 400 else _state("running"))

    controller(handler)
    started = time.monotonic()
    result = runner.invoke(
        cli_app.app, ["planner", "eval", "--campaign", CAMP, "--timeout", "1"]
    )
    took = time.monotonic() - started
    assert result.exit_code == 1, result.output
    assert "did not finish within 1 s" in result.stderr
    assert "keeps running on the controller" in result.stderr
    assert took < 3.5
    assert 1 <= gets[0] <= 400


def test_timeout_default_and_bounds(
    runner: CliRunner, controller: TransportFactory
) -> None:
    from meshforge.common.ml_eval import (
        DEFAULT_ML_EVAL_TIMEOUT_S,
        EVAL_REPLAY_MARGIN_S,
    )

    assert EVAL_WAIT_DEFAULT_S == DEFAULT_ML_EVAL_TIMEOUT_S + EVAL_REPLAY_MARGIN_S
    controller(lambda _r: httpx.Response(500))
    bad = runner.invoke(
        cli_app.app, ["planner", "eval", "--campaign", CAMP, "--timeout", "0"]
    )
    assert bad.exit_code == 2  # usage error, before any request


def test_md_after_poll(
    runner: CliRunner, controller: TransportFactory, tmp_path: Path
) -> None:
    urls: list[str] = []
    polls = iter([_state("running"), _done()])

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        if request.method == "POST":
            return httpx.Response(202, json={"report_id": 9, "status": "running"})
        if "format=markdown" in str(request.url):
            return httpx.Response(200, text="# report 9\n")
        return httpx.Response(200, json=next(polls))

    controller(handler)
    target = tmp_path / "r.md"
    result = runner.invoke(
        cli_app.app, ["planner", "eval", "--campaign", CAMP, "--md", str(target)]
    )
    assert result.exit_code == 0, result.output
    assert target.read_text(encoding="utf-8") == "# report 9\n"
    assert urls[-1].endswith(f"{PATH}/9?format=markdown")


def test_a_running_evaluation_is_one_error_line(
    runner: CliRunner, controller: TransportFactory
) -> None:
    controller(
        lambda _r: httpx.Response(
            409,
            json={
                "error": {
                    "code": "conflict",
                    "message": "an evaluation is already running on this controller",
                    "reason": "eval_already_running",
                    "report_id": 4,
                }
            },
        )
    )
    result = runner.invoke(cli_app.app, ["planner", "eval", "--campaign", CAMP])
    assert result.exit_code != 0
    assert "already running" in result.stderr
    assert "Traceback" not in result.output


def test_an_answer_without_a_report_id_is_an_error(
    runner: CliRunner, controller: TransportFactory
) -> None:
    controller(lambda _r: httpx.Response(202, json={"status": "running"}))
    result = runner.invoke(cli_app.app, ["planner", "eval", "--campaign", CAMP])
    assert result.exit_code == 1
    assert "did not say which report" in result.stderr
