"""V0.5 G3 (D94) tester: hostile sidecars and hostile log folders."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from meshforge.common.ids import new_attempt_id
from meshforge.controller.jobs.log_gaps import (
    MAX_SIDECAR_BYTES,
    read_sidecar,
    write_sidecar,
)
from meshforge.controller.jobs.logs import LogHub

pytestmark = pytest.mark.unit

posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX file types")


def _hub(tmp_path: Path) -> LogHub:
    hub = LogHub(tmp_path / "logs", blobs=None, max_bytes=1000)
    hub.prepare()
    return hub


@posix_only
def test_a_directory_named_like_a_sidecar_is_refused_not_read(tmp_path: Path) -> None:
    (tmp_path / "x.gaps").mkdir()
    with pytest.raises(OSError):
        read_sidecar(tmp_path / "x.gaps")


@posix_only
def test_the_hub_ignores_a_directory_sidecar(tmp_path: Path) -> None:
    hub = _hub(tmp_path)
    attempt = str(new_attempt_id())
    hub.gaps_path(attempt).mkdir()
    assert hub.ended_state(attempt) is None
    assert hub.stats.gaps_unreadable == 1


def test_a_sidecar_of_exactly_the_bound_is_read_and_one_more_refused(
    tmp_path: Path,
) -> None:
    path = tmp_path / "a.gaps"
    path.write_bytes(b"x" * MAX_SIDECAR_BYTES)
    assert read_sidecar(path) == b"x" * MAX_SIDECAR_BYTES
    path.write_bytes(b"x" * (MAX_SIDECAR_BYTES + 1))
    with pytest.raises(ValueError, match="too large"):
        read_sidecar(path)


def test_write_sidecar_replaces_atomically_and_leaves_no_temp(tmp_path: Path) -> None:
    path = tmp_path / "a.gaps"
    write_sidecar(path, b"one")
    write_sidecar(path, b"two")
    assert path.read_bytes() == b"two"
    assert [p.name for p in tmp_path.iterdir()] == ["a.gaps"]


def test_truncated_and_binary_sidecars_never_raise_out_of_the_hub(
    tmp_path: Path,
) -> None:
    hub = _hub(tmp_path)
    for blob in (b"", b'{"v":1,"streams":{"stdout":{"gaps":[[1,', b"\xff\xfe\x00" * 50):
        attempt = str(new_attempt_id())
        hub.gaps_path(attempt).write_bytes(blob)
        assert hub.ended_state(attempt) is None


@posix_only
@pytest.mark.xfail(
    strict=True,
    reason="DEFECT: read_sidecar opens a FIFO named <attempt>.gaps with a blocking "
    "open(O_RDONLY) (no O_NONBLOCK), hanging the caller (the event loop)",
)
def test_a_fifo_sidecar_does_not_hang_the_reader(tmp_path: Path) -> None:
    fifo = tmp_path / "x.gaps"
    os.mkfifo(fifo)
    code = textwrap.dedent(
        f"""
        from pathlib import Path
        from meshforge.controller.jobs.log_gaps import read_sidecar
        try:
            read_sidecar(Path({str(fifo)!r}))
        except (OSError, ValueError):
            pass
        """
    )
    try:
        subprocess.run([sys.executable, "-c", code], timeout=5, check=True)
    except subprocess.TimeoutExpired:
        pytest.fail("read_sidecar blocked on a FIFO")
