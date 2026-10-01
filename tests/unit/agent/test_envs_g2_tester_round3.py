"""V0.5 G2 tester round 3: summary exclusion width, VCS/option-line edge cases,
per-requirement option normalisation."""

from __future__ import annotations

from pathlib import Path

import pytest

from meshforge.agent.envs import EnvError, normalize_requirements
from meshforge.agent.envs.builder import MARKER_NAME
from meshforge.agent.envs.fsutil import tree_summary

pytestmark = pytest.mark.unit


def _tree(root: Path, files: list[str]) -> None:
    for rel in files:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"data")


# ------------------------------------------------------------- exclusions
@pytest.mark.parametrize(
    "ignored",
    [
        "lib/__pycache__/m.cpython-312.pyc",
        "data/__pycache__/x.pyc",
        "a/b/c/__pycache__/y.cpython-313.opt-1.pyc",
    ],
)
def test_pyc_directly_in_a_pycache_dir_is_ignored(tmp_path: Path, ignored: str) -> None:
    _tree(tmp_path, ["lib/a.py"])
    before = tree_summary(tmp_path, skip_root_names=(MARKER_NAME,))
    _tree(tmp_path, [ignored])
    assert tree_summary(tmp_path, skip_root_names=(MARKER_NAME,)) == before


@pytest.mark.parametrize(
    "counted",
    [
        "lib/m.pyc",  # sourceless pyc outside __pycache__ is real payload
        "lib/__pycache__/m.PYC",
        "lib/__pycache__/m.py",
        "lib/__pycache__/m.pyc.bak",
        "lib/__pycache__/sub/m.pyc",  # nested below the bytecode dir
        "lib/x__pycache__/m.pyc",
        "lib/__pycache__x/m.pyc",
        "lib/sub/" + MARKER_NAME,  # marker copy in a subdir
        "lib/__pycache__/" + MARKER_NAME,
    ],
)
def test_everything_else_is_counted(tmp_path: Path, counted: str) -> None:
    _tree(tmp_path, ["lib/a.py"])
    before = tree_summary(tmp_path, skip_root_names=(MARKER_NAME,))
    _tree(tmp_path, [counted])
    assert tree_summary(tmp_path, skip_root_names=(MARKER_NAME,)) != before


def test_the_root_marker_is_the_only_name_skipped(tmp_path: Path) -> None:
    _tree(tmp_path, ["lib/a.py"])
    before = tree_summary(tmp_path, skip_root_names=(MARKER_NAME,))
    _tree(tmp_path, [MARKER_NAME])
    assert tree_summary(tmp_path, skip_root_names=(MARKER_NAME,)) == before


def test_a_symlinked_dir_named_pycache_is_counted_as_a_link(tmp_path: Path) -> None:
    _tree(tmp_path, ["lib/a.py", "elsewhere/x.pyc"])
    before = tree_summary(tmp_path)
    (tmp_path / "lib" / "__pycache__").symlink_to(tmp_path / "elsewhere")
    assert tree_summary(tmp_path) != before


# ------------------------------------------------------------ VCS edge cases
@pytest.mark.parametrize(
    "ref",
    [
        "git+ssh://git@github.com/o/r.git@v1.2",
        "git+https://h/r.git@abc123#subdirectory=pkg/sub",
        "git+https://h/r.git@abc123#egg=foo&subdirectory=pkg",
        "GIT+HTTPS://h/r.git",
        "Hg+Https://h/r",
        "svn+https://h/r/trunk",
        "bzr+https://h/r",
    ],
)
def test_network_vcs_references_are_kept(ref: str) -> None:
    out = normalize_requirements(f"foo @ {ref}\n".encode()).decode()
    assert out.startswith("foo @ ") and ref in out


@pytest.mark.parametrize(
    "ref",
    [
        "git+file:///srv/r",
        "Git+File:///srv/r",
        "GIT+FILE:///srv/r",
        "git+%66ile:///srv/r",
        "git+/srv/r",
        "git+./r",
        "git+C:/r",
        "hg+file:///r",
        "git+ftp://h/r",
        "git+data:x",
        "git:///x",
        "git+ssh:/srv/r",
        "https:/x",
        "https:x",
        "//h/x",
        "HTTPS:\\\\h\\x",
        " ",
    ],
)
def test_local_or_malformed_references_are_refused(ref: str) -> None:
    with pytest.raises(EnvError):
        normalize_requirements(f"foo @ {ref}\n".encode())


def test_a_remote_reference_after_a_marker_cannot_smuggle_a_local_one() -> None:
    with pytest.raises(EnvError):
        normalize_requirements(b"foo @ https://h/a ; x @ file:///etc/passwd\n")


# ------------------------------------------- option lines that stay global
@pytest.mark.parametrize(
    "line",
    [
        "--index-url file:///srv/index",
        "--index-url=file:///srv/index",
        "--extra-index-url /srv/index",
        "--find-links /srv/wheels",
        "--find-links=./wheels",
        "-f ../wheels",
        "-f C:\\wheels",
        "-i file:///srv/index",
        "--index-url FILE:///srv/index",
    ],
)
def test_global_options_pointing_at_local_paths_are_refused(line: str) -> None:
    """Same class as a local direct reference: the directory's contents are not in
    the env hash, so two different environments would share one."""
    with pytest.raises(EnvError):
        normalize_requirements((line + "\nfoo==1\n").encode())


@pytest.mark.parametrize(
    "line",
    [
        "--index-url https://pypi.example/simple",
        "--extra-index-url http://10.0.0.5:8080/simple",
        "--find-links https://wheels.example/",
        "--no-index",
        "--pre",
    ],
)
def test_global_options_over_the_network_are_kept_and_hashed(line: str) -> None:
    out = normalize_requirements((line + "\nfoo==1\n").encode()).decode()
    assert line in out
    other = normalize_requirements(b"foo==1\n").decode()
    assert out != other


# --------------------------------------------- per-requirement options (D4)
def test_hash_options_are_order_insensitive_and_whitespace_insensitive() -> None:
    a = normalize_requirements(
        b"foo==1 \\\n  --hash=sha256:aa \\\n  --hash=sha256:bb\n"
    )
    b = normalize_requirements(b"foo==1 --hash=sha256:bb    --hash sha256:aa\n")
    assert a == b == b"foo==1 --hash=sha256:aa --hash=sha256:bb\n"


def test_different_hashes_are_different_environments() -> None:
    a = normalize_requirements(b"foo==1 --hash=sha256:aa\n")
    b = normalize_requirements(b"foo==1 --hash=sha256:bb\n")
    c = normalize_requirements(b"foo==1\n")
    assert len({a, b, c}) == 3


def test_a_pip_compile_file_normalises_line_per_requirement() -> None:
    text = (
        "# This file is autogenerated by pip-compile\n"
        "--index-url https://pypi.example/simple\n"
        "certifi==2024.2.2 \\\n"
        "    --hash=sha256:" + "a" * 64 + " \\\n"
        "    --hash=sha256:" + "b" * 64 + "\n"
        "    # via requests\n"
        "Requests==2.31.0 ; python_version >= '3.8' \\\n"
        "    --hash=sha256:" + "c" * 64 + "\n"
    )
    out = normalize_requirements(text.encode()).decode().splitlines()
    assert "--index-url https://pypi.example/simple" in out
    assert any(
        line.startswith("certifi==2024.2.2 --hash=sha256:aaaa") and "bbbb" in line
        for line in out
    )
    assert any(
        line.startswith("requests==2.31.0 ; python_version >= '3.8'") for line in out
    ), out
