"""SP3 checks on a built bundle: clean-environment runs, env-build probe, sizes.

"Clean environment" = what ``env -i PATH=... HOME=$tmp`` gives: only ``PATH`` and
``HOME``. Two ``PATH`` values are used:

* ``/usr/bin:/bin`` - the task's literal check (this machine *has* a system
  Python 3.12 there, so a lookup by name could still find it);
* a *toolbox* folder holding only symlinks to ``realpath`` and ``dirname`` (what
  the launchers need) - no Python and no uv reachable by name at all.
"""

from __future__ import annotations

import json
import lzma
import os
import shutil
import statistics
import subprocess
import sys
import tarfile
import time
import zlib
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Final

SYSTEM_PATH: Final = "/usr/bin:/bin"
LAUNCHER_TOOLS: Final = ("realpath", "dirname")
PROBE: Final = Path(__file__).resolve().parent / "env_probe.py"
SUPPORT: Final = Path(__file__).resolve().parents[3] / "tests" / "_support"
BUILD_SCRIPT: Final = (
    Path(__file__).resolve().parents[3] / "scripts" / "build_bundle.py"
)
PYTHON_REL: Final = "python/bin/python3.12"
WHERE_AM_I: Final = (
    "import json, sys; print(json.dumps({'executable': sys.executable, "
    "'prefix': sys.prefix, 'base_prefix': sys.base_prefix, 'path': sys.path}))"
)


@dataclass(frozen=True, slots=True)
class CmdResult:
    name: str
    path_kind: str
    exit: int
    seconds: float
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.exit == 0


def make_toolbox(folder: Path) -> Path:
    """A ``PATH`` folder with only the tools the sh launchers call."""
    folder.mkdir(parents=True, exist_ok=True)
    for tool in LAUNCHER_TOOLS:
        found = shutil.which(tool)
        if found is None:
            raise RuntimeError(f"{tool} not found on this machine")
        link = folder / tool
        if not link.exists():
            link.symlink_to(os.path.realpath(found))
    return folder


def clean_env(path: str, home: Path, **extra: str) -> dict[str, str]:
    home.mkdir(parents=True, exist_ok=True)
    return {"PATH": path, "HOME": str(home), **extra}


def run_cmd(
    name: str,
    argv: Sequence[str | Path],
    env: Mapping[str, str],
    path_kind: str,
    *,
    cwd: Path | None = None,
    timeout: float = 120.0,
) -> CmdResult:
    started = time.monotonic()
    done = subprocess.run(
        [str(a) for a in argv],
        env=dict(env),
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return CmdResult(
        name=name,
        path_kind=path_kind,
        exit=done.returncode,
        seconds=round(time.monotonic() - started, 3),
        stdout=done.stdout[-4000:],
        stderr=done.stderr[-2000:],
    )


def relocation_checks(
    bundle: Path, home: Path, path_value: str, path_kind: str, role: str = "agent"
) -> list[CmdResult]:
    """SP3 (c): the commands a fresh machine runs first, from ``home`` as cwd."""
    env = clean_env(path_value, home)
    bin_dir = bundle / "bin"
    cases: list[tuple[str, list[str | Path]]] = []
    if role == "agent":
        cases += [
            ("meshforge-agent --version", [bin_dir / "meshforge-agent", "--version"]),
            (
                "meshforge-agent hardware show",
                [bin_dir / "meshforge-agent", "hardware", "show"],
            ),
            ("uv --version (bundle)", [bundle / "uv", "--version"]),
        ]
    else:
        cases += [
            (
                "meshforge-controller --version",
                [bin_dir / "meshforge-controller", "--version"],
            ),
            (
                "meshforge-controller --help",
                [bin_dir / "meshforge-controller", "--help"],
            ),
        ]
    cases += [
        ("compute --help", [bin_dir / "compute", "--help"]),
        ("python: where am I", [bundle / PYTHON_REL, "-I", "-c", WHERE_AM_I]),
    ]
    return [run_cmd(name, argv, env, path_kind, cwd=home) for name, argv in cases]


def symlinked_launcher(
    bundle: Path, folder: Path, home: Path, path_kind: str
) -> CmdResult:
    """A launcher reached through a symlink (how a package puts it on PATH)."""
    folder.mkdir(parents=True, exist_ok=True)
    link = folder / "meshforge-agent"
    if link.is_symlink():
        link.unlink()
    link.symlink_to(bundle / "bin" / "meshforge-agent")
    env = clean_env(f"{folder}:{make_toolbox(home.parent / 'toolbox')}", home)
    return run_cmd(
        "meshforge-agent --version (via symlink on PATH)",
        ["meshforge-agent", "--version"],
        env,
        path_kind,
        cwd=home,
    )


def env_probe(
    bundle: Path, work: Path, path_value: str, path_kind: str
) -> dict[str, Any]:
    """SP3 (d): run :mod:`.env_probe` with the bundled interpreter, in a fresh
    ``work`` folder (so every build is a cold ``built``, never a cache hit)."""
    if work.exists():
        make_writable(work)
        shutil.rmtree(work)
    work.mkdir(parents=True)
    env = clean_env(path_value, work / "home")
    started = time.monotonic()
    done = subprocess.run(
        [str(bundle / PYTHON_REL), str(PROBE), str(work), str(SUPPORT)],
        env=env,
        cwd=work,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    result: dict[str, Any] = {
        "path_kind": path_kind,
        "exit": done.returncode,
        "seconds": round(time.monotonic() - started, 3),
    }
    try:
        result.update(json.loads(done.stdout.strip().splitlines()[-1]))
    except (ValueError, IndexError):
        result["stdout"] = done.stdout[-2000:]
        result["stderr"] = done.stderr[-2000:]
    return result


def startup_seconds(
    argv: Sequence[str | Path], env: Mapping[str, str], reps: int
) -> dict[str, float]:
    """Median / min wall time of ``reps`` runs (after one warm-up)."""
    times: list[float] = []
    for i in range(reps + 1):
        started = time.monotonic()
        subprocess.run(
            [str(a) for a in argv], env=dict(env), capture_output=True, check=True
        )
        if i:
            times.append(time.monotonic() - started)
    return {
        "median_s": round(statistics.median(times), 3),
        "min_s": round(min(times), 3),
    }


def strip_bytecode_read_only(bundle: Path) -> None:
    """Remove every pyc and make the tree read-only: the "no pyc shipped,
    installed read-only" case, where Python recompiles in memory at every start."""
    for cache in sorted(bundle.rglob("__pycache__")):
        shutil.rmtree(cache)
    for dirpath, _dirs, _files in os.walk(bundle):
        os.chmod(dirpath, 0o555)  # noqa: S103 - read-only on purpose (a no-pyc start-up)


def make_writable(bundle: Path) -> None:
    for dirpath, _dirs, _files in os.walk(bundle):
        os.chmod(dirpath, 0o755)  # noqa: S103 - our own scratch copy


def apparent_bytes(root: Path, pattern: str = "*") -> int:
    return sum(
        p.lstat().st_size
        for p in root.rglob(pattern)
        if p.is_file() and not p.is_symlink()
    )


def sizes(bundle: Path) -> dict[str, int]:
    site = bundle / "python/lib/python3.12/site-packages"
    stdlib = bundle / "python/lib/python3.12"
    pyc = apparent_bytes(bundle, "*.pyc")
    return {
        "total": apparent_bytes(bundle),
        "python_executable": (bundle / PYTHON_REL).stat().st_size,
        "uv_binary": (bundle / "python/bin/uv").stat().st_size
        if (bundle / "python/bin/uv").exists()
        else 0,
        "stdlib_incl_pyc": apparent_bytes(stdlib) - apparent_bytes(site),
        "site_packages_incl_pyc": apparent_bytes(site),
        "pyc_all": pyc,
        "manifest": (bundle / "MANIFEST.json").stat().st_size,
    }


def compressed_sizes(bundle: Path, out: Path) -> dict[str, int]:
    """Download-size estimates: ``tar`` of the bundle, gzip -6 and xz -6."""
    out.mkdir(parents=True, exist_ok=True)
    tar_path = out / "bundle.tar"
    with tarfile.open(tar_path, "w", format=tarfile.PAX_FORMAT) as tar:
        tar.add(bundle, arcname=bundle.name)
    raw = tar_path.read_bytes()
    gz = zlib.compress(raw, 6)
    xz = lzma.compress(raw, preset=6)
    tar_path.unlink()
    return {"tar": len(raw), "gzip_6": len(gz), "xz_6": len(xz)}


def verify_intact(bundle: Path) -> list[str]:
    """``build_bundle.py --verify``: differences from the bundle's manifest."""
    done = subprocess.run(
        [sys.executable, str(BUILD_SCRIPT), "--verify", str(bundle)],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    lines = done.stdout.splitlines()
    if done.returncode == 0 and lines[-1:] == ["intact"]:
        return []
    return lines or [done.stderr[-2000:]]


def as_dicts(results: Sequence[CmdResult]) -> list[dict[str, Any]]:
    return [{**asdict(r), "ok": r.ok} for r in results]
