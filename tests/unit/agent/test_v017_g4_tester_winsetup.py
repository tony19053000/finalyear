"""V0.17 G4 tester: independent acceptance tests for the Windows installer decisions.

Derived from brief section 5d (items 2, 6, 7 and the standing checklist "no
confidently-wrong output"), NOT from the implementation's own tests. Windows
cannot run here: everything is driven through a tester-written fake ``Ops``
(an in-memory SCM). The invariants asserted over a whole matrix of scenarios:

* OK  <=> the service is RUNNING on the interpreter that ``current.txt`` names;
* ``current.txt`` / the PATH shims never name a version that is not running
  (the one deliberate exception: a service stopped on purpose is pointed at the
  new version, and the result is NEEDS_ATTENTION, never OK);
* every non-OK result carries at least one line saying why;
* ``setup`` never raises ``OpsError``;
* no result line contains a token / key looking string.
"""

from __future__ import annotations

import itertools
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import PureWindowsPath

import pytest

from meshforge.agent import winsetup as ws
from meshforge.agent.wininstall import ProgramLayout, program_layout
from meshforge.common.exit_codes import ExitCode

pytestmark = pytest.mark.unit

ROOT = PureWindowsPath(r"C:\Program Files\MeshForge")
V1 = ProgramLayout(ROOT, "0.17.0-1")
V2 = ProgramLayout(ROOT, "0.17.0-2")
PY1, PY2 = str(V1.python_exe), str(V2.python_exe)
BEHAVIOURS = ("healthy", "exit4", "exit5", "no_console", "no_started", "hang", "dies")


@dataclass
class Scm:
    """Tester fake: a service whose behaviour depends on which python it runs."""

    python: str | None = None
    state: str = "absent"
    exit_code: int = 0
    behaviour: dict[str, str] = field(default_factory=dict)
    enrolled_: bool = False
    stop_ok: bool = True
    migrate_result: tuple[str, list[str]] = ("nothing_to_migrate", [])
    migrate_raises: bool = False
    legacy: str | None = None
    legacy_raises: bool = False
    console_on: bool = True
    clock: float = 0.0
    current: str | None = None
    shims: str | None = None
    log: list[str] = field(default_factory=list)
    started_logged: bool = False
    firewall: list[bool] = field(default_factory=list)
    peer: bool | None = False

    # -- SCM
    def service_status(self) -> ws.ServiceStatus:
        if self.python is None and self.state == "absent":
            return ws.ServiceStatus(exists=False)
        return ws.ServiceStatus(
            exists=True,
            state=self.state,
            pid=7 if self.state == "running" else None,
            service_exit_code=self.exit_code,
            win32_exit_code=1066 if self.exit_code else 0,
            python=self.python,
        )

    def stop_service(self, timeout_s: float) -> bool:
        self.log.append("stop")
        if not self.stop_ok:
            return False
        if self.state != "absent":
            self.state = "stopped"
        return True

    def start_service(self) -> None:
        self.log.append("start")
        assert self.python is not None
        how = self.behaviour.get(self.python, "healthy")
        self.exit_code = 0
        self.started_logged = how in ("healthy", "no_console")
        self.state = {
            "exit4": "stopped",
            "exit5": "stopped",
            "dies": "stopped",
            "hang": "start_pending",
        }.get(how, "running")
        self.exit_code = {"exit4": 4, "exit5": 5, "dies": 1}.get(how, 0)

    def install_service(self, python_exe: str) -> None:
        self.log.append(f"install:{python_exe}")
        if self.state == "absent":
            self.state = "stopped"
        self.python = python_exe

    def uninstall_service(self) -> bool:
        existed = self.state != "absent"
        self.state, self.python = "absent", None
        return existed

    def legacy_task_state(self) -> str | None:
        return self.legacy

    def end_and_disable_legacy_task(self) -> None:
        if self.legacy_raises:
            raise ws.OpsError("access denied")
        self.log.append("legacy_disabled")
        self.legacy = "Disabled"

    def delete_legacy_task(self) -> None:
        self.legacy = None

    def migrate(self) -> tuple[str, list[str]]:
        if self.migrate_raises:
            raise ws.OpsError("icacls failed")
        return self.migrate_result

    def peer_serving(self) -> bool | None:
        return self.peer

    def sync_firewall(self, peer_serving: bool) -> None:
        self.firewall.append(peer_serving)

    def console_readers(self) -> list[str]:
        return ["S-1-5-21-1-2-3-1001"]

    def grant_console(self, sids: Sequence[str]) -> None:
        pass

    def console_enabled(self) -> bool:
        return self.console_on

    def console_healthy(self) -> bool:
        return self.behaviour.get(self.python or "", "healthy") != "no_console"

    def agent_log_offset(self) -> int:
        return 0

    def agent_started_since(self, offset: int) -> bool:
        return self.started_logged

    def enrolled(self) -> bool:
        return self.enrolled_

    def node_id(self) -> str | None:
        return "node-abc" if self.enrolled_ else None

    def write_current(self, version_dir: str) -> None:
        self.current = version_dir

    def write_shims(self, version_dir: str) -> None:
        self.shims = version_dir

    def ensure_on_path(self) -> None:
        pass

    def remove_from_path(self) -> None:
        pass

    def remove_docker_access(self) -> None:
        pass

    def remove_data(self) -> list[str]:
        return ["removed"]

    def monotonic(self) -> float:
        return self.clock

    def sleep(self, seconds: float) -> None:
        self.clock += seconds


def _run(scm: Scm, layout: ProgramLayout = V2, timeout: float = 5.0) -> ws.SetupResult:
    return ws.setup(scm, layout, health_timeout_s=timeout)


def _check_invariants(scm: Scm, res: ws.SetupResult, layout: ProgramLayout) -> None:
    status = scm.service_status()
    if res.outcome is ws.SetupOutcome.OK:
        assert status.state == "running"
        assert res.service_state == "running"
        assert res.exit_code == ExitCode.OK
        assert scm.current == layout.version_dir
        assert scm.python == str(layout.python_exe)
    else:
        assert res.lines, "a non-OK result must say why"
        assert res.exit_code != ExitCode.OK
        assert res.summary()
    if res.outcome is ws.SetupOutcome.ROLLED_BACK:
        assert status.state == "running"
        assert scm.python == res.running_python
    if res.outcome is ws.SetupOutcome.FAILED:
        assert res.exit_code == ExitCode.FAILURE
    # current.txt never names a version that is not the service's interpreter
    # unless the service was deliberately left stopped (NEEDS_ATTENTION).
    if scm.current is not None and res.outcome is not ws.SetupOutcome.NEEDS_ATTENTION:
        assert scm.python is not None
        assert program_layout(scm.python).version_dir == scm.current
    joined = "\n".join(res.lines).lower()
    for needle in ("bearer", "begin private key", "token="):
        assert needle not in joined


# ----------------------------------------------------------- the whole matrix
@pytest.mark.parametrize(
    ("old", "new", "enrolled", "console_on"),
    list(
        itertools.product((None, *BEHAVIOURS), BEHAVIOURS, (False, True), (False, True))
    ),
)
def test_invariants_over_every_install_and_upgrade_scenario(
    old: str | None, new: str, enrolled: bool, console_on: bool
) -> None:
    scm = Scm(enrolled_=enrolled, console_on=console_on)
    scm.behaviour = {PY2: new}
    if old is not None:
        scm.behaviour[PY1] = old
        scm.python, scm.state = PY1, "running" if old == "healthy" else "stopped"
    res = _run(scm)
    _check_invariants(scm, res, V2)
    # the headline outcomes the brief demands
    healthy_new = new == "healthy" or (new == "no_started" and not enrolled)
    healthy_new = healthy_new or (new == "no_console" and not console_on)
    if healthy_new:
        assert res.outcome is ws.SetupOutcome.OK
    else:
        assert res.outcome is not ws.SetupOutcome.OK
    if old is None and not healthy_new:
        assert res.outcome in (ws.SetupOutcome.FAILED, ws.SetupOutcome.NEEDS_ATTENTION)


def test_fresh_install_not_enrolled_is_ok_without_agent_started() -> None:
    """A laptop that has not joined never logs agent.started (the supervisor waits)."""
    scm = Scm(behaviour={PY2: "no_started"})
    res = _run(scm)
    assert res.outcome is ws.SetupOutcome.OK
    assert any("join" in line.lower() for line in res.lines)


def test_enrolled_laptop_without_agent_started_is_not_ok() -> None:
    scm = Scm(enrolled_=True, behaviour={PY2: "no_started"})
    res = _run(scm)
    assert res.outcome is not ws.SetupOutcome.OK
    assert any("agent.started" in line for line in res.lines)


# ------------------------------------------------------------------- rollback
def test_upgrade_rolls_back_to_previous_and_leaves_old_current() -> None:
    scm = Scm(python=PY1, state="running", current=V1.version_dir, shims=V1.version_dir)
    scm.behaviour = {PY1: "healthy", PY2: "no_console"}
    res = _run(scm, timeout=3)
    assert res.outcome is ws.SetupOutcome.ROLLED_BACK
    assert scm.python == PY1 and scm.state == "running"
    assert scm.current == V1.version_dir == scm.shims
    assert res.exit_code != ExitCode.OK
    assert any(V2.version_dir in line for line in res.lines)


@pytest.mark.parametrize("new", ["exit4", "exit5", "hang", "dies", "no_console"])
def test_every_new_version_failure_triggers_rollback_when_previous_is_good(
    new: str,
) -> None:
    scm = Scm(python=PY1, state="running", current=V1.version_dir)
    scm.behaviour = {PY1: "healthy", PY2: new}
    res = _run(scm, timeout=3)
    assert res.outcome is ws.SetupOutcome.ROLLED_BACK
    assert scm.python == PY1 and scm.state == "running"


def test_rollback_that_also_fails_is_failed_not_ok() -> None:
    scm = Scm(python=PY1, state="running", current=V1.version_dir)
    scm.behaviour = {PY1: "dies", PY2: "dies"}
    res = _run(scm, timeout=3)
    assert res.outcome is ws.SetupOutcome.FAILED
    assert any("rolling back" in line for line in res.lines)
    assert scm.current == V1.version_dir  # never moved to the broken version


def test_repair_of_same_version_has_nothing_to_go_back_to() -> None:
    scm = Scm(python=PY2, state="running", current=V2.version_dir)
    scm.behaviour = {PY2: "dies"}
    res = _run(scm, timeout=3)
    assert res.outcome is ws.SetupOutcome.FAILED
    assert res.previous_python is None


def test_python_comparison_is_case_insensitive_for_repairs() -> None:
    scm = Scm(python=PY2.upper(), state="running")
    scm.behaviour = {PY2.upper(): "healthy", PY2: "healthy"}
    res = _run(scm)
    assert res.previous_python is None


@pytest.mark.parametrize(
    "state", ["start_pending", "stop_pending", "paused", "running"]
)
def test_non_stopped_service_is_stopped_before_it_is_re_pointed(state: str) -> None:
    scm = Scm(python=PY1, state=state)
    _run(scm)
    assert scm.log.index("stop") < scm.log.index(f"install:{PY2}")
    assert scm.log.index(f"install:{PY2}") < scm.log.index("start")


def test_service_that_will_not_stop_changes_nothing_and_is_failed() -> None:
    scm = Scm(python=PY1, state="running", stop_ok=False, current=V1.version_dir)
    res = _run(scm)
    assert res.outcome is ws.SetupOutcome.FAILED
    assert f"install:{PY2}" not in scm.log and "start" not in scm.log
    assert scm.python == PY1 and scm.current == V1.version_dir
    assert any("did not stop" in line for line in res.lines)


# -------------------------------------------------------------- blocked migration
@pytest.mark.parametrize("old_state", [None, "running"])
def test_blocked_migration_never_starts_the_service(old_state: str | None) -> None:
    scm = Scm(
        migrate_result=("blocked", ["two identities: C:\\Users\\a, C:\\Users\\b"])
    )
    if old_state:
        scm.python, scm.state = PY1, old_state
    res = _run(scm)
    assert res.outcome is ws.SetupOutcome.NEEDS_ATTENTION
    assert res.exit_code == ExitCode.CONFIG_ERROR
    assert "start" not in scm.log
    assert scm.state != "running"
    assert any("two identities" in line for line in res.lines)
    assert any("sc.exe start" in line for line in res.lines)


# ----------------------------------------------------------------- step errors
def test_step_error_is_a_result_not_an_exception_and_names_the_step() -> None:
    scm = Scm(migrate_raises=True)
    res = _run(scm)
    assert res.outcome is ws.SetupOutcome.FAILED
    assert any("icacls failed" in line for line in res.lines)
    assert "start" not in scm.log


def test_legacy_task_failure_does_not_abort_but_is_reported() -> None:
    scm = Scm(legacy="Running", legacy_raises=True)
    res = _run(scm)
    assert any(
        "scheduled task" in line and "access denied" in line for line in res.lines
    )
    assert res.outcome is ws.SetupOutcome.OK  # service itself is healthy


def test_legacy_task_is_disabled_before_the_service_starts() -> None:
    scm = Scm(legacy="Running")
    _run(scm)
    assert scm.log.index("legacy_disabled") < scm.log.index("start")


def test_firewall_follows_peer_setting_and_unreadable_config_is_reported() -> None:
    for peer in (True, False):
        scm = Scm(peer=peer)
        _run(scm)
        assert scm.firewall == [peer]
    scm = Scm(peer=None)
    res = _run(scm)
    assert scm.firewall == []
    assert any("firewall" in line for line in res.lines)


# ---------------------------------------------------------------- health check
def test_wait_healthy_zero_timeout_polls_once_and_does_not_hang() -> None:
    scm = Scm(python=PY2, state="start_pending")
    h = ws.wait_healthy(
        scm,
        log_offset=0,
        timeout_s=0.0,
        need_console=True,
        need_started=True,
    )
    assert not h.ok
    assert scm.clock == 0.0


def test_wait_healthy_stops_waiting_when_service_stopped_even_with_time_left() -> None:
    scm = Scm(python=PY2, state="stopped", exit_code=4)
    h = ws.wait_healthy(
        scm,
        log_offset=0,
        timeout_s=60,
        need_console=True,
        need_started=True,
    )
    assert not h.ok and "exit code 4" in h.reason
    assert scm.clock == 0.0


def test_wait_healthy_missing_service() -> None:
    h = ws.wait_healthy(
        Scm(),
        log_offset=0,
        timeout_s=3,
        need_console=False,
        need_started=False,
    )
    assert not h.ok and "does not exist" in h.reason


def test_wait_healthy_slow_start_succeeds_before_deadline() -> None:
    class Slow(Scm):
        def service_status(self) -> ws.ServiceStatus:
            self.state = "running" if self.clock >= 10 else "start_pending"
            return super().service_status()

    scm = Slow(python=PY2, state="start_pending")
    h = ws.wait_healthy(
        scm,
        log_offset=0,
        timeout_s=60,
        need_console=True,
        need_started=False,
    )
    assert h.ok


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (b'{"event": "agent.started", "node_id": "x"}', True),
        (b"2026-10-26 [info     ] agent.started   node_id=x", True),
        (b"agent.started", True),
        (b"agent.started\r\n", True),
        (b"", False),
        (b'{"event": "agent.start_failed"}', False),
        (b'{"event": "agent.started_late"}', False),
        (b'{"event": "myagent.started"}', False),
        (b'{"event": "agent.started.v2"}', False),
        (b"agent started", False),
        (b"AGENT.STARTED", False),
        ('{"event": "agent.started"}'.encode("utf-16-le"), False),
    ],
)
def test_log_has_agent_started(data: bytes, expected: bool) -> None:
    assert ws.log_has_agent_started(data) is expected


def test_log_has_agent_started_on_a_huge_log_is_fast() -> None:
    blob = b"x" * 20_000_000 + b" agent.started"
    assert ws.log_has_agent_started(blob)


# ----------------------------------------------------------------- result shape
def test_result_json_has_a_stable_shape_and_is_serialisable() -> None:
    import json

    res = _run(Scm(enrolled_=True))
    d = res.as_dict()
    assert set(d) == {
        "outcome", "version", "lines", "previous_python", "running_python",
        "node_id", "service_state",
    }  # fmt: skip
    json.dumps(d)
    assert d["node_id"] == "node-abc"


def test_exit_code_map_is_total_and_only_ok_is_zero() -> None:
    for outcome in ws.SetupOutcome:
        r = ws.SetupResult(outcome, "v")
        assert (r.exit_code == ExitCode.OK) == (outcome is ws.SetupOutcome.OK)
        assert r.summary()


# -------------------------------------------------------------------- teardown
def test_teardown_removes_everything_and_keeps_data_by_default() -> None:
    scm = Scm(python=PY1, state="running", legacy="Disabled")
    r = ws.teardown(scm, remove_data=False)
    assert r.ok and scm.state == "absent" and scm.legacy is None
    assert any("data kept" in line for line in r.lines)
    assert "removed" not in r.lines


def test_teardown_remove_data_flag() -> None:
    r = ws.teardown(Scm(python=PY1, state="running"), remove_data=True)
    assert r.ok and "removed" in r.lines


def test_teardown_twice_is_idempotent() -> None:
    scm = Scm(python=PY1, state="running")
    assert ws.teardown(scm, remove_data=False).ok
    second = ws.teardown(scm, remove_data=False)
    assert second.ok
    assert any("no MeshForgeAgent service" in line for line in second.lines)


def test_teardown_never_deletes_data_when_service_removal_failed() -> None:
    class Stuck(Scm):
        def uninstall_service(self) -> bool:
            raise ws.OpsError("marked for deletion")

        def remove_data(self) -> list[str]:  # pragma: no cover - must not run
            raise AssertionError("data removed while the service is still installed")

    r = ws.teardown(Stuck(python=PY1, state="running"), remove_data=True)
    assert not r.ok and r.exit_code == ExitCode.FAILURE
    assert any("data kept" in line for line in r.lines)


def test_teardown_continues_past_best_effort_failures() -> None:
    class Flaky(Scm):
        def remove_docker_access(self) -> None:
            raise ws.OpsError("no such group")

        def remove_from_path(self) -> None:
            raise ws.OpsError("registry busy")

        def delete_legacy_task(self) -> None:
            raise ws.OpsError("task busy")

    scm = Flaky(python=PY1, state="running", legacy="Ready")
    r = ws.teardown(scm, remove_data=False)
    assert r.ok and scm.state == "absent"
    text = "\n".join(r.lines)
    assert "no such group" in text and "registry busy" in text and "task busy" in text


def test_teardown_data_removal_error_fails_result() -> None:
    class Locked(Scm):
        def remove_data(self) -> list[str]:
            raise ws.OpsError("file in use")

    r = ws.teardown(Locked(python=PY1, state="running"), remove_data=True)
    assert not r.ok and any("file in use" in line for line in r.lines)
