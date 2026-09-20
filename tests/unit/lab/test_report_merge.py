"""Unit tests for the report merge (roadmap 0.2.13).

``report --out <existing file>`` used to re-render the whole file from
``TEMPLATE.md``, blanking every hand-written section. These tests pin the
replacement: generated regions are refreshed, human regions are taken from the
file on disk, anything unexpected is refused loudly and the file is left
byte-identical.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from spikes._harness.envelope import make_envelope
from spikes._harness.report import (
    PRESERVED_REGIONS,
    ReportMergeError,
    TemplateError,
    merge_report,
    read_template,
    render_report,
    write_report,
)
from spikes._harness.reps import CaseResult

NOW = datetime(2026, 9, 20, 9, 0, tzinfo=UTC)
REPO_ROOT = Path(__file__).resolve().parents[3]
REAL_S6_REPORT = REPO_ROOT / "lab" / "reports" / "2026-09-20-S6-environments.md"
REAL_S6_RAW = (
    REPO_ROOT
    / "lab"
    / "reports"
    / "raw"
    / "2026-09-20-S6-environments"
    / "l1-dffe8596.json"
)


def _env(host: str, values: list[float], *, commit: str = "abc1234") -> dict:  # type: ignore[type-arg]
    case = CaseResult(
        case_id="tcp",
        label=f"TCP {host}",
        metrics={"receiver_mbps": "Mbit/s"},
        samples=[{"receiver_mbps": v} for v in values],
        errors=[],
    )
    return make_envelope(
        spike="S1",
        topic="S1-network",
        link="ethernet",
        params={},
        cases=[case],
        errors=None,
        git={"commit": commit, "dirty": False},
        host={"hostname": host, "os": "Windows", "os_release": "11"},
        now=NOW,
    )


def _render(envs: list[dict], *, day: str = "2026-09-20", extra: str = "") -> str:  # type: ignore[type-arg]
    return render_report(
        read_template(),
        envs,
        topic="S1-network",
        title="S1 network matrix",
        day=day,
        task="0.2.5",
        spike="S1",
        extra_results=extra,
    )


BASE = _render([_env("hostA", [1, 2, 3, 4, 5])])
FRESH = _render([_env("hostA", [9, 9, 9, 9, 9, 9]), _env("hostB", [1, 2, 3, 4, 5])])


def _section(text: str, title: str) -> str:
    """Body of ``## <title>`` in ``text`` (test-side parser, deliberately dumb)."""
    lines = text.splitlines(keepends=True)
    start = next(i for i, ln in enumerate(lines) if ln == f"## {title}\n")
    end = next(
        (
            i
            for i in range(start + 1, len(lines))
            if lines[i].startswith("## ") and not lines[i].startswith("### ")
        ),
        len(lines),
    )
    return "".join(lines[start + 1 : end])


def _edit(text: str, title: str, body: str) -> str:
    return text.replace(f"## {title}\n{_section(text, title)}", f"## {title}\n{body}")


# ------------------------------------------------------------- round trip
def test_merging_an_untouched_render_equals_a_plain_render() -> None:
    """The round-trip guarantee: no drift, no duplicated headings, no lost
    trailing newline when the human sections are still the placeholders."""
    assert merge_report(BASE, BASE) == BASE


def test_merging_an_untouched_render_takes_the_new_generated_regions() -> None:
    merged = merge_report(BASE, FRESH)
    assert merged == FRESH


def test_generated_regions_are_refreshed_while_human_text_survives() -> None:
    existing = _edit(BASE, "Goal", "Does the Wi-Fi link carry 300 Mbit/s?\n")
    merged = merge_report(existing, FRESH)
    assert _section(merged, "Goal") == "Does the Wi-Fi link carry 300 Mbit/s?\n"
    assert _section(merged, "Results") == _section(FRESH, "Results")
    assert "hostB" in merged  # the new host row and result row are there
    assert merged.splitlines()[0] == FRESH.splitlines()[0]


# ------------------------------------------------------- heavy human edits
HEAVY_GOAL = """A long goal.

### Why now
Several paragraphs, a list:

- one
- two

And a line that looks generated: **n = <≥5>** and `lab/reports/raw/YYYY-MM-DD-<topic>/`.
"""

HEAVY_CONCLUSION = """Three plain sentences about what the data shows.

| verdict | value |
|---|---|
| good | yes |

More prose after a table a human added under Conclusion.
"""

HEAVY_COMMANDS = """Exact commands:
```bash
# host: laptop1
echo "## not a heading"
uv run python lab/spikes/s1_network.py serve
```
~~~powershell
## also not a heading
~~~
"""


def _heavily_edited() -> str:
    text = _edit(BASE, "Goal", HEAVY_GOAL)
    text = _edit(text, "Conclusion", HEAVY_CONCLUSION)
    text = _edit(text, "Commands run", HEAVY_COMMANDS)
    text = _edit(text, "Expected vs actual", "\nWe expected 300, we saw 280.\n\n")
    text = _edit(text, "Decisions triggered", "- [x] ADR-0009 updated\n")
    return text.replace(
        "Background load, time of day, and anything unusual:",
        "Background load, time of day, and anything unusual: nothing running.\n"
        "\n"
        "| my own table | added by a human |\n"
        "|---|---|\n"
        "| a | b |",
    )


def test_a_heavily_edited_report_keeps_every_human_region() -> None:
    merged = merge_report(_heavily_edited(), FRESH)
    assert _section(merged, "Goal") == HEAVY_GOAL
    assert _section(merged, "Conclusion") == HEAVY_CONCLUSION
    # ``_section`` above is a dumb parser and would stop at the ``## `` line
    # inside the fenced block, so this region is checked as a whole string.
    assert HEAVY_COMMANDS in merged
    assert "We expected 300, we saw 280." in merged
    assert "- [x] ADR-0009 updated" in merged
    assert "nothing running." in merged
    assert "| my own table | added by a human |" in merged
    # ... and the generated ones are the fresh ones
    assert _section(merged, "Results") == _section(FRESH, "Results")
    assert _section(merged, "Raw output location") == _section(
        FRESH, "Raw output location"
    )
    assert merged.count("## Goal\n") == 1
    assert merged.count("## Results\n") == 1


def test_headings_inside_a_fenced_code_block_are_not_sections() -> None:
    """``## `` in a shell snippet must not be parsed as a report section."""
    existing = _edit(BASE, "Commands run", HEAVY_COMMANDS)
    merged = merge_report(existing, FRESH)
    assert 'echo "## not a heading"' in merged
    assert "## also not a heading" in merged
    for heading in (
        "Goal",
        "Machines & conditions",
        "Commands run",
        "Raw output location",
        "Results",
        "Expected vs actual",
        "Conclusion",
        "Decisions triggered",
    ):
        assert merged.count(f"\n## {heading}\n") == 1, heading


def test_human_text_inside_a_generated_region_is_replaced() -> None:
    """Documented behaviour: the Results section belongs to the generator."""
    existing = BASE.replace(
        "Notes on outliers or failed runs (list them, don't hide them):\n",
        "A sentence a human wrote inside Results.\n"
        "Notes on outliers or failed runs (list them, don't hide them):\n",
    )
    merged = merge_report(existing, FRESH)
    assert "A sentence a human wrote inside Results." not in merged


def test_a_hand_filled_host_table_cell_is_replaced() -> None:
    """Also documented: only the prose *after* the host table is the human's."""
    existing = BASE.replace("(speed: TBD)", "(speed: 866 Mbit/s)")
    merged = merge_report(existing, FRESH)
    assert "866 Mbit/s" not in merged
    assert "(speed: TBD)" in merged


# ------------------------------------------------------------- idempotence
def test_two_consecutive_merges_are_identical() -> None:
    once = merge_report(_heavily_edited(), FRESH)
    twice = merge_report(once, FRESH)
    assert twice == once
    assert merge_report(twice, FRESH) == once


# ------------------------------------------------------------ line endings
def test_a_crlf_report_merges_and_the_result_is_lf_only() -> None:
    crlf = _heavily_edited().replace("\n", "\r\n")
    merged = merge_report(crlf, FRESH)
    assert "\r" not in merged
    assert _section(merged, "Goal") == HEAVY_GOAL
    assert _section(merged, "Results") == _section(FRESH, "Results")


def test_a_report_without_a_trailing_newline_still_ends_with_one() -> None:
    merged = merge_report(BASE.rstrip("\n"), FRESH)
    assert merged.endswith("\n")
    assert not merged.endswith("\n\n\n")


# ---------------------------------------------------------------- refusals
@pytest.mark.parametrize(
    ("broken", "expected"),
    [
        (BASE.replace("## Conclusion\n", ""), "missing section(s): '## Conclusion'"),
        (BASE + "## Goal\nsecond one\n", "duplicated section(s): '## Goal' (2x)"),
        (
            BASE + "## Appendix\nmine\n",
            "unexpected top-level heading(s): '## Appendix'",
        ),
        (BASE.replace("# S1 network matrix — 2026-09-20\n", ""), "no '# ' title line"),
        ("", "missing section(s)"),
        ("not a report at all\n\njust prose\n", "missing section(s)"),
    ],
)
def test_a_file_that_is_not_shaped_like_a_report_is_refused(
    broken: str, expected: str
) -> None:
    with pytest.raises(ReportMergeError) as exc:
        merge_report(broken, FRESH)
    assert expected in exc.value.problem
    assert "--force" in str(exc.value)
    assert "unchanged" in str(exc.value)


def test_reordered_sections_are_refused_and_named() -> None:
    goal = f"## Goal\n{_section(BASE, 'Goal')}"
    conclusion = f"## Conclusion\n{_section(BASE, 'Conclusion')}"
    swapped = BASE.replace(goal, "@@GOAL@@").replace(conclusion, goal)
    swapped = swapped.replace("@@GOAL@@", conclusion)
    with pytest.raises(ReportMergeError, match="out of order"):
        merge_report(swapped, FRESH)


def test_a_machines_section_without_a_host_table_is_refused() -> None:
    lines = [ln for ln in BASE.splitlines(keepends=True) if not ln.startswith("|")]
    with pytest.raises(ReportMergeError, match="no host table"):
        merge_report("".join(lines), FRESH)


def test_a_truncated_report_is_refused() -> None:
    truncated = BASE[: BASE.index("## Results")] + "## Results\n| half a ta"
    with pytest.raises(ReportMergeError, match="missing section"):
        merge_report(truncated, FRESH)


def test_an_unclosed_code_fence_is_refused_not_silently_swallowed() -> None:
    """Everything after an unterminated ``` is code, so the later headings
    disappear: the merge must refuse rather than eat half the report."""
    existing = _edit(BASE, "Commands run", "```bash\necho hi\n")
    with pytest.raises(ReportMergeError, match="missing section"):
        merge_report(existing, FRESH)


def test_a_broken_template_is_a_template_error_not_a_merge_error() -> None:
    """If the fresh render itself loses a heading, the failure names the
    generator, not the human's file."""
    broken_fresh = FRESH.replace("## Conclusion\n", "## Summary\n")
    with pytest.raises(TemplateError, match="generated report cannot be merged"):
        merge_report(BASE, broken_fresh)


# ------------------------------------------------------------ write_report
def test_write_report_creates_a_missing_file_and_its_parents(tmp_path: Path) -> None:
    out = tmp_path / "deep" / "r.md"
    result = write_report(out, BASE)
    assert result.merged is False and result.forced is False
    assert result.messages() == []
    assert out.read_text(encoding="utf-8") == BASE


def test_write_report_merges_an_existing_file_by_default(tmp_path: Path) -> None:
    out = tmp_path / "r.md"
    out.write_text(_heavily_edited(), encoding="utf-8", newline="\n")
    result = write_report(out, FRESH)
    assert result.merged is True and result.forced is False
    assert HEAVY_GOAL in out.read_text(encoding="utf-8")
    assert all(region in result.messages()[0] for region in PRESERVED_REGIONS)


def test_force_regenerates_the_whole_file(tmp_path: Path) -> None:
    out = tmp_path / "r.md"
    out.write_text(_heavily_edited(), encoding="utf-8", newline="\n")
    result = write_report(out, FRESH, force=True)
    assert result.merged is False and result.forced is True
    assert out.read_text(encoding="utf-8") == FRESH
    assert "hand-written sections are gone" in result.messages()[0]


def test_force_on_a_missing_file_says_nothing_about_destruction(
    tmp_path: Path,
) -> None:
    result = write_report(tmp_path / "r.md", FRESH, force=True)
    assert result.forced is False and result.messages() == []


def test_a_refused_merge_leaves_the_file_byte_identical(tmp_path: Path) -> None:
    out = tmp_path / "r.md"
    original = _heavily_edited().replace("\n", "\r\n").encode("utf-8")
    out.write_bytes(original)
    broken = out.with_name("broken.md")
    broken.write_bytes(BASE.replace("## Results\n", "").encode("utf-8"))
    before = broken.read_bytes()
    with pytest.raises(ReportMergeError):
        write_report(broken, FRESH)
    assert broken.read_bytes() == before
    # and the CRLF file is merged, not mangled, when it is well formed
    write_report(out, FRESH)
    assert out.read_bytes() != original
    assert b"\r" not in out.read_bytes()


def test_a_file_that_is_not_utf8_is_refused_and_untouched(tmp_path: Path) -> None:
    out = tmp_path / "r.md"
    out.write_bytes(b"\xff\xfe# S1\x00\x00 report")
    with pytest.raises(ReportMergeError, match="not valid UTF-8"):
        write_report(out, FRESH)
    assert out.read_bytes() == b"\xff\xfe# S1\x00\x00 report"


def test_writing_over_a_directory_raises_oserror_not_a_silent_pass(
    tmp_path: Path,
) -> None:
    target = tmp_path / "adir"
    target.mkdir()
    with pytest.raises(OSError):
        write_report(target, FRESH)


# ------------------------------------------------- the real S6 artefacts
def test_the_real_s6_report_keeps_its_prose_when_merged(tmp_path: Path) -> None:
    """The report this task exists for. Read-only: we copy it to tmp first."""
    original = REAL_S6_REPORT.read_bytes()
    digest = hashlib.md5(REAL_S6_RAW.read_bytes(), usedforsecurity=False)
    assert digest.hexdigest() == ("09e7c21ffe131cf55cd0d85c79bd82ce"), (
        "the real Laptop 1 measurement must never be modified"
    )
    existing = original.decode("utf-8")
    # a fresh render of *another* spike's data must not be able to eat the prose
    merged = merge_report(existing, FRESH)
    for prose in (
        "Per laptop: is Docker usable for workloads",  # their Goal
        "Background load, time of day, and anything unusual: Laptop 1 on AC",
        "docker builder prune",  # their Machines prose
        "# host: JARVISM3 (Laptop 1, Ubuntu 24.04, bash)",  # their Commands run
    ):
        assert prose in merged, prose
    assert _section(merged, "Goal") == _section(existing, "Goal")
    assert _section(merged, "Commands run") == _section(existing, "Commands run")
    assert _section(merged, "Conclusion") == _section(existing, "Conclusion")
    assert _section(merged, "Expected vs actual") == _section(
        existing, "Expected vs actual"
    )
    assert "Laptop 1 on AC" in _section(merged, "Machines & conditions")
    assert _section(merged, "Results") == _section(FRESH, "Results")
    assert REAL_S6_REPORT.read_bytes() == original
