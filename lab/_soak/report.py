"""The soak verdict as a markdown report (pure; the JSON is the evidence)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def _cell(value: object) -> str:
    if value is None:
        return "-"
    return str(value).replace("|", "\\|").replace("\n", " ")


def render(verdict: Mapping[str, Any], *, json_name: str) -> str:
    run = verdict["run"]
    laptops = ", ".join(
        f"{t['label']} ({t['os']}, {'+'.join(t['roles'])})" for t in run["targets"]
    )
    lines = [
        f"# V1.0 soak - {run['hours']} h{' (compressed dry run)' if run.get('compressed') else ''}"
        f" - **{verdict['verdict']}**",
        "",
        f"> Generated {verdict['generated_at']} by `lab/soak.py report` from "
        f"`{json_name}` (the JSON is the evidence; this file is its rendering).",
        "",
        "## Run",
        "",
        "| Field | Value |",
        "|---|---|",
        f"| Seed / nonce | {run['seed']} / {run['nonce']} |",
        f"| Start (t0) / end | {run['t0']} / {run['end']} |",
        f"| Load + chaos / quiet windows / idle tail | {run['hours']} h / "
        f"{run['quiet_h']} h each / {run['tail_h']} h |",
        f"| Link | {run.get('link')} |",
        f"| Laptops | {laptops} |",
        f"| Git / package | {_cell((verdict.get('git') or {}).get('commit'))} / "
        f"{_cell(verdict.get('package_version'))} |",
        "",
        "## Rules (D410, D411)",
        "",
        "| Rule | Id | Verdict | Summary |",
        "|---|---|---|---|",
    ]
    lines.extend(
        f"| {r['rule']} | `{r['id']}` | **{r['verdict']}** | {_cell(r['summary'])} |"
        for r in verdict["rules"]
    )
    lines += ["", "## Details", ""]
    for rule in verdict["rules"]:
        lines += _details(rule)
    if verdict.get("problems"):
        lines += ["## Problems reading the inputs", ""]
        lines += [f"- {_cell(p)}" for p in verdict["problems"]]
        lines.append("")
    return "\n".join(lines) + "\n"


def _details(rule: Mapping[str, Any]) -> list[str]:
    out = [f"### {rule['id']} - {rule['verdict']}", ""]
    details = rule["details"]
    rid = rule["id"]
    if rid == "lost_jobs" and details:
        out.append(f"States: `{details.get('states')}`; outputs content-checked: "
                   f"{details.get('outputs_checked')}.")  # fmt: skip
        for key in ("lost", "late", "failed_explained", "duplicate_submissions",
                    "unresolved_submissions"):  # fmt: skip
            items = details.get(key) or []
            out.append(f"- {key}: {len(items)}")
            out += [f"  - `{_cell(i)}`" for i in items[:50]]
    elif rid in ("memory", "handles", "disk", "coverage") and isinstance(details, list):
        if details:
            keys = [k for k in details[0] if k != "note"]
            out.append("| " + " | ".join(keys) + " |")
            out.append("|" + "---|" * len(keys))
            out += [
                "| " + " | ".join(_cell(r.get(k)) for k in keys) + " |" for r in details
            ]
    elif rid == "chaos":
        out.append(
            f"By kind: `{details.get('by_kind')}`; by outcome: `{details.get('by_outcome')}`."
        )
        if details.get("proof"):
            out.append(f"Proof (disk_full attempts in the window): `{details['proof']}`; "
                       f"not proven: `{details.get('unproven')}`.")  # fmt: skip
        out.append("")
        out.append("| id | kind | target | started | effect until | outcome | detail |")
        out.append("|---|---|---|---|---|---|---|")
        out += [
            f"| {a['id']} | {a['kind']} | {a['target']} | {a['started_at']} | "
            f"{a['effect_until']} | {a['outcome']} | {_cell(a.get('job_id') or a.get('detail'))} |"
            for a in details.get("actions") or []
        ]
    elif rid in ("crashes", "heartbeat"):
        for key, items in details.items():
            if isinstance(items, list):
                out.append(f"- {key}: {len(items)}")
                out += [f"  - `{_cell(i)}`" for i in items[:50]]
            else:
                out.append(f"- {key}: {items}")
    else:
        out.append(f"```\n{details}\n```")
    out.append("")
    return out
