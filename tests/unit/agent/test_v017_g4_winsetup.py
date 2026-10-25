"""V0.17 G4: the Windows installer's decisions (``winsetup.setup`` / ``teardown``).

Every step the installer helper takes - legacy task, stop, install/re-point,
migration, console access, firewall, start + health, rollback, current.txt -
is decided in :mod:`meshforge.agent.winsetup` against the small ``Ops``
interface; here a fake drives it on any OS. The standing rule under test: the
result never says "ok" over a service that is not RUNNING and healthy, and
every non-ok result says why.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import PureWindowsPath

import pytest

from meshforge.agent import winsetup as wsu
from meshforge.agent.wininstall import ProgramLayout
from meshforge.common.exit_codes import ExitCode

pytestmark = pytest.mark.unit

ROOT = PureWindowsPath(r"C:\Program Files\MeshForge")
NEW = ProgramLayout(ROOT, "0.17.0-2")
OLD = ProgramLayout(ROOT, "0.17.0-1")
NEW_PY = str(NEW.python_exe)
OLD_PY = str(OLD.python_exe)


@dataclass
class FakeOps:
    """An in-memory Windows: one service, a log, a clock."""

    exists: bool = False
    state: str = "absent"
    python: str | None = None
    service_exit_code: int = 0
    win32_exit_code: int = 0
    stop_works: bool = True
    start_error: str | None = None
    # python.exe -> what the service does when started with it
    behaviour: dict[str, str] = field(default_factory=dict)
    """``healthy`` (default) | ``exit4`` | ``exit5`` | ``no_console`` |
    ``no_started`` | ``crash_loop`` (start_pending forever)."""
    migration: tuple[str, list[str]] = ("nothing_to_migrate", [])
    is_enrolled: bool = False
    legacy: str | None = None
    legacy_fails: bool = False
    peer: bool | None = False
    readers: list[str] = field(default_factory=lambda: ["S-1-5-21-1-2-3-1001"])
    grant_fails: bool = False
    console_on: bool = True
    uninstall_error: str | None = None
    docker_error: str | None = None
    data_error: str | None = None
    now: float = 0.0
    calls: list[tuple[str, object]] = field(default_factory=list)
    _started_logged: bool = False

    def _call(self, name: str, arg: object = None) -> None:
        self.calls.append((name, arg))

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]

    def args(self, name: str) -> list[object]:
        return [arg for n, arg in self.calls if n == name]

    # ------------------------------------------------------------ SCM
    def service_status(self) -> wsu.ServiceStatus:
        if not self.exists:
            return wsu.ServiceStatus(exists=False)
        return wsu.ServiceStatus(
            exists=True,
            state=self.state,
            pid=4242 if self.state == "running" else None,
            win32_exit_code=self.win32_exit_code,
            service_exit_code=self.service_exit_code,
            python=self.python,
        )

    def stop_service(self, timeout_s: float) -> bool:
        self._call("stop_service", timeout_s)
        if not self.stop_works:
            return False
        if self.exists:
            self.state = "stopped"
            self.service_exit_code = 0
        return True

    def start_service(self) -> None:
        self._call("start_service", self.python)
        if self.start_error is not None:
            raise wsu.OpsError(self.start_error)
        how = self.behaviour.get(self.python or "", "healthy")
        self.service_exit_code = 0
        if how in ("exit4", "exit5"):
            self.state = "stopped"
            self.win32_exit_code = 1066
            self.service_exit_code = 4 if how == "exit4" else 5
        elif how == "crash_loop":
            self.state = "start_pending"
        else:
            self.state = "running"
            self._started_logged = how != "no_started"

    def install_service(self, python_exe: str) -> None:
        self._call("install_service", python_exe)
        if not self.exists:
            self.exists, self.state = True, "stopped"
        self.python = python_exe

    def uninstall_service(self) -> bool:
        self._call("uninstall_service")
        if self.uninstall_error is not None:
            raise wsu.OpsError(self.uninstall_error)
        existed, self.exists, self.state = self.exists, False, "absent"
        return existed

    # ---------------------------------------------------- legacy task
    def legacy_task_state(self) -> str | None:
        return self.legacy

    def end_and_disable_legacy_task(self) -> None:
        self._call("end_and_disable_legacy_task")
        if self.legacy_fails:
            raise wsu.OpsError("schtasks /Change failed (exit 1): access denied")
        self.legacy = "Disabled"

    def delete_legacy_task(self) -> None:
        self._call("delete_legacy_task")
        self.legacy = None

    # ------------------------------------------------- the rest
    def migrate(self) -> tuple[str, list[str]]:
        self._call("migrate")
        return self.migration

    def peer_serving(self) -> bool | None:
        return self.peer

    def sync_firewall(self, peer_serving: bool) -> None:
        self._call("sync_firewall", peer_serving)

    def console_readers(self) -> list[str]:
        return list(self.readers)

    def grant_console(self, sids: Sequence[str]) -> None:
        self._call("grant_console", list(sids))
        if self.grant_fails:
            raise wsu.OpsError("icacls failed (exit 5)")

    def console_enabled(self) -> bool:
        return self.console_on

    def console_healthy(self) -> bool:
        how = self.behaviour.get(self.python or "", "healthy")
        return self.state == "running" and how != "no_console"

    def agent_log_offset(self) -> int:
        self._started_logged = False
        return 100

    def agent_started_since(self, offset: int) -> bool:
        assert offset == 100
        return self._started_logged

    def enrolled(self) -> bool:
        return self.is_enrolled

    def node_id(self) -> str | None:
        return "node-7f3a" if self.is_enrolled else None

    def write_current(self, version_dir: str) -> None:
        self._call("write_current", version_dir)

    def write_shims(self, version_dir: str) -> None:
        self._call("write_shims", version_dir)

    def ensure_on_path(self) -> None:
        self._call("ensure_on_path")

    def remove_from_path(self) -> None:
        self._call("remove_from_path")

    def remove_docker_access(self) -> None:
        self._call("remove_docker_access")
        if self.docker_error is not None:
            raise wsu.OpsError(self.docker_error)

    def remove_data(self) -> list[str]:
        self._call("remove_data")
        if self.data_error is not None:
            raise wsu.OpsError(self.data_error)
        return [r"removed all MeshForge data: C:\ProgramData\MeshForge"]

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _upgrade(**kw: object) -> FakeOps:
    """A laptop running the previous version, already joined."""
    ops = FakeOps(exists=True, state="running", python=OLD_PY, is_enrolled=True)
    for key, value in kw.items():
        setattr(ops, key, value)
    return ops


def _never_ok_over_a_stopped_service(result: wsu.SetupResult) -> None:
    if result.outcome is wsu.SetupOutcome.OK:
        assert result.service_state == "running"
    else:
        assert result.lines, "a non-ok result must say why"
        assert result.exit_code != ExitCode.OK


# ------------------------------------------------------------ fresh install
def test_fresh_install_starts_checks_and_points_current_at_the_new_version() -> None:
    ops = FakeOps()
    result = wsu.setup(ops, NEW, health_timeout_s=60)
    assert result.outcome is wsu.SetupOutcome.OK
    assert result.exit_code == ExitCode.OK
    assert result.service_state == "running"
    assert result.running_python == NEW_PY
    assert result.previous_python is None
    assert ops.args("install_service") == [NEW_PY]
    assert ops.args("write_current") == ["0.17.0-2"]
    assert ops.args("write_shims") == ["0.17.0-2"]
    assert "ensure_on_path" in ops.names()
    # pointer files only after the health check passed
    assert ops.names().index("start_service") < ops.names().index("write_current")
    # not joined yet: the user is told how to join, no node_id is made up
    assert result.node_id is None
    assert "Start menu" in result.lines[-1]
    assert result.summary() == "MeshForge is installed and running."


def test_fresh_install_does_not_wait_for_agent_started_before_the_join() -> None:
    # Not enrolled: the supervisor only runs the console, agent.started never
    # comes - the console answering is the health check.
    ops = FakeOps(behaviour={NEW_PY: "no_started"})
    assert wsu.setup(ops, NEW).outcome is wsu.SetupOutcome.OK


def test_a_joined_laptop_must_log_agent_started_within_the_timeout() -> None:
    ops = FakeOps(is_enrolled=True, behaviour={NEW_PY: "no_started"})
    result = wsu.setup(ops, NEW, health_timeout_s=60)
    assert result.outcome is wsu.SetupOutcome.FAILED
    assert any("agent.started" in line for line in result.lines)
    assert "write_current" not in ops.names()
    assert ops.now >= 60  # it really waited, polling
    _never_ok_over_a_stopped_service(result)


def test_console_that_never_answers_is_not_healthy() -> None:
    ops = FakeOps(behaviour={NEW_PY: "no_console"})
    result = wsu.setup(ops, NEW, health_timeout_s=10)
    assert result.outcome is wsu.SetupOutcome.FAILED
    assert any("127.0.0.1:47130" in line for line in result.lines)


def test_console_disabled_in_config_is_not_waited_for() -> None:
    ops = FakeOps(console_on=False, behaviour={NEW_PY: "no_console"})
    assert wsu.setup(ops, NEW).outcome is wsu.SetupOutcome.OK


def test_start_pending_forever_times_out_and_names_the_state() -> None:
    ops = FakeOps(behaviour={NEW_PY: "crash_loop"})
    result = wsu.setup(ops, NEW, health_timeout_s=30)
    assert result.outcome is wsu.SetupOutcome.FAILED
    assert result.service_state == "start_pending"
    assert any("the service is start_pending" in line for line in result.lines)


@pytest.mark.parametrize(("how", "code"), [("exit4", 4), ("exit5", 5)])
def test_agent_refusing_its_config_leaves_needs_attention_with_the_reason(
    how: str, code: int
) -> None:
    ops = FakeOps(behaviour={NEW_PY: how})
    result = wsu.setup(ops, NEW, health_timeout_s=60)
    assert result.outcome is wsu.SetupOutcome.NEEDS_ATTENTION
    assert result.exit_code == ExitCode.CONFIG_ERROR
    assert result.service_state == "stopped"
    text = "\n".join(result.lines)
    assert f"exit code {code}" in text
    assert "sc.exe start MeshForgeAgent" in text
    # a STOPPED service ends the wait at once: no minute of polling
    assert ops.now == 0
    # installed: the pointer names the new version (it is the only one)
    assert ops.args("write_current") == ["0.17.0-2"]
    assert "MeshForge is installed but NOT running" in result.summary()


def test_start_error_is_reported_not_raised() -> None:
    ops = FakeOps(start_error="Access is denied. (5)")
    result = wsu.setup(ops, NEW)
    assert result.outcome is wsu.SetupOutcome.FAILED
    assert any("did not start: Access is denied" in line for line in result.lines)


def test_a_failing_step_is_a_failed_result_never_an_exception() -> None:
    class Broken(FakeOps):
        def install_service(self, python_exe: str) -> None:
            raise wsu.OpsError("creating service MeshForgeAgent failed: denied (5)")

    result = wsu.setup(Broken(), NEW)
    assert result.outcome is wsu.SetupOutcome.FAILED
    assert result.lines == [
        "setup stopped: creating service MeshForgeAgent failed: denied (5)"
    ]
    assert result.service_state == "absent"


# --------------------------------------------------------------- migration
def test_blocked_migration_installs_but_leaves_the_service_stopped() -> None:
    ops = FakeOps(
        migration=("blocked", ["more than one per-user identity found:", "  a", "  b"])
    )
    result = wsu.setup(ops, NEW)
    assert result.outcome is wsu.SetupOutcome.NEEDS_ATTENTION
    assert "start_service" not in ops.names()
    assert result.service_state == "stopped"
    assert result.lines[0] == "more than one per-user identity found:"
    assert "left STOPPED" in result.lines[-1]
    # Q13: the install itself completed
    assert ops.args("install_service") == [NEW_PY]
    assert ops.args("write_current") == ["0.17.0-2"]
    _never_ok_over_a_stopped_service(result)


def test_migration_lines_reach_the_result() -> None:
    ops = FakeOps(migration=("migrated", ["copied node n1 into C:\\ProgramData..."]))
    result = wsu.setup(ops, NEW)
    assert result.outcome is wsu.SetupOutcome.OK
    assert result.lines[0].startswith("copied node n1")
    # migrate after the service (and its folders + ACLs) exists, before start
    names = ops.names()
    assert names.index("install_service") < names.index("migrate")
    assert names.index("migrate") < names.index("start_service")


# --------------------------------------------------------------- legacy task
def test_the_dev_scheduled_task_is_stopped_first_and_reported_as_a_task() -> None:
    ops = FakeOps(legacy="Running")
    result = wsu.setup(ops, NEW)
    assert ops.names()[0] == "end_and_disable_legacy_task"
    line = result.lines[0]
    assert "scheduled task \\MeshForgeAgent" in line
    assert "was Running" in line
    assert "service replaces it" in line


def test_a_legacy_task_that_cannot_be_stopped_is_a_warning_not_a_stop() -> None:
    ops = FakeOps(legacy="Ready", legacy_fails=True)
    result = wsu.setup(ops, NEW)
    assert result.outcome is wsu.SetupOutcome.OK
    assert "(not the service)" in result.lines[0]
    assert "Task Scheduler" in result.lines[0]


def test_no_legacy_task_no_line() -> None:
    ops = FakeOps()
    result = wsu.setup(ops, NEW)
    assert not any("scheduled task" in line for line in result.lines)


# --------------------------------------------------------- console, firewall
def test_the_installing_user_gets_console_access() -> None:
    ops = FakeOps(readers=["S-1-5-21-9-9-9-1001"])
    wsu.setup(ops, NEW)
    assert ops.args("grant_console") == [["S-1-5-21-9-9-9-1001"]]


def test_console_access_failure_says_how_to_grant_it_later() -> None:
    ops = FakeOps(grant_fails=True)
    result = wsu.setup(ops, NEW)
    assert result.outcome is wsu.SetupOutcome.OK
    assert any("console-access" in line for line in result.lines)


def test_no_reader_no_grant() -> None:
    ops = FakeOps(readers=[])
    wsu.setup(ops, NEW)
    assert "grant_console" not in ops.names()


@pytest.mark.parametrize("peer", [True, False])
def test_firewall_follows_peer_serving(peer: bool) -> None:
    ops = FakeOps(peer=peer)
    wsu.setup(ops, NEW)
    assert ops.args("sync_firewall") == [peer]


def test_unreadable_config_leaves_the_firewall_alone_and_says_so() -> None:
    ops = FakeOps(peer=None)
    result = wsu.setup(ops, NEW)
    assert "sync_firewall" not in ops.names()
    assert any("firewall rule was not changed" in line for line in result.lines)


# ------------------------------------------------------------------ upgrade
def test_upgrade_stops_the_old_version_repoints_and_keeps_the_node() -> None:
    ops = _upgrade()
    result = wsu.setup(ops, NEW)
    assert result.outcome is wsu.SetupOutcome.OK
    assert result.previous_python == OLD_PY
    assert result.running_python == NEW_PY
    assert result.node_id == "node-7f3a"
    names = ops.names()
    assert names.index("stop_service") < names.index("install_service")
    assert ops.args("install_service") == [NEW_PY]
    assert "running as node node-7f3a" in result.lines[-1]


def test_failed_upgrade_rolls_back_to_the_previous_version() -> None:
    ops = _upgrade(behaviour={NEW_PY: "exit4"})
    result = wsu.setup(ops, NEW)
    assert result.outcome is wsu.SetupOutcome.ROLLED_BACK
    assert result.exit_code == ExitCode.FAILURE
    assert ops.args("install_service") == [NEW_PY, OLD_PY]
    assert ops.python == OLD_PY and ops.state == "running"
    assert result.running_python == OLD_PY
    assert result.service_state == "running"
    # pointers name the version that runs: the new one is never written
    assert ops.args("write_current") == ["0.17.0-1"]
    assert ops.args("write_shims") == ["0.17.0-1"]
    text = "\n".join(result.lines)
    assert "version 0.17.0-2: the agent stopped on its own with exit code 4" in text
    assert "previous version (0.17.0-1) runs again" in text
    assert result.summary().startswith("The new version did not start")


def test_rollback_that_fails_too_is_failed_with_both_reasons() -> None:
    ops = _upgrade(behaviour={NEW_PY: "no_started", OLD_PY: "exit5"})
    result = wsu.setup(ops, NEW, health_timeout_s=20)
    assert result.outcome is wsu.SetupOutcome.FAILED
    assert "write_current" not in ops.names()
    text = "\n".join(result.lines)
    assert "agent.started" in text
    assert "rolling back to" in text and "exit code 5" in text
    _never_ok_over_a_stopped_service(result)


def test_rollback_to_a_python_outside_the_layout_still_runs_it() -> None:
    odd = r"D:\dev\python.exe"
    ops = _upgrade(python=odd, behaviour={NEW_PY: "exit4"})
    result = wsu.setup(ops, NEW)
    assert result.outcome is wsu.SetupOutcome.ROLLED_BACK
    assert ops.python == odd
    # no version folder to name: the pointer files are left alone
    assert "write_current" not in ops.names()
    assert any(odd in line for line in result.lines)


def test_repair_of_the_running_version_has_nothing_to_roll_back_to() -> None:
    ops = _upgrade(python=NEW_PY.upper(), behaviour={NEW_PY: "exit4"})
    result = wsu.setup(ops, NEW)
    assert result.previous_python is None
    assert result.outcome is wsu.SetupOutcome.NEEDS_ATTENTION
    assert ops.args("install_service") == [NEW_PY]


def test_running_service_that_will_not_stop_changes_nothing() -> None:
    ops = _upgrade(stop_works=False)
    result = wsu.setup(ops, NEW)
    assert result.outcome is wsu.SetupOutcome.FAILED
    assert "install_service" not in ops.names()
    assert "write_current" not in ops.names()
    assert "nothing was changed" in result.lines[-1]
    assert result.service_state == "running"


def test_a_stopped_previous_service_is_not_stopped_again() -> None:
    ops = _upgrade(state="stopped")
    wsu.setup(ops, NEW)
    assert ops.names().count("stop_service") == 0


# ------------------------------------------------------------- wait_healthy
def test_wait_healthy_returns_at_once_for_a_missing_service() -> None:
    ops = FakeOps()
    health = wsu.wait_healthy(
        ops, log_offset=0, timeout_s=60, need_console=True, need_started=True
    )
    assert not health.ok
    assert health.reason == "the service does not exist"
    assert ops.now == 0


def test_stopped_reason_names_the_log_for_an_unknown_code() -> None:
    status = wsu.ServiceStatus(
        exists=True, state="stopped", win32_exit_code=1067, service_exit_code=0
    )
    reason = wsu._stopped_reason(status)
    assert "exit 1067/0" in reason
    assert "service.log" in reason


def test_results_carry_no_secret_shaped_fields() -> None:
    result = wsu.setup(FakeOps(), NEW)
    assert set(result.as_dict()) == {
        "outcome",
        "version",
        "lines",
        "previous_python",
        "running_python",
        "node_id",
        "service_state",
    }


# ----------------------------------------------------------------- teardown
def test_teardown_keeps_data_by_default() -> None:
    ops = FakeOps(exists=True, state="running", python=NEW_PY, legacy="Disabled")
    result = wsu.teardown(ops, remove_data=False)
    assert result.ok and result.exit_code == ExitCode.OK
    assert "remove_data" not in ops.names()
    names = ops.names()
    assert {"remove_docker_access", "remove_from_path", "uninstall_service"} <= set(
        names
    )
    assert "delete_legacy_task" in names
    assert any("data kept" in line for line in result.lines)
    assert any("removed the MeshForgeAgent service" in line for line in result.lines)


def test_teardown_remove_data_after_the_service_is_gone() -> None:
    ops = FakeOps(exists=True, state="stopped", python=NEW_PY)
    result = wsu.teardown(ops, remove_data=True)
    assert result.ok
    names = ops.names()
    assert names.index("uninstall_service") < names.index("remove_data")
    assert result.lines[-1].startswith("removed all MeshForge data")


def test_teardown_never_deletes_data_while_the_service_stays() -> None:
    ops = FakeOps(exists=True, uninstall_error="did not stop within 60 s")
    result = wsu.teardown(ops, remove_data=True)
    assert not result.ok
    assert result.exit_code == ExitCode.FAILURE
    assert "remove_data" not in ops.names()
    assert any("could not be removed: did not stop" in line for line in result.lines)
    assert result.lines[-1] == "data kept: the service is still installed"


def test_teardown_best_effort_steps_do_not_stop_the_rest() -> None:
    ops = FakeOps(exists=True, docker_error="net failed")
    result = wsu.teardown(ops, remove_data=False)
    assert result.ok
    assert "uninstall_service" in ops.names()
    assert result.lines[0] == "Docker access not removed: net failed"


def test_teardown_data_removal_failure_fails_the_result() -> None:
    ops = FakeOps(exists=True, data_error=r"C:\ProgramData\MeshForge\x: in use")
    result = wsu.teardown(ops, remove_data=True)
    assert not result.ok
    assert "the data could not be removed" in result.lines[-1]


def test_teardown_without_a_service_says_so() -> None:
    result = wsu.teardown(FakeOps(), remove_data=False)
    assert result.ok
    assert "no MeshForgeAgent service was installed" in result.lines
    assert result.as_dict()["ok"] is True

