"""``compute planner replay DECISION_ID`` - re-plan one stored decision and
say whether it reproduces (V0.15 G6a; D364, Q23; ADR-0020).

**Admin** (``GET /v1/planner/decisions/{id}/replay``). The controller plans
the decision again from its stored replay - with the calibration it used
and, for an ML decision, the model set it used, loaded by version - and
compares the candidates field for field. Prints the status and the first
differing paths; ``--json`` prints the API document.

Exit codes: 0 ``reproduced``; 1 anything else (``differs``,
``ml_model_unavailable`` - the set is gone, never re-planned with
calibration instead -, ``no_replay``, ``replay_unreadable``); the usual
codes for a refused request.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any, Final

import typer

from meshforge.cli import render
from meshforge.cli import shared as cli
from meshforge.cli.exits import CliExit
from meshforge.cli.planner_commands import planner_app

__all__ = ["replay_lines"]

_REPRODUCED: Final = "reproduced"
_WHY: Final = {
    "reproduced": "the same candidates, field for field, and the same snapshot",
    "differs": "re-planned, but the result differs from the stored decision",
    "ml_model_unavailable": "its model set can no longer be loaded (not "
    "re-planned with calibration instead)",
    "no_replay": "the decision stored no replay",
    "replay_unreadable": "the stored replay cannot be read",
}


def replay_lines(doc: Mapping[str, Any]) -> list[str]:
    """The replay answer as lines (pure; tests read them)."""
    status = render.echo(doc.get("status"))
    version = doc.get("ml_model_version")
    lines = [
        f"decision {render.echo(doc.get('decision_id'))} (job "
        f"{render.echo(doc.get('job_id'))}, {render.echo(doc.get('outcome'))}, "
        f"policy {render.echo(doc.get('policy'))}): {status} - "
        f"{_WHY.get(str(doc.get('status')), 'unknown status')}",
        "model set: " + (render.echo(version) if version else "none (no ML)"),
        f"chosen: stored {render.echo(doc.get('stored_chosen_node_id'))}, "
        f"replayed {render.echo(doc.get('replayed_chosen_node_id'))}",
    ]
    differences = doc.get("differences")
    total = doc.get("differences_total")
    if isinstance(differences, list) and differences:
        lines.append(f"differences ({render.echo(total)}):")
        lines += [f"  {render.echo(d)}" for d in differences]
    return lines


@planner_app.command("replay")
def planner_replay(
    decision_id: Annotated[
        int, typer.Argument(min=1, help="The decision id (compute explain).")
    ],
    json_output: cli.JsonOption = False,
) -> None:
    """Re-plan a stored decision and compare it (admin)."""
    settings = cli._settings()
    with cli._client(settings) as client:
        doc = cli._fetch(lambda: client.planner_decision_replay(decision_id))
    if json_output:
        cli._echo_json(doc)
    else:
        for text in replay_lines(doc):
            render.line(text)
    if doc.get("status") != _REPRODUCED:
        raise typer.Exit(int(CliExit.FAILURE))
