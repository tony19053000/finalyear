"""Build the D384 relocatable runtime bundle (V0.17 G1; prototype: spike SP3).

``meshforge-<ver>-<os>-<arch>/`` = a python-build-standalone CPython 3.12 with the
``meshforge`` wheel and one role's locked dependencies installed into it, plus
launchers that find the interpreter by a path relative to themselves::

    BUILDINFO.json           version, git sha, uv.lock digest, Python, role, sizes
    MANIFEST.sha256          every file's sha256 (``sha256sum -c`` format) + '#'
                             lines for symlinks, empty dirs, executables
    python/                  the interpreter (POSIX: bin/python3.12, lib/python3.12/...)
    bin/meshforge-agent      launchers (Windows: .cmd); controller role: meshforge-controller
    bin/compute
    uv -> python/bin/uv      POSIX only: the pinned uv (see "uv" below)
    pgsql/{bin,lib,share}    controller role only: the trimmed, sha256-pinned
                             PostgreSQL 16 of the managed database (V0.17 G2,
                             D390; scripts/pg_bundle.py)

and, next to it, ``meshforge-<ver>-<os>-<arch>.tar.xz`` (reproducible), whose size
is the **download** size checked against the role's budget (Q17:
``packaging/pins.toml [size_budget_mb]``).

Inputs, all pinned so the same commit gives the same bundle (``--diff`` two
builds to check):

* **Python** - the python-build-standalone ``install_only_stripped`` archive named
  in ``packaging/pins.toml`` (URL + sha256, never "latest"), downloaded into the
  cache and checked before use. Its version is checked again by running it.
* **Dependencies** - ``uv export --frozen --extra <role>`` (hashes from ``uv.lock``)
  installed with ``--require-hashes --no-deps --only-binary :all:`` for a fixed
  target platform (``--python-platform``: manylinux_2_28 on Linux), so the build
  machine's glibc does not choose the wheels.
* **meshforge wheel** - ``uv build --wheel`` with the hash-pinned build backend in
  ``scripts/bundle_build_constraints.txt`` and ``SOURCE_DATE_EPOCH`` = the HEAD
  commit time. Never editable. The controller role first builds the dashboard
  (``npm run build``; SP3 F4) or fails clearly.
* **uv** - the ``uv`` wheel from ``uv.lock`` (agent extra), installed like every
  other dependency: the agent's env builder already runs it via
  ``uv.find_uv_bin()`` (D86). ``pins.toml [uv] version`` must equal the lock's.

Relocation: wheel console scripts get an absolute ``#!`` line from the installer;
they are rewritten to the ``/bin/sh`` trampoline python-build-standalone uses for
``pip`` (and their RECORD rows rehashed), ``direct_url.json`` / ``uv_cache.json``
are dropped, bytecode is compiled with ``unchecked-hash`` pycs, the build path
stripped from ``co_filename`` and ``PYTHONHASHSEED=0``, modes/mtimes are
normalised, and every file is scanned for the build machine's paths - including
the trust-store paths of its environment (``SSL_CERT_FILE``... SP3 F3) - a hit
fails the build.

Windows (V0.17 G4): the same steps on the ``windows-latest`` runner with
``--os windows`` (layout ``python/python.exe``, ``python/Lib/site-packages``,
``bin/*.cmd`` launchers); the wheels' ``Scripts/*.exe`` console-script
trampolines (they name the build interpreter) and the ``.py`` scripts whose
``#!`` names it are dropped; pywin32 needs no post-install (its ``.pth`` file
and ``pywin32_system32`` DLL folder work with ``-I``; the build imports
``win32service`` + ``servicemanager`` with ``-I`` to prove it). The installer
(``packaging/windows/``, ``scripts/build_windows_installer.py``) wraps the
folder.

Usage (from the repo, in the project venv with the agent extra)::

    uv run python scripts/build_bundle.py --role agent --out dist/
    uv run python scripts/build_bundle.py --verify dist/meshforge-...-linux-x86_64
    uv run python scripts/build_bundle.py --diff dist/a/meshforge-... dist/b/meshforge-...

Exit codes: 0 ok; 1 build/verify failed or bundles differ; 2 usage error.

The pure parts (layouts, launcher text, ``#!``/RECORD rewrites, leak scan,
manifest, pins, the archive) are in ``scripts/bundle_lib.py``. Windows: the
OS-specific parts are :class:`bundle_lib.Layout`, ``launcher_windows`` and the
``layout.posix`` branches here, built and installed by the ``package-windows``
CI job (V0.17 G4).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import urllib.request
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

_SCRIPTS: Final = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:  # loaded by path (tests) rather than run
    sys.path.insert(0, str(_SCRIPTS))

import pg_bundle  # noqa: E402
from bundle_lib import (  # noqa: E402
    BUILDINFO_NAME,
    MANIFEST_NAME,
    BundleError,
    Download,
    EntryPoint,
    Layout,
    Manifest,
    bundle_name,
    check_no_secrets,
    check_size_budget,
    deterministic_tar_xz,
    diff_entries,
    diff_meta,
    find_leaks,
    launcher_text,
    layout_for,
    leak_needles,
    load_pins,
    manifest_text,
    normalise_arch,
    parse_entry_points,
    parse_manifest_text,
    pinned,
    read_manifest,
    rewrite_shebang,
    scan_tree,
    script_names,
    sha256_file,
    update_record,
    verify_bundle,
)

REPO: Final = Path(__file__).resolve().parent.parent
BUILD_CONSTRAINTS: Final = REPO / "scripts" / "bundle_build_constraints.txt"
PINS: Final = REPO / "packaging" / "pins.toml"

Role = Literal["agent", "controller"]
ROLE_EXTRAS: Final[Mapping[str, tuple[str, ...]]] = {
    "agent": ("agent",),
    # V1.0 G7 (D423): the installed controller trains the ML planner's models
    # (retrain task, `compute planner ml compare/classify`) - without the `ml`
    # extra every train exits 2 "install the ml extra" (L4 lab day blocked).
    "controller": ("controller", "ml"),
}
ROLE_COMMANDS: Final[Mapping[str, tuple[str, ...]]] = {
    # V0.17 G3: the app-menu launcher of the local console
    "agent": ("meshforge-agent", "compute", "meshforge-console-open"),
    "controller": ("meshforge-controller", "compute"),
}
DASHBOARD_INDEX: Final = "meshforge/controller/web/dist/index.html"
CONSOLE_INDEX: Final = "meshforge/agent/console/dist/index.html"
"""V0.17 G3 (D391): the agent's local console UI, built by the same `npm run build`."""
ROLE_UI: Final[Mapping[str, str]] = {
    "agent": CONSOLE_INDEX,
    "controller": DASHBOARD_INDEX,
}
TRUST_ENV: Final = (
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "PIP_CERT",
    "NODE_EXTRA_CA_CERTS",
)
"""SP3 F3: a build host's trust-store paths. The build itself may use them (a
corporate proxy needs them), but none may end up inside the bundle."""


# ------------------------------------------------------------- build helpers
def clean_env(extra: Mapping[str, str]) -> dict[str, str]:
    """The environment for every build tool: no user ``UV_*`` / ``PIP_*`` /
    ``PYTHON*`` settings, no bytecode writes, ``extra`` on top."""
    keep = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("UV_", "PIP_", "PYTHON", "VIRTUAL_ENV", "CONDA"))
    }
    return {
        **keep,
        "PYTHONDONTWRITEBYTECODE": "1",
        "UV_NO_PROGRESS": "1",
        "UV_PYTHON_DOWNLOADS": "never",
        **extra,
    }


def run(
    argv: Sequence[str | Path],
    *,
    env: Mapping[str, str],
    what: str,
    cwd: Path | None = None,
) -> str:
    """Run a build tool; stdout on success, ``BundleError`` with its stderr tail."""
    try:
        done = subprocess.run(
            [str(a) for a in argv],
            env=dict(env),
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise BundleError(
            f"{what} could not start: {exc}", reason="tool_failed"
        ) from None
    if done.returncode != 0:
        tail = (done.stderr or done.stdout)[-2000:]
        raise BundleError(
            f"{what} failed (exit {done.returncode}):\n{tail}", reason="tool_failed"
        )
    return done.stdout


def project_version(repo: Path) -> str:
    data = tomllib.loads((repo / "pyproject.toml").read_text(encoding="utf-8"))
    return str(data["project"]["version"])


def locked_version(lock_text: str, package: str) -> str | None:
    """The version ``uv.lock`` pins for ``package``."""
    match = re.search(
        rf'^\[\[package\]\]\nname = "{re.escape(package)}"\nversion = "([^"]+)"',
        lock_text,
        re.MULTILINE,
    )
    return match.group(1) if match else None


def source_date_epoch(repo: Path) -> int:
    """``SOURCE_DATE_EPOCH`` if set, else the HEAD commit time."""
    if value := os.environ.get("SOURCE_DATE_EPOCH"):
        return int(value)
    git = shutil.which("git")
    if git is None:
        raise BundleError(
            "set SOURCE_DATE_EPOCH (no git to read it)", reason="no_epoch"
        )
    out = run(
        [git, "-C", repo, "log", "-1", "--format=%ct"], env=os.environ, what="git log"
    )
    return int(out.strip())


def git_state(repo: Path) -> tuple[str, bool]:
    """``(HEAD sha, tracked files modified)``; ``MESHFORGE_GIT_SHA`` overrides
    for a source tarball without ``.git`` (then ``dirty`` is unknown = False)."""
    if value := os.environ.get("MESHFORGE_GIT_SHA"):
        return value.strip(), False
    git = shutil.which("git")
    if git is None:
        return "unknown", False
    try:
        sha = run([git, "-C", repo, "rev-parse", "HEAD"], env=os.environ, what="git")
        status = run(
            [git, "-C", repo, "status", "--porcelain", "--untracked-files=no"],
            env=os.environ,
            what="git status",
        )
    except BundleError:
        return "unknown", False
    return sha.strip(), bool(status.strip())


def build_uv() -> tuple[str, str]:
    """The lock-pinned uv binary (from the project venv's ``uv`` wheel) + version."""
    try:
        import uv  # the project's own pinned uv (agent extra)

        path = uv.find_uv_bin()
    except (ImportError, FileNotFoundError):
        raise BundleError(
            "run in the project venv with the agent extra (the uv wheel): "
            "uv run --extra agent python scripts/build_bundle.py ...",
            reason="no_uv",
        ) from None
    version = run([path, "--version"], env=os.environ, what="uv --version").split()[1]
    return path, version


# ------------------------------------------------------------------ python
def fetch(download: Download, cache: Path) -> Path:
    """``download`` in ``cache/downloads`` with its sha256 checked (a cached file
    is re-checked; a mismatch is deleted and refused, never used)."""
    folder = cache / "downloads"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / download.file_name
    if not path.exists():
        part = path.with_name(path.name + ".part")
        try:
            with (
                urllib.request.urlopen(download.url, timeout=120) as response,  # noqa: S310 - https pin
                part.open("wb") as out,
            ):
                shutil.copyfileobj(response, out, 1 << 20)
        except OSError as exc:
            part.unlink(missing_ok=True)
            raise BundleError(
                f"download of {download.url} failed: {exc}", reason="download_failed"
            ) from None
        part.rename(path)
    digest = sha256_file(path)
    if digest != download.sha256:
        path.unlink()
        raise BundleError(
            f"{download.file_name}: sha256 {digest} is not the pinned "
            f"{download.sha256} (deleted; check packaging/pins.toml)",
            reason="sha256_mismatch",
        )
    return path


STRIP_POSIX: Final[tuple[tuple[str, str], ...]] = (
    # (glob relative to python/, why it is not shipped)
    ("bin/pip*", "uv installs; EXTERNALLY-MANAGED stays, so pip would refuse anyway"),
    ("lib/python3.12/site-packages/pip", "as above"),
    ("lib/python3.12/site-packages/pip-*.dist-info", "as above"),
    ("lib/python3.12/ensurepip", "bundles a pip wheel; uv venv does not need it"),
    ("bin/idle3*", "IDE"),
    ("lib/python3.12/idlelib", "IDE"),
    ("bin/2to3*", "Python 2 migration tool"),
    ("lib/python3.12/lib2to3", "Python 2 migration tool (deprecated)"),
    ("lib/python3.12/turtledemo", "demo"),
    ("lib/python3.12/tkinter", "GUI toolkit: nothing in MeshForge uses Tk"),
    ("lib/python3.12/lib-dynload/_tkinter*", "GUI toolkit"),
    ("lib/tcl*", "GUI toolkit"),
    ("lib/tk*", "GUI toolkit"),
    ("lib/itcl*", "GUI toolkit"),
    ("lib/thread*", "GUI toolkit (Tcl threads)"),
    ("lib/libtcl*", "GUI toolkit"),
    ("lib/libtk*", "GUI toolkit"),
    ("share/man", "man pages"),
    # bin/python3.12 links libpython statically (ldd: libc/libm/... only); the
    # shared copy exists for programs that embed Python. Nothing here embeds it;
    # SP3 (d) builds job venvs from the bundle without it. 32 MB.
    ("lib/libpython3*.so*", "embedding only; the interpreter is static"),
)
STRIP_WINDOWS: Final[tuple[tuple[str, str], ...]] = (
    # V0.17 G4: (glob relative to python/, why) - the same choices as POSIX.
    ("Scripts/pip*", "uv installs; pip's launchers name the build interpreter"),
    ("Lib/site-packages/pip", "uv installs"),
    ("Lib/site-packages/pip-*.dist-info", "uv installs"),
    ("Lib/ensurepip", "bundles a pip wheel; uv venv does not need it"),
    ("Lib/idlelib", "IDE"),
    ("Lib/lib2to3", "Python 2 migration tool (deprecated)"),
    ("Lib/turtledemo", "demo"),
    ("Lib/tkinter", "GUI toolkit: nothing in MeshForge uses Tk"),
    ("Lib/test", "CPython's own test suite"),
    ("DLLs/_tkinter.pyd", "GUI toolkit"),
    ("DLLs/tcl*.dll", "GUI toolkit"),
    ("DLLs/tk*.dll", "GUI toolkit"),
    ("tcl", "GUI toolkit"),
)


def install_python(archive: Path, root: Path, layout: Layout) -> None:
    """Unpack the PBS archive (top folder ``python/``) into ``root``, safely
    (``tarfile`` ``data`` filter: no absolute paths, no escaping links), drop
    its bytecode, and strip what MeshForge does not use."""
    root.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as tar:
        names = tar.getnames()
        if not names or any(n.split("/", 1)[0] != "python" for n in names):
            raise BundleError(
                f"{archive.name} is not a python-build-standalone install_only "
                "archive (expected one top folder 'python/')",
                reason="bad_python",
            )
        tar.extractall(root, filter="data")
    python = root / "python"
    for cache in sorted(python.rglob("__pycache__")):
        shutil.rmtree(cache)
    for pattern, _why in STRIP_POSIX if layout.posix else STRIP_WINDOWS:
        for path in sorted(python.glob(pattern)):
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()


def check_python_version(root: Path, layout: Layout, version: str) -> None:
    out = run(
        [
            root / layout.python_exe,
            "-I",
            "-c",
            "import sys; print(sys.version.split()[0])",
        ],
        env=clean_env({}),
        what="the bundled python",
    ).strip()
    if out != version:
        raise BundleError(
            f"the pinned archive runs Python {out}, pins.toml says {version}",
            reason="bad_python",
        )


# ------------------------------------------------------------------- wheel
def ensure_dashboard(repo: Path, *, build: bool) -> None:
    """SP3 F4: the controller wheel must carry the dashboard: run the frontend
    build (``npm ci`` first when ``node_modules`` is missing)."""
    if not build:
        return
    npm = shutil.which("npm")
    if npm is None:
        raise BundleError(
            "the controller bundle needs the dashboard and npm is not installed: "
            "install Node.js 22 (see frontend/package.json engines) or build the "
            "dashboard elsewhere and pass --skip-dashboard-build",
            reason="no_dashboard",
        )
    frontend = repo / "frontend"
    env = dict(os.environ)
    if not (frontend / "node_modules").is_dir():
        run([npm, "--prefix", frontend, "ci"], env=env, what="npm ci")
    run([npm, "--prefix", frontend, "run", "build"], env=env, what="npm run build")


def build_wheel(uv: str, repo: Path, work: Path, env: Mapping[str, str]) -> Path:
    out = work / "wheel"
    # Relative paths from cwd=repo: uv 0.12 mis-parses a --build-constraints path
    # that contains a space ("File not found: /home/.../FINAL").
    run(
        [
            uv,
            "build",
            "--no-config",
            "--wheel",
            "--out-dir",
            out,
            "--build-constraints",
            BUILD_CONSTRAINTS.relative_to(repo).as_posix(),
            "--require-hashes",
            ".",
        ],
        env=env,
        what="uv build --wheel",
        cwd=repo,
    )
    wheels = sorted(out.glob("meshforge-*.whl"))
    if len(wheels) != 1:
        raise BundleError(f"expected one wheel, got {wheels}", reason="tool_failed")
    return wheels[0]


def wheel_text(wheel: Path, suffix: str) -> str | None:
    with zipfile.ZipFile(wheel) as zf:
        for name in zf.namelist():
            if name.endswith(suffix):
                return zf.read(name).decode("utf-8")
    return None


def wheel_has(wheel: Path, name: str) -> bool:
    with zipfile.ZipFile(wheel) as zf:
        return name in zf.namelist()


def requirements(
    uv: str, repo: Path, extras: Sequence[str], wheel: Path, env: Mapping[str, str]
) -> str:
    """The locked, hashed requirements of ``extras`` + the wheel, by its sha256."""
    argv: list[str | Path] = [
        uv,
        "export",
        "--frozen",
        "--no-config",
        "--no-dev",
        "--no-emit-project",
        "--format",
        "requirements-txt",
        "--no-header",
        "--no-annotate",
        "--project",
        repo,
    ]
    for extra in extras:
        argv += ["--extra", extra]
    locked = run(argv, env=env, what="uv export")
    digest = sha256_file(wheel)
    return locked + f"meshforge @ {wheel.as_uri()} --hash=sha256:{digest}\n"


def install(
    uv: str, root: Path, layout: Layout, reqs: Path, env: Mapping[str, str]
) -> None:
    run(
        [
            uv,
            "pip",
            "install",
            "--no-config",
            "--python",
            root / layout.python_exe,
            "--break-system-packages",
            "--require-hashes",
            "--no-deps",
            "--only-binary",
            ":all:",
            "--link-mode",
            "copy",
            "--python-platform",
            layout.python_platform,
            "-r",
            reqs,
        ],
        env=env,
        what="uv pip install",
    )


DROPPED_DIST_INFO: Final = ("direct_url.json", "uv_cache.json")
"""Installer notes about a local wheel: ``direct_url.json`` names its build path,
``uv_cache.json`` holds the install time (the only build-to-build difference SP3
found). Neither is needed at run time; RECORD rows are dropped with them."""


def relocate_installed(root: Path, layout: Layout) -> list[str]:
    """Make the installed wheels relocatable; returns what changed (for the log)."""
    site = layout.site_packages_path(root)
    changed: dict[Path, bytes | None] = {}
    for name in DROPPED_DIST_INFO:
        for path in sorted(site.glob(f"*.dist-info/{name}")):
            path.unlink()
            changed[path] = None
    if layout.posix:
        python_abs = str(root / layout.python_exe).encode()
        python_name = Path(layout.python_exe).name
        scripts = root / layout.scripts_dir
        for path in sorted(scripts.iterdir()):
            if path.is_symlink() or not path.is_file():
                continue
            data = path.read_bytes()
            new = rewrite_shebang(data, python_abs, python_name)
            if new is not None:
                path.write_bytes(new)
                changed[path] = new
    else:
        # V0.17 G4: uv's console-script .exe trampolines (and any .py script
        # whose #! it rewrote) name the absolute build interpreter: dropped. The
        # bundle's own launchers are bin/*.cmd; binaries a wheel ships as data
        # (uv.exe) are kept.
        for path in windows_scripts_to_drop(root, layout):
            path.unlink()
            changed[path] = None
    for record in sorted(site.glob("*.dist-info/RECORD")):
        text = record.read_text(encoding="utf-8")
        new_text = update_record(text, site, changed)
        if new_text != text:
            record.write_text(new_text, encoding="utf-8")
    return sorted(p.relative_to(root).as_posix() for p in changed)


def windows_scripts_to_drop(root: Path, layout: Layout) -> list[Path]:
    """``Scripts/<name>.exe`` / ``<name>-script.py`` of every installed entry
    point, and text files there whose ``#!`` line names the build interpreter."""
    scripts = root / layout.scripts_dir
    if not scripts.is_dir():
        return []
    site = layout.site_packages_path(root)
    names: set[str] = set()
    for entry_points in sorted(site.glob("*.dist-info/entry_points.txt")):
        names |= script_names(entry_points.read_text(encoding="utf-8"))
    python_abs = str(root / layout.python_exe).encode()
    drop: list[Path] = []
    for path in sorted(scripts.iterdir()):
        if not path.is_file():
            continue
        stem = path.name.lower()
        if any(
            stem in (f"{n.lower()}.exe", f"{n.lower()}-script.py", f"{n.lower()}.pyw")
            for n in names
        ):
            drop.append(path)
            continue
        head = path.read_bytes()[:4096]
        if head.startswith(b"#!") and python_abs.lower() in head.lower():
            drop.append(path)
    return drop


STRIP_SITE: Final[Mapping[str, tuple[tuple[str, str], ...]]] = {
    # V0.17 G2 (Lead, G2 progress note item 14): the controller .deb (nfpm's xz)
    # must fit the Q17 150 MB budget. Globs relative to site-packages; nothing
    # MeshForge runs imports them (the installed controller is run end to end in
    # tests/component/packaging/test_v017_g2_controller_deb.py).
    "agent": (),
    "controller": (
        ("numpy/**/tests", "numpy's own test suites"),
        ("numpy/_core/include", "C headers for building extensions"),
        ("pyarrow/tests", "pyarrow's own test suite"),
        ("pyarrow/include", "C++ headers for building extensions"),
        ("pyarrow/src", "C++ sources for building extensions"),
        ("greenlet/tests", "greenlet's own test suite"),
        # V1.0 G7 (D423): the `ml` extra (scikit-learn + scipy, LightGBM,
        # XGBoost) is now in the controller bundle; these keep it in the budget.
        # The installed controller trains a model set end to end in
        # tests/component/packaging/test_v10_g7_controller_ml.py.
        ("scipy/**/tests", "scipy's own test suites"),
        ("sklearn/**/tests", "scikit-learn's own test suites"),
        ("xgboost/testing", "XGBoost's test helpers (never imported by xgboost)"),
        # Arrow Flight (RPC) and the S3 filesystem: native libraries loaded only
        # by pyarrow.flight / pyarrow._s3fs (pyarrow.fs guards that import);
        # MeshForge reads and writes local Parquet files only.
        ("pyarrow/libarrow_flight.so*", "Arrow Flight RPC (pyarrow.flight only)"),
        ("pyarrow/libarrow_python_flight.so*", "Arrow Flight Python glue"),
        ("pyarrow/_flight.*", "pyarrow.flight's extension module and source"),
        ("pyarrow/flight.py", "pyarrow.flight (its native parts are not shipped)"),
        ("pyarrow/libarrow_s3.so*", "the S3 filesystem (pyarrow._s3fs only)"),
        ("pyarrow/_s3fs.*", "the S3 filesystem's extension module and source"),
    ),
}
"""Per role: what of the installed wheels is not shipped (role -> (glob, why))."""


def strip_site_packages(
    root: Path, layout: Layout, rules: Sequence[tuple[str, str]]
) -> list[str]:
    """Delete what ``rules`` match in site-packages and drop the deleted files'
    RECORD rows (so ``RECORD`` still lists only shipped files). Returns the
    deleted files relative to ``root``, sorted."""
    site = layout.site_packages_path(root)
    removed: dict[Path, bytes | None] = {}
    for pattern, _why in rules:
        for path in sorted(site.glob(pattern)):
            if not os.path.lexists(path):
                continue  # inside a folder an earlier match removed
            if path.is_dir() and not path.is_symlink():
                for item in sorted(path.rglob("*")):
                    if item.is_symlink() or not item.is_dir():
                        removed[Path(os.path.normpath(item))] = None
                shutil.rmtree(path)
            else:
                removed[Path(os.path.normpath(path))] = None
                path.unlink()
    if removed:
        for record in sorted(site.glob("*.dist-info/RECORD")):
            text = record.read_text(encoding="utf-8")
            new_text = update_record(text, site, removed)
            if new_text != text:
                record.write_text(new_text, encoding="utf-8")
    return sorted(p.relative_to(root).as_posix() for p in removed)


COMPILE_SCRIPT: Final = """\
import compileall, py_compile, sys
ok = compileall.compile_dir(
    sys.argv[1], quiet=1, workers=1, stripdir=sys.argv[2], force=True,
    invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH,
)
sys.exit(0 if ok else 3)
"""


def compile_bytecode(root: Path, layout: Layout, env: Mapping[str, str]) -> list[str]:
    """Deterministic bytecode: ``unchecked-hash`` pycs (no source mtime), the build
    path stripped from ``co_filename``, ``PYTHONHASHSEED=0``, one worker (SP3:
    marshal back-references depend on what a worker compiled before). The program
    dir is read-only once installed, so without these every start would recompile
    in memory. Returns files that do not compile (warned, not fatal: some wheels
    ship deliberately invalid sources), relative to ``root``."""
    for cache in sorted((root / "python").rglob("__pycache__")):
        shutil.rmtree(cache)
    # PYTHONDONTWRITEBYTECODE stays set: py_compile ignores it, but the modules
    # compileall itself imports must not get import-time (mtime, absolute path) pycs.
    compile_env = {**env, "PYTHONHASHSEED": "0"}
    done = subprocess.run(
        [
            str(root / layout.python_exe),
            "-I",
            "-c",
            COMPILE_SCRIPT,
            str(root / layout.stdlib_dir),
            str(root),
        ],
        env=compile_env,
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode not in (0, 3):
        raise BundleError(
            f"compileall crashed:\n{done.stderr[-2000:]}", reason="tool_failed"
        )
    # Root-relative: the list goes into BUILDINFO.json, which must not name the
    # build dir (it would break reproducibility and the leak rule).
    found = re.findall(r"\*\*\* Error compiling '([^']+)'", done.stdout)
    return sorted({_root_relative(root, path) for path in found})


def _root_relative(root: Path, path: str) -> str:
    try:
        return Path(path).relative_to(root).as_posix()
    except ValueError:
        return Path(path).name


WINDOWS_IMPORT_CHECK: Final = (
    "import pywintypes, win32api, win32con, win32gui, win32security, "
    "win32service, win32serviceutil, servicemanager, win32ts, win32event, "
    "win32process; "
    "from win32com.shell import shell; print('pywin32 ok')"
)
"""V0.17 G4: what the service host and the installer helper import, run with
``-I`` from the finished bundle (no post-install, no registry)."""


def check_windows_imports(root: Path, layout: Layout, env: Mapping[str, str]) -> None:
    if layout.posix:
        return
    run(
        # -B: -I ignores PYTHONDONTWRITEBYTECODE, and an import-time pyc
        # (mtime-stamped) would break the bundle's reproducibility.
        [root / layout.python_exe, "-I", "-B", "-c", WINDOWS_IMPORT_CHECK],
        env=env,
        what="pywin32 import check (python -I)",
    )


def write_launchers(
    root: Path, layout: Layout, entries: Mapping[str, EntryPoint], role: str
) -> None:
    bin_dir = root / "bin"
    bin_dir.mkdir()
    for name in ROLE_COMMANDS[role]:
        entry = entries.get(name)
        if entry is None:
            raise BundleError(
                f"the wheel has no {name!r} entry point", reason="bad_entry_point"
            )
        path = bin_dir / (name + layout.launcher_suffix)
        path.write_bytes(launcher_text(entry, layout).encode("utf-8"))
        if layout.posix:
            path.chmod(0o755)
    uv_in_python = root / layout.scripts_dir / layout.uv_name
    if layout.posix and uv_in_python.exists():
        (root / layout.uv_name).symlink_to(os.path.relpath(uv_in_python, root))


def normalise_tree(root: Path, epoch: int) -> None:
    """Modes 0755/0644 (dirs 0755) and every mtime = ``epoch``, so an archive of the
    bundle (deb/tar) is reproducible too."""
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        here = Path(dirpath)
        for name in [*filenames, *dirnames]:
            path = here / name
            st = path.lstat()
            if stat.S_ISLNK(st.st_mode):
                os.utime(path, (epoch, epoch), follow_symlinks=False)
                continue
            path.chmod(
                0o755 if stat.S_ISDIR(st.st_mode) or st.st_mode & 0o111 else 0o644
            )
            os.utime(path, (epoch, epoch))
    root.chmod(0o755)
    os.utime(root, (epoch, epoch))


def _write_normalised(path: Path, text: str, epoch: int) -> None:
    path.write_text(text, encoding="utf-8", newline="\n")
    path.chmod(0o644)
    os.utime(path, (epoch, epoch))


def trust_store_paths(environ: Mapping[str, str]) -> list[str]:
    """SP3 F3: the build host's trust-store paths (leak needles)."""
    return [environ[name] for name in TRUST_ENV if environ.get(name)]


def uv_dirs(uv: str) -> list[str]:
    """The build user's own uv cache and managed-Python dirs (``uv cache dir`` /
    ``uv python dir`` in the caller's environment, not :func:`clean_env`): where
    the interpreter running this build and an sdist build would come from."""
    dirs: list[str] = []
    for what in ("cache", "python"):
        try:
            out = run([uv, what, "dir"], env=os.environ, what=f"uv {what} dir")
        except BundleError:
            continue  # best effort: the other needles still apply
        if out.strip():
            dirs.append(out.strip())
    return dirs


def build_machine_paths(
    *,
    work: Path,
    out: Path,
    repo: Path,
    cache: Path,
    environ: Mapping[str, str],
    interpreter: Sequence[str],
    uv: Sequence[str],
) -> list[str]:
    """The leak-check roots: the directories that exist only on *this* build
    machine and that a bundle referring to would not be relocatable (CI fix of
    the first V0.17 G4 run).

    * the staging dir (``work``), the output dir and the source checkout;
    * the bundle cache (Python archive, uv cache of the build, PostgreSQL);
    * the build interpreter's prefixes and the user's uv cache / Python dirs;
    * the trust-store paths (SP3 F3).

    Deliberately **not** the home directory: hash-checked third-party wheels
    embed *their* build machines' paths (``~/.cargo/registry`` in Rust
    extensions, ``~/AppData/Local/Temp/cibw-run-*`` in numpy's config, the
    ``/home/runner/work/<project>`` source roots in cyclonedx SBOMs), and a CI
    runner has the same home as the runner that built them. Those are inert
    debug strings in files we ship byte-for-byte, never paths the bundle uses.
    """
    return [
        str(work),
        str(out),
        str(repo),
        str(cache),
        *interpreter,
        *uv,
        *trust_store_paths(environ),
    ]


# ----------------------------------------------------------------- the build
@dataclass(frozen=True, slots=True)
class BuildOptions:
    role: Role
    out: Path
    cache: Path
    os_name: str | None = None
    arch: str | None = None
    extra_extras: tuple[str, ...] = ()
    force: bool = False
    allow_missing_dashboard: bool = False
    build_dashboard: bool = True
    archive: bool = True
    size_budget: bool = True
    pins: Path = PINS


def log(message: str) -> None:
    print(f"build_bundle: {message}", file=sys.stderr)


def target_layout(options: BuildOptions) -> tuple[Layout, str]:
    """This machine's layout; ``--os/--arch`` must name this machine (the build
    runs the bundled interpreter, so it is built on its target OS)."""
    host_os = "windows" if platform.system() == "Windows" else platform.system().lower()
    arch = normalise_arch(platform.machine())
    if options.os_name is not None and options.os_name != host_os:
        raise BundleError(
            f"--os {options.os_name} on a {host_os} machine: build each OS's bundle "
            "on that OS (CI has a runner per OS)",
            reason="unsupported",
        )
    if options.arch is not None and normalise_arch(options.arch) != arch:
        raise BundleError(
            f"--arch {options.arch} on a {arch} machine: no cross builds",
            reason="unsupported",
        )
    layout = layout_for(
        platform.system(),
        platform.machine(),
        experimental_windows=options.os_name == "windows",
    )
    return layout, arch


def build(options: BuildOptions) -> Path:
    layout, arch = target_layout(options)
    pins = load_pins(options.pins)
    python_pin = pinned(pins, "python", f"{layout.os_name}-{arch}")
    budget_table = pins.get("size_budget_mb")
    budget = budget_table.get(options.role) if isinstance(budget_table, dict) else None
    version = project_version(REPO)
    lock_text = (REPO / "uv.lock").read_text(encoding="utf-8")
    uv, uv_version = build_uv()
    pinned_uv = locked_version(lock_text, "uv")
    uv_table = pins.get("uv")
    uv_pin = uv_table.get("version") if isinstance(uv_table, dict) else None
    if pinned_uv != uv_version or uv_pin != uv_version:
        raise BundleError(
            f"uv {uv_version} is not the pin (uv.lock {pinned_uv}, pins.toml "
            f"{uv_pin}): `uv sync --frozen --extra agent`, and keep both pins equal",
            reason="uv_mismatch",
        )
    epoch = source_date_epoch(REPO)
    git_sha, git_dirty = git_state(REPO)
    name = bundle_name(version, layout, arch)
    out = options.out.resolve()
    final = out / name
    archive = out / f"{name}.tar.xz"
    if (final.exists() or (options.archive and archive.exists())) and not options.force:
        raise BundleError(f"{final} exists (use --force)", reason="exists")
    out.mkdir(parents=True, exist_ok=True)
    options.cache.mkdir(parents=True, exist_ok=True)
    extras = (*ROLE_EXTRAS[options.role], *options.extra_extras)

    if options.build_dashboard:
        log("building the dashboard and the agent console (npm run build)")
        ensure_dashboard(REPO, build=True)

    with tempfile.TemporaryDirectory(prefix=".bundle-", dir=out) as tmp:
        work = Path(tmp)
        root = work / name
        env = clean_env(
            {
                "UV_CACHE_DIR": str(options.cache / "uv"),
                "SOURCE_DATE_EPOCH": str(epoch),
            }
        )
        log(f"python {python_pin.version} ({python_pin.file_name})")
        install_python(fetch(python_pin, options.cache), root, layout)
        check_python_version(root, layout, python_pin.version)
        log("building the meshforge wheel")
        wheel = build_wheel(uv, REPO, work, env)
        ui = ROLE_UI[options.role]
        has_dashboard = wheel_has(wheel, ui)
        if not has_dashboard and not options.allow_missing_dashboard:
            raise BundleError(
                f"the wheel has no {ui.rsplit('/', 2)[0]} UI even after the frontend "
                "build; check `npm --prefix frontend run build`, or "
                "--allow-missing-dashboard",
                reason="no_dashboard",
            )
        entries = parse_entry_points(
            wheel_text(wheel, ".dist-info/entry_points.txt") or ""
        )
        reqs = work / "requirements.txt"
        reqs.write_text(requirements(uv, REPO, extras, wheel, env), encoding="utf-8")
        log(f"installing extras {', '.join(extras)} from uv.lock (hash-checked)")
        install(uv, root, layout, reqs, env)
        stripped = strip_site_packages(root, layout, STRIP_SITE[options.role])
        log(f"stripped {len(stripped)} installed files (tests, headers)")
        relocated = relocate_installed(root, layout)
        log(f"relocated {len(relocated)} installed files")
        bad = compile_bytecode(root, layout, env)
        for path in bad:
            log(f"warning: does not compile (shipped as source only): {path}")
        write_launchers(root, layout, entries, options.role)
        if options.role == "agent":
            check_windows_imports(root, layout, env)
        if options.role == "controller":
            # V0.17 G2 (D390): the managed PostgreSQL, trimmed, at <bundle>/pgsql.
            pg_pin_ = pg_bundle.pg_pin(pins, f"{layout.os_name}-{arch}")
            log(f"postgresql {pg_pin_.version} ({pg_pin_.file_name})")
            try:
                trimmed = pg_bundle.ensure_trimmed(pg_pin_, options.cache / "pg")
            except OSError as exc:
                raise BundleError(
                    f"PostgreSQL download failed: {exc}", reason="download_failed"
                ) from None
            pg_bundle.install_pgsql(trimmed, root)
            postgresql = {
                "version": pg_pin_.version,
                "sha256": pg_pin_.sha256,
                "urls": list(pg_pin_.urls),
            }
        else:
            postgresql = None
        normalise_tree(root, epoch)

        needles = leak_needles(
            build_machine_paths(
                work=work,
                out=out,
                repo=REPO,
                cache=options.cache,
                environ=os.environ,
                interpreter=sorted({sys.prefix, sys.base_prefix, sys.exec_prefix}),
                uv=uv_dirs(uv),
            )
        )
        leaks = find_leaks(root, needles)
        if leaks:
            shown = "\n".join(f"  {leak.path}: {leak.needle}" for leak in leaks[:20])
            raise BundleError(
                f"{len(leaks)} file(s) name a build-machine path:\n{shown}",
                reason="path_leak",
            )
        # V1.0 G3 (D413): no key, token, secret file or test folder ships
        check_no_secrets(root)
        files = scan_tree(root)
        manifest = Manifest(
            meta={
                "name": name,
                "version": version,
                "role": options.role,
                "extras": list(extras),
                "os": layout.os_name,
                "arch": arch,
                "python_platform": layout.python_platform,
                "git_sha": git_sha,
                "git_dirty": git_dirty,
                "python": {
                    "version": python_pin.version,
                    "url": python_pin.url,
                    "sha256": python_pin.sha256,
                },
                "uv": uv_version,
                "uv_lock_sha256": hashlib.sha256(lock_text.encode("utf-8")).hexdigest(),
                "wheel_sha256": sha256_file(wheel),
                "dashboard": has_dashboard,
                "postgresql": postgresql,
                "source_date_epoch": epoch,
                "installed_bytes": sum(int(str(f.get("size", 0))) for f in files),
                "uncompiled": bad,
            },
        )
        _write_normalised(root / BUILDINFO_NAME, manifest.buildinfo_json(), epoch)
        manifest.files = scan_tree(root)  # BUILDINFO.json included
        _write_normalised(root / MANIFEST_NAME, manifest_text(manifest.files), epoch)
        if final.exists():
            shutil.rmtree(final)
        root.rename(final)

    log(f"built {final} ({manifest.meta['installed_bytes']} bytes on disk)")
    if options.archive:
        log(f"writing {archive.name} (xz)")
        size = deterministic_tar_xz(final, archive, epoch)
        log(f"download size {size / 1e6:.1f} MB")
        if options.size_budget and isinstance(budget, int | float):
            check_size_budget(size, float(budget), archive.name)
    return final


# ----------------------------------------------------------------------- CLI
def _load_side(path: Path) -> tuple[list[dict[str, object]], dict[str, object]]:
    """A bundle dir (MANIFEST.sha256 + BUILDINFO.json) or a MANIFEST.sha256."""
    if path.is_dir():
        entries = read_manifest(path)
        try:
            meta = json.loads((path / BUILDINFO_NAME).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = {}
        return entries, meta if isinstance(meta, dict) else {}
    try:
        return parse_manifest_text(path.read_text(encoding="utf-8")), {}
    except OSError as exc:
        raise BundleError(f"cannot read {path}: {exc}", reason="no_manifest") from None


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--role", choices=sorted(ROLE_EXTRAS))
    mode.add_argument(
        "--verify",
        type=Path,
        metavar="BUNDLE",
        help="check a bundle against its MANIFEST.sha256",
    )
    mode.add_argument(
        "--diff",
        nargs=2,
        type=Path,
        metavar=("A", "B"),
        help="compare two bundles (folders) or two MANIFEST.sha256 files",
    )
    parser.add_argument("--out", type=Path, help="output directory (build)")
    parser.add_argument("--os", dest="os_name", choices=["linux", "windows"])
    parser.add_argument("--arch", choices=["x86_64"])
    parser.add_argument(
        "--cache",
        type=Path,
        default=Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
        / "meshforge-bundle",
        help="downloads cache (python + uv wheels); default ~/.cache/meshforge-bundle",
    )
    parser.add_argument(
        "--extra", action="append", default=[], help="add an extra (e.g. ml)"
    )
    parser.add_argument(
        "--force", action="store_true", help="replace an existing bundle"
    )
    parser.add_argument("--allow-missing-dashboard", action="store_true")
    parser.add_argument(
        "--skip-dashboard-build",
        action="store_true",
        help="controller: use the dashboard already built (no npm run build)",
    )
    parser.add_argument(
        "--no-archive",
        action="store_true",
        help="skip the .tar.xz (and with it the download-size check)",
    )
    parser.add_argument(
        "--no-size-budget",
        action="store_true",
        help="write the .tar.xz but do not fail on its size (e.g. with --extra ml)",
    )
    parser.add_argument("--pins", type=Path, default=PINS, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.role and args.out is None:
        parser.error("--role needs --out")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.diff:
            (entries_a, meta_a), (entries_b, meta_b) = (
                _load_side(p) for p in args.diff
            )
            differences = diff_meta(meta_a, meta_b) + diff_entries(entries_a, entries_b)
            for line in differences:
                print(line)
            print(f"{len(differences)} difference(s)")
            return 1 if differences else 0
        if args.verify:
            differences = verify_bundle(args.verify)
            for line in differences:
                print(line)
            print("intact" if not differences else f"{len(differences)} difference(s)")
            return 1 if differences else 0
        build(
            BuildOptions(
                role=args.role,
                out=args.out,
                cache=args.cache,
                os_name=args.os_name,
                arch=args.arch,
                extra_extras=tuple(args.extra),
                force=args.force,
                allow_missing_dashboard=args.allow_missing_dashboard,
                build_dashboard=not args.skip_dashboard_build,
                archive=not args.no_archive,
                size_budget=not args.no_size_budget,
                pins=args.pins,
            )
        )
    except BundleError as exc:
        print(f"build_bundle: error ({exc.reason}): {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
