"""V1.0 G9-B item 4: ``compute run --exclude-node <id|name>`` (repeatable).

A node id is taken as is; a name must match exactly one node's display name or
hostname (case-insensitive) - none or several is a usage error, never a guess;
more than 16 is a usage error before anything is uploaded.
"""

from __future__ import annotations

from typing import Any

import pytest
import typer
from typer.testing import CliRunner

from meshforge.cli import job_commands
from meshforge.cli.app import app
from meshforge.common.exit_codes import ExitCode

pytestmark = pytest.mark.unit

N1 = "node_01J00000000000000000000001"
N2 = "node_01J00000000000000000000002"
N3 = "node_01J00000000000000000000003"


class FakeClient:
    def __init__(self) -> None:
        self.calls = 0

    def nodes(self) -> dict[str, Any]:
        self.calls += 1
        return {
            "nodes": [
                {"node_id": N1, "hostname": "JARVISM3", "display_name": "L1"},
                {"node_id": N2, "hostname": "AILESH", "display_name": ""},
                {"node_id": N3, "hostname": "dup", "display_name": "twin"},
                {"node_id": "node_01J00000000000000000000004", "hostname": "twin"},
                "garbage",
            ]
        }


def test_ids_pass_without_a_request_and_names_resolve_once() -> None:
    client = FakeClient()
    assert job_commands._resolve_excluded(client, [N2, N1, N2]) == [N1, N2]
    assert client.calls == 0
    got = job_commands._resolve_excluded(client, ["jarvism3", "ailesh", N3])
    assert got == [N1, N2, N3]
    assert client.calls == 1  # fetched once for both names
    assert job_commands._resolve_excluded(client, [" l1 "]) == [N1]


@pytest.mark.parametrize("value", ["nobody", "twin", "   ", "AILESH\x1b[2J"])
def test_unknown_ambiguous_or_empty_names_are_usage_errors(
    value: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(typer.Exit) as info:
        job_commands._resolve_excluded(FakeClient(), [value])
    assert info.value.exit_code == ExitCode.USAGE_ERROR
    err = capsys.readouterr().err
    assert "\x1b" not in err
    if value == "twin":
        assert "matches 2 nodes" in err


def test_more_than_16_is_refused_before_any_upload() -> None:
    argv = ["run", "."]
    for i in range(17):
        argv += ["--exclude-node", f"n{i}"]
    result = CliRunner().invoke(app, argv)
    assert result.exit_code == ExitCode.USAGE_ERROR, result.output
    assert "at most 16" in result.output
