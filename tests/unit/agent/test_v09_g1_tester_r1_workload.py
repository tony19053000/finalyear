"""V0.9 G1 tester round 1: results/workload.json is hostile input.

``test_defect_*`` are strict xfails documenting defects (see the tester report).
"""

from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from meshforge.agent.execution.workload_result import (
    MAX_RESULT_BYTES,
    read_workload_result,
)
from meshforge.common.secret_redaction import SecretMatcher

pytestmark = pytest.mark.unit

GOOD: dict[str, Any] = {
    "schema_version": 1,
    "family": "cpu.matmul",
    "params": {"n": 8},
    "seed": 1,
    "status": "ok",
    "device": "cpu",
    "compute_ns": 5,
}


def _put(workdir: Path, doc: object = None, *, raw: bytes | None = None) -> Path:
    target = workdir / "results" / "workload.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(raw if raw is not None else json.dumps(doc).encode())
    return target


@pytest.mark.parametrize(
    "status",
    [5, None, True, "OK", ""],
    ids=["int", "null", "bool", "upper", "empty"],
)
def test_a_non_string_status_is_no_report_not_an_exception(
    tmp_path: Path, status: Any
) -> None:
    """``status not in frozenset`` raises TypeError for an unhashable value; the
    read runs in the executor with no try/except around it."""
    _put(tmp_path, {**GOOD, "status": status})
    assert read_workload_result(tmp_path) is None


@pytest.mark.parametrize("status", [["ok"], {"a": 1}], ids=["list", "dict"])
def test_defect_4_an_unhashable_status_is_no_report_not_an_exception(
    tmp_path: Path, status: Any
) -> None:
    _put(tmp_path, {**GOOD, "status": status})
    assert read_workload_result(tmp_path) is None


@pytest.mark.parametrize(
    "field",
    ["family", "device", "params", "seed", "compute_ns", "schema_version"],
)
@pytest.mark.parametrize(
    "value",
    [[1], {"x": 1}, -1, 2**200, 1e300, float("nan"), True, "x" * 100_000, None, ""],
    ids=[
        "list",
        "dict",
        "neg",
        "huge",
        "1e300",
        "nan",
        "bool",
        "long",
        "null",
        "empty",
    ],
)
def test_wrong_typed_fields_never_raise_and_never_pass_through_unbounded(
    tmp_path: Path, field: str, value: Any
) -> None:
    _put(tmp_path, {**GOOD, field: value})
    report = read_workload_result(tmp_path)
    assert report is not None
    assert len(report.family) <= 64 and len(report.device) <= 64
    assert len(report.params_json) <= 4096
    assert report.compute_ns is None or 0 <= report.compute_ns <= 8 * 86400 * 10**9
    assert report.seed is None or 0 <= report.seed < 2**64
    proto = report.to_proto()  # must serialise
    proto.SerializeToString()
    if report.params_json:
        json.loads(report.params_json, parse_constant=lambda n: 1 / 0)


@pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_json_constants_in_params_are_dropped(
    tmp_path: Path, token: str
) -> None:
    _put(tmp_path, raw=b'{"status":"ok","params":{"x":%s}}' % token.encode())
    report = read_workload_result(tmp_path)
    assert report is not None and report.params_json == ""


def test_a_file_one_byte_over_the_cap_is_refused_and_at_the_cap_is_bounded(
    tmp_path: Path,
) -> None:
    pad = b" " * (MAX_RESULT_BYTES - len(json.dumps(GOOD)))
    _put(tmp_path, raw=json.dumps(GOOD).encode() + pad)
    assert read_workload_result(tmp_path) is not None
    _put(tmp_path, raw=json.dumps(GOOD).encode() + pad + b" ")
    assert read_workload_result(tmp_path) is None


def test_a_sparse_giant_file_is_not_read(tmp_path: Path) -> None:
    target = _put(tmp_path, GOOD)
    with open(target, "r+b") as fh:
        fh.truncate(5 * 1024**3)
    assert read_workload_result(tmp_path) is None


def test_duplicate_keys_and_unicode_do_not_confuse_the_reader(tmp_path: Path) -> None:
    _put(
        tmp_path,
        raw='{"status":"unavailable","status":"ok","family":"cpu.x","params":{"ü":"日本"}}'.encode(),
    )
    report = read_workload_result(tmp_path)
    assert report is not None and report.status == "ok"
    assert json.loads(report.params_json) == {"ü": "日本"}


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink semantics")
def test_symlinked_results_dir_symlinked_file_and_fifo_are_refused(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    (outside).mkdir()
    (outside / "workload.json").write_text(json.dumps(GOOD))

    a = tmp_path / "a"
    a.mkdir()
    os.symlink(outside, a / "results")  # whole directory symlinked
    assert read_workload_result(a) is None

    b = tmp_path / "b"
    (b / "results").mkdir(parents=True)
    os.symlink(outside / "workload.json", b / "results" / "workload.json")
    assert read_workload_result(b) is None

    c = tmp_path / "c"
    (c / "results").mkdir(parents=True)
    os.mkfifo(c / "results" / "workload.json")
    assert read_workload_result(c) is None  # and does not hang

    d = tmp_path / "d"
    (d / "results").mkdir(parents=True)
    os.symlink("/dev/zero", d / "results" / "workload.json")
    assert read_workload_result(d) is None

    e = tmp_path / "e"
    (e / "results" / "workload.json").mkdir(parents=True)  # a directory
    assert read_workload_result(e) is None


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink semantics")
def test_a_swap_to_a_symlink_after_the_directory_was_opened_is_refused(
    tmp_path: Path,
) -> None:
    """The only race the reader has is path-based: it opens through directory
    descriptors with O_NOFOLLOW, so replacing ``results`` by a symlink between
    calls is refused on the next call (no state is cached)."""
    _put(tmp_path, GOOD)
    assert read_workload_result(tmp_path) is not None
    real = tmp_path / "results"
    moved = tmp_path / "results.moved"
    real.rename(moved)
    secret_dir = tmp_path / "elsewhere"
    secret_dir.mkdir()
    (secret_dir / "workload.json").write_text(json.dumps(GOOD))
    os.symlink(secret_dir, real)
    assert read_workload_result(tmp_path) is None


def test_a_missing_workdir_is_no_report(tmp_path: Path) -> None:
    assert read_workload_result(tmp_path / "nope") is None


# ------------------------------------------------------------ secret echo


def _echo(tmp_path: Path, secret: str, where: str) -> str:
    doc: dict[str, Any] = {**GOOD}
    if where == "value":
        doc["params"] = {"note": secret}
    elif where == "key":
        doc["params"] = {secret: 1}
    elif where == "nested":
        doc["params"] = {"a": [{"b": f"prefix-{secret}-suffix"}]}
    _put(tmp_path, doc)
    report = read_workload_result(
        tmp_path, matcher=SecretMatcher({"S": secret.encode()})
    )
    assert report is not None
    return report.params_json


@pytest.mark.security
@pytest.mark.parametrize("where", ["value", "key", "nested"])
def test_an_ascii_secret_echoed_raw_is_dropped(tmp_path: Path, where: str) -> None:
    secret = "tr0ub4dor-and-3-horses"
    assert _echo(tmp_path, secret, where) == ""


@pytest.mark.security
@pytest.mark.parametrize(
    "encode",
    [
        lambda s: base64.b64encode(s).decode(),
        lambda s: base64.urlsafe_b64encode(s).decode().rstrip("="),
        lambda s: s.hex(),
        lambda s: s.hex().upper(),
        lambda s: "".join(f"%{b:02x}" for b in s),
    ],
    ids=["b64", "b64url-nopad", "hex", "HEX", "percent"],
)
def test_an_ascii_secret_echoed_in_a_known_encoding_is_dropped(
    tmp_path: Path, encode: Any
) -> None:
    secret = b"tr0ub4dor-and-3-horses"
    assert _echo(tmp_path, encode(secret), "value") == ""


@pytest.mark.security
def test_a_secret_split_across_two_fields_is_a_documented_residual() -> None:
    """Not detectable by substring matching (and not claimed): recorded so that
    nobody mistakes the redaction for a guarantee against a malicious job that
    already holds the secret. The dataset is admin-only (RE-I6)."""
    assert True


@pytest.mark.security
@pytest.mark.parametrize(
    "secret",
    ["pässword-xyz-123", 'ab"cd\\ef-ghijk', "emoji-😀-secret-1", "日本語のパスワード"],
    ids=["latin1", "quote-backslash", "emoji", "cjk"],
)
@pytest.mark.parametrize("where", ["value", "key"])
def test_defect_3_a_secret_with_json_escaped_characters_must_be_dropped(
    tmp_path: Path, secret: str, where: str
) -> None:
    out = _echo(tmp_path, secret, where)
    assert out == "", f"secret leaked into the report: {out!r}"
