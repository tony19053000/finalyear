"""``compute planner ml train|models|pin|unpin`` - the ML model registry
(V0.15 G3, D339, D358-D361).

* ``train [--wait] [--timeout S]`` (**admin**, ``POST /v1/planner/ml/train``):
  start a retrain on the controller (a child process there: dataset, model
  selection, activation gate). ``--wait`` polls ``GET /v1/planner/ml/train``
  until it ends and prints the result - the new set ``active`` or
  ``rejected`` with the gate's reason, or why nothing was written
  (``no_new_runs``, ``insufficient_data``, ...); exit 1 if it failed. A
  controller without the ``ml`` extra answers 409 (exit 1 with the reason).
* ``models [--limit N] [--set VERSION] [--json]`` (``GET
  /v1/planner/ml/models``): the newest sets (status, reason, learned terms),
  what the planner may use now, and the retrain task's state; ``--set``
  shows one set's members, metrics and gate rules.
* ``pin VERSION`` / ``unpin VERSION`` (**admin**): the override and
  rollback. A rejected set, or one whose artefacts are gone, cannot be
  pinned (exit 1).

``--json`` prints the API document. Every value from the controller goes
through the renderer's cell / echo scrubbing.
"""

from __future__ import annotations

import re
import time
from collections.abc import Mapping
from typing import Annotated, Any, Final

import typer
from rich.table import Table

from meshforge.cli import render
from meshforge.cli import shared as cli
from meshforge.cli.exits import CliExit
from meshforge.cli.planner_commands import ml_app

__all__ = [
    "ml_models_lines",
    "ml_set_table",
    "ml_sets_table",
    "regret_text",
    "retrain_line",
]

_SET_RE: Final = re.compile(r"^sha256:[0-9a-f]{64}$")
_POLL_S: Final = 2.0
_SHORT: Final = 14
_DASH: Final = "-"


def _short(version: Any) -> str:
    text = render.echo(version)
    return text[: len("sha256:") + 7] if text.startswith("sha256:") else text


def _checked(set_version: str) -> str:
    if not _SET_RE.fullmatch(set_version):
        cli._die(
            "VERSION must be a model set version sha256:<64 hex> "
            "(see compute planner ml models)",
            CliExit.USAGE_ERROR,
        )
    return set_version


def _learned(members: Any) -> str:
    if not isinstance(members, list):
        return _DASH
    parts = [
        f"{render.echo(m.get('term'))}={render.echo(m.get('algorithm'))}"
        for m in members
        if isinstance(m, Mapping) and m.get("algorithm") not in (None, "none")
    ]
    return ", ".join(parts) or "none"


def ml_sets_table(doc: Mapping[str, Any]) -> Table:
    """The sets list (pure; tests read it)."""
    table = render.table(
        "SET", "STATUS", "CREATED", "LEARNED", "REASON", title="model sets"
    )
    sets = doc.get("sets")
    for item in sets if isinstance(sets, list) else []:
        if not isinstance(item, Mapping):
            continue
        table.add_row(
            render.cell(_short(item.get("set_version"))),
            render.cell(item.get("status")),
            render.cell(item.get("created_at")),
            render.cell(_learned(item.get("members"))),
            render.cell(item.get("reason")),
        )
    return table


def ml_set_table(doc: Mapping[str, Any]) -> Table:
    """One set's members (pure)."""
    table = render.table(
        "TERM", "ALGORITHM", "ROWS", "INNER MALE", "WIDTH", "REASON", title="members"
    )
    members = doc.get("members")
    for m in members if isinstance(members, list) else []:
        if not isinstance(m, Mapping):
            continue
        metrics = m.get("metrics") if isinstance(m.get("metrics"), Mapping) else {}
        assert isinstance(metrics, Mapping)  # noqa: S101 - narrowed above
        table.add_row(
            render.cell(m.get("term")),
            render.cell(m.get("algorithm")),
            render.cell(metrics.get("rows")),
            render.cell(metrics.get("inner_male")),
            render.cell(metrics.get("interval_width")),
            render.cell(m.get("reason")),
        )
    return table


def retrain_line(state: Mapping[str, Any]) -> str:
    """The retrain task in one line (pure)."""
    if not state.get("available"):
        missing = state.get("missing")
        names = (
            ", ".join(render.echo(m) for m in missing)
            if isinstance(missing, list)
            else ""
        )
        return f"retrain: unavailable (the ml extra is not installed: {names})"
    if state.get("running"):
        return f"retrain: running since {render.echo(state.get('running_since'))}"
    last = state.get("last")
    if not isinstance(last, Mapping):
        return "retrain: none yet"
    return "retrain: last " + _result_text(last)


def _result_text(last: Mapping[str, Any]) -> str:
    when = render.echo(last.get("finished_at"))
    if last.get("error"):
        return (
            f"FAILED ({render.echo(last.get('error'))}: "
            f"{render.echo(last.get('detail'))}) at {when}"
        )
    if last.get("created_set"):
        text = (
            f"set {_short(last.get('created_set'))} "
            f"{render.echo(last.get('status'))} at {when}"
        )
        if last.get("reason"):
            text += f": {render.echo(last.get('reason'))}"
        return text
    return f"nothing written ({render.echo(last.get('skipped'))}) at {when}"


def ml_models_lines(doc: Mapping[str, Any]) -> list[str]:
    """What is in use and the retrain state (pure)."""
    in_use = doc.get("in_use") if isinstance(doc.get("in_use"), Mapping) else {}
    assert isinstance(in_use, Mapping)  # noqa: S101 - narrowed above
    mode = render.echo(in_use.get("mode"))
    used = in_use.get("in_use_set")
    if mode == "off":
        line = "ml: off (the planner uses the calibration only)"
    elif used:
        line = f"ml: {mode}, planning with set {_short(used)} ({render.echo(in_use.get('kind'))})"
    elif in_use.get("unavailable"):
        line = (
            f"ml: {mode}, serving set {_short(doc.get('serving_set'))} not loaded "
            f"({render.echo(in_use.get('unavailable'))}); calibration only"
        )
    else:
        line = f"ml: {mode}, no model set in use (calibration only)"
    retrain = doc.get("retrain")
    lines = [line]
    if isinstance(retrain, Mapping):
        lines.append(retrain_line(retrain))
    return lines


def _show_set(doc: Mapping[str, Any]) -> None:
    render.line(
        f"set {render.echo(doc.get('set_version'))}: {render.echo(doc.get('status'))}"
        + (f" - {render.echo(doc.get('reason'))}" if doc.get("reason") else "")
    )
    render.line(
        f"dataset {render.echo(doc.get('dataset_digest'))}, features "
        f"v{render.echo(doc.get('feature_version'))}"
    )
    render.show(ml_set_table(doc))
    gate = doc.get("gate")
    if isinstance(gate, Mapping) and gate:
        failed = gate.get("rules_failed")
        render.line(
            "gate: "
            + ("accepted" if gate.get("accepted") else "rejected")
            + f" against {render.echo((gate.get('reference') or {}).get('kind'))}"
            + (
                f"; failed {', '.join(render.echo(r) for r in failed)}"
                if isinstance(failed, list) and failed
                else ""
            )
            + f"; regret {regret_text(gate.get('regret'))}"
        )


def regret_text(regret: Any) -> str:
    """The gate's regret part (pure): G3 rows hold the string
    ``pending_g4``; V0.15 G5a (D371) a block - ``passed`` / ``rejected`` /
    ``skipped`` / ``not_run`` with the groups and both mean regrets."""
    if not isinstance(regret, Mapping):
        return render.echo(regret)
    status = render.echo(regret.get("status"))
    if regret.get("groups") and regret.get("regret_candidate_pct") is not None:
        current = regret.get("current")
        against = (
            render.echo(current.get("set_version") or current.get("kind"))
            if isinstance(current, Mapping)
            else "-"
        )
        return (
            f"{status} (candidate {render.echo(regret.get('regret_candidate_pct'))} % "
            f"vs {against} {render.echo(regret.get('regret_current_pct'))} % over "
            f"{render.echo(regret.get('groups'))} groups)"
        )
    reason = regret.get("reason")
    return f"{status} ({render.echo(reason)})" if reason else status


@ml_app.command("models")
def ml_models(
    limit: Annotated[
        int, typer.Option("--limit", min=1, max=200, help="Newest N sets.")
    ] = 20,
    set_version: Annotated[
        str | None,
        typer.Option("--set", help="Show this set (members, gate)."),
    ] = None,
    json_output: cli.JsonOption = False,
) -> None:
    """The ML model sets and what the planner may use now."""
    if set_version is not None:
        _checked(set_version)
    settings = cli._settings()
    with cli._client(settings) as client:
        if set_version is not None:
            one = cli._fetch(lambda: client.planner_ml_model_set(set_version))
            if json_output:
                cli._echo_json(one)
                return
            _show_set(one)
            return
        doc = cli._fetch(lambda: client.planner_ml_models(limit=limit))
    if json_output:
        cli._echo_json(doc)
        return
    for text in ml_models_lines(doc):
        render.line(text)
    render.show(ml_sets_table(doc))


@ml_app.command("train")
def ml_train(
    wait: Annotated[
        bool, typer.Option("--wait", help="Wait for the retrain and print it.")
    ] = False,
    timeout: Annotated[
        float,
        typer.Option(
            "--timeout", min=1.0, help="With --wait: give up after this many s."
        ),
    ] = 7200.0,
    json_output: cli.JsonOption = False,
) -> None:
    """Retrain the ML model set now (admin; runs on the controller)."""
    settings = cli._settings()
    with cli._client(settings) as client:
        doc = cli._fetch(client.planner_ml_train)
        state = doc.get("retrain") if isinstance(doc.get("retrain"), Mapping) else {}
        assert isinstance(state, Mapping)  # noqa: S101 - narrowed above
        if not wait:
            if json_output:
                cli._echo_json(doc)
                return
            render.line(
                "retrain started"
                if doc.get("started")
                else "a retrain is already running"
            )
            return
        deadline = time.monotonic() + timeout
        while True:
            # `running` is true from the request until the retrain's end
            state = cli._fetch(client.planner_ml_train_state)
            last = state.get("last")
            if not state.get("running") and isinstance(last, Mapping):
                break
            if time.monotonic() > deadline:
                cli._die(
                    f"the retrain did not finish within {timeout:g} s (it keeps "
                    "running on the controller; see compute planner ml models)",
                    CliExit.FAILURE,
                )
            time.sleep(_POLL_S)
    if json_output:
        cli._echo_json(state)
    else:
        assert isinstance(last, Mapping)  # noqa: S101 - finished above
        render.line(_result_text(last))
    if isinstance(last, Mapping) and last.get("error"):
        raise typer.Exit(int(CliExit.FAILURE))


def _pin(set_version: str, *, pin: bool, json_output: bool) -> None:
    _checked(set_version)
    settings = cli._settings()
    with cli._client(settings) as client:
        doc = cli._fetch(lambda: client.planner_ml_pin(set_version, pin=pin))
    if json_output:
        cli._echo_json(doc)
        return
    _show_set(doc)


@ml_app.command("pin")
def ml_pin(
    set_version: Annotated[str, typer.Argument(help="sha256:<64 hex>")],
    json_output: cli.JsonOption = False,
) -> None:
    """Pin a model set: the planner uses it until unpinned (admin)."""
    _pin(set_version, pin=True, json_output=json_output)


@ml_app.command("unpin")
def ml_unpin(
    set_version: Annotated[str, typer.Argument(help="sha256:<64 hex>")],
    json_output: cli.JsonOption = False,
) -> None:
    """Unpin a model set (admin)."""
    _pin(set_version, pin=False, json_output=json_output)
