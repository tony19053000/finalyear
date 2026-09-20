"""Independent tester pass on 0.2.13 (report CLIs must not destroy prose).

These tests pin defects found by adversarial testing that are NOT covered by
the implementer's own ``test_report_merge.py`` / ``test_report_cli_merge.py``.
Each test documents the exact reproduction and the expected (currently
failing) behaviour. Do not weaken these to make them pass - if they fail,
that is the implementer's bug to fix, not this file's.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from spikes._harness.envelope import make_envelope
from spikes._harness.report import (
    merge_report,
    read_template,
    render_report,
    write_report,
)
from spikes._harness.reps import CaseResult

NOW = datetime(2026, 9, 20, 9, 0, tzinfo=UTC)


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


# --------------------------------------------------------------- DEF: crash
def test_a_lone_surrogate_in_a_result_hostname_is_refused_not_crashed(
    tmp_path: Path,
) -> None:
    """A result JSON is data written by another process (or corrupted). JSON
    permits an unpaired ``\\uD800``-range escape, which ``json.loads`` decodes
    into a Python string holding a lone surrogate - a value that cannot be
    encoded as UTF-8. That value flows from ``host.hostname`` straight into
    the rendered report text and then into ``atomic_write_text``'s
    ``fh.write(text)``, which raises ``UnicodeEncodeError`` - a crash, not the
    documented "refuse loudly, file untouched" contract. ``write_report``
    should catch this (as it already does for ``UnicodeDecodeError`` on
    *read*) and raise something the CLI's ``except (OSError, ...)`` clauses
    handle, leaving the target byte-identical.
    """
    out = tmp_path / "r.md"
    out.write_text(BASE, encoding="utf-8", newline="\n")
    poisoned = FRESH.replace("hostB", "host\udcffB")
    with pytest.raises(OSError):
        write_report(out, poisoned)
    assert out.read_bytes() == BASE.encode("utf-8")


# ------------------------------------------------------- DEF: silent loss
def test_a_human_table_glued_to_the_host_table_is_not_silently_dropped() -> None:
    """Attack #1 from the brief: a human pastes their own ``|``-table
    immediately under the generated host table, with no blank line
    separating the two (an extremely easy mistake - most editors do not
    insist on a blank line before a table). ``_host_table_parts`` treats any
    unbroken run of ``|``-starting lines as the *generated* host table, so
    the human's table is silently absorbed and discarded: exit 0, no
    warning, ``WriteResult.messages()`` still claims the Machines prose was
    kept. This is exactly the "silent loss" defect class 0.2.13 exists to
    kill - it must either be preserved or the loss must be surfaced loudly,
    never silent.
    """
    marker = "MY-CUSTOM-CELL-VALUE-4f2c9"
    glued = BASE.replace(
        "| hostA | Windows 11 | see inventory JSON | Ethernet (speed: TBD) | TBD |\n",
        "| hostA | Windows 11 | see inventory JSON | Ethernet (speed: TBD) | TBD |\n"
        f"| my own table | {marker} |\n"
        "|---|---|\n"
        "| custom-metric | 42 |\n",
    )
    assert marker in glued  # sanity: the fixture really contains the human table
    merged = merge_report(glued, FRESH)
    assert marker in merged, (
        "a human table pasted directly under the host table (no blank line) "
        "was silently discarded by the merge"
    )


# ------------------------------------------------------------------- DEF: BOM
def test_a_bom_prefixed_report_from_a_windows_editor_can_still_be_merged() -> None:
    """Three of the four team laptops are Windows; PowerShell's ``Set-Content``
    / ``Out-File`` (and older Notepad) write UTF-8 **with a BOM** by default.
    A hand-edited report saved that way starts with U+FEFF, which is not
    whitespace, so ``preamble.lstrip().startswith("# ")`` is false and the
    merge refuses with "no '# ' title line before the first section" even
    though the file is a perfectly good report. CRLF is already normalised
    for the same cross-platform reason (see ``_normalise_newlines``); a
    leading BOM should be stripped the same way instead of permanently
    blocking merges until a human manually edits the bytes or someone runs
    ``--force`` (which discards every hand-written section).
    """
    existing = "﻿" + BASE.replace(
        "The question this run answers, and the ARCHITECTURE §27 question or "
        "ADR it informs.\n",
        "A BOM-saved goal, written on Windows.\n",
    )
    merged = merge_report(existing, FRESH)
    assert "A BOM-saved goal, written on Windows." in merged
    assert "﻿" not in merged
