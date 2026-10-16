"""V0.6 G2: ``compute explain`` rendering, the client's 409 rejections, and
``compute run``'s new flag checks - without a controller."""

from __future__ import annotations

import io
import json
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from rich.console import Console
from typer.testing import CliRunner

from meshforge.cli.app import app
from meshforge.cli.client import ControllerClient, ControllerError
from meshforge.cli.explain_commands import decision_tables
from meshforge.common.errors import FailureType
from meshforge.common.exit_codes import ExitCode

pytestmark = pytest.mark.unit

TOKEN = "t" * 43


def _term(seconds: float, source: str, **detail: Any) -> dict[str, Any]:
    return {"seconds": seconds, "source": source, "detail": detail}


def _estimate(compute_source: str = "history") -> dict[str, Any]:
    return {
        "total_s": 12.5,
        "terms": {
            "queue": _term(0, "measured"),
            "transfer": _term(2.5, "assumed", missing_bytes=10),
            "env": _term(0, "assumed"),
            "compute": _term(10, compute_source),
            "output": _term(0, "assumed"),
        },
    }


PAYLOAD: dict[str, Any] = {
    "job_id": "job_x",
    "count": 2,
    "decisions": [
        {
            "id": 1,
            "attempt_no": 1,
            "attempt_id": None,
            "outcome": "waiting",
            "policy": "cost_model",
            "chosen_node_id": None,
            "created_at": "2026-10-05T10:00:00Z",
            "snapshot_digest": "0" * 64,
            "candidates": [
                {
                    "node_id": "node_busy",
                    "mode": "offload",
                    "feasible": False,
                    "reject_code": "slots_full",
                    "reject_text": "every CPU slot of the node is in use",
                },
            ],
            "actual": None,
        },
        {
            "id": 2,
            "attempt_no": 1,
            "attempt_id": "att_1",
            "outcome": "dispatched",
            "policy": "cost_model",
            "chosen_node_id": "node_local",
            "created_at": "2026-10-05T10:00:05Z",
            "snapshot_digest": "1" * 64,
            "candidates": [
                {
                    "node_id": "node_local",
                    "mode": "local",
                    "feasible": True,
                    "slot": "cpu",
                    "estimate": _estimate(),
                    "score": 12.5,
                    "rank": 1,
                },
                {
                    "node_id": "node_far",
                    "mode": "offload",
                    "feasible": True,
                    "slot": "cpu",
                    "estimate": _estimate("scaled"),
                    "score": 40.0,
                    "rank": 2,
                },
                {
                    "node_id": "node_\x1b[31mevil",
                    "mode": "offload",
                    "feasible": False,
                    "reject_code": "not_exec",
                    "reject_text": "the node does not accept jobs\x07",
                },
            ],
            "actual": {
                "attempt_id": "att_1",
                "node_id": "node_local",
                "state": "done",
                "failure_type": None,
                "phases_ms": {
                    "queue": 120,
                    "stage": 0,
                    "env": None,
                    "run": 9000,
                    "collect": 300,
                },
            },
        },
    ],
}


def _text(payload: dict[str, Any]) -> str:
    out = io.StringIO()
    console = Console(file=out, width=250, color_system=None)
    for table in decision_tables(payload):
        console.print(table)
    return out.getvalue()


def test_explain_shows_every_candidate_reason_source_and_actual() -> None:
    text = _text(PAYLOAD)
    for node in ("node_busy", "node_local", "node_far"):
        assert node in text
    assert "slots_full: every CPU slot of the node is in use" in text
    assert "not_exec" in text
    assert "waiting (policy cost_model)" in text
    assert "dispatched (policy cost_model)" in text
    assert "chosen" in text and "local" in text
    # predicted terms carry their source
    assert "2.50s (assumed)" in text
    assert "10.00s (history)" in text and "10.00s (scaled)" in text
    assert "0.00s (measured)" in text
    # actual vs predicted, phase names, a skipped phase
    assert "actual: attempt att_1 on node_local - done" in text
    assert "9.00s" in text and "skipped" in text and "stage" in text


def test_explain_flattens_control_characters() -> None:
    text = _text(PAYLOAD)
    assert "\x1b" not in text and "\x07" not in text


def test_explain_tolerates_garbage() -> None:
    assert _text({"decisions": "x"}) == ""
    junk = {"decisions": [{"candidates": [1, {"estimate": "x"}], "actual": 3}]}
    assert "decision" in _text(junk)


def _client(handler: Any) -> ControllerClient:
    return ControllerClient(
        "http://ctl:1",
        timeout_s=1.0,
        token=SecretStr(TOKEN),
        transport=httpx.MockTransport(handler),
    )


def test_client_keeps_the_rejections_of_a_409_bounded_and_clean() -> None:
    rejections = [
        {"node_id": f"node_{i}\x1b", "reject_code": "no_gpu", "reject_text": "t\n"}
        for i in range(70)
    ]

    def handler(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            json={
                "error": {
                    "code": "no_feasible_plan",
                    "message": "none",
                    "rejections": rejections,
                }
            },
        )

    with _client(handler) as client, pytest.raises(ControllerError) as caught:
        client.submit_job({"manifest": {}}, idempotency_key="k")
    assert caught.value.failure_type is FailureType.NO_FEASIBLE_PLAN
    kept = caught.value.details["rejections"]
    assert len(kept) == 64
    assert all(
        "\x1b" not in r["node_id"] and "\n" not in r["reject_text"] for r in kept
    )


def test_client_decisions_query() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"decisions": [], "count": 0})

    with _client(handler) as client:
        client.decisions("job_x", attempt=3)
    assert seen[0].url.path == "/v1/jobs/job_x/decisions"
    assert seen[0].url.params["attempt"] == "3"


@pytest.mark.parametrize(
    "argv",
    [
        ["run", ".", "--policy", "fastest"],
        ["run", ".", "--node", "node_01J00000000000000000000000", "--local"],
        ["run", ".", "--priority", "11"],
        ["run", ".", "--priority", "-11"],
        ["explain", "not-a-job"],
        ["explain", "job_01J00000000000000000000000", "--attempt", "0"],
    ],
)
def test_bad_flags_are_usage_errors_before_any_request(argv: list[str]) -> None:
    result = CliRunner().invoke(app, argv)
    assert result.exit_code == ExitCode.USAGE_ERROR, result.output


def test_explain_json_passes_the_answer_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from meshforge.cli import shared as cli_shared

    def handler(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=PAYLOAD)

    monkeypatch.setattr(cli_shared, "_client", lambda _settings: _client(handler))
    result = CliRunner().invoke(
        app, ["explain", "job_01J00000000000000000000000", "--json"]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["count"] == 2
    plain = CliRunner().invoke(app, ["explain", "job_01J00000000000000000000000"])
    assert plain.exit_code == 0 and "node_far" in plain.output
