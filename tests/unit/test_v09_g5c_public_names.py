"""V0.9 G5c item 0.1 (D248 N2/N5): no module reaches into another's private names.

* ``telemetry.export`` publishes ``Chunks`` / ``ARROW_TYPES`` and
  ``labels_export`` imports those public names;
* no ``meshforge.cli`` bench command module imports an ``_underscore`` name from
  another *command* module (the shared helpers live in ``cli/shared.py``,
  used as ``cli._name`` - its documented convention).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SRC = Path(__file__).resolve().parents[2] / "src" / "meshforge"


def _private_imports(path: Path) -> list[tuple[str, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                if alias.name.startswith("_") and not alias.name.startswith("__"):
                    out.append((node.module, alias.name))
    return out


def test_labels_export_uses_the_public_export_names() -> None:
    pytest.importorskip("pyarrow")
    from meshforge.controller.telemetry import export, labels_export

    assert "Chunks" in export.__all__ and "ARROW_TYPES" in export.__all__
    assert not hasattr(export, "_Chunks") and not hasattr(export, "_ARROW_TYPES")
    assert labels_export.Chunks is export.Chunks
    assert labels_export.ARROW_TYPES is export.ARROW_TYPES
    leaks = [
        (m, n)
        for m, n in _private_imports(SRC / "controller/telemetry/labels_export.py")
        if m.endswith("telemetry.export")
    ]
    assert leaks == []


def test_no_bench_command_module_imports_a_private_name_of_another() -> None:
    # bench_* only: explain_commands' _checked_job_id import predates G5c
    # (a follow-up, not this item)
    offenders = []
    for path in sorted((SRC / "cli").glob("bench_*_commands.py")):
        for module, name in _private_imports(path):
            if module.startswith("meshforge.cli.") and module.endswith("_commands"):
                offenders.append((path.name, module, name))
    assert offenders == []


def test_the_campaign_id_check_lives_in_shared() -> None:
    import typer

    from meshforge.cli import shared

    good = "camp_01M59T2Q4M40RXWQB9QQC603M1"
    assert shared._checked_campaign_id(good) == good
    with pytest.raises(typer.Exit) as exc:
        shared._checked_campaign_id("../healthz")
    assert exc.value.exit_code == 2
