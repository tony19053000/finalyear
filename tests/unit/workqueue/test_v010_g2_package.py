"""V0.10 G2 (D262): the work-queue package stays generic and its tables match
the controller's conventions."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

PACKAGE = Path(__file__).resolve().parents[3] / "src" / "meshforge" / "workqueue"

# Words of the callers (Mode 3 splitting, Mode 4 batching, jobs/nodes). The queue
# must not know any of them - Mode 4 reuses it unchanged (brief §5b "Done when").
CALLER_WORDS = re.compile(
    r"chunk|lane|split|mode ?[1-4]|combine|batch|\bjob|\bnode|attempt_id|planner",
    re.IGNORECASE,
)


def _sources() -> list[Path]:
    files = sorted(PACKAGE.glob("*.py"))
    assert {f.name for f in files} >= {
        "__init__.py",
        "core.py",
        "store.py",
        "tables.py",
    }
    return files


def test_no_caller_vocabulary_in_the_package() -> None:
    hits = [
        f"{path.name}:{n}: {line.strip()}"
        for path in _sources()
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if CALLER_WORDS.search(line)
    ]
    assert not hits, "\n".join(hits)


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
    return out


def test_core_is_pure_stdlib() -> None:
    allowed = {
        "__future__",
        "math",
        "collections.abc",
        "dataclasses",
        "datetime",
        "enum",
        "typing",
    }
    assert _imports(PACKAGE / "core.py") <= allowed


def test_package_init_imports_nothing() -> None:
    assert _imports(PACKAGE / "__init__.py") == set()


def test_naming_convention_equals_the_controllers() -> None:
    pytest.importorskip("sqlalchemy", reason="needs the controller extra")
    from meshforge.controller.store.base import NAMING_CONVENTION
    from meshforge.workqueue.tables import METADATA
    from meshforge.workqueue.tables import NAMING_CONVENTION as WQ_CONVENTION

    assert WQ_CONVENTION == NAMING_CONVENTION
    assert METADATA.naming_convention == NAMING_CONVENTION
    assert set(METADATA.tables) == {"work_queue", "work_item", "work_lease"}


def test_state_checks_list_every_enum_value() -> None:
    pytest.importorskip("sqlalchemy", reason="needs the controller extra")
    from sqlalchemy import CheckConstraint

    from meshforge.workqueue.core import ItemState, LeaseState, QueueState
    from meshforge.workqueue.tables import work_item, work_lease, work_queue

    for table, enum in (
        (work_queue, QueueState),
        (work_item, ItemState),
        (work_lease, LeaseState),
    ):
        checks = [
            str(c.sqltext)
            for c in table.constraints
            if isinstance(c, CheckConstraint) and c.name == f"ck_{table.name}_state"
        ]
        assert len(checks) == 1
        for value in enum:
            assert f"'{value.value}'" in checks[0]


def test_migration_0025_frozen_states_match_the_enums() -> None:
    pytest.importorskip("alembic", reason="needs the controller extra")
    import importlib.util

    from meshforge.workqueue.core import ItemState, LeaseState, QueueState

    path = (
        PACKAGE.parent
        / "controller"
        / "store"
        / "migrations"
        / "versions"
        / "0025_work_queue.py"
    )
    spec = importlib.util.spec_from_file_location("mig_0025", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for frozen, enum in (
        (module.QUEUE_STATES_0025, QueueState),
        (module.ITEM_STATES_0025, ItemState),
        (module.LEASE_STATES_0025, LeaseState),
    ):
        assert frozen == ", ".join(f"'{v.value}'" for v in enum)
