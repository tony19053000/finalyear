"""Unit tests for one-file-per-run spike results (lab bug L2 R1-5/R1-6).

``s7_hardware.py run --quick`` and the full ``run`` used to write the same
file, so the full run destroyed the quick run (and a second full run destroyed
the first). Same for S6, and for S1/S2/S3 re-runs of one peer + link.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from spikes._harness.envelope import (
    ResultCollisionError,
    load_dir,
    make_envelope,
    raw_result_path,
)
from spikes._harness.reps import CaseResult
from spikes._harness.runs import (
    latest_runs,
    parse_stem,
    run_path,
    superseded_block,
    write_run_envelope,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
T0 = datetime(2026, 10, 30, 10, 0, 0, tzinfo=UTC)


def _env(
    host: str = "laptop-1",
    *,
    spike: str = "S7",
    quick: bool | None = None,
    at: datetime = T0,
    value: float = 1.0,
) -> dict[str, Any]:
    case = CaseResult(
        case_id="c1",
        label="c1",
        metrics={"x": "ms"},
        samples=[{"x": value} for _ in range(5)],
        requested_reps=5,
    )
    extra: dict[str, Any] = {} if quick is None else {"quick": quick}
    doc: dict[str, Any] = make_envelope(
        spike=spike,
        topic=f"{spike}-test",
        link=None,
        params={},
        cases=[case],
        extra=extra,
        git={"commit": "abc", "dirty": False},
        host={"hostname": host},
        now=at,
    )
    return doc


def _names(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.iterdir())


# ------------------------------------------------------------------ file names
def test_quick_run_gets_its_own_name(tmp_path: Path) -> None:
    day = date(2026, 10, 30)
    full = raw_result_path("S7-hardware", "L2", day, root=tmp_path)
    quick = raw_result_path("S7-hardware", "L2", day, quick=True, root=tmp_path)
    assert full != quick
    assert quick.name == full.stem + "__quick.json"
    peer = raw_result_path("S1-network", "a", day, suffix="to-b_wifi", quick=True)
    assert peer.name == "a__to-b_wifi__quick.json"


@pytest.mark.parametrize(
    ("stem", "base", "quick", "run"),
    [
        ("l2-1a2b3c4d", "l2-1a2b3c4d", False, 1),
        ("l2__quick", "l2", True, 1),
        ("l2__run2", "l2", False, 2),
        ("l2__quick__run13", "l2", True, 13),
        ("a__to-b_wifi__run3", "a__to-b_wifi", False, 3),
        ("x__run1", "x__run1", False, 1),  # never generated: not a run marker
        ("x__run0", "x__run0", False, 1),
    ],
)
def test_parse_stem(stem: str, base: str, quick: bool, run: int) -> None:
    got = parse_stem(stem)
    assert (got.base, got.quick, got.run) == (base, quick, run)


def test_run_path() -> None:
    p = Path("d/l2__quick.json")
    assert run_path(p, 1) == p
    assert run_path(p, 2) == Path("d/l2__quick__run2.json")


# -------------------------------------------------------------------- writing
def test_second_and_third_run_get_new_names_and_never_overwrite(
    tmp_path: Path,
) -> None:
    path = tmp_path / "raw" / "l2.json"
    first = write_run_envelope(_env(value=1), path)
    first_bytes = first.read_bytes()
    second = write_run_envelope(_env(value=2, at=T0 + timedelta(minutes=1)), path)
    third = write_run_envelope(_env(value=3, at=T0 + timedelta(minutes=2)), path)
    assert (first.name, second.name, third.name) == (
        "l2.json",
        "l2__run2.json",
        "l2__run3.json",
    )
    assert first.read_bytes() == first_bytes  # the first run is untouched
    assert _names(path.parent) == ["l2.json", "l2__run2.json", "l2__run3.json"]


def test_quick_then_full_then_full_keeps_all_three(tmp_path: Path) -> None:
    """The exact L2 sequence: quick, full, full -> three files."""
    day = date(2026, 10, 30)
    quick = raw_result_path("S7-hardware", "l2", day, quick=True, root=tmp_path)
    full = raw_result_path("S7-hardware", "l2", day, root=tmp_path)
    write_run_envelope(_env(quick=True), quick)
    write_run_envelope(_env(quick=False), full)
    write_run_envelope(_env(quick=False), full)
    assert _names(full.parent) == ["l2.json", "l2__quick.json", "l2__run2.json"]
    assert json.loads((full.parent / "l2__quick.json").read_text())["extra"] == {
        "quick": True
    }


def test_other_hosts_file_is_still_refused(tmp_path: Path) -> None:
    path = tmp_path / "x.json"
    write_run_envelope(_env("hostA"), path)
    before = path.read_bytes()
    with pytest.raises(ResultCollisionError, match="hostA"):
        write_run_envelope(_env("hostB"), path)
    assert path.read_bytes() == before
    assert _names(tmp_path) == ["x.json"]


def test_unreadable_existing_file_is_kept_not_replaced(tmp_path: Path) -> None:
    path = tmp_path / "x.json"
    path.write_text("{truncated", encoding="utf-8")
    written = write_run_envelope(_env(), path)
    assert written.name == "x__run2.json"
    assert path.read_text(encoding="utf-8") == "{truncated"


def test_failed_write_leaves_no_reserved_or_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "x.json"
    write_run_envelope(_env(), path)

    def boom(*_a: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(KeyboardInterrupt):
        write_run_envelope(_env(), path)
    assert _names(tmp_path) == ["x.json"]


def test_unencodable_text_reserves_nothing(tmp_path: Path) -> None:
    doc = _env()
    doc["extra"] = {"bad": "\ud800"}
    with pytest.raises(OSError, match="cannot be encoded"):
        write_run_envelope(doc, tmp_path / "x.json")
    assert _names(tmp_path) == []


def test_a_racing_writer_cannot_take_the_same_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another process creates the free name between the probe and the write:
    the O_EXCL reservation moves this run to the next number instead."""
    import spikes._harness.runs as runs

    path = tmp_path / "x.json"
    real = runs._reserve

    def racing(target: Path) -> bool:
        if target == path:
            target.write_text("other process", encoding="utf-8")
        return real(target)

    monkeypatch.setattr(runs, "_reserve", racing)
    assert write_run_envelope(_env(), path).name == "x__run2.json"
    assert path.read_text(encoding="utf-8") == "other process"


# ------------------------------------------------------------------ selection
def _load(folder: Path) -> list[dict[str, Any]]:
    docs, skipped = load_dir(folder)
    assert skipped == []
    return docs


def test_full_run_beats_a_newer_quick_run(tmp_path: Path) -> None:
    write_run_envelope(_env(quick=False, at=T0), tmp_path / "l2.json")
    write_run_envelope(
        _env(quick=True, at=T0 + timedelta(hours=1)), tmp_path / "l2__quick.json"
    )
    chosen = latest_runs(_load(tmp_path))
    assert [d["_source"] for d in chosen.kept] == ["l2.json"]
    assert chosen.superseded == [
        "`l2__quick.json`: superseded by `l2.json` (quick run; kept on disk, "
        "not counted)"
    ]


def test_newest_full_run_wins(tmp_path: Path) -> None:
    path = tmp_path / "l2.json"
    write_run_envelope(_env(at=T0, value=1), path)
    write_run_envelope(_env(at=T0 + timedelta(hours=2), value=2), path)
    write_run_envelope(_env(at=T0 + timedelta(hours=1), value=3), path)
    chosen = latest_runs(_load(tmp_path))
    assert [d["_source"] for d in chosen.kept] == ["l2__run2.json"]
    assert len(chosen.superseded) == 2
    assert all("(older run;" in line for line in chosen.superseded)


def test_same_second_later_run_number_wins(tmp_path: Path) -> None:
    path = tmp_path / "l2.json"
    write_run_envelope(_env(), path)
    write_run_envelope(_env(), path)
    assert [d["_source"] for d in latest_runs(_load(tmp_path)).kept] == [
        "l2__run2.json"
    ]


def test_quick_only_slot_is_kept_and_still_flagged_quick(tmp_path: Path) -> None:
    write_run_envelope(_env(quick=True), tmp_path / "l2__quick.json")
    chosen = latest_runs(_load(tmp_path))
    assert [d["_source"] for d in chosen.kept] == ["l2__quick.json"]
    assert chosen.kept[0]["extra"]["quick"] is True  # spike reports mark it
    assert chosen.superseded == []


def test_old_quick_file_without_suffix_is_detected_by_its_json(
    tmp_path: Path,
) -> None:
    """Files written before this fix carry quick only inside the JSON."""
    (tmp_path / "l2.json").write_text(
        json.dumps(_env(quick=True, at=T0 + timedelta(hours=1))), encoding="utf-8"
    )
    write_run_envelope(_env(quick=False, at=T0), tmp_path / "l2__run2.json")
    chosen = latest_runs(_load(tmp_path))
    assert [d["_source"] for d in chosen.kept] == ["l2__run2.json"]


def test_slots_hosts_and_spikes_are_selected_independently(tmp_path: Path) -> None:
    write_run_envelope(_env("a", spike="S1"), tmp_path / "a__to-b_wifi.json")
    write_run_envelope(_env("a", spike="S1"), tmp_path / "a__to-b_ethernet.json")
    write_run_envelope(_env("a", spike="S1"), tmp_path / "a__to-c_wifi.json")
    write_run_envelope(_env("b", spike="S1"), tmp_path / "b__to-a_wifi.json")
    write_run_envelope(_env("a", spike="S1"), tmp_path / "a__to-b_wifi.json")
    chosen = latest_runs(_load(tmp_path))
    assert sorted(d["_source"] for d in chosen.kept) == [
        "a__to-b_ethernet.json",
        "a__to-b_wifi__run2.json",
        "a__to-c_wifi.json",
        "b__to-a_wifi.json",
    ]
    assert len(chosen.superseded) == 1


def test_envelopes_without_source_are_always_kept() -> None:
    docs = [_env(), _env()]
    assert latest_runs(docs).kept == docs


def test_superseded_block() -> None:
    assert superseded_block([]) == ""
    block = superseded_block(["`a.json`: superseded by `b.json` (older run)"])
    assert "Superseded result files" in block
    assert "- `a.json`: superseded by `b.json` (older run)\n" in block


# -------------------------------------------------------- CLIs (S7 and S6)
def _script(name: str) -> ModuleType:
    path = REPO_ROOT / "lab" / "spikes" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"{name}_runs_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_s7_quick_then_full_twice_keeps_every_file_and_reports_the_last_full(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from spikes._s7.run import HardwareRun

    s7 = _script("s7_hardware")
    clock = iter(T0 + timedelta(minutes=m) for m in range(10))

    def run_scores(cfg: Any, *, log: Any) -> HardwareRun:
        doc = make_envelope(
            spike="S7",
            topic="S7-hardware",
            link=None,
            host_label=cfg.name,
            params={"quick": cfg.profile.quick},
            cases=[],
            extra={"quick": cfg.profile.quick, "warnings": []},
            git={"commit": "abc", "dirty": False},
            host={"hostname": "host-l2"},
            now=next(clock),
        )
        return HardwareRun(envelope=doc, complete=True, warnings=[])

    monkeypatch.setattr(s7, "run_scores", run_scores)
    raw = tmp_path / "2026-10-30-S7-hardware"
    base = ["run", "--out-dir", str(raw), "--name", "L2"]
    assert s7.main([*base, "--quick"]) == s7.EXIT_OK
    assert s7.main(base) == s7.EXIT_OK
    assert s7.main(base) == s7.EXIT_OK
    stem = raw_result_path("S7-hardware", "L2", date(2026, 10, 30)).stem
    assert _names(raw) == [
        f"{stem}.json",
        f"{stem}__quick.json",
        f"{stem}__run2.json",
    ]
    err = capsys.readouterr().err
    assert f"written: {raw / f'{stem}__run2.json'}" in err

    out = tmp_path / "r.md"
    assert s7.main(["report", "--results", str(raw), "--out", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    assert "Superseded result files" in text
    assert f"`{stem}__quick.json`: superseded by `{stem}__run2.json` (quick" in text
    assert f"`{stem}.json`: superseded by `{stem}__run2.json` (older run" in text
    assert "quick (smoke)" not in text  # no quick run reaches the scoreboard
    assert text.count("| L2 | full |") == 1  # one run-conditions row: the last full
    assert "not counted:" in capsys.readouterr().err


def test_s6_quick_and_full_runs_write_distinct_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from spikes._s6.run import EnvironmentRun

    s6 = _script("s6_environments")

    def run_environments(cfg: Any, *, log: Any) -> EnvironmentRun:
        doc = _env("host-l2", spike="S6", quick=cfg.quick)
        doc["host"]["label"] = cfg.name
        return EnvironmentRun(envelope=doc, complete=True, warnings=[])

    monkeypatch.setattr(s6, "run_environments", run_environments)
    base = ["run", "--out-dir", str(tmp_path), "--name", "l2", "--skip-gpu"]
    assert s6.main([*base, "--quick"]) == s6.EXIT_OK
    assert s6.main(base) == s6.EXIT_OK
    assert s6.main(base) == s6.EXIT_OK
    assert _names(tmp_path) == ["l2.json", "l2__quick.json", "l2__run2.json"]
