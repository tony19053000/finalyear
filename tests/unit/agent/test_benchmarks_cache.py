"""G7-A (0.3.4, D34/D35): fingerprint, the scores.json cache and scores.lock."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest
from mf_bench_fakes import make_facts, make_gpu, make_scoreset
from structlog.testing import capture_logs

from meshforge.agent.benchmarks import cache
from meshforge.agent.benchmarks.cache import (
    CacheStatus,
    ScorerBusyError,
    fingerprint,
    fingerprint_inputs,
    load_cache,
    save_cache,
    scorer_lock,
)
from meshforge.agent.hardware.summary import HardwareFacts

pytestmark = pytest.mark.unit

GIB = 2**30
BACKENDS: dict[str, str | None] = {"numpy": "2.5.3", "torch": None}


def _fp(facts: HardwareFacts, backends: dict[str, str | None] = BACKENDS) -> str:
    return fingerprint(fingerprint_inputs(facts, backends))


BASE = make_facts(gpus=(make_gpu(0),))


def _with_cpu(**kw: Any) -> HardwareFacts:
    return dataclasses.replace(BASE, cpu=dataclasses.replace(BASE.cpu, **kw))


def _with_gpu(**kw: Any) -> HardwareFacts:
    return dataclasses.replace(BASE, gpus=(make_gpu(0, **kw),))


# -------------------------------------------------------------- fingerprint
def test_fingerprint_is_deterministic_and_hex() -> None:
    assert _fp(BASE) == _fp(make_facts(gpus=(make_gpu(0),)))
    assert len(_fp(BASE)) == 64 and int(_fp(BASE), 16) >= 0


@pytest.mark.parametrize(
    ("label", "facts", "backends"),
    [
        ("cpu model", _with_cpu(model="Other CPU"), BACKENDS),
        ("cpu threads", _with_cpu(logical_threads=16), BACKENDS),
        ("ram GiB", _with_cpu(ram_bytes=32 * GIB), BACKENDS),
        ("gpu model", _with_gpu(model="NVIDIA RTX A2000"), BACKENDS),
        ("gpu vram", _with_gpu(vram_bytes=6 * GIB), BACKENDS),
        ("gpu driver", _with_gpu(driver_version="600.1"), BACKENDS),
        (
            "gpu added",
            dataclasses.replace(BASE, gpus=(make_gpu(0), make_gpu(1))),
            BACKENDS,
        ),
        ("gpu removed", dataclasses.replace(BASE, gpus=()), BACKENDS),
        ("numpy version", BASE, {"numpy": "2.6.0", "torch": None}),
        ("torch installed", BASE, {"numpy": "2.5.3", "torch": "2.14.0"}),
        ("numpy removed", BASE, {"numpy": None, "torch": None}),
    ],
)
def test_fingerprint_changes_on_each_input(
    label: str, facts: HardwareFacts, backends: dict[str, str | None]
) -> None:
    assert _fp(facts, backends) != _fp(BASE), label


def test_fingerprint_changes_with_score_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = _fp(BASE)
    monkeypatch.setattr(cache, "SCORE_VERSION", 2)
    assert _fp(BASE) != before


def test_ram_is_rounded_to_gib_so_tiny_differences_do_not_rescore() -> None:
    assert _fp(_with_cpu(ram_bytes=16 * GIB + 50 * 2**20)) == _fp(BASE)


def test_backend_versions_never_imports_torch_or_numpy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys

    for name in ("torch", "numpy"):
        monkeypatch.setitem(sys.modules, name, None)
    versions = cache.backend_versions()
    assert set(versions) == {"torch", "numpy"}
    blocked = {name: sys.modules.get(name) for name in ("torch", "numpy")}
    assert blocked == {"torch": None, "numpy": None}


# -------------------------------------------------------------------- cache
def test_cache_round_trip_is_valid(tmp_path: Path) -> None:
    fp = _fp(BASE)
    scores = make_scoreset(fingerprint=fp)
    save_cache(tmp_path, scores, fingerprint_inputs(BASE, BACKENDS))
    state = load_cache(tmp_path, fp)
    assert state.status is CacheStatus.VALID and state.valid
    assert state.scores == scores
    assert state.mtime_ns is not None
    doc = json.loads((tmp_path / "scores.json").read_text(encoding="utf-8"))
    assert doc["fingerprint_inputs"]["cpu_model"] == "Fictional CPU 9000"


def test_missing_cache(tmp_path: Path) -> None:
    assert load_cache(tmp_path, "a" * 64).status is CacheStatus.MISSING


def test_other_fingerprint_is_stale(tmp_path: Path) -> None:
    save_cache(tmp_path, make_scoreset(fingerprint=_fp(BASE)), {})
    state = load_cache(tmp_path, _fp(_with_cpu(model="Upgraded CPU")))
    assert state.status is CacheStatus.STALE and not state.valid
    assert state.scores is not None  # still readable, for display


def test_score_version_bump_makes_the_cache_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fp = _fp(BASE)
    save_cache(tmp_path, make_scoreset(fingerprint=fp), {})
    monkeypatch.setattr(cache, "SCORE_VERSION", 2)
    state = load_cache(tmp_path, fp)
    assert state.status is CacheStatus.STALE
    assert "score_version" in state.reason


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"{not json",
        b"[1, 2]",
        b'{"format": 99, "scores": {}}',
        b'{"format": 1, "scores": {"run_id": "nope"}}',
        b"\xff\xfe\x00garbage",
    ],
)
def test_corrupt_cache_is_logged_and_treated_as_missing(
    tmp_path: Path, content: bytes
) -> None:
    (tmp_path / "scores.json").write_bytes(content)
    with capture_logs() as logs:
        state = load_cache(tmp_path, "a" * 64, node_id="node_x")
    assert state.status is CacheStatus.CORRUPT and state.scores is None
    events = [e for e in logs if e["event"] == "agent.scores.cache_corrupt"]
    assert len(events) == 1
    assert events[0]["log_level"] == "warning"
    assert events[0]["node_id"] == "node_x"
    assert str(tmp_path) not in json.dumps(events[0], default=str)


def test_oversized_cache_is_corrupt_without_reading_it(tmp_path: Path) -> None:
    (tmp_path / "scores.json").write_bytes(b" " * ((1 << 20) + 1))
    with capture_logs():
        assert load_cache(tmp_path, "a" * 64).status is CacheStatus.CORRUPT


def test_save_replaces_atomically_and_leaves_no_temp_file(tmp_path: Path) -> None:
    save_cache(tmp_path, make_scoreset(), {})
    save_cache(tmp_path, make_scoreset(), {})
    assert sorted(p.name for p in tmp_path.iterdir()) == ["scores.json"]


# --------------------------------------------------------------------- lock
def test_second_scorer_is_busy_not_blocked(tmp_path: Path) -> None:
    with scorer_lock(tmp_path), pytest.raises(ScorerBusyError), scorer_lock(tmp_path):
        pass  # pragma: no cover - never entered
    with scorer_lock(tmp_path):  # released: free again
        pass
    assert (tmp_path / "scores.lock").exists()


def test_a_timeout_inside_the_locked_body_is_not_reported_as_busy(
    tmp_path: Path,
) -> None:
    with pytest.raises(TimeoutError), scorer_lock(tmp_path):
        raise TimeoutError("from the body")
