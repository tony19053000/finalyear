"""V0.17 G5 (brief §5e rollback matrix, Windows agent; Q38/Q39): every failure
point of the installer's setup run, driven against the G4 fake Windows, with
the real file-based upgrade records (:mod:`meshforge.agent.winupgrade`) in a
scratch folder.

The invariant checked after every outcome: ``current.txt`` names the version
folder of the interpreter the service's ``binPath`` runs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

import pytest
from test_v017_g4_winsetup import NEW, NEW_PY, OLD, OLD_PY, FakeOps

from meshforge.agent import winsetup as wsu
from meshforge.agent.wininstall import program_layout
from meshforge.agent.winupgrade import FileRecords, Journal, NullRecords, plain_version

pytestmark = pytest.mark.unit


class PowerLoss(BaseException):
    """The plug is pulled: nothing after this line of setup runs."""


@dataclass
class Windows(FakeOps):
    """The G4 fake plus ``current.txt`` and an optional power cut."""

    current: str | None = None
    cut_after: str | None = None

    def write_current(self, version_dir: str) -> None:
        super().write_current(version_dir)
        self.current = version_dir

    def install_service(self, python_exe: str) -> None:
        super().install_service(python_exe)
        if self.cut_after == "install_service":
            self.state = "stopped"
            raise PowerLoss

    def migrate(self) -> tuple[str, list[str]]:
        result = super().migrate()
        if self.cut_after == "migrate":
            raise PowerLoss
        return result


def upgrade_from_old(**kwargs: object) -> Windows:
    ops = Windows(exists=True, state="running", python=OLD_PY, is_enrolled=True)
    for key, value in kwargs.items():
        setattr(ops, key, value)
    ops.current = OLD.version_dir
    return ops


def assert_invariant(ops: Windows) -> None:
    assert ops.python is not None
    binpath_dir = program_layout(PureWindowsPath(ops.python)).version_dir
    assert ops.current == binpath_dir, (ops.current, binpath_dir)


@pytest.fixture
def records(tmp_path: Path) -> FileRecords:
    # the fake SCM's interpreters are Windows paths that do not exist here; the
    # real "inside {app} and exists" rule has its own tests below
    return FileRecords(
        tmp_path / "MeshForge", python_ok=lambda p: p in (OLD_PY, NEW_PY)
    )


def _rolled_back(records: FileRecords) -> dict[str, str]:
    return dict(json.loads((records.folder / "rolled-back.json").read_text()))


# ---------------------------------------------------------- matrix rows
def test_row1_new_version_unhealthy_previous_runs_and_it_is_recorded(
    records: FileRecords,
) -> None:
    ops = upgrade_from_old(behaviour={NEW_PY: "no_console"})
    result = wsu.setup(ops, NEW, health_timeout_s=3, records=records)
    assert result.outcome is wsu.SetupOutcome.ROLLED_BACK
    assert ops.state == "running" and ops.python == OLD_PY
    assert_invariant(ops)
    record = _rolled_back(records)
    assert record["from"] == OLD.version_dir and record["to"] == NEW.version_dir
    assert "console does not answer" in record["reason"]
    assert records.read_journal() is None  # coherent end: journal closed


def test_row2_migration_raises_previous_restored_and_running(
    records: FileRecords,
) -> None:
    class Raising(Windows):
        def migrate(self) -> tuple[str, list[str]]:
            raise wsu.OpsError("icacls on the identity folder failed (exit 5)")

    ops = Raising(exists=True, state="running", python=OLD_PY, is_enrolled=True)
    ops.current = OLD.version_dir
    result = wsu.setup(ops, NEW, health_timeout_s=3, records=records)
    assert result.outcome is wsu.SetupOutcome.ROLLED_BACK
    assert ops.state == "running" and ops.python == OLD_PY
    assert_invariant(ops)
    assert "identity migration" in _rolled_back(records)["reason"]
    # the new version never ran over a half-decided identity
    assert ops.args("start_service") == [OLD_PY]


def test_row3_new_version_will_not_stop_failed_and_pointers_agree(
    records: FileRecords,
) -> None:
    class Stuck(Windows):
        def stop_service(self, timeout_s: float) -> bool:
            if self.names().count("stop_service") >= 1:
                self._call("stop_service", timeout_s)
                return False
            return super().stop_service(timeout_s)

    ops = Stuck(
        exists=True, state="running", python=OLD_PY, behaviour={NEW_PY: "no_console"}
    )
    ops.current = OLD.version_dir
    result = wsu.setup(ops, NEW, health_timeout_s=3, records=records)
    assert result.outcome is wsu.SetupOutcome.FAILED
    assert ops.python == NEW_PY  # nothing re-pointed under a running process
    assert_invariant(ops)
    assert any("did not stop" in line for line in result.lines)
    # FAILED keeps the journal: the repair run still knows where to go back to
    journal = records.read_journal()
    assert journal is not None and journal.from_python == OLD_PY


def test_rollback_that_fails_too_is_failed_and_says_both(records: FileRecords) -> None:
    ops = upgrade_from_old(behaviour={NEW_PY: "exit4", OLD_PY: "no_console"})
    result = wsu.setup(ops, NEW, health_timeout_s=3, records=records)
    assert result.outcome is wsu.SetupOutcome.FAILED
    assert any("failed too" in line for line in result.lines)
    assert ops.python == OLD_PY


# --------------------------------------------- Q39 power loss / killed setup
def test_power_loss_after_the_switch_then_repair_finishes(records: FileRecords) -> None:
    ops = upgrade_from_old(cut_after="install_service")
    with pytest.raises(PowerLoss):
        wsu.setup(ops, NEW, health_timeout_s=3, records=records)
    # the half state: the SCM names the new version, current.txt the old one
    assert ops.python == NEW_PY and ops.current == OLD.version_dir
    # cut inside the switch: the SCM changed, the journal still says
    # "switching" - either way it names the version to go back to
    journal = records.read_journal()
    assert journal == Journal(OLD_PY, NEW.version_dir, "switching")
    # the repair run (the same installer again) finishes it
    ops.cut_after = None
    result = wsu.setup(ops, NEW, health_timeout_s=3, records=records)
    assert result.outcome is wsu.SetupOutcome.OK
    assert any("did not finish" in line for line in result.lines)
    assert_invariant(ops)
    assert ops.current == NEW.version_dir
    assert records.read_journal() is None


def test_power_loss_after_the_switch_then_repair_reverts_when_unhealthy(
    records: FileRecords,
) -> None:
    ops = upgrade_from_old(cut_after="migrate", behaviour={NEW_PY: "no_started"})
    with pytest.raises(PowerLoss):
        wsu.setup(ops, NEW, health_timeout_s=3, records=records)
    ops.cut_after = None
    # The service already points at the new version - G4 would see a "repair"
    # with nothing to go back to. The journal names the real previous version.
    result = wsu.setup(ops, NEW, health_timeout_s=3, records=records)
    assert result.previous_python == OLD_PY
    assert result.outcome is wsu.SetupOutcome.ROLLED_BACK
    assert ops.python == OLD_PY and ops.state == "running"
    assert_invariant(ops)


def test_power_loss_before_the_switch_changes_nothing_that_matters(
    records: FileRecords,
) -> None:
    class CutAtStop(Windows):
        def stop_service(self, timeout_s: float) -> bool:
            super().stop_service(timeout_s)
            raise PowerLoss

    ops = CutAtStop(exists=True, state="running", python=OLD_PY)
    ops.current = OLD.version_dir
    with pytest.raises(PowerLoss):
        wsu.setup(ops, NEW, health_timeout_s=3, records=records)
    assert ops.python == OLD_PY and ops.current == OLD.version_dir  # still coherent
    assert records.read_journal() == Journal(OLD_PY, NEW.version_dir, "switching")


def test_a_journal_for_the_same_version_is_a_plain_repair(records: FileRecords) -> None:
    records.write_journal(Journal(NEW_PY, NEW.version_dir, "checking"))
    ops = Windows(exists=True, state="stopped", python=NEW_PY)
    result = wsu.setup(ops, NEW, health_timeout_s=3, records=records)
    assert result.outcome is wsu.SetupOutcome.OK
    assert result.previous_python is None


# ------------------------------------------------------ Q38 forced failure
def test_forced_failure_rolls_back_once_then_the_upgrade_works(
    records: FileRecords,
) -> None:
    records.folder.mkdir(parents=True)
    (records.folder / "fail-upgrade-to").write_text(f"{NEW.version_dir}\n")
    ops = upgrade_from_old()
    result = wsu.setup(ops, NEW, health_timeout_s=3, records=records)
    assert result.outcome is wsu.SetupOutcome.ROLLED_BACK
    assert any("forced upgrade failure" in line for line in result.lines)
    assert_invariant(ops)
    assert not (records.folder / "fail-upgrade-to").exists()  # one-shot
    again = wsu.setup(ops, NEW, health_timeout_s=3, records=records)
    assert again.outcome is wsu.SetupOutcome.OK
    assert_invariant(ops)
    assert not (
        records.folder / "rolled-back.json"
    ).exists()  # a good upgrade clears it


def test_forced_failure_never_applies_to_a_fresh_install(records: FileRecords) -> None:
    records.folder.mkdir(parents=True)
    (records.folder / "fail-upgrade-to").write_text("*\n")
    ops = Windows()
    result = wsu.setup(ops, NEW, health_timeout_s=3, records=records)
    assert result.outcome is wsu.SetupOutcome.OK


@pytest.mark.parametrize(
    ("trigger", "hit"),
    [
        ("*", True),
        ("0.17.0-2", True),
        ("0.17.0", True),
        ("0.17.0-1", False),
        ("0.18.0", False),
    ],
)
def test_trigger_matches_the_version_or_its_folder(
    records: FileRecords, trigger: str, hit: bool
) -> None:
    records.folder.mkdir(parents=True)
    (records.folder / "fail-upgrade-to").write_text(trigger)
    assert (records.forced_failure("0.17.0-2") is not None) is hit


def test_plain_version() -> None:
    assert plain_version("0.17.1-3") == "0.17.1"
    assert plain_version("0.17.1") == "0.17.1"
    assert plain_version("0.1.0.dev0-1") == "0.1.0.dev0"


def test_null_records_keep_g4_behaviour() -> None:
    ops = upgrade_from_old(behaviour={NEW_PY: "no_console"})
    result = wsu.setup(ops, NEW, health_timeout_s=3, records=NullRecords())
    assert result.outcome is wsu.SetupOutcome.ROLLED_BACK


def test_journal_file_format_is_the_linux_one(records: FileRecords) -> None:
    from meshforge.common.upgrade_state import upgrade_notice

    records.write_journal(Journal(OLD_PY, NEW.version_dir, "checking"))
    text = (records.folder / "state").read_text()
    assert f"from={OLD_PY}\n" in text and "phase=checking\n" in text
    notice = upgrade_notice(records.folder.parent, windows=True, require_root=False)
    assert notice is not None and notice.kind == "in_progress"
    assert "run the installer again" in notice.message


# ---------------------------- fix round 1: the journal names only {app} pythons
def test_a_journalled_python_must_exist_inside_the_program_folder(
    tmp_path: Path,
) -> None:
    root = tmp_path / "MeshForge"
    good = root / "0.17.0-1" / "python" / "python.exe"
    good.parent.mkdir(parents=True)
    good.write_bytes(b"")
    outside = tmp_path / "elsewhere" / "python.exe"
    outside.parent.mkdir()
    outside.write_bytes(b"")
    records = FileRecords(root)
    for python, kept in (
        (str(good), True),
        (str(outside), False),  # a foreign binary
        (str(root / "0.17.0-9" / "python" / "python.exe"), False),  # missing
        (str(root / "0.17.0-1" / "python" / "evil.exe"), False),  # not python.exe
        (str(root / ".." / "elsewhere" / "python.exe"), False),  # escapes {app}
    ):
        records.write_journal(Journal(python, "0.17.0-2", "checking"))
        journal = records.read_journal()
        assert journal is not None
        assert (journal.from_python == python) is kept, python
