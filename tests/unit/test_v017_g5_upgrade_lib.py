"""V0.17 G5 (Q35/Q38/Q39): the shared upgrade/rollback shell functions of the
.deb maintainer scripts (``packaging/linux/common/upgrade.sh``), run for real
with ``sh`` in a scratch program folder, plus the build step that inlines them.

What the functions promise, row by row (brief §5e rollback matrix, Linux):

* preinst keeps the running version as a **hard-link** copy in ``.previous``
  (survives dpkg deleting the originals) and starts an upgrade record;
* the switch, the success clean-up, the rollback (pointer back + record +
  restart) and the no-restart rollback (migration refused);
* the forced-failure trigger is one-shot and only a root-owned regular file;
* a power loss: another boot + phase ``unpacking`` -> back on the kept
  version; phase ``configuring`` -> the new one finishes; the same boot ->
  nothing (dpkg is running right now); never acted on twice;
* the invariant after every outcome: ``current`` names exactly one complete
  version folder.

``systemctl`` is a fake on ``PATH`` that only records its arguments.
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
sys.path.insert(0, str(REPO / "scripts"))

import build_deb  # noqa: E402

OLD: Final = "0.17.0"
NEW: Final = "0.17.1"


class Box:
    """A scratch program folder + boot id + fake systemctl."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.opt = root / "opt"
        self.opt.mkdir()
        self.boot = root / "boot_id"
        self.boot.write_text("boot-A\n")
        self.bin = root / "bin"
        self.bin.mkdir()
        self.calls = root / "systemctl.calls"
        fake = self.bin / "systemctl"
        fake.write_text(f'#!/bin/sh\necho "$*" >> "{self.calls}"\nexit 0\n')
        fake.chmod(0o755)

    def version(self, name: str, *, parent: Path | None = None) -> Path:
        folder = (parent or self.opt) / name
        (folder / "bin").mkdir(parents=True)
        (folder / "BUILDINFO.json").write_text(json.dumps({"version": name}))
        (folder / "bin" / "meshforge-agent").write_text(f"#!/bin/sh\necho {name}\n")
        return folder

    def point(self, target: str) -> None:
        link = self.opt / "current"
        if link.is_symlink():
            link.unlink()
        link.symlink_to(target)

    def sh(
        self, commands: str, *, version: str = NEW
    ) -> subprocess.CompletedProcess[str]:
        script = f"""
set -eu
MF_OPT='{self.opt}'
MF_UNIT=meshforge-agent.service
MF_VERSION='{version}'
. '{LIB}'
MF_BOOT_ID_FILE='{self.boot}'
mf_install_dir() {{ mkdir -p "$1"; }}
mf_root_owned() {{ [ ! -e "$1.not-root" ]; }}
{commands}
"""
        env = {**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}"}
        return subprocess.run(
            ["sh", "-c", script], capture_output=True, text=True, env=env, check=False
        )

    def ok(self, commands: str, **kwargs: str) -> str:
        done = self.sh(commands, **kwargs)
        assert done.returncode == 0, done.stderr + done.stdout
        return done.stdout

    def state(self) -> dict[str, str]:
        path = self.opt / ".upgrade" / "state"
        if not path.exists():
            return {}
        return dict(
            line.split("=", 1) for line in path.read_text().splitlines() if "=" in line
        )

    def current(self) -> str:
        return os.readlink(self.opt / "current")

    def running_version(self) -> str:
        """The invariant: ``current`` names one complete version folder."""
        target = (self.opt / "current").resolve(strict=True)
        info = json.loads((target / "BUILDINFO.json").read_text())
        assert (target / "bin" / "meshforge-agent").is_file()
        return str(info["version"])

    def systemctl(self) -> list[str]:
        return self.calls.read_text().splitlines() if self.calls.exists() else []

    def record(self, name: str) -> dict[str, str]:
        return dict(json.loads((self.opt / ".upgrade" / name).read_text()))


@pytest.fixture
def box(tmp_path: Path) -> Box:
    if shutil.which("sh") is None:
        pytest.skip("no sh")
    made = Box(tmp_path)
    made.version(OLD)
    made.point(OLD)
    return made


def _dpkg_unpacks_new(box: Box) -> None:
    """What dpkg's unpack does to the program folder: the new tree, the old
    files deleted (by unlink - the hard links in .previous survive), the
    pointer replaced."""
    box.version(NEW)
    shutil.rmtree(box.opt / OLD)
    box.point(NEW)


# ------------------------------------------------------------------ preinst
def test_begin_keeps_a_hard_link_copy_and_records_the_upgrade(box: Box) -> None:
    box.ok("mf_upgrade_begin")
    kept = box.opt / ".previous" / OLD
    assert (kept / "BUILDINFO.json").is_file()
    original = box.opt / OLD / "bin" / "meshforge-agent"
    assert os.stat(kept / "bin" / "meshforge-agent").st_ino == os.stat(original).st_ino
    state = box.state()
    assert state["from"] == OLD and state["to"] == NEW
    assert state["phase"] == "unpacking"
    assert state["previous"] == f".previous/{OLD}"
    assert state["boot"] == "boot-A"
    _dpkg_unpacks_new(box)
    assert (kept / "bin" / "meshforge-agent").read_text().strip().endswith(OLD)


def test_begin_prunes_to_one_previous_version(box: Box) -> None:
    box.version("0.15.0", parent=box.opt / ".previous")
    (box.opt / ".previous" / ".tmp").mkdir()
    box.ok("mf_upgrade_begin")
    assert sorted(p.name for p in (box.opt / ".previous").iterdir()) == [OLD]


def test_begin_on_a_rolled_back_laptop_keeps_what_runs(box: Box) -> None:
    box.version("0.16.0", parent=box.opt / ".previous")
    box.point(".previous/0.16.0")
    box.ok("mf_upgrade_begin")
    assert box.state()["from"] == "0.16.0"
    assert box.state()["previous"] == ".previous/0.16.0"
    assert sorted(p.name for p in (box.opt / ".previous").iterdir()) == ["0.16.0"]


def test_begin_without_a_running_version_keeps_nothing(box: Box) -> None:
    (box.opt / "current").unlink()
    box.ok("mf_upgrade_begin")
    assert box.state()["from"] == "" and box.state()["previous"] == ""
    assert not (box.opt / ".previous" / OLD).exists()


def test_begin_falls_back_to_a_real_copy_or_warns(box: Box) -> None:
    # cp -al refused (e.g. another file system): a full copy when there is room
    out = box.ok(
        'cp() { if [ "$1" = -al ]; then return 1; fi; command cp "$@"; }\nmf_upgrade_begin'
    )
    assert (box.opt / ".previous" / OLD / "BUILDINFO.json").is_file()
    assert "warning" not in out
    # ...and no room: an honest warning, no rollback copy, no half folder
    shutil.rmtree(box.opt / ".previous")
    out = box.ok(
        'cp() { if [ "$1" = -al ]; then return 1; fi; command cp "$@"; }\n'
        "mf_free_kb() { echo 10; }\nmf_upgrade_begin"
    )
    assert "not enough free disk" in out
    assert "cannot switch back by itself" in out
    assert not (box.opt / ".previous" / OLD).exists()
    assert not (box.opt / ".previous" / ".tmp").exists()
    assert box.state()["previous"] == ""


# ----------------------------------------------------------------- postinst
def test_switch_then_done(box: Box) -> None:
    box.ok("mf_upgrade_begin")
    box.version(NEW)
    box.ok("mf_upgrade_switch")
    assert box.current() == NEW
    assert box.state()["phase"] == "configuring"
    assert box.state()["previous"] == f".previous/{OLD}"
    (box.opt / ".upgrade" / "rolled-back.json").write_text("{}")
    box.ok("mf_upgrade_done")
    assert not (box.opt / ".previous").exists()
    assert box.state() == {}
    assert not (box.opt / ".upgrade" / "rolled-back.json").exists()
    assert box.running_version() == NEW


def test_switch_on_a_fresh_install_creates_no_record(box: Box) -> None:
    box.version(NEW)
    box.ok("mf_upgrade_switch")
    assert box.current() == NEW and box.state() == {}


def test_rollback_points_back_restarts_and_records_why(box: Box) -> None:
    box.ok("mf_upgrade_begin")
    _dpkg_unpacks_new(box)
    box.ok("mf_upgrade_switch")
    systemd = Path("/run/systemd/system").is_dir()
    box.ok(r"""mf_upgrade_rollback 'console said "no" \ crashed'""")
    assert box.current() == f".previous/{OLD}"
    assert box.running_version() == OLD
    record = box.record("rolled-back.json")
    assert record["action"] == "rolled_back"
    assert record["from"] == OLD and record["to"] == NEW
    assert record["reason"] == 'console said "no" \\ crashed'
    assert record["package"] == "meshforge-agent"
    assert box.state()["phase"] == "rolled_back"
    if systemd:  # the functions only call systemctl where systemd runs
        calls = box.systemctl()
        assert calls[0] == "stop meshforge-agent.service"
        assert calls[-1] == "start meshforge-agent.service"
    box.ok("mf_upgrade_rolled_back_ok")
    assert box.state() == {}
    assert (box.opt / ".previous" / OLD).is_dir()  # it is what runs now


def test_rollback_without_restart_only_moves_the_pointer(box: Box) -> None:
    box.ok("mf_upgrade_begin")
    _dpkg_unpacks_new(box)
    box.ok("mf_upgrade_switch")
    box.ok("mf_upgrade_rollback 'identity migration refused' norestart")
    assert box.current() == f".previous/{OLD}"
    assert box.systemctl() == []


def test_rollback_without_a_kept_version_changes_nothing(box: Box) -> None:
    (box.opt / "current").unlink()
    box.ok("mf_upgrade_begin")
    box.version(NEW)
    box.ok("mf_upgrade_switch")
    done = box.sh("mf_upgrade_rollback why")
    assert done.returncode == 1
    assert box.current() == NEW
    assert not (box.opt / ".upgrade" / "rolled-back.json").exists()
    assert box.systemctl() == []


# ------------------------------------------------------- Q38 forced failure
@pytest.mark.parametrize(
    ("content", "matches"),
    [
        (f"{NEW}\n", True),
        ("*\n", True),
        (f" {NEW} \n# why\n", True),
        ("0.17.2\n", False),
        ("", False),
    ],
)
def test_forced_failure_names_a_version(box: Box, content: str, matches: bool) -> None:
    upg = box.opt / ".upgrade"
    upg.mkdir()
    (upg / "fail-upgrade-to").write_text(content)
    done = box.sh("mf_forced_failure")
    assert (done.returncode == 0) is matches


def test_forced_failure_only_from_a_root_owned_regular_file(box: Box) -> None:
    upg = box.opt / ".upgrade"
    upg.mkdir()
    real = box.root / "elsewhere"
    real.write_text(f"{NEW}\n")
    (upg / "fail-upgrade-to").symlink_to(real)
    assert box.sh("mf_forced_failure").returncode == 1
    (upg / "fail-upgrade-to").unlink()
    (upg / "fail-upgrade-to").write_text(f"{NEW}\n")
    (upg / "fail-upgrade-to.not-root").write_text("")
    assert box.sh("mf_forced_failure").returncode == 1


def test_forced_failure_is_one_shot(box: Box) -> None:
    box.ok("mf_upgrade_begin")
    _dpkg_unpacks_new(box)
    box.ok("mf_upgrade_switch")
    (box.opt / ".upgrade" / "fail-upgrade-to").write_text(f"{NEW}\n")
    box.ok("mf_upgrade_rollback forced")
    assert not (box.opt / ".upgrade" / "fail-upgrade-to").exists()


def test_a_trigger_for_another_version_survives_a_rollback(box: Box) -> None:
    box.ok("mf_upgrade_begin")
    _dpkg_unpacks_new(box)
    box.ok("mf_upgrade_switch")
    (box.opt / ".upgrade" / "fail-upgrade-to").write_text("0.18.0\n")
    box.ok("mf_upgrade_rollback other")
    assert (box.opt / ".upgrade" / "fail-upgrade-to").exists()


# ----------------------------------------------------- Q39 half-done upgrade
def test_power_loss_during_the_unpack_reverts_at_the_next_boot(box: Box) -> None:
    box.ok("mf_upgrade_begin")
    _dpkg_unpacks_new(box)  # unpacked, never configured
    box.boot.write_text("boot-B\n")  # the machine came back
    out = box.sh("mf_upgrade_recover")
    assert out.returncode == 0
    assert "interrupted" in out.stderr
    assert box.current() == f".previous/{OLD}"
    assert box.running_version() == OLD  # complete, though dpkg deleted the originals
    record = box.record("interrupted.json")
    assert record["action"] == "reverted" and record["to"] == NEW
    # fix round 1: dpkg is half-installed here; only installing again repairs it
    assert "sudo dpkg -i" in out.stderr
    assert "dpkg --configure -a" not in out.stderr
    assert box.state()["phase"] == "recovered-unpacking"
    # a later boot never acts twice
    box.boot.write_text("boot-C\n")
    box.ok("mf_upgrade_recover")
    assert box.state()["phase"] == "recovered-unpacking"
    # `dpkg --configure -a` (the repair run) then switches to the new version
    box.ok("mf_upgrade_switch")
    assert box.current() == NEW and box.state()["phase"] == "configuring"
    box.ok("mf_upgrade_done")
    assert box.running_version() == NEW
    assert not (box.opt / ".previous").exists()


def test_power_loss_after_the_switch_lets_the_new_version_finish(box: Box) -> None:
    box.ok("mf_upgrade_begin")
    _dpkg_unpacks_new(box)
    box.ok("mf_upgrade_switch")
    box.boot.write_text("boot-B\n")
    out = box.sh("mf_upgrade_recover")
    assert out.returncode == 0
    assert "dpkg --configure -a" in out.stderr  # configure is all that is missing
    assert box.current() == NEW
    assert box.record("interrupted.json")["action"] == "finishing"
    assert box.state()["phase"] == "recovered-configuring"


def test_the_same_boot_is_an_upgrade_in_progress_and_is_left_alone(box: Box) -> None:
    box.ok("mf_upgrade_begin")
    _dpkg_unpacks_new(box)
    box.ok("mf_upgrade_recover")
    assert box.current() == NEW
    assert box.state()["phase"] == "unpacking"
    assert not (box.opt / ".upgrade" / "interrupted.json").exists()


def test_power_loss_without_a_kept_version_starts_what_is_installed(box: Box) -> None:
    (box.opt / "current").unlink()
    box.ok("mf_upgrade_begin")
    box.version(NEW)
    box.point(NEW)
    box.boot.write_text("boot-B\n")
    out = box.sh("mf_upgrade_recover")
    assert "sudo dpkg -i" in out.stderr
    assert box.current() == NEW
    assert box.record("interrupted.json")["action"] == "incomplete"


def test_recover_without_a_record_does_nothing(box: Box) -> None:
    box.ok("mf_upgrade_recover")
    assert box.current() == OLD and not (box.opt / ".upgrade").exists()


# ----------------------------------------------------------------- remove
def test_forget_removes_every_trace(box: Box) -> None:
    box.ok("mf_upgrade_begin")
    (box.opt / ".current.123").symlink_to(OLD)
    box.ok("mf_upgrade_forget")
    assert not (box.opt / ".previous").exists()
    assert not (box.opt / ".upgrade").exists()
    assert not (box.opt / ".current.123").is_symlink()
    assert box.current() == OLD  # dpkg's own files are dpkg's


def test_the_pointer_swap_never_leaves_a_tmp_link(box: Box) -> None:
    box.version(NEW)
    box.ok("mf_point_at 0.17.1")
    assert box.current() == NEW
    assert [p.name for p in box.opt.iterdir() if p.name.startswith(".current")] == []


# ------------------------------------------------------- build-time inlining
def test_expand_inlines_the_library_and_fills_the_version(tmp_path: Path) -> None:
    common = tmp_path / "common"
    common.mkdir()
    (common / "lib.sh").write_text("lib_fn() { :; }")
    text = "#!/bin/sh\nV=@MF_VERSION@\n# @meshforge-include lib.sh\nlib_fn\n"
    out = build_deb.expand_script(text, {"MF_VERSION": "0.17.1"}, common=common)
    assert "V=0.17.1\n" in out
    assert "lib_fn() { :; }\n# ---- end lib.sh ----\nlib_fn\n" in out
    assert "@meshforge-include" not in out


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("# @meshforge-include nope.sh\n", "not in"),
        ("x=@MF_NOPE@\n", "unknown token"),
        ("# @meshforge-include nested.sh\n", "nested"),
    ],
)
def test_expand_refuses_holes(tmp_path: Path, text: str, match: str) -> None:
    common = tmp_path / "common"
    common.mkdir()
    (common / "nested.sh").write_text("# @meshforge-include other.sh\n")
    # build_deb's own BundleError: other tests may load scripts/ under other
    # module names, so the class imported here can be a different object
    with pytest.raises(build_deb.BundleError, match=match):
        build_deb.expand_script(text, {"MF_VERSION": "1"}, common=common)


@pytest.mark.parametrize("role", ["agent", "controller"])
def test_staged_scripts_are_complete_posix_sh(tmp_path: Path, role: str) -> None:
    spec = build_deb.SPECS[role]
    build_deb.stage(
        tmp_path, spec.packaging, b"x", 0, spec.staged, {"MF_VERSION": "0.17.1"}
    )
    scripts = [
        tmp_path / rel
        for rel in spec.staged
        if rel.startswith("scripts/") or rel == "upgrade-recover"
    ]
    assert len(scripts) == 5
    for path in scripts:
        text = path.read_text()
        assert "@MF_" not in text and "@meshforge-include" not in text, path
        assert path.stat().st_mode & 0o111
        if shutil.which("sh"):
            done = subprocess.run(
                ["sh", "-n", str(path)], capture_output=True, check=False
            )
            assert done.returncode == 0, (path, done.stderr)
        checker = shutil.which("shellcheck")
        if checker is not None:
            done = subprocess.run(
                [checker, "-s", "sh", "-S", "warning", str(path)],
                capture_output=True, text=True, check=False,
            )  # fmt: skip
            assert done.returncode == 0, done.stdout
    preinst = (tmp_path / "scripts" / "preinst").read_text()
    assert "MF_VERSION=0.17.1\n" in preinst and "mf_upgrade_begin()" in preinst
    unit = next(rel for rel in spec.staged if rel.endswith(".service"))
    recover = "agent" if role == "agent" else "controller"
    assert (
        f"ExecStartPre=-+/usr/lib/meshforge/{recover}-upgrade-recover"
        in (tmp_path / unit).read_text()
    )
