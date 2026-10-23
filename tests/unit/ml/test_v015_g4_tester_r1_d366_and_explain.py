"""V0.15 G4 tester round 1: D366 (feature_version 1 refused with a hint) and the
explain ML line on hostile stored data.

* a feature_version-1 Parquet file is refused by ``data.load`` AND by the real
  ``python -m meshforge.controller.ml.train compare`` child (exit code 2, the
  hint on stderr naming the rebuild command) - nothing is trained on it;
* feature_version 3 / junk / missing metadata are refused too (no hint for
  them);
* a version-2 file loads; the published fingerprint is (1, 2);
* ``explain``'s ML line never raises on any stored ``detail.ml`` shape (it
  prints the stored record, so it is an injection / crash surface) and never
  prints terminal control characters from a stored version string.
"""

from __future__ import annotations

import io
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("pyarrow", reason="needs the controller extra")
pytest.importorskip("sklearn", reason="needs the ml extra")

from mf_ml_synthetic import parquet_file, synthetic_rows
from rich.console import Console

from meshforge.cli.explain_commands import _ml_note, _term_cell
from meshforge.common.exit_codes import ExitCode
from meshforge.controller.ml.dataset import (
    DATASET_METADATA_KEY,
    FEATURE_METADATA_KEY,
)
from meshforge.controller.ml.schema import FEATURE_VERSION
from meshforge.controller.ml.train.data import (
    KNOWN_VERSIONS,
    DatasetVersionError,
    load,
)

pytestmark = pytest.mark.unit


def _file(tmp_path: Path, name: str = "ds.parquet") -> Path:
    rows = synthetic_rows("signal", families=3, groups_per_family=3, runs_per_group=4)
    return parquet_file(rows, tmp_path / name)


def _rewrite(path: Path, meta: dict[bytes, bytes | None]) -> Path:
    import pyarrow.parquet as pq

    table = pq.read_table(path)
    metadata = dict(table.schema.metadata or {})
    for key, value in meta.items():
        if value is None:
            metadata.pop(key, None)
        else:
            metadata[key] = value
    out = path.with_name("rewritten-" + path.name)
    pq.write_table(table.replace_schema_metadata(metadata), out)
    return out


def test_the_current_feature_version_is_2_and_only_it_is_known() -> None:
    assert FEATURE_VERSION == 2
    assert {fv for _dv, fv in KNOWN_VERSIONS} == {2}


def test_a_version_2_file_loads(tmp_path: Path) -> None:
    assert load(_file(tmp_path)).n > 0


def test_a_feature_version_1_file_is_refused_with_the_rebuild_hint(
    tmp_path: Path,
) -> None:
    old = _rewrite(_file(tmp_path), {FEATURE_METADATA_KEY: b"1"})
    with pytest.raises(DatasetVersionError) as err:
        load(old)
    text = str(err.value)
    assert "feature_version 1" in text and "rebuild" in text
    assert "compute planner ml dataset" in text


@pytest.mark.parametrize("value", [b"3", b"0", b"-1", b"two", b"", b"2.0", b"1 "])
def test_other_versions_are_refused_without_a_misleading_hint(
    tmp_path: Path, value: bytes
) -> None:
    bad = _rewrite(_file(tmp_path), {FEATURE_METADATA_KEY: value})
    with pytest.raises((DatasetVersionError, ValueError)) as err:
        load(bad)
    assert "rebuild it with" not in str(err.value) or value.strip() == b"1"


def test_missing_metadata_is_refused(tmp_path: Path) -> None:
    for key in (FEATURE_METADATA_KEY, DATASET_METADATA_KEY):
        bad = _rewrite(_file(tmp_path), {key: None})
        with pytest.raises((DatasetVersionError, ValueError, KeyError)):
            load(bad)


def test_the_real_compare_child_refuses_a_version_1_file(tmp_path: Path) -> None:
    old = _rewrite(_file(tmp_path), {FEATURE_METADATA_KEY: b"1"})
    out = tmp_path / "out"
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "meshforge.controller.ml.train",
            "compare",
            "--dataset",
            str(old),
            "--out",
            str(out),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == int(ExitCode.USAGE_ERROR), (
        done.returncode,
        done.stderr,
    )
    assert "feature_version 1" in done.stderr + done.stdout
    assert "rebuild" in done.stderr + done.stdout
    assert not (out / "report.json").exists()


# ------------------------------------------------------------ explain hostile


def _cell(block: Any) -> str:
    term = {"seconds": 3.0, "source": "history", "detail": {"ml": block}}
    return _term_cell(term, "compute")


@pytest.mark.parametrize(
    "block",
    [
        None,
        [],
        "x",
        3,
        {},
        {"used": True},
        {"used": True, "y_hat": float("nan"), "lo": 0, "hi": 1, "model_version": "v"},
        {"used": True, "y_hat": float("inf"), "lo": 0, "hi": 1, "model_version": "v"},
        {"used": True, "y_hat": 1e308, "lo": -1e308, "hi": 1e308, "model_version": "v"},
        {"used": True, "y_hat": 51, "lo": 0, "hi": 1, "model_version": "v"},
        {"used": True, "y_hat": 0.1, "lo": 1, "hi": 0, "model_version": "v"},
        {"used": True, "y_hat": True, "lo": 0, "hi": 1, "model_version": "v"},
        {"used": True, "y_hat": "0.1", "lo": "a", "hi": None, "model_version": 5},
        {"used": True, "y_hat": 0.1, "lo": 0, "hi": 1, "raw_s": "bad"},
        {"used": True, "y_hat": 0.1, "lo": 0, "hi": 1, "raw_s": float("nan")},
        {"used": False, "reason": 5},
        {"used": False, "reason": None, "ood_reason": 7},
        {"used": False, "reason": "ood", "ood_reason": "x" * 10_000},
        {"used": "yes", "model_version": "v"},
        {
            "used": False,
            "reason": "ood\x1b[31m RED",
            "ood_reason": "\x07\x1b]0;pwn\x07",
        },
        {
            "used": True,
            "model_version": "\x1b[2J\x1b[Hv" * 3,
            "y_hat": 0,
            "lo": 0,
            "hi": 0,
        },
    ],
)
def test_the_ml_note_never_raises_on_any_stored_shape(block: Any) -> None:
    note = _ml_note({"detail": {"ml": block}})
    assert isinstance(note, str)
    cell = _cell(block)
    assert isinstance(cell, str)


def _rendered(tables: list[Any]) -> str:
    console = Console(
        file=io.StringIO(),
        width=400,
        height=50,
        force_terminal=False,
        color_system=None,
        no_color=True,
        markup=True,
        highlight=True,
        emoji=True,
        legacy_windows=False,
    )
    for table in tables:
        console.print(table)
    buffer = console.file
    assert isinstance(buffer, io.StringIO)
    return buffer.getvalue()


@pytest.mark.parametrize(
    "block",
    [
        {
            "used": False,
            "reason": "ood\x1b[31m",
            "ood_reason": "\x1b]0;PWNED\x07",
            "model_version": "\x1b[2J",
        },
        {
            "used": True,
            "model_version": "\x1b[2J\x1b[H\r\nFAKE ROW",
            "y_hat": 0.1,
            "lo": 0.0,
            "hi": 0.2,
            "raw_s": 1.0,
        },
        {"used": False, "reason": "[bold red]x[/]", "ood_reason": "[link=x]y[/link]"},
    ],
)
def test_control_characters_in_a_stored_record_do_not_reach_the_terminal(
    block: dict[str, Any],
) -> None:
    """A stored ``detail.ml`` is data from the database: whatever it holds,
    the rendered ``explain`` tables carry no ESC / BEL / CR from it, and
    markup in it is shown, not interpreted."""
    import copy
    import json

    from mf_explain_contract import FIXTURE

    from meshforge.cli.explain_commands import decision_tables

    payload = copy.deepcopy(json.loads(FIXTURE.read_text(encoding="utf-8"))["payload"])
    n = 0
    for decision in payload["decisions"]:
        for row in decision["candidates"]:
            for term in ((row.get("estimate") or {}).get("terms") or {}).values():
                term["detail"]["ml"] = copy.deepcopy(block)
                n += 1
    assert n
    text = _rendered(decision_tables(payload))
    for char in ("\x1b", "\x07", "\r", "\x00", "\b", "\v"):
        assert char not in text, (char, block)
