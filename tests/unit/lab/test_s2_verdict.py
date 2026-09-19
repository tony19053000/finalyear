"""Unit tests for the S2 verdict aggregation (roadmap 0.2.6).

A human must be able to read one table and know, per direction: reachable or
not, and if not, what to change. Untested directions are TBD, measured but
under-repeated ones are INVALID - never silently "yes"."""

from __future__ import annotations

import re
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
    doc = _envelope(
        {"http-ping": _case(5), "grpc-bidi-idle30s-kaon": _case(survived=[1])}
    )
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


def _verdict_rows(table: str) -> list[str]:
    """The data rows of the first (verdict) table only."""
    block = table.split("#### Latency")[0]
    return [ln for ln in block.splitlines() if ln.startswith("|")][2:]


def _columns(row: str) -> list[str]:
    """Split a markdown row on unescaped pipes, the way a renderer does."""
    return [c.strip() for c in re.split(r"(?<!\\)\|", row)[1:-1]]


# --------------------------------------------------- MIN_VALID_REPS boundary
@pytest.mark.parametrize(
    ("samples", "expected"),
    [
        (MIN_VALID_REPS - 1, vd.INVALID),
        (MIN_VALID_REPS, vd.YES),
        (MIN_VALID_REPS + 1, vd.YES),
    ],
)
def test_case_verdict_at_the_validity_boundary(samples: int, expected: str) -> None:
    assert vd.case_verdict(_case(samples)).value == expected


@pytest.mark.parametrize(
    ("survived", "expected"),
    [
        ([1] * (MIN_VALID_REPS - 1), vd.INVALID),
        ([1] * MIN_VALID_REPS, vd.YES),
        ([1] * (MIN_VALID_REPS + 1), vd.YES),
    ],
)
def test_session_verdict_at_the_validity_boundary(
    survived: list[int], expected: str
) -> None:
    assert vd.session_verdict(_case(survived=survived)).value == expected


def test_a_custom_min_reps_moves_the_boundary_with_it() -> None:
    """The threshold is a parameter, not a constant baked into the wording."""
    assert vd.case_verdict(_case(3), min_reps=3).value == vd.YES
    assert vd.case_verdict(_case(3), min_reps=4).value == vd.INVALID
    assert str(MIN_VALID_REPS) in vd.INVALID


def test_one_dropped_session_is_never_yes_however_many_survived() -> None:
    result = vd.session_verdict(_case(survived=[1] * 20 + [0]))
    assert result.value == "flaky 20/21"
    assert result.ok is False


def test_flaky_below_five_still_shows_both_counts() -> None:
    """A run that both failed and is under-repeated reads ``flaky k/n``: the
    two numbers are in the cell, so nobody can read it as a clean pass."""
    result = vd.case_verdict(_case(2, errors=[{"failure_type": "timeout"}]))
    assert result.value == "flaky 2/3"


# ------------------------------------------------- pairs, links and re-runs
def _pair_envelope(client: str, server: str, link: str, **extra: Any) -> dict[str, Any]:
    doc = _envelope({"http-ping": _case(5), "grpc-unary": _case(5)}, **extra)
    doc["host"] = {"hostname": client.lower(), "label": client, "os": "Linux"}
    doc["params"] = {"server_name": server, "server": "192.168.1.21"}
    doc["link"] = link
    return doc


def test_both_directions_of_a_pair_are_separate_rows() -> None:
    verdicts = vd.direction_verdicts(
        [
            _pair_envelope("L1", "L2", "ethernet"),
            _pair_envelope("L2", "L1", "ethernet"),
        ]
    )
    assert sorted(verdicts) == ["L1 -> L2 [ethernet]", "L2 -> L1 [ethernet]"]
    table = vd.render_verdicts(verdicts, vd.expected_directions(["L1", "L2"]))
    rows = _verdict_rows(table)
    assert [_columns(r)[0] for r in rows] == ["L1 -> L2", "L2 -> L1"]
    assert not [r for r in rows if set(_columns(r)[1:]) == {vd.TBD}], (
        "neither direction may be reported as untested"
    )


def test_the_two_links_of_one_direction_are_never_merged() -> None:
    verdicts = vd.direction_verdicts(
        [
            _pair_envelope("L1", "L2", "ethernet"),
            _pair_envelope("L1", "L2", "wifi"),
        ]
    )
    assert sorted(verdicts) == ["L1 -> L2 [ethernet]", "L1 -> L2 [wifi]"]
    table = vd.render_verdicts(verdicts)
    assert "Ethernet" in table
    assert "Wi-Fi" in table


def test_an_untested_direction_of_a_measured_pair_is_tbd_everywhere() -> None:
    table = vd.render_verdicts(
        vd.direction_verdicts([_pair_envelope("L1", "L2", "wifi")]),
        vd.expected_directions(["L1", "L2"]),
    )
    tbd_row = next(ln for ln in table.splitlines() if ln.startswith("| L2 -> L1"))
    assert set(_columns(tbd_row)[1:]) == {vd.TBD}


def test_a_rerun_keeps_the_newest_result_and_lists_both_files() -> None:
    old = _pair_envelope("L1", "L2", "ethernet")
    old["timestamp_utc"] = "2026-09-19T10:00:00Z"
    old["_source"] = "old.json"
    old["cases"]["grpc-unary"] = _case(
        0, errors=[{"failure_type": "connect_timeout", "message": "blocked"}]
    )
    new = _pair_envelope("L1", "L2", "ethernet")
    new["timestamp_utc"] = "2026-09-20T09:00:00Z"
    new["_source"] = "new.json"
    for order in ([old, new], [new, old]):
        verdicts = vd.direction_verdicts(order)
        result = verdicts["L1 -> L2 [ethernet]"]
        assert result.cases["grpc-unary"].value == vd.YES, "the newest run must win"
        assert sorted(result.sources) == ["new.json", "old.json"]


def test_a_rerun_without_timestamps_keeps_the_first_seen() -> None:
    first = _pair_envelope("L1", "L2", "wifi")
    second = _pair_envelope("L1", "L2", "wifi")
    for doc in (first, second):
        doc.pop("timestamp_utc", None)
    first["_source"], second["_source"] = "a.json", "b.json"
    second["cases"]["grpc-unary"] = _case(0, errors=[{"failure_type": "timeout"}])
    result = vd.direction_verdicts([first, second])["L1 -> L2 [wifi]"]
    assert result.cases["grpc-unary"].value == vd.YES
    assert sorted(result.sources) == ["a.json", "b.json"]


# -------------------------------------------------------- outbound-only gate
def test_outbound_only_is_tbd_when_no_grpc_case_ran_at_all() -> None:
    """The peer was lost before any gRPC case: "no" would claim the ADR-0002
    model was tested and failed, which it was not."""
    doc = _envelope({"http-ping": _case(5)})
    result = vd.direction_verdict(doc)
    assert result.outbound_only.value == vd.TBD
    assert "no valid result" in result.outbound_only.detail


def test_outbound_only_is_tbd_when_only_the_session_is_missing() -> None:
    doc = _envelope({"grpc-unary": _case(5)})
    result = vd.direction_verdict(doc)
    assert result.outbound_only.value == vd.TBD
    assert "long-lived session" in result.outbound_only.detail


def test_outbound_only_is_tbd_when_the_evidence_is_under_repeated() -> None:
    """INVALID is not a failure and not a proof."""
    doc = _envelope(
        {
            "grpc-unary": _case(MIN_VALID_REPS - 1),
            "grpc-bidi-idle30s-kaon": _case(survived=[1] * (MIN_VALID_REPS - 1)),
        }
    )
    assert vd.direction_verdict(doc).outbound_only.value == vd.TBD


def test_outbound_only_is_yes_when_one_keepalive_mode_survived() -> None:
    """A stream dropped with keepalive *off* is an idle-survival result, not a
    refutation of the outbound-only connection model."""
    doc = _envelope(
        {
            "grpc-unary": _case(5),
            "grpc-bidi-idle30s-kaon": _case(survived=[1] * 5),
            "grpc-bidi-idle30s-kaoff": _case(survived=[0] * 5),
        }
    )
    assert vd.direction_verdict(doc).outbound_only.value == vd.YES


def test_outbound_only_is_no_when_every_session_dropped() -> None:
    doc = _envelope(
        {
            "grpc-unary": _case(5),
            "grpc-bidi-idle30s-kaon": _case(survived=[0] * 5),
        }
    )
    assert vd.direction_verdict(doc).outbound_only.value == vd.NO


# --------------------------------------------------------- markdown safety
def test_a_pipe_or_newline_in_an_error_message_cannot_shift_the_table() -> None:
    doc = _envelope(
        {
            "http-ping": _case(
                0,
                errors=[
                    {
                        "failure_type": "connect_timeout",
                        "message": "netsh | advfirewall said:\nno rule",
                    }
                ],
            )
        },
        local={
            "system": "Windows | 11",
            "release": "10.0.26100",
            "wsl": {"is_wsl": False},
        },
    )
    table = vd.render_verdicts(vd.direction_verdicts([doc]))
    block = table.split("#### Latency")[0]
    lines = [ln for ln in block.splitlines() if ln.startswith("|")]
    header, separator, row = lines[0], lines[1], lines[2]
    assert len(_columns(row)) == len(_columns(separator)) == len(_columns(header))
    assert "advfirewall" in table
    assert "\\|" in table


def test_missing_metrics_render_as_a_dash_not_zero() -> None:
    table = vd.render_verdicts(vd.direction_verdicts([_envelope({})]))
    metrics = table.split("#### Latency and throughput")[1]
    assert "—" in metrics
    assert " 0.00 " not in metrics


def test_a_failure_without_a_message_does_not_crash_the_table() -> None:
    doc = _envelope({"http-ping": _case(0, errors=[{}])})
    result = vd.direction_verdict(doc)
    assert result.cases["http-ping"].value == vd.NO
    assert vd.render_verdicts({"k": result}).count("\n") > 5


# ------------------------------------------------------------ host labelling
def test_hosts_are_renamed_case_insensitively() -> None:
    doc = _pair_envelope("JARVISM3", "DESKTOP-42", "ethernet")
    labels = {"jarvism3": "L1", "desktop-42": "L2"}
    result = vd.direction_verdict(doc, labels)
    assert result.direction == "L1 -> L2"
    assert (result.client, result.server) == ("L1", "L2")


def test_an_unknown_host_keeps_its_recorded_name() -> None:
    doc = _pair_envelope("L9", "L2", "wifi")
    result = vd.direction_verdict(doc, {"l2": "B"})
    assert result.direction == "L9 -> B", "no label is invented for L9"


def test_renamed_directions_match_the_expected_list() -> None:
    verdicts = vd.direction_verdicts(
        [_pair_envelope("hostA", "hostB", "ethernet")],
        {"hosta": "L1", "hostb": "L2"},
    )
    table = vd.render_verdicts(verdicts, vd.expected_directions(["L1", "L2"]))
    rows = _verdict_rows(table)
    assert [_columns(r)[0] for r in rows] == ["L1 -> L2", "L2 -> L1"]
    assert set(_columns(rows[1])[1:]) == {vd.TBD}, "only the missing one is TBD"
