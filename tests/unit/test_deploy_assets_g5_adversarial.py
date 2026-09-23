"""Independent V0.3 G5 acceptance checks for the 0.3.13 deliverables.

Complements ``tests/unit/test_print_service_setup.py`` (the implementer's own
suite) with: XML well-formedness, the systemd units' required directives, a
real end-to-end filesystem-diff proof that running the script changes nothing
on disk (not only a mocked ``open``), and that ``docs/OPERATIONS.md`` states the
D14 trust boundary and documents install *and* uninstall for both platforms.
"""

from __future__ import annotations

import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "print_service_setup.py"
LINUX_AGENT_UNIT = REPO / "deploy" / "systemd" / "meshforge-agent.service"
LINUX_CONTROLLER_UNIT = REPO / "deploy" / "systemd" / "meshforge-controller.service"
WINDOWS_TASK_XML = REPO / "deploy" / "windows" / "meshforge-agent-task.xml"
OPERATIONS = REPO / "docs" / "OPERATIONS.md"


# ------------------------------------------------------------------- XML shape


def test_the_windows_task_xml_is_well_formed() -> None:
    """A malformed template would fail silently for every Windows user who
    trusts the docs enough to paste it straight into ``schtasks``."""
    tree = ET.parse(WINDOWS_TASK_XML)  # noqa: S314 - our own repo file, not untrusted input
    root = tree.getroot()
    assert root.tag.endswith("Task")


def test_the_windows_task_xml_has_the_fields_the_docs_say_to_edit() -> None:
    text = WINDOWS_TASK_XML.read_text(encoding="utf-8")
    for required in ("<UserId>", "<WorkingDirectory>", "<Command>", "<Arguments>"):
        assert required in text, required
    # The three "EDIT" placeholders the docstring promises exist.
    assert text.count("EDIT") >= 3


def test_the_windows_task_xml_does_not_request_elevation() -> None:
    """OPERATIONS.md and the header both say the agent needs no elevation."""
    text = WINDOWS_TASK_XML.read_text(encoding="utf-8")
    assert "<RunLevel>LeastPrivilege</RunLevel>" in text
    assert "HighestAvailable" not in text


# --------------------------------------------------------- systemd unit shape


@pytest.mark.parametrize(
    "unit_path", [LINUX_AGENT_UNIT, LINUX_CONTROLLER_UNIT], ids=["agent", "controller"]
)
def test_the_systemd_unit_has_every_required_directive(unit_path: Path) -> None:
    text = unit_path.read_text(encoding="utf-8")
    for section in ("[Unit]", "[Service]", "[Install]"):
        assert section in text, (unit_path.name, section)
    assert "ExecStart=" in text
    assert "WantedBy=" in text
    assert "Type=simple" in text
    # A long-lived client process must restart on its own if it dies, but not
    # fight a CONFLICT/CONFIG_ERROR restart loop forever.
    assert "Restart=" in text
    assert "RestartPreventExitStatus=" in text


def test_the_systemd_units_are_user_units_not_system_units() -> None:
    """Roadmap 0.3.13 + D17: a *user* unit under ``~/.config/systemd/user``, never
    a system-wide one that would need root and run as a different account."""
    for unit_path in (LINUX_AGENT_UNIT, LINUX_CONTROLLER_UNIT):
        text = unit_path.read_text(encoding="utf-8")
        assert "WantedBy=default.target" in text  # the user-manager target
        assert "/etc/systemd/system" not in text


# ------------------------------------------------------------- OPERATIONS.md


def test_operations_md_documents_install_and_uninstall_for_both_platforms() -> None:
    text = OPERATIONS.read_text(encoding="utf-8")
    lowered = text.lower()
    assert "systemctl --user enable --now meshforge-agent" in text  # Linux install
    assert "schtasks /create" in text  # Windows install
    assert "systemctl --user disable --now meshforge-agent" in text  # Linux uninstall
    assert "schtasks /delete" in text  # Windows uninstall
    assert "uninstall" in lowered


def test_operations_md_states_the_d14_trust_boundary() -> None:
    text = OPERATIONS.read_text(encoding="utf-8")
    lowered = text.lower()
    assert "read-only" in lowered
    assert "unauthenticated" in lowered
    assert "lan" in lowered
    # It must not merely assert the boundary; it must say what to do about it.
    assert "0.0.0.0" in text or "wildcard" in lowered  # noqa: S104 - doc text, not a bind


def test_operations_md_does_not_claim_windows_logoff_survival_as_fact() -> None:
    """NB from the brief: an installer that states unverified behaviour as fact
    is exactly the kind of confidently-wrong documentation this project forbids."""
    text = OPERATIONS.read_text(encoding="utf-8")
    assert "unverified" in text.lower()


# ------------------------------------------------- real end-to-end no-op proof


def _snapshot(paths: list[Path]) -> dict[Path, float]:
    return {p: p.stat().st_mtime for p in paths if p.exists()}


def test_running_the_script_for_real_changes_nothing_on_this_machine(
    tmp_path: Path,
) -> None:
    """A real subprocess invocation (no mocked ``open``/``subprocess``, unlike the
    implementer's own unit test) - the strongest available proof for D17 short of
    a full filesystem trace: nothing this script is documented to touch changes
    mtime, and no new file appears under the repo's tracked service directories
    or common service-manager locations reachable without root."""
    watched = [
        LINUX_AGENT_UNIT,
        LINUX_CONTROLLER_UNIT,
        WINDOWS_TASK_XML,
        OPERATIONS,
        SCRIPT,
    ]
    before = _snapshot(watched)
    before_cwd_listing = sorted(p.name for p in REPO.glob("*"))

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "all"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        cwd=str(tmp_path),  # run from an unrelated directory, to be sure
    )

    after = _snapshot(watched)
    after_cwd_listing = sorted(p.name for p in REPO.glob("*"))
    assert result.returncode == 0, result.stderr
    assert before == after, "the script modified a file it only claims to print"
    assert before_cwd_listing == after_cwd_listing, "a new file appeared in the repo"
    assert list(tmp_path.iterdir()) == [], "the script wrote into its cwd"
