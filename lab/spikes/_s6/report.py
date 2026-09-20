"""The S6-specific markdown block appended after the harness Results table:
verdicts per laptop, the cost table, the V0.5 criterion 5 calibration lines,
the Docker facts, and every diagnosis with its fix.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

from spikes._harness import MIN_VALID_REPS
from spikes._harness.report import cell
from spikes._s6.verdict import (
    COST_ROWS,
    TARGET_START_OVERHEAD_S,
    HostVerdict,
    host_verdicts,
)

_FACT_COLUMNS: Final[tuple[tuple[str, str], ...]] = (
    ("versions", "docker client / server"),
    ("daemon_os", "daemon OS"),
    ("kernel", "kernel"),
    ("runtimes", "runtimes"),
    ("cgroup", "cgroup"),
    ("driver", "storage driver"),
    ("wsl_host", "tool ran in WSL"),
    ("host_gpu", "host GPU (nvidia-smi)"),
)


def _row(cells: Sequence[object]) -> str:
    return "| " + " | ".join(cell(c) for c in cells) + " |\n"


def verdict_table(rows: Sequence[HostVerdict]) -> str:
    head = (
        _row(
            [
                "Laptop",
                "OS",
                "docker",
                "gpu-in-container",
                "torch-cuda-in-container",
                "runtime recommendation (ADR-0004)",
            ]
        )
        + "|---|---|---|---|---|---|\n"
    )
    body = "".join(
        _row(
            [
                f"{r.host}{' (quick run)' if r.quick else ''}",
                r.os_text,
                r.docker.cell(),
                r.gpu_in_container.cell(),
                r.torch_cuda.cell(),
                r.recommendation.cell(),
            ]
        )
        for r in rows
    )
    return head + (body or "| (no results) | | | | | |\n")


def cost_table(rows: Sequence[HostVerdict]) -> str:
    hosts = [r.host for r in rows]
    head = (
        _row(["Cost (mean ± stdev (n))", *hosts]) + "|---|" + "---|" * len(hosts) + "\n"
    )
    body = "".join(
        _row([label, *(r.costs.get(label, "TBD") for r in rows)])
        for _c, _m, label in COST_ROWS
    )
    return head + body


def facts_table(rows: Sequence[HostVerdict]) -> str:
    head = _row(["Laptop", *(title for _k, title in _FACT_COLUMNS)])
    head += "|---|" + "---|" * len(_FACT_COLUMNS) + "\n"
    body = "".join(
        _row([r.host, *(r.docker_facts.get(key, "?") for key, _t in _FACT_COLUMNS)])
        for r in rows
    )
    return head + (body or "| (no results) |" + " |" * len(_FACT_COLUMNS) + "\n")


def fixes_list(rows: Sequence[HostVerdict]) -> str:
    items = [f"- {r.host}: {cell(line)}" for r in rows for line in r.fixes()]
    return "\n".join(items) + "\n" if items else "- none\n"


def render_environments(
    envelopes: Sequence[Mapping[str, Any]], labels: Mapping[str, str] | None = None
) -> str:
    rows = host_verdicts(envelopes, labels)
    parts = ["### Environment verdicts per laptop\n"]
    if not envelopes:
        parts.append("No S6 result files found: every cell is TBD.\n")
    parts.append(verdict_table(rows))
    parts.append("### Environment costs per laptop\n")
    parts.append(cost_table(rows))
    parts.append(
        f"### Calibration for V0.5 criterion 5 (target ≤ {TARGET_START_OVERHEAD_S:g} s)\n"
    )
    parts.append(
        "\n".join(f"- {cell(r.calibration)}" for r in rows) + "\n"
        if rows
        else "- TBD\n"
    )
    parts.append("### Docker facts\n")
    parts.append(facts_table(rows))
    parts.append("### Diagnoses and fixes\n")
    parts.append(fixes_list(rows))
    parts.append(
        "Cells: `usable`/`yes` only from ≥ "
        f"{MIN_VALID_REPS} clean measured repetitions · `unusable`/`no — <failure_type>: "
        "<message> [fix: …]` when measured and failed · `flaky k/n` · "
        f"`INVALID (n<{MIN_VALID_REPS})` when it worked with too few repetitions · "
        "`skipped` when an operator flag skipped the case · `n/a` when the host has no "
        "NVIDIA GPU · `INVALID (malformed)` when the result file's case entry is not a "
        "usable object · `TBD` when nobody ran it. The recommendation is `docker` only "
        "from `usable` plus (`yes` or no GPU on the host), never from an INVALID row. "
        "The docker-gpu-torch cold pull is one informational number in its `attrs` "
        "(n=1) and is never a row here.\n"
    )
    return "\n".join(parts)
