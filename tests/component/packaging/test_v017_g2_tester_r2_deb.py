"""V0.17 G2 tester round 2: the installed controller .deb - token hygiene and the
safe ``db restore`` on the real packaged service (systemd container).

Slow. ``MESHFORGE_TEST_CONTROLLER_DEB_DIR`` = a folder with the controller debs.
"""

from __future__ import annotations

# ruff: noqa: E501
import json
import sys
from collections.abc import Iterator

import pytest
from mf_deb import Debs, SystemdBox, built_controller_debs, systemd_image

pytestmark = [
    pytest.mark.docker,
    pytest.mark.slow,
    pytest.mark.timeout(3600),
    pytest.mark.skipif(sys.platform != "linux", reason="Linux package"),
]

ROOT = "/var/lib/meshforge/db"


@pytest.fixture(scope="module")
def debs(tmp_path_factory: pytest.TempPathFactory) -> Debs:
    return built_controller_debs(tmp_path_factory.mktemp("g2r2-debs"))


@pytest.fixture(scope="module")
def box(debs: Debs) -> Iterator[SystemdBox]:
    b = SystemdBox.start(systemd_image("24.04"), name_hint="g2r2")
    try:
        done = b.install(debs.first)
        assert done.returncode == 0, done.stdout[-4000:]
        b.install_out = done.stdout  # type: ignore[attr-defined]
        yield b
    finally:
        b.remove()


def _token(b: SystemdBox) -> str:
    url = next(
        line.split()[-1]
        for line in b.ok("meshforge-controller setup url").splitlines()
        if "/setup#token=" in line
    )
    return url.split("#token=", 1)[1]


def _wait_up(b: SystemdBox) -> None:
    """Active and (pre-setup) answering: `setup url` works only while setup is open."""
    b.ok(
        "for i in $(seq 120); do systemctl is-active meshforge-controller >/dev/null && "
        "meshforge-controller setup url >/dev/null 2>&1 && exit 0; sleep 1; done; exit 1"
    )


def test_01_token_is_nowhere_outside_its_file(box: SystemdBox, debs: Debs) -> None:
    token = _token(box)
    assert len(token) >= 20
    assert token not in box.install_out  # type: ignore[attr-defined]
    # every log place a maintainer script or the service could write to
    hits = box.run(
        f"grep -rIl --exclude-dir=proc -F '{token}' /var/log /var/cache /tmp /etc "
        "2>/dev/null; true"
    ).stdout.split()
    assert hits == [], f"setup token found in {hits}"
    journal = box.run("journalctl --no-pager -o cat 2>/dev/null").stdout
    assert token not in journal
    # the second package revision (an upgrade over the first) prints nothing either
    up = box.install(debs.second)
    assert up.returncode == 0, up.stdout[-3000:]
    assert token not in up.stdout and "#token=" not in up.stdout
    assert box.run("grep -rIlF '#token=' /var/log 2>/dev/null").stdout.strip() == ""
    assert _token(box) == token or _token(box)  # still obtainable after upgrade
    # not in any process's argv or environment visible to others
    ps = box.run("ps -eo args").stdout
    assert token not in ps


def test_02_garbage_and_truncated_restore_leave_the_packaged_db_untouched(
    box: SystemdBox,
) -> None:
    box.ok("systemctl stop meshforge-controller")
    ok = box.run("meshforge-controller db backup --out /tmp/good.dump")
    assert ok.returncode == 0, ok.stdout + ok.stderr
    box.ok(
        "head -c 3000 /tmp/good.dump > /tmp/trunc.dump; echo junk > /tmp/junk.dump; : > /tmp/empty.dump"
    )
    for f in ("junk", "trunc", "empty"):
        r = box.run(f"meshforge-controller db restore /tmp/{f}.dump --yes 2>&1")
        assert r.returncode != 0, f
        assert "Traceback" not in r.stdout, r.stdout[-1500:]
        assert (
            "current database was not changed" in r.stdout or "pg_restore" in r.stdout
        )
    assert box.run("pgrep -u meshforge -x postgres").stdout.strip() == ""
    st = box.run("meshforge-controller db status")
    assert st.returncode == 0, st.stdout + st.stderr
    box.ok("systemctl start meshforge-controller")
    _wait_up(box)


def test_03_restore_good_dump_twice_then_service_comes_back(box: SystemdBox) -> None:
    box.ok("systemctl stop meshforge-controller")
    for _ in range(2):
        r = box.run("meshforge-controller db restore /tmp/good.dump --yes 2>&1")
        assert r.returncode == 0, r.stdout[-2000:]
    leftovers = box.run("pgrep -u meshforge -x postgres").stdout.strip()
    assert leftovers == "", "postgres left running after restore"
    box.ok("systemctl start meshforge-controller")
    _wait_up(box)
    assert (
        "Traceback"
        not in box.run(
            "journalctl -u meshforge-controller -o cat --since '-120s'"
        ).stdout
    )


def test_04_rollback_failed_record_lifecycle_through_the_cli(box: SystemdBox) -> None:
    box.ok("systemctl stop meshforge-controller")
    rec = {
        "backup": "/tmp/good.dump", "from_version": "0.0.1", "to_version": "0.0.2",
        "error": "RuntimeError: boom", "at": "2026-10-25T00:00:00+00:00",
    }  # fmt: skip
    box.ok(
        f'su -s /bin/sh meshforge -c "cat > {ROOT}/rollback-failed.json"',
        stdin=json.dumps(rec),
    )
    st = box.run("meshforge-controller db status")
    assert "ROLLBACK FAILED" in st.stdout, st.stdout
    assert "db restore /tmp/good.dump" in st.stdout
    js = json.loads(box.ok("meshforge-controller db status --json"))
    assert js["rollback_failed"]["to_version"] == "0.0.2"
    # a failed restore keeps the record
    box.run("echo junk > /tmp/junk2.dump")
    assert (
        box.run("meshforge-controller db restore /tmp/junk2.dump --yes").returncode != 0
    )
    assert "ROLLBACK FAILED" in box.run("meshforge-controller db status").stdout
    # a damaged record is still reported, not a crash
    box.ok(
        f"echo '{{{{{{' > {ROOT}/rollback-failed.json && chown meshforge {ROOT}/rollback-failed.json"
    )
    st = box.run("meshforge-controller db status")
    assert st.returncode == 0 and "Traceback" not in st.stdout + st.stderr
    assert "ROLLBACK FAILED" in st.stdout
    # the good restore clears it
    r = box.run("meshforge-controller db restore /tmp/good.dump --yes 2>&1")
    assert r.returncode == 0, r.stdout[-1500:]
    assert "ROLLBACK FAILED" not in box.run("meshforge-controller db status").stdout
    assert box.run(f"test -e {ROOT}/rollback-failed.json").returncode != 0
    # a good start also clears a (re-created) record
    box.ok(
        f"echo '{json.dumps(rec)}' > {ROOT}/rollback-failed.json && chown meshforge {ROOT}/rollback-failed.json"
    )
    box.ok("systemctl start meshforge-controller")
    _wait_up(box)
    box.ok(
        f"for i in $(seq 90); do test -e {ROOT}/rollback-failed.json || exit 0; sleep 1; done; exit 1"
    )
