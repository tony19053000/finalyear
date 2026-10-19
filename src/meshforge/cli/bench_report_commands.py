"""``compute bench report CAMPAIGN_ID [--json] [--strict]`` - data quality report (V0.9 G5c, D220).

Asks ``GET /v1/bench/campaigns/{id}/quality`` (admin) and prints the campaign
summary (runs by state, infrastructure failure rate, pre-emptions, condition
and workload failures, the criterion verdicts), then one row per
configuration - **flagged ones first** (too few repetitions, CV above the
threshold, a required field missing), then the rest; both in
``(group, config)`` order. ``--json`` prints the API's document plus
``schema_version`` (docs/CLI.md section 3).

Exit ``0`` whatever the report says; with ``--strict`` exit ``1``
(``FAILURE``) unless both criteria are **met** - a failed criterion and a
campaign still running (verdicts pending) both fail, so a script gating on
the report never passes on an unfinished campaign.

Every number's definition: docs/TELEMETRY.md "Quality report".
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Annotated, Any, Final

import typer

from meshforge.cli import render
from meshforge.cli import shared as cli
from meshforge.cli.bench_commands import bench_app
from meshforge.cli.exits import CliExit

__all__ = ["config_rows", "criteria_met", "summary_lines"]

_COLUMNS: Final = (
    "GROUP",
    "CONFIG",
    "N",
    "MEDIAN ms",
    "CV",
    "CI ms",
    "OUTLIERS",
    "MISSING",
    "FLAGS",
)


def _pct(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return "-"
    return f"{100.0 * value:.1f} %"


def _verdict(value: object) -> str:
    if value is None:
        return "pending"
    return "met" if value else "NOT met"


def _counts(values: object) -> str:
    if not isinstance(values, Mapping) or not values:
        return "none"
    return ", ".join(f"{k} {v}" for k, v in values.items())


def summary_lines(doc: Mapping[str, Any]) -> list[str]:
    """The summary block (pure; tests read it)."""
    s = doc.get("summary") or {}
    runs = s.get("runs_by_state") or {}
    lines = [
        f"{doc.get('campaign_id')}: {s.get('campaign_state', '?')}"
        + (" (still running: verdicts pending)" if s.get("pending") else ""),
        "runs: " + ", ".join(f"{k} {v}" for k, v in runs.items()),
        f"records: {s.get('records', 0)} "
        f"(fillers / warm-ups apart: {_counts(s.get('bench_role_records'))})",
        f"measured (ok, used by the labels): {s.get('measured_records', 0)}",
        f"infrastructure failures: {_pct(s.get('infra_failure_rate'))} "
        f"({s.get('infra_failures', 0)}; {s.get('owner_preempted', 0)} stopped by "
        "the owner guard, not counted)",
        f"condition failures: {_counts(s.get('condition_failures'))}",
        f"workload failures: {_counts(s.get('workload_failures'))}",
        f"ok but rejected by the label filter: {_counts(s.get('rejected_ok'))}",
        f"other failures: {_counts(s.get('other_failures'))}",
        f"criterion 1 (infra failures < 2 %): {_verdict(s.get('crit1_infra_ok'))}",
        "criterion 3 (every config >= 3 repetitions with a CV): "
        + _verdict(s.get("crit3_reps_ok")),
        f"configs: {s.get('configs', 0)}, flagged {s.get('flagged_configs', 0)} "
        f"(CV threshold {float(s.get('cv_threshold') or 0):g})",
    ]
    return [render.flat(line) for line in lines]


def _flags(config: Mapping[str, Any]) -> str:
    out = []
    if config.get("reps_flag"):
        out.append("reps")
    if config.get("cv_flag"):
        out.append("cv")
    missing = config.get("missing") or {}
    if any(missing.values()):
        out.append("missing")
    if config.get("state") == "unsupported":
        out.append("unsupported")
    return ",".join(out) or "-"


def _num(value: object, fmt: str = "{:.1f}") -> str:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return "-"
    return fmt.format(value)


def config_rows(doc: Mapping[str, Any]) -> list[tuple[str, ...]]:
    """One row per configuration, flagged first (pure; tests read it)."""
    configs: Sequence[Mapping[str, Any]] = doc.get("configs") or []
    ordered = [c for c in configs if c.get("flagged")] + [
        c for c in configs if not c.get("flagged")
    ]
    rows: list[tuple[str, ...]] = []
    for c in ordered:
        total = c.get("total_ms") or {}
        missing = c.get("missing") or {}
        rows.append(
            (
                str(c.get("group_id", "")),
                f"{c.get('strategy', '?')} @ {c.get('node_id', '?')}",
                str(c.get("n", 0)),
                _num(total.get("median")),
                _num(total.get("cv"), "{:.3f}"),
                (
                    f"{_num(total.get('ci_low'))}..{_num(total.get('ci_high'))}"
                    if total
                    else "-"
                ),
                str(len(c.get("outlier_attempt_ids") or ())),
                ", ".join(f"{k} {v}" for k, v in missing.items() if v) or "-",
                _flags(c),
            )
        )
    return rows


def criteria_met(doc: Mapping[str, Any]) -> bool:
    s = doc.get("summary") or {}
    return s.get("crit1_infra_ok") is True and s.get("crit3_reps_ok") is True


@bench_app.command()
def report(
    campaign_id: Annotated[str, typer.Argument(help="The campaign (camp_...).")],
    strict: Annotated[
        bool,
        typer.Option(
            "--strict",
            help="Exit 1 unless both criteria are met (pending counts as not met).",
        ),
    ] = False,
    json_output: cli.JsonOption = False,
) -> None:
    """Data quality report of a campaign: repetitions, CV, outliers, missing
    fields, failure rates, criterion verdicts (admin)."""
    checked = cli._checked_campaign_id(campaign_id)
    settings = cli._settings()
    with cli._client(settings) as client:
        document = cli._fetch(lambda: client.bench_quality(checked))
    if json_output:
        cli._echo_json(document)
    else:
        for line in summary_lines(document):
            render.line(line)
        table = render.table(*_COLUMNS, title=f"Campaign {checked}: configurations")
        for row in config_rows(document):
            table.add_row(*(render.cell(v) for v in row))
        render.show(table)
    if strict and not criteria_met(document):
        raise typer.Exit(CliExit.FAILURE)
