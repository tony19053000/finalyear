"""The five spike report CLIs must never destroy hand-written prose (0.2.13).

Same contract for ``s1_network.py matrix`` and the ``report`` subcommand of
``s2_crossos.py``, ``s3_transport.py``, ``s6_environments.py`` and
``s7_hardware.py``: merge by default, refuse a file that is not a report,
regenerate only with ``--force``, and write atomically.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from spikes import (
    s1_network,
    s2_crossos,
    s3_transport,
    s6_environments,
    s7_hardware,
)
from spikes._harness.envelope import SCHEMA_VERSION

REPO_ROOT = Path(__file__).resolve().parents[3]
REAL_S6_REPORT = REPO_ROOT / "lab" / "reports" / "2026-09-20-S6-environments.md"
REAL_S6_RAW = REPO_ROOT / "lab" / "reports" / "raw" / "2026-09-20-S6-environments"

#: (id, main, subcommand) for every CLI that writes a report.
CLIS: list[tuple[str, Callable[[list[str]], int], str]] = [
    ("s1", s1_network.main, "matrix"),
    ("s2", s2_crossos.main, "report"),
    ("s3", s3_transport.main, "report"),
    ("s6", s6_environments.main, "report"),
    ("s7", s7_hardware.main, "report"),
]
IDS = [c[0] for c in CLIS]
GOAL = "Does the link carry what the planner assumes? Written by a human.\n"


def _run(
    cli: tuple[str, Callable[[list[str]], int], str],
    tmp_path: Path,
    out: Path,
    *flags: str,
) -> int:
    """Build a report from an empty results folder (no measurement needed)."""
    results = tmp_path / "2026-09-20-empty"
    results.mkdir(exist_ok=True)
    _, main, sub = cli
    return main([sub, "--results", str(results), "--out", str(out), *flags])


def _set_goal(path: Path, goal: str = GOAL) -> None:
    text = path.read_text(encoding="utf-8")
    start = text.index("## Goal\n") + len("## Goal\n")
    end = text.index("## Machines & conditions\n")
    path.write_text(text[:start] + goal + "\n" + text[end:], encoding="utf-8")


@pytest.mark.parametrize("cli", CLIS, ids=IDS)
def test_first_run_writes_and_second_run_keeps_the_goal(
    cli: tuple[str, Callable[[list[str]], int], str],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    out = tmp_path / "report.md"
    assert _run(cli, tmp_path, out) == 0
    _set_goal(out)
    assert _run(cli, tmp_path, out) == 0
    text = out.read_text(encoding="utf-8")
    assert GOAL in text
    assert "The question this run answers" not in text  # the placeholder is gone
    err = capsys.readouterr().err
    assert "kept the hand-written text of: Goal" in err


@pytest.mark.parametrize("cli", CLIS, ids=IDS)
def test_force_regenerates_and_says_so(
    cli: tuple[str, Callable[[list[str]], int], str],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    out = tmp_path / "report.md"
    assert _run(cli, tmp_path, out) == 0
    _set_goal(out)
    assert _run(cli, tmp_path, out, "--force") == 0
    text = out.read_text(encoding="utf-8")
    assert GOAL not in text
    assert "The question this run answers" in text
    assert "hand-written sections are gone" in capsys.readouterr().err


@pytest.mark.parametrize("cli", CLIS, ids=IDS)
def test_a_file_that_is_not_a_report_is_refused_and_left_untouched(
    cli: tuple[str, Callable[[list[str]], int], str],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    name = cli[0]
    out = tmp_path / "report.md"
    out.write_bytes(b"# my own notes\r\n\r\nnothing to do with a spike\r\n")
    before = out.read_bytes()
    assert _run(cli, tmp_path, out) == s1_network.EXIT_IO
    assert out.read_bytes() == before, name
    err = capsys.readouterr().err
    assert "missing section(s)" in err
    assert "--force" in err and "unchanged" in err


@pytest.mark.parametrize("cli", CLIS, ids=IDS)
def test_a_duplicated_heading_is_refused(
    cli: tuple[str, Callable[[list[str]], int], str],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    out = tmp_path / "report.md"
    assert _run(cli, tmp_path, out) == 0
    with out.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write("\n## Goal\nan accidental paste\n")
    before = out.read_bytes()
    assert _run(cli, tmp_path, out) == s1_network.EXIT_IO
    assert out.read_bytes() == before
    assert "duplicated section(s): '## Goal' (2x)" in capsys.readouterr().err


@pytest.mark.parametrize("cli", CLIS, ids=IDS)
def test_re_running_twice_is_idempotent(
    cli: tuple[str, Callable[[list[str]], int], str], tmp_path: Path
) -> None:
    out = tmp_path / "report.md"
    _run(cli, tmp_path, out)
    _set_goal(out)
    _run(cli, tmp_path, out)
    once = out.read_bytes()
    _run(cli, tmp_path, out)
    assert out.read_bytes() == once


@pytest.mark.parametrize("cli", CLIS, ids=IDS)
def test_the_written_file_has_no_crlf_even_from_a_crlf_original(
    cli: tuple[str, Callable[[list[str]], int], str], tmp_path: Path
) -> None:
    out = tmp_path / "report.md"
    _run(cli, tmp_path, out)
    _set_goal(out)
    out.write_bytes(out.read_text(encoding="utf-8").replace("\n", "\r\n").encode())
    assert _run(cli, tmp_path, out) == 0
    assert b"\r" not in out.read_bytes()
    assert GOAL in out.read_text(encoding="utf-8")


def test_a_result_file_with_a_lone_surrogate_is_refused_not_crashed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``"\\ud800"`` is valid JSON but has no UTF-8 encoding. It must come out
    as the ordinary exit-1 refusal, with the old report byte-identical - not
    as a ``UnicodeEncodeError`` traceback from inside the write."""
    out = tmp_path / "report.md"
    assert _run(CLIS[0], tmp_path, out) == 0
    _set_goal(out)
    before = out.read_bytes()
    results = tmp_path / "2026-09-20-empty"
    envelope = {
        "schema_version": SCHEMA_VERSION,
        "spike": "S1",
        "topic": "S1-network",
        "link": "ethernet",
        "params": {},
        "cases": {},
        "errors": [],
        "git": {"commit": "abc1234", "dirty": False},
        "host": {"hostname": "host\ud800A", "os": "Windows", "os_release": "11"},
        "timestamp_utc": "2026-09-20T09:00:00+00:00",
        "tool": "test",
    }
    (results / "poisoned.json").write_text(
        json.dumps(envelope, ensure_ascii=True), encoding="utf-8"
    )
    assert (
        s1_network.main(["matrix", "--results", str(results), "--out", str(out)])
        == s1_network.EXIT_IO
    )
    assert out.read_bytes() == before
    err = capsys.readouterr().err
    assert "cannot be encoded" in err and "unchanged" in err
    assert not list(tmp_path.glob("*.tmp")) and not list(tmp_path.glob(".*.tmp"))


# --------------------------------------------------- the real S6 artefacts
def test_regenerating_the_real_s6_report_reproduces_it_byte_for_byte(
    tmp_path: Path,
) -> None:
    """The exact operation that nearly destroyed the S6 write-up: re-running
    ``s6_environments.py report`` over the finished file. It must come back
    identical - hand-written prose included - and the real file in the
    repository must not be touched by this test."""
    original = REAL_S6_REPORT.read_bytes()
    copy = tmp_path / "2026-09-20-S6-environments.md"
    copy.write_bytes(original)
    code = s6_environments.main(
        [
            "report",
            "--results",
            str(REAL_S6_RAW),
            "--out",
            str(copy),
            "--labels",
            "L1=JARVISM3",
        ]
    )
    assert code == 0
    assert copy.read_bytes() == original
    assert REAL_S6_REPORT.read_bytes() == original
