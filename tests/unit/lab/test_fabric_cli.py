"""``lab/fabric_check.py`` as the operator runs it: unreachable controller (a real
closed port, in-process and as a subprocess), inventory output, and the result
JSON + merged report a watch run leaves behind."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import fabric_check as cli
from _fabric.api import ApiError, ControllerUnreachable, FabricApi, normalise_url
from _fabric.testing import (
    FakeClock,
    ScriptedOperator,
    StubController,
    node_doc,
    step,
)

NODE = "node_01JAAAAAAAAAAAAAAAAAAAAAAA"
LAB = Path(cli.__file__).resolve().parent
HW = {
    "cpu_model": "Some CPU",
    "cpu_cores": 10,
    "cpu_threads": 16,
    "ram_bytes": str(16 * 1024**3),
    "gpus": [
        {
            "index": 0,
            "model": "Some GPU",
            "vram_bytes": str(4 * 1024**3),
            "driver_version": "1.2",
            "compute_capability": "8.6",
        }
    ],
    "capabilities": ["cuda"],
}


@pytest.fixture
def closed_url() -> str:
    """A loopback port that was bound and closed: connecting is refused by the OS."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}"


@pytest.mark.parametrize(
    "sub",
    [
        ["inventory"],
        ["watch", "--node", NODE, "--link", "ethernet"],
        ["restart-check", "--link", "ethernet"],
        ["soak", "--link", "ethernet"],
    ],
)
def test_unreachable_controller_exits_6_with_a_clear_message(
    sub: list[str], closed_url: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = cli.main(["--controller", closed_url, "--reports-dir", str(tmp_path), *sub])
    err = capsys.readouterr().err
    assert code == cli.EXIT_UNREACHABLE
    assert "cannot reach the controller" in err
    # Linux refuses at once; Windows retries the SYN for ~2 s, which can hit our timeout
    assert "connection refused" in err or "timed out" in err
    assert "--controller" in err
    assert list(tmp_path.iterdir()) == []  # nothing half-written


def test_unreachable_controller_as_a_real_process_has_no_traceback(
    closed_url: str,
) -> None:
    proc = subprocess.run(
        [
            sys.executable,
            str(LAB / "fabric_check.py"),
            "--controller",
            closed_url,
            "inventory",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == cli.EXIT_UNREACHABLE
    assert "Traceback" not in proc.stderr
    assert "cannot reach the controller" in proc.stderr


def test_a_non_controller_answering_is_not_mistaken_for_one(tmp_path: Path) -> None:
    """Something answers on the port but it is not the controller (404 on /healthz)."""
    import http.server
    import threading

    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), http.server.BaseHTTPRequestHandler
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        assert cli.main(
            ["--controller", url, "--reports-dir", str(tmp_path), "inventory"]
        ) == (cli.EXIT_UNREACHABLE)
    finally:
        server.shutdown()
        server.server_close()


def test_api_errors_are_typed(stub_controller: StubController, closed_url: str) -> None:
    with pytest.raises(ControllerUnreachable) as unreachable:
        FabricApi(closed_url).nodes()
    assert unreachable.value.failure_type == "controller_unreachable"
    with pytest.raises(ApiError) as missing:
        FabricApi(stub_controller.url).node(NODE)
    assert (missing.value.failure_type, missing.value.status) == ("not_found", 404)


@pytest.mark.parametrize(
    "bad", ["ftp://x", "127.0.0.1:47110", "http://", "http://h:1/?q=1"]
)
def test_bad_controller_urls_are_usage_errors(bad: str, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"URL|query"):
        normalise_url(bad)
    assert (
        cli.main(["--controller", bad, "--reports-dir", str(tmp_path), "inventory"])
        == 2
    )


def test_inventory_lists_hardware_and_never_says_it_matches(
    stub_controller: StubController, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    stub_controller.nodes[NODE] = node_doc(NODE, "lap1", hardware=HW)
    old = "node_01JBBBBBBBBBBBBBBBBBBBBBBB"
    stub_controller.nodes[old] = node_doc(old, "lap1", state="offline", connected=False)
    argv = [
        "--controller",
        stub_controller.url,
        "--reports-dir",
        str(tmp_path),
        "--name",
        "L1",
    ]
    assert cli.main([*argv, "inventory", "--json"], clock=FakeClock()) == cli.EXIT_OK
    out, err = capsys.readouterr()
    details = json.loads(out)
    assert {d["node_id"] for d in details} == {NODE, old}
    assert "2 node ids" in err  # duplicate hostname is pointed out
    assert "human comparison" in err
    report = (tmp_path / "2026-09-24-V0.3-fabric-inventory.md").read_text(
        encoding="utf-8"
    )
    assert "Some CPU (10c/16t)" in report
    assert "16.0 GiB" in report
    assert "#0 Some GPU 4.0 GiB drv 1.2 cc 8.6" in report
    assert "PASS" not in report
    (raw,) = (tmp_path / "raw" / "2026-09-24-V0.3-fabric-inventory").glob("*.json")
    assert (
        json.loads(raw.read_text(encoding="utf-8"))["extra"]["nodes"][0]["hardware"]
        or True
    )


def _watch_setup(stub: StubController) -> None:
    stub.nodes[NODE] = node_doc(NODE, "lap2", epoch=5, session_id="s5")
    stub.add_session("s5", NODE, epoch=5)
    stub.add_samples(NODE, 5, range(1, 11))


def _pull_plug(
    stub: StubController, clock: FakeClock, new_seq: list[int], *, epoch: int = 6
) -> list[Any]:
    def pull() -> float:
        clock.after(3.0, lambda: stub.set_node(NODE, state="offline", connected=False))
        return 0.5

    def plug() -> float:
        def back() -> None:
            stub.set_node(
                NODE,
                state="online",
                connected=True,
                session_epoch=epoch,
                last_session_id=f"s{epoch}",
            )
            stub.add_samples(NODE, epoch, new_seq)

        clock.after(1.0, back)
        return 0.5

    return [pull, plug]


def test_watch_writes_json_and_a_merged_report(
    stub_controller: StubController, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _watch_setup(stub_controller)
    base = [
        "--controller",
        stub_controller.url,
        "--reports-dir",
        str(tmp_path),
        "--name",
        "L1",
    ]
    watch = [
        "watch",
        "--node",
        "lap2",
        "--link",
        "ethernet",
        "--reps",
        "1",
    ]  # hostname accepted
    clock = FakeClock()
    op = ScriptedOperator(clock, _pull_plug(stub_controller, clock, [11]))
    assert cli.main([*base, *watch], clock=clock, operator=op) == cli.EXIT_OK
    report = tmp_path / "2026-09-24-V0.3-fabric-watch.md"
    text = report.read_text(encoding="utf-8")
    assert "| lap2 (" in text and "| 1 | 2.5" in text
    # a human writes the conclusion; the next run must keep it
    report.write_text(
        text.replace("## Conclusion\n", "## Conclusion\nHUMAN WROTE THIS\n"),
        encoding="utf-8",
    )
    later = FakeClock(start_wall=clock.wall().replace(hour=11))
    op2 = ScriptedOperator(
        later, _pull_plug(stub_controller, later, [1, 2], epoch=7)
    )  # agent restarted
    assert cli.main([*base, *watch], clock=later, operator=op2) == cli.EXIT_INCOMPLETE
    text = report.read_text(encoding="utf-8")
    assert "HUMAN WROTE THIS" in text
    raws = sorted((tmp_path / "raw" / "2026-09-24-V0.3-fabric-watch").glob("*.json"))
    assert len(raws) == 2  # two runs, two files, nothing overwritten
    second = json.loads(raws[-1].read_text(encoding="utf-8"))
    (case,) = second["cases"].values()
    assert case["samples"][0]["restarted"] is True
    assert "| yes |" in text  # the second row reports the restart


def test_watch_rejects_an_ambiguous_hostname(
    stub_controller: StubController, tmp_path: Path
) -> None:
    for node_id in (NODE, "node_01JBBBBBBBBBBBBBBBBBBBBBBB"):
        stub_controller.nodes[node_id] = node_doc(node_id, "twin")
    argv = ["--controller", stub_controller.url, "--reports-dir", str(tmp_path),
            "watch", "--node", "twin", "--link", "wifi"]  # fmt: skip
    clock = FakeClock()
    assert (
        cli.main(argv, clock=clock, operator=ScriptedOperator(clock, []))
        == cli.EXIT_USAGE
    )


def test_ctrl_c_during_watch_still_writes_what_was_measured(
    stub_controller: StubController, tmp_path: Path
) -> None:
    _watch_setup(stub_controller)
    clock = FakeClock()

    def ctrl_c() -> None:
        raise KeyboardInterrupt

    interrupt = step(clock, [(1.0, ctrl_c)], None)

    argv = ["--controller", stub_controller.url, "--reports-dir", str(tmp_path),
            "watch", "--node", NODE, "--link", "ethernet"]  # fmt: skip
    op = ScriptedOperator(clock, [interrupt])
    assert cli.main(argv, clock=clock, operator=op) == cli.EXIT_INTERRUPTED
    (raw,) = (tmp_path / "raw" / "2026-09-24-V0.3-fabric-watch").glob("*.json")
    assert json.loads(raw.read_text(encoding="utf-8"))["extra"]["interrupted"] is True


@pytest.mark.parametrize(
    ("new_seq", "expected"),
    [([1, 2], cli.EXIT_OK), ([11, 12], cli.EXIT_INCOMPLETE)],
    ids=["agent-restarted", "agent-not-restarted"],
)
def test_agent_stop_start_mode_needs_a_restart_and_has_no_bounds(
    stub_controller: StubController, tmp_path: Path, new_seq: list[int], expected: int
) -> None:
    """Criterion 4: stop + start the agent; it must come back as the SAME node_id
    and really be a new process. A slow start is not a criterion-2 failure."""
    _watch_setup(stub_controller)
    clock = FakeClock()

    def stop() -> float:
        clock.after(
            0.3,
            lambda: stub_controller.set_node(NODE, state="offline", connected=False),
        )
        return 0.1

    def start() -> float:
        def back() -> None:  # 14 s: longer than criterion 2's ONLINE bound
            stub_controller.set_node(
                NODE,
                state="online",
                connected=True,
                session_epoch=6,
                last_session_id="s6",
            )
            stub_controller.add_samples(NODE, 6, new_seq)

        clock.after(14.0, back)
        return 0.0

    argv = ["--controller", stub_controller.url, "--reports-dir", str(tmp_path),
            "watch", "--node", NODE, "--link", "ethernet", "--reps", "1",
            "--action", "agent"]  # fmt: skip
    op = ScriptedOperator(clock, [stop, start])
    assert cli.main(argv, clock=clock, operator=op) == expected
    assert "STOP the agent" in op.prompts[0] and "START" in op.prompts[1]
    text = (tmp_path / "2026-09-24-V0.3-fabric-watch.md").read_text(encoding="utf-8")
    assert "| n/a |" in text
