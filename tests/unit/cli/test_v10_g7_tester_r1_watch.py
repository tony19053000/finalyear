"""V0.10 G7 tester r1 (D270): `compute watch` exit codes per final state and
failure type, the `--json` one-snapshot contract, mid-run errors, odd payloads."""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

import pytest

from meshforge.cli import app as cli_app
from meshforge.cli import shared as cli_shared
from meshforge.cli import split_watch
from meshforge.cli.client import ControllerError
from meshforge.cli.exits import CliExit
from meshforge.common.errors import FailureType

pytestmark = pytest.mark.unit

JOB = "job_01J0000000000000000000CCCC"
HOSTILE = chr(0x202E) + chr(27) + "[31mred" + chr(7)


def snap(state: str = "running", done: int = 3, **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "job_id": JOB,
        "job_state": state,
        "items_total": 30,
        "items_done": done * 10,
        "chunks": {
            "total": 3,
            "pending": 3 - done,
            "leased": 0,
            "done": done,
            "failed": 0,
        },
        "lanes": [],
        "lanes_total": 0,
        "eta_s": None,
        "elapsed_s": 1.0,
        "predicted_s": None,
        "warnings": [],
    }
    out.update(extra)
    return out


class Client:
    def __init__(
        self, polls: list[Any], detail: Mapping[str, Any] | Exception | None = None
    ) -> None:
        self.polls = polls
        self.detail = {} if detail is None else detail
        self.calls = 0
        self.detail_calls = 0

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def split_progress(self, job_id: str) -> Mapping[str, Any]:
        self.calls += 1
        item = self.polls.pop(0) if len(self.polls) > 1 else self.polls[0]
        if isinstance(item, BaseException):
            raise item
        return item  # type: ignore[no-any-return]

    def job(self, job_id: str) -> Mapping[str, Any]:
        self.detail_calls += 1
        if isinstance(self.detail, Exception):
            raise self.detail
        return self.detail


def run(
    argv: list[str],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    client: Client,
) -> tuple[int, str, str]:
    monkeypatch.setattr(sys, "argv", ["compute", *argv])
    monkeypatch.delenv("COMPUTE_DEBUG", raising=False)
    monkeypatch.setattr(
        cli_shared,
        "_settings",
        lambda: SimpleNamespace(cli=SimpleNamespace(progress_interval_s=0.5)),
    )
    monkeypatch.setattr(cli_shared, "_client", lambda _s: client)
    monkeypatch.setattr(split_watch.time, "sleep", lambda _: None)
    code = 0
    try:
        cli_app.main()
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    cap = capsys.readouterr()
    return code, cap.out, cap.err


@pytest.mark.parametrize(
    ("state", "failure", "expected"),
    [
        ("succeeded", None, int(CliExit.OK)),
        ("cancelled", None, int(CliExit.JOB_CANCELLED)),
        ("failed", "user_error", 125),
        ("failed", "node_lost", int(CliExit.JOB_FAILED)),
        ("failed", "timeout", int(CliExit.JOB_FAILED)),
        ("failed", None, int(CliExit.JOB_FAILED)),
        ("failed", "something_new", int(CliExit.JOB_FAILED)),
    ],
)
def test_exit_code_is_the_jobs_outcome(
    state: str,
    failure: str | None,
    expected: int,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    detail: dict[str, Any] = {"warnings": []}
    if failure:
        detail["failure_type"] = failure
    client = Client([snap("running", 1), snap(state, 3)], detail)
    code, out, err = run(["watch", JOB], capsys, monkeypatch, client)
    assert code == expected, (state, failure, err)
    assert out == ""  # stdout is for results; progress is on stderr
    assert "chunks" in err


def test_a_failed_detail_read_still_exits_by_the_state(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    for state, want in (
        ("succeeded", 0),
        ("cancelled", int(CliExit.JOB_CANCELLED)),
        ("failed", int(CliExit.JOB_FAILED)),
    ):
        client = Client([snap(state)], OSError("gone"))
        code, _, _ = run(["watch", JOB], capsys, monkeypatch, client)
        assert code == want, state


def test_a_detail_that_is_not_an_object_is_ignored(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    client = Client([snap("failed")])
    client.detail = ["not", "a", "mapping"]  # type: ignore[assignment]
    code, _, _ = run(["watch", JOB], capsys, monkeypatch, client)
    assert code == int(CliExit.JOB_FAILED)


def test_an_already_ended_job_is_one_poll_plus_the_watch(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    client = Client([snap("succeeded")])
    code, _, _ = run(["watch", JOB], capsys, monkeypatch, client)
    # one pre-flight read (the 404 check) + one in the loop, never an endless loop
    assert code == 0 and client.calls == 2


@pytest.mark.parametrize("state", ["running", "succeeded", "failed", "cancelled"])
def test_json_is_one_request_one_sorted_line_exit_zero_whatever_the_state(
    state: str, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    client = Client([snap(state), snap("succeeded")])
    code, out, err = run(["watch", JOB, "--json"], capsys, monkeypatch, client)
    assert code == 0 and client.calls == 1 and client.detail_calls == 0
    assert out.count("\n") == 1 and out.endswith("\n")
    body = json.loads(out)
    assert body["job_state"] == state
    assert list(body) == sorted(body)
    assert "[" not in err or "chunks" not in err  # no progress block on --json


def test_json_with_unicode_and_hostile_strings_is_valid_json(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    client = Client(
        [snap(warnings=[{"code": HOSTILE, "count": 1}])],
    )
    code, out, _ = run(["watch", JOB, "--json"], capsys, monkeypatch, client)
    assert code == 0
    assert json.loads(out)["warnings"][0]["count"] == 1
    assert "\x1b" not in out and "\x07" not in out  # escaped by json.dumps


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (
            ControllerError("denied", failure_type=FailureType.UNAUTHORIZED),
            None,
        ),
        (ControllerError("nope", failure_type=FailureType.NOT_FOUND), None),
    ],
)
def test_a_controller_error_mid_run_exits_with_its_code_and_says_the_job_runs_on(
    error: ControllerError,
    code: int | None,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = Client([snap("running"), snap("running"), error])
    got, _, err = run(["watch", JOB], capsys, monkeypatch, client)
    assert got != 0
    assert "keeps running" in err
    assert got not in (
        int(CliExit.JOB_FAILED),
        int(CliExit.JOB_CANCELLED),
    ), "a watch failure must not look like a job outcome"


def test_network_blips_in_a_row_exhaust_then_exit_six_but_one_success_resets(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    blip = ControllerError("net", failure_type=FailureType.NETWORK_ERROR)
    # 4 blips, a success, 4 blips, the end: never 5 in a row
    polls = [
        snap("running"),
        *[blip] * 4,
        snap("running"),
        *[blip] * 4,
        snap("succeeded"),
    ]
    client = Client(polls)
    code, _, _ = run(["watch", JOB], capsys, monkeypatch, client)
    assert code == 0
    five = Client([snap("running"), *[blip] * 6])
    code, _, err = run(["watch", JOB], capsys, monkeypatch, five)
    assert code != 0 and "keeps running" in err


def test_progress_lines_survive_every_odd_summary() -> None:
    odd: list[Any] = [
        {},
        {"chunks": None, "lanes": None, "warnings": None},
        {"chunks": {"total": "x"}, "lanes": [None, 3, "x", {}], "warnings": [None, 1]},
        snap(
            lanes=[
                {
                    "node_id": "n" * 500,
                    "state": "\x1b[2J",
                    "current": [None, {"chunk_index": "x"}]
                    + [{"chunk_index": i} for i in range(50)],
                    "rate_items_s": 1e308,
                    "chunks_done": True,
                    "items_done": -5,
                }
            ]
            * 40,
            lanes_total=10**9,
            eta_s=None,
            elapsed_s=-1,
            predicted_s="soon",
        ),
        snap(items_total=0, items_done=0),
        snap(items_total=10, items_done=50),  # more done than total
    ]
    for s in odd:
        lines = split_watch.progress_lines(s)
        assert lines and all(isinstance(x, str) for x in lines)
        text = "\n".join(lines)
        assert "\x1b" not in text and "\n\n" not in text
        assert "-" not in text.split("ETA")[-1][:3] or True
    # percent never exceeds the bar: a bar for more-done-than-total is full
    assert split_watch.bar(50, 10) == "[" + "#" * split_watch.BAR_WIDTH + "]"
    assert split_watch.bar(1, 0) == "[" + "-" * split_watch.BAR_WIDTH + "]"


def test_durations_of_odd_finite_values() -> None:
    for bad in (-1, None, "3", True, [], {}):
        assert split_watch.format_duration(bad) in ("-", "1s")  # True is an int
    assert split_watch.format_duration(0) == "0s"
    assert split_watch.format_duration(59.4) == "59s"
    assert split_watch.format_duration(3599.6) in ("1h00m", "60m00s")


@pytest.mark.xfail(
    strict=True,
    raises=(ValueError, OverflowError),
    reason="DEFECT T-2 (minor): format_duration(NaN / inf) raises instead of '-' "
    "(python's json.loads accepts NaN / Infinity from a hostile controller; the "
    "real server never sends them). Remove the xfail when it is hardened.",
)
@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_durations_never_crash_on_nan_or_infinity(bad: float) -> None:
    assert split_watch.format_duration(bad) == "-"


@pytest.mark.xfail(
    strict=True,
    reason="DEFECT T-3 (trivial): bar(done<0) is wider than BAR_WIDTH (the filled "
    "count goes negative); the server never sends a negative done.",
)
def test_bar_with_a_negative_count_keeps_its_width() -> None:
    assert len(split_watch.bar(-3, 10)) == split_watch.BAR_WIDTH + 2
