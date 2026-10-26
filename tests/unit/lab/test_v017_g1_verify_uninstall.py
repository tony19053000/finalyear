"""V0.17 G1: ``lab/verify_uninstall.py`` (D393, criterion 2) - the verdict rules.

The probe's facts -> LEFT / kept / note. ``remove`` may keep data, the service
account and dpkg's ``config-files`` state; ``purge`` (``--data-removed``) may
not. A unit systemd still lists (e.g. left ``failed``), a process, a program
file, a unit file or a ufw rule is always a leftover. Running the real probe on
a machine with the package is tests/component/packaging (slow).
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parents[3]

pytestmark = pytest.mark.unit


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "verify_uninstall", REPO / "lab" / "verify_uninstall.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


vu = _load()


def test_nothing_found_is_clean() -> None:
    verdict = vu.evaluate([("dpkg", "absent"), ("ufw", "absent")], data_removed=True)
    assert verdict.clean and verdict.leftovers == [] and verdict.kept == []


@pytest.mark.parametrize(
    ("fact", "says"),
    [
        (("program", "/opt/meshforge"), "program file left: /opt/meshforge"),
        (
            ("unit_file", "/etc/systemd/system/x.wants/meshforge-agent.service"),
            "unit file",
        ),
        (("unit_loaded", "meshforge-agent.service not-found failed"), "still knows"),
        (("unit_known", "meshforge-agent.path enabled"), "still knows"),
        (("process", "123 /opt/meshforge/current/python/bin/python3.12"), "process"),
        (("ufw_profile", "/etc/ufw/applications.d/meshforge"), "ufw app profile"),
        (("ufw_rule", "47120/tcp ALLOW Anywhere # MeshForge"), "ufw rule"),
        (("dpkg", "installed"), "dpkg package state: installed"),
        (("dpkg", "half-configured"), "dpkg package state"),
    ],
)
@pytest.mark.parametrize("data_removed", [False, True])
def test_always_leftovers(fact: tuple[str, str], says: str, data_removed: bool) -> None:
    verdict = vu.evaluate([fact], data_removed=data_removed)
    assert not verdict.clean
    assert says in verdict.leftovers[0]


def test_remove_keeps_data_accounts_and_config_files_state() -> None:
    facts = [
        ("data", "/var/lib/meshforge"),
        ("data", "/etc/meshforge"),
        ("account", "passwd:meshforge"),
        ("dpkg", "config-files"),
        ("user_unit", "/home/a/.config/systemd/user/meshforge-agent.service"),
    ]
    kept = vu.evaluate(facts, data_removed=False)
    assert kept.clean
    assert len(kept.kept) == 5
    purged = vu.evaluate(facts, data_removed=True)
    assert [line.split(":")[0] for line in purged.leftovers] == [
        "data left after purge",
        "data left after purge",
        "account left after purge",
        "dpkg package state",
    ]
    # a developer's old user unit file is never ours to delete (D387)
    assert any("user unit" in line for line in purged.kept)


def test_unchecked_is_a_note_never_a_pass_claim() -> None:
    verdict = vu.evaluate(
        [("ufw", "not-root"), ("systemd", "absent")], data_removed=False
    )
    assert verdict.clean
    assert len(verdict.notes) == 2
    assert "not checked" in vu.render(verdict)


def test_parse_ignores_noise() -> None:
    assert vu.parse_probe("program\t/opt/meshforge\nnoise\n\nufw\tabsent \n") == [
        ("program", "/opt/meshforge"),
        ("ufw", "absent"),
    ]


def test_render_says_clean_or_counts() -> None:
    assert vu.render(vu.evaluate([], data_removed=False)).endswith(
        "CLEAN: nothing left behind"
    )
    text = vu.render(vu.evaluate([("program", "/opt/meshforge")], data_removed=False))
    assert "LEFT   program file left: /opt/meshforge" in text
    assert text.endswith("NOT CLEAN: 1 item(s) left behind")


def test_probe_script_is_filled_and_valid_sh() -> None:
    script = vu.probe_script()
    assert "__" not in script.replace("__file__", "")
    assert script.startswith("# verify_uninstall probe\n")
    for path in (*vu.PROGRAM_PATHS, *vu.UNIT_PATHS, *vu.DATA_PATHS):
        assert path in script
    sh = shutil.which("sh")
    if sh is None:
        pytest.skip("no sh here")
    done = subprocess.run([sh, "-n", "-c", script], capture_output=True, check=False)
    assert done.returncode == 0, done.stderr


@pytest.mark.skipif(sys.platform != "linux", reason="the probe is POSIX sh on Linux")
def test_probe_runs_here_and_never_lists_itself() -> None:
    facts = vu.parse_probe(vu.run_probe(None))
    kinds = {kind for kind, _ in facts}
    assert "dpkg" in kinds and "ufw" in kinds
    assert not any(
        "verify_uninstall" in value for kind, value in facts if kind == "process"
    )


@pytest.mark.skipif(sys.platform == "win32", reason="the probe runs for real there")
def test_windows_checks_run_only_on_windows(capsys: pytest.CaptureFixture[str]) -> None:
    # V0.17 G4: the Windows checks exist; elsewhere --os windows is a usage error
    assert vu.main(["--os", "windows"]) == vu.EXIT_USAGE
    assert "runs on Windows" in capsys.readouterr().err
    assert vu.main(["--os", "plan9"]) == vu.EXIT_USAGE
