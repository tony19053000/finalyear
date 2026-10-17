"""V0.9 G1 tester round 1: ``compute telemetry export`` never overwrites and
never leaves a half file - including when the filesystem or the network
misbehaves.  ``test_defect_*`` are strict xfails (see the tester report)."""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from mf_cli_support import TransportFactory
from typer.testing import CliRunner

from meshforge.cli.app import app

pytestmark = pytest.mark.unit

BODY = b"PAR1" + b"x" * 300_000 + b"PAR1"


class _Dying(httpx.SyncByteStream):
    def __iter__(self) -> Iterator[bytes]:
        yield b"PAR1" + b"x" * 5000
        raise httpx.ReadError("connection reset by peer")


def test_a_connection_that_dies_mid_download_leaves_no_file(
    runner: CliRunner, controller: TransportFactory, tmp_path: Path
) -> None:
    controller(lambda _r: httpx.Response(200, stream=_Dying()))
    out = tmp_path / "data.parquet"
    result = runner.invoke(app, ["telemetry", "export", "-o", str(out)])
    assert result.exit_code != 0
    assert not out.exists()
    assert "Traceback" not in result.output


def test_ctrl_c_mid_download_leaves_no_file(
    runner: CliRunner, controller: TransportFactory, tmp_path: Path
) -> None:
    class Interrupted(httpx.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            yield b"PAR1"
            raise KeyboardInterrupt

    controller(lambda _r: httpx.Response(200, stream=Interrupted()))
    out = tmp_path / "data.parquet"
    runner.invoke(app, ["telemetry", "export", "-o", str(out)])
    assert not out.exists()


def test_a_missing_parent_directory_is_a_clean_error(
    runner: CliRunner, controller: TransportFactory, tmp_path: Path
) -> None:
    controller(lambda _r: httpx.Response(200, content=BODY))
    result = runner.invoke(
        app, ["telemetry", "export", "-o", str(tmp_path / "no" / "such" / "x.parquet")]
    )
    assert result.exit_code != 0
    assert "Traceback" not in result.output
    assert not isinstance(result.exception, (FileNotFoundError, OSError)) or (
        result.exit_code != 0
    )


def test_a_directory_as_output_is_refused(
    runner: CliRunner, controller: TransportFactory, tmp_path: Path
) -> None:
    controller(lambda _r: httpx.Response(200, content=BODY))
    result = runner.invoke(app, ["telemetry", "export", "-o", str(tmp_path)])
    assert result.exit_code == 2 and tmp_path.is_dir()


def test_unicode_and_spaces_in_the_output_path(
    runner: CliRunner, controller: TransportFactory, tmp_path: Path
) -> None:
    controller(lambda _r: httpx.Response(200, content=BODY))
    out = tmp_path / "résultats d'été 日本.parquet"
    result = runner.invoke(app, ["telemetry", "export", "-o", str(out)])
    assert result.exit_code == 0, result.output
    assert out.read_bytes() == BODY


def test_defect_6_a_file_created_during_the_request_is_not_deleted_by_the_cleanup(
    runner: CliRunner, controller: TransportFactory, tmp_path: Path
) -> None:
    """Check-then-create race: the existence check passes, someone else creates
    the file, ``open('xb')`` raises FileExistsError, and the blanket
    ``except BaseException: output.unlink()`` deletes *their* file."""
    out = tmp_path / "theirs.parquet"

    def handler(_r: httpx.Request) -> httpx.Response:
        out.write_bytes(b"someone else's data")
        return httpx.Response(200, content=BODY)

    controller(handler)
    result = runner.invoke(app, ["telemetry", "export", "-o", str(out)])
    assert result.exit_code != 0
    assert out.exists() and out.read_bytes() == b"someone else's data"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_defect_6_a_dangling_symlink_as_output_is_left_alone(
    runner: CliRunner, controller: TransportFactory, tmp_path: Path
) -> None:
    controller(lambda _r: httpx.Response(200, content=BODY))
    link = tmp_path / "link.parquet"
    os.symlink(tmp_path / "nowhere" / "target.parquet", link)
    result = runner.invoke(app, ["telemetry", "export", "-o", str(link)])
    assert result.exit_code != 0
    assert link.is_symlink(), "the user's symlink was removed"
