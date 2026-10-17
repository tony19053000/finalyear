"""V0.9 G2 (0.9.2 item 5): ``compute bench workloads [--json]``."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from meshforge.bench.workloads import list_families
from meshforge.cli import app as cli_app
from meshforge.cli import shared
from meshforge.cli.bench_commands import params_summary
from meshforge.cli.render import JSON_SCHEMA_VERSION

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def _no_controller(monkeypatch: pytest.MonkeyPatch) -> None:
    """The command is local: building a controller client is a bug."""

    def refuse(*_a: object) -> None:
        raise AssertionError("compute bench workloads must not call the controller")

    monkeypatch.setattr(shared, "_client", refuse)
    monkeypatch.setattr(shared, "_settings", refuse)


def test_json_is_one_sorted_line_with_every_family(runner: CliRunner) -> None:
    result = runner.invoke(cli_app.app, ["bench", "workloads", "--json"])
    assert result.exit_code == 0, result.output
    assert result.stdout.count("\n") == 1
    document = json.loads(result.stdout)
    assert (
        result.stdout
        == json.dumps(document, separators=(",", ":"), sort_keys=True) + "\n"
    )
    assert document["schema_version"] == JSON_SCHEMA_VERSION
    assert document["count"] == len(list_families()) == 15
    names = [f["name"] for f in document["families"]]
    assert names == sorted(names)
    assert document["runtime_window_s"] == [5.0, 180.0]
    assert document["reference_hw"]["cpu_fp32_gflops_all"] > 0
    for family in document["families"]:
        assert set(family) >= {
            "name",
            "kind",
            "description",
            "env_base",
            "gpu",
            "size_params",
            "params_schema",
            "smallest",
            "smallest_hint_s",
            "runtime_window_s",
        }
        assert family["params_schema"]["additionalProperties"] is False
    assert result.stderr == ""


def _has(document: object, path: str) -> bool:
    head, _, rest = path.partition(".")
    name = head.removesuffix("[]")
    if not isinstance(document, dict) or name not in document:
        return False
    value = document[name]
    if head.endswith("[]"):
        return isinstance(value, list) and all(
            _has(item, rest) if rest else True for item in value
        )
    return _has(value, rest) if rest else True


def test_the_documented_json_fields_are_present(runner: CliRunner) -> None:
    """docs/CLI.md section 3 lists the guaranteed fields of this document."""
    text = (REPO / "docs" / "CLI.md").read_text(encoding="utf-8")
    row = next(
        line
        for line in text.splitlines()
        if line.startswith("| `compute bench workloads` | built by the CLI |")
    )
    fields = re.findall(r"`([^`]+)`", row.split("| built by the CLI |", 1)[1])
    assert len(fields) >= 10
    document = json.loads(
        runner.invoke(cli_app.app, ["bench", "workloads", "--json"]).stdout
    )
    missing = [f for f in fields if not _has(document, f)]
    assert missing == []


def test_table_lists_every_family_and_the_window(runner: CliRunner) -> None:
    result = runner.invoke(
        cli_app.app, ["bench", "workloads"], env={"COLUMNS": "160", "NO_COLOR": "1"}
    )
    assert result.exit_code == 0, result.output
    for family in list_families():
        assert family.name in result.stdout
    assert "5-180 s" in result.stdout
    assert "\x1b[" not in result.stdout


def test_bench_without_a_subcommand_shows_help(runner: CliRunner) -> None:
    result = runner.invoke(cli_app.app, ["bench"])
    assert "workloads" in result.output


def test_params_summary_formats_ranges_choices_and_defaults() -> None:
    schema = {
        "properties": {
            "n": {"type": "integer", "minimum": 256, "maximum": 4096, "default": 2048},
            "dtype": {"enum": ["float64", "float32"], "default": "float64"},
            "level": {"enum": [1, 6, 9], "default": 6},
        }
    }
    assert params_summary(schema) == (
        "n 256..4_096 (2_048)\ndtype float64|float32 (float64)\nlevel 1|6|9 (6)"
    )
