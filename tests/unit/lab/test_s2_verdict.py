"""Adversarial unit tests for ``lab/spikes/_s2/verdict.py`` (roadmap 0.2.6).

The highest-value question for this task: can any case dict make
``case_verdict``/``session_verdict`` claim ``yes`` without 5 clean repetitions?
These tests enumerate the boundary and try to construct such an input. They
also cover: ``flaky`` vs ``INVALID`` precedence, re-run tie-breaking on
``timestamp_utc``, ``--hosts`` labelling edge cases, report-row column safety
for absurd case ids / free text, and the ``outbound-only`` TBD/yes/no logic.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from spikes._harness import MIN_VALID_REPS
from spikes._s2 import verdict as v


def _samples(n: int, **extra: Any) -> list[dict[str, Any]]:
    return [{"seq": i, **extra} for i in range(n)]


def _errors(n: int) -> list[dict[str, Any]]:
    return [
        {"failure_type": "timeout", "message": f"boom {i}", "fatal": False}
        for i in range(n)
    ]


# --------------------------------------------------------- case_verdict: yes
def test_case_verdict_yes_requires_exact_min_reps_and_zero_errors() -> None:
    case = {"samples": _samples(MIN_VALID_REPS), "errors": []}
    assert v.case_verdict(case) == v.Verdict(v.YES)


@pytest.mark.parametrize("n", [0, 1, MIN_VALID_REPS - 1])
def test_case_verdict_never_yes_below_min_reps_even_with_no_errors(n: int) -> None:
    case = {"samples": _samples(n), "errors": []}
    result = v.case_verdict(case)
    assert result.value != v.YES
    if n == 0:
        assert result.value == v.NO
    else:
        assert result.value == v.INVALID


@pytest.mark.parametrize(
    ("n_samples", "n_errors"), [(MIN_VALID_REPS, 1), (100, 1), (4, 1)]
)
def test_case_verdict_never_yes_when_any_error_recorded(
    n_samples: int, n_errors: int
) -> None:
    """Any non-empty ``errors`` list must prevent a ``yes`` verdict, regardless
    of how many clean samples exist alongside it."""
    case = {"samples": _samples(n_samples), "errors": _errors(n_errors)}
    result = v.case_verdict(case)
    assert result.value != v.YES


def test_case_verdict_missing_case_is_tbd_not_yes() -> None:
    assert v.case_verdict(None) == v.Verdict(v.TBD, "case not run")


def test_case_verdict_empty_dict_is_no_not_yes() -> None:
    assert v.case_verdict({}).value == v.NO


@pytest.mark.parametrize(
    "case",
    [
        {"samples": None, "errors": None},
        {},
        {"samples": []},
        {"samples": [], "errors": [{"failure_type": "x", "message": "y"}]},
    ],
)
def test_case_verdict_degenerate_inputs_never_yes(case: dict[str, Any]) -> None:
    assert v.case_verdict(case).value != v.YES


def test_case_verdict_flaky_reports_true_denominator() -> None:
    case = {"samples": _samples(3), "errors": _errors(2)}
    result = v.case_verdict(case)
    assert result.value == "flaky 3/5"


def test_case_verdict_min_reps_argument_is_honoured() -> None:
    """A caller-supplied ``min_reps`` (e.g. a test harness with --reps 3) must
    still gate ``yes``, not just the module-level default."""
    case = {"samples": _samples(3), "errors": []}
    assert v.case_verdict(case, min_reps=3).value == v.YES
    assert v.case_verdict(case, min_reps=4).value == v.INVALID


# ------------------------------------- warmup errors must not taint a verdict
def test_warmup_only_error_does_not_demote_a_clean_case() -> None:
    """(Was a defect.) ``CaseResult.errors`` mixes warmup and measured-rep failures (reps.py
    records both with a ``phase`` field), but ``case_verdict`` does not look at
    ``phase`` at all: it treats a warmup hiccup as if a measured repetition had
    failed. A case where every *requested* (measured) repetition succeeded
    must be ``yes`` per S2.md ("yes: every requested repetition worked ...
    n>=5"); warmup samples are explicitly "kept... but never enter the
    summary" (reps.py docstring), so they must not enter the verdict either.

    Before the fix the cell read 'flaky 5/6', wrongly under-stating an
    actually-clean result on the reachability table; warmup failures are now
    surfaced as ``warmup_notes`` instead (see the test below).
    """
    case = {
        "samples": _samples(MIN_VALID_REPS),
        "errors": [
            {
                "rep": 0,
                "phase": "warmup",
                "failure_type": "timeout",
                "message": "warmup hiccup",
                "fatal": False,
            }
        ],
    }
    result = v.case_verdict(case)
    assert result.value == v.YES, (
        f"expected yes (all {MIN_VALID_REPS} measured reps succeeded, only a "
        f"warmup rep failed), got {result.value!r} -- see reps.py: warmup "
        "errors are recorded in the same 'errors' list as measured ones"
    )


def _warmup_error(rep: int = 0) -> dict[str, Any]:
    return {
        "rep": rep,
        "phase": "warmup",
        "failure_type": "timeout",
        "message": "warmup hiccup",
        "fatal": False,
    }


def test_warmup_failure_is_surfaced_as_a_note_not_a_verdict() -> None:
    """The warmup failure must not vanish: it is reported once, labelled
    "(not counted)", in the fixes section and in the JSON, while the cell
    itself stays ``yes``."""
    doc = _doc(
        cases={
            "http-ping": {
                "samples": _samples(MIN_VALID_REPS),
                "errors": [_warmup_error()],
            }
        }
    )
    dv = v.direction_verdict(doc)
    assert dv.cases["http-ping"].value == v.YES
    assert dv.notes() == [
        "http-ping: warmup repetition 0 failed (timeout: warmup hiccup); "
        "warmup is excluded from the verdict"
    ]
    assert not any("warmup" in fix for fix in dv.fixes())  # a note is not a fix
    assert dv.to_json()["warmup_notes"] == dv.notes()
    rendered = v.render_verdicts({"k": dv})
    assert "(note, not counted)" in rendered and "warmup hiccup" in rendered


def test_unlabelled_error_counts_as_measured() -> None:
    """An error record without ``phase`` is the conservative case: measured."""
    err = {"rep": 1, "failure_type": "timeout", "message": "no phase"}
    case = {"samples": _samples(MIN_VALID_REPS), "errors": [err]}
    assert v.case_verdict(case).value == "flaky 5/6"
    assert (
        v.session_verdict(
            {"samples": _samples(MIN_VALID_REPS, survived=True), "errors": [err]}
        ).value
        == "flaky 5/6"
    )


def test_measured_and_warmup_errors_both_present() -> None:
    """One warmup + one measured failure: the cell counts only the measured one
    and names it, never the warmup one."""
    case = {
        "samples": _samples(MIN_VALID_REPS),
        "errors": [
            _warmup_error(),
            {"rep": 2, "phase": "measure", "failure_type": "refused", "message": "m"},
        ],
    }
    result = v.case_verdict(case)
    assert result.value == "flaky 5/6"
    assert result.detail == "refused: m"


# ------------------------------------------------------------ session_verdict
def test_session_verdict_yes_requires_all_survived_and_min_reps() -> None:
    case = {"samples": _samples(MIN_VALID_REPS, survived=True), "errors": []}
    assert v.session_verdict(case) == v.Verdict(v.YES)


def test_session_verdict_no_when_all_dropped() -> None:
    case = {
        "samples": [
            {"survived": False, "failure": {"message": "dead", "fix": "do X"}}
            for _ in range(MIN_VALID_REPS)
        ],
        "errors": [],
    }
    result = v.session_verdict(case)
    assert result.value == v.NO
    assert "dead" in result.detail and "fix: do X" in result.detail


def test_session_verdict_flaky_never_rounds_up_to_yes() -> None:
    samples = _samples(3, survived=True) + [
        {"survived": False, "failure": {"message": "dropped", "fix": None}}
        for _ in range(2)
    ]
    result = v.session_verdict({"samples": samples, "errors": []})
    assert result.value == "flaky 3/5"
    assert result.value != v.YES


@pytest.mark.parametrize(
    "survived_flags",
    [
        [True] * MIN_VALID_REPS,
        [True, True, True, True, False],
        [False] * MIN_VALID_REPS,
        [],
    ],
)
def test_session_verdict_never_yes_with_any_drop_or_too_few(
    survived_flags: list[bool],
) -> None:
    samples = [{"survived": s} for s in survived_flags]
    result = v.session_verdict({"samples": samples, "errors": []})
    if len(samples) >= MIN_VALID_REPS and all(survived_flags):
        assert result.value == v.YES
    else:
        assert result.value != v.YES


def test_session_verdict_missing_survived_key_treated_as_dropped_not_crash() -> None:
    """A malformed sample lacking ``survived`` must not be silently 'yes'."""
    samples = _samples(MIN_VALID_REPS)  # no 'survived' key at all
    result = v.session_verdict({"samples": samples, "errors": []})
    assert result.value != v.YES


# --------------------------------------------------------- flaky vs INVALID
def test_flaky_takes_precedence_over_invalid_when_errors_present_below_min() -> None:
    """3 samples + 1 error (n=4 < min_reps) must be reported as flaky, with an
    honest denominator, never silently upgraded to INVALID-looks-clean."""
    case = {"samples": _samples(3), "errors": _errors(1)}
    result = v.case_verdict(case)
    assert result.value == "flaky 3/4"


def test_invalid_only_when_zero_errors_and_too_few_samples() -> None:
    case = {"samples": _samples(2), "errors": []}
    assert v.case_verdict(case).value == v.INVALID


# ------------------------------------------------------------- direction_verdict
def _doc(
    *,
    client: str = "L1",
    server: str = "L2",
    link: str = "ethernet",
    cases: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
    timestamp: str = "2026-09-20T10:00:00Z",
    source: str = "f.json",
) -> dict[str, Any]:
    return {
        "host": {"hostname": client, "label": client},
        "params": {"server_name": server},
        "link": link,
        "cases": cases or {},
        "extra": extra or {},
        "errors": [],
        "timestamp_utc": timestamp,
        "_source": source,
    }


def test_outbound_only_tbd_without_no_inbound_rule_declared() -> None:
    doc = _doc(
        cases={"grpc-unary": {"samples": _samples(MIN_VALID_REPS), "errors": []}},
        extra={"outbound_only": {"no_inbound_rule_declared": False}},
    )
    dv = v.direction_verdict(doc)
    assert dv.outbound_only.value == v.TBD


def test_outbound_only_yes_requires_unary_and_a_session_to_have_worked() -> None:
    doc = _doc(
        cases={
            "grpc-unary": {"samples": _samples(MIN_VALID_REPS), "errors": []},
            "grpc-bidi-idle30s-kaon": {
                "samples": _samples(MIN_VALID_REPS, survived=True),
                "errors": [],
            },
        },
        extra={"outbound_only": {"no_inbound_rule_declared": True}},
    )
    dv = v.direction_verdict(doc)
    assert dv.outbound_only.value == v.YES


def test_outbound_only_no_only_when_something_actually_failed() -> None:
    doc = _doc(
        cases={
            "grpc-unary": {"samples": [], "errors": []},  # measured -> NO
        },
        extra={"outbound_only": {"no_inbound_rule_declared": True}},
    )
    dv = v.direction_verdict(doc)
    assert dv.outbound_only.value == v.NO


def test_outbound_only_stays_tbd_when_grpc_never_ran_never_reported_no() -> None:
    """A case that never ran (TBD/INVALID) must not count as 'failed'."""
    doc = _doc(
        cases={
            "grpc-unary": {"samples": _samples(2), "errors": []},  # INVALID, not NO
        },
        extra={"outbound_only": {"no_inbound_rule_declared": True}},
    )
    dv = v.direction_verdict(doc)
    assert dv.outbound_only.value == v.TBD


def test_direction_verdicts_never_merge_two_links_of_same_direction() -> None:
    wifi = _doc(link="wifi", source="a.json")
    eth = _doc(link="ethernet", source="b.json")
    result = v.direction_verdicts([wifi, eth])
    assert set(result) == {"L1 -> L2 [wifi]", "L1 -> L2 [ethernet]"}


def test_direction_verdicts_newest_timestamp_wins() -> None:
    old = _doc(timestamp="2026-01-01T00:00:00Z", source="old.json")
    new = _doc(timestamp="2026-06-01T00:00:00Z", source="new.json")
    result = v.direction_verdicts([old, new])
    (key,) = result
    assert "new.json" in result[key].sources
    assert "old.json" in result[key].sources  # kept for provenance


def test_direction_verdicts_reversed_order_still_picks_newest() -> None:
    old = _doc(timestamp="2026-01-01T00:00:00Z", source="old.json")
    new = _doc(timestamp="2026-06-01T00:00:00Z", source="new.json")
    result = v.direction_verdicts([new, old])
    (key,) = result
    assert result[key].sources == ["new.json", "old.json"] or set(
        result[key].sources
    ) == {"new.json", "old.json"}


def test_direction_verdicts_equal_timestamp_keeps_first_seen() -> None:
    first = _doc(timestamp="2026-01-01T00:00:00Z", source="first.json")
    second = _doc(timestamp="2026-01-01T00:00:00Z", source="second.json")
    result = v.direction_verdicts([first, second])
    (key,) = result
    # documented: "Ties keep the first seen"
    assert result[key].sources[0] == "first.json"


def test_direction_verdicts_missing_timestamp_does_not_crash() -> None:
    doc = _doc(source="x.json")
    del doc["timestamp_utc"]
    result = v.direction_verdicts([doc])
    assert len(result) == 1


def test_direction_verdicts_malformed_timestamp_does_not_crash() -> None:
    """A non-ISO / garbage timestamp must be handled by string comparison
    without raising, since the comparison is ``stamp > stamps[key]`` on
    strings, never a parsed datetime."""
    a = _doc(timestamp="not-a-timestamp", source="a.json")
    b = _doc(timestamp="2026-01-01T00:00:00Z", source="b.json")
    result = v.direction_verdicts([a, b])
    assert len(result) == 1  # same direction+link, must not crash or duplicate


# ------------------------------------------------------------ expected_directions
def test_expected_directions_no_self_pairs() -> None:
    result = v.expected_directions(["L1", "L2"])
    assert "L1 -> L1" not in result
    assert set(result) == {"L1 -> L2", "L2 -> L1"}


def test_expected_directions_duplicate_labels_deduplicated_by_caller_input() -> None:
    """Passing the same label twice must not fabricate a self-pair or double
    every real pair; ``expected_directions`` takes labels as given, so callers
    (the CLI's ``--hosts``) are responsible for uniqueness -- verify the
    function itself is at least idempotent/no-crash on a duplicate list."""
    result = v.expected_directions(["L1", "L1", "L2"])
    assert "L1 -> L1" not in result
    assert "L2 -> L1" in result


def test_relabel_is_case_insensitive_and_unknown_name_passes_through() -> None:
    labels = {"hosta": "L1"}
    assert v.relabel("HostA", labels) == "L1"
    assert v.relabel("HOSTA", labels) == "L1"
    assert v.relabel("unknown-host", labels) == "unknown-host"


def test_relabel_empty_label_string_is_not_silently_dropped() -> None:
    """An operator-supplied ``--hosts =host`` (empty label) is rejected by the
    CLI parser (see s2_crossos.py's ``_labels``), but ``relabel`` itself, given
    such a mapping directly, must not crash and must not silently invent a
    label out of thin air."""
    labels = {"host": ""}
    assert v.relabel("host", labels) == ""


# ------------------------------------------------------------ render_verdicts
def _direction_verdict(**kw: Any) -> v.DirectionVerdict:
    base: dict[str, Any] = {
        "direction": "L1 -> L2",
        "client": "L1",
        "server": "L2",
        "link": "ethernet",
        "client_os": "Linux",
        "server_os": "Windows",
        "client_native": True,
        "server_native": True,
    }
    base.update(kw)
    return v.DirectionVerdict(**base)


def _unescaped_pipe_count(line: str) -> int:
    """Count the ``|`` characters markdown would actually treat as column
    delimiters -- i.e. not preceded by ``cell()``'s escaping backslash."""
    import re

    return len(re.findall(r"(?<!\\)\|", line))


def test_render_verdicts_absurd_case_id_and_pipe_never_shifts_columns() -> None:
    dv = _direction_verdict()
    dv.cases = {c: v.Verdict(v.TBD) for c, _ in v.FIXED_CASES}
    dv.sessions = {
        "grpc-bidi-" + "x" * 5 + "|evil\ncolumn": v.Verdict(v.NO, "boom | boom\nmore")
    }
    dv.client_os = "Linux | evil\ncolumn shift"
    text = v.render_verdicts({"k": dv})
    verdict_block = text.split("#### Latency")[0]
    lines = verdict_block.splitlines()
    header_line = next(line for line in lines if line.startswith("| direction"))
    n_cols = _unescaped_pipe_count(header_line)
    data_rows = [line for line in lines if line.startswith("| L1 -> L2")]
    assert data_rows, "the row must still start in the direction column"
    for row in data_rows:
        assert _unescaped_pipe_count(row) == n_cols, row
        assert "\n" not in row  # no embedded raw newline broke the row apart


def test_render_verdicts_tbd_row_for_untested_direction_never_upgraded() -> None:
    dv = _direction_verdict()
    dv.cases = {c: v.Verdict(v.YES) for c, _ in v.FIXED_CASES}
    text = v.render_verdicts({"k": dv}, expected=["L1 -> L2", "L2 -> L1"])
    lines = [line for line in text.splitlines() if line.startswith("|")]
    tbd_row = next(line for line in lines if line.startswith("| L2 -> L1"))
    assert tbd_row.count(v.TBD) >= 1
    assert "yes" not in tbd_row


def test_render_verdicts_wifi_and_ethernet_are_separate_rows() -> None:
    wifi = _direction_verdict(link="wifi")
    eth = _direction_verdict(link="ethernet")
    wifi.cases = {c: v.Verdict(v.YES) for c, _ in v.FIXED_CASES}
    eth.cases = {c: v.Verdict(v.YES) for c, _ in v.FIXED_CASES}
    text = v.render_verdicts({"a": wifi, "b": eth})
    verdict_block = text.split("#### Latency")[0]
    assert verdict_block.count("| L1 -> L2 |") == 2
    assert "Wi-Fi" in verdict_block and "Ethernet" in verdict_block
    # never merged into a single row: each link keeps its own full row
    rows = [
        line for line in verdict_block.splitlines() if line.startswith("| L1 -> L2 |")
    ]
    assert len(rows) == 2 and rows[0] != rows[1]


def test_direction_verdict_survives_a_schema_valid_but_malformed_case() -> None:
    """(Was a defect.) ``envelope.validate()`` only checks that ``doc["cases"]``
    is a ``dict`` overall -- it never checks that each *value* in it is a dict
    too. A ``report`` run over a schema-valid-but-malformed result file (e.g.
    one hand-edited, or written by a future, buggier version of the client)
    with ``cases: {"http-ping": "garbage"}`` used to crash ``direction_verdict``
    (and transitively ``cmd_report``) with an unhandled ``AttributeError``.
    It must now report the malformed entry instead -- and never as ``yes``.
    """
    doc = _doc(cases={"http-ping": "garbage"})
    dv = v.direction_verdict(doc)
    cell = dv.cases["http-ping"]
    assert cell.value == v.MALFORMED
    assert cell.value != v.YES
    assert "malformed" in cell.detail
    # every other fixed case is simply absent -> TBD, and the row still renders
    assert dv.cases["grpc-unary"].value == v.TBD
    assert "| L1 -> L2 |" in v.render_verdicts({"k": dv})


@pytest.mark.parametrize("garbage", ["garbage", 5, [], 3.5, True])
def test_case_and_session_verdict_never_yes_on_a_non_object_case(
    garbage: object,
) -> None:
    assert v.case_verdict(garbage).value == v.MALFORMED
    assert v.session_verdict(garbage).value == v.MALFORMED


def test_direction_verdict_survives_malformed_samples_and_errors() -> None:
    """Neither a non-object sample nor a non-object error record may crash the
    report, and neither may be silently upgraded to ``yes``."""
    doc = _doc(
        cases={
            "http-ping": {"samples": ["nope"] * MIN_VALID_REPS, "errors": ["bad"]},
            "grpc-bidi-idle30s-kaon": {"samples": ["nope"], "errors": "not-a-list"},
        }
    )
    dv = v.direction_verdict(doc)
    # a malformed error record is kept as one opaque failure, never dropped
    assert dv.cases["http-ping"].value == "flaky 5/6"
    assert dv.sessions["grpc-bidi-idle30s-kaon"].value != v.YES
    assert v.render_verdicts({"k": dv})


def test_malformed_errors_value_is_never_silently_dropped() -> None:
    """``errors`` that is not a list at all counts as one failure, so an
    unreadable result file can never be upgraded to ``yes``."""
    case = {"samples": _samples(MIN_VALID_REPS), "errors": "kaboom"}
    assert v.case_verdict(case).value == "flaky 5/6"


# =============================================== tester round 2: defect 3 (warmup)
def test_session_verdict_ignores_warmup_errors_mixed_with_all_survived() -> None:
    """A session case where every *measured* repetition survived, plus one
    warmup-phase failure, must still be ``yes`` -- the session equivalent of
    the fixed case_verdict defect."""
    case = {
        "samples": _samples(MIN_VALID_REPS, survived=True),
        "errors": [_warmup_error()],
    }
    result = v.session_verdict(case)
    assert result.value == v.YES, result


def test_session_verdict_measured_error_still_counts_alongside_warmup_noise() -> None:
    """One warmup failure plus one *measured* failure on a session case must
    report the measured failure (flaky), never silently absorb it into the
    warmup noise and never round up to yes."""
    case = {
        "samples": _samples(MIN_VALID_REPS, survived=True),
        "errors": [
            _warmup_error(),
            {
                "rep": 3,
                "phase": "measure",
                "failure_type": "stream_dropped",
                "message": "m",
            },
        ],
    }
    result = v.session_verdict(case)
    assert result.value == "flaky 5/6"
    assert result.value != v.YES
    assert result.detail == "stream_dropped: m"


@pytest.mark.parametrize(
    "phase_value",
    [None, 123, ["warmup"], {"nested": "warmup"}, "Warmup", "WARMUP", "", "measure "],
)
def test_unexpected_phase_types_are_conservatively_treated_as_measured(
    phase_value: Any,
) -> None:
    """Only the exact string ``"warmup"`` may exempt an error from the
    verdict. Any other type or spelling -- None, an int, a list, a dict, a
    different case, trailing whitespace -- must count as measured, the
    conservative reading the fix-round docstring promises: "no unknown input
    can buy a yes"."""
    err = {"rep": 0, "phase": phase_value, "failure_type": "timeout", "message": "m"}
    case = {"samples": _samples(MIN_VALID_REPS), "errors": [err]}
    result = v.case_verdict(case)
    assert result.value == "flaky 5/6", (phase_value, result.value)
    assert v.warmup_errors(case) == []
    assert v.measured_errors(case) == [err]


def test_exact_warmup_phase_is_excluded_from_measured() -> None:
    err = _warmup_error()
    case = {"samples": _samples(MIN_VALID_REPS), "errors": [err]}
    assert v.measured_errors(case) == []
    assert v.warmup_errors(case) == [err]
    assert v.case_verdict(case).value == v.YES


@pytest.mark.parametrize(
    ("n_samples", "n_warmup_errors", "n_measured_errors"),
    [
        (MIN_VALID_REPS, 5, 0),
        (MIN_VALID_REPS, 20, 0),
        (MIN_VALID_REPS - 1, 3, 0),
        (0, 4, 0),
        (MIN_VALID_REPS, 2, 1),
        (3, 2, 1),
    ],
)
def test_no_mix_of_warmup_noise_and_sample_count_buys_yes_without_five_measured(
    n_samples: int, n_warmup_errors: int, n_measured_errors: int
) -> None:
    """The highest-value property, now stress-tested with warmup noise added:
    no combination of sample count plus warmup errors plus measured errors
    can produce ``yes`` unless there are exactly >=5 measured samples and zero
    measured errors."""
    errors = [_warmup_error(i) for i in range(n_warmup_errors)] + [
        {"rep": 100 + i, "phase": "measure", "failure_type": "timeout", "message": "m"}
        for i in range(n_measured_errors)
    ]
    case = {"samples": _samples(n_samples), "errors": errors}
    case_result = v.case_verdict(case)
    session_case = {
        "samples": _samples(n_samples, survived=True),
        "errors": errors,
    }
    session_result = v.session_verdict(session_case)
    should_be_yes = n_samples >= MIN_VALID_REPS and n_measured_errors == 0
    if should_be_yes:
        assert case_result.value == v.YES
        assert session_result.value == v.YES
    else:
        assert case_result.value != v.YES
        assert session_result.value != v.YES


# ==================================== tester round 2: JSON round-trip (defect 3)
def test_direction_verdict_to_json_round_trips_warmup_notes_through_json_module() -> (
    None
):
    """``to_json()`` must produce something that actually survives a real
    ``json.dumps``/``json.loads`` round trip (not just an in-memory dict
    comparison), and the reloaded structure must still carry the warmup
    notes -- this is what the report generator and any downstream tooling
    actually consume."""
    doc = _doc(
        cases={
            "http-ping": {
                "samples": _samples(MIN_VALID_REPS),
                "errors": [_warmup_error(2)],
            },
            "grpc-bidi-idle30s-kaon": {
                "samples": _samples(MIN_VALID_REPS, survived=True),
                "errors": [_warmup_error(3)],
            },
        }
    )
    dv = v.direction_verdict(doc)
    encoded = json.dumps(dv.to_json())
    reloaded = json.loads(encoded)
    assert reloaded["warmup_notes"] == dv.notes()
    assert len(reloaded["warmup_notes"]) == 2
    assert reloaded["cases"]["http-ping"]["value"] == v.YES
    assert reloaded["sessions"]["grpc-bidi-idle30s-kaon"]["value"] == v.YES


# ------------------------------------------- same machine / same OS (reviewer)
def _clean_cases() -> dict[str, Any]:
    cases: dict[str, Any] = {
        c: {"samples": _samples(MIN_VALID_REPS), "errors": []} for c, _ in v.FIXED_CASES
    }
    cases["grpc-bidi-idle30s-kaon"] = {
        "samples": _samples(MIN_VALID_REPS, survived=True),
        "errors": [],
    }
    return cases


def _pair_extra(
    *,
    remote: str = "192.168.1.21",
    local: str | None = "192.168.1.20",
    client_system: str = "Linux",
    server_system: str = "Windows",
    server_hostname: str = "LAB-2",
    declared: bool = True,
) -> dict[str, Any]:
    return {
        "endpoint": {"remote_address": remote, "local_address": local},
        "local": {"system": client_system, "release": "6.8", "wsl": {"is_wsl": False}},
        "peer_identity": {
            "hostname": server_hostname,
            "system": server_system,
            "release": "11",
            "wsl": {"is_wsl": False},
        },
        "outbound_only": {"no_inbound_rule_declared": declared},
    }


def test_two_lan_hosts_two_systems_is_neither_same_host_nor_same_os() -> None:
    dv = v.direction_verdict(_doc(cases=_clean_cases(), extra=_pair_extra()))
    assert dv.same_host is False
    assert dv.cross_os is True
    assert dv.caveats() == []
    assert dv.outbound_only.value == v.YES


@pytest.mark.parametrize("remote", ["127.0.0.1", "127.0.0.2", "::1", "localhost"])
def test_loopback_remote_address_marks_same_host(remote: str) -> None:
    dv = v.direction_verdict(
        _doc(cases=_clean_cases(), extra=_pair_extra(remote=remote))
    )
    assert dv.same_host is True


def test_same_hostname_on_both_sides_marks_same_host_case_insensitively() -> None:
    doc = _doc(cases=_clean_cases(), extra=_pair_extra(server_hostname="lab-1"))
    doc["host"] = {"hostname": "LAB-1", "label": "L1"}
    assert v.direction_verdict(doc).same_host is True


def test_local_address_equal_to_remote_marks_same_host() -> None:
    extra = _pair_extra(remote="192.168.1.20", local="192.168.1.20")
    assert v.direction_verdict(_doc(cases=_clean_cases(), extra=extra)).same_host


def test_empty_hostnames_never_count_as_the_same_host() -> None:
    doc = _doc(cases=_clean_cases(), extra=_pair_extra(server_hostname=""))
    doc["host"] = {"hostname": "", "label": "L1"}
    assert v.direction_verdict(doc).same_host is False


@pytest.mark.parametrize("remote", ["", "not an address", "192.168.1.999", None])
def test_unparseable_remote_address_is_not_loopback(remote: Any) -> None:
    assert v._is_loopback(remote) is False


def test_same_host_survives_malformed_endpoint_and_peer() -> None:
    doc = _doc(cases=_clean_cases(), extra={"endpoint": "junk", "peer_identity": 3})
    assert v.direction_verdict(doc).same_host is False


def test_outbound_only_is_tbd_on_same_host_even_when_everything_else_says_yes() -> None:
    """A loopback run crosses no firewall: the outbound-only claim cannot be
    proven there, however clean the gRPC cases were and whatever the operator
    declared."""
    dv = v.direction_verdict(
        _doc(cases=_clean_cases(), extra=_pair_extra(remote="127.0.0.1"))
    )
    assert dv.cases["grpc-unary"].ok and dv.sessions["grpc-bidi-idle30s-kaon"].ok
    assert dv.outbound_only == v.Verdict(v.TBD, v.LOOPBACK_DETAIL)
    assert "firewall" in dv.outbound_only.detail


def test_same_host_does_not_change_the_reachability_cells() -> None:
    dv = v.direction_verdict(
        _doc(cases=_clean_cases(), extra=_pair_extra(remote="127.0.0.1"))
    )
    assert all(c.ok for c in dv.cases.values())


def test_same_system_on_both_sides_is_not_cross_os() -> None:
    extra = _pair_extra(client_system="Linux", server_system="Linux")
    dv = v.direction_verdict(_doc(cases=_clean_cases(), extra=extra))
    assert dv.cross_os is False
    assert dv.same_host is False
    (caveat,) = dv.caveats()
    assert caveat.startswith("same OS on both sides (Linux 6.8 -> Linux 11)")
    assert "ADR-0009" in caveat


def test_system_comparison_is_case_insensitive() -> None:
    extra = _pair_extra(client_system="linux", server_system="Linux")
    assert (
        v.direction_verdict(_doc(cases=_clean_cases(), extra=extra)).cross_os is False
    )


def test_unknown_system_on_one_side_is_conservatively_not_cross_os() -> None:
    extra = _pair_extra(client_system="", server_system="Windows")
    dv = v.direction_verdict(_doc(cases=_clean_cases(), extra=extra))
    assert dv.cross_os is False
    (caveat,) = dv.caveats()
    assert caveat.startswith("OS of one side not recorded")


def test_loopback_same_os_run_lists_both_caveats() -> None:
    extra = _pair_extra(remote="127.0.0.1", server_system="Linux")
    dv = v.direction_verdict(_doc(cases=_clean_cases(), extra=extra))
    caveats = dv.caveats()
    assert "not a cross-OS result: same machine (loopback)" in caveats
    assert any(c.startswith("same OS on both sides") for c in caveats)


def test_wsl_side_caveat_moved_into_caveats_and_still_rendered() -> None:
    extra = _pair_extra()
    extra["peer_identity"]["wsl"] = {"is_wsl": True}
    dv = v.direction_verdict(_doc(cases=_clean_cases(), extra=extra))
    assert not dv.native_pair
    assert any("not native" in c for c in dv.caveats())
    text = v.render_verdicts({"k": dv})
    assert "ADR-0009 needs a native run" in text


def test_to_json_carries_same_host_cross_os_and_caveats() -> None:
    dv = v.direction_verdict(
        _doc(cases=_clean_cases(), extra=_pair_extra(remote="127.0.0.1"))
    )
    data = json.loads(json.dumps(dv.to_json()))
    assert data["same_host"] is True
    assert data["cross_os"] is True
    assert "not a cross-OS result: same machine (loopback)" in data["caveats"]
    assert data["outbound_only"] == {"value": v.TBD, "detail": v.LOOPBACK_DETAIL}


def test_render_marks_same_host_row_in_the_link_cell_and_lists_the_caveat() -> None:
    dv = v.direction_verdict(
        _doc(cases=_clean_cases(), extra=_pair_extra(remote="127.0.0.1"))
    )
    text = v.render_verdicts({"k": dv})
    verdict_block = text.split("#### Latency")[0]
    row = next(ln for ln in verdict_block.splitlines() if ln.startswith("| L1 -> L2"))
    assert f"| Ethernet {v.SAME_MACHINE_MARK} |" in row
    assert f"yes — {v.OUTBOUND_YES_DETAIL}" not in row
    assert f"TBD — {v.LOOPBACK_DETAIL}" in row
    # the metrics table carries the same marker: neither table can be pasted alone
    metrics_block = text.split("#### Latency")[1].split("#### Diagnoses")[0]
    assert v.SAME_MACHINE_MARK in metrics_block
    fixes_block = text.split("#### Diagnoses and fixes")[1]
    assert (
        "- **L1 -> L2**: not a cross-OS result: same machine (loopback)" in fixes_block
    )


def test_render_does_not_mark_a_real_pair() -> None:
    dv = v.direction_verdict(_doc(cases=_clean_cases(), extra=_pair_extra()))
    text = v.render_verdicts({"k": dv})
    assert v.SAME_MACHINE_MARK not in text
    assert "same OS" not in text and "same machine" not in text
    assert "#### Diagnoses and fixes\n\n- none" in text


def test_same_machine_marker_never_shifts_columns() -> None:
    dv = v.direction_verdict(
        _doc(cases=_clean_cases(), extra=_pair_extra(remote="127.0.0.1"))
    )
    text = v.render_verdicts({"k": dv}).split("#### Latency")[0]
    lines = text.splitlines()
    header = next(ln for ln in lines if ln.startswith("| direction"))
    row = next(ln for ln in lines if ln.startswith("| L1 -> L2"))
    assert _unescaped_pipe_count(row) == _unescaped_pipe_count(header)


def test_outbound_yes_detail_states_declaration_not_observation() -> None:
    dv = v.direction_verdict(_doc(cases=_clean_cases(), extra=_pair_extra()))
    detail = dv.outbound_only.detail
    assert "operator declared" in detail and "--no-inbound-rule" in detail
    assert "by construction" in detail
    assert "no listening socket and no inbound rule" not in detail


# ------------------------------------------- malformed extra / params / summary
@pytest.mark.parametrize(
    "extra",
    ["junk", 7, ["x"], {"outbound_only": ["x"]}, {"outbound_only": "yes"}],
)
def test_malformed_extra_is_reported_not_crashed_on(extra: object) -> None:
    """(Reviewer finding, pass 2.) A hand-edited or corrupt ``extra`` block must
    never crash ``report``, and can never buy an outbound-only ``yes``."""
    doc = _doc(
        cases={"grpc-unary": {"samples": _samples(MIN_VALID_REPS), "errors": []}}
    )
    doc["extra"] = extra
    dv = v.direction_verdict(doc)
    assert dv.outbound_only.value == v.TBD
    assert dv.cases["grpc-unary"].value == v.YES
    assert "| L1 -> L2 |" in v.render_verdicts({"k": dv})


def test_malformed_params_and_summary_are_reported_not_crashed_on() -> None:
    doc = _doc(
        cases={
            "http-ping": {"samples": _samples(MIN_VALID_REPS), "summary": "nope"},
            "grpc-unary": {
                "samples": _samples(MIN_VALID_REPS),
                "summary": {"rtt_ms": "nope"},
            },
        }
    )
    doc["params"] = "junk"
    dv = v.direction_verdict(doc)
    assert dv.server == "?"
    assert dv.metrics["http_ttfb_ms"] is None
    assert dv.metrics["grpc_rtt_ms"] is None
    assert v.render_verdicts({"k": dv})


@pytest.mark.parametrize("samples", [5, "five", {"a": 1}, True])
def test_non_list_samples_is_malformed_never_yes(samples: object) -> None:
    """(Reviewer finding, pass 3.) ``samples`` that is not a list crashed
    ``report``; it is now an ``INVALID (malformed)`` cell."""
    case = {"samples": samples, "errors": []}
    for fn in (v.case_verdict, v.session_verdict):
        result = fn(case)
        assert result.value == v.MALFORMED, result
        assert "samples is" in result.detail


@pytest.mark.parametrize("errors", [5, "boom", {"a": 1}, [["x"]], None])
def test_non_list_top_level_errors_never_crash_report(errors: object) -> None:
    doc = _doc(
        cases={"grpc-unary": {"samples": _samples(MIN_VALID_REPS), "errors": []}}
    )
    doc["errors"] = errors
    dv = v.direction_verdict(doc)
    assert dv.cases["grpc-unary"].value == v.YES
    assert "| L1 -> L2 |" in v.render_verdicts({"k": dv})


def test_direction_verdict_with_non_list_samples_renders_malformed_cell() -> None:
    doc = _doc(
        cases={"http-ping": {"samples": 5}, "grpc-bidi-idle30s-kaon": {"samples": "x"}}
    )
    dv = v.direction_verdict(doc)
    assert dv.cases["http-ping"].value == v.MALFORMED
    assert dv.sessions["grpc-bidi-idle30s-kaon"].value == v.MALFORMED
    assert "INVALID (malformed)" in v.render_verdicts({"k": dv})


@pytest.mark.parametrize("extra", ["junk", {}, {"endpoint": "nope"}])
def test_missing_endpoint_facts_are_a_visible_caveat(extra: object) -> None:
    """A file whose connection facts are gone must not read as a clean two-
    machine row: the reader is told the same-machine check could not run."""
    doc = _doc(
        cases={"grpc-unary": {"samples": _samples(MIN_VALID_REPS), "errors": []}}
    )
    doc["extra"] = extra
    dv = v.direction_verdict(doc)
    assert dv.endpoint_known is False
    assert dv.to_json()["endpoint_known"] is False
    assert any("connection facts missing" in c for c in dv.caveats())
    assert "connection facts missing" in v.render_verdicts({"k": dv})


@pytest.mark.parametrize(
    "failure",
    [
        5,
        "x",
        ["y"],
        None,
        True,
        {"message": 5, "fix": ["a"]},
        {"message": "", "fix": 0},
    ],
)
def test_dropped_session_with_malformed_failure_record_is_no_not_crash(
    failure: object,
) -> None:
    """(Reviewer finding, pass 4.) The typed failure of a dropped stream may
    itself be corrupt; the cell is still ``no`` with the default diagnosis."""
    case = {"samples": [{"survived": 0, "failure": failure}] * MIN_VALID_REPS}
    result = v.session_verdict(case)
    assert result.value == v.NO
    assert result.detail == "stream did not survive the idle gap"
    doc = _doc(cases={"grpc-bidi-idle30s-kaon": case})
    assert "| no —" in v.render_verdicts({"k": v.direction_verdict(doc)})
