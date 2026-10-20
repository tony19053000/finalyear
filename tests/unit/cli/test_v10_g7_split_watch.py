"""V0.10 G7 (D270): the CLI's split progress - the rendered block, the poll
loop (pipe / JSON / transient errors), ``compute watch`` and ``compute run -f``
routing for split jobs."""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator, Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from meshforge.cli import app as cli_app
from meshforge.cli import shared as cli_shared
from meshforge.cli import split_watch, watch_commands
from meshforge.cli.client import ControllerError
from meshforge.cli.exits import CliExit
from meshforge.common.errors import FailureType

pytestmark = pytest.mark.unit

JOB = "job_01J0000000000000000000CCCC"


def summary(done: int = 30, state: str = "running", **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "job_id": JOB,
        "job_state": state,
        "queue_state": "open",
        "items_total": 600,
        "chunk_items": 10,
        "chunks": {
            "total": 60,
            "pending": 60 - done - 2,
            "leased": 2,
            "done": done,
            "failed": 0,
        },
        "items_done": done * 10,
        "live_leases": 2,
        "speculative_live": 0,
        "speculated": 0,
        "lanes": [
            {
                "attempt_id": "att_1",
                "node_id": "node_A",
                "state": "running",
                "live": True,
                "chunks_done": done - 10,
                "items_done": (done - 10) * 10,
                "failed_tries": 1,
                "current": [
                    {"chunk_index": 41, "lease_seq": 1, "speculative": False},
                    {"chunk_index": 42, "lease_seq": 2, "speculative": True},
                ],
                "rate_items_s": 12.5,
            },
            {
                "attempt_id": "att_2",
                "node_id": "node_B",
                "state": "lost",
                "live": False,
                "chunks_done": 10,
                "items_done": 100,
                "failed_tries": 0,
                "current": [],
                "rate_items_s": None,
            },
        ],
        "lanes_total": 2,
        "rate_items_s": 12.5,
        "eta_s": 24.0,
        "elapsed_s": 75.0,
        "predicted_s": 90.0,
        "warnings": [],
    }
    out.update(extra)
    return out


def test_block_shows_bar_counts_eta_and_one_line_per_node() -> None:
    lines = split_watch.progress_lines(summary())
    head = lines[0]
    assert head.startswith("[############------------] 300/600 items (50%)")
    assert "30/60 chunks" in head and "running" in head
    assert "ETA 24s" in head and "elapsed 1m15s / predicted 1m30s" in head
    assert "1 failed tries" in head and "2 running" in head
    assert "node_A" in lines[1] and "20 chunks" in lines[1]
    assert "12.5 items/s" in lines[1] and "now: 41, 42*" in lines[1]
    assert "node_B" in lines[2] and "lost" in lines[2] and lines[2].count("-") >= 1
    assert len(lines) == 3


def test_block_after_the_end_has_no_eta_and_shows_warnings() -> None:
    lines = split_watch.progress_lines(
        summary(
            done=60,
            state="succeeded",
            warnings=[{"code": "nondeterministic_chunk", "count": 2}],
        )
    )
    assert "ETA" not in lines[0] and "100%" in lines[0]
    assert lines[-1] == "  warning: nondeterministic_chunk x2"


def test_hostile_values_are_flattened_and_odd_shapes_survive() -> None:
    hostile = summary(job_state="x\x1b[2J\r\n", lanes=[{"node_id": "\x1b]0;t\x07n"}])
    for line in split_watch.progress_lines(hostile):
        assert "\x1b" not in line and "\r" not in line and "\n" not in line
    weird = {"chunks": "nope", "lanes": 5, "warnings": {}, "items_total": "9"}
    assert split_watch.progress_lines(weird)[0].startswith("[------------------------]")
    many = summary(lanes=[summary()["lanes"][0]] * 20, lanes_total=20)
    assert split_watch.progress_lines(many)[-1] == "  ... 4 more lane(s)"


@pytest.mark.parametrize(
    ("seconds", "text"),
    [(None, "-"), (-1, "-"), (0, "0s"), (59.4, "59s"), (65, "1m05s"), (3725, "1h02m")],
)
def test_format_duration(seconds: object, text: str) -> None:
    assert split_watch.format_duration(seconds) == text


def _polls(*items: Any) -> Iterator[Any]:
    yield from items


def _fetcher(items: Iterator[Any]) -> Any:
    def fetch() -> Mapping[str, Any]:
        item = next(items)
        if isinstance(item, Exception):
            raise item
        return item

    return fetch


def test_pipe_prints_one_line_per_change_until_the_end(
    capsys: pytest.CaptureFixture[str],
) -> None:
    sleeps: list[float] = []
    final = split_watch.watch_split(
        _fetcher(
            _polls(summary(10), summary(10), summary(20), summary(60, "succeeded"))
        ),
        interval_s=0.5,
        is_terminal=False,
        sleep=sleeps.append,
    )
    assert final["job_state"] == "succeeded"
    err = capsys.readouterr()
    assert err.out == ""
    assert err.err.count("\n") == 3  # the repeated poll printed nothing
    assert sleeps == [0.5, 0.5, 0.5]


def test_json_prints_one_document_per_change_on_stdout(
    capsys: pytest.CaptureFixture[str],
) -> None:
    split_watch.watch_split(
        _fetcher(_polls(summary(10), summary(10), summary(60, "failed"))),
        interval_s=1,
        json_output=True,
        sleep=lambda _: None,
    )
    out = capsys.readouterr()
    docs = [json.loads(line) for line in out.out.splitlines()]
    assert [d["chunks"]["done"] for d in docs] == [10, 60]
    assert all(d["schema_version"] for d in docs)
    assert out.err == ""


def test_transient_errors_are_retried_a_bounded_number_of_times() -> None:
    blip = ControllerError("down", failure_type=FailureType.NETWORK_ERROR)
    final = split_watch.watch_split(
        _fetcher(_polls(blip, blip, summary(60, "succeeded"))),
        interval_s=1,
        is_terminal=False,
        sleep=lambda _: None,
        transient=watch_commands._transient,
    )
    assert final["job_state"] == "succeeded"
    with pytest.raises(ControllerError):
        split_watch.watch_split(
            _fetcher(_polls(*[blip] * split_watch.MAX_TRANSIENT_FAILURES)),
            interval_s=1,
            is_terminal=False,
            sleep=lambda _: None,
            transient=watch_commands._transient,
        )
    denied = ControllerError("no", failure_type=FailureType.UNAUTHORIZED)
    with pytest.raises(ControllerError):
        split_watch.watch_split(
            _fetcher(_polls(denied)),
            interval_s=1,
            is_terminal=False,
            sleep=lambda _: None,
            transient=watch_commands._transient,
        )


def test_terminal_redraws_in_place_and_stops(
    capsys: pytest.CaptureFixture[str],
) -> None:
    split_watch.watch_split(
        _fetcher(_polls(summary(10), summary(60, "succeeded"))),
        interval_s=1,
        is_terminal=True,
        sleep=lambda _: None,
    )
    err = capsys.readouterr().err
    assert "60/60 chunks" in err


# ------------------------------------------------------------------ commands


class FakeClient:
    def __init__(self, polls: list[Any], detail: Mapping[str, Any]) -> None:
        self.polls = polls
        self.detail = detail
        self.calls: list[str] = []

    def __enter__(self) -> FakeClient:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def split_progress(self, job_id: str) -> Mapping[str, Any]:
        self.calls.append(job_id)
        item = self.polls.pop(0) if len(self.polls) > 1 else self.polls[0]
        if isinstance(item, BaseException):
            raise item
        return item

    def job(self, job_id: str) -> Mapping[str, Any]:
        return self.detail


def run_main(
    argv: list[str],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    client: FakeClient,
) -> tuple[int, str, str]:
    monkeypatch.setattr(sys, "argv", ["compute", *argv])
    monkeypatch.delenv("COMPUTE_DEBUG", raising=False)
    monkeypatch.setattr(
        cli_shared,
        "_settings",
        lambda: SimpleNamespace(cli=SimpleNamespace(progress_interval_s=0.5)),
    )
    monkeypatch.setattr(cli_shared, "_client", lambda _settings: client)
    monkeypatch.setattr(split_watch.time, "sleep", lambda _: None)
    code = 0
    try:
        cli_app.main()
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    cap = capsys.readouterr()
    return code, cap.out, cap.err


def test_watch_exits_with_the_jobs_outcome(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ok = FakeClient([summary(10), summary(60, "succeeded")], {"warnings": []})
    code, out, err = run_main(["watch", JOB], capsys, monkeypatch, ok)
    assert code == CliExit.OK and "60/60 chunks" in err and out == ""
    failed = FakeClient(
        [summary(10, "failed")],
        {
            "failure_type": "user_error",
            "warnings": [{"code": "nondeterministic_chunk", "count": 1}],
        },
    )
    code, _, err = run_main(["watch", JOB], capsys, monkeypatch, failed)
    assert code != CliExit.OK
    assert "failed (user_error)" in err and "warning: nondeterministic_chunk" in err


def test_watch_json_and_refusals(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ok = FakeClient([summary(60, "succeeded")], {})
    code, out, _ = run_main(["watch", JOB, "--json"], capsys, monkeypatch, ok)
    assert code == CliExit.OK and json.loads(out)["job_state"] == "succeeded"
    missing = FakeClient(
        [ControllerError("no such split job", failure_type=FailureType.NOT_FOUND)],
        {},
    )
    code, _, err = run_main(["watch", JOB], capsys, monkeypatch, missing)
    assert code == CliExit.NOT_FOUND and "no such split job" in err
    code, _, _ = run_main(["watch", "not-a-job"], capsys, monkeypatch, ok)
    assert code == CliExit.USAGE_ERROR
    code, _, _ = run_main(["watch", JOB, "--interval", "0.1"], capsys, monkeypatch, ok)
    assert code == CliExit.USAGE_ERROR


def test_ctrl_c_detaches(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    client = FakeClient([summary(10), KeyboardInterrupt()], {})
    code, _, err = run_main(["watch", JOB], capsys, monkeypatch, client)
    assert code == CliExit.INTERRUPTED and "keeps running" in err


class RunClient(FakeClient):
    controller_url = "https://127.0.0.1:1"
    auth_header = None
    ssl_context = None

    def submit_job(self, body: Mapping[str, Any], *, idempotency_key: str) -> Any:
        self.submitted = body
        return {"job_id": JOB, "state": "queued", "created": True}


def _project(tmp_path: Path, *, split: bool) -> Path:
    if split:
        lines = [
            "name: p",
            "entrypoint: python main.py {items_file} {out}",
            "split:",
            "  over:",
            "    range: {stop: 10}",
        ]
    else:
        lines = ["name: p", "entrypoint: python main.py"]
    (tmp_path / "compute.yaml").write_text("\n".join(lines) + "\n")
    (tmp_path / "main.py").write_text("print(1)\n")
    return tmp_path


@pytest.mark.parametrize(
    ("split", "extra", "watched"),
    [(True, [], True), (True, ["--no-progress"], False), (False, [], False)],
)
def test_run_follow_shows_progress_for_split_jobs_only(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    split: bool,
    extra: list[str],
    watched: bool,
) -> None:
    from meshforge.cli import job_commands, transfer

    root = tmp_path / "proj"
    root.mkdir()
    project = _project(root, split=split)
    client = RunClient([summary(60, "succeeded")], {})
    followed: list[str] = []

    def follow(_client: Any, job_id: str, _timeout: float) -> None:
        followed.append(job_id)

    monkeypatch.setattr(job_commands, "_follow_to_exit", follow)
    monkeypatch.setattr(job_commands, "_own_node_id", lambda *_a, **_k: None)
    monkeypatch.setattr(
        transfer,
        "upload_tree",
        lambda *_a: SimpleNamespace(
            uploaded_blobs=0, uploaded_bytes=0, skipped_blobs=1, resumed_parts=0
        ),
    )
    monkeypatch.setattr(sys, "argv", ["compute", "run", str(project), "-f", *extra])
    monkeypatch.setattr(
        cli_shared,
        "_settings",
        lambda: SimpleNamespace(
            data_dir=tmp_path,
            cli=SimpleNamespace(progress_interval_s=0.5, timeout_s=5.0),
        ),
    )
    monkeypatch.setattr(cli_shared, "_client", lambda _settings: client)
    monkeypatch.setattr(split_watch.time, "sleep", lambda _: None)
    code = 0
    try:
        cli_app.main()
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 0
    err = capsys.readouterr().err
    assert code == CliExit.OK, err
    assert (client.calls == [JOB]) is watched
    assert (followed == [JOB]) is not watched
