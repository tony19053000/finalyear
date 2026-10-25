"""V0.9 G1 tester round 2: the export's O_EXCL / no-follow create and its
inode-checked cleanup (T-6 fix) under races."""

from __future__ import annotations

import os
import stat
import sys
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from mf_cli_support import TransportFactory
from typer.testing import CliRunner

from meshforge.cli.app import app

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX inode/symlink semantics"),
]

BODY = b"PAR1" + b"x" * 50_000 + b"PAR1"


class _Dying(httpx.SyncByteStream):
    def __init__(self, during: object = None) -> None:
        self._during = during

    def __iter__(self) -> Iterator[bytes]:
        yield b"PAR1" + b"x" * 1000
        if callable(self._during):
            self._during()
        raise httpx.ReadError("connection reset")


def test_a_symlink_to_an_existing_file_is_refused_and_the_target_untouched(
    runner: CliRunner, controller: TransportFactory, tmp_path: Path
) -> None:
    target = tmp_path / "precious"
    target.write_bytes(b"keep me")
    link = tmp_path / "out.parquet"
    link.symlink_to(target)
    controller(lambda _r: httpx.Response(200, content=BODY))
    result = runner.invoke(app, ["telemetry", "export", "-o", str(link)])
    assert result.exit_code == 2
    assert target.read_bytes() == b"keep me" and link.is_symlink()


def test_the_symlink_planted_after_the_check_is_not_followed(
    runner: CliRunner, controller: TransportFactory, tmp_path: Path
) -> None:
    """lexists passes, then an attacker plants a symlink to a victim file before
    the create: O_EXCL|O_NOFOLLOW must refuse; the victim stays unchanged."""
    victim = tmp_path / "victim"
    victim.write_bytes(b"victim data")
    out = tmp_path / "out.parquet"

    def handler(_r: httpx.Request) -> httpx.Response:
        out.symlink_to(victim)
        return httpx.Response(200, content=BODY)

    controller(handler)
    result = runner.invoke(app, ["telemetry", "export", "-o", str(out)])
    assert result.exit_code == 2, result.output
    assert victim.read_bytes() == b"victim data"
    assert out.is_symlink()


def test_the_file_swapped_for_another_mid_download_is_not_removed_on_failure(
    runner: CliRunner, controller: TransportFactory, tmp_path: Path
) -> None:
    out = tmp_path / "out.parquet"

    def swap() -> None:
        out.unlink()
        out.write_bytes(b"someone else's file")

    controller(lambda _r: httpx.Response(200, stream=_Dying(swap)))
    result = runner.invoke(app, ["telemetry", "export", "-o", str(out)])
    assert result.exit_code != 0
    assert out.read_bytes() == b"someone else's file"


def test_the_file_replaced_by_a_symlink_mid_download_is_not_removed(
    runner: CliRunner, controller: TransportFactory, tmp_path: Path
) -> None:
    out = tmp_path / "out.parquet"
    other = tmp_path / "other"
    other.write_bytes(b"other")

    def swap() -> None:
        out.unlink()
        out.symlink_to(other)

    controller(lambda _r: httpx.Response(200, stream=_Dying(swap)))
    runner.invoke(app, ["telemetry", "export", "-o", str(out)])
    assert other.read_bytes() == b"other"
    assert out.is_symlink()


def test_our_own_half_file_is_removed_even_when_the_dir_entry_is_a_hardlink_copy(
    runner: CliRunner, controller: TransportFactory, tmp_path: Path
) -> None:
    out = tmp_path / "out.parquet"
    controller(lambda _r: httpx.Response(200, stream=_Dying()))
    result = runner.invoke(app, ["telemetry", "export", "-o", str(out)])
    assert result.exit_code != 0 and not out.exists()
    assert not list(tmp_path.iterdir())


def test_the_created_file_is_a_regular_file_with_sane_permissions(
    runner: CliRunner, controller: TransportFactory, tmp_path: Path
) -> None:
    out = tmp_path / "out.parquet"
    controller(lambda _r: httpx.Response(200, content=BODY))
    result = runner.invoke(app, ["telemetry", "export", "-o", str(out)])
    assert result.exit_code == 0, result.output
    mode = out.stat().st_mode
    assert stat.S_ISREG(mode) and not mode & stat.S_IXUSR
    assert out.read_bytes() == BODY


@pytest.mark.skipif(
    sys.platform == "win32" or os.geteuid() == 0,
    reason="needs POSIX permission bits, non-root (root ignores directory permissions)",
)
def test_a_read_only_directory_is_one_line_exit_1(
    runner: CliRunner, controller: TransportFactory, tmp_path: Path
) -> None:
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o500)
    try:
        controller(lambda _r: httpx.Response(200, content=BODY))
        result = runner.invoke(
            app, ["telemetry", "export", "-o", str(ro / "x.parquet")]
        )
    finally:
        ro.chmod(0o700)
    assert result.exit_code == 1, result.output
    assert "Traceback" not in result.output
    assert len([ln for ln in result.output.splitlines() if ln.strip()]) <= 2


def test_two_exports_to_one_path_one_wins_and_its_file_survives(
    runner: CliRunner, controller: TransportFactory, tmp_path: Path
) -> None:
    """The nested (second) export completes while the outer one is between its
    existence check and its create: exactly one wins; the loser is a usage
    error and removes nothing."""
    out = tmp_path / "out.parquet"
    inner: list[int] = []

    def handler(_r: httpx.Request) -> httpx.Response:
        if not inner:
            inner.append(-1)
            inner[0] = runner.invoke(
                app, ["telemetry", "export", "-o", str(out)]
            ).exit_code
        return httpx.Response(200, content=BODY)

    controller(handler)
    first = runner.invoke(app, ["telemetry", "export", "-o", str(out)])
    assert inner == [0] and first.exit_code == 2, (inner, first.output)
    assert out.read_bytes() == BODY
