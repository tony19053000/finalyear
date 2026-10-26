"""V0.17 G5 tester (Q38, brief §5e item 2): the forced-failure trigger cannot be
set by job code or by the service account.

Threat model: job code runs as the ``meshforge`` service user (Linux) or the
service's virtual account (Windows) and can write the data folders. If it could
make the next upgrade "fail", it could force a rollback to an older version
(a downgrade attack on a patched flaw) - so:

* the trigger is read ONLY from the root/Administrators-only program folder;
* no configuration key, environment variable or data-folder file can name it;
* a trigger file that is a symlink, not a regular file, or (POSIX) not owned
  by root is ignored by every reader (shell library, controller, Windows setup).
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Final

import pytest

pytestmark = pytest.mark.unit

REPO: Final = Path(__file__).resolve().parents[2]
SRC: Final = REPO / "src" / "meshforge"


def test_only_the_three_known_readers_know_the_trigger_name() -> None:
    """If a new module starts reading it (a settings field, an env var, a data
    folder path) this fails and a human re-does the threat model."""
    readers = set()
    for path in SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "fail-upgrade-to" in text or "FORCED_FAILURE_NAME" in text:
            readers.add(path.relative_to(SRC).as_posix())
    assert readers == {
        "common/upgrade_state.py",
        "agent/winupgrade.py",
    }, readers


def test_callers_read_it_only_next_to_current_never_from_the_data_dir() -> None:
    server = (SRC / "controller/server.py").read_text(encoding="utf-8")
    assert "forced_failure(program_dir(root), version)" in server
    winsetup = (SRC / "agent/winsetup.py").read_text(encoding="utf-8")
    assert "run.records.forced_failure(" in winsetup
    # the record folder is built from the PROGRAM root, never ProgramData
    for path in (SRC / "agent").glob("win*.py"):
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"FileRecords\(([^)]*)\)", text):
            assert "data" not in match.group(1).lower(), (path.name, match.group(0))


def test_no_setting_or_environment_variable_names_the_trigger() -> None:
    for path in SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for needle in (
            "FAIL_UPGRADE",
            "FORCE_UPGRADE_FAIL",
            "fail_upgrade",
            "forced_failure_file",
        ):
            assert needle not in text, f"{path}: {needle}"
    settings = (SRC / "controller/settings.py").read_text(encoding="utf-8")
    assert "fail-upgrade" not in settings


def test_the_service_units_cannot_write_the_program_folder() -> None:
    for unit in (
        REPO / "packaging/linux/meshforge-agent.service",
        REPO / "packaging/linux/controller/meshforge-controller.service",
    ):
        text = unit.read_text(encoding="utf-8")
        assert "ProtectSystem=strict" in text, unit.name
        paths = re.findall(r"^ReadWritePaths=(.+)$", text, re.MULTILINE)
        assert paths, unit.name
        for line in paths:
            for item in line.split():
                assert "/opt" not in item, f"{unit.name} may write {item}"
        # and the unit file itself is root's: no User= root escalation for jobs
        assert re.search(r"^User=meshforge$", text, re.MULTILINE)


def test_the_installer_gives_the_service_no_rights_in_the_program_folder() -> None:
    iss = (REPO / "packaging/windows/meshforge-agent.iss").read_text(encoding="utf-8")
    for line in iss.splitlines():
        if "{app}" in line and re.search(
            r"Permissions|icacls|grant", line, re.IGNORECASE
        ):
            pytest.fail(f"the installer changes rights under {{app}}: {line}")


@pytest.mark.skipif(
    sys.platform == "win32" or os.geteuid() == 0,
    reason="needs a POSIX, non-root test user",
)
def test_a_trigger_written_by_a_non_root_user_is_ignored_by_the_python_reader(
    tmp_path: Path,
) -> None:
    from meshforge.common.upgrade_state import forced_failure, upgrade_dir

    program = tmp_path / "meshforge"
    folder = upgrade_dir(program)
    folder.mkdir(parents=True)
    (folder / "fail-upgrade-to").write_text("*\n")  # owned by the test user, not root
    assert forced_failure(program, "0.17.1") is None
    # the lab/CI helper (require_root=False) is the only way it counts
    assert forced_failure(program, "0.17.1", require_root=False) is not None


def test_a_symlinked_trigger_is_ignored_by_the_python_reader(tmp_path: Path) -> None:
    from meshforge.common.upgrade_state import forced_failure, upgrade_dir

    program = tmp_path / "meshforge"
    folder = upgrade_dir(program)
    folder.mkdir(parents=True)
    real = tmp_path / "real"
    real.write_text("*\n")
    try:
        (folder / "fail-upgrade-to").symlink_to(real)
    except OSError:
        pytest.skip("symlinks not permitted")
    assert forced_failure(program, "0.17.1", require_root=False) is None


def test_the_no_root_check_is_not_reachable_from_production_callers() -> None:
    """``require_root=False`` exists for tests; no shipped caller may pass it."""
    for path in SRC.rglob("*.py"):
        if path.name == "upgrade_state.py":
            continue
        assert "require_root=False" not in path.read_text(encoding="utf-8"), path
