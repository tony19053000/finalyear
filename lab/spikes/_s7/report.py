"""Aggregate every laptop's S7 result into one comparable table per metric.

Layout of the generated markdown (appended to the standard report by
``s7_hardware.py report``):

* one table per metric (``gflops``, ``triad_gb_s``, ``tflops``, ...) with one
  row per case and one column per laptop,
* a ranking line per headline metric (strongest laptop first) - the planner
  ranks nodes with exactly these numbers,
* a backend/conditions table, because a torch number and a ``python_fallback``
  number must never be read as the same thing.

Cells: ``mean ± stdev`` (n >= 5), ``INVALID (n=<n>)`` when a laptop ran too few
repetitions, ``TBD`` when that laptop has no result for the row at all, and
``err: <failure_type>`` when the case exists but every repetition failed.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Final

from spikes._harness import MIN_VALID_REPS
from spikes._harness.envelope import host_label
from spikes._harness.report import fmt
from spikes._harness.reps import JSON
from spikes._harness.stats import Summary

#: Metrics that rank a laptop (shown with a ranking line, in this order).
HEADLINE: Final = (
    "gflops",
    "tflops",
    "triad_gb_s",
    "copy_gb_s",
    "device_copy_gb_s",
    "python_mflops",
)
TBD: Final = "TBD"


def is_score(metric: str) -> bool:
    """Durations (``elapsed_s``, ``copy_s``, ``event_s``) are kept in the JSON
    but are not scores; rates (``*_gb_s``, ``*flops``) are."""
    return not metric.endswith("_s") or metric.endswith("_gb_s")


def host_names(
    envelopes: Sequence[Mapping[str, JSON]], labels: Mapping[str, str] | None = None
) -> list[str]:
    """Display name per laptop: ``labels`` (hostname -> short name) wins.

    Hosts that appear only in ``labels`` (no result file yet) are kept, so the
    table shows them as ``TBD`` instead of pretending they do not exist.
    """
    names = {str(v) for v in (labels or {}).values()}
    for doc in envelopes:
        names.add(display_name(doc, labels))
    return sorted(names)


def display_name(doc: Mapping[str, JSON], labels: Mapping[str, str] | None) -> str:
    """``labels`` (hostname -> short name) wins; the match is case-insensitive
    because hostnames are (Windows reports ``LAPTOP-1``, the file says
    ``laptop-1``). Falls back to the label stored in the result."""
    host = doc.get("host") or {}
    hostname = str(host.get("hostname", "")) if isinstance(host, dict) else ""
    folded = {k.casefold(): v for k, v in (labels or {}).items()}
    return folded.get(hostname.casefold()) or host_label(doc)


def _summary(case: Mapping[str, JSON], metric: str) -> Summary | None:
    raw = (case.get("summary") or {}).get(metric)
    if raw is None:
        return None
    try:
        return Summary.from_json(raw)
    except (TypeError, ValueError, AttributeError):
        return None


def collect(
    envelopes: Sequence[Mapping[str, JSON]], labels: Mapping[str, str] | None = None
) -> dict[str, dict[str, dict[str, Summary]]]:
    """``{metric: {case_id: {host: Summary}}}`` over every result file."""
    table: dict[str, dict[str, dict[str, Summary]]] = {}
    for doc in envelopes:
        host = display_name(doc, labels)
        for case_id, case in sorted((doc.get("cases") or {}).items()):
            for metric in sorted(case.get("summary") or {}):
                if not is_score(metric):
                    continue
                summary = _summary(case, metric)
                if summary is None:
                    continue
                table.setdefault(metric, {}).setdefault(case_id, {})[host] = summary
    return table


def case_errors(
    envelopes: Sequence[Mapping[str, JSON]], labels: Mapping[str, str] | None = None
) -> dict[str, dict[str, str]]:
    """``{case_id: {host: failure_type}}`` for cases whose repetitions failed."""
    out: dict[str, dict[str, str]] = {}
    for doc in envelopes:
        host = display_name(doc, labels)
        for case_id, case in (doc.get("cases") or {}).items():
            if case.get("samples"):
                continue
            errors = case.get("errors") or []
            if errors:
                out.setdefault(case_id, {})[host] = str(
                    errors[0].get("failure_type", "error")
                )
    return out


def cell(summary: Summary | None, error: str | None = None) -> str:
    """One table cell: value, INVALID, err: ... or TBD."""
    if summary is None or summary.n == 0:
        return f"err: {error}" if error else TBD
    text = f"{fmt(summary.mean, 2)} ± {fmt(summary.stdev, 2)}"
    if summary.n < MIN_VALID_REPS:
        return f"**INVALID (n={summary.n})** {text}"
    return text


def unit_of(envelopes: Sequence[Mapping[str, JSON]], metric: str) -> str:
    for doc in envelopes:
        for case in (doc.get("cases") or {}).values():
            unit = (case.get("metrics") or {}).get(metric)
            if unit:
                return str(unit)
    return ""


def ranking(rows: Mapping[str, Mapping[str, Summary]], hosts: Sequence[str]) -> str:
    """Laptops ordered by their **best valid mean over the cases** of one metric.

    The case that produced the number is named next to it, so it is explicit
    that e.g. the ``gflops`` ranking is by ``cpu-float32-all`` (all cores,
    float32), never by the 1-thread or float64 rows, and the ``tflops`` ranking
    by the fastest dtype the card supports.
    """
    best: dict[str, tuple[float, str]] = {}
    for case_id in sorted(rows):
        for host, summary in rows[case_id].items():
            if summary.n < MIN_VALID_REPS or summary.mean is None:
                continue
            current = best.get(host)
            if current is None or summary.mean > current[0]:
                best[host] = (summary.mean, case_id)
    ordered = sorted(best.items(), key=lambda kv: (-kv[1][0], kv[0]))
    missing = [h for h in hosts if h not in best]
    text = (
        " > ".join(f"{h} ({fmt(v, 2)}, `{case}`)" for h, (v, case) in ordered)
        or "no valid results"
    )
    if missing:
        text += f" · no valid result: {', '.join(missing)}"
    return text


def metric_table(
    metric: str,
    rows: Mapping[str, Mapping[str, Summary]],
    hosts: Sequence[str],
    unit: str,
    errors: Mapping[str, Mapping[str, str]],
) -> str:
    head = f"| Case | {' | '.join(hosts)} |\n"
    head += "|---|" + "---|" * len(hosts) + "\n"
    body = ""
    for case_id in sorted(rows):
        cells = [
            cell(rows[case_id].get(h), errors.get(case_id, {}).get(h)) for h in hosts
        ]
        body += f"| `{case_id}` | {' | '.join(cells)} |\n"
    for case_id in sorted(set(errors) - set(rows)):
        cells = [cell(None, errors.get(case_id, {}).get(h)) for h in hosts]
        body += f"| `{case_id}` | {' | '.join(cells)} |\n"
    title = f"#### {metric} ({unit}) — mean ± stdev\n\n"
    return title + head + body + f"\nRanking: {ranking(rows, hosts)}\n"


def conditions_table(
    envelopes: Sequence[Mapping[str, JSON]], labels: Mapping[str, str] | None = None
) -> str:
    head = (
        "| Laptop | run | backends | GPU | power | governor / plan | throttling |\n"
        "|---|---|---|---|---|---|---|\n"
    )
    rows = ""
    for doc in sorted(envelopes, key=lambda d: display_name(d, labels)):
        extra = doc.get("extra") or {}
        caps = extra.get("capabilities") or {}
        gpu = extra.get("gpu") or {}
        devices = gpu.get("devices") or []
        gpu_text = (
            ", ".join(str(d.get("device_name")) for d in devices)
            if gpu.get("available")
            else f"none ({gpu.get('failure_type', 'unavailable')})"
        )
        cpu_env = ((extra.get("environment") or {}).get("before") or {}).get(
            "cpu"
        ) or {}
        governor = cpu_env.get("distinct") or cpu_env.get("plan") or "?"
        if isinstance(governor, list):
            governor = ", ".join(str(g) for g in governor) or "?"
        stability = extra.get("stability") or {}
        throttled = sorted(
            case
            for case, value in stability.items()
            if isinstance(value, dict) and value.get("thermal_throttling_suspected")
        )
        battery = extra.get("on_battery")
        power = "unknown"
        if battery:
            power = "BATTERY — not comparable"
        elif battery is False:
            power = "AC"
        rows += (
            f"| {display_name(doc, labels)} "
            f"| {'quick (smoke)' if extra.get('quick') else 'full'} "
            f"| torch {caps.get('torch') or '—'} / numpy {caps.get('numpy') or '—'} "
            f"| {gpu_text} "
            f"| {power} "
            f"| {governor} "
            f"| {', '.join(throttled) if throttled else 'no'} |\n"
        )
    return head + (rows or "| (no results) | | | | | | |\n")


def render_scoreboard(
    envelopes: Sequence[Mapping[str, JSON]], labels: Mapping[str, str] | None = None
) -> str:
    """The full S7 markdown block: one table per metric, then the conditions."""
    hosts = host_names(envelopes, labels)
    table = collect(envelopes, labels)
    errors = case_errors(envelopes, labels)
    ordered = [m for m in HEADLINE if m in table] + sorted(
        m for m in table if m not in HEADLINE
    )
    parts = ["### Hardware scores per laptop\n"]
    if not envelopes:
        parts.append("No S7 result files found: every cell is TBD.\n")
    parts.extend(
        metric_table(metric, table[metric], hosts, unit_of(envelopes, metric), errors)
        for metric in ordered
    )
    if errors and not ordered:
        parts.append(
            "Every case failed on every laptop; the failure types are listed in "
            "the notes below.\n"
        )
    parts.append("### Run conditions\n")
    parts.append(conditions_table(envelopes, labels))
    parts.append(
        "\nCells: `mean ± stdev` · **INVALID** when n < "
        f"{MIN_VALID_REPS} · `TBD` when that laptop has no result for the row · "
        "`err: <failure_type>` when every repetition failed.\n"
        "`python_mflops` is the interpreter-bound fallback (no torch/numpy on "
        "that laptop) and must never be compared with `gflops`.\n"
    )
    return "\n".join(parts)
