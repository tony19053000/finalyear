"""Unit tests for the markdown report generator (fills lab/reports/TEMPLATE.md)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from spikes._harness.envelope import make_envelope
from spikes._harness.report import TemplateError, read_template, render_report
from spikes._harness.reps import CaseResult

NOW = datetime(2026, 9, 20, 9, 0, tzinfo=UTC)


def _env(host: str, values: list[float], *, dirty: bool = False, **kw: object) -> dict:  # type: ignore[type-arg]
    case = CaseResult(
        case_id="tcp",
        label=f"TCP {host}",
        metrics={"receiver_mbps": "Mbit/s"},
        samples=[{"receiver_mbps": v} for v in values],
        errors=kw.pop("case_errors", []),  # type: ignore[arg-type]
    )
    return make_envelope(
        spike="S1",
        topic="S1-network",
        link=kw.pop("link", "ethernet"),  # type: ignore[arg-type]
        params={},
        cases=[case],
        errors=kw.pop("errors", None),  # type: ignore[arg-type]
        git={"commit": "abc1234", "dirty": dirty},
        host={"hostname": host, "os": "Windows", "os_release": "11"},
        now=NOW,
    )


def _render(envs: list[dict], extra: str = "") -> str:  # type: ignore[type-arg]
    return render_report(
        read_template(),
        envs,
        topic="S1-network",
        title="S1 network matrix",
        day="2026-09-20",
        task="0.2.5",
        spike="S1",
        extra_results=extra,
    )


def test_all_generated_placeholders_are_filled() -> None:
    text = _render([_env("hostA", [900, 910, 920, 930, 940])])
    for placeholder in (
        "<Topic>",
        "Roadmap task: <0.2.x>",
        "Git commit: `<short sha>`",
        "**n = <≥5>**",
        "\n| | | | | | | |\n",
        "\n| | | | | |\n",
        "`lab/reports/raw/YYYY-MM-DD-<topic>/`",
    ):
        assert placeholder not in text, placeholder
    assert text.startswith("# S1 network matrix — 2026-09-20\n")
    assert "`lab/reports/2026-09-20-S1-network.md`" in text
    assert "`lab/reports/raw/2026-09-20-S1-network/`" in text
    assert "Git commit: `abc1234`" in text
    assert "| hostA | Windows 11 |" in text
    assert (
        "| [Ethernet] hostA: TCP hostA | receiver_mbps (Mbit/s) | 5 | 920.00 |" in text
    )
    assert "**n = 5**" in text
    # Human sections stay for a human/agent to write.
    assert "## Conclusion" in text and "## Goal" in text


def test_invalid_rows_errors_and_dirty_commits_are_listed() -> None:
    bad = _env(
        "hostB",
        [1, 2, 3],
        dirty=True,
        link="wifi",
        errors=[{"failure_type": "connection_lost", "message": "server lost"}],
        case_errors=[
            {"rep": 2, "phase": "measure", "failure_type": "timeout", "message": "slow"}
        ],
    )
    text = _render([_env("hostA", [1, 2, 3, 4, 5]), bad])
    assert "[Wi-Fi] hostB: TCP hostB **INVALID (n<5)**" in text
    assert "**n = 3**" in text  # smallest n of any row
    assert "- INVALID: [Wi-Fi] hostB: TCP hostB / receiver_mbps: only n=3" in text
    assert "run error connection_lost: server lost" in text
    assert "tcp rep 2 (measure): timeout: slow" in text
    assert "abc1234, abc1234+dirty" in text


def test_extra_results_follow_the_table() -> None:
    text = _render([_env("hostA", [1, 2, 3, 4, 5])], extra="### Matrices\n\nhello")
    table_at = text.index("| [Ethernet] hostA")
    extra_at = text.index("### Matrices")
    notes_at = text.index("Notes on outliers or failed runs (list")
    assert table_at < extra_at < notes_at


def test_no_results() -> None:
    text = _render([])
    assert "| (no results) | | 0 |" in text
    assert "**n = 0**" in text
    assert "- none" in text


def test_missing_placeholder_raises() -> None:
    template = read_template().replace("Roadmap task: <0.2.x>", "Roadmap task: ?")
    with pytest.raises(TemplateError, match="Roadmap task"):
        render_report(
            template, [], topic="t", title="t", day="2026-01-01", task="x", spike="S1"
        )


def test_malformed_summary_becomes_invalid_row_not_crash() -> None:
    env = _env("hostA", [1, 2, 3, 4, 5])
    env["cases"]["tcp"]["summary"]["receiver_mbps"] = {"n": "five"}
    text = _render([env])
    assert "INVALID (n<5)" in text


def test_link_none_renders_not_applicable() -> None:
    text = _render([_env("hostA", [1, 2, 3, 4, 5], link=None)])
    assert (
        "| hostA | Windows 11 | see inventory JSON | n/a (speed: TBD) | TBD |" in text
    )
    assert "| [n/a] hostA: TCP hostA | receiver_mbps (Mbit/s) | 5 |" in text
    assert "[None]" not in text and "[ethernet]" not in text


def test_link_none_and_ethernet_are_separate_host_rows() -> None:
    text = _render(
        [_env("hostA", [1, 2, 3, 4, 5], link=None), _env("hostA", [1, 2, 3, 4, 5])]
    )
    assert text.count("| hostA | Windows 11 |") == 2


def test_pipe_in_cells_is_escaped() -> None:
    from spikes._harness.report import cell

    assert cell("a|b") == "a\\|b"
    assert cell("two\nlines\r\n") == "two lines  "
    env = _env("host|A", [1, 2, 3, 4, 5])
    env["cases"]["tcp"]["label"] = "TCP | tricky"
    env["cases"]["tcp"]["metrics"] = {"receiver_mbps": "Mbit|s"}
    text = _render([env])
    assert "| host\\|A | Windows 11 |" in text
    assert (
        "| [Ethernet] host\\|A: TCP \\| tricky | receiver_mbps (Mbit\\|s) | 5 |" in text
    )
    # every table row of the Results table still has the same number of cells
    rows = [ln for ln in text.splitlines() if ln.startswith("| [Ethernet]")]
    assert rows and all(ln.count("|") - ln.count("\\|") == 8 for ln in rows)
