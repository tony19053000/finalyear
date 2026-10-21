"""V0.10 TD2 tester: independent attacks on ``clean_detail`` and on the
cleaning of ``params_json`` / ``measured_json`` in ``parse_status``."""

from __future__ import annotations

import json
import random
import time
from typing import Any

import pytest

from meshforge.common.proto.meshforge.v1 import agent_pb2, common_pb2
from meshforge.controller.jobs.reports import parse_status
from meshforge.controller.jobs.store import clean_detail
from meshforge.controller.store.models.job import MAX_DETAIL_CHARS

pytestmark = pytest.mark.unit

SPOOF = "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069\u200e\u200f\u061c"
ZW = "\u200b\u200c\u200d\u2060\ufeff"


def _safe(text: str) -> bool:
    return text.isprintable() and not any(c in SPOOF + ZW for c in text)


# -------------------------------------------------------------- clean_detail
def test_strip_then_truncate_never_counts_removed_text() -> None:
    filler = "\x1b[31m" + SPOOF + ZW
    raw = (filler + "a") * (MAX_DETAIL_CHARS + 10)
    got = clean_detail(raw)
    assert got == "a" * MAX_DETAIL_CHARS


def test_truncation_boundary_with_sequence_straddling_the_cut() -> None:
    for pad in range(MAX_DETAIL_CHARS - 6, MAX_DETAIL_CHARS + 2):
        raw = "y" * pad + "\x1b[1;31;40mZ" + "w" * 20
        got = clean_detail(raw)
        assert got is not None and len(got) <= MAX_DETAIL_CHARS
        assert "\x1b" not in got and "[1;31" not in got
        assert got == ("y" * pad + "Z" + "w" * 20)[:MAX_DETAIL_CHARS]


def test_exactly_max_and_one_over() -> None:
    assert clean_detail("a" * MAX_DETAIL_CHARS) == "a" * MAX_DETAIL_CHARS
    assert clean_detail("a" * (MAX_DETAIL_CHARS + 1)) == "a" * MAX_DETAIL_CHARS


def test_no_text_is_hidden_by_an_unterminated_sequence() -> None:
    got = clean_detail("job failed \x1b]0;the real reason is here")
    assert got is not None and "the real reason is here" in got
    got = clean_detail("job failed \x9dsecret reason")
    assert got is not None and "secret reason" in got


def test_bidi_cannot_reorder_the_stored_detail() -> None:
    got = clean_detail("error in harmless\u202etxt.exe")
    assert got == "error in harmlesstxt.exe"


def test_result_is_one_printable_line() -> None:
    got = clean_detail("a\r\nb\rc\nd\te\x00f\x7fg\u2028h\u0085i")
    assert got is not None and "\n" not in got and "\r" not in got
    assert got.isprintable()


@pytest.mark.parametrize("seed", range(30))
def test_fuzz_idempotent_and_safe(seed: int) -> None:
    rng = random.Random(seed)  # noqa: S311
    alphabet = [
        "\x1b[",
        "\x1b]",
        "\x07",
        "\x9b",
        "\x9d",
        "\x1b\\",
        "\r\n",
        "\n",
        "\t",
        "a",
        "bc",
        "31m",
        ";",
        "\u65e5",
        *SPOOF,
        *ZW,
        "\u00a0",
        "\u2028",
        "\x00",
    ]
    raw = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 600)))
    once = clean_detail(raw)
    assert once is not None
    assert clean_detail(once) == once, repr(raw)
    assert _safe(once)
    assert len(once) <= MAX_DETAIL_CHARS


def test_64_kib_hostile_detail_is_fast_and_safe() -> None:
    for unit in ("\x9d\u202e", "\x1b]\u200b", "\x1bP\x9b", "\x1b[0 ", "\x1b" + " " * 8):
        raw = unit * (65_536 // len(unit))
        start = time.perf_counter()
        got = clean_detail(raw)
        assert time.perf_counter() - start < 1.0, repr(unit)
        assert got is not None and _safe(got)


# ----------------------------------------------------------- parse_status
def _status(**workload: Any) -> agent_pb2.AttemptStatus:
    status = agent_pb2.AttemptStatus(
        job_id="job_01M55BPPFQG4T1EJHDEXH59BN6",
        attempt_id="att_01M55BPPFQG4T1EJHDEXH59BN6",
        fencing_token=1,
        state=common_pb2.ATTEMPT_STATE_DONE,
    )
    status.phases.run_s = 1.0
    status.workload.CopyFrom(agent_pb2.WorkloadReport(status="ok", **workload))
    return status


def _params(doc: Any, *, raw: str | None = None) -> Any:
    report = parse_status(_status(params_json=raw if raw else json.dumps(doc)))
    assert not isinstance(report, str), report
    assert report.workload is not None
    return report.workload["params"]


def test_clean_documents_are_byte_identical_after_round_trip() -> None:
    docs: list[dict[str, Any]] = [
        {},
        {"n": 1},
        {"a": {"b": {"c": [1, 2.5, True, None, "x", {"d": []}]}}},
        {"\u65e5\u672c": "\u00e9\u00e8 \u2603 \U0001f600", "t": "tab\tnl\n"},
        {"big": 10**18, "neg": -3, "f": 1e-9, "e": 1e300},
        {"quote": 'he said "hi"', "slash": "a\\b", "uni": "\u00ad\u00a0"},
        {"k" * 100: "v" * 3000},
    ]
    for doc in docs:
        got = _params(doc)
        assert got == doc
        assert json.dumps(got, sort_keys=True) == json.dumps(doc, sort_keys=True)
        for key, value in doc.items():
            assert type(got[key]) is type(value)


def test_non_string_values_are_unchanged_and_bool_stays_bool() -> None:
    got = _params({"t": True, "f": False, "n": None, "i": 7, "x": 0.25, "z": 0})
    assert got == {"t": True, "f": False, "n": None, "i": 7, "x": 0.25, "z": 0}
    assert got["t"] is True and got["f"] is False and type(got["i"]) is int


def test_hostile_keys_and_values_every_position() -> None:
    bad = "\x1b[31m\u202e\u200b"
    doc = {
        f"k{bad}": f"v{bad}",
        "list": [f"{bad}a", [f"{bad}b", {f"{bad}c": f"{bad}d"}]],
        "obj": {f"{bad}x": {f"{bad}y": f"{bad}z"}},
    }
    got = _params(doc)
    assert got == {
        "k": "v",
        "list": ["a", ["b", {"c": "d"}]],
        "obj": {"x": {"y": "z"}},
    }


def test_key_collision_after_cleaning_keeps_the_report() -> None:
    got = _params(None, raw='{"a\\u202e": 1, "a": 2, "a\\u200b": 3}')
    assert list(got) == ["a"] and isinstance(got["a"], int)


def test_key_cleaning_to_empty_string_is_kept_as_empty_key() -> None:
    got = _params(None, raw='{"\\u202e": 1}')
    assert got == {"": 1}


def test_list_order_and_length_preserved() -> None:
    got = _params({"l": ["\u202ea", "b", "\x1b[0mc", 4, None, ["\u200b"]]})
    assert got == {"l": ["a", "b", "c", 4, None, [""]]}


def test_nested_depth_does_not_overflow_or_lose_leaf() -> None:
    for depth in (50, 300, 900):
        raw = '{"a":' * depth + '"\\u001b[31mleaf\\u202e"' + "}" * depth
        report = parse_status(_status(params_json=raw))
        if isinstance(report, str):  # refused by the JSON parser's own bound
            assert "params_json" in report
            continue
        assert report.workload is not None
        node: Any = report.workload["params"]
        for _ in range(depth):
            node = node["a"]
        assert node == "leaf"


def test_wide_document_is_fast() -> None:
    doc = {f"k{i}\u202e": f"\x1b[31mv{i}" for i in range(120)}
    raw = json.dumps(doc)
    start = time.perf_counter()
    got = _params(None, raw=raw)
    assert time.perf_counter() - start < 2.0
    assert got["k0"] == "v0" and len(got) == 120


def test_nul_and_lone_surrogate_are_still_refused_not_cleaned() -> None:
    for raw in ('{"a":"x\\u0000y"}', '{"a\\u0000":1}', '{"a":"\\ud800"}'):
        report = parse_status(_status(params_json=raw))
        assert isinstance(report, str) and "params_json" in report, raw


def test_nan_and_infinity_still_refused() -> None:
    for raw in ('{"a":NaN}', '{"a":Infinity}', '{"a":1e999}'):
        assert isinstance(parse_status(_status(params_json=raw)), str), raw


def test_measured_json_cleaned_and_clean_one_unchanged() -> None:
    def condition(measured: Any) -> Any:
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
                measured_json=json.dumps(measured),
                reverted=True,
            )
        )
        report = parse_status(status)
        assert not isinstance(report, str), report
        assert report.workload is not None
        return report.workload["condition"]["measured"]

    clean = {"cores": 2, "load": [0.5, 0.75], "n": {"a": None, "b": True}, "s": "ok"}
    assert condition(clean) == clean
    got = condition({"\x1b[2Jk\u202e": ["\u200bv", {"\x08q": "\x1b]0;t\x07w"}]})
    assert got == {"k": ["v", {"q": "w"}]}


def test_failure_message_refusal_is_intact_for_every_spoof_char() -> None:
    """D22: the agent's protocol strings are refused (not cleaned)."""
    for ch in (*SPOOF, *ZW, "\x1b", "\x07", "\x9b", "\r", "\n", "\x00"):
        status = agent_pb2.AttemptStatus(
            job_id="job_01M55BPPFQG4T1EJHDEXH59BN6",
            attempt_id="att_01M55BPPFQG4T1EJHDEXH59BN6",
            fencing_token=1,
            state=common_pb2.ATTEMPT_STATE_FAILED,
        )
        status.failure.CopyFrom(
            common_pb2.Failure(
                failure_type=common_pb2.FAILURE_TYPE_USER_ERROR,
                message=f"exit 1 {ch} bad",
            )
        )
        report = parse_status(status)
        assert isinstance(report, str), repr(ch)
        assert "failure.message" in report


def test_clean_failure_message_is_accepted_verbatim() -> None:
    status = agent_pb2.AttemptStatus(
        job_id="job_01M55BPPFQG4T1EJHDEXH59BN6",
        attempt_id="att_01M55BPPFQG4T1EJHDEXH59BN6",
        fencing_token=1,
        state=common_pb2.ATTEMPT_STATE_FAILED,
    )
    status.failure.CopyFrom(
        common_pb2.Failure(
            failure_type=common_pb2.FAILURE_TYPE_USER_ERROR, message="exit code 3"
        )
    )
    report = parse_status(status)
    assert not isinstance(report, str) and report.detail == "exit code 3"
