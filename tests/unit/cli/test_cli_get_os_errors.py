"""V0.4 G5 follow-ups (reviewer pass 3): ``compute get`` and OS-level failures.

* T2-1: an ``OSError`` that is not a :class:`BundleError` (ENOSPC while
  unpacking, an unwritable parent) is a clean one-line error, the old folder is
  untouched and no ``.compute-get-*`` staging folder is left behind;
* if the final rename fails *and* the rollback rename fails too, the parked old
  folder is kept (never deleted with the staging folder) and its exact path is
  printed so the user can recover it.

SAFETY: every destination is a fresh folder under ``tmp_path``, passed as an
explicit ``--dest``; ``Path.rename`` patches only affect paths inside it.
"""

from __future__ import annotations

import errno
import hashlib
import os
import re
import sys
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from mf_cli_support import TransportFactory
from typer.testing import CliRunner

from meshforge.cli import job_commands
from meshforge.cli.app import app
from meshforge.common import bundle as bundle_module
from meshforge.common.bundle import pack_to_file
from meshforge.common.exit_codes import ExitCode

pytestmark = pytest.mark.unit

JOB = "job_01JQZ00000000000000000000A"


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): (b"<dir>" if p.is_dir() else p.read_bytes())
        for p in sorted(root.rglob("*"))
    }


@pytest.fixture
def existing(tmp_path: Path) -> Path:
    folder = tmp_path / "work" / "out"
    (folder / "sub").mkdir(parents=True)
    (folder / "keep.txt").write_bytes(b"my precious results\n")
    (folder / "sub" / "model.bin").write_bytes(bytes(range(256)) * 4)
    return folder


@pytest.fixture
def served(tmp_path: Path, controller: TransportFactory) -> Callable[[], None]:
    """Serve a real, valid outputs bundle for ``JOB``."""
    src = tmp_path / "produced"
    src.mkdir()
    (src / "result.txt").write_bytes(b"fresh output\n")
    archive = tmp_path / "outputs.bundle"
    pack_to_file(src, archive)
    body = archive.read_bytes()
    digest = "sha256:" + hashlib.sha256(body).hexdigest()
    job = {
        "job_id": JOB,
        "state": "succeeded",
        "attempts": [
            {"attempt_id": "att_1", "fencing_token": 1, "output_digest": digest}
        ],
    }

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/v1/jobs/{JOB}":
            return httpx.Response(200, json=job)
        if request.url.path.startswith("/v1/blobs/"):
            return httpx.Response(200, stream=httpx.ByteStream(body))
        return httpx.Response(404, json={"error": {"code": "not_found"}})

    return lambda: controller(handle)


def _get(runner: CliRunner, dest: Path) -> tuple[int, str]:
    result = runner.invoke(app, ["get", JOB, "--dest", str(dest), "--force"])
    if result.exception is not None and not isinstance(result.exception, SystemExit):
        raise result.exception
    return result.exit_code, result.output


def _temp_dirs(parent: Path) -> list[Path]:
    return [p for p in parent.iterdir() if p.name.startswith(".compute-get-")]


# ------------------------------------------------------------- T2-1: unpack


def test_enospc_while_unpacking_is_a_clean_error(
    runner: CliRunner,
    served: Callable[[], None],
    existing: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A disk-full error half-way through unpacking: one line naming the
    staging path and the cause, exit FAILURE, old folder untouched, no
    staging folder left (including the half-written files)."""
    before = _snapshot(existing)
    served()

    def full_disk(archive: Path, dest: Path) -> object:
        dest.mkdir()
        (dest / "partial.bin").write_bytes(b"x" * 100)
        raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC), str(dest / "big"))

    monkeypatch.setattr(bundle_module, "unpack", full_disk)
    code, out = _get(runner, existing)

    assert code == ExitCode.FAILURE, out
    assert "cannot unpack the outputs into" in out
    assert os.strerror(errno.ENOSPC) in out
    assert str(existing) in out
    assert "Traceback" not in out
    assert len(out.strip().splitlines()) == 1
    assert _snapshot(existing) == before
    assert _temp_dirs(existing.parent) == []


def test_unpack_oserror_without_strerror_names_its_type(
    runner: CliRunner,
    served: Callable[[], None],
    existing: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = _snapshot(existing)
    served()

    def odd(archive: Path, dest: Path) -> object:
        raise PermissionError()

    monkeypatch.setattr(bundle_module, "unpack", odd)
    code, out = _get(runner, existing)
    assert code == ExitCode.FAILURE, out
    assert "PermissionError" in out
    assert _snapshot(existing) == before
    assert _temp_dirs(existing.parent) == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
@pytest.mark.skipif(
    sys.platform == "win32" or os.geteuid() == 0,
    reason="needs POSIX permission bits, non-root (root ignores permission bits)",
)
def test_unwritable_parent_names_the_folder_and_cause(
    runner: CliRunner, served: Callable[[], None], tmp_path: Path
) -> None:
    parent = tmp_path / "locked"
    parent.mkdir()
    parent.chmod(0o500)
    try:
        served()
        code, out = _get(runner, parent / "out")
        assert code == ExitCode.FAILURE, out
        assert f"cannot create a staging folder in {parent}" in out
        assert os.strerror(errno.EACCES) in out
        assert list(parent.iterdir()) == []
    finally:
        parent.chmod(0o700)


# ------------------------------------------------- _swap_in: double failure


def _break_both_renames(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    """The final rename (``new`` -> target) and the rollback (``old`` ->
    target) both fail; every other rename under ``root`` works."""
    real = Path.rename

    def rename(self: Path, target: str | os.PathLike[str]) -> Path:
        if root in self.parents and self.name in {"new", "old"}:
            raise PermissionError(errno.EACCES, os.strerror(errno.EACCES))
        return real(self, target)

    monkeypatch.setattr(Path, "rename", rename)


def test_swap_in_double_failure_raises_with_the_parked_path(
    tmp_path: Path, existing: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = _snapshot(existing)
    stage = tmp_path / "work" / ".compute-get-x"
    fresh = stage / "new"
    fresh.mkdir(parents=True)
    (fresh / "n.txt").write_bytes(b"n")
    _break_both_renames(monkeypatch, tmp_path)

    with pytest.raises(job_commands._StrandedError) as info:
        job_commands._swap_in(fresh, existing, stage / "old")
    assert info.value.parked == stage / "old"
    assert info.value.errno == errno.EACCES
    assert _snapshot(stage / "old") == before


def test_get_keeps_the_parked_folder_when_the_rollback_fails(
    runner: CliRunner,
    served: Callable[[], None],
    existing: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both renames fail: the old folder must survive at the printed path -
    the staging folder holding it is not deleted."""
    before = _snapshot(existing)
    served()
    _break_both_renames(monkeypatch, tmp_path)

    code, out = _get(runner, existing)

    assert code == ExitCode.FAILURE, out
    assert "Traceback" not in out
    match = re.search(r"it is safe at (\S+)$", out.strip())
    assert match is not None, out
    parked = Path(match.group(1))
    assert existing.parent in parked.parents  # inside our tmp_path, nowhere else
    assert _snapshot(parked) == before
    assert not existing.exists()
    [staging] = _temp_dirs(existing.parent)
    assert parked == staging / "old"


# ------------------------------------------------ D-1: download / sha256


def test_oserror_while_hashing_the_download_is_a_clean_error(
    runner: CliRunner,
    served: Callable[[], None],
    existing: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = _snapshot(existing)
    served()

    def unreadable(path: Path) -> str:
        raise OSError(errno.EIO, os.strerror(errno.EIO), str(path))

    monkeypatch.setattr(job_commands, "_sha256", unreadable)
    code, out = _get(runner, existing)
    assert code == ExitCode.FAILURE, out
    assert "cannot save the outputs to" in out and "outputs.bundle" in out
    assert os.strerror(errno.EIO) in out
    assert len(out.strip().splitlines()) == 1
    assert _snapshot(existing) == before
    assert _temp_dirs(existing.parent) == []


# ------------------------------------- reviewer: interrupted between renames


def test_ctrl_c_between_the_renames_keeps_the_parked_folder(
    runner: CliRunner,
    served: Callable[[], None],
    existing: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ctrl-C arrives after the old folder was parked and before the new one
    is renamed into place: the staging folder (holding the only copy of the
    old folder) must not be deleted, and the parked path is printed."""
    before = _snapshot(existing)
    served()
    real = Path.rename

    def rename(self: Path, target: str | os.PathLike[str]) -> Path:
        if tmp_path in self.parents and self.name == "new":
            raise KeyboardInterrupt
        return real(self, target)

    monkeypatch.setattr(Path, "rename", rename)
    result = runner.invoke(app, ["get", JOB, "--dest", str(existing), "--force"])

    assert isinstance(result.exception, KeyboardInterrupt) or result.exit_code != 0
    match = re.search(r"the old folder is safe at (\S+)", result.output)
    assert match is not None, result.output
    parked = Path(match.group(1))
    [staging] = _temp_dirs(existing.parent)
    assert parked == staging / "old"
    assert _snapshot(parked) == before
    assert not existing.exists()


def test_stranded_error_keeps_the_cause_filename(tmp_path: Path) -> None:
    cause = PermissionError(errno.EACCES, os.strerror(errno.EACCES), "/x/new")
    err = job_commands._StrandedError(cause, tmp_path / "old")
    assert err.filename == "/x/new"
    assert err.errno == errno.EACCES
    assert err.parked == tmp_path / "old"
