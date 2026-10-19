"""``compute bench plan|create|campaigns|runs|submit`` - benchmark campaigns (V0.9 G3, D216).

* ``plan SPEC [--node ID ...] [--show N] [--json]`` - expand a campaign spec
  **locally** (:func:`meshforge.bench.campaign.expand`) against the live node
  list (``GET /v1/nodes``, revoked nodes excluded) or the ``--node`` ids, and
  print the run count, the estimated hours and the first runs. Runs nothing,
  stores nothing; the same spec and nodes always print the same list.
* ``create SPEC`` - store the campaign on the controller (``POST
  /v1/bench/campaigns``: validated and expanded there, once); prints its id.
* ``campaigns [ID]`` - list campaigns (run counts per state), or one with its
  spec.
* ``runs ID [--state S]`` - the campaign's runs in execution order.
* ``submit ID [-n K] [--wait [--timeout S]]`` - submit the next ``K`` planned
  runs as pinned, lowest-priority jobs (idempotent per run); ``--wait`` polls
  until those runs ended and exits ``126`` when any failed.

Everything but ``plan --node`` needs the controller and an **admin** login
(campaigns are job submission at scale; docs/THREAT_MODEL.md). ``--json``
documents: docs/CLI.md section 3.
"""

from __future__ import annotations

import functools
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Final

import typer

from meshforge.cli import render
from meshforge.cli import shared as cli
from meshforge.cli.bench_commands import bench_app
from meshforge.cli.exits import CliExit

if TYPE_CHECKING:
    from meshforge.bench.campaign import Expansion, NodeRef
    from meshforge.cli.client import ControllerClient

__all__ = ["plan_document"]

MAX_SPEC_FILE_BYTES: Final = 256 * 1024
WAIT_POLL_S: Final = 2.0
RUN_STATES: Final = ("planned", "submitted", "done", "failed", "skipped")
_PLAN_COLUMNS: Final = ("SEQ", "REP", "FAMILY", "PARAMS", "STRATEGY", "NODE", "ORIGIN")
_RUN_COLUMNS: Final = (
    "SEQ",
    "STATE",
    "REP",
    "FAMILY",
    "STRATEGY",
    "NODE",
    "JOB",
    "DETAIL",
)


def _read_spec(path: Path) -> str:
    try:
        if path.stat().st_size > MAX_SPEC_FILE_BYTES:
            cli._die(
                f"{path}: larger than {MAX_SPEC_FILE_BYTES} bytes", CliExit.USAGE_ERROR
            )
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        cli._die(f"{path}: not UTF-8 text", CliExit.USAGE_ERROR)
    except OSError as exc:
        cli._die(
            f"cannot read {path}: {exc.strerror or type(exc).__name__}",
            CliExit.USAGE_ERROR,
        )


def _live_nodes(client: ControllerClient) -> list[NodeRef]:
    from meshforge.bench.campaign import NodeRef
    from meshforge.common.bench_conditions import advertised_kinds

    def threads(hardware: Mapping[str, Any]) -> int | None:
        value = hardware.get("cpu_threads") or hardware.get("cpu_cores")
        return value if isinstance(value, int) and value > 0 else None

    payload = cli._fetch(client.nodes)
    out: list[NodeRef] = []
    for n in payload.get("nodes", []):
        if not isinstance(n, Mapping) or n.get("state") == "revoked":
            continue
        hardware = n.get("hardware")
        hw: Mapping[str, Any] = hardware if isinstance(hardware, Mapping) else {}
        tags = hw.get("capabilities")
        out.append(
            NodeRef(
                str(n["node_id"]),
                str(n.get("hostname") or "") or None,
                # V0.9 G4: the same support check the controller makes at create
                conditions=advertised_kinds(
                    [str(t) for t in tags] if isinstance(tags, list) else []
                ),
                threads=threads(hw),
            )
        )
    return out


def _short_params(params: Mapping[str, Any]) -> str:
    return " ".join(f"{k}={v}" for k, v in sorted(params.items()))


def plan_document(
    expansion: Expansion, *, name: str, digest: str, repetitions: int, show: int
) -> dict[str, Any]:
    """The ``bench plan --json`` document (without ``schema_version``)."""
    from meshforge.bench.campaign import run_dict

    return {
        "name": name,
        "spec_digest": digest,
        "runs": len(expansion.runs),
        "groups": expansion.groups,
        "configs": expansion.configs,
        "repetitions": repetitions,
        "dropped": expansion.dropped,
        "duplicates": expansion.duplicates,
        "dropped_detail": list(expansion.problems),
        "estimated_hours": expansion.estimated_hours,
        "nodes": sorted({r.node_id for r in expansion.runs}),
        "first": [run_dict(r) for r in expansion.runs[:show]],
    }


@bench_app.command()
def plan(
    spec_file: Annotated[Path, typer.Argument(help="The campaign spec (YAML).")],
    node: Annotated[
        list[str] | None,
        typer.Option(
            "--node",
            help="Plan against these node ids instead of asking the controller "
            "(repeat; works offline).",
        ),
    ] = None,
    show: Annotated[int, typer.Option("--show", min=0, help="Runs to list.")] = 20,
    json_output: cli.JsonOption = False,
) -> None:
    """Expand a campaign spec and print its runs and cost - runs nothing."""
    from meshforge.bench.campaign import (
        CampaignSpecError,
        NodeRef,
        expand,
        load_spec,
        spec_digest,
    )

    text = _read_spec(spec_file)
    try:
        spec = load_spec(text)
    except CampaignSpecError as exc:
        cli._die(f"{spec_file}: {exc.message}", CliExit.USAGE_ERROR)
    if node:
        nodes = [NodeRef(str(cli._checked_node_id(n))) for n in node]
    else:
        settings = cli._settings()
        with cli._client(settings) as client:
            nodes = _live_nodes(client)
    try:
        expansion = expand(spec, nodes)
    except CampaignSpecError as exc:
        cli._die(f"{spec_file}: {exc.message}", CliExit.USAGE_ERROR)
    document = plan_document(
        expansion,
        name=spec.name,
        digest=spec_digest(spec),
        repetitions=spec.repetitions,
        show=show,
    )
    if json_output:
        cli._echo_json(document)
        return
    if show:
        table = render.table(
            *_PLAN_COLUMNS, title=f"Campaign {spec.name}: the first runs"
        )
        for run in document["first"]:
            table.add_row(
                render.cell(run["seq"]),
                render.cell(run["repetition"]),
                render.cell(run["family"]),
                render.cell(_short_params(run["params"])),
                render.cell(run["strategy"]),
                render.cell(run["node_id"]),
                render.cell(run["origin_node_id"]),
            )
        render.show(table)
    render.line(
        f"{document['runs']} runs = {document['configs']} configurations x "
        f"{document['repetitions']} repetitions ({document['groups']} parameter "
        f"groups, {len(document['nodes'])} nodes); about "
        f"{document['estimated_hours']:.2f} h on the reference mid laptop"
    )
    if expansion.dropped or expansion.duplicates:
        render.note(
            f"{expansion.dropped} parameter point(s) dropped (outside the family's "
            f"limits), {expansion.duplicates} duplicate(s) merged"
        )


def _campaign_line(campaign: Mapping[str, Any]) -> str:
    counts = campaign.get("counts", {})
    shown = ", ".join(f"{k} {counts.get(k, 0)}" for k in RUN_STATES)
    return render.flat(
        f"{campaign['campaign_id']} {campaign['name']} ({campaign['state']}): "
        f"{campaign['run_count']} runs - {shown}"
    )


@bench_app.command()
def create(
    spec_file: Annotated[Path, typer.Argument(help="The campaign spec (YAML).")],
    json_output: cli.JsonOption = False,
) -> None:
    """Store a campaign on the controller (validated and expanded there; admin).
    Prints the campaign id."""
    text = _read_spec(spec_file)
    settings = cli._settings()
    with cli._client(settings) as client:
        created = cli._fetch(lambda: client.bench_create(text))
    if json_output:
        cli._echo_json(created)
        return
    render.line(str(created["campaign_id"]))
    summary = created.get("summary", {})
    render.note(
        f"{created['run_count']} runs ({summary.get('configs', '?')} configurations), "
        f"about {float(summary.get('estimated_hours', 0)):.2f} h; submit with "
        f"`compute bench submit {created['campaign_id']} -n K`"
    )


@bench_app.command()
def campaigns(
    campaign_id: Annotated[
        str | None, typer.Argument(help="One campaign (shown with its spec).")
    ] = None,
    json_output: cli.JsonOption = False,
) -> None:
    """List benchmark campaigns, or show one (admin)."""
    checked = None if campaign_id is None else cli._checked_campaign_id(campaign_id)
    settings = cli._settings()
    with cli._client(settings) as client:
        if checked is not None:
            one = cli._fetch(lambda: client.bench_campaign(checked))
            if json_output:
                cli._echo_json(one)
                return
            render.line(_campaign_line(one))
            render.line(render.scrub(str(one.get("spec", "")).rstrip()))
            return
        many = cli._fetch(client.bench_campaigns)
    if json_output:
        cli._echo_json(many)
        return
    listed = many.get("campaigns", [])
    if not listed:
        render.note("no campaigns yet (`compute bench create SPEC`)")
    for campaign in listed:
        render.line(_campaign_line(campaign))


def _runs_page(
    client: ControllerClient, campaign_id: str, state: str | None, after: int
) -> Mapping[str, Any]:
    return client.bench_runs(campaign_id, state=state, after=after)


def _all_runs(
    client: ControllerClient, campaign_id: str, state: str | None
) -> list[Mapping[str, Any]]:
    out: list[Mapping[str, Any]] = []
    after = 0
    while True:
        page = cli._fetch(
            functools.partial(_runs_page, client, campaign_id, state, after)
        )
        out.extend(page.get("runs", []))
        following = page.get("next_after")
        if not isinstance(following, int) or following <= after:
            return out
        after = following


@bench_app.command()
def runs(
    campaign_id: Annotated[str, typer.Argument(help="The campaign (camp_...).")],
    state: Annotated[
        str | None,
        typer.Option("--state", help="planned / submitted / done / failed / skipped."),
    ] = None,
    json_output: cli.JsonOption = False,
) -> None:
    """A campaign's runs in execution order (admin)."""
    checked = cli._checked_campaign_id(campaign_id)
    if state is not None and state not in RUN_STATES:
        cli._die(
            "--state: planned, submitted, done, failed or skipped", CliExit.USAGE_ERROR
        )
    settings = cli._settings()
    with cli._client(settings) as client:
        rows = _all_runs(client, checked, state)
    if json_output:
        cli._echo_json({"campaign_id": checked, "runs": rows, "count": len(rows)})
        return
    table = render.table(*_RUN_COLUMNS, title=f"Campaign {checked}: runs")
    for run in rows:
        table.add_row(*(render.cell(run.get(key)) for key in _RUN_KEYS))
    render.show(table)


_RUN_KEYS: Final = (
    "seq",
    "state",
    "repetition",
    "family",
    "strategy",
    "node_id",
    "job_id",
    "detail",
)


def _wait_runs(
    client: ControllerClient,
    campaign_id: str,
    run_ids: Sequence[str],
    timeout_s: float | None,
) -> dict[str, str]:
    """Poll (monotonic clock) until every run of ``run_ids`` left
    ``submitted``; run id -> its final state."""
    wanted = set(run_ids)
    deadline = None if timeout_s is None else time.monotonic() + timeout_s
    while True:
        states = {
            str(r["run_id"]): str(r["state"])
            for r in _all_runs(client, campaign_id, None)
            if r.get("run_id") in wanted
        }
        running = sum(1 for s in states.values() if s == "submitted")
        if not running:
            return states
        if deadline is not None and time.monotonic() >= deadline:
            cli._die(
                f"{running} run(s) still running after {timeout_s:g} s",
                CliExit.FAILURE,
            )
        time.sleep(WAIT_POLL_S)


@bench_app.command()
def submit(
    campaign_id: Annotated[str, typer.Argument(help="The campaign (camp_...).")],
    count: Annotated[
        int, typer.Option("-n", "--count", min=1, max=500, help="Runs to submit.")
    ] = 1,
    wait: Annotated[
        bool, typer.Option("--wait", help="Wait until the submitted runs ended.")
    ] = False,
    timeout_s: Annotated[
        float | None,
        typer.Option("--timeout", min=1.0, help="With --wait: give up after S s."),
    ] = None,
    json_output: cli.JsonOption = False,
) -> None:
    """Submit the campaign's next K planned runs as pinned lowest-priority jobs
    (admin; idempotent per run)."""
    checked = cli._checked_campaign_id(campaign_id)
    settings = cli._settings()
    with cli._client(settings) as client:
        result = cli._fetch(lambda: client.bench_submit(checked, count))
        submitted = [
            str(o["run_id"])
            for o in result.get("outcomes", [])
            if o.get("outcome") == "submitted"
        ]
        ended = (
            _wait_runs(client, checked, submitted, timeout_s)
            if wait and submitted
            else {}
        )
    failed = sorted(run for run, state in ended.items() if state == "failed")
    if json_output:
        cli._echo_json({**result, **({"ended": ended} if wait else {})})
    else:
        render.line(
            f"submitted {result.get('submitted', 0)}, deferred "
            f"{result.get('deferred', 0)}, skipped {result.get('skipped', 0)}"
        )
        for outcome in result.get("outcomes", []):
            if outcome.get("outcome") in ("deferred", "skipped"):
                render.note(
                    f"run {outcome.get('seq')} {outcome.get('outcome')}: "
                    f"{outcome.get('detail') or ''}"
                )
        if wait and submitted:
            render.line(f"ended: {len(ended) - len(failed)} done, {len(failed)} failed")
    if failed:
        raise typer.Exit(CliExit.JOB_FAILED)
