"""Unit tests for the S2 verdict aggregation (roadmap 0.2.6).

A human must be able to read one table and know, per direction: reachable or
not, and if not, what to change. Untested directions are TBD, measured but
under-repeated ones are INVALID - never silently "yes"."""

from __future__ import annotations

from typing import Any

import pytest

from spikes._harness import MIN_VALID_REPS
from spikes._s2 import verdict as vd


def _case(
    samples: int = 5,
    *,
    errors: list[dict[str, Any]] | None = None,
    summary: dict[str, Any] | None = None,
    survived: list[int] | None = None,
) -> dict[str, Any]:
    rows: list[dict[str, Any]]
    if survived is not None:
        rows = [
            {"survived": s, "failure": None if s else {"message": "idle drop"}}
            for s in survived
        ]
    else:
        rows = [{"ttfb_ms": 1.0} for _ in range(samples)]
    return {
        "label": "case",
        "samples": rows,
        "errors": errors or [],
        "summary": summary or {},
        "attrs": {},
        "metrics": {},
    }


def _envelope(cases: dict[str, Any], **extra: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "schema_version": 1,
        "spike": "S2",
        "topic": "S2-crossos",
        "link": "ethernet",
        "host": {"hostname": "laptop1", "label": "L1", "os": "Linux"},
        "params": {"server_name": "L2", "server": "192.168.1.21"},
        "cases": cases,
        "errors": [],
        "extra": {
            "local": {"system": "Linux", "release": "6.8.0", "wsl": {"is_wsl": False}},
            "peer_identity": {
                "system": "Windows",
                "release": "11",
                "wsl": {"is_wsl": False},
            },
            "outbound_only": {"no_inbound_rule_declared": True},
        },
        "timestamp_utc": "2026-09-19T10:00:00Z",
    }
    doc["extra"].update(extra)
    return doc


def test_full_pass_is_yes() -> None:
    assert vd.case_verdict(_case(5)).value == vd.YES


def test_too_few_repetitions_is_invalid() -> None:
    result = vd.case_verdict(_case(MIN_VALID_REPS - 1))
    assert result.value == vd.INVALID
    assert "n=4" in result.detail


def test_missing_case_is_tbd() -> None:
    assert vd.case_verdict(None).value == vd.TBD


def test_total_failure_reports_the_diagnosis() -> None:
    case = _case(
        0,
        errors=[
            {
                "failure_type": "connect_timeout",
                "message": "HTTP connect: timed out [fix: open the port]",
            }
        ],
    )
    result = vd.case_verdict(case)
    assert result.value == vd.NO
    assert "connect_timeout" in result.detail
    assert "fix" in result.detail


def test_partial_failure_is_flaky_not_yes() -> None:
    case = _case(4, errors=[{"failure_type": "timeout", "message": "slow"}])
    assert vd.case_verdict(case).value.startswith("flaky 4/5")


def test_session_that_always_survives_is_yes() -> None:
    assert vd.session_verdict(_case(survived=[1] * 5)).value == vd.YES


def test_session_that_never_survives_is_no_with_the_reason() -> None:
    result = vd.session_verdict(_case(survived=[0] * 5))
    assert result.value == vd.NO
    assert "idle drop" in result.detail


def test_session_that_sometimes_survives_is_flaky() -> None:
    result = vd.session_verdict(_case(survived=[1, 0, 1, 1, 1]))
    assert result.value == "flaky 4/5"
    assert result.detail


def test_session_needs_five_repetitions_too() -> None:
    assert vd.session_verdict(_case(survived=[1, 1])).value == vd.INVALID


def test_direction_verdict_reads_both_sides() -> None:
    doc = _envelope(
        {
            "http-ping": _case(5, summary={"ttfb_ms": {"n": 5, "mean": 1.5}}),
            "http-blob": _case(5, summary={"mbps": {"n": 5, "mean": 300.0}}),
            "grpc-unary": _case(5, summary={"rtt_ms": {"n": 5, "mean": 2.5}}),
            "grpc-stream": _case(5, summary={"mbps": {"n": 5, "mean": 250.0}}),
            "grpc-bidi-idle30s-kaon": _case(survived=[1] * 5),
            "grpc-bidi-idle30s-kaoff": _case(survived=[0] * 5),
        }
    )
    result = vd.direction_verdict(doc)
    assert result.direction == "L1 -> L2"
    assert result.client_os.startswith("Linux")
    assert result.server_os.startswith("Windows")
    assert result.native_pair is True
    assert result.cases["http-ping"].value == vd.YES
    assert result.sessions["grpc-bidi-idle30s-kaon"].value == vd.YES
    assert result.sessions["grpc-bidi-idle30s-kaoff"].value == vd.NO
    assert result.metrics["http_ttfb_ms"] == 1.5
    assert result.metrics["grpc_mbps"] == 250.0
    assert result.outbound_only.value == vd.YES
    assert any("kaoff" in fix for fix in result.fixes())


def test_wsl_side_is_flagged_as_not_a_native_pair() -> None:
    doc = _envelope(
        {"http-ping": _case(5)},
        peer_identity={"system": "Linux", "release": "5.15", "wsl": {"is_wsl": True}},
    )
    result = vd.direction_verdict(doc)
    assert result.native_pair is False
    assert "(WSL)" in result.server_os


def test_outbound_only_stays_tbd_until_the_operator_declares_it() -> None:
    doc = _envelope({"grpc-unary": _case(5)}, outbound_only={})
    result = vd.direction_verdict(doc)
    assert result.outbound_only.value == vd.TBD
    assert "--no-inbound-rule" in result.outbound_only.detail


def test_outbound_only_is_no_when_grpc_failed() -> None:
    doc = _envelope(
        {
            "grpc-unary": _case(
                0, errors=[{"failure_type": "connect_timeout", "message": "blocked"}]
            )
        }
    )
    assert vd.direction_verdict(doc).outbound_only.value == vd.NO


def test_run_errors_carry_their_fix_into_the_report() -> None:
    doc = _envelope({"http-ping": _case(5)})
    doc["errors"] = [
        {"failure_type": "connect_timeout", "message": "peer lost", "fix": "open 47202"}
    ]
    result = vd.direction_verdict(doc)
    assert any("open 47202" in fix for fix in result.fixes())


def test_expected_directions_are_ordered_pairs() -> None:
    assert vd.expected_directions(["L1", "L2"]) == ["L1 -> L2", "L2 -> L1"]
    assert len(vd.expected_directions(["L1", "L2", "L3"])) == 6


def test_untested_direction_is_rendered_tbd() -> None:
    doc = _envelope({"http-ping": _case(5), "grpc-bidi-idle30s-kaon": _case(survived=[1])})
    table = vd.render_verdicts(
        vd.direction_verdicts([doc]), vd.expected_directions(["L1", "L2"])
    )
    assert "L2 -> L1" in table
    assert table.count(vd.TBD) >= 5
    assert "#### Diagnoses and fixes" in table


@pytest.mark.parametrize("sessions", [0, 1, 2])
def test_table_columns_and_separator_match(sessions: int) -> None:
    cases: dict[str, Any] = {"http-ping": _case(5)}
    for i in range(sessions):
        cases[f"grpc-bidi-idle{i}s-kaon"] = _case(survived=[1] * 5)
    table = vd.render_verdicts(vd.direction_verdicts([_envelope(cases)]))
    lines = [ln for ln in table.splitlines() if ln.startswith("|")]
    header, separator, row = lines[0], lines[1], lines[2]
    assert header.count("|") == separator.count("|") == row.count("|")


def test_empty_input_still_renders() -> None:
    table = vd.render_verdicts({}, ["L1 -> L2"])
    assert "L1 -> L2" in table
    assert "(no results)" in table
