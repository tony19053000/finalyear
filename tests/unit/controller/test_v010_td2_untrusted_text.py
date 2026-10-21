"""V0.10 TD2: job / agent text is cleaned at the controller's storage
chokepoints - ``clean_detail`` (every job / attempt / chunk failure and event
detail) and the free-form JSON documents of an ``AttemptStatus``
(``workload.params_json``, ``condition.measured_json``)."""

from __future__ import annotations

import json
import time
from typing import Any

import pytest

from meshforge.common.proto.meshforge.v1 import agent_pb2, common_pb2
from meshforge.controller.jobs.reports import parse_status
from meshforge.controller.jobs.store import clean_detail
from meshforge.controller.store.models.job import MAX_DETAIL_CHARS

pytestmark = pytest.mark.unit

HOSTILE = "\x1b]0;owned\x07\x1b[2J\x1b[31mred\x1b[0m ‮exe.txt​\r\nnext\x9b1m"


def _is_display_safe(text: str) -> bool:
    return not any(
        ord(c) < 0x20
        or 0x7F <= ord(c) <= 0x9F
        or c in "‪‫‬‭‮⁦⁧⁨⁩"
        or c in "​‌‍‎‏⁠﻿؜"
        for c in text
    )


# -------------------------------------------------------------- clean_detail
def test_clean_detail_removes_sequences_whole_and_keeps_one_line() -> None:
    assert clean_detail(HOSTILE) == "red exe.txt next"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        ("", ""),
        ("exit code 1", "exit code 1"),
        ("a\nb\tc", "a b c"),
        ("\x1b[31mValueError\x1b[0m: bad", "ValueError: bad"),
        ("x⁧y⁩z", "xyz"),
        ("日本語", "日本語"),
    ],
)
def test_clean_detail_exact(raw: str | None, expected: str | None) -> None:
    assert clean_detail(raw) == expected


def test_clean_detail_cuts_after_cleaning_so_no_half_sequence_survives() -> None:
    raw = "\x1b[31m" * 2000 + "x" * (MAX_DETAIL_CHARS + 50)
    got = clean_detail(raw)
    assert got == "x" * MAX_DETAIL_CHARS
    edge = "y" * (MAX_DETAIL_CHARS - 2) + "\x1b[31mz"
    assert clean_detail(edge) == "y" * (MAX_DETAIL_CHARS - 2) + "z"


def test_clean_detail_is_bounded_on_64_kib_of_hostile_text() -> None:
    raw = ("\x1b]" + "‮​\x9d") * (65_536 // 5)
    start = time.perf_counter()
    got = clean_detail(raw)
    assert time.perf_counter() - start < 0.5
    assert got is not None and _is_display_safe(got)


# --------------------------------------------------- AttemptStatus documents
def _done(**workload: Any) -> agent_pb2.AttemptStatus:
    status = agent_pb2.AttemptStatus(
        job_id="job_01M55BPPFQG4T1EJHDEXH59BN6",
        attempt_id="att_01M55BPPFQG4T1EJHDEXH59BN6",
        fencing_token=1,
        state=common_pb2.ATTEMPT_STATE_DONE,
    )
    status.phases.run_s = 1.0
    status.workload.CopyFrom(agent_pb2.WorkloadReport(status="ok", **workload))
    return status


def _strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for k, v in value.items() for s in (*_strings(k), *_strings(v))]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return []


def test_job_written_workload_params_are_cleaned_not_refused() -> None:
    params = {
        "model\x1b[31m": "resnet‮18",
        "n": 3,
        "ok": True,
        "none": None,
        "f": 1.5,
        "nested": {"title": "\x1b]0;owned\x07t", "list": ["a​b", 1, {"k": "\x9b2J"}]},
    }
    report = parse_status(_done(params_json=json.dumps(params)))
    assert not isinstance(report, str), report
    assert report.workload is not None
    assert report.workload["params"] == {
        "model": "resnet18",
        "n": 3,
        "ok": True,
        "none": None,
        "f": 1.5,
        "nested": {"title": "t", "list": ["ab", 1, {"k": ""}]},
    }


def test_clean_params_are_stored_exactly_as_sent() -> None:
    params = {"family": "matmul", "size": 512, "tags": ["a", "b"], "x": {"y": "z\n"}}
    report = parse_status(_done(params_json=json.dumps(params)))
    assert not isinstance(report, str) and report.workload is not None
    assert report.workload["params"] == params


def test_deeply_nested_params_are_cleaned_iteratively() -> None:
    depth = 800
    raw = '{"a":' * depth + '"\\u001b[31mx"' + "}" * depth
    report = parse_status(_done(params_json=raw))
    if isinstance(report, str):  # the parser's own depth bound refused it
        return
    assert report.workload is not None
    assert all(_is_display_safe(s) for s in _strings(report.workload["params"]))


def test_condition_measured_strings_are_cleaned() -> None:
    status = agent_pb2.AttemptStatus(
        job_id="job_01M55BPPFQG4T1EJHDEXH59BN6",
        attempt_id="att_01M55BPPFQG4T1EJHDEXH59BN6",
        fencing_token=1,
        state=common_pb2.ATTEMPT_STATE_DONE,
    )
    status.condition.CopyFrom(
        agent_pb2.ConditionReport(
            spec="cpu_load:2",
            verified=True,
            measured_json=json.dumps({"cores": 2, "note‮": "\x1b[2Jbusy"}),
            reverted=True,
        )
    )
    report = parse_status(status)
    assert not isinstance(report, str), report
    assert report.workload is not None
    assert report.workload["condition"]["measured"] == {"cores": 2, "note": "busy"}


def test_failure_message_with_escape_is_still_refused_at_ingress() -> None:
    """D22 unchanged: the agent's own protocol strings are refused, not cleaned
    (the agent replaces non-printables before sending)."""
    status = agent_pb2.AttemptStatus(
        job_id="job_01M55BPPFQG4T1EJHDEXH59BN6",
        attempt_id="att_01M55BPPFQG4T1EJHDEXH59BN6",
        fencing_token=1,
        state=common_pb2.ATTEMPT_STATE_FAILED,
    )
    status.failure.CopyFrom(
        common_pb2.Failure(
            failure_type=common_pb2.FAILURE_TYPE_USER_ERROR,
            message="exit 1 ‮",
        )
    )
    assert isinstance(parse_status(status), str)
