"""V0.4 G5 D74: expanding and packing an attempt's declared outputs (no network).

Glob rules, folder patterns, missing patterns, the ``.meshforge`` exclusion,
refused links (a file, a folder, a link inside a matched folder), the byte bound
(max accepted, max+1 refused) and determinism of the packed archive.
"""

from __future__ import annotations

import io
import os
import sys
from pathlib import Path

import pytest

from meshforge.agent.execution.outputs import (
    OutputError,
    collect_outputs,
    expand_outputs,
)
from meshforge.common.bundle import BundleError, pack_files, unpack
from meshforge.common.errors import FailureType

pytestmark = pytest.mark.unit

needs_symlinks = pytest.mark.skipif(
    sys.platform == "win32", reason="creating symlinks needs Developer Mode on Windows"
)


def _tree(root: Path, files: dict[str, bytes]) -> Path:
    for rel, data in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return root


@pytest.fixture
def work(tmp_path: Path) -> Path:
    return _tree(
        tmp_path / "work",
        {
            "a.csv": b"a",
            "b.txt": b"b",
            "results/r1.csv": b"r1",
            "results/deep/r2.csv": b"r2",
            "other/x.csv": b"x",
            ".meshforge/stdout.log": b"log",
            "main.py": b"print(1)",
        },
    )


def test_glob_rules(work: Path) -> None:
    files, missing = expand_outputs(work, ("*.csv",))
    assert files == ["a.csv"] and missing == []  # top level only
    files, _ = expand_outputs(work, ("**/*.csv",))
    assert files == ["a.csv", "other/x.csv", "results/deep/r2.csv", "results/r1.csv"]
    files, _ = expand_outputs(work, ("results/", "b.txt"))
    assert files == ["b.txt", "results/deep/r2.csv", "results/r1.csv"]
    files, _ = expand_outputs(work, ("./results/*.csv",))
    assert files == ["results/r1.csv"]


def test_missing_patterns_are_listed_not_errors(work: Path) -> None:
    files, missing = expand_outputs(work, ("results/", "nope/*.bin", "absent.txt"))
    assert files == ["results/deep/r2.csv", "results/r1.csv"]
    assert missing == ["nope/*.bin", "absent.txt"]


def test_internal_folder_is_never_an_output(work: Path) -> None:
    files, missing = expand_outputs(work, (".meshforge/", "**/*.log", "*"))
    assert not any(f.startswith(".meshforge") for f in files)
    assert ".meshforge/" in missing and "**/*.log" in missing


def test_an_empty_matched_folder_is_not_missing(tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()
    assert expand_outputs(tmp_path, ("empty/",)) == ([], [])


@needs_symlinks
@pytest.mark.parametrize(
    ("make", "pattern"),
    [
        (lambda w: os.symlink("/etc/passwd", w / "leak.txt"), "leak.txt"),
        (lambda w: os.symlink("/etc", w / "etcdir"), "etcdir/"),
        (lambda w: os.symlink("r1.csv", w / "results" / "inside.csv"), "results/"),
    ],
)
def test_links_are_refused_typed(work: Path, make: object, pattern: str) -> None:
    make(work)  # type: ignore[operator]
    with pytest.raises(OutputError) as info:
        expand_outputs(work, (pattern,))
    assert info.value.failure_type is FailureType.OUTPUT_ERROR
    assert info.value.retryable is False


def test_collect_packs_deterministically_and_round_trips(
    work: Path, tmp_path: Path
) -> None:
    first = collect_outputs(work, ("results/",), tmp_path / "o1", max_bytes=10**6)
    second = collect_outputs(work, ("results/",), tmp_path / "o2", max_bytes=10**6)
    assert first.digest is not None and first.digest == second.digest
    assert first.files == 2 and first.missing == ()
    assert first.archive is not None
    dest = tmp_path / "unpacked"
    unpack(first.archive, dest)
    assert (dest / "results" / "r1.csv").read_bytes() == b"r1"
    assert (dest / "results" / "deep" / "r2.csv").read_bytes() == b"r2"
    assert not (dest / "main.py").exists()


def test_nothing_matched_means_no_archive(work: Path, tmp_path: Path) -> None:
    got = collect_outputs(work, ("none/",), tmp_path / "o", max_bytes=10**6)
    assert got.archive is None and got.digest is None and got.missing == ("none/",)
    assert not (tmp_path / "o").exists()


def test_byte_bound_max_accepted_and_max_plus_one_refused(tmp_path: Path) -> None:
    work = _tree(tmp_path / "w", {"out.bin": b"z" * 1000})
    ok = collect_outputs(work, ("out.bin",), tmp_path / "ok", max_bytes=1000)
    assert ok.digest is not None
    with pytest.raises(OutputError) as info:
        collect_outputs(work, ("out.bin",), tmp_path / "no", max_bytes=999)
    assert info.value.retryable is False
    assert not (tmp_path / "no").exists()


def test_a_gitignored_output_is_still_packed(tmp_path: Path) -> None:
    """Outputs use pack_files: no ignore rules drop a declared output."""
    work = _tree(
        tmp_path / "w",
        {".gitignore": b"*.pyc\nresults/\n", "results/model.pyc": b"weights"},
    )
    got = collect_outputs(work, ("results/",), tmp_path / "o", max_bytes=10**6)
    assert got.files == 1


@needs_symlinks
def test_pack_files_refuses_a_symlinked_parent(tmp_path: Path) -> None:
    work = _tree(tmp_path / "w", {"real/f.txt": b"f"})
    os.symlink(work / "real", work / "alias")
    with pytest.raises(BundleError):
        pack_files(work, ["alias/f.txt"], io.BytesIO())
