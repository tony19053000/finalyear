"""V1.0 G3 tester: the controller prerm's firewall cleanup (D389) with a fake ufw.

Runs the real ``packaging/linux/controller/scripts/prerm`` under ``sh`` with
stub ``ufw``/``systemctl`` first on PATH and its absolute paths rewritten into a
temp dir (so nothing on the host is touched). Cases: ufw active/inactive/absent,
rule listed or not, ``ufw`` failing, ``delete`` failing, upgrade vs remove.
"""

from __future__ import annotations

import shutil
import stat
import subprocess
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.unit,
    pytest.mark.security,
    pytest.mark.skipif(shutil.which("sh") is None, reason="needs POSIX sh"),
]

PRERM = Path(__file__).resolve().parents[2] / "packaging/linux/controller/scripts/prerm"
RULE = "MeshForge Controller"


def stub(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def run_prerm(
    tmp: Path,
    action: str,
    *,
    ufw: str | None,
    ufw_status: str = "Status: inactive",
    added: str = "",
    added_rc: int = 0,
    delete_rc: int = 0,
) -> tuple[int, list[str]]:
    bindir = tmp / "bin"
    bindir.mkdir(exist_ok=True)
    log = tmp / "calls.log"
    log.write_text("")
    stub(bindir / "systemctl", f'echo "systemctl $*" >> "{log}"\nexit 0\n')
    if ufw is not None:
        stub(
            bindir / "ufw",
            f'''echo "ufw $*" >> "{log}"
case "$1 $2" in
  "status ") echo "{ufw_status}"; exit 0;;
  "show added") printf '%s\\n' "{added}"; exit {added_rc};;
  "delete allow") exit {delete_rc};;
esac
case "$1" in status) echo "{ufw_status}";; esac
exit 0
''',
        )
    state = tmp / "var-lib" / "controller"
    state.mkdir(parents=True, exist_ok=True)
    opt = tmp / "opt" / "controller"
    opt.mkdir(parents=True, exist_ok=True)
    text = (
        PRERM.read_text(encoding="utf-8")
        .replace("/var/lib/meshforge/controller", str(state))
        .replace("/opt/meshforge/controller", str(opt))
        .replace("/run/systemd/system", str(tmp / "no-systemd"))
    )
    script = tmp / "prerm"
    script.write_text(text, encoding="utf-8")
    # dash/sh with only our stubs plus coreutils
    env = {
        "PATH": f"{bindir}:/usr/bin:/bin"
        if ufw is not None
        else f"{bindir}:{_clean_path(tmp)}"
    }
    done = subprocess.run(
        ["/bin/sh", str(script), action],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return done.returncode, log.read_text().splitlines()


def _clean_path(tmp: Path) -> str:
    """A PATH with coreutils but no ufw (the host may have /usr/sbin/ufw)."""
    safe = tmp / "safebin"
    safe.mkdir(exist_ok=True)
    for tool in ("grep", "rm", "sh", "env"):
        found = shutil.which(tool)
        if found and not (safe / tool).exists():
            (safe / tool).symlink_to(found)
    return str(safe)


RULE_LINE = f"ufw allow '{RULE}'"


def test_remove_with_ufw_off_but_rule_present_deletes_it(tmp_path: Path) -> None:
    rc, calls = run_prerm(
        tmp_path, "remove", ufw="y", ufw_status="Status: inactive", added=RULE_LINE
    )
    assert rc == 0
    assert f"ufw delete allow {RULE}" in calls


def test_remove_with_ufw_active_deletes_it(tmp_path: Path) -> None:
    rc, calls = run_prerm(
        tmp_path, "remove", ufw="y", ufw_status="Status: active", added=RULE_LINE
    )
    assert rc == 0 and f"ufw delete allow {RULE}" in calls


def test_remove_without_our_rule_never_deletes(tmp_path: Path) -> None:
    rc, calls = run_prerm(tmp_path, "remove", ufw="y", added="ufw allow 'OpenSSH'")
    assert rc == 0 and not any(c.startswith("ufw delete") for c in calls)


def test_remove_with_ufw_absent_succeeds(tmp_path: Path) -> None:
    rc, calls = run_prerm(tmp_path, "remove", ufw=None)
    assert rc == 0
    assert not any(c.startswith("ufw") for c in calls)


def test_remove_with_ufw_show_added_failing_still_succeeds(tmp_path: Path) -> None:
    """Unprivileged/broken ufw must not abort the package removal (set -e)."""
    rc, _ = run_prerm(tmp_path, "remove", ufw="y", added="", added_rc=1)
    assert rc == 0


def test_remove_with_delete_failing_still_succeeds(tmp_path: Path) -> None:
    rc, calls = run_prerm(tmp_path, "remove", ufw="y", added=RULE_LINE, delete_rc=1)
    assert rc == 0 and f"ufw delete allow {RULE}" in calls


def test_deconfigure_also_removes_the_rule(tmp_path: Path) -> None:
    rc, calls = run_prerm(tmp_path, "deconfigure", ufw="y", added=RULE_LINE)
    assert rc == 0 and f"ufw delete allow {RULE}" in calls


@pytest.mark.parametrize("action", ["upgrade", "failed-upgrade", "bogus"])
def test_upgrade_keeps_the_rule(action: str, tmp_path: Path) -> None:
    rc, calls = run_prerm(tmp_path, action, ufw="y", added=RULE_LINE)
    assert rc == 0 and not any(c.startswith("ufw") for c in calls)


def test_no_action_argument_is_harmless(tmp_path: Path) -> None:
    rc, calls = run_prerm(tmp_path, "", ufw="y", added=RULE_LINE)
    assert rc == 0 and not any(c.startswith("ufw delete") for c in calls)


def test_prerm_never_depends_on_ufw_status_output(tmp_path: Path) -> None:
    """Regression for the M5 bug: `ufw status` hides rules while ufw is off."""
    rc, calls = run_prerm(
        tmp_path, "remove", ufw="y", ufw_status="Status: inactive", added=RULE_LINE
    )
    assert rc == 0 and any(c.startswith("ufw delete") for c in calls)
    assert (
        PRERM.read_text(encoding="utf-8").count("ufw status") == 0
        or "show added" in PRERM.read_text()
    )


def test_postinst_only_adds_when_ufw_active() -> None:
    post = (PRERM.parent / "postinst").read_text(encoding="utf-8")
    assert "Status: active" in post and "ufw enable" not in post
