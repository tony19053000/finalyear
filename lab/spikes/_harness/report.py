"""Generate a lab report from ``lab/reports/TEMPLATE.md`` and result envelopes.

Filled in automatically: title, header (file, task, spike, commit), the
"Machines & conditions" host rows, the raw-output location, ``n``, the Results
table (one row per case x metric, rows with n < 5 marked INVALID), and the list
of failed repetitions / invalid rows. Goal, conclusion and decisions stay for a
human or agent to write. Spikes may append extra markdown (e.g. the S1 matrices)
right after the Results table. If the template no longer contains a placeholder
this code relies on, :class:`TemplateError` is raised instead of silently
producing a half-filled report.

Re-running ``report --out`` over an existing file **merges**: the generated
regions are refreshed and the hand-written ones are taken from the file on disk
(see "merging" at the bottom of this module). ``--force`` restores the old
regenerate-everything behaviour.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from spikes._harness import MIN_VALID_REPS
from spikes._harness.atomic import atomic_write_text
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
        return (
            f"**n = 0** (no valid rows; {n_invalid} {_rows(n_invalid)} INVALID "
            f"(n < {MIN_VALID_REPS}), marked in the table)"
        )
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


# ------------------------------------------------------------------- merging
#
# ``report --out FILE`` is re-run whenever new results arrive or this code
# changes, but the file it writes is only half generated: the other half is
# prose a human wrote. A plain overwrite destroys that prose. The regions are
# fixed by the placeholders above and by ``TEMPLATE.md``:
#
#   generated  the title line and the ``> File: ...`` meta line; the host table
#              rows of "Machines & conditions"; "Raw output location"; the whole
#              "Results" section (the ``n`` headline, the results table, any
#              extra tables a spike appends, and the notes list).
#   human      "Goal"; the prose *after* the host table; "Commands run";
#              "Expected vs actual"; "Conclusion"; "Decisions triggered".
#
# :func:`merge_report` starts from the fresh render and splices the human
# regions of the existing file into it, so merging an untouched generated
# report reproduces a plain render byte for byte. Text a human wrote *inside* a
# generated region is replaced - that is what "generated" means - and the CLI
# prints which regions it kept. If the existing file is not shaped like one of
# our reports, or is a *different* spike's report (its ``Roadmap task:`` /
# ``Spike:`` meta values disagree with the fresh render's),
# :class:`ReportMergeError` is raised and nothing is written.

GOAL: Final = "Goal"
MACHINES: Final = "Machines & conditions"
COMMANDS: Final = "Commands run"
RAW_LOCATION: Final = "Raw output location"
RESULTS: Final = "Results"
EXPECTED: Final = "Expected vs actual"
CONCLUSION: Final = "Conclusion"
DECISIONS: Final = "Decisions triggered"

SECTIONS: Final = (
    GOAL,
    MACHINES,
    COMMANDS,
    RAW_LOCATION,
    RESULTS,
    EXPECTED,
    CONCLUSION,
    DECISIONS,
)
HUMAN_SECTIONS: Final = (GOAL, COMMANDS, EXPECTED, CONCLUSION, DECISIONS)
MACHINES_PROSE: Final = f"{MACHINES} (prose after the host table)"
#: Human regions, in document order, as named to the user.
PRESERVED_REGIONS: Final = (
    GOAL,
    MACHINES_PROSE,
    COMMANDS,
    EXPECTED,
    CONCLUSION,
    DECISIONS,
)

FORCE_HELP: Final = (
    "regenerate an existing --out from the template, discarding every "
    "hand-written section (default: keep them and refresh only the generated "
    "parts)"
)

_FENCE_RE: Final = re.compile(r"^ {0,3}(`{3,}|~{3,})")
#: An unescaped ``|``: a cell separator (``cell()`` writes literal pipes as ``\|``).
_PIPE_RE: Final = re.compile(r"(?<!\\)\|")
_DASHES_RE: Final = re.compile(r":?-+:?")
_BOM: Final = "﻿"
#: Meta fields of the ``> File: ...`` line that say which spike a report is.
_IDENTITY_FIELDS: Final = ("Roadmap task", "Spike")


class ReportMergeError(ValueError):
    """``--out`` exists but is not a report this generator can merge into."""

    def __init__(self, problem: str, *, path: Path | None = None) -> None:
        self.problem = problem
        self.path = path
        where = f"{path}: " if path is not None else ""
        super().__init__(
            f"{where}{problem}. Nothing was written, the file is unchanged. Fix "
            "the file, choose another --out, or re-run with --force to "
            "regenerate it from the template (that discards every hand-written "
            "section)."
        )


class _NoHostTableError(Exception):
    """A "Machines & conditions" body without a host table (internal)."""


@dataclass(frozen=True, slots=True)
class _Section:
    title: str
    heading: str  # the exact ``## ...`` line, newline included
    body: str  # everything up to the next top-level heading, exact


@dataclass(frozen=True, slots=True)
class _Parsed:
    preamble: str
    sections: tuple[_Section, ...]

    def body(self, title: str) -> str:
        for section in self.sections:
            if section.title == title:
                return section.body
        raise _NoHostTableError(title)  # unreachable once _shape_problem() passed


@dataclass(frozen=True, slots=True)
class WriteResult:
    """What :func:`write_report` did, so the CLI can say it out loud."""

    path: Path
    merged: bool
    forced: bool

    def messages(self) -> list[str]:
        if self.merged:
            return [
                "kept the hand-written text of: "
                + ", ".join(PRESERVED_REGIONS)
                + "; every other region was regenerated"
            ]
        if self.forced:
            return [
                "--force: the existing file was regenerated from the template, "
                "its hand-written sections are gone"
            ]
        return []


def _normalise_newlines(text: str) -> str:
    """CRLF / CR -> LF and no leading BOM, so a report edited on Windows merges
    like any other.

    PowerShell's ``Set-Content`` / ``Out-File`` (and older Notepad) save UTF-8
    **with** a BOM. U+FEFF is not whitespace, so a BOM would otherwise hide the
    ``# `` title line and make every merge refuse for ever. Output stays LF and
    BOM-free.
    """
    return text.lstrip(_BOM).replace("\r\n", "\n").replace("\r", "\n")


def _fence_opened(line: str) -> str | None:
    match = _FENCE_RE.match(line)
    return match.group(1) if match else None


def _fence_closed(line: str, fence: str) -> bool:
    stripped = line.strip()
    indent = len(line) - len(line.lstrip(" "))
    return indent <= 3 and len(stripped) >= len(fence) and set(stripped) == {fence[0]}


def _split_sections(text: str) -> _Parsed:
    """Split a report into its preamble and its ``## `` sections, exactly.

    Headings inside fenced code blocks are text, not headings (``## `` can
    legitimately appear in a "Commands run" snippet). Joining every piece back
    together reproduces ``text`` unchanged.
    """
    preamble: list[str] = []
    sections: list[_Section] = []
    heading: str | None = None
    title = ""
    body: list[str] = []
    fence: str | None = None
    for line in text.splitlines(keepends=True):
        if fence is not None:
            if _fence_closed(line, fence):
                fence = None
        else:
            opened = _fence_opened(line)
            if opened is not None:
                fence = opened
            elif line.startswith("## "):
                if heading is not None:
                    sections.append(_Section(title, heading, "".join(body)))
                heading, title, body = line, line[3:].strip(), []
                continue
        (preamble if heading is None else body).append(line)
    if heading is not None:
        sections.append(_Section(title, heading, "".join(body)))
    return _Parsed("".join(preamble), tuple(sections))


def _columns(line: str) -> int:
    """How many cell separators a table row has (its shape)."""
    return len(_PIPE_RE.findall(line))


def _is_delimiter_row(line: str) -> bool:
    """``|---|---|`` & co: the row that *starts* a markdown table's body."""
    stripped = line.strip()
    if not stripped.startswith("|"):
        return False
    cells = [c.strip() for c in _PIPE_RE.split(stripped)[1:-1] or [""]]
    return all(c and _DASHES_RE.fullmatch(c) for c in cells)


def _host_table_parts(body: str) -> tuple[str, str]:
    """A "Machines & conditions" body as (host table, prose after it).

    The generated table is the run of ``|`` lines that starts at the first one
    and keeps the *shape* of that header row; everything from there on is the
    human's. A table they pasted directly under the generated one - no blank
    line, the easy mistake - is therefore kept, because it gives itself away
    twice: its rows have a different number of cells, and it brings its own
    ``|---|`` delimiter row (the generated table has exactly one, right under
    its header). When the delimiter is what gives it away, the split moves one
    line up, so the pasted table keeps its own header row too.

    The one case this cannot see is a bare ``|`` row with exactly the shape of
    a host row and no delimiter under it: it is byte-for-byte what the
    generator itself writes. Separate your own rows with a blank line.
    """
    lines = body.splitlines(keepends=True)
    start = next((i for i, ln in enumerate(lines) if ln.startswith("|")), None)
    if start is None:
        raise _NoHostTableError(body)
    width = _columns(lines[start])
    end = start + 1
    while end < len(lines) and lines[end].startswith("|"):
        if _columns(lines[end]) != width:
            break  # a differently shaped row: not part of the generated table
        if end > start + 1 and _is_delimiter_row(lines[end]):
            end -= 1  # a second table starts here; the row above is its header
            break
        end += 1
    return "".join(lines[:end]), "".join(lines[end:])


def _shape_problem(parsed: _Parsed) -> str | None:
    """The first way ``parsed`` differs from a generated report, or ``None``."""
    titles = [s.title for s in parsed.sections]
    duplicated = [t for t in SECTIONS if titles.count(t) > 1]
    if duplicated:
        return "duplicated section(s): " + ", ".join(
            f"'## {t}' ({titles.count(t)}x)" for t in duplicated
        )
    missing = [t for t in SECTIONS if t not in titles]
    if missing:
        return "missing section(s): " + ", ".join(f"'## {t}'" for t in missing)
    unexpected = [t for t in titles if t not in SECTIONS]
    if unexpected:
        return (
            "unexpected top-level heading(s): "
            + ", ".join(f"'## {t}'" for t in unexpected)
            + " (write your own sub-headings as '### ')"
        )
    if titles != list(SECTIONS):
        return (
            "sections are out of order: expected "
            + " / ".join(SECTIONS)
            + "; found "
            + " / ".join(titles)
        )
    if not parsed.preamble.lstrip().startswith("# "):
        return "no '# ' title line before the first section"
    try:
        _host_table_parts(parsed.body(MACHINES))
    except _NoHostTableError:
        return f"'## {MACHINES}' has no host table (no line starts with '|')"
    return None


def _identity(preamble: str, field: str) -> str | None:
    """``field``'s value on the ``> File: ...`` meta line, if it has one.

    Only the first whitespace-delimited token counts, so a human who annotates
    the line (``Spike: S6 (Linux only)``) or re-punctuates it with ``-`` instead
    of ``·`` is not mistaken for another spike. ``None`` when the line, the
    field or a real value is missing (a template placeholder such as ``<0.2.x>``
    counts as missing, so a report started by hand from TEMPLATE.md still
    merges).
    """
    for line in preamble.splitlines():
        if not line.lstrip().startswith(">"):
            continue
        match = re.search(rf"{re.escape(field)}:\s*(\S+)", line)
        if match is None:
            continue
        value = match.group(1).rstrip("·,;")
        if value and not (value.startswith("<") and value.endswith(">")):
            return value
    return None


def _identity_problem(old: _Parsed, new: _Parsed) -> str | None:
    """Whether ``old`` is a *different* spike's report than ``new``.

    Merging one spike's fresh data into another spike's finished report would
    leave a file whose title and meta line name one spike while every kept
    paragraph describes another - a confidently wrong artefact, and almost
    always the wrong ``--out``.
    """
    for field in _IDENTITY_FIELDS:
        mine, theirs = _identity(old.preamble, field), _identity(new.preamble, field)
        if mine is not None and theirs is not None and mine != theirs:
            return (
                f"this file looks like another spike's report: the '{field}' on "
                f"its '> File:' line is {mine!r}, the report being generated is "
                f"{theirs!r} (if that line was hand-edited, restore it instead "
                "of using --force)"
            )
    return None


def merge_report(existing: str, fresh: str, *, path: Path | None = None) -> str:
    """``fresh`` with the human-written regions of ``existing`` spliced in.

    Raises :class:`TemplateError` if ``fresh`` is not shaped like a report (the
    template changed under us) and :class:`ReportMergeError` if ``existing`` is
    not, or if it belongs to another spike. Never returns a partially merged
    text, and never drops a human region: every region named in
    :data:`PRESERVED_REGIONS` is taken verbatim from ``existing``.
    """
    new = _split_sections(_normalise_newlines(fresh))
    problem = _shape_problem(new)
    if problem is not None:
        raise TemplateError(f"the generated report cannot be merged: {problem}")
    old = _split_sections(_normalise_newlines(existing))
    problem = _shape_problem(old) or _identity_problem(old, new)
    if problem is not None:
        raise ReportMergeError(problem, path=path)
    out = [new.preamble]
    for section in new.sections:
        body = section.body
        if section.title in HUMAN_SECTIONS:
            body = old.body(section.title)
        elif section.title == MACHINES:
            table, _ = _host_table_parts(section.body)
            _, prose = _host_table_parts(old.body(MACHINES))
            body = table + prose
        out.append(section.heading + body)
    text = "".join(out)
    return text if text.endswith("\n") else text + "\n"


def write_report(path: Path, text: str, *, force: bool = False) -> WriteResult:
    """Write a rendered report to ``path`` atomically, keeping human prose.

    ``path`` does not exist, or ``force`` -> ``text`` is written as rendered.
    Otherwise the human regions of the existing file are merged into ``text``
    (:func:`merge_report`). Raises :class:`ReportMergeError` when the existing
    file is not a report of the expected shape or is another spike's, and
    ``OSError`` when it cannot be read, cannot be written, or holds text that
    cannot be encoded (:class:`~spikes._harness.atomic.UnencodableTextError`);
    in every failure case ``path`` is left byte-identical.
    """
    existed = path.exists()
    merge = existed and not force
    if merge:
        try:
            existing = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ReportMergeError(
                f"the file is not valid UTF-8 text ({exc.reason} at byte {exc.start})",
                path=path,
            ) from exc
        text = merge_report(existing, text, path=path)
    atomic_write_text(path, text)
    return WriteResult(path=path, merged=merge, forced=existed and force)
