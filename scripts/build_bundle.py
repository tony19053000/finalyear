"""Build the D384 relocatable runtime bundle (V0.17 G0 spike SP3, kept for G1).

``meshforge-<ver>-<os>-<arch>/`` = a python-build-standalone CPython 3.12 with the
``meshforge`` wheel and one role's locked dependencies installed into it, plus
launchers that find the interpreter by a path relative to themselves::

    MANIFEST.json            every file: path, size, sha256, mode (not itself)
    python/                  the interpreter (POSIX: bin/python3.12, lib/python3.12/...)
    bin/meshforge-agent      launchers (Windows: .cmd); controller role: meshforge-controller
    bin/compute
    uv -> python/bin/uv      POSIX only: the pinned uv (see "uv" below)

Inputs, all pinned so the same commit gives the same bundle (``--diff`` two
builds' manifests to check):

* **Python** - ``uv python install <PYTHON_VERSION>`` into a private directory by the
  ``uv`` that ``uv.lock`` pins (this script must run in the project venv, where the
  ``uv`` wheel provides it). uv downloads the python-build-standalone
  ``install_only_stripped`` archive and checks it against the sha256 compiled into
  that uv release, so (uv version, Python version) fixes the archive; its URL and
  PBS release are recorded in the manifest. uv rewrites the archive's ``/install``
  placeholder to the install path in text files (``_sysconfigdata``,
  ``python3.12-config``, ``Makefile``, ``*.pc``); the copy rewrites it back.
* **Dependencies** - ``uv export --frozen --extra <role>`` (hashes from ``uv.lock``)
  installed with ``--require-hashes --no-deps --only-binary :all:`` for a fixed
  target platform (``--python-platform``: manylinux_2_28 on Linux), so the build
  machine's glibc does not choose the wheels.
* **meshforge wheel** - ``uv build --wheel`` with the hash-pinned build backend in
  ``scripts/bundle_build_constraints.txt`` and ``SOURCE_DATE_EPOCH``.
* **uv** - the ``uv`` wheel from ``uv.lock`` (agent extra), installed like every
  other dependency: the agent's env builder already runs it via
  ``uv.find_uv_bin()`` (D86). POSIX gets a relative ``uv`` symlink at the top for
  operators; no second copy (50 MB). The controller role has no uv.

Relocation: wheel console scripts get an absolute ``#!`` line from the installer;
they are rewritten to the ``/bin/sh`` trampoline python-build-standalone uses for
``pip`` (and their RECORD rows rehashed), ``direct_url.json`` (the wheel's build
path) is dropped, bytecode is compiled with ``unchecked-hash`` pycs, the build path
stripped from ``co_filename`` and ``PYTHONHASHSEED=0`` (frozenset constants), and
every file is scanned for the build machine's paths - a hit fails the build.

Usage (from the repo, in the project venv)::

    uv run python scripts/build_bundle.py --role agent --out dist/bundle
    uv run python scripts/build_bundle.py --verify dist/bundle/meshforge-...-linux-x86_64
    uv run python scripts/build_bundle.py --diff a/MANIFEST.json b/MANIFEST.json

Exit codes: 0 ok; 1 build/verify failed or manifests differ; 2 usage error.

The pure parts (layouts, launcher text, ``#!``/RECORD rewrites, leak scan,
manifest) are in ``scripts/bundle_lib.py``; this file runs the tools. Windows: the
OS-specific parts are :class:`bundle_lib.Layout`, ``launcher_windows`` and the
``layout.posix`` branches here (strip list, shebangs, the ``uv`` symlink). They are
written but untested until a Windows runner exists (SP1) - ``layout_for`` refuses
Windows unless ``--experimental-windows``.
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
import tempfile
import tomllib
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

_SCRIPTS: Final = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:  # loaded by path (tests) rather than run
    sys.path.insert(0, str(_SCRIPTS))

from bundle_lib import (  # noqa: E402
    MANIFEST_NAME,
    PLACEHOLDER,
    BundleError,
    EntryPoint,
    Layout,
    Manifest,
    bundle_name,
    diff_manifests,
    find_leaks,
    launcher_text,
    layout_for,
    leak_needles,
    normalise_arch,
    parse_entry_points,
    replace_in_text_files,
    rewrite_shebang,
    scan_tree,
    sha256_file,
    update_record,
    verify_bundle,
)

REPO: Final = Path(__file__).resolve().parent.parent
BUILD_CONSTRAINTS: Final = REPO / "scripts" / "bundle_build_constraints.txt"
PYTHON_VERSION: Final = "3.12.14"
"""The bundled CPython. Bump deliberately (a new PBS build changes every hash)."""

Role = Literal["agent", "controller"]
ROLE_EXTRAS: Final[Mapping[str, tuple[str, ...]]] = {
    "agent": ("agent",),
    "controller": ("controller",),
}
ROLE_COMMANDS: Final[Mapping[str, tuple[str, ...]]] = {
    "agent": ("meshforge-agent", "compute"),
    "controller": ("meshforge-controller", "compute"),
}
DASHBOARD_INDEX: Final = "meshforge/controller/web/dist/index.html"


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
    done = subprocess.run(
        [str(a) for a in argv],
        env=dict(env),
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
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


@dataclass(frozen=True, slots=True)
class PythonSource:
    prefix: Path
    version: str
    url: str
    pbs_release: str


def obtain_python(uv: str, cache: Path) -> PythonSource:
    """``uv python install`` into a private dir under ``cache`` (never the user's
    managed pythons, which may have had packages installed into them)."""
    env = clean_env(
        {
            "UV_PYTHON_INSTALL_DIR": str(cache / "python"),
            "UV_CACHE_DIR": str(cache / "uv"),
            "UV_PYTHON_DOWNLOADS": "manual",
        }
    )
    request = f"cpython-{PYTHON_VERSION}"
    listing = json.loads(
        run(
            [
                uv,
                "python",
                "list",
                request,
                "--no-config",
                "--managed-python",
                "--output-format",
                "json",
                "--show-urls",
                "--all-versions",
            ],
            env=env,
            what="uv python list",
        )
    )
    urls = [e["url"] for e in listing if e.get("url") and e.get("variant") == "default"]
    run(
        [uv, "python", "install", "--no-config", "--no-bin", request],
        env=env,
        what=f"uv python install {request}",
    )
    found = run(
        [uv, "python", "find", "--no-config", "--managed-python", request],
        env=env,
        what="uv python find",
    ).strip()
    exe = Path(found)
    prefix = Path(
        run(
            [exe, "-I", "-c", "import sys; print(sys.prefix)"], env=env, what="python"
        ).strip()
    )
    build = (
        (prefix / "BUILD").read_text().strip() if (prefix / "BUILD").exists() else ""
    )
    url = urls[0] if urls else ""
    in_url = re.search(r"/download/(\d+)/", url)
    release = build or (in_url.group(1) if in_url else "")
    return PythonSource(
        prefix=prefix, version=PYTHON_VERSION, url=url, pbs_release=release
    )


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


def _ignore_bytecode(_dir: str, names: list[str]) -> set[str]:
    return {n for n in names if n == "__pycache__" or n.endswith((".pyc", ".pyo"))}


def copy_python(source: PythonSource, dest: Path, layout: Layout) -> None:
    """Copy the interpreter (symlinks kept, no bytecode), strip unused parts, and
    undo uv's ``/install`` rewrite."""
    shutil.copytree(source.prefix, dest, symlinks=True, ignore=_ignore_bytecode)
    if layout.posix:
        for pattern, _why in STRIP_POSIX:
            for path in sorted(dest.glob(pattern)):
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                else:
                    path.unlink()
    for prefix in {str(source.prefix), os.path.realpath(source.prefix)}:
        replace_in_text_files(dest, prefix.encode(), PLACEHOLDER.encode())


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
    # Windows (untested): uv's console-script .exe trampolines embed the absolute
    # interpreter path - SP1 must decide whether to delete them (the bundle's own
    # launchers do not use them) or regenerate them.
    for record in sorted(site.glob("*.dist-info/RECORD")):
        text = record.read_text(encoding="utf-8")
        new_text = update_record(text, site, changed)
        if new_text != text:
            record.write_text(new_text, encoding="utf-8")
    return sorted(p.relative_to(root).as_posix() for p in changed)


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
    path stripped from ``co_filename``, ``PYTHONHASHSEED=0``. The program dir is
    read-only once installed, so without these every start would recompile in
    memory. Returns files that do not compile (warned, not fatal: some wheels ship
    deliberately invalid sources, e.g. Python-2 test data)."""
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
    return sorted(set(re.findall(r"\*\*\* Error compiling '([^']+)'", done.stdout)))


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


# ----------------------------------------------------------------- the build
@dataclass(frozen=True, slots=True)
class BuildOptions:
    role: Role
    out: Path
    cache: Path
    extra_extras: tuple[str, ...] = ()
    force: bool = False
    allow_missing_dashboard: bool = False
    experimental_windows: bool = False


def log(message: str) -> None:
    print(f"build_bundle: {message}", file=sys.stderr)


def build(options: BuildOptions) -> Path:
    layout = layout_for(
        platform.system(),
        platform.machine(),
        experimental_windows=options.experimental_windows,
    )
    arch = normalise_arch(platform.machine())
    version = project_version(REPO)
    lock_text = (REPO / "uv.lock").read_text(encoding="utf-8")
    uv, uv_version = build_uv()
    if (pinned := locked_version(lock_text, "uv")) != uv_version:
        raise BundleError(
            f"uv {uv_version} is not the uv.lock pin ({pinned}): `uv sync --frozen --extra agent`",
            reason="uv_mismatch",
        )
    epoch = source_date_epoch(REPO)
    name = bundle_name(version, layout, arch)
    out = options.out.resolve()
    final = out / name
    if final.exists() and not options.force:
        raise BundleError(f"{final} exists (use --force)", reason="exists")
    out.mkdir(parents=True, exist_ok=True)
    options.cache.mkdir(parents=True, exist_ok=True)
    extras = (*ROLE_EXTRAS[options.role], *options.extra_extras)

    with tempfile.TemporaryDirectory(prefix=".bundle-", dir=out) as tmp:
        work = Path(tmp)
        root = work / name
        env = clean_env(
            {
                "UV_CACHE_DIR": str(options.cache / "uv"),
                "SOURCE_DATE_EPOCH": str(epoch),
            }
        )
        log(f"python {PYTHON_VERSION} via uv {uv_version}")
        source = obtain_python(uv, options.cache)
        copy_python(source, root / "python", layout)
        log("building the meshforge wheel")
        wheel = build_wheel(uv, REPO, work, env)
        has_dashboard = wheel_has(wheel, DASHBOARD_INDEX)
        if (
            options.role == "controller"
            and not has_dashboard
            and not options.allow_missing_dashboard
        ):
            raise BundleError(
                "the wheel has no dashboard (controller/web/dist): run `npm --prefix "
                "frontend run build` first, or --allow-missing-dashboard",
                reason="no_dashboard",
            )
        entries = parse_entry_points(
            wheel_text(wheel, ".dist-info/entry_points.txt") or ""
        )
        reqs = work / "requirements.txt"
        reqs.write_text(requirements(uv, REPO, extras, wheel, env), encoding="utf-8")
        log(f"installing extras {', '.join(extras)} from uv.lock (hash-checked)")
        install(uv, root, layout, reqs, env)
        relocated = relocate_installed(root, layout)
        log(f"relocated {len(relocated)} installed files")
        bad = compile_bytecode(root, layout, env)
        for path in bad:
            log(f"warning: does not compile (shipped as source only): {path}")
        write_launchers(root, layout, entries, options.role)
        normalise_tree(root, epoch)

        leaks = find_leaks(
            root,
            leak_needles([work, out, REPO, source.prefix, options.cache, Path.home()]),
        )
        if leaks:
            shown = "\n".join(f"  {leak.path}: {leak.needle}" for leak in leaks[:20])
            raise BundleError(
                f"{len(leaks)} file(s) name a build-machine path:\n{shown}",
                reason="path_leak",
            )
        manifest = Manifest(
            meta={
                "name": name,
                "version": version,
                "role": options.role,
                "extras": list(extras),
                "os": layout.os_name,
                "arch": arch,
                "python_platform": layout.python_platform,
                "python": {
                    "version": source.version,
                    "pbs_release": source.pbs_release,
                    "url": source.url,
                    "obtained_by": f"uv {uv_version} python install (sha256 checked by uv)",
                },
                "uv": uv_version,
                "uv_lock_sha256": hashlib.sha256(lock_text.encode("utf-8")).hexdigest(),
                "wheel_sha256": sha256_file(wheel),
                "dashboard": has_dashboard,
                "source_date_epoch": epoch,
                "uncompiled": bad,
            },
            files=scan_tree(root),
        )
        (root / MANIFEST_NAME).write_text(manifest.to_json(), encoding="utf-8")
        (root / MANIFEST_NAME).chmod(0o644)
        os.utime(root / MANIFEST_NAME, (epoch, epoch))
        if final.exists():
            shutil.rmtree(final)
        root.rename(final)
    log(
        f"built {final} ({sum(int(str(f.get('size', 0))) for f in manifest.files)} bytes)"
    )
    return final


# ----------------------------------------------------------------------- CLI
def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--role", choices=sorted(ROLE_EXTRAS))
    mode.add_argument(
        "--verify",
        type=Path,
        metavar="BUNDLE",
        help="check a bundle against its manifest",
    )
    mode.add_argument(
        "--diff", nargs=2, type=Path, metavar=("A", "B"), help="compare two manifests"
    )
    parser.add_argument("--out", type=Path, help="output directory (build)")
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
    parser.add_argument("--experimental-windows", action="store_true")
    args = parser.parse_args(argv)
    if args.role and args.out is None:
        parser.error("--role needs --out")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.diff:
            a, b = (json.loads(p.read_text(encoding="utf-8")) for p in args.diff)
            differences = diff_manifests(a, b)
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
                extra_extras=tuple(args.extra),
                force=args.force,
                allow_missing_dashboard=args.allow_missing_dashboard,
                experimental_windows=args.experimental_windows,
            )
        )
    except BundleError as exc:
        print(f"build_bundle: error ({exc.reason}): {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
