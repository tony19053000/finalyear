"""V0.9 G4 lane B: benchmark conditions through the real executor (D217/D241).

Real burner processes (``cpu_load``), a real fake ``tc`` program (netem: a
script that keeps the qdisc state in a file - real ``tc`` / sudo is never
run), the real marker and start-up sweep. The workload runtime is a fake that
blocks until the test lets it finish, so the condition can be inspected while
the "job" runs. Checked per condition: apply -> verify -> the terminal
status's ``ConditionReport`` -> revert, on done, on cancel, on agent stop;
a forced verification failure fails the attempt ``config_error``
``condition_failed:`` before the workload runs; an agent restart undoes what
a crashed process left (marker sweep).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("psutil", reason="the agent extra")

import psutil

from meshforge.agent.conditions import load as load_module
from meshforge.agent.conditions.marker import BURNER_MODULE, ConditionMarker
from meshforge.agent.conditions.runner import ConditionRunner
from meshforge.agent.execution.executor import Executor
from meshforge.agent.execution.offer import ExecutionSetup
from meshforge.agent.execution.runtime import (
    RunObserver,
    RunResult,
    RuntimeAvailability,
)
from meshforge.agent.execution.selection import execution_offer
from meshforge.agent.settings import AgentSettings
from meshforge.common.bench_conditions import ConditionKind
from meshforge.common.bundle import pack_to_file
from meshforge.common.ids import new_attempt_id, new_command_id, new_job_id
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.common.proto_codec import (
    attempt_state_from_proto,
    failure_type_from_proto,
)

pytestmark = [pytest.mark.component, pytest.mark.timeout(120)]


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in list(os.environ):
        if name.upper().startswith("MESHFORGE_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))


@pytest.fixture(autouse=True)
def _no_burner_survives() -> Any:
    """A failing test must not leave a load running into the next one."""
    yield
    for proc in _burners():
        with contextlib.suppress(psutil.Error):
            proc.kill()


def _spawn(args: list[str], **kwargs: Any) -> subprocess.Popen[Any]:
    return subprocess.Popen(args, stdin=subprocess.DEVNULL, **kwargs)


class GatedRuntime:
    """Enters the run, then waits for ``release`` (or a cancel)."""

    name = "subprocess"

    def __init__(self) -> None:
        self.calls = 0
        self.running = asyncio.Event()
        self.release = asyncio.Event()

    async def probe(self) -> RuntimeAvailability:
        return RuntimeAvailability("subprocess", True)

    async def run(
        self, spec: Any, on_output: Any, *, observer: RunObserver | None = None
    ) -> RunResult:
        assert observer is not None
        self.calls += 1
        observer.enter_run()
        self.running.set()
        await self.release.wait()
        observer.leave_run()
        return RunResult(exit_code=0)


class Reports:
    def __init__(self) -> None:
        self.sent: list[agent_pb2.AttemptStatus] = []

    async def __call__(self, _name: str, payload: agent_pb2.AttemptStatus) -> str:
        copy = agent_pb2.AttemptStatus()
        copy.CopyFrom(payload)
        self.sent.append(copy)
        return str(new_command_id())

    def states(self) -> list[str]:
        return [attempt_state_from_proto(s.state).value for s in self.sent]

    @property
    def final(self) -> agent_pb2.AttemptStatus:
        return self.sent[-1]


FAKE_TC = textwrap.dedent(
    """
    import json, sys
    state_file, log_file, *args = sys.argv[1:]
    with open(log_file, "a") as log:
        log.write(json.dumps(args) + "\\n")
    try:
        state = json.load(open(state_file))
    except FileNotFoundError:
        state = {}
    verb, iface = args[1], args[3]
    if verb == "show":
        print(state.get(iface, "qdisc fq_codel 0: root refcnt 2"))
    elif verb == "replace":
        state[iface] = "qdisc netem 8001: root refcnt 2 limit 1000 " + " ".join(args[6:])
    elif verb == "del":
        if iface not in state:
            print("RTNETLINK answers: No such file or directory", file=sys.stderr)
            sys.exit(2)
        del state[iface]
    else:
        sys.exit(64)
    json.dump(state, open(state_file, "w"))
    """
)


def _fake_tc(tmp_path: Path) -> tuple[list[str], Path, Path]:
    script = tmp_path / "fake_tc.py"
    script.write_text(FAKE_TC)
    state, log = tmp_path / "tc-state.json", tmp_path / "tc-log.jsonl"
    return [sys.executable, str(script), str(state), str(log)], state, log


def _tc_calls(log: Path) -> list[list[str]]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines()]


def _qdisc(state: Path) -> dict[str, str]:
    return json.loads(state.read_text()) if state.exists() else {}


def _executor(
    tmp_path: Path,
    runtime: GatedRuntime,
    reports: Reports,
    *,
    kinds: set[ConditionKind],
    conditions: dict[str, Any] | None = None,
    **runner_extra: Any,
) -> tuple[Executor, str]:
    project = tmp_path / "proj"
    project.mkdir(exist_ok=True)
    (project / "main.py").write_text("print(1)\n")
    bundle = tmp_path / "src.bundle"
    digest = str(pack_to_file(project, bundle).digest)

    async def fake_fetch(_base: str, _digest: Any, _token: str, dest: Path) -> int:
        shutil.copyfile(bundle, dest)
        return dest.stat().st_size  # noqa: ASYNC240 - a test fake

    settings = AgentSettings.load(
        data_dir=tmp_path / "data",
        agent={
            "controller_address": "127.0.0.1:1",
            "execution": {"enabled": True, "allow_unsandboxed": True, "grace_s": 0.5},
            "conditions": {"verify_window_s": 1.0, **(conditions or {})},
        },
    )
    offer = execution_offer(
        enabled=True,
        allow_unsandboxed=True,
        docker=None,
        subprocess=RuntimeAvailability("subprocess", True),
    )
    runner = ConditionRunner(
        settings.agent.conditions,
        data_dir=settings.data_dir,
        kinds=kinds,
        controller=lambda: ("10.0.0.1", 50051),
        **runner_extra,
    )
    executor = Executor(
        settings,
        ExecutionSetup(offer=offer, runtimes={"subprocess": runtime}),  # type: ignore[dict-item]
        node_id="node_x",
        report=reports,
        controller_host=lambda: "127.0.0.1",
        fetch=fake_fetch,
        conditions=runner,
    )
    return executor, digest


def _start(digest: str, condition: str | None) -> agent_pb2.ControllerMessage:
    start = agent_pb2.StartAttempt(
        job_id=str(new_job_id()),
        attempt_id=str(new_attempt_id()),
        fencing_token=1,
        spec=agent_pb2.ExecutionSpec(
            bundle_digest=digest,
            blob_port=47110,
            fetch_token="tok",
            manifest_json='{"name":"x","entrypoint":["python","main.py"]}',
            timeout_s=60,
        ),
    )
    if condition is not None:
        start.condition.spec = condition
    return agent_pb2.ControllerMessage(
        message_id=str(new_command_id()), start_attempt=start
    )


def _cancel(msg: agent_pb2.ControllerMessage) -> agent_pb2.ControllerMessage:
    return agent_pb2.ControllerMessage(
        message_id=str(new_command_id()),
        cancel_attempt=agent_pb2.CancelAttempt(
            attempt_id=msg.start_attempt.attempt_id,
            fencing_token=1,
            reason="user",
        ),
    )


async def _settle(executor: Executor, timeout_s: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        tasks = [a.task for a in executor._attempts.values() if a.task]
        if tasks and all(t.done() for t in tasks):
            return
        await asyncio.sleep(0.02)
    raise AssertionError("attempt did not end")


def _burners() -> list[Any]:
    me = os.getpid()
    out = []
    for proc in psutil.Process(me).children(recursive=True):
        try:
            if BURNER_MODULE in " ".join(proc.cmdline()):
                out.append(proc)
        except psutil.Error:
            continue
    return out


async def _no_burners(timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        alive = [
            p
            for p in _burners()
            if p.is_running() and p.status() != psutil.STATUS_ZOMBIE
        ]
        if not alive:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"burners still alive: {alive}")


def _report(status: agent_pb2.AttemptStatus) -> dict[str, Any]:
    assert status.HasField("condition"), "no ConditionReport in the terminal status"
    c = status.condition
    return {
        "spec": c.spec,
        "verified": c.verified,
        "reverted": c.reverted,
        "reason": c.reason,
        "measured": json.loads(c.measured_json) if c.measured_json else None,
    }


# ------------------------------------------------------------ cpu_load


@pytest.mark.asyncio
async def test_cpu_load_applied_verified_held_and_reverted_on_done(
    tmp_path: Path,
) -> None:
    runtime, reports = GatedRuntime(), Reports()
    executor, digest = _executor(
        tmp_path, runtime, reports, kinds={ConditionKind.CPU_LOAD}
    )
    await executor.start()
    msg = _start(digest, "cpu_load:1")
    executor.handle(msg)
    await asyncio.wait_for(runtime.running.wait(), 30)
    # while the job runs: one burner, at the lowest priority, in the marker
    burners = _burners()
    assert len(burners) == 1
    if os.name == "posix":
        assert burners[0].nice() == 19
    entry = ConditionMarker(tmp_path / "data" / "conditions").load()
    assert entry[msg.start_attempt.attempt_id]["pids"][0][0] == burners[0].pid
    runtime.release.set()
    await _settle(executor)
    assert reports.states()[-1] == "done"
    report = _report(reports.final)
    assert report["spec"] == "cpu_load:1" and report["verified"] and report["reverted"]
    assert report["measured"]["load_cores"] >= 0.8
    # the condition was applied before staging: no status before it
    assert reports.states()[0] == "staging"
    await _no_burners()
    assert ConditionMarker(tmp_path / "data" / "conditions").load() == {}
    await executor.close()


@pytest.mark.asyncio
async def test_cpu_load_reverted_on_cancel(tmp_path: Path) -> None:
    runtime, reports = GatedRuntime(), Reports()
    executor, digest = _executor(
        tmp_path, runtime, reports, kinds={ConditionKind.CPU_LOAD}
    )
    await executor.start()
    msg = _start(digest, "cpu_load:1")
    executor.handle(msg)
    await asyncio.wait_for(runtime.running.wait(), 30)
    assert len(_burners()) == 1
    executor.handle(_cancel(msg))
    await _settle(executor)
    final = reports.final
    assert failure_type_from_proto(final.failure.failure_type).value == "cancelled"
    report = _report(final)
    assert report["reverted"] and report["verified"]  # it held until the cancel
    await _no_burners()
    await executor.close()


@pytest.mark.asyncio
async def test_cpu_load_reverted_on_agent_stop(tmp_path: Path) -> None:
    runtime, reports = GatedRuntime(), Reports()
    executor, digest = _executor(
        tmp_path, runtime, reports, kinds={ConditionKind.CPU_LOAD}
    )
    await executor.start()
    executor.handle(_start(digest, "cpu_load:1"))
    await asyncio.wait_for(runtime.running.wait(), 30)
    await executor.close()
    final = reports.final
    assert failure_type_from_proto(final.failure.failure_type).value == "node_lost"
    assert _report(final)["reverted"]
    await _no_burners()


@pytest.mark.asyncio
async def test_forced_verify_failure_fails_before_the_workload_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(load_module, "CPU_VERIFY_FRACTION", 50.0)  # cannot be met
    runtime, reports = GatedRuntime(), Reports()
    executor, digest = _executor(
        tmp_path, runtime, reports, kinds={ConditionKind.CPU_LOAD}
    )
    await executor.start()
    executor.handle(_start(digest, "cpu_load:1"))
    await _settle(executor)
    assert reports.states() == ["failed"]  # nothing staged, nothing ran
    assert runtime.calls == 0
    final = reports.final
    assert failure_type_from_proto(final.failure.failure_type).value == "config_error"
    assert final.failure.message.startswith("condition_failed:cpu_load: measured")
    assert final.failure.retryable is False
    report = _report(final)
    assert report["verified"] is False and report["reverted"] is True
    assert "busy cores" in report["reason"]
    assert report["measured"]["cores"] == 1
    await _no_burners()
    await executor.close()


@pytest.mark.asyncio
async def test_unadvertised_condition_is_refused_typed(tmp_path: Path) -> None:
    runtime, reports = GatedRuntime(), Reports()
    executor, digest = _executor(tmp_path, runtime, reports, kinds=set())
    await executor.start()
    executor.handle(_start(digest, "cpu_load:1"))
    await _settle(executor)
    final = reports.final
    assert (
        final.failure.message
        == "condition_failed:cpu_load: this node does not offer cpu_load"
    )
    assert runtime.calls == 0 and _burners() == []
    await executor.close()


@pytest.mark.asyncio
async def test_no_condition_means_no_report(tmp_path: Path) -> None:
    runtime, reports = GatedRuntime(), Reports()
    runtime.release.set()
    executor, digest = _executor(
        tmp_path, runtime, reports, kinds={ConditionKind.CPU_LOAD}
    )
    await executor.start()
    executor.handle(_start(digest, None))
    await _settle(executor)
    assert reports.states()[-1] == "done"
    assert not reports.final.HasField("condition")
    await executor.close()


# ------------------------------------------------------------ gpu_load


@pytest.mark.asyncio
async def test_gpu_load_without_cuda_fails_verification(tmp_path: Path) -> None:
    """On a machine without CUDA the burner exits 3 and the condition fails
    (a real CUDA run is the lab step)."""
    try:
        import torch

        if torch.cuda.is_available():  # pragma: no cover - CUDA lab machines
            pytest.skip("this machine has CUDA; the lab checklist covers it")
    except ImportError:
        pass
    runtime, reports = GatedRuntime(), Reports()
    executor, digest = _executor(
        tmp_path,
        runtime,
        reports,
        kinds={ConditionKind.GPU_LOAD},
        gpu_util=lambda _i: 0.0,
    )
    await executor.start()
    executor.handle(_start(digest, "gpu_load:50"))
    await _settle(executor, timeout_s=60)
    final = reports.final
    assert final.failure.message.startswith("condition_failed:gpu_load:")
    assert runtime.calls == 0
    assert _report(final)["reverted"]
    await _no_burners()
    await executor.close()


# ------------------------------------------------------------ netem (fake tc)


async def _rtt_steps(values: list[float]) -> Any:
    it = iter(values)

    async def rtt(_host: str, _port: int) -> float | None:
        return next(it, values[-1])

    return rtt


@pytest.mark.asyncio
async def test_netem_with_a_fake_tc_applied_and_reverted(tmp_path: Path) -> None:
    command, state, log = _fake_tc(tmp_path)
    runtime, reports = GatedRuntime(), Reports()
    executor, digest = _executor(
        tmp_path,
        runtime,
        reports,
        kinds={ConditionKind.NETEM},
        conditions={"netem": True, "netem_interface": "eth9", "tc_command": command},
        egress=lambda _h, _p: "eth9",
        rtt=await _rtt_steps([1.0, 52.0]),
    )
    await executor.start()
    executor.handle(_start(digest, "netem:delay=50ms,loss=1%"))
    await asyncio.wait_for(runtime.running.wait(), 30)
    assert _qdisc(state)["eth9"].endswith("delay 50ms loss 1%")
    runtime.release.set()
    await _settle(executor)
    report = _report(reports.final)
    assert report["verified"] and report["reverted"]
    assert report["measured"]["rtt_added_ms"] == 51.0
    assert _qdisc(state) == {}
    assert _tc_calls(log) == [
        ["qdisc", "show", "dev", "eth9"],
        [
            "qdisc",
            "replace",
            "dev",
            "eth9",
            "root",
            "netem",
            "delay",
            "50ms",
            "loss",
            "1%",
        ],
        ["qdisc", "show", "dev", "eth9"],
        ["qdisc", "show", "dev", "eth9"],  # fix r1: delete only a netem root
        ["qdisc", "del", "dev", "eth9", "root"],
    ]
    await executor.close()


@pytest.mark.asyncio
async def test_netem_reverted_on_cancel(tmp_path: Path) -> None:
    command, state, log = _fake_tc(tmp_path)
    runtime, reports = GatedRuntime(), Reports()
    executor, digest = _executor(
        tmp_path,
        runtime,
        reports,
        kinds={ConditionKind.NETEM},
        conditions={"netem": True, "netem_interface": "eth9", "tc_command": command},
        egress=lambda _h, _p: "eth9",
    )
    await executor.start()
    msg = _start(digest, "netem:loss=2%")
    executor.handle(msg)
    await asyncio.wait_for(runtime.running.wait(), 30)
    assert "eth9" in _qdisc(state)
    executor.handle(_cancel(msg))
    await _settle(executor)
    assert _qdisc(state) == {}
    assert _report(reports.final)["reverted"]
    assert _tc_calls(log)[-1] == ["qdisc", "del", "dev", "eth9", "root"]
    await executor.close()


@pytest.mark.asyncio
async def test_netem_rtt_not_grown_fails_and_still_reverts(tmp_path: Path) -> None:
    command, state, _log = _fake_tc(tmp_path)
    runtime, reports = GatedRuntime(), Reports()
    executor, digest = _executor(
        tmp_path,
        runtime,
        reports,
        kinds={ConditionKind.NETEM},
        conditions={"netem": True, "netem_interface": "eth9", "tc_command": command},
        egress=lambda _h, _p: "eth9",
        rtt=await _rtt_steps([1.0, 1.5]),
    )
    await executor.start()
    executor.handle(_start(digest, "netem:delay=100ms"))
    await _settle(executor)
    final = reports.final
    assert final.failure.message.startswith("condition_failed:netem: RTT grew")
    assert runtime.calls == 0 and _qdisc(state) == {}
    assert _report(final)["reverted"]
    await executor.close()


# ------------------------------------------------------------ agent restart


@pytest.mark.asyncio
@pytest.mark.security
async def test_agent_restart_sweeps_what_a_crashed_process_left(tmp_path: Path) -> None:
    """A previous agent process died with a burner running and netem applied:
    the marker lists both; the next start kills that burner (same pid and
    creation time) and deletes the qdisc - and never touches another process."""
    command, state, log = _fake_tc(tmp_path)
    state.write_text(json.dumps({"eth9": "qdisc netem 8001: root delay 50ms"}))
    orphan = _spawn(
        [
            sys.executable,
            "-m",
            BURNER_MODULE,
            "cpu",
            "--deadline-s",
            "60",
            "--parent",
            str(os.getpid()),
        ],
    )
    # a process the sweep must not kill (its pid is listed with another
    # creation time: a reused pid)
    bystander = _spawn([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        marker = ConditionMarker(tmp_path / "data" / "conditions")
        created = psutil.Process(orphan.pid).create_time()
        marker.put(
            "att_dead",
            {
                "spec": "cpu_load:1",
                "pids": [
                    [orphan.pid, created],
                    [bystander.pid, created - 100.0],  # wrong creation time
                ],
                "netem": "eth9",
                "netem_installed": True,  # fix r1: only an installed netem
            },
        )
        runtime, reports = GatedRuntime(), Reports()
        executor, _digest = _executor(
            tmp_path,
            runtime,
            reports,
            kinds={ConditionKind.CPU_LOAD, ConditionKind.NETEM},
            conditions={
                "netem": True,
                "netem_interface": "eth9",
                "tc_command": command,
            },
        )
        await executor.start()
        assert orphan.wait(timeout=10) is not None
        assert bystander.poll() is None  # untouched
        assert _qdisc(state) == {}
        assert ["qdisc", "del", "dev", "eth9", "root"] in _tc_calls(log)
        assert marker.load() == {}
        await executor.close()
    finally:
        for proc in (orphan, bystander):
            if proc.poll() is None:
                proc.kill()
                proc.wait()


@pytest.mark.security
def test_a_burner_ends_by_itself_when_its_agent_dies() -> None:
    """Belt and braces for the sweep: a burner whose parent is gone exits
    within about a second (its own parent poll)."""
    burner: psutil.Process | None = None
    parent = _spawn(
        [
            sys.executable,
            "-c",
            "import subprocess, sys, os, time;"
            f"p = subprocess.Popen([sys.executable, '-m', {BURNER_MODULE!r}, 'cpu',"
            " '--deadline-s', '60', '--parent', str(os.getpid())]);"
            "print(p.pid, flush=True); time.sleep(60)",
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert parent.stdout is not None
        burner = psutil.Process(int(parent.stdout.readline()))
        parent.kill()
        parent.wait()
        started = time.monotonic()
        burner.wait(timeout=10)
        assert time.monotonic() - started < 5
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait()
        if parent.stdout is not None:
            parent.stdout.close()
        if burner is not None:
            with contextlib.suppress(psutil.Error):
                burner.kill()
