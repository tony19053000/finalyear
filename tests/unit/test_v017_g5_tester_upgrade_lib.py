"""V0.17 G5 tester (brief §5e items 2+3, Q35/Q38/Q39): the Linux upgrade /
rollback / half-upgrade shell library, driven through whole upgrade *stories*
with a power cut at every step, rather than one function at a time.

The invariant after every outcome (brief): ``current`` names exactly one
complete version folder, and a start never sees two half-pointed versions.
The library is run for real with ``sh`` in a scratch folder (a folder name
with spaces on purpose); ``systemctl`` is a recording fake.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Final

import pytest

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX sh package scripts"),
]

REPO: Final = Path(__file__).resolve().parents[2]
LIB: Final = REPO / "packaging" / "linux" / "common" / "upgrade.sh"
OLD: Final = "0.17.0"
NEW: Final = "0.17.1"
NEWER: Final = "0.17.2"


class Machine:
    """A scratch /opt/meshforge, a boot id, a recording systemctl."""

    def __init__(self, root: Path, unit: str = "meshforge-agent.service") -> None:
        self.root = root
        self.unit = unit
        self.opt = root / "opt dir" / "meshforge"
        self.opt.mkdir(parents=True)
        self.boot_file = root / "boot_id"
        self.boot_file.write_text("boot-1\n")
        self.bin = root / "bin"
        self.bin.mkdir()
        self.calls = root / "systemctl.calls"
        fake = self.bin / "systemctl"
        fake.write_text(f'#!/bin/sh\necho "$*" >> "{self.calls}"\nexit 0\n')
        fake.chmod(0o755)
        self.systemd_dir = root / "run-systemd-system"

    # ---- the disk
    def tree(self, version: str, *, complete: bool = True) -> Path:
        folder = self.opt / version
        (folder / "bin").mkdir(parents=True, exist_ok=True)
        (folder / "lib").mkdir(exist_ok=True)
        (folder / "lib" / "big.so").write_text("x" * 200)
        (folder / "bin" / "meshforge-agent").write_text(f"#!/bin/sh\necho {version}\n")
        if complete:
            (folder / "BUILDINFO.json").write_text(json.dumps({"version": version}))
        return folder

    def point(self, target: str) -> None:
        link = self.opt / "current"
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(target)

    def reboot(self) -> None:
        self.boot_file.write_text(
            f"boot-{len(self.boot_file.read_text())}-{os.urandom(3).hex()}\n"
        )

    # ---- dpkg's part of the story
    def dpkg_unpack(self, new: str, old: str, *, stop_after: str = "all") -> None:
        """dpkg unpacks the new tree, removes the old tree's files (unlink, so
        hard links elsewhere survive) and swaps the pointer. ``stop_after``
        cuts the power at: ``new_partial`` | ``old_half_deleted`` | ``new_full``
        (all files there, old gone, pointer not yet swapped) | ``all``."""
        self.tree(new, complete=False)
        if stop_after == "new_partial":
            return
        if stop_after == "pointed_partial":  # the pointer is replaced early
            self.point(new)
            return
        (self.opt / new / "BUILDINFO.json").write_text(json.dumps({"version": new}))
        # dpkg deletes the old files one by one
        victim = self.opt / old / "lib" / "big.so"
        if victim.exists():
            victim.unlink()
        if stop_after == "old_half_deleted":
            return
        shutil.rmtree(self.opt / old, ignore_errors=True)
        if stop_after == "new_full":
            return
        self.point(new)

    # ---- running the library
    def sh(
        self, commands: str, *, version: str = NEW, root_owned: bool = True
    ) -> subprocess.CompletedProcess[str]:
        override = 'mf_root_owned() { [ ! -e "$1.not-root" ]; }\n' if root_owned else ""
        script = f"""
set -u
MF_OPT='{self.opt}'
MF_UNIT={self.unit}
MF_VERSION='{version}'
. '{LIB}'
MF_BOOT_ID_FILE='{self.boot_file}'
mf_install_dir() {{ mkdir -p "$1"; }}
{override}
{commands}
"""
        env = {**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}"}
        return subprocess.run(
            ["sh", "-c", script], capture_output=True, text=True, env=env, check=False
        )

    def ok(self, commands: str, **kwargs: object) -> str:
        done = self.sh(commands, **kwargs)  # type: ignore[arg-type]
        assert done.returncode == 0, done.stderr + done.stdout
        return done.stdout + done.stderr

    # ---- observations
    def state(self) -> dict[str, str]:
        path = self.opt / ".upgrade" / "state"
        if not path.exists():
            return {}
        return dict(
            line.split("=", 1) for line in path.read_text().splitlines() if "=" in line
        )

    def pointer(self) -> str:
        return os.readlink(self.opt / "current")

    def runs(self) -> str:
        """Invariant: ``current`` resolves to ONE complete version folder; the
        version it reports."""
        target = (self.opt / "current").resolve(strict=True)
        info = json.loads((target / "BUILDINFO.json").read_text())
        assert (target / "bin" / "meshforge-agent").is_file()
        assert (target / "lib" / "big.so").is_file(), f"{target} is half-deleted"
        return str(info["version"])

    def systemctl(self) -> list[str]:
        return self.calls.read_text().splitlines() if self.calls.exists() else []


@pytest.fixture
def machine(tmp_path: Path) -> Machine:
    if shutil.which("sh") is None:
        pytest.skip("no sh")
    made = Machine(tmp_path)
    made.tree(OLD)
    made.point(OLD)
    return made


# --------------------------------------------------------------------------
# Power loss at every step of an upgrade (Q39): after the reboot, a start
# (ExecStartPre recover) must leave `current` on a complete version.
# --------------------------------------------------------------------------
CUTS = [
    "preinst_done",
    "new_partial",
    "pointed_partial",
    "old_half_deleted",
    "new_full",
    "unpacked",
    "switched",
]


def _upgrade_until(machine: Machine, cut: str) -> None:
    machine.ok("mf_upgrade_begin")
    if cut == "preinst_done":
        return
    stop = {
        "new_partial": "new_partial",
        "pointed_partial": "pointed_partial",
        "old_half_deleted": "old_half_deleted",
        "new_full": "new_full",
        "unpacked": "all",
        "switched": "all",
    }[cut]
    machine.dpkg_unpack(NEW, OLD, stop_after=stop)
    if cut == "switched":
        machine.ok("mf_upgrade_switch")


@pytest.mark.parametrize("cut", CUTS)
def test_power_cut_at_every_step_then_a_start_runs_a_complete_version(
    machine: Machine, cut: str
) -> None:
    _upgrade_until(machine, cut)
    machine.reboot()
    machine.ok("mf_upgrade_recover")
    running = machine.runs()  # asserts the invariant itself
    if cut == "switched":
        assert running == NEW  # configuring phase: the new version finishes
        assert machine.state()["phase"] == "recovered-configuring"
    else:
        assert running == OLD  # anything before the switch: the kept previous
        assert machine.state()["phase"] == "recovered-unpacking"
    record = json.loads((machine.opt / ".upgrade" / "interrupted.json").read_text())
    assert record["from"] == OLD and record["to"] == NEW
    assert record["action"] == ("finishing" if cut == "switched" else "reverted")


@pytest.mark.parametrize("cut", CUTS)
def test_recovery_runs_once_and_a_second_start_changes_nothing(
    machine: Machine, cut: str
) -> None:
    _upgrade_until(machine, cut)
    machine.reboot()
    machine.ok("mf_upgrade_recover")
    before = (
        machine.pointer(),
        dict(machine.state()),
        (machine.opt / ".upgrade" / "interrupted.json").read_text(),
    )
    machine.reboot()
    machine.ok("mf_upgrade_recover")
    machine.ok("mf_upgrade_recover")
    after = (
        machine.pointer(),
        dict(machine.state()),
        (machine.opt / ".upgrade" / "interrupted.json").read_text(),
    )
    assert before == after


@pytest.mark.parametrize(
    "cut",
    ["preinst_done", "new_partial", "pointed_partial", "old_half_deleted", "new_full"],
)
def test_after_a_reverted_start_dpkg_configure_a_finishes_the_upgrade(
    machine: Machine, cut: str
) -> None:
    """The repair run (`dpkg --configure -a`) is just the postinst again; the
    unpacked-but-unconfigured new tree it needs must still be complete or the
    postinst's own start fails honestly - the old version must still be
    recoverable either way."""
    _upgrade_until(machine, cut)
    machine.reboot()
    machine.ok("mf_upgrade_recover")
    assert machine.runs() == OLD
    # dpkg re-runs the unpack (retry) then configure
    machine.ok("mf_upgrade_begin")  # preinst of the retried upgrade
    assert machine.state()["from"] == OLD
    assert (machine.opt / ".previous" / OLD / "BUILDINFO.json").is_file()
    shutil.rmtree(machine.opt / NEW, ignore_errors=True)
    machine.dpkg_unpack(NEW, OLD)  # old folder may already be gone: tolerated
    machine.ok("mf_upgrade_switch")
    assert machine.runs() == NEW
    machine.ok("mf_upgrade_done")
    assert not (machine.opt / ".previous").exists()
    assert not (machine.opt / ".upgrade" / "state").exists()


def test_the_same_boot_means_an_upgrade_is_running_and_is_left_alone(
    machine: Machine,
) -> None:
    _upgrade_until(machine, "old_half_deleted")
    machine.ok("mf_upgrade_recover")  # no reboot
    assert machine.state()["phase"] == "unpacking"
    assert not (machine.opt / ".upgrade" / "interrupted.json").exists()


def test_a_cut_before_any_state_file_is_a_noop(machine: Machine) -> None:
    machine.reboot()
    out = machine.ok("mf_upgrade_recover")
    assert out.strip() == ""
    assert machine.runs() == OLD


# --------------------------------------------------------------------------
# damaged records: a start must never fail and never invent a pointer
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"\x00\x01\xff\xfe garbage",
        b"phase=unpacking\n",  # no boot, no from/to
        b"phase=unpacking\nboot=other\nprevious=../../etc\n",
        b"phase=unpacking\nboot=other\nprevious=.previous/does-not-exist\nfrom=a\nto=b\n",
        b"phase=exploding\nboot=other\n",
        b"phase=unpacking\nboot=other\nprevious=" + b"x" * 100_000 + b"\n",
        b"phase=\nboot=other\n",
    ],
    ids=[
        "empty",
        "binary",
        "no-boot",
        "traversal",
        "missing-prev",
        "unknown-phase",
        "huge",
        "blank-phase",
    ],
)
def test_a_damaged_state_file_never_breaks_a_start_or_moves_the_pointer_wildly(
    machine: Machine, content: bytes
) -> None:
    (machine.opt / ".upgrade").mkdir()
    (machine.opt / ".upgrade" / "state").write_bytes(content)
    machine.reboot()
    done = machine.sh("mf_upgrade_recover")
    assert done.returncode == 0, done.stderr
    target = machine.pointer()
    # whatever happened, `current` is the original or a kept folder INSIDE the
    # program folder - never a path outside it
    assert ".." not in target and not target.startswith("/")
    assert machine.runs() == OLD


@pytest.mark.xfail(
    strict=True,
    reason="G5-T2 (minor, hardening): the state file's `previous=` is not "
    "validated, so root-written '../evil' becomes the pointer; only root can "
    "write the folder, so this is robustness, not a privilege boundary",
)
def test_traversal_in_the_state_file_cannot_point_current_outside(
    machine: Machine,
) -> None:
    """previous=../../etc: BUILDINFO.json would have to exist there to be
    accepted; make it exist (an attacker-prepared folder next to the program
    dir) and check the library still refuses to follow it out of the folder."""
    outside = machine.opt.parent / "evil"
    outside.mkdir()
    (outside / "BUILDINFO.json").write_text("{}")
    (machine.opt / ".upgrade").mkdir()
    (machine.opt / ".upgrade" / "state").write_text(
        "from=0.17.0\nto=0.17.1\nphase=unpacking\nprevious=../evil\nboot=zzz\n"
    )
    machine.reboot()
    machine.ok("mf_upgrade_recover")
    assert ".." not in machine.pointer(), machine.pointer()


# --------------------------------------------------------------------------
# the rollback matrix (Linux rows) with pointer + service invariants
# --------------------------------------------------------------------------
def test_row_new_version_unhealthy_previous_runs_service_restarted(
    machine: Machine,
) -> None:
    machine.systemd_dir.mkdir()
    machine.ok("mf_upgrade_begin")
    machine.dpkg_unpack(NEW, OLD)
    machine.ok("mf_upgrade_switch")
    assert machine.runs() == NEW
    # health check fails -> postinst rolls back
    rolled = machine.sh("mf_upgrade_rollback 'console did not answer' && echo rolled")
    assert rolled.returncode == 0 and "rolled" in rolled.stdout
    assert machine.runs() == OLD
    assert machine.pointer() == f".previous/{OLD}"
    record = json.loads((machine.opt / ".upgrade" / "rolled-back.json").read_text())
    assert record["reason"] == "console did not answer"
    assert record["from"] == OLD and record["to"] == NEW
    machine.ok("mf_upgrade_rolled_back_ok")
    assert not (machine.opt / ".upgrade" / "state").exists()
    # the kept copy is what runs: it must still be there
    assert (machine.opt / ".previous" / OLD / "BUILDINFO.json").is_file()


def test_rollback_restarts_the_unit_after_the_pointer_moved(machine: Machine) -> None:
    """Order matters: stop -> (pointer) -> start. A start before the pointer
    moves would run the new version again."""
    machine.ok("mf_upgrade_begin")
    machine.dpkg_unpack(NEW, OLD)
    machine.ok("mf_upgrade_switch")
    probe = machine.root / "pointer-at-start"
    # the fake systemctl records the pointer each time it is called
    fake = machine.bin / "systemctl"
    fake.write_text(
        f'#!/bin/sh\necho "$* -> $(readlink "{machine.opt}/current")" >> "{probe}"\nexit 0\n'
    )
    fake.chmod(0o755)
    # /run/systemd/system must exist for the library to call systemctl: it is
    # an absolute path; only run this part when the host has it
    if not Path("/run/systemd/system").is_dir():
        pytest.skip(
            "host has no /run/systemd/system; the restart branch is not reachable"
        )
    machine.ok("mf_upgrade_rollback why")
    lines = probe.read_text().splitlines()
    stop = next(i for i, line in enumerate(lines) if line.startswith("stop"))
    start = next(i for i, line in enumerate(lines) if line.startswith("start"))
    assert stop < start
    assert lines[start].endswith(f".previous/{OLD}")


def test_row_no_previous_kept_rollback_changes_nothing_and_says_so(
    machine: Machine,
) -> None:
    # preinst could not keep a copy (no BUILDINFO.json in the running tree)
    (machine.opt / OLD / "BUILDINFO.json").unlink()
    out = machine.ok("mf_upgrade_begin")
    assert "no rollback copy" in out or machine.state()["previous"] == ""
    machine.dpkg_unpack(NEW, OLD)
    machine.ok("mf_upgrade_switch")
    done = machine.sh("mf_upgrade_rollback x")
    assert done.returncode == 1
    assert machine.runs() == NEW  # untouched, still a complete version
    assert not (machine.opt / ".upgrade" / "rolled-back.json").exists()


def test_row_migration_refused_pointer_moves_back_without_a_restart(
    machine: Machine,
) -> None:
    machine.ok("mf_upgrade_begin")
    machine.dpkg_unpack(NEW, OLD)
    machine.ok("mf_upgrade_switch")
    machine.ok("mf_upgrade_rollback 'identity migration: boom' norestart")
    assert machine.runs() == OLD
    assert machine.systemctl() == []  # the running process is the old one already


def test_rollback_twice_is_idempotent(machine: Machine) -> None:
    machine.ok("mf_upgrade_begin")
    machine.dpkg_unpack(NEW, OLD)
    machine.ok("mf_upgrade_switch")
    machine.ok("mf_upgrade_rollback first norestart")
    machine.ok("mf_upgrade_rollback second norestart")
    assert machine.runs() == OLD
    assert machine.pointer() == f".previous/{OLD}"


def test_rolled_back_laptop_then_a_good_upgrade_ends_clean(machine: Machine) -> None:
    # failed upgrade to NEW
    machine.ok("mf_upgrade_begin")
    machine.dpkg_unpack(NEW, OLD)
    machine.ok("mf_upgrade_switch")
    machine.ok("mf_upgrade_rollback boom norestart")
    machine.ok("mf_upgrade_rolled_back_ok")
    assert machine.runs() == OLD
    # a fixed package NEWER arrives: running tree is .previous/OLD
    machine.ok("mf_upgrade_begin", version=NEWER)
    assert machine.state()["from"] == OLD
    assert machine.state()["previous"] == f".previous/{OLD}"
    machine.dpkg_unpack(NEWER, NEW)
    shutil.rmtree(machine.opt / NEW, ignore_errors=True)
    machine.ok("mf_upgrade_switch", version=NEWER)
    machine.ok("mf_upgrade_done", version=NEWER)
    assert machine.runs() == NEWER
    assert sorted(p.name for p in machine.opt.iterdir() if p.name[0] != ".") == [
        "0.17.2",
        "current",
    ]
    assert list((machine.opt / ".upgrade").iterdir()) == []
    # no stale failure notice
    assert not (machine.opt / ".upgrade" / "rolled-back.json").exists()
    assert not (machine.opt / ".upgrade" / "state").exists()


def test_power_cut_while_running_a_rolled_back_version_does_not_flip_it(
    machine: Machine,
) -> None:
    machine.ok("mf_upgrade_begin")
    machine.dpkg_unpack(NEW, OLD)
    machine.ok("mf_upgrade_switch")
    machine.ok("mf_upgrade_rollback boom norestart")  # state=rolled_back
    machine.reboot()
    machine.ok("mf_upgrade_recover")
    assert machine.runs() == OLD
    assert machine.state()["phase"] == "rolled_back"


def test_two_upgrades_back_to_back_keep_exactly_one_previous(machine: Machine) -> None:
    for old, new in ((OLD, NEW), (NEW, NEWER)):
        machine.ok("mf_upgrade_begin", version=new)
        machine.dpkg_unpack(new, old)
        machine.ok("mf_upgrade_switch", version=new)
        machine.ok("mf_upgrade_done", version=new)
        assert machine.runs() == new
    assert not (machine.opt / ".previous").exists()


def test_begin_cleans_a_stale_tmp_copy_and_odd_entries(machine: Machine) -> None:
    prev = machine.opt / ".previous"
    (prev / ".tmp" / "junk").mkdir(parents=True)
    (prev / "0.9.9").mkdir()
    (prev / ".hidden").mkdir()
    machine.ok("mf_upgrade_begin")
    assert sorted(p.name for p in prev.iterdir()) == [OLD]


def test_the_kept_copy_survives_dpkg_deleting_the_original(machine: Machine) -> None:
    machine.ok("mf_upgrade_begin")
    shutil.rmtree(machine.opt / OLD)
    kept = machine.opt / ".previous" / OLD
    assert (kept / "lib" / "big.so").read_text() == "x" * 200
    assert (kept / "bin" / "meshforge-agent").is_file()


def test_a_foreign_current_symlink_is_not_copied_or_followed(machine: Machine) -> None:
    machine.point("../../../etc")
    out = machine.ok("mf_upgrade_begin")
    assert "not ours" in out
    assert not (machine.opt / ".previous").exists() or not any(
        (machine.opt / ".previous").iterdir()
    )
    assert machine.state()["previous"] == ""


def test_two_packages_share_nothing_in_their_upgrade_state(tmp_path: Path) -> None:
    """Agent (/opt/meshforge) and controller (/opt/meshforge/controller) on one
    machine: the agent's forget/begin never touches the controller's records."""
    if shutil.which("sh") is None:
        pytest.skip("no sh")
    agent = Machine(tmp_path)
    agent.tree(OLD)
    agent.point(OLD)
    ctl_opt = agent.opt / "controller"
    ctl_opt.mkdir()
    (ctl_opt / ".upgrade").mkdir()
    (ctl_opt / ".upgrade" / "state").write_text("phase=unpacking\n")
    (ctl_opt / ".previous" / "0.9").mkdir(parents=True)
    agent.ok("mf_upgrade_begin")
    agent.ok("mf_upgrade_done")
    agent.ok("mf_upgrade_forget")
    assert (ctl_opt / ".upgrade" / "state").read_text() == "phase=unpacking\n"
    assert (ctl_opt / ".previous" / "0.9").is_dir()
    assert (
        not (agent.opt / ".previous").exists() and not (agent.opt / ".upgrade").exists()
    )


def test_forget_also_removes_pointer_temp_links(machine: Machine) -> None:
    (machine.opt / ".current.99999").symlink_to(OLD)
    machine.ok("mf_upgrade_begin")
    machine.ok("mf_upgrade_forget")
    assert [p.name for p in machine.opt.iterdir() if p.name.startswith(".")] == []


# --------------------------------------------------------------------------
# Q38: the forced-failure trigger
# --------------------------------------------------------------------------
def _trigger(machine: Machine, text: bytes | str) -> Path:
    folder = machine.opt / ".upgrade"
    folder.mkdir(exist_ok=True)
    path = folder / "fail-upgrade-to"
    path.write_bytes(text if isinstance(text, bytes) else text.encode())
    return path


@pytest.mark.parametrize(
    ("content", "hit"),
    [
        (f"{NEW}\n", True),
        (f"{NEW}\r\n", True),
        (f"  {NEW}  \n", True),
        ("*\n", True),
        (f"{NEW}\nsecond line ignored\n", True),
        (f"{OLD}\n", False),
        (f"{NEW}-extra\n", False),
        (f"x{NEW}\n", False),
        ("", False),
        ("\n", False),
        ("**\n", False),
        (f"# {NEW}\n", False),
        ("0.17.*\n", False),  # only a literal version or one star
    ],
)
def test_trigger_content_rules(machine: Machine, content: str, hit: bool) -> None:
    _trigger(machine, content)
    done = machine.sh("mf_forced_failure")
    assert (done.returncode == 0) is hit


def test_a_symlinked_trigger_is_ignored_even_if_it_points_at_a_good_file(
    machine: Machine,
) -> None:
    real = machine.root / "elsewhere"
    real.write_text(f"{NEW}\n")
    (machine.opt / ".upgrade").mkdir()
    (machine.opt / ".upgrade" / "fail-upgrade-to").symlink_to(real)
    assert machine.sh("mf_forced_failure").returncode != 0


def test_a_directory_or_fifo_named_like_the_trigger_is_ignored(
    machine: Machine,
) -> None:
    (machine.opt / ".upgrade" / "fail-upgrade-to").mkdir(parents=True)
    assert machine.sh("mf_forced_failure").returncode != 0
    (machine.opt / ".upgrade" / "fail-upgrade-to").rmdir()
    os.mkfifo(machine.opt / ".upgrade" / "fail-upgrade-to")
    done = subprocess.run(  # a fifo must not hang the check
        ["sh", "-c", "true"], check=True
    )
    assert done.returncode == 0
    assert machine.sh("mf_forced_failure").returncode != 0


@pytest.mark.skipif(os.geteuid() == 0, reason="needs a non-root test user")
def test_the_real_ownership_check_refuses_a_file_a_non_root_user_wrote(
    machine: Machine,
) -> None:
    """No override of mf_root_owned here: this is the library as shipped. The
    file is owned by the test user (not root) -> it must not count. This is what
    stops the service account (or job code running as it) from planting the
    trigger in a folder it somehow could write."""
    _trigger(machine, f"{NEW}\n")
    assert machine.sh("mf_forced_failure", root_owned=False).returncode != 0
    assert machine.sh("mf_root_owned '/'", root_owned=False).returncode == 0


def test_a_forced_failure_is_consumed_by_the_rollback_it_causes(
    machine: Machine,
) -> None:
    machine.ok("mf_upgrade_begin")
    machine.dpkg_unpack(NEW, OLD)
    machine.ok("mf_upgrade_switch")
    trigger = _trigger(machine, f"{NEW}\n")
    machine.ok("mf_upgrade_rollback forced norestart")
    assert not trigger.exists()
    assert machine.sh("mf_forced_failure").returncode != 0


def test_a_trigger_for_another_version_is_not_consumed(machine: Machine) -> None:
    machine.ok("mf_upgrade_begin")
    machine.dpkg_unpack(NEW, OLD)
    machine.ok("mf_upgrade_switch")
    trigger = _trigger(machine, f"{NEWER}\n")
    machine.ok("mf_upgrade_rollback real-failure norestart")
    assert trigger.exists()


def test_a_trigger_planted_in_the_data_dir_is_never_read(machine: Machine) -> None:
    """Q38 as approved said 'under the admin-only data dir'; the implementation
    moved it to the program folder because /var/lib/meshforge belongs to the
    service account. Whatever is planted there must have no effect."""
    data = machine.root / "var-lib-meshforge"
    (data / ".upgrade").mkdir(parents=True)
    (data / ".upgrade" / "fail-upgrade-to").write_text("*\n")
    (data / "fail-upgrade-to").write_text("*\n")
    assert machine.sh("mf_forced_failure").returncode != 0


def test_exec_start_pre_is_best_effort_in_both_units() -> None:
    for unit in (
        REPO / "packaging/linux/meshforge-agent.service",
        REPO / "packaging/linux/controller/meshforge-controller.service",
    ):
        text = unit.read_text(encoding="utf-8")
        assert "ExecStartPre=-+" in text, (
            f"{unit.name}: recovery must never block a start"
        )


def test_power_cut_inside_the_rollback_itself_still_runs_a_complete_version(
    machine: Machine,
) -> None:
    """The cut lands after the pointer went back to .previous but before the
    record says rolled_back (state still 'configuring')."""
    machine.ok("mf_upgrade_begin")
    machine.dpkg_unpack(NEW, OLD)
    machine.ok("mf_upgrade_switch")
    machine.point(f".previous/{OLD}")  # rollback's first step, then the plug
    machine.reboot()
    machine.ok("mf_upgrade_recover")
    assert machine.runs() == OLD  # the pointer is not flipped back to the new one
    assert machine.pointer() == f".previous/{OLD}"
    # and the repair run still has what it needs
    machine.ok("mf_upgrade_switch")
    assert machine.runs() == NEW
