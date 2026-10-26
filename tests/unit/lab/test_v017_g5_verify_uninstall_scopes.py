"""V0.17 G5 (brief §5e item 5): ``lab/verify_uninstall.py --package agent|controller``
- one of two packages removed from a shared machine - and the upgrade leftovers
(``.previous``, ``.upgrade``) as program files.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parents[3]

pytestmark = pytest.mark.unit


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "verify_uninstall_g5", REPO / "lab" / "verify_uninstall.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


vu = _load()

CONTROLLER_STAYS = [
    ("program", "/opt/meshforge"),
    ("program", "/usr/lib/meshforge"),
    ("program", "/usr/bin/meshforge-controller"),
    ("program_entry", "/opt/meshforge/controller"),
    ("program_entry", "/usr/lib/meshforge/controller-upgrade-recover"),
    ("unit_file", "/usr/lib/systemd/system/meshforge-controller.service"),
    ("unit_loaded", "meshforge-controller.service loaded active running"),
    ("process", "812 /opt/meshforge/controller/0.17.1/pgsql/bin/postgres -D x"),
    ("ufw_profile", "/etc/ufw/applications.d/meshforge-controller"),
    ("data", "/var/lib/meshforge"),
    ("data", "/etc/meshforge"),
    ("data_entry", "/var/lib/meshforge/controller"),
    ("data_entry", "/var/lib/meshforge/db"),
    ("data_entry", "/etc/meshforge/controller.toml"),
    ("account", "passwd:meshforge"),
    ("account", "group:meshforge"),
    ("dpkg", "not-installed"),
    ("dpkg_controller", "installed"),
    ("ufw", "checked"),
]


def test_agent_purged_next_to_a_controller_is_clean() -> None:
    verdict = vu.evaluate(CONTROLLER_STAYS, data_removed=True, package="agent")
    assert verdict.clean, verdict.leftovers
    assert verdict.package == "agent"
    assert "package agent" in vu.render(verdict)


@pytest.mark.parametrize(
    ("fact", "says"),
    [
        (("program_entry", "/opt/meshforge/.previous"), "program file left"),
        (("program_entry", "/opt/meshforge/.upgrade"), "program file left"),
        (("program_entry", "/opt/meshforge/current"), "program file left"),
        (
            ("program_entry", "/usr/lib/meshforge/agent-upgrade-recover"),
            "program file left",
        ),
        (("program", "/usr/bin/compute"), "program file left"),
        (("data_entry", "/var/lib/meshforge/identity"), "data left after purge"),
        (("data_entry", "/etc/meshforge/config.toml"), "data left after purge"),
        (("account", "group:meshforge-admin"), "account left"),
        (
            ("process", "77 /opt/meshforge/current/python/bin/python3.12 -m x"),
            "process",
        ),
        (("unit_loaded", "meshforge-agent.service not-found failed"), "still knows"),
    ],
)
def test_agent_leftovers_are_found_next_to_a_controller(
    fact: tuple[str, str], says: str
) -> None:
    verdict = vu.evaluate([*CONTROLLER_STAYS, fact], data_removed=True, package="agent")
    assert len(verdict.leftovers) == 1, verdict.leftovers
    assert says in verdict.leftovers[0]


AGENT_STAYS = [
    ("program", "/opt/meshforge"),
    ("program", "/usr/lib/meshforge"),
    ("program", "/usr/bin/meshforge-agent"),
    ("program", "/usr/bin/compute"),
    ("program_entry", "/opt/meshforge/0.17.1"),
    ("program_entry", "/opt/meshforge/current"),
    ("program_entry", "/usr/lib/meshforge/agent-upgrade-recover"),
    ("unit_loaded", "meshforge-agent.service loaded active running"),
    ("process", "90 /opt/meshforge/current/python/bin/python3.12 -I x run"),
    ("data", "/var/lib/meshforge"),
    ("data", "/etc/meshforge"),
    ("data_entry", "/var/lib/meshforge/identity"),
    ("account", "passwd:meshforge"),
    ("account", "group:meshforge-admin"),
    ("dpkg", "installed"),
    ("dpkg_controller", "config-files"),
]


def test_controller_removed_next_to_an_agent_keep_data() -> None:
    facts = [*AGENT_STAYS, ("data_entry", "/var/lib/meshforge/db")]
    verdict = vu.evaluate(facts, data_removed=False, package="controller")
    assert verdict.clean, verdict.leftovers
    assert any("/var/lib/meshforge/db" in line for line in verdict.kept)
    purged = vu.evaluate(facts, data_removed=True, package="controller")
    assert sorted(purged.leftovers) == [
        "data left after purge: /var/lib/meshforge/db",
        "dpkg package state: config-files",
    ]


@pytest.mark.parametrize(
    "fact",
    [
        ("program_entry", "/opt/meshforge/controller"),
        ("program", "/usr/bin/meshforge-controller"),
        ("process", "5 /opt/meshforge/controller/0.17.1/pgsql/bin/postgres"),
        ("ufw_profile", "/etc/ufw/applications.d/meshforge-controller"),
    ],
)
def test_controller_leftovers_are_found_next_to_an_agent(fact: tuple[str, str]) -> None:
    verdict = vu.evaluate(
        [*AGENT_STAYS, fact], data_removed=False, package="controller"
    )
    assert len(verdict.leftovers) == 1, verdict.leftovers


def test_last_package_gone_the_shared_folders_and_user_must_go() -> None:
    facts = [
        ("program", "/opt/meshforge"),
        ("data", "/var/lib/meshforge"),
        ("account", "passwd:meshforge"),
        ("dpkg", "not-installed"),
        ("dpkg_controller", "not-installed"),
    ]
    verdict = vu.evaluate(facts, data_removed=True, package="agent")
    assert verdict.leftovers == [
        "program file left: /opt/meshforge",
        "data left after purge: /var/lib/meshforge",
        "account left after purge: passwd:meshforge",
    ]


def test_all_scope_ignores_entries_and_judges_the_folders() -> None:
    facts = [
        ("program_entry", "/opt/meshforge/.previous"),
        ("dpkg_controller", "absent"),
    ]
    assert vu.evaluate(facts, data_removed=False).clean
    left = vu.evaluate([("dpkg_controller", "installed")], data_removed=False)
    assert left.leftovers == ["dpkg package state: installed"]


def test_notes_are_kept_in_every_scope() -> None:
    for package in vu.PACKAGE_SCOPES:
        verdict = vu.evaluate(
            [("ufw", "not-root"), ("systemd", "absent")],
            data_removed=False,
            package=package,
        )
        assert len(verdict.notes) == 2, package


def test_probe_lists_entries_and_the_controller_state() -> None:
    script = vu.probe_script()
    assert "program_entry" in script and "data_entry" in script
    assert "meshforge-controller" in script and "dpkg_controller" in script
    assert "__CONTROLLER_PACKAGE__" not in script


def test_cli_accepts_package_and_json(capsys: pytest.CaptureFixture[str]) -> None:
    assert vu.main(["--os", "linux", "--package", "nope"]) == vu.EXIT_USAGE
    capsys.readouterr()
