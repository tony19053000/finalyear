"""V0.4 G5 tester round 3: break the ``compute get`` OS-error handling and the
``_swap_in`` stranded-folder guard at every step.

SAFETY: every destination is a fresh folder under ``tmp_path`` passed as an
explicit ``--dest``; ``Path.rename`` patches act only on paths under it.
"""

from __future__ import annotations

import errno
import hashlib
import os
import tempfile
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from mf_cli_support import TransportFactory
from typer.testing import CliRunner

from meshforge.cli import job_commands
from meshforge.cli.app import app
from meshforge.cli.client import ControllerClient
from meshforge.common import bundle as bundle_module
from meshforge.common.bundle import pack_to_file
from meshforge.common.exit_codes import ExitCode

pytestmark = pytest.mark.unit

JOB = "job_01JQZ00000000000000000000A"
ODD = "my dir ünï 日本 [x]"  # spaces, unicode, markup brackets


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): (b"<dir>" if p.is_dir() else p.read_bytes())
        for p in sorted(root.rglob("*"))
    }


def _staging(parent: Path) -> list[Path]:
    return [p for p in parent.iterdir() if p.name.startswith(".compute-get-")]


def _make_existing(base: Path, name: str = "out") -> Path:
    folder = base / name
    (folder / "sub").mkdir(parents=True)
    (folder / "keep.txt").write_bytes(b"precious\n")
    (folder / "sub" / "m.bin").write_bytes(bytes(range(256)))
    return folder


@pytest.fixture
def existing(tmp_path: Path) -> Path:
    return _make_existing(tmp_path / "work")


@pytest.fixture
def served(tmp_path: Path, controller: TransportFactory) -> Callable[[], None]:
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
        "attempts": [{"attempt_id": "a", "fencing_token": 1, "output_digest": digest}],
    }

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/v1/jobs/{JOB}":
            return httpx.Response(200, json=job)
        if request.url.path.startswith("/v1/blobs/"):
            return httpx.Response(200, stream=httpx.ByteStream(body))
        return httpx.Response(404, json={"error": {"code": "not_found"}})

    return lambda: controller(handle)


def _get(runner: CliRunner, dest: Path) -> tuple[int, str, BaseException | None]:
    result = runner.invoke(app, ["get", JOB, "--dest", str(dest), "--force"])
    exc = result.exception
    if isinstance(exc, SystemExit):
        exc = None
    return result.exit_code, result.output, exc


def _clean_failure(code: int, out: str, exc: BaseException | None) -> None:
    assert exc is None, repr(exc)
    assert code == ExitCode.FAILURE, out
    assert "Traceback" not in out
    assert len(out.strip().splitlines()) == 1, out


# ------------------------------------------------ staging creation failures


def test_mkdtemp_oserror_is_one_clean_line(
    runner: CliRunner,
    served: Callable[[], None],
    existing: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = _snapshot(existing)
    served()

    def boom(*a: object, **k: object) -> str:
        raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))

    monkeypatch.setattr(tempfile, "mkdtemp", boom)
    code, out, exc = _get(runner, existing)
    _clean_failure(code, out, exc)
    assert str(existing.parent) in out and os.strerror(errno.ENOSPC) in out
    assert _snapshot(existing) == before
    assert _staging(existing.parent) == []


def test_parent_mkdir_failure_when_parent_is_a_file(
    runner: CliRunner, served: Callable[[], None], tmp_path: Path
) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_bytes(b"i am a file")
    served()
    code, out, exc = _get(runner, blocker / "sub" / "out")
    _clean_failure(code, out, exc)
    assert "cannot create a staging folder" in out
    assert blocker.read_bytes() == b"i am a file"


def test_staging_failure_strerrorless_oserror_names_type(
    runner: CliRunner,
    served: Callable[[], None],
    existing: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    served()

    def boom(*a: object, **k: object) -> str:
        raise PermissionError()

    monkeypatch.setattr(tempfile, "mkdtemp", boom)
    code, out, exc = _get(runner, existing)
    _clean_failure(code, out, exc)
    assert "PermissionError" in out


# ----------------------------------------------------------- unpack failures


def test_unpack_midfile_oserror_unicode_path_cleans_up(
    runner: CliRunner,
    served: Callable[[], None],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = _make_existing(tmp_path / ODD)
    before = _snapshot(existing)
    served()

    def full(archive: Path, dest: Path) -> object:
        dest.mkdir()
        (dest / "half.bin").write_bytes(b"x" * 5000)
        raise OSError(errno.EDQUOT, os.strerror(errno.EDQUOT))

    monkeypatch.setattr(bundle_module, "unpack", full)
    code, out, exc = _get(runner, existing)
    _clean_failure(code, out, exc)
    assert ODD in out and str(existing) in out
    assert _snapshot(existing) == before
    assert _staging(existing.parent) == []


def test_keyboard_interrupt_during_unpack_cleans_staging(
    runner: CliRunner,
    served: Callable[[], None],
    existing: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = _snapshot(existing)
    served()

    def interrupted(archive: Path, dest: Path) -> object:
        dest.mkdir()
        (dest / "half.bin").write_bytes(b"x")
        raise KeyboardInterrupt

    monkeypatch.setattr(bundle_module, "unpack", interrupted)
    result = runner.invoke(app, ["get", JOB, "--dest", str(existing), "--force"])
    assert result.exit_code != 0
    assert _snapshot(existing) == before
    assert _staging(existing.parent) == []


def test_corrupt_bundle_with_matching_digest_is_a_bundle_error(
    runner: CliRunner, existing: Path, controller: TransportFactory
) -> None:
    body = b"this is not a bundle at all"
    digest = "sha256:" + hashlib.sha256(body).hexdigest()
    job = {
        "job_id": JOB,
        "state": "succeeded",
        "attempts": [{"attempt_id": "a", "fencing_token": 1, "output_digest": digest}],
    }

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/v1/jobs/{JOB}":
            return httpx.Response(200, json=job)
        return httpx.Response(200, stream=httpx.ByteStream(body))

    controller(handle)
    before = _snapshot(existing)
    code, out, exc = _get(runner, existing)
    _clean_failure(code, out, exc)
    assert "cannot unpack the outputs" in out
    assert _snapshot(existing) == before
    assert _staging(existing.parent) == []


# ------------------------------------------------- download / verify failures


def test_download_oserror_is_one_clean_line(
    runner: CliRunner,
    served: Callable[[], None],
    existing: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = _snapshot(existing)
    served()

    def full(self: ControllerClient, digest: str, dest: Path) -> None:
        dest.write_bytes(b"partial")
        raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))

    monkeypatch.setattr(ControllerClient, "download_blob", full)
    code, out, exc = _get(runner, existing)
    try:
        _clean_failure(code, out, exc)
    finally:
        # whatever happens the old folder and staging must be safe
        assert _snapshot(existing) == before
        assert _staging(existing.parent) == []


def test_digest_mismatch_leaves_everything(
    runner: CliRunner,
    served: Callable[[], None],
    existing: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = _snapshot(existing)
    served()
    monkeypatch.setattr(job_commands, "_sha256", lambda p: "0" * 64)
    code, out, exc = _get(runner, existing)
    _clean_failure(code, out, exc)
    assert _snapshot(existing) == before
    assert _staging(existing.parent) == []


# ------------------------------------------------------------ rename failures


def _fail_renames(
    monkeypatch: pytest.MonkeyPatch, root: Path, names: set[str], *, plain: bool = False
) -> list[tuple[str, str]]:
    """Fail renames of ``new`` (-> target) and/or ``old`` (rollback / park)."""
    real = Path.rename
    seen: list[tuple[str, str]] = []

    def rename(self: Path, target: str | os.PathLike[str]) -> Path:
        if root in self.parents:
            key = "park" if self.name == "out" else self.name
            seen.append((key, str(target)))
            if key in names:
                if plain:
                    raise PermissionError()
                raise PermissionError(errno.EACCES, os.strerror(errno.EACCES))
        return real(self, target)

    monkeypatch.setattr(Path, "rename", rename)
    return seen


def test_first_rename_fails_old_folder_stays(
    runner: CliRunner,
    served: Callable[[], None],
    existing: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Parking the old folder fails: nothing has moved."""
    before = _snapshot(existing)
    served()
    _fail_renames(monkeypatch, tmp_path, {"park"})
    code, out, exc = _get(runner, existing)
    _clean_failure(code, out, exc)
    assert "left as it was" in out
    assert _snapshot(existing) == before
    assert _staging(existing.parent) == []


def test_second_rename_fails_rollback_succeeds(
    runner: CliRunner,
    served: Callable[[], None],
    existing: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = _snapshot(existing)
    served()
    _fail_renames(monkeypatch, tmp_path, {"new"})
    code, out, exc = _get(runner, existing)
    _clean_failure(code, out, exc)
    assert _snapshot(existing) == before
    assert _staging(existing.parent) == []


def test_new_target_final_rename_fails_cleans_staging(
    runner: CliRunner,
    served: Callable[[], None],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No old folder to park; the only rename fails: staging goes."""
    dest = tmp_path / "work" / "fresh"
    dest.parent.mkdir()
    served()
    _fail_renames(monkeypatch, tmp_path, {"new"})
    code, out, exc = _get(runner, dest)
    _clean_failure(code, out, exc)
    assert not dest.exists()
    assert _staging(dest.parent) == []


def test_stranded_with_strerrorless_errors_and_odd_path(
    runner: CliRunner,
    served: Callable[[], None],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = _make_existing(tmp_path / ODD)
    before = _snapshot(existing)
    served()
    _fail_renames(monkeypatch, tmp_path, {"new", "old"}, plain=True)
    code, out, exc = _get(runner, existing)
    _clean_failure(code, out, exc)
    marker = "it is safe at "
    assert marker in out
    parked = Path(out.strip().split(marker, 1)[1])
    assert ODD in str(parked)
    assert _snapshot(parked) == before
    assert not existing.exists()


def test_stranded_copy_survives_a_second_successful_get(
    runner: CliRunner,
    served: Callable[[], None],
    existing: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = _snapshot(existing)
    served()
    with monkeypatch.context() as m:
        _fail_renames(m, tmp_path, {"new", "old"})
        code, out, exc = _get(runner, existing)
    _clean_failure(code, out, exc)
    parked = Path(out.strip().split("it is safe at ", 1)[1])
    assert _snapshot(parked) == before

    # second, healthy get into the same (now missing) destination
    code2, out2, exc2 = _get(runner, existing)
    assert exc2 is None and code2 == ExitCode.OK, out2
    assert (existing / "result.txt").read_bytes() == b"fresh output\n"
    assert _snapshot(parked) == before  # still there, untouched
    assert len(_staging(existing.parent)) == 1  # only the stranded staging dir


def test_stranded_error_unit_contract(tmp_path: Path) -> None:
    err = job_commands._StrandedError(PermissionError(), tmp_path / "p")
    assert isinstance(err, OSError)
    assert err.parked == tmp_path / "p"
    assert job_commands._os_cause(err)  # never empty
