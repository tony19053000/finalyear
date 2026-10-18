"""``compute bench start|pause|resume|cancel|status`` - unattended campaigns (V0.9 G5, D219/D246).

* ``start ID [--per-node N] [--cooldown S] [--retries R]`` - hand the campaign
  to the controller's background runner (it then submits the runs itself:
  throttled per node, never to a node whose owner is using it or that runs a
  user's job; ``compute bench submit`` is refused from now on);
* ``pause ID`` / ``resume ID`` - stop / restart submissions (running jobs
  finish);
* ``cancel ID`` - cancel the campaign's benchmark jobs, skip its open runs;
* ``status ID`` - counts by state, done %, ETA (median run time), the
  infrastructure failure rate (owner pre-emptions excluded).

All need the controller and an **admin** login. ``--json`` prints the API
document (docs/CLI.md section 3).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any

import typer

from meshforge.cli import render
from meshforge.cli import shared as cli
from meshforge.cli.bench_campaign_commands import RUN_STATES, _campaign_id
from meshforge.cli.bench_commands import bench_app

__all__ = ["status_lines"]


def _duration(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    total = round(seconds)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}h{minutes:02d}m" if hours else f"{minutes}m{secs:02d}s"


def status_lines(doc: Mapping[str, Any]) -> list[str]:
    """The human view of a ``status`` document (pure; tests read it)."""
    counts = doc.get("counts", {})
    shown = ", ".join(f"{k} {counts.get(k, 0)}" for k in RUN_STATES)
    state = str(doc.get("state", "?"))
    if doc.get("finished_reason"):
        state += f" ({doc['finished_reason']})"
    owner = "runner" if doc.get("runner_owned") else "manual (compute bench submit)"
    rate = doc.get("infra_failure_rate")
    lines = [
        f"{doc.get('campaign_id')}: {state}, driven by {owner}",
        f"runs: {shown}",
        f"done: {float(doc.get('done_percent', 0.0)):.1f} %, ETA "
        f"{_duration(doc.get('eta_s'))} (median run {_duration(doc.get('median_run_s'))})",
        "infrastructure failures: "
        + ("none recorded yet" if rate is None else f"{100.0 * float(rate):.1f} %")
        + f" ({doc.get('infra_failures', 0)} of {doc.get('records', 0)} attempts; "
        f"{doc.get('retries', 0)} retries; {doc.get('preempted', 0)} stopped by "
        "the owner guard, not counted)",
        f"throttle: {doc.get('max_concurrent_per_node')} per node, cool-down "
        f"{float(doc.get('cooldown_s', 0)):g} s, {doc.get('max_infra_retries')} "
        "infra retries",
    ]
    if doc.get("runner_degraded"):
        lines.append("warning: the campaign runner keeps failing (see controller logs)")
    return [render.flat(line) for line in lines]


def _control(
    campaign_id: str,
    verb: str,
    body: Mapping[str, Any] | None,
    json_output: bool,
) -> None:
    checked = _campaign_id(campaign_id)
    settings = cli._settings()
    with cli._client(settings) as client:
        result = cli._fetch(lambda: client.bench_control(checked, verb, body))
    if json_output:
        cli._echo_json(result)
        return
    reason = result.get("finished_reason")
    render.line(
        render.flat(
            f"{result.get('campaign_id')}: {result.get('state')}"
            + (f" ({reason})" if reason else "")
        )
    )
    if verb == "cancel":
        render.note(f"cancel requested for {result.get('cancelled_jobs', 0)} job(s)")
    elif verb in ("start", "resume"):
        render.note(f"follow it with `compute bench status {checked}`")


@bench_app.command()
def start(
    campaign_id: Annotated[str, typer.Argument(help="The campaign (camp_...).")],
    per_node: Annotated[
        int | None,
        typer.Option(
            "--per-node", min=1, max=64, help="Benchmark jobs at once per node."
        ),
    ] = None,
    cooldown_s: Annotated[
        float | None,
        typer.Option(
            "--cooldown", min=0.0, max=86_400.0, help="Seconds between runs on a node."
        ),
    ] = None,
    retries: Annotated[
        int | None,
        typer.Option(
            "--retries", min=0, max=10, help="Retries of an infrastructure failure."
        ),
    ] = None,
    json_output: cli.JsonOption = False,
) -> None:
    """Let the controller run the campaign unattended (admin; idempotent)."""
    body = {
        k: v
        for k, v in {
            "max_concurrent_per_node": per_node,
            "cooldown_s": cooldown_s,
            "max_infra_retries": retries,
        }.items()
        if v is not None
    }
    _control(campaign_id, "start", body or None, json_output)


@bench_app.command()
def pause(
    campaign_id: Annotated[str, typer.Argument(help="The campaign (camp_...).")],
    json_output: cli.JsonOption = False,
) -> None:
    """Stop submitting the campaign's runs; running jobs finish (admin)."""
    _control(campaign_id, "pause", None, json_output)


@bench_app.command()
def resume(
    campaign_id: Annotated[str, typer.Argument(help="The campaign (camp_...).")],
    json_output: cli.JsonOption = False,
) -> None:
    """Resume a paused campaign (admin)."""
    _control(campaign_id, "resume", None, json_output)


@bench_app.command()
def cancel(
    campaign_id: Annotated[str, typer.Argument(help="The campaign (camp_...).")],
    json_output: cli.JsonOption = False,
) -> None:
    """Cancel the campaign: its benchmark jobs cancelled, open runs skipped (admin)."""
    _control(campaign_id, "cancel", None, json_output)


@bench_app.command()
def status(
    campaign_id: Annotated[str, typer.Argument(help="The campaign (camp_...).")],
    json_output: cli.JsonOption = False,
) -> None:
    """Progress of a campaign: counts, done %, ETA, infra failure rate (admin)."""
    checked = _campaign_id(campaign_id)
    settings = cli._settings()
    with cli._client(settings) as client:
        doc = cli._fetch(lambda: client.bench_status(checked))
    if json_output:
        cli._echo_json(doc)
        return
    for line in status_lines(doc):
        render.line(line)
