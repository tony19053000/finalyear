"""Unit tests for the spike result envelope (schema, determinism, privacy, paths)."""

from __future__ import annotations

import hashlib
import json
import math
import os
import unicodedata
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from spikes._harness.atomic import atomic_write_text
from spikes._harness.envelope import (
    SCHEMA_VERSION,
    EnvelopeError,
    ResultCollisionError,
    dumps,
    host_label,
    load_dir,
    load_envelope,
    make_envelope,
    raw_dir,
    raw_result_path,
    safe_name,
    safe_topic,
    validate,
    write_envelope,
)
from spikes._harness.reps import CaseResult

NOW = datetime(2026, 9, 16, 10, 0, 0, tzinfo=UTC)
GIT = {"commit": "abc1234", "dirty": False}
HOST = {"hostname": "laptop-1", "os": "Linux", "os_release": "6.8"}


def _case(
    case_id: str = "c1", values: tuple[float, ...] = (1, 2, 3, 4, 5)
) -> CaseResult:
    return CaseResult(
        case_id=case_id,
        label=f"label {case_id}",
        metrics={"x": "ms"},
        samples=[{"x": v} for v in values],
        requested_reps=len(values),
    )


def _env(**kw: object) -> dict[str, object]:
    args: dict[str, object] = {
        "spike": "S9",
        "topic": "S9-test",
        "link": "wifi",
        "params": {"a": 1},
        "cases": [_case()],
        "git": GIT,
        "host": HOST,
        "now": NOW,
    }
    args.update(kw)
    return make_envelope(**args)  # type: ignore[arg-type]


def test_envelope_shape() -> None:
    doc = _env()
    assert doc["schema_version"] == SCHEMA_VERSION
    assert doc["timestamp_utc"] == "2026-09-16T10:00:00Z"
    assert doc["git"] == GIT
    assert doc["host"]["hostname"] == "laptop-1"  # type: ignore[index]
    assert doc["host"]["label"] == "laptop-1"  # type: ignore[index]
    case = doc["cases"]["c1"]  # type: ignore[index]
    assert case["samples"] == [{"x": v} for v in (1, 2, 3, 4, 5)]
    assert case["summary"]["x"]["n"] == 5
    assert case["summary"]["x"]["mean"] == pytest.approx(3.0)
    assert validate(doc) is doc


def test_timestamp_is_converted_to_utc() -> None:
    ist = timezone(timedelta(hours=5, minutes=30))
    doc = _env(now=datetime(2026, 9, 16, 15, 30, tzinfo=ist))
    assert doc["timestamp_utc"] == "2026-09-16T10:00:00Z"


def test_host_label() -> None:
    doc = _env(host_label="L1")
    assert host_label(doc) == "L1"
    assert host_label({"host": {"hostname": "h"}}) == "h"


def test_bad_link_and_duplicate_cases_rejected() -> None:
    with pytest.raises(ValueError, match="link"):
        _env(link="lan")
    with pytest.raises(ValueError, match="duplicate"):
        _env(cases=[_case("same"), _case("same")])


def test_dumps_is_deterministic_and_strict() -> None:
    a = _env()
    b = json.loads(json.dumps(a))
    b = dict(reversed(list(b.items())))
    assert dumps(a) == dumps(b)
    text = dumps(a)
    assert text.endswith("\n")
    assert list(json.loads(text)) == sorted(json.loads(text))
    with pytest.raises(ValueError):
        dumps({"x": math.nan})


def test_sanitizer_removes_home_and_public_ips() -> None:
    home = Path.home()
    doc = _env(
        params={
            "path": str(home / "repo" / "x.json"),
            "peer": "8.8.8.8",
            "lan": "192.168.1.9",
        }
    )
    text = dumps(doc)
    assert str(home) not in text
    assert "8.8.8.8" not in text and "<public-ip>" in text
    assert "192.168.1.9" in text  # private addresses are kept on purpose


def _h8(text: str) -> str:
    return hashlib.sha256(unicodedata.normalize("NFC", text).encode()).hexdigest()[:8]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # already lower-case and safe: unchanged
        ("desktop-ab12", "desktop-ab12"),
        ("host.local", "host.local"),
        ("", "unknown"),
        # case-folded (NTFS is case-insensitive), so the name changed -> suffix
        ("DESKTOP-AB12", f"desktop-ab12-{_h8('DESKTOP-AB12')}"),
        ("a/b\\c:D", f"a_b_c_d-{_h8('a/b\\c:D')}"),
        ("../..", f"unknown-{_h8('../..')}"),
        ("host name", f"host_name-{_h8('host name')}"),
        ("ラップトップ", f"unknown-{_h8('ラップトップ')}"),
    ],
)
def test_safe_name(text: str, expected: str) -> None:
    assert safe_name(text) == expected


def test_safe_name_hash_is_nfc_stable() -> None:
    composed, decomposed = "café", "café"
    assert safe_name(composed) == safe_name(decomposed)
    # a changed name never equals an unchanged one
    assert safe_name("host_name") != safe_name("host name")
    # case-only differences survive on a case-insensitive filesystem
    assert safe_name("Lab1").casefold() != safe_name("lab1").casefold()


def test_topic_directory_keeps_its_case(tmp_path: Path) -> None:
    """Topics come from code, not from hosts, so the documented mixed-case
    directory name (``YYYY-MM-DD-S1-network``) is kept as written."""
    assert safe_topic("S1-network") == "S1-network"
    assert raw_dir("S1-network", date(2026, 9, 20), root=tmp_path).name == (
        "2026-09-20-S1-network"
    )
    assert safe_topic("S1 network/x") == f"S1_network_x-{_h8('S1 network/x')}"


def test_raw_result_path(tmp_path: Path) -> None:
    path = raw_result_path(
        "S1-network", "laptop 1", date(2026, 9, 16), suffix="to-b_wifi", root=tmp_path
    )
    expected = f"laptop_1-{_h8('laptop 1')}__to-b_wifi.json"
    assert path == tmp_path / "2026-09-16-S1-network" / expected
    assert raw_result_path("t", "h", date(2026, 1, 2), root=tmp_path).name == "h.json"


def _env_for(hostname: str) -> dict[str, Any]:
    doc: dict[str, Any] = dict(_env())
    doc["host"] = {**HOST, "hostname": hostname}
    return doc


def test_write_envelope_refuses_other_hosts_file(tmp_path: Path) -> None:
    path = tmp_path / "x.json"
    write_envelope(_env_for("hostA"), path)
    before = path.read_bytes()
    with pytest.raises(ResultCollisionError, match="hostA"):
        write_envelope(_env_for("hostB"), path)
    assert path.read_bytes() == before
    assert isinstance(ResultCollisionError("x"), OSError)  # CLI maps it to EXIT_IO


def test_write_envelope_same_host_rerun_and_garbage_replaced(tmp_path: Path) -> None:
    path = tmp_path / "x.json"
    write_envelope(_env_for("hostA"), path)
    write_envelope(_env_for("hostA"), path)  # re-run on the same host is fine
    path.write_text("{truncated", encoding="utf-8")
    write_envelope(_env_for("hostB"), path)  # an unreadable file holds no result
    assert load_envelope(path)["host"]["hostname"] == "hostB"
    assert [p.name for p in tmp_path.iterdir()] == ["x.json"]  # no temp files left


def test_atomic_write_leaves_target_and_no_temp_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "x.json"
    atomic_write_text(path, "old\n")

    def boom(*_a: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(KeyboardInterrupt):
        atomic_write_text(path, "new\n")
    assert path.read_text(encoding="utf-8") == "old\n"
    assert [p.name for p in tmp_path.iterdir()] == ["x.json"]


def test_write_load_round_trip(tmp_path: Path) -> None:
    doc = _env()
    path = tmp_path / "sub dir ü" / "x.json"
    write_envelope(doc, path)
    assert load_envelope(path) == doc
    assert path.read_bytes().count(b"\r\n") == 0  # LF on every OS


def test_load_dir_skips_invalid_files(tmp_path: Path) -> None:
    write_envelope(_env(), tmp_path / "b_good.json")
    (tmp_path / "a_broken.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "c_list.json").write_text("[]", encoding="utf-8")
    old = json.loads(dumps(_env()))
    old["schema_version"] = 99
    (tmp_path / "d_old.json").write_text(json.dumps(old), encoding="utf-8")
    partial = json.loads(dumps(_env()))
    del partial["cases"]
    (tmp_path / "e_partial.json").write_text(json.dumps(partial), encoding="utf-8")
    (tmp_path / "notes.txt").write_text("ignored", encoding="utf-8")
    docs, skipped = load_dir(tmp_path)
    assert [d["_source"] for d in docs] == ["b_good.json"]
    assert len(skipped) == 4
    joined = "\n".join(skipped)
    for reason in (
        "invalid JSON",
        "not a JSON object",
        "schema_version",
        "missing keys",
    ):
        assert reason in joined


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda d: d.update(link="bluetooth"), "link"),
        (lambda d: d.update(cases=[]), "must be objects"),
        (lambda d: d.update(host={"os": "x"}), "hostname"),
    ],
)
def test_validate_errors(mutate: object, match: str) -> None:
    doc = json.loads(dumps(_env()))
    mutate(doc)  # type: ignore[operator]
    with pytest.raises(EnvelopeError, match=match):
        validate(doc)
