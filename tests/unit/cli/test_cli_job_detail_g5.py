"""V0.5 G5: ``compute jobs JOB_ID`` shows named inputs and transfer telemetry,
and controller text cannot drive the terminal."""

from __future__ import annotations

import io

import pytest
from rich.console import Console

from meshforge.cli.render import job_detail_table

pytestmark = [pytest.mark.unit]

TREE = "sha256:" + "c" * 64


def _render(payload: dict[str, object]) -> str:
    out = io.StringIO()
    Console(file=out, width=400, force_terminal=True, color_system=None).print(
        job_detail_table(payload)
    )
    return out.getvalue()


def test_inputs_and_transfers_are_shown() -> None:
    text = _render(
        {
            "job_id": "job_x",
            "name": "n",
            "state": "succeeded",
            "attempts": [
                {
                    "attempt_id": "att_x",
                    "node_id": "node_b",
                    "state": "done",
                    "transfers": [
                        {
                            "tree_id": TREE,
                            "source": "cache",
                            "bytes": 1048576,
                            "duration_ms": 1500,
                            "units": 3,
                            "failures": 0,
                            "resumed_chunks": 2,
                        },
                        {
                            "tree_id": TREE,
                            "source": "node_a",
                            "bytes": 2097152,
                            "duration_ms": 250,
                            "units": 4,
                            "failures": 1,
                            "dropped": "timeout",
                        },
                    ],
                }
            ],
            "inputs": [
                {"ref": "ds@2", "mount": "data/ds", "tree_id": TREE, "name": "ds"}
            ],
        }
    )
    assert "cache hits=3 (1.0 MiB) staged in 1.50 s" in text
    assert "resumed_chunks=2" in text
    assert "from node_a: 2.0 MiB in 0.25 s, units=4" in text
    assert "dropped=timeout" in text
    assert "ds@2 -> data/ds" in text


def test_hostile_text_in_transfers_is_neutralised() -> None:
    text = _render(
        {
            "job_id": "job_x",
            "attempts": [
                {
                    "attempt_id": "a",
                    "transfers": [
                        {"source": "\x1b]0;pwned\x07evil", "bytes": "x", "units": 1}
                    ],
                }
            ],
            "inputs": [{"ref": "\x1b[2Jx", "mount": "m"}],
        }
    )
    assert "\x1b]" not in text and "\x07" not in text and "\x1b[2J" not in text
