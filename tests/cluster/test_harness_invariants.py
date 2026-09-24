"""The per-scenario invariant check has teeth (roadmap 0.3.14).

Every cluster scenario ends with ``Fabric.check_invariants()``; a check that
could never fail would make every scenario's "and nothing else went wrong" a
lie. Each rule is fed a log that breaks it and must raise; a clean log passes.
No processes, no database: the logs are written by hand.
"""

from __future__ import annotations

import json
import os
import types
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("psutil", reason="the cluster harness needs the agent extra")

from mf_cluster import (
    FORBIDDEN_EVENTS,
    Fabric,
    HarnessError,
    LogFile,
    ManagedProcess,
    Node,
)


def _fabric(
    tmp_path: Path, controller_lines: list[str], agent_lines: list[str]
) -> Fabric:
    db = types.SimpleNamespace(
        host="127.0.0.1",
        port=5432,
        name="x",
        user="x",
        password=types.SimpleNamespace(get_secret_value=lambda: "x"),
    )
    fabric = Fabric(db, tmp_path)
    controller = ManagedProcess("controller", [], dict(os.environ), tmp_path)
    agent = ManagedProcess("agent0", [], dict(os.environ), tmp_path)
    for proc, lines in ((controller, controller_lines), (agent, agent_lines)):
        path = tmp_path / f"{proc.name}.0.log"
        path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
        proc.logs.append(LogFile(path))
        proc.pids.append(os.getpid())  # "started once"; never signalled here
    fabric.controller = controller
    fabric.nodes = [Node(0, agent, link=None, data_dir=tmp_path)]  # type: ignore[arg-type]
    return fabric


def _event(name: str, **extra: Any) -> str:
    return json.dumps({"event": name, "level": "info", **extra})


def test_clean_logs_pass(tmp_path: Path) -> None:
    fabric = _fabric(tmp_path, [_event("session.opened")], [_event("agent.started")])
    fabric.check_invariants()
    fabric._http.close()


@pytest.mark.parametrize("event", sorted(FORBIDDEN_EVENTS))
def test_each_forbidden_event_fails(tmp_path: Path, event: str) -> None:
    fabric = _fabric(tmp_path, [_event(event)], [])
    with pytest.raises(HarnessError, match=event):
        fabric.check_invariants()
    fabric.check_invariants(allow_events=[event])  # an explicit allowance works
    fabric._http.close()


def test_a_logged_exception_fails(tmp_path: Path) -> None:
    fabric = _fabric(
        tmp_path, [], [_event("agent.oops", exception=[{"exc_type": "X"}])]
    )
    with pytest.raises(HarnessError, match="traceback logged"):
        fabric.check_invariants()
    fabric._http.close()


def test_a_raw_traceback_fails(tmp_path: Path) -> None:
    fabric = _fabric(tmp_path, ["Traceback (most recent call last):", '  File "x"'], [])
    with pytest.raises(HarnessError, match="raw traceback"):
        fabric.check_invariants()
    fabric._http.close()


def test_an_unrequested_restart_fails(tmp_path: Path) -> None:
    fabric = _fabric(tmp_path, [], [])
    fabric.nodes[0].process.pids.append(os.getpid())  # a second start nobody asked for
    with pytest.raises(HarnessError, match="restarted without the test asking"):
        fabric.check_invariants()
    fabric.nodes[0].restarts = 1  # ...unless the test did ask
    fabric.check_invariants()
    fabric._http.close()


def test_a_dead_process_that_should_live_fails(tmp_path: Path) -> None:
    fabric = _fabric(tmp_path, [], [])
    fabric.nodes[0].process.expected_alive = True  # but it has no running Popen
    with pytest.raises(HarnessError, match="agent0: exited"):
        fabric.check_invariants()
    fabric._http.close()
