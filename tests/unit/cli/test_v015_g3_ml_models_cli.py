"""V0.15 G3: ``compute planner ml train|models|pin|unpin``.

The human views and the requests: ``models`` (what is in use, the retrain
line, the sets table; ``--set`` with members and gate), ``train`` (started /
already running; ``--wait`` polls ``GET /v1/planner/ml/train`` until the
retrain ends and prints its result, exit 1 when it failed), ``pin`` /
``unpin`` (the set path; a malformed version is a usage error before any
request), server strings scrubbed, a 409 is exit 1 with the message.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from mf_cli_support import TransportFactory
from typer.testing import CliRunner

from meshforge.cli import app as cli_app
from meshforge.cli import ml_model_commands

pytestmark = pytest.mark.unit

SET_A = "sha256:" + "a" * 64
SET_B = "sha256:" + "b" * 64


def _set(version: str = SET_A, **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "set_version": version,
        "created_at": "2026-10-22T10:00:00Z",
        "status_changed_at": "2026-10-22T10:00:00Z",
        "status": "active",
        "reason": None,
        "dataset_digest": "sha256:" + "d" * 64,
        "feature_version": 1,
        "members": [
            {
                "term": "compute",
                "algorithm": "lightgbm",
                "version": "sha256:" + "c" * 64,
                "params": {"num_leaves": 7},
                "metrics": {"rows": 400, "inner_male": 0.081, "interval_width": 0.4},
                "reason": None,
                "artefact_bytes": 1234,
            },
            {
                "term": "env",
                "algorithm": "none",
                "version": "sha256:" + "e" * 64,
                "params": {},
                "metrics": {"rows": 12},
                "reason": "insufficient_data: 12 rows",
                "artefact_bytes": None,
            },
        ],
    }
    base.update(over)
    return base


def _state(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "available": True,
        "missing": [],
        "running": False,
        "running_since": None,
        "retrains": 1,
        "failures": 0,
        "last": None,
        "interval_s": 86400.0,
    }
    base.update(over)
    return base


def _last(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "trigger": "manual",
        "started_at": "2026-10-22T10:00:00Z",
        "finished_at": "2026-10-22T10:01:00Z",
        "created_set": SET_B,
        "status": "rejected",
        "reason": "not_better: no learned term beats the calibration",
        "skipped": None,
        "error": None,
        "detail": None,
        "superseded_set": None,
        "rows": 900,
        "seconds": {"total_s": 60.0},
    }
    base.update(over)
    return base


MODELS = {
    "in_use": {
        "mode": "auto",
        "pinned_config": None,
        "kind": "active",
        "in_use_set": SET_A,
        "unavailable": None,
    },
    "serving_set": SET_A,
    "sets": [
        _set(),
        _set(SET_B, status="rejected", reason="family_tolerance: x\x1b[31m"),
    ],
    "retrain": _state(last=_last()),
}


def test_models_shows_what_is_in_use_and_the_sets(
    runner: CliRunner, controller: TransportFactory
) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url.path}?{request.url.query.decode()}")
        return httpx.Response(200, json=MODELS)

    controller(handler)
    result = runner.invoke(
        cli_app.app, ["planner", "ml", "models", "--limit", "5"], env={"COLUMNS": "200"}
    )
    assert result.exit_code == 0, result.output
    assert seen == ["GET /v1/planner/ml/models?limit=5"]
    out = result.stdout
    assert "ml: auto, planning with set sha256:aaaaaaa (active)" in out
    assert "retrain: last set sha256:bbbbbbb rejected" in out
    assert "compute=lightgbm" in out
    assert "family_tolerance" in out
    assert "\x1b[31m" not in out  # scrubbed


def test_models_set_shows_members_and_the_gate(
    runner: CliRunner, controller: TransportFactory
) -> None:
    detail = {
        **_set(),
        "gate": {
            "accepted": False,
            "rules_failed": ["coverage", "not_better"],
            "reference": {"kind": "active"},
            "regret": "pending_g4",
        },
        "inputs": {},
    }
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json=detail)

    controller(handler)
    result = runner.invoke(
        cli_app.app, ["planner", "ml", "models", "--set", SET_A], env={"COLUMNS": "200"}
    )
    assert result.exit_code == 0, result.output
    assert seen == [f"/v1/planner/ml/models/{SET_A}"]
    out = result.stdout
    assert f"set {SET_A}: active" in out
    assert "lightgbm" in out and "insufficient_data" in out
    assert "gate: rejected against active; failed coverage, not_better" in out
    assert "regret pending_g4" in out


def test_a_malformed_set_version_is_a_usage_error_without_a_request(
    runner: CliRunner, controller: TransportFactory
) -> None:
    seen: list[str] = []
    controller(lambda r: seen.append(r.url.path) or httpx.Response(200, json={}))
    for argv in (
        ["planner", "ml", "pin", "sha256:xyz"],
        ["planner", "ml", "unpin", "../../healthz"],
        ["planner", "ml", "models", "--set", "7"],
    ):
        result = runner.invoke(cli_app.app, argv)
        assert result.exit_code == 2, (argv, result.output)
    assert seen == []


def test_pin_and_unpin_post_to_the_set(
    runner: CliRunner, controller: TransportFactory
) -> None:
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        status = "pinned" if request.url.path.endswith("/pin") else "superseded"
        return httpx.Response(
            200, json={**_set(status=status), "gate": {}, "inputs": {}}
        )

    controller(handler)
    result = runner.invoke(cli_app.app, ["planner", "ml", "pin", SET_A])
    assert result.exit_code == 0, result.output
    assert f"set {SET_A}: pinned" in result.stdout
    result = runner.invoke(cli_app.app, ["planner", "ml", "unpin", SET_A])
    assert result.exit_code == 0, result.output
    assert seen == [
        ("POST", f"/v1/planner/ml/models/{SET_A}/pin"),
        ("POST", f"/v1/planner/ml/models/{SET_A}/unpin"),
    ]


def test_pin_refused_is_exit_1_with_the_reason(
    runner: CliRunner, controller: TransportFactory
) -> None:
    controller(
        lambda _r: httpx.Response(
            409,
            json={
                "error": {
                    "failure_type": "conflict",
                    "message": "model set was rejected by the activation gate",
                    "retryable": False,
                }
            },
        )
    )
    result = runner.invoke(cli_app.app, ["planner", "ml", "pin", SET_B])
    assert result.exit_code == 1, result.output
    assert "rejected by the activation gate" in result.output


def test_train_without_wait(runner: CliRunner, controller: TransportFactory) -> None:
    controller(
        lambda _r: httpx.Response(
            200, json={"started": True, "retrain": _state(running=True)}
        )
    )
    result = runner.invoke(cli_app.app, ["planner", "ml", "train"])
    assert result.exit_code == 0, result.output
    assert "retrain started" in result.stdout


def test_train_wait_polls_until_the_retrain_ends(
    runner: CliRunner, controller: TransportFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ml_model_commands, "_POLL_S", 0.0)
    states = iter(
        [
            _state(running=True, last=_last(created_set=SET_A, status="active")),
            _state(running=True, last=_last(created_set=SET_A, status="active")),
            _state(running=False, last=_last()),
        ]
    )
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.method)
        if request.method == "POST":
            return httpx.Response(
                200, json={"started": True, "retrain": _state(running=True)}
            )
        return httpx.Response(200, json=next(states))

    controller(handler)
    result = runner.invoke(cli_app.app, ["planner", "ml", "train", "--wait"])
    assert result.exit_code == 0, result.output
    assert seen == ["POST", "GET", "GET", "GET"]
    assert "set sha256:bbbbbbb rejected" in result.stdout
    assert "not_better" in result.stdout


def test_train_wait_failed_retrain_is_exit_1(
    runner: CliRunner, controller: TransportFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ml_model_commands, "_POLL_S", 0.0)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"started": False, "retrain": _state()})
        return httpx.Response(
            200,
            json=_state(
                last=_last(
                    created_set=None,
                    status=None,
                    reason=None,
                    error="timeout",
                    detail="killed after 3600 s",
                )
            ),
        )

    controller(handler)
    result = runner.invoke(cli_app.app, ["planner", "ml", "train", "--wait"])
    assert result.exit_code == 1, result.output
    assert "FAILED (timeout: killed after 3600 s)" in result.stdout


def test_train_unavailable_controller_is_exit_1(
    runner: CliRunner, controller: TransportFactory
) -> None:
    controller(
        lambda _r: httpx.Response(
            409,
            json={
                "error": {
                    "failure_type": "conflict",
                    "message": "this controller cannot train models: the ml extra "
                    "is not installed (missing sklearn)",
                    "retryable": False,
                }
            },
        )
    )
    result = runner.invoke(cli_app.app, ["planner", "ml", "train"])
    assert result.exit_code == 1
    assert "ml extra is not installed" in result.output


def test_retrain_line_variants() -> None:
    assert "unavailable" in ml_model_commands.retrain_line(
        _state(available=False, missing=["lightgbm"])
    )
    assert "running since" in ml_model_commands.retrain_line(
        _state(running=True, running_since="2026-10-22T10:00:00Z")
    )
    assert ml_model_commands.retrain_line(_state()) == "retrain: none yet"
    assert "nothing written (no_new_runs)" in ml_model_commands.retrain_line(
        _state(last=_last(created_set=None, skipped="no_new_runs"))
    )
