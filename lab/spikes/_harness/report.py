"""Generate a lab report from ``lab/reports/TEMPLATE.md`` and result envelopes.

Filled in automatically: title, header (file, task, spike, commit), the
"Machines & conditions" host rows, the raw-output location, ``n``, the Results
table (one row per case x metric, rows with n < 5 marked INVALID), and the list
of failed repetitions / invalid rows. Goal, conclusion and decisions stay for a
human or agent to write. Spikes may append extra markdown (e.g. the S1 matrices)
right after the Results table. If the template no longer contains a placeholder
this code relies on, :class:`TemplateError` is raised instead of silently
producing a half-filled report.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from spikes._harness import MIN_VALID_REPS
from spikes._harness.envelope import LAB_DIR, host_label
from spikes._harness.reps import JSON
from spikes._harness.stats import Summary

TEMPLATE_PATH: Final = LAB_DIR / "reports" / "TEMPLATE.md"
LINK_NAMES: Final = {"wifi": "Wi-Fi", "ethernet": "Ethernet"}
LINK_NOT_APPLICABLE: Final = "n/a"

_TITLE: Final = "# <Topic> — <YYYY-MM-DD>"
_FILE: Final = "`lab/reports/YYYY-MM-DD-<topic>.md`"
_TASK: Final = "Roadmap task: <0.2.x>"
_SPIKE: Final = "Spike: <S1–S7 or —>"  # noqa: RUF001 (template text)
_AUTHOR: Final = "Author: <agent/person>"
_COMMIT: Final = "Git commit: `<short sha>`"
_HOSTS_ROW: Final = "\n| | | | | |\n"
_RAW: Final = "`lab/reports/raw/YYYY-MM-DD-<topic>/`"
_N: Final = "**n = <≥5>**"
_RESULTS_ROW: Final = "\n| | | | | | | |\n"
_NOTES: Final = "Notes on outliers or failed runs (list them, don't hide them):\n"


class TemplateError(ValueError):
    """The template is missing a placeholder the generator fills."""


@dataclass(frozen=True, slots=True)
class ResultRow:
    case: str
    metric: str
    unit: str
    summary: Summary

    @property
    def invalid(self) -> bool:
        return self.summary.n < MIN_VALID_REPS


def fmt(value: float | None, digits: int = 2) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def link_name(link: str | None) -> str:
    """Display name of a link; ``None`` (spike sends no traffic) -> ``n/a``."""
    if link is None:
        return LINK_NOT_APPLICABLE
    return LINK_NAMES.get(link, link)


def cell(text: object) -> str:
    """Make ``text`` safe inside a markdown table cell: a literal ``|`` would
    otherwise start a new column, and a newline would end the row."""
    return str(text).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


_EMPTY_SUMMARY: Final = Summary(0, None, None, None, None, None, None)


def result_rows(envelopes: Sequence[dict[str, JSON]]) -> list[ResultRow]:
    """One row per (envelope, case, metric), in file then case-id order.

    A result file is data written by another machine, possibly hand-edited:
    a case entry that is not an object, or a ``summary``/``metrics`` that is
    not a mapping, is rendered as one **INVALID** row (n = 0) that says so,
    never crashed on and never counted as a measurement.
    """
    rows: list[ResultRow] = []
    for doc in envelopes:
        host = host_label(doc)
        for case_id in sorted(doc["cases"]):
            case = doc["cases"][case_id]
            if not isinstance(case, Mapping):
                rows.append(
                    ResultRow(
                        case=cell(f"[{link_name(doc['link'])}] {host}: {case_id}"),
                        metric=cell(f"malformed case entry ({type(case).__name__})"),
                        unit="",
                        summary=_EMPTY_SUMMARY,
                    )
                )
                continue
            label = case.get("label") or case_id
            units = case.get("metrics")
            if not isinstance(units, Mapping):
                units = {}
            summaries = case.get("summary")
            if not isinstance(summaries, Mapping):
                summaries = {}
            for metric in sorted(summaries):
                try:
                    summary = Summary.from_json(summaries[metric])
                except (TypeError, ValueError, AttributeError):
                    summary = _EMPTY_SUMMARY
                rows.append(
                    ResultRow(
                        case=cell(f"[{link_name(doc['link'])}] {host}: {label}"),
                        metric=cell(metric),
                        unit=cell(units.get(metric) or ""),
                        summary=summary,
                    )
                )
    return rows


def results_table_rows(rows: Sequence[ResultRow]) -> str:
    lines = [
        f"| {_case_cell(r)} | {r.metric} ({r.unit}) | {r.summary.n} | "
        f"{fmt(r.summary.mean)} | {fmt(r.summary.stdev)} | {fmt(r.summary.min)} | "
        f"{fmt(r.summary.max)} |\n"
        for r in rows
    ]
    return "".join(lines) or "| (no results) | | 0 | — | — | — | — |\n"


def _case_cell(row: ResultRow) -> str:
    return f"{row.case} **INVALID (n<{MIN_VALID_REPS})**" if row.invalid else row.case


def n_headline(rows: Sequence[ResultRow]) -> str:
    """Text that replaces the template's ``**n = <≥5>**`` placeholder.

    It must stay true in every case: the bold ``n`` describes the **valid**
    rows only (``summary.n >= MIN_VALID_REPS``), because one skipped case used
    to drag an otherwise healthy table's headline to ``n = 0``. Invalid rows
    are never hidden - they are counted in the same sentence and stay marked
    ``INVALID`` in the table (see :func:`_case_cell`) and in :func:`notes`.
    """
    if not rows:
        return "**n = 0** (no rows)"
    valid = sorted(r.summary.n for r in rows if not r.invalid)
    n_invalid = len(rows) - len(valid)
    if not valid:
        return "**n = 0** (no valid rows)"
    low, high = valid[0], valid[-1]
    count = f"**n = {low}**" if low == high else f"**n = {low}–{high}**"  # noqa: RUF001 (en dash range)
    if not n_invalid:
        return f"{count} (every row)" if low == high else f"{count} (smallest: {low})"
    return (
        f"{count} across the {len(valid)} valid {_rows(len(valid))}; "
        f"{n_invalid} {_rows(n_invalid)} INVALID (n < {MIN_VALID_REPS}), "
        "marked in the table"
    )


def _rows(count: int) -> str:
    return "row" if count == 1 else "rows"


def notes(envelopes: Sequence[dict[str, JSON]], rows: Sequence[ResultRow]) -> str:
    items: list[str] = [
        f"- INVALID: {r.case} / {r.metric}: only n={r.summary.n} successful "
        f"repetitions (< {MIN_VALID_REPS})."
        for r in rows
        if r.invalid
    ]
    for doc in envelopes:
        src = doc.get("_source", host_label(doc))
        items.extend(
            f"- `{src}`: run error {_describe(err)}"
            for err in _records(doc.get("errors"))
        )
        for case_id in sorted(doc["cases"]):
            case = doc["cases"][case_id]
            if not isinstance(case, Mapping):
                items.append(
                    f"- `{src}` {case_id}: malformed case entry "
                    f"({type(case).__name__}, not an object) - not a measurement."
                )
                continue
            items.extend(
                f"- `{src}` {case_id} rep {_field(err, 'rep')} ({_field(err, 'phase')}): "
                f"{_describe(err)}"
                for err in _records(case.get("errors"))
            )
            if case.get("aborted"):
                items.append(f"- `{src}` {case_id}: aborted after a fatal error.")
    return "\n".join(items) + "\n" if items else "- none\n"


def _records(errors: object) -> list[object]:
    """An ``errors`` value as a list, whatever shape a result file gave it.

    A non-list value is kept as one opaque record, never dropped."""
    if errors is None or errors == []:
        return []
    if isinstance(errors, Sequence) and not isinstance(errors, str | bytes):
        return list(errors)
    return [errors]


def _field(err: object, key: str) -> object:
    return err.get(key) if isinstance(err, Mapping) else None


def _describe(err: object) -> str:
    if not isinstance(err, Mapping):
        return cell(f"malformed error record: {err!r}")
    return cell(f"{err.get('failure_type')}: {err.get('message')}")


def _replace_once(text: str, old: str, new: str) -> str:
    if old not in text:
        raise TemplateError(f"template placeholder not found: {old.strip()!r}")
    return text.replace(old, new, 1)


def _commits(envelopes: Sequence[dict[str, JSON]]) -> str:
    seen: list[str] = []
    for doc in envelopes:
        git = doc.get("git") or {}
        commit = str(git.get("commit") or "unknown")
        if git.get("dirty"):
            commit += "+dirty"
        if commit not in seen:
            seen.append(commit)
    return ", ".join(seen) or "unknown"


def _host_rows(envelopes: Sequence[dict[str, JSON]]) -> str:
    seen: dict[tuple[str, str], str] = {}
    for doc in envelopes:
        host = doc["host"]
        label = host_label(doc)
        key = (label, link_name(doc["link"]))
        if key not in seen:
            os_text = f"{host.get('os', '?')} {host.get('os_release', '')}".strip()
            name = (
                label if label == host["hostname"] else f"{label} ({host['hostname']})"
            )
            seen[key] = (
                f"| {cell(name)} | {cell(os_text)} | see inventory JSON | "
                f"{link_name(doc['link'])} (speed: TBD) | TBD |\n"
            )
    return "".join(seen[k] for k in sorted(seen)) or _HOSTS_ROW.lstrip("\n")


def render_report(
    template: str,
    envelopes: Sequence[dict[str, JSON]],
    *,
    topic: str,
    title: str,
    day: str,
    task: str,
    spike: str,
    author: str = "generated by lab/spikes",
    extra_results: str = "",
) -> str:
    """Fill ``template`` (the text of TEMPLATE.md) from ``envelopes``."""
    rows = result_rows(envelopes)
    stem = f"{day}-{topic}"
    text = template
    text = _replace_once(text, _TITLE, f"# {title} — {day}")
    text = _replace_once(text, _FILE, f"`lab/reports/{stem}.md`")
    text = _replace_once(text, _TASK, f"Roadmap task: {task}")
    text = _replace_once(text, _SPIKE, f"Spike: {spike}")
    text = _replace_once(text, _AUTHOR, f"Author: {author}")
    text = _replace_once(text, _COMMIT, f"Git commit: `{_commits(envelopes)}`")
    text = _replace_once(text, _HOSTS_ROW, "\n" + _host_rows(envelopes))
    text = _replace_once(text, _RAW, f"`lab/reports/raw/{stem}/`")
    text = _replace_once(text, _N, n_headline(rows))
    table = results_table_rows(rows)
    if extra_results:
        table += "\n" + extra_results.rstrip("\n") + "\n\n"
    text = _replace_once(text, _RESULTS_ROW, "\n" + table)
    return _replace_once(text, _NOTES, _NOTES + "\n" + notes(envelopes, rows))


def read_template(path: Path = TEMPLATE_PATH) -> str:
    return path.read_text(encoding="utf-8")
