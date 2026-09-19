"""Tester-added adversarial/edge-case tests for spike S1 + shared harness (0.2.5).

Targets gaps not covered by the implementer's own unit tests: atomic writes
under interruption, unicode-hostname filename collisions, the exact n=4/n=5
INVALID boundary, and report/matrix determinism (byte-identical output for
byte-identical input). Never modifies ``src``/``lab`` production code; failing
assertions here are reported as defects, not weakened.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from spikes._harness.envelope import (
    make_envelope,
    raw_result_path,
    safe_name,
    write_envelope,
)
from spikes._harness.reps import CaseResult
from spikes._s1.matrix import cell, render_matrix

NOW = datetime(2026, 9, 20, 9, 0, tzinfo=UTC)


def _case(values: list[float]) -> CaseResult:
    return CaseResult(
        case_id="tcp-upload-x1",
        label="TCP",
        metrics={"receiver_mbps": "Mbit/s"},
        attrs={
            "kind": "tcp",
            "direction": "upload",
            "streams": 1,
            "sender": "A",
            "receiver": "B",
        },
        samples=[{"receiver_mbps": v} for v in values],
    )


def _env(host: str, values: list[float]) -> dict:  # type: ignore[type-arg]
    return make_envelope(
        spike="S1",
        topic="S1-network",
        link="ethernet",
        params={"client_name": host, "server_name": "B"},
        cases=[_case(values)],
        git={"commit": "abc", "dirty": False},
        host={"hostname": host},
        host_label=host,
        now=NOW,
    )


# --------------------------------------------------------- atomic write
def _leftover_tmp_files(directory: Path) -> list[Path]:
    return [p for p in directory.iterdir() if p.suffix == ".tmp"]


def _patch_flaky_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """Interrupt while writing the temp file's payload (partial bytes on disk)."""
    real_fdopen = os.fdopen

    def fake_fdopen(fd: int, mode: str = "r", *args: object, **kwargs: object) -> Any:
        fh: Any = real_fdopen(fd, mode, *args, **kwargs)  # type: ignore[call-overload]
        real_write = fh.write

        def flaky_write(data: str) -> int:
            real_write(data[: len(data) // 2])
            fh.flush()
            raise KeyboardInterrupt

        fh.write = flaky_write
        return fh

    monkeypatch.setattr(os, "fdopen", fake_fdopen)


def _patch_flaky_fsync(monkeypatch: pytest.MonkeyPatch) -> None:
    """Interrupt right after the payload is written but before fsync completes."""

    def flaky_fsync(fd: int) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(os, "fsync", flaky_fsync)


def _patch_flaky_replace(monkeypatch: pytest.MonkeyPatch) -> None:
    """Interrupt after the temp file is fully written+fsynced, before the swap."""

    def flaky_replace(src: object, dst: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(os, "replace", flaky_replace)


_STAGES = {
    "write": _patch_flaky_write,
    "fsync": _patch_flaky_fsync,
    "replace": _patch_flaky_replace,
}


@pytest.mark.parametrize("stage", sorted(_STAGES))
def test_write_envelope_is_atomic_under_interruption(
    stage: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Success criterion 4/robustness: 'no partial corrupted JSON' on SIGINT mid-write.

    Tests the atomic-write *property*, not the old in-place-write mechanism:
    a ``KeyboardInterrupt`` is injected at each stage the real writer goes
    through (temp-file write, fsync, ``os.replace``). In every case the
    interrupt must propagate, the target file must be either absent or still
    hold its previous fully-valid content (never truncated/corrupt), and no
    ``.tmp`` scratch file must be left behind in the target's directory.
    """
    doc = _env("hostA", [1.0, 2.0, 3.0, 4.0, 5.0])
    path = tmp_path / "out.json"
    old_text = '{"previous": "valid content"}\n'
    path.write_text(old_text, encoding="utf-8")

    _STAGES[stage](monkeypatch)
    with pytest.raises(KeyboardInterrupt):
        write_envelope(doc, path)

    assert path.exists(), "the previous valid file must not be deleted"
    text = path.read_text(encoding="utf-8")
    json.loads(text)  # must not raise: never left truncated/corrupt
    assert text == old_text, "an interrupted write must not touch the old file"
    assert _leftover_tmp_files(tmp_path) == [], "temp file was not cleaned up"


@pytest.mark.parametrize("stage", sorted(_STAGES))
def test_write_envelope_is_atomic_under_interruption_with_no_prior_file(
    stage: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same as above, but there is no pre-existing file: the target must stay
    absent (not appear half-written) and no temp file must remain."""
    doc = _env("hostA", [1.0, 2.0, 3.0, 4.0, 5.0])
    path = tmp_path / "out.json"

    _STAGES[stage](monkeypatch)
    with pytest.raises(KeyboardInterrupt):
        write_envelope(doc, path)

    assert not path.exists(), "no partial file may appear when none existed before"
    assert _leftover_tmp_files(tmp_path) == [], "temp file was not cleaned up"


# --------------------------------------------------------- unicode hostnames
def test_unicode_hostnames_with_no_ascii_collide_in_safe_name() -> None:
    """Two distinct non-ASCII hostnames must not collapse to the same file name.

    ``safe_name`` strips everything outside ``[A-Za-z0-9._-]``, so two hosts
    whose names are entirely non-ASCII both become ``"unknown"``. In the real
    lab workflow, every laptop's result file lands in the *same* directory
    before ``matrix`` runs, so this is a silent file collision (one host's
    results overwrite another's), not just a display quirk.
    """
    host_a = "ラップトップ"
    host_b = "笔记本"
    name_a, name_b = safe_name(host_a), safe_name(host_b)
    # DEFECT: both become "unknown" -> raw_result_path collides for two
    # different real hosts, silently dropping one machine's measurements.
    assert name_a != name_b, (
        f"{host_a!r} and {host_b!r} both sanitise to {name_a!r}; "
        "their result files would overwrite each other"
    )


def test_unicode_hostname_result_paths_do_not_collide() -> None:
    from datetime import date

    path_a = raw_result_path("S1-network", "ラップトップ", date(2026, 9, 20))
    path_b = raw_result_path("S1-network", "笔记本", date(2026, 9, 20))
    assert path_a != path_b


# --------------------------------------------------------- n=4 vs n=5 boundary
def test_invalid_threshold_is_exactly_at_n4_n5() -> None:
    four = [10.0, 20.0, 30.0, 40.0]
    five = [10.0, 20.0, 30.0, 40.0, 50.0]
    assert cell(four).startswith("INVALID (n=4)")
    assert not cell(five).startswith("INVALID")
    assert "(n=5)" in cell(five)


def test_matrix_row_invalid_exactly_at_n4() -> None:
    env4 = _env("A", [1.0, 2.0, 3.0, 4.0])
    md4 = render_matrix([env4])
    assert "INVALID (n=4)" in md4
    env5 = _env("A", [1.0, 2.0, 3.0, 4.0, 5.0])
    md5 = render_matrix([env5])
    assert "INVALID (n=" not in md5


# --------------------------------------------------------- determinism
def test_matrix_report_is_byte_identical_for_identical_input(tmp_path: Path) -> None:
    from spikes._harness.report import TEMPLATE_PATH, render_report

    env = _env("A", [1.0, 2.0, 3.0, 4.0, 5.0])
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    kwargs = {
        "topic": "S1-network",
        "title": "S1 network matrix",
        "day": "2026-09-20",
        "task": "0.2.5",
        "spike": "S1",
        "extra_results": render_matrix([env]),
    }
    first = render_report(template, [env], **kwargs)
    second = render_report(template, [dict(env)], **kwargs)
    assert first == second
