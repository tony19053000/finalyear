"""V0.7 G5 tester round 1: ways a job tries to hide what it writes from the
output cap (hardlinks, sparse files, symlink loops, unreadable folders, deleted
but still open files), through the real Executor and a real subprocess.

Sizes are MiB, the cap 4 MiB, the poll 0.2 s.
"""

from __future__ import annotations

import os
import sys
import textwrap
from pathlib import Path

import pytest
from mf_one_attempt import run_one_attempt

from meshforge.agent.execution.subprocess_runtime import SubprocessRuntime
from meshforge.common.errors import FailureType

pytestmark = [
    pytest.mark.timeout(120),
    pytest.mark.skipif(os.name != "posix", reason="POSIX file semantics"),
]
MIB = 1024 * 1024
EXECUTION = {"max_written_bytes": 4 * MIB, "output_check_interval_s": 0.2}


def _attempt(tmp_path: Path, code: str) -> tuple[str, FailureType | None, str]:
    final, _ = run_one_attempt(
        tmp_path,
        SubprocessRuntime(),
        entrypoint=[sys.executable, "-c", textwrap.dedent(code)],
        execution=EXECUTION,
        timeout_s=60,
    )
    return final.state, final.failure_type, final.message


def test_plain_writes_past_the_cap_are_stopped(tmp_path: Path) -> None:
    state, kind, message = _attempt(
        tmp_path,
        """
        import time
        with open("big.bin", "wb") as f:
            f.write(b"x" * (12 * 1024 * 1024)); f.flush()
        time.sleep(20)
        """,
    )
    assert (state, kind) == ("failed", FailureType.OUTPUT_ERROR), message
    assert "output_limit" in message


def test_many_hardlinks_to_one_file_count_once(tmp_path: Path) -> None:
    state, kind, message = _attempt(
        tmp_path,
        """
        import os
        with open("a.bin", "wb") as f:
            f.write(b"x" * (3 * 1024 * 1024))
        for i in range(200):
            os.link("a.bin", f"l{i}.bin")
        print("fine", flush=True)
        """,
    )
    assert state == "done", (state, kind, message)


def test_a_big_sparse_file_costs_only_what_it_occupies(tmp_path: Path) -> None:
    state, kind, message = _attempt(
        tmp_path,
        """
        with open("sparse.bin", "wb") as f:
            f.truncate(8 * 1024 * 1024 * 1024)
        print("fine", flush=True)
        """,
    )
    assert state == "done", (state, kind, message)


def test_a_symlink_to_a_huge_tree_is_not_followed(tmp_path: Path) -> None:
    state, kind, message = _attempt(
        tmp_path,
        """
        import os
        os.symlink("/usr", "usr_link")
        os.symlink(".", "loop")
        os.symlink("loop/loop/loop", "deep")
        print("fine", flush=True)
        """,
    )
    assert state == "done", (state, kind, message)


def test_writes_hidden_in_a_chmod_000_folder_still_count(tmp_path: Path) -> None:
    state, kind, message = _attempt(
        tmp_path,
        """
        import os, time
        os.makedirs("a/b")
        with open("a/b/big.bin", "wb") as f:
            f.write(b"x" * (12 * 1024 * 1024)); f.flush()
        os.chmod("a/b", 0); os.chmod("a", 0)
        time.sleep(20)
        """,
    )
    assert (state, kind) == ("failed", FailureType.OUTPUT_ERROR), message


def test_writes_spread_over_many_small_files_still_count(tmp_path: Path) -> None:
    state, kind, message = _attempt(
        tmp_path,
        """
        import time
        for i in range(3000):
            with open(f"f{i}.bin", "wb") as f:
                f.write(b"y" * 4096)
        time.sleep(20)
        """,
    )
    assert (state, kind) == ("failed", FailureType.OUTPUT_ERROR), message


def test_a_deleted_but_open_file_is_not_counted_documented_limit(
    tmp_path: Path,
) -> None:
    """ACCEPTED RISK (D160, THREAT_MODEL WS-E8): a file the job unlinks while
    holding it open is invisible to the output-cap walk (the poll measures paths
    under the workdir; the space is held by an open descriptor only). The cap
    does not trip; the host stays protected by the free-disk guard
    (``disk_full``), the job's timeout and the space being freed when the job
    exits. Pinned here so a future fix (e.g. a per-attempt filesystem quota)
    flips this test on purpose."""
    state, kind, message = _attempt(
        tmp_path,
        """
        import os, time
        f = open("ghost.bin", "wb")
        f.write(b"x" * (24 * 1024 * 1024)); f.flush()
        os.unlink("ghost.bin")
        time.sleep(3)   # still holding 24 MiB on the host disk
        """,
    )
    assert (state, kind) == ("done", None), (state, kind, message)
