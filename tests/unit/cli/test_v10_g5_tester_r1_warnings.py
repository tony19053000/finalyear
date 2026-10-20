"""V0.10 G5 tester r1: the job-warning surface of the CLI (D283 pin 10)."""

from __future__ import annotations

from typing import Any

import pytest

from meshforge.cli import job_commands, render

pytestmark = [pytest.mark.unit]


def test_warning_line_names_code_count_chunks() -> None:
    lines = render.warning_lines(
        {
            "warnings": [
                {
                    "code": "nondeterministic_chunk",
                    "count": 3,
                    "chunks": [4, 9, 12],
                    "detail": "two results differ",
                }
            ]
        }
    )
    assert len(lines) == 1
    assert "nondeterministic_chunk" in lines[0] and "x3" in lines[0]
    assert "4, 9, 12" in lines[0]


def test_long_chunk_list_is_cut_and_hostile_shapes_are_ignored() -> None:
    lines = render.warning_lines(
        {
            "warnings": [
                {"code": "c", "count": 99, "chunks": list(range(64))},
                "not a mapping",
                7,
                {"code": "d", "count": 1, "chunks": "x"},
            ]
        }
    )
    assert len(lines) == 2 and lines[0].count(",") <= 17 and "..." in lines[0]
    assert (
        render.warning_lines({}) == [] and render.warning_lines({"warnings": 5}) == []
    )


def test_print_warnings_flattens_control_characters(
    capsys: pytest.CaptureFixture[str],
) -> None:
    class Client:
        def job(self, job_id: str) -> dict[str, Any]:
            return {
                "warnings": [
                    {"code": "x\x1b[31m", "count": 1, "detail": "a\r\nforged: line"}
                ]
            }

    job_commands._print_warnings(Client(), "job_x")
    err = capsys.readouterr().err
    assert "\x1b" not in err and "\r" not in err
    assert err.count("\n") == 1


def test_print_warnings_survives_a_failing_read(
    capsys: pytest.CaptureFixture[str],
) -> None:
    class Client:
        def job(self, job_id: str) -> dict[str, Any]:
            raise OSError("down")

    job_commands._print_warnings(Client(), "job_x")
    assert capsys.readouterr().err == ""
