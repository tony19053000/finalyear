"""V0.17 G5 tester (brief §5e items 2+5, Q39): the Windows installer's setup run
under a power cut at EVERY step, then the repair run; plus damaged journals and
trigger files. Windows is the G4 in-memory fake (the SCM, ``current.txt``,
files in a scratch folder) with the real file-based upgrade records.

Acceptance read from the brief: "next start/repair detects half-state and
finishes or reverts; never two versions half-pointed" and "the pointer
(``current.txt``) and the service's binary path always name the same version
after any outcome".
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath

import pytest
from test_v017_g4_winsetup import NEW, NEW_PY, OLD, OLD_PY, FakeOps

from meshforge.agent import winsetup as wsu
from meshforge.agent.wininstall import program_layout
from meshforge.agent.winupgrade import FileRecords, Journal

pytestmark = pytest.mark.unit


class PowerLoss(BaseException):
    """Not an Exception: nothing in setup may swallow the plug being pulled."""


@dataclass
class Counter:
    count: int = 0
    cut_at: int | None = None

    def tick(self) -> None:
        self.count += 1
        if self.cut_at is not None and self.count == self.cut_at:
            raise PowerLoss


@dataclass
class Windows(FakeOps):
    """G4 fake + ``current.txt`` + a cut at the Nth operation (of any kind)."""

    current: str | None = None
    counter: Counter = field(default_factory=Counter)

    def _call(self, name: str, arg: object = None) -> None:
        self.counter.tick()
        super()._call(name, arg)

    def write_current(self, version_dir: str) -> None:
        super().write_current(version_dir)
        self.current = version_dir

    def reboot(self) -> None:
        """Power is back: a stopped-in-the-middle service is simply down; the
        SCM keeps whatever binPath it had."""
        self.counter.cut_at = None
        if self.exists:
            self.state = "stopped"


class CountingRecords(FileRecords):
    """The real records, each call counted (a cut can land between any two)."""

    def __init__(self, root: Path, counter: Counter) -> None:
        super().__init__(root)
        self.counter = counter

    def write_journal(self, journal: Journal) -> None:
        self.counter.tick()
        super().write_journal(journal)

    def clear_journal(self) -> None:
        self.counter.tick()
        super().clear_journal()

    def write_rolled_back(self, *, from_dir: str, to_dir: str, reason: str) -> None:
        self.counter.tick()
        super().write_rolled_back(from_dir=from_dir, to_dir=to_dir, reason=reason)

    def clear_rolled_back(self) -> None:
        self.counter.tick()
        super().clear_rolled_back()

    def consume_forced_failure(self) -> None:
        self.counter.tick()
        super().consume_forced_failure()


def binpath_dir(ops: Windows) -> str:
    assert ops.python is not None
    return program_layout(PureWindowsPath(ops.python)).version_dir


def fresh_upgrade(new_behaviour: str, counter: Counter | None = None) -> Windows:
    ops = Windows(
        exists=True,
        state="running",
        python=OLD_PY,
        is_enrolled=True,
        behaviour={NEW_PY: new_behaviour} if new_behaviour != "healthy" else {},
        counter=counter or Counter(),
    )
    ops.current = OLD.version_dir
    return ops


def total_steps(new_behaviour: str, tmp: Path) -> int:
    counter = Counter()
    ops = fresh_upgrade(new_behaviour, counter)
    wsu.setup(ops, NEW, health_timeout_s=3, records=CountingRecords(tmp, counter))
    return counter.count


def _count(new_behaviour: str) -> int:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        return total_steps(new_behaviour, Path(tmp))


STEPS_HEALTHY = _count("healthy")
STEPS_UNHEALTHY = _count("no_console")


def test_the_sweeps_cover_a_meaningful_number_of_steps() -> None:
    # guards the sweep itself: if setup stops calling anything the sweep is empty
    assert STEPS_HEALTHY >= 10 and STEPS_UNHEALTHY > STEPS_HEALTHY


def _cut_then_repair(
    tmp_path: Path, behaviour: str, cut_at: int
) -> tuple[Windows, wsu.SetupResult, FileRecords]:
    counter = Counter(cut_at=cut_at)
    ops = fresh_upgrade(behaviour, counter)
    records = CountingRecords(tmp_path / "MeshForge", counter)
    with pytest.raises(PowerLoss):
        wsu.setup(ops, NEW, health_timeout_s=3, records=records)
    # --- the plug is back; what the machine looks like BEFORE any repair
    ops.reboot()
    # the service must never be in a state setup cannot repair, and the SCM must
    # name one of the two installed versions
    assert binpath_dir(ops) in (OLD.version_dir, NEW.version_dir)
    # --- the repair: the same installer once more
    result = wsu.setup(ops, NEW, health_timeout_s=3, records=records)
    return ops, result, records


@pytest.mark.parametrize("cut_at", range(1, STEPS_HEALTHY + 1))
def test_power_cut_at_step_n_of_a_good_upgrade_then_repair_ends_on_the_new_version(
    tmp_path: Path, cut_at: int
) -> None:
    ops, result, records = _cut_then_repair(tmp_path, "healthy", cut_at)
    assert result.outcome is wsu.SetupOutcome.OK, (cut_at, result.lines)
    assert ops.python == NEW_PY and ops.state == "running"
    assert ops.current == binpath_dir(ops) == NEW.version_dir
    assert records.read_journal() is None  # coherent end: journal closed
    assert not (records.folder / "rolled-back.json").exists()


@pytest.mark.parametrize("cut_at", range(1, STEPS_UNHEALTHY + 1))
def test_power_cut_at_step_n_of_a_failing_upgrade_then_repair_ends_on_the_old_version(
    tmp_path: Path, cut_at: int
) -> None:
    ops, result, records = _cut_then_repair(tmp_path, "no_console", cut_at)
    # the new version never becomes healthy: whatever the cut, the end state is
    # the previous version running (never "neither", never the broken one)
    assert result.outcome is wsu.SetupOutcome.ROLLED_BACK, (cut_at, result.lines)
    assert ops.python == OLD_PY and ops.state == "running"
    assert ops.current == binpath_dir(ops) == OLD.version_dir
    assert records.read_journal() is None


@pytest.mark.parametrize("cut_at", range(1, STEPS_HEALTHY + 1))
def test_a_cut_is_never_a_crash_of_the_next_boot_even_without_a_repair(
    tmp_path: Path, cut_at: int
) -> None:
    """Between the cut and the repair the SCM may autostart the service. The
    journal (if any) must be readable and name a real version folder."""
    counter = Counter(cut_at=cut_at)
    ops = fresh_upgrade("healthy", counter)
    records = CountingRecords(tmp_path / "MeshForge", counter)
    with pytest.raises(PowerLoss):
        wsu.setup(ops, NEW, health_timeout_s=3, records=records)
    journal = records.read_journal()
    if journal is not None:
        assert journal.to_version_dir == NEW.version_dir
        assert journal.from_python in (None, OLD_PY)
    if records.folder.exists():  # an atomic write never leaves its temp file
        assert [p.name for p in records.folder.glob(".*")] == []


def test_a_repair_run_that_is_itself_cut_can_be_repaired_again(tmp_path: Path) -> None:
    """Two power cuts in a row (repair interrupted): the third run still ends
    coherent. Sweep the second cut over every step of the repair."""
    first_cut = max(3, STEPS_UNHEALTHY // 2)
    for second_cut in range(1, STEPS_UNHEALTHY + 1):
        scratch = tmp_path / f"s{second_cut}"
        counter = Counter(cut_at=first_cut)
        ops = fresh_upgrade("no_console", counter)
        records = CountingRecords(scratch / "MeshForge", counter)
        with pytest.raises(PowerLoss):
            wsu.setup(ops, NEW, health_timeout_s=3, records=records)
        ops.reboot()
        counter.count = 0
        counter.cut_at = second_cut
        try:
            wsu.setup(ops, NEW, health_timeout_s=3, records=records)
        except PowerLoss:
            ops.reboot()
        counter.cut_at = None
        result = wsu.setup(ops, NEW, health_timeout_s=3, records=records)
        assert result.outcome is wsu.SetupOutcome.ROLLED_BACK, (
            first_cut,
            second_cut,
            result.lines,
        )
        assert ops.python == OLD_PY and ops.current == OLD.version_dir
        assert ops.state == "running"


# --------------------------------------------------------- damaged records
@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"\x00\xff\xfe binary",
        b"to=\nfrom=C:\\x\n",
        b"from=" + b"A" * 100_000 + b"\nto=0.17.0-2\nphase=checking\n",
        b"to=..\\..\\evil\nfrom=\nphase=switching\n",
        "to=0.17.0-2\nfrom=C:\\Prögram Files\\MeshForge\\0.17.0-1\\python\\python.exe\n".encode(),
    ],
    ids=["empty", "binary", "no-to", "huge-from", "traversal-to", "unicode-from"],
)
def test_a_damaged_journal_never_breaks_a_setup_run(
    tmp_path: Path, content: bytes
) -> None:
    folder = tmp_path / "MeshForge" / "upgrade"
    folder.mkdir(parents=True)
    (folder / "state").write_bytes(content)
    ops = fresh_upgrade("healthy")
    result = wsu.setup(
        ops, NEW, health_timeout_s=3, records=FileRecords(tmp_path / "MeshForge")
    )
    assert result.outcome is wsu.SetupOutcome.OK, result.lines
    assert ops.current == binpath_dir(ops) == NEW.version_dir


def test_an_unhealthy_upgrade_with_a_journal_pointing_nowhere_never_strands_the_service(
    tmp_path: Path,
) -> None:
    """A journal whose `from` is an interpreter that does not exist must not be
    followed blindly; the real running previous (the SCM's) wins."""
    records = FileRecords(tmp_path / "MeshForge")
    records.write_journal(Journal(r"C:\gone\python.exe", NEW.version_dir, "checking"))
    ops = fresh_upgrade("no_console")
    result = wsu.setup(ops, NEW, health_timeout_s=3, records=records)
    assert result.outcome is wsu.SetupOutcome.ROLLED_BACK
    assert ops.python == OLD_PY and ops.state == "running"
    assert ops.current == binpath_dir(ops)


def test_a_journal_that_is_a_symlink_is_ignored(tmp_path: Path) -> None:
    folder = tmp_path / "MeshForge" / "upgrade"
    folder.mkdir(parents=True)
    target = tmp_path / "elsewhere"
    target.write_text(f"from={OLD_PY}\nto={NEW.version_dir}\nphase=checking\n")
    try:
        (folder / "state").symlink_to(target)
    except OSError:
        pytest.skip("symlinks not permitted")
    assert FileRecords(tmp_path / "MeshForge").read_journal() is None


def test_a_trigger_that_is_a_symlink_or_a_folder_is_ignored(tmp_path: Path) -> None:
    folder = tmp_path / "MeshForge" / "upgrade"
    folder.mkdir(parents=True)
    target = tmp_path / "elsewhere"
    target.write_text("*\n")
    records = FileRecords(tmp_path / "MeshForge")
    try:
        (folder / "fail-upgrade-to").symlink_to(target)
    except OSError:
        pytest.skip("symlinks not permitted")
    assert records.forced_failure(NEW.version_dir) is None
    (folder / "fail-upgrade-to").unlink()
    (folder / "fail-upgrade-to").mkdir()
    assert records.forced_failure(NEW.version_dir) is None


@pytest.mark.parametrize(
    ("text", "hit"),
    [
        ("*", True),
        ("*\r\n", True),
        ("0.17.0-2\r\n", True),
        ("0.17.0\n", True),
        ("  0.17.0-2  \n", True),
        ("", False),
        pytest.param("\n\n0.17.0-2\n", False),
        ("0.17", False),
        ("0.17.0-2 # note", False),
        ("0.17.*", False),
        ("*\x00", False),
    ],
)
def test_windows_trigger_content_rules(tmp_path: Path, text: str, hit: bool) -> None:
    folder = tmp_path / "MeshForge" / "upgrade"
    folder.mkdir(parents=True)
    (folder / "fail-upgrade-to").write_text(text, encoding="utf-8")
    got = FileRecords(tmp_path / "MeshForge").forced_failure(NEW.version_dir)
    assert (got is not None) is hit


def test_the_trigger_is_one_shot_across_two_unhealthy_attempts(tmp_path: Path) -> None:
    """A real failure while a trigger exists consumes it too (it was 'used'),
    so a stale trigger cannot fail the next, healthy, upgrade."""
    records = FileRecords(tmp_path / "MeshForge")
    records.folder.mkdir(parents=True)
    (records.folder / "fail-upgrade-to").write_text("*\n")
    ops = fresh_upgrade("no_console")
    first = wsu.setup(ops, NEW, health_timeout_s=3, records=records)
    assert first.outcome is wsu.SetupOutcome.ROLLED_BACK
    assert not (records.folder / "fail-upgrade-to").exists()
    ops.behaviour = {}
    second = wsu.setup(ops, NEW, health_timeout_s=3, records=records)
    assert second.outcome is wsu.SetupOutcome.OK
    assert ops.current == binpath_dir(ops) == NEW.version_dir


def test_forced_failure_trigger_is_not_applied_to_a_same_version_repair(
    tmp_path: Path,
) -> None:
    records = FileRecords(tmp_path / "MeshForge")
    records.folder.mkdir(parents=True)
    (records.folder / "fail-upgrade-to").write_text("*\n")
    ops = Windows(exists=True, state="stopped", python=NEW_PY, is_enrolled=True)
    ops.current = NEW.version_dir
    result = wsu.setup(ops, NEW, health_timeout_s=3, records=records)
    assert result.outcome is wsu.SetupOutcome.OK


# --------------------------------------------------- rollback-too rows
def test_rollback_target_refuses_to_start_too_leaves_a_failed_result_not_a_crash(
    tmp_path: Path,
) -> None:
    ops = fresh_upgrade("exit4")
    ops.behaviour[OLD_PY] = "exit4"
    result = wsu.setup(
        ops, NEW, health_timeout_s=3, records=FileRecords(tmp_path / "M")
    )
    assert result.outcome is wsu.SetupOutcome.FAILED
    # honest: the journal stays so a later repair still knows where to go
    journal = FileRecords(tmp_path / "M").read_journal()
    assert journal is not None and journal.from_python == OLD_PY
    assert any("failed too" in line for line in result.lines)


def test_result_json_for_a_rollback_is_machine_readable(tmp_path: Path) -> None:
    ops = fresh_upgrade("no_console")
    result = wsu.setup(
        ops, NEW, health_timeout_s=3, records=FileRecords(tmp_path / "M")
    )
    data = result.as_dict()
    assert json.loads(json.dumps(data))["outcome"] == "rolled_back"
    assert data["running_python"] == OLD_PY
    assert int(result.exit_code) != 0  # the installer must NOT report success
