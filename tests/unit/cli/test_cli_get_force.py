"""V0.4 G5 fix round (reviewer B2): ``compute get --force`` never destroys first.

Before the fix ``--force`` removed the destination *before* the job lookup,
download and digest check, so any later failure (no outputs, a network error,
a digest mismatch, a bad archive) destroyed the user's folder - and
``--dest .`` emptied the working directory. Now the new folder is built next to
the target and swapped in only on success; dangerous targets are refused.

The transport is ``httpx.MockTransport``; the bundle is a real one.
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

from meshforge.cli import job_commands
from meshforge.cli.app import app
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
def bundle(tmp_path: Path) -> bytes:
    src = tmp_path / "produced"
    src.mkdir()
    (src / "result.txt").write_bytes(b"fresh output\n")
    archive = tmp_path / "outputs.bundle"
    pack_to_file(src, archive)
    return archive.read_bytes()


def _job(digest: str | None) -> dict[str, object]:
    attempt: dict[str, object] = {"attempt_id": "att_1", "fencing_token": 1}
    if digest is not None:
        attempt["output_digest"] = digest
    return {"job_id": JOB, "state": "succeeded", "attempts": [attempt]}


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
    result = runner.invoke(app, ["get", JOB, "--dest", str(dest), "--force", *extra])
    if result.exception is not None and not isinstance(result.exception, SystemExit):
        raise result.exception
    return result.exit_code, result.output


def _no_temp_left(parent: Path) -> None:
    assert not [p for p in parent.iterdir() if p.name.startswith(".compute-get-")]


def test_force_success_replaces_the_folder(
    runner: CliRunner, controller: TransportFactory, existing: Path, bundle: bytes
) -> None:
    digest = "sha256:" + hashlib.sha256(bundle).hexdigest()
    controller(_handler(_job(digest), _serving(bundle)))
    code, out = _get(runner, existing)
    assert code == 0, out
    assert _snapshot(existing) == {"result.txt": b"fresh output\n"}
    _no_temp_left(existing.parent)


def test_no_outputs_leaves_the_folder(
    runner: CliRunner, controller: TransportFactory, existing: Path
) -> None:
    before = _snapshot(existing)
    controller(_handler(_job(None), _serving(b"")))
    code, out = _get(runner, existing)
    assert code == ExitCode.FAILURE and "has no outputs" in out
    assert _snapshot(existing) == before


def test_network_error_leaves_the_folder(
    runner: CliRunner, controller: TransportFactory, existing: Path, bundle: bytes
) -> None:
    before = _snapshot(existing)
    digest = "sha256:" + hashlib.sha256(bundle).hexdigest()

    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    controller(_handler(_job(digest), broken))
    code, _ = _get(runner, existing)
    assert code != 0
    assert _snapshot(existing) == before
    _no_temp_left(existing.parent)


def test_digest_mismatch_leaves_the_folder(
    runner: CliRunner, controller: TransportFactory, existing: Path, bundle: bytes
) -> None:
    before = _snapshot(existing)
    digest = "sha256:" + hashlib.sha256(bundle).hexdigest()
    controller(_handler(_job(digest), _serving(bundle + b"tampered")))
    code, out = _get(runner, existing)
    assert code == ExitCode.FAILURE and "do not match their digest" in out
    assert _snapshot(existing) == before
    _no_temp_left(existing.parent)


def test_bad_archive_leaves_the_folder(
    runner: CliRunner, controller: TransportFactory, existing: Path
) -> None:
    before = _snapshot(existing)
    garbage = b"this is not a zstd tar bundle"
    digest = "sha256:" + hashlib.sha256(garbage).hexdigest()
    controller(_handler(_job(digest), _serving(garbage)))
    code, out = _get(runner, existing)
    assert code == ExitCode.FAILURE and "cannot unpack" in out
    assert _snapshot(existing) == before
    _no_temp_left(existing.parent)


def test_a_failed_final_rename_puts_the_old_folder_back(
    tmp_path: Path, existing: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = _snapshot(existing)
    fresh = tmp_path / "work" / "fresh"
    fresh.mkdir()
    (fresh / "new.txt").write_bytes(b"new")
    real = Path.rename

    def rename(self: Path, target: str | os.PathLike[str]) -> Path:
        if self == fresh:
            raise PermissionError("in use")
        return real(self, target)

    monkeypatch.setattr(Path, "rename", rename)
    with pytest.raises(PermissionError):
        job_commands._swap_in(fresh, existing, tmp_path / "work" / "parked")
    assert _snapshot(existing) == before
    assert not (tmp_path / "work" / "parked").exists()


# ------------------------------------------------------------------ refusals


def _never_called(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"no request may be sent: {request.url}")


@pytest.mark.parametrize("which", ["cwd", "dot", "ancestor", "home"])
def test_force_refuses_dangerous_targets(
    runner: CliRunner,
    controller: TransportFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    which: str,
) -> None:
    home = tmp_path / "home"
    cwd = home / "project" / "run"
    cwd.mkdir(parents=True)
    (cwd / "code.py").write_bytes(b"print(1)\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.chdir(cwd)
    dest = {
        "cwd": cwd,
        "dot": Path("."),
        "ancestor": Path(".."),
        "home": home,
    }[which]
    before = _snapshot(home)
    controller(_never_called)
    code, out = _get(runner, dest)
    assert code == ExitCode.USAGE_ERROR, out
    assert "refusing --force" in out
    assert _snapshot(home) == before


def test_force_refuses_a_filesystem_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Checked on the helper only: driving the CLI at a real root would let a
    # regressed implementation delete from it.
    monkeypatch.chdir(tmp_path)
    assert job_commands._unsafe_force_target(Path(tmp_path.anchor)) is not None
    assert job_commands._unsafe_force_target(tmp_path / "outputs") is None


def test_force_is_fine_for_a_subfolder_of_cwd(
    runner: CliRunner,
    controller: TransportFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bundle: bytes,
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "outputs").mkdir()
    (tmp_path / "outputs" / "old.txt").write_bytes(b"old")
    digest = "sha256:" + hashlib.sha256(bundle).hexdigest()
    controller(_handler(_job(digest), _serving(bundle)))
    code, out = _get(runner, Path("outputs"))
    assert code == 0, out
    assert _snapshot(tmp_path / "outputs") == {"result.txt": b"fresh output\n"}
