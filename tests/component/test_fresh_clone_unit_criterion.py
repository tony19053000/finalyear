"""Tester (task 0.1.8), V0.1 success criterion 1: "Fresh clone -> `uv sync` ->
`pytest -m unit` passes on Linux and Windows."

This reproduces the criterion literally: copy the working tree into an isolated
temp directory (own ``UV_CACHE_DIR``, no ``.venv``/``.git``/caches carried over),
run a *plain* ``uv sync`` (the default dependency group, **no** extras -- this is
what "fresh clone -> uv sync" means; it must not require `--extra controller` or
`--all-extras`), then ``pytest -m unit`` inside that environment. Real `uv`, real
subprocess, no mocks. Never modifies `src/`.

Companion check: with the Docker daemon made unreachable (`DOCKER_HOST` pointed at
a closed port), a bare `pytest -q` (all tiers, no `-m` filter) must report zero
failures and zero collection errors -- component tests that need Docker must skip,
never error the whole run, even without the controller extra installed.

Isolation (implementer, 0.1.8 fix round -- commands and return-code assertions
unchanged; the unit-run summary check is now count-based, because a substring
check for "failed" also matched "2 xfailed"):

* The bare ``pytest -q`` inside the clone would collect *this* module again and
  recurse (clone -> clone -> ...). Child runs get ``MESHFORGE_FRESH_CLONE_CHILD=1``
  and this module skips itself there with that reason.
* Child pytest runs get a private ``PYTEST_DEBUG_TEMPROOT`` so their numbered
  ``pytest-of-<user>`` temp dirs cannot rotate away (delete) the parent's.
* ``VIRTUAL_ENV`` and the outer ``.venv/bin`` PATH entry (both set by the outer
  ``uv run``) are replaced by the clone's venv, so console-script lookups
  (``shutil.which``) cannot resolve to the original checkout -- the cause of the
  "skip reason shows the original repo's path" observation in the 0.1.8 CI run.
* A cold-cache ``uv sync`` plus two full test runs cannot fit the suite-wide
  300 s pytest-timeout (which also covers fixture setup), so this module gets
  its own larger budget; each subprocess keeps its own 300 s timeout.
* Marked ``slow`` (opt-in, ``--run-slow``): a bare ``pytest`` skips it. CI runs it
  in the ``fresh-clone`` job on Linux and Windows.
* Cross-platform: the venv's script dir is ``Scripts`` with ``.exe`` suffixes on
  Windows (``bin`` elsewhere); pytest is invoked as ``<clone venv python> -m
  pytest`` so no console-script lookup is involved, and PATH entries are compared
  normalised (case/separators) so the outer venv is scrubbed on Windows too.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

CHILD_ENV_FLAG = "MESHFORGE_FRESH_CLONE_CHILD"

pytestmark = [
    pytest.mark.component,
    pytest.mark.slow,
    # uv sync (cold cache) 300 s + two pytest runs 300 s each, plus copy slack.
    pytest.mark.timeout(1000),
    pytest.mark.skipif(
        os.environ.get(CHILD_ENV_FLAG) == "1",
        reason="already running inside a fresh-clone child run (no recursion)",
    ),
]

REPO_ROOT = Path(__file__).resolve().parents[2]

#: venv layout differs by OS: ``Scripts\python.exe`` on Windows, ``bin/python``.
VENV_BIN = "Scripts" if os.name == "nt" else "bin"
EXE_SUFFIX = ".exe" if os.name == "nt" else ""

COPY_EXCLUDE = {
    ".venv",
    ".git",
    "__pycache__",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    ".import_linter_cache",
    "dist",
}


def _copy_repo(dest: Path) -> None:
    def ignore(_dir: str, names: list[str]) -> set[str]:
        return {n for n in names if n in COPY_EXCLUDE}

    shutil.copytree(REPO_ROOT, dest, ignore=ignore)


def _venv_bin(venv: Path) -> Path:
    return venv / VENV_BIN


def _venv_python(clone: Path) -> Path:
    return _venv_bin(clone / ".venv") / f"python{EXE_SUFFIX}"


def _norm_path(entry: str) -> str:
    """Comparable form of a PATH entry (Windows: case-insensitive, either slash)."""
    return os.path.normcase(os.path.normpath(entry)) if entry else entry


def _path_key(env: dict[str, str]) -> str:
    """The PATH variable's actual key (Windows env may spell it ``Path``)."""
    return next((k for k in env if k.upper() == "PATH"), "PATH")


def _child_env(clone: Path) -> dict[str, str]:
    """Inherited env, minus the outer venv, marked as a child, own temp root."""
    env = dict(os.environ)
    outer_venv = env.pop("VIRTUAL_ENV", None)
    # Activate the clone's venv on PATH and drop the outer one: otherwise console
    # scripts (`compute`, `meshforge-agent`, ...) resolve via `shutil.which` to the
    # ORIGINAL checkout's .venv/bin -- a cross-repo leak, not a fresh clone.
    outer_bins = {_norm_path(str(_venv_bin(REPO_ROOT / ".venv")))}
    if outer_venv:
        outer_bins.add(_norm_path(str(_venv_bin(Path(outer_venv)))))
    key = _path_key(env)
    path = [
        p
        for p in env.pop(key, "").split(os.pathsep)
        if p and _norm_path(p) not in outer_bins
    ]
    env["PATH"] = os.pathsep.join([str(_venv_bin(clone / ".venv")), *path])
    env[CHILD_ENV_FLAG] = "1"
    temproot = clone.parent / "child-tmp"
    temproot.mkdir(exist_ok=True)
    env["PYTEST_DEBUG_TEMPROOT"] = str(temproot)
    return env


@pytest.fixture(scope="module")
def plain_sync_clone(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A fresh copy of the repo after a plain (no extras) `uv sync`."""
    root = tmp_path_factory.mktemp("meshforge-fresh-clone")
    project_dir = root / "clone"
    cache_dir = root / "uv-cache"
    _copy_repo(project_dir)

    env = _child_env(project_dir)
    env["UV_CACHE_DIR"] = str(cache_dir)

    result = subprocess.run(
        ["uv", "sync"],
        cwd=project_dir,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert result.returncode == 0, (
        f"`uv sync` (fresh clone, no extras) failed:\nstdout={result.stdout}\n"
        f"stderr={result.stderr}"
    )
    return project_dir


def test_fresh_clone_uv_sync_pytest_unit_passes(plain_sync_clone: Path) -> None:
    """Criterion 1, literally: `uv sync` then `pytest -m unit` must succeed.

    This must work with *no* optional extras installed -- a unit-tier run must
    never require the controller/agent/gpu extras, even indirectly through a
    collection-time import in an unrelated (non-unit) test module.
    """
    env = _child_env(plain_sync_clone)
    env.pop("DOCKER_HOST", None)
    result = subprocess.run(
        [str(_venv_python(plain_sync_clone)), "-m", "pytest", "-m", "unit", "-q"],
        cwd=plain_sync_clone,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert result.returncode == 0, (
        "`pytest -m unit` must pass after a plain `uv sync` (fresh clone, no "
        f"extras):\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    # Count-based, not substring-based: "2 xfailed" must not read as "failed".
    summary = result.stdout.lower()
    assert not re.search(r"\b\d+ failed\b", summary), summary
    assert not re.search(r"\b\d+ errors?\b", summary), summary


def test_fresh_clone_docker_unreachable_bare_pytest_has_no_failures(
    plain_sync_clone: Path,
) -> None:
    """Even without the controller extra, an unreachable Docker daemon must make
    Docker-needing tests *skip*, not error the whole collection/run.
    """
    env = _child_env(plain_sync_clone)
    env["DOCKER_HOST"] = "tcp://127.0.0.1:1"
    result = subprocess.run(
        [str(_venv_python(plain_sync_clone)), "-m", "pytest", "-q"],
        cwd=plain_sync_clone,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert result.returncode == 0, (
        "a bare `pytest -q` with Docker unreachable and no extras installed must "
        f"have zero failures/errors:\nstdout={result.stdout}\nstderr={result.stderr}"
    )
