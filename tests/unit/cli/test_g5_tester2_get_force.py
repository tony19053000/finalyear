"""V0.4 G5 tester round 2 (fix B2): more ways to break ``compute get --force``
than the implementer's own ``test_cli_get_force.py`` exercised.

SAFETY: every destination here is a fresh folder under ``tmp_path``; nothing
outside it is ever touched, even by a regressed implementation, because the
CLI is invoked with an explicit ``--dest`` inside ``tmp_path`` and the process
cwd/HOME are repointed into ``tmp_path`` where a test needs them at all.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from mf_cli_support import TransportFactory
from typer.testing import CliRunner

from meshforge.cli.client import ControllerClient
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
    folder.mkdir(parents=True)
    (folder / "keep.txt").write_bytes(b"my precious results\n")
    return folder


@pytest.fixture
def bundle(tmp_path: Path) -> bytes:
    src = tmp_path / "produced"
    src.mkdir()
    (src / "result.txt").write_bytes(b"fresh output\n")
    archive = tmp_path / "outputs.bundle"
    pack_to_file(src, archive)
    return archive.read_bytes()


def _job(digest: str) -> dict[str, object]:
    return {
        "job_id": JOB,
        "state": "succeeded",
        "attempts": [
            {"attempt_id": "att_1", "fencing_token": 1, "output_digest": digest}
        ],
    }


def _handler(
    job: dict[str, object], blob: Callable[[httpx.Request], httpx.Response]
) -> Callable[[httpx.Request], httpx.Response]:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/v1/jobs/{JOB}":
            return httpx.Response(200, json=job)
        if request.url.path.startswith("/v1/blobs/"):
            return blob(request)
        return httpx.Response(404, json={"error": {"code": "not_found"}})

    return handle


def _serving(body: bytes) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _r: httpx.Response(200, stream=httpx.ByteStream(body))


def _get(runner: CliRunner, dest: Path, *extra: str) -> tuple[int, str]:
    result = runner.invoke(
        __import__("meshforge.cli.app", fromlist=["app"]).app,
        ["get", JOB, "--dest", str(dest), "--force", *extra],
    )
    if result.exception is not None and not isinstance(
        result.exception, (SystemExit, KeyboardInterrupt)
    ):
        raise result.exception
    return result.exit_code, result.output


def _no_temp_left(parent: Path) -> None:
    assert not [p for p in parent.iterdir() if p.name.startswith(".compute-get-")]


# --------------------------------------------------------- symlinked target


def test_force_never_follows_a_symlinked_target(
    runner: CliRunner, controller: TransportFactory, tmp_path: Path, bundle: bytes
) -> None:
    """``--dest`` pointing at a symlink whose target is a real directory full of
    files must be refused outright - never unlinked, never followed, never
    have its target's contents replaced."""
    real_dir = tmp_path / "real-outputs"
    real_dir.mkdir()
    (real_dir / "important.txt").write_bytes(b"do not touch\n")
    link = tmp_path / "link-to-real"
    link.symlink_to(real_dir)

    digest = "sha256:" + hashlib.sha256(bundle).hexdigest()
    controller(_handler(_job(digest), _serving(bundle)))
    code, out = _get(runner, link)

    assert code == ExitCode.USAGE_ERROR, out
    assert "not a folder" in out or "exists and is not a folder" in out
    # the symlink itself is untouched, and its target still has its own file
    assert link.is_symlink()
    assert real_dir.exists()
    assert (real_dir / "important.txt").read_bytes() == b"do not touch\n"


# --------------------------------------------------------- unwritable parent


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permission bits")
@pytest.mark.xfail(
    strict=True,
    reason="DEFECT T2-1 (minor): get() does not catch OSError from "
    "target.parent.mkdir / TemporaryDirectory, so an unwritable parent ends in a "
    "Python traceback instead of a clean one-line error. Nothing is damaged.",
)
def test_force_with_an_unwritable_parent_fails_cleanly(
    runner: CliRunner, controller: TransportFactory, tmp_path: Path, bundle: bytes
) -> None:
    """The destination's parent has no write permission: the CLI cannot even
    create its ``.compute-get-*`` staging folder there. It must fail with a
    clean, non-zero, non-crash exit and touch nothing - never fall back to
    deleting or writing anywhere else."""
    parent = tmp_path / "locked"
    parent.mkdir()
    target = parent / "out"
    parent.chmod(0o500)  # read + execute, no write
    try:
        digest = "sha256:" + hashlib.sha256(bundle).hexdigest()
        controller(_handler(_job(digest), _serving(bundle)))
        code, _out = _get(runner, target)
        assert code != 0
        # nothing was created under the locked parent
        assert list(parent.iterdir()) == []
    finally:
        parent.chmod(0o700)


# --------------------------------------------------------- KeyboardInterrupt


def test_ctrl_c_during_download_leaves_the_old_folder_and_no_temp_dir(
    runner: CliRunner,
    controller: TransportFactory,
    existing: Path,
    bundle: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Ctrl-C while the archive is downloading must not leave a half-swapped
    destination or an orphaned ``.compute-get-*`` staging folder behind: the
    temp dir is inside a ``with tempfile.TemporaryDirectory()`` block, so
    unwinding on the exception must clean it up, and ``_swap_in`` must never
    have been reached.

    Typer's own ``core.py`` wraps every command and turns an uncaught
    ``KeyboardInterrupt`` into ``typer.Exit(130)`` (the same code ``run -f``
    and ``logs -f`` use for a detach) - unlike those two commands, ``get`` has
    no inner ``except KeyboardInterrupt`` of its own, so no "detached" message
    is printed, but the exit code is still the fabric's INTERRUPTED code and
    the ``with`` blocks still unwind normally."""
    before = _snapshot(existing)
    digest = "sha256:" + hashlib.sha256(bundle).hexdigest()
    controller(_handler(_job(digest), _serving(bundle)))

    def boom(self: ControllerClient, hex_digest: str, dest: Path) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr(ControllerClient, "download_blob", boom)
    result = runner.invoke(
        __import__("meshforge.cli.app", fromlist=["app"]).app,
        ["get", JOB, "--dest", str(existing), "--force"],
    )
    assert isinstance(result.exception, SystemExit)
    assert result.exit_code == ExitCode.INTERRUPTED
    assert result.exit_code == 130
    assert _snapshot(existing) == before
    _no_temp_left(existing.parent)
