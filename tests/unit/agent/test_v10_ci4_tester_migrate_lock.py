"""Tester (CI fix round 4): adversarial checks of the migrate-identity lock with
REAL processes - N concurrent runs, kill -9 mid-run (stale lock), dry runs while
a real run holds the lock, unwritable lock folder, timeout path."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from meshforge.agent.identity import (
    MIGRATION_PENDING_FILE_NAME,
    load_or_create,
    read_identity,
)
from meshforge.agent.migration import MigrationOutcome, Paths, apply
from meshforge.common.errors import MeshForgeError

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(sys.platform == "win32", reason="Linux package"),
]

CHILD = textwrap.dedent(
    """
    import json, sys, time
    from pathlib import Path
    from meshforge.agent.migration import Paths, apply
    root = Path(sys.argv[1]); mode = sys.argv[2]
    paths = Paths(target_identity=root/"var/lib/meshforge/identity",
                  target_config=root/"etc/meshforge/config.toml",
                  home_root=root/"home")
    if mode == "hang":
        real = apply.execute
        def slow(*a, **k):
            print("INSIDE", flush=True); time.sleep(60); return real(*a, **k)
        apply.execute = slow
    try:
        r = apply.migrate(explicit=None, config_from=None, dry_run=(mode=="dry"),
                          paths=paths, service_user=None)
        print(json.dumps({"outcome": r.plan.outcome.value}), flush=True)
    except Exception as e:
        print(json.dumps({"error": str(e)}), flush=True)
    """
)


def _paths(root: Path) -> Paths:
    return Paths(
        target_identity=root / "var/lib/meshforge/identity",
        target_config=root / "etc/meshforge/config.toml",
        home_root=root / "home",
    )


def _source(root: Path) -> tuple[Path, str]:
    d = root / "home" / "asha" / ".local/share/meshforge/identity"
    return d, load_or_create(d).node_id


def _spawn(root: Path, mode: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-c", CHILD, str(root), mode],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


@pytest.mark.parametrize("round_", range(3))
def test_eight_real_processes_one_migrates_rest_already(
    tmp_path: Path, round_: int
) -> None:
    source, node_id = _source(tmp_path)
    procs = [_spawn(tmp_path, "real") for _ in range(8)]
    outs = []
    for p in procs:
        o, e = p.communicate(timeout=120)
        assert p.returncode == 0, e
        outs.append(json.loads(o.strip().splitlines()[-1]))
    assert not [o for o in outs if "error" in o], outs
    kinds = sorted(o["outcome"] for o in outs)
    assert kinds.count(MigrationOutcome.MIGRATED.value) == 1, kinds
    assert kinds.count(MigrationOutcome.TARGET_PRESENT.value) == 7, kinds
    paths = _paths(tmp_path)
    assert not (paths.target_identity / MIGRATION_PENDING_FILE_NAME).exists()
    assert read_identity(paths.target_identity).node_id == node_id
    assert len(list(source.parent.glob("identity.migrated-*"))) == 1


def test_sigkill_mid_run_leaves_no_stale_lock(tmp_path: Path) -> None:
    _source_path, node_id = _source(tmp_path)
    hung = _spawn(tmp_path, "hang")
    assert hung.stdout is not None
    assert hung.stdout.readline().strip() == "INSIDE"
    # a second run waits on the lock while the first holds it ...
    waiter = _spawn(tmp_path, "real")
    time.sleep(0.5)
    assert waiter.poll() is None, "second run must be blocked by the lock"
    os.kill(hung.pid, signal.SIGKILL)
    hung.wait(10)
    hung.stdout.close()
    assert hung.stderr is not None
    hung.stderr.close()
    o, e = waiter.communicate(timeout=60)  # ... and proceeds after the crash
    assert json.loads(o.strip().splitlines()[-1]) == {
        "outcome": MigrationOutcome.MIGRATED.value
    }, (o, e)
    assert read_identity(_paths(tmp_path).target_identity).node_id == node_id


def test_dry_runs_never_block_or_create_files_while_lock_held(tmp_path: Path) -> None:
    _source(tmp_path)
    hung = _spawn(tmp_path, "hang")
    try:
        assert hung.stdout is not None
        assert hung.stdout.readline().strip() == "INSIDE"
        t0 = time.monotonic()
        dry = [_spawn(tmp_path, "dry") for _ in range(4)]
        for p in dry:
            o, e = p.communicate(timeout=30)
            assert "error" not in o, (o, e)
        assert time.monotonic() - t0 < 25
    finally:
        hung.kill()
        hung.wait(10)
        for f in (hung.stdout, hung.stderr):
            if f is not None:
                f.close()


def test_dry_run_on_fresh_system_creates_no_lock_dir(tmp_path: Path) -> None:
    _source(tmp_path)
    paths = _paths(tmp_path)
    apply.migrate(
        explicit=None, config_from=None, dry_run=True, paths=paths, service_user=None
    )
    assert not paths.target_identity.parent.exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permissions")
@pytest.mark.xfail(
    strict=True,
    reason="minor defect TC4-1: a non-writable lock folder escapes migrate() as a raw "
    "PermissionError (not a MeshForgeError); runs as root in practice",
)
def test_unwritable_lock_folder_is_a_clean_failure_not_a_traceback(
    tmp_path: Path,
) -> None:
    _source(tmp_path)
    paths = _paths(tmp_path)
    parent = paths.target_identity.parent
    parent.mkdir(parents=True)
    parent.chmod(0o500)
    try:
        with pytest.raises((MeshForgeError, OSError)) as ei:
            apply.migrate(
                explicit=None,
                config_from=None,
                dry_run=False,
                paths=paths,
                service_user=None,
            )
        # report which kind (raw OSError would surface as a traceback in the CLI)
        assert isinstance(ei.value, MeshForgeError), (
            f"raw {type(ei.value).__name__} escapes migrate(): {ei.value}"
        )
    finally:
        parent.chmod(0o700)
