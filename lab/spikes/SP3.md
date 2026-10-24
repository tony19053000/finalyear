# Spike SP3: the D384 relocatable bundle (V0.17 G0, roadmap 0.17.0)

**Question:** can `scripts/build_bundle.py` turn `uv.lock` into a self-contained runtime
folder (`meshforge-<ver>-<os>-<arch>/`) that is **reproducible** (same commit → same file
hashes), **small enough** (brief target: < 150 MB agent, < 250 MB controller incl.
PostgreSQL), **relocatable** (works after a move, with no system Python and no `uv` on
`PATH`), and able to run the agent's **job env builds** with its own `uv`?

**Answer (Linux x86_64, 2026-10-24):** yes to reproducible, relocatable and env builds;
**no to the size targets as written**. The agent bundle is 297 MB on disk (67 MB as
`tar.xz`), the controller bundle 410 MB (90 MB `tar.xz`) before PostgreSQL. The
floors are the interpreter (31 MB), the pinned `uv` (50 MB), numpy+OpenBLAS (66 MB,
agent) and pyarrow (165 MB, controller). See "Sizes" for what could be cut and what
the Lead must decide.

Machine: the team's Linux laptop (i7-12650H, Ubuntu, glibc 2.39), uv 0.12.21 (the
`uv.lock` pin), CPython 3.12.14 from python-build-standalone release 20260929.

## How to re-run

```bash
uv sync --frozen --extra agent --extra controller
uv run --no-sync python lab/spikes/sp3_bundle.py --role agent --work /tmp/sp3 --cold-cache
npm --prefix frontend run build      # the controller wheel must carry the dashboard
uv run --no-sync python lab/spikes/sp3_bundle.py --role controller --work /tmp/sp3
# the regression test (builds twice, moves, runs, env build; ~35 s warm, minutes cold):
uv run --no-sync python scripts/pytest_exit_guard.py \
    tests/component/lab/test_v017_sp3_bundle.py --run-slow -q
```

`sp3_bundle.py` writes `<work>/sp3-<role>.json` with every command's exit code, time,
stdout/stderr tail, the env-probe results and the sizes.

## What the builder does (and why)

| Step | How | Why |
|---|---|---|
| Python | `uv python install cpython-3.12.14` into a **private** dir (`~/.cache/meshforge-bundle/python`) by the lock-pinned uv | uv checks the PBS archive against the sha256 compiled into that uv release, so (uv version, Python version) pins the bytes. The URL and PBS release are written into the manifest. The user's own uv-managed Pythons are never used (packages may have been installed into them). |
| Undo uv's path patch | uv rewrites PBS's `/install` placeholder to the install dir in `_sysconfigdata*.py`, `python3.12-config`, `Makefile`, `*.pc`; the copy writes `/install` back | otherwise 21 sysconfig values name the build machine's cache path |
| Strip | pip, ensurepip, idle, 2to3/lib2to3, turtledemo, tkinter + Tcl/Tk, man pages, **`libpython3.12.so` (32 MB)** | `bin/python3.12` links libpython statically (`ldd`: libc/libm/… only); the `.so` is for embedders. `EXTERNALLY-MANAGED` is kept, so nobody `pip install`s into the program dir. |
| meshforge wheel | `uv build --wheel` with `scripts/bundle_build_constraints.txt` (hatchling + deps, hash-pinned) and `SOURCE_DATE_EPOCH` = HEAD commit time | pyproject only says `hatchling>=1.27`; an unpinned backend would change the wheel over time |
| Dependencies | `uv export --frozen --no-dev --extra <role> --no-emit-project` + the wheel by sha256 → `uv pip install --require-hashes --no-deps --only-binary :all: --link-mode copy --python-platform x86_64-manylinux_2_28 --break-system-packages` | hashes from `uv.lock`; a **fixed target platform** so the build machine's glibc does not pick the wheels (manylinux_2_28 covers Ubuntu 22.04/24.04 and RHEL 8) |
| Relocation fixes | wheel console scripts' absolute `#!` → PBS's `/bin/sh` trampoline (RECORD rehashed); drop `direct_url.json` and `uv_cache.json` (RECORD rows too) | the installer writes the build path into scripts; `uv_cache.json` holds the install time |
| Bytecode | delete every `__pycache__`, then `compileall` with `unchecked-hash` pycs, `stripdir=<bundle>`, `PYTHONHASHSEED=0`, **one worker**, `PYTHONDONTWRITEBYTECODE=1` for the compiler process itself | see (a) |
| Launchers | `bin/meshforge-agent`, `bin/compute` (+ `bin/meshforge-controller`): `/bin/sh` scripts that `realpath` themselves and `exec ../python/bin/python3.12 -I -c '<import main; sys.exit(main())>'`; POSIX also gets `uv -> python/bin/uv` | relative paths survive a move and a symlink from `/usr/bin`; `-I` ignores `PYTHON*` variables, user site and cwd |
| Normalise | modes 0755/0644, every mtime = `SOURCE_DATE_EPOCH` | so a `.deb`/`.tar` of the folder is reproducible too |
| Leak scan | every file (and symlink target) searched for the work dir, out dir, repo, Python cache, build cache and `$HOME`; any hit fails the build (`path_leak`) | the "no build-machine paths" rule, enforced, not hoped for |
| Manifest | `MANIFEST.json`: meta (role, extras, Python version/URL/PBS release, uv version, `uv.lock` sha256, wheel sha256, epoch) + every file's path/size/sha256/mode, symlinks, empty dirs | `--diff A B` compares two builds; `--verify DIR` checks an installed bundle |

## Results

| # | Check | Result |
|---|---|---|
| a | Agent bundle built twice, different out dirs, **cold** download cache for the second, one out dir containing a space | `MANIFEST.json` byte-identical (sha256 `12a85271…` before the single-worker change; identical again after it, regression test passes) |
| a | Controller bundle built 3× | identical (`3325f459…`) **after** the single-worker compile fix (see residue history) |
| b | Agent bundle size | **296.8 MB** on disk; `tar` 315 MB → gzip -6 **98.2 MB**, xz -6 **66.9 MB** |
| b | Controller bundle size (no PostgreSQL yet) | **410.3 MB** on disk; gzip -6 130.1 MB, xz -6 **90.2 MB** |
| b | Controller + `ml` extra | 703.4 MB on disk |
| c | Moved to `…/moved here/agent/`, only `PATH` + `HOME` set (`PATH=/usr/bin:/bin`) | `meshforge-agent --version` ✔ 0.07 s · `hardware show` ✔ 0.80 s · `compute --help` ✔ 0.22 s · `uv --version` ✔ (0.12.21) · interpreter `sys.executable`/`sys.prefix`/`sys.path` all inside the moved bundle ✔ |
| c | Same, `PATH` = a toolbox holding only `realpath`, `dirname` (no Python, no uv by name) | all five ✔ (same times) |
| c | Launcher reached through a symlink on `PATH` (the `.deb` shape) | ✔ |
| c | Start-up, `meshforge-agent --version`, median of 5 | **0.066 s** with shipped pycs vs **0.324 s** without pycs on a read-only tree (recompiled in memory every start) |
| c | Controller bundle moved, both PATHs: `meshforge-controller --version`, `--help`, `compute --help`, self-report | all ✔ |
| c | `build_bundle.py --verify` on the moved agent bundle after all of the above + env builds | `intact`: running the bundle writes nothing into it |
| d | EnvBuilder (agent code, running in the bundle) with default `uv_argv` | `uv_argv` = `<bundle>/python/bin/uv` ✔ (the lock-pinned uv wheel, D86) |
| d | `ensure_target` (subprocess runtime: installs for `sys.executable` = the bundle Python), all three PATHs | `built`, import ✔ (0.02–0.06 s, offline wheelhouse) |
| d | `ensure_venv`, `PATH=/usr/bin:/bin` | `built` ✔ **but on `/usr/bin/python3.12` - the system Python** (finding F1) |
| d | `ensure_venv`, toolbox PATH (no Python anywhere on PATH) | **`python_not_found`** ("no Python 3.12 on this node") (finding F1) |
| d | `ensure_venv`, bundle `python/bin` first on PATH | `built` on the bundled interpreter, import ✔ 0.08 s |

`hardware show` reported "gpus: none detected" - the NVIDIA driver was not loaded on
this laptop during the spike (the dev venv reports the same `NVMLError_DriverNotLoaded`),
so GPU visibility from the bundle is **not** proven here (see 🧑 step).

### (a) Reproducibility residue, in the order it was found and fixed

1. `uv build` failed outright: uv 0.12 mis-parses a `--build-constraints` path containing
   a space (`File not found: /home/aayush/Desktop/FINAL`). Fix: run it with `cwd=repo`
   and relative paths.
2. 73 stdlib `.pyc` named the build dir: the `compileall` process imports modules
   itself, and with bytecode writing on those got timestamp pycs with absolute
   `co_filename`, which `compileall` then skipped as up to date. Fix: keep
   `PYTHONDONTWRITEBYTECODE=1` for the compiler (py_compile ignores it) and `force=True`.
3. `meshforge-…dist-info/uv_cache.json` (+ its RECORD row) differed: uv stores the
   install time for a local wheel. Fix: drop it, like `direct_url.json`.
4. Controller only: `pycparser/__pycache__/c_parser.cpython-312.pyc` differed in size
   and hash between two builds. The same file compiles identically in fresh processes;
   the difference comes from `compileall`'s worker pool (marshal writes `FLAG_REF`
   back-references based on object reference counts, which depend on what the same
   worker compiled before). Fix: one worker (`workers=1`): a fixed order gives fixed
   state. Cost: build time ~8 s warm instead of ~4 s.

No residue is left on this machine. **Not yet shown:** identical output on a second
build machine (different distro/glibc/CPU) - the CI runner should build and `--diff`
against a laptop build.

### (b) Sizes: where the bytes are (agent, on disk)

| Part | MB |
|---|---|
| `python/bin/python3.12` (static interpreter) | 31.0 |
| `python/bin/uv` (pinned uv wheel binary; `uvx` 0.3) | 50.3 |
| stdlib incl. pycs (after strip) | 22.9 |
| site-packages incl. pycs | 186.7 |
| ↳ numpy 43 + numpy.libs (OpenBLAS) 27 | 70 |
| ↳ zstandard (ships both the C and the CFFI backend, 11 MB each) | 23 |
| ↳ grpc | 19 |
| ↳ meshforge (incl. 7.4 MB controller code the agent never imports) | 17 |
| ↳ cryptography | 16 |
| ↳ pygments (via rich) | 9 |
| all `.pyc` (stdlib + site-packages) | 47.7 |
| `MANIFEST.json` | 2.0 |

Controller: site-packages 351 MB, of which **pyarrow 165 MB** (only the telemetry
Parquet export, D211/D222), sqlalchemy 22, zstandard 23, grpc 18, uvloop 16, meshforge 15,
cryptography 15, asyncpg 13. With `--extra ml`: + scipy 133, sklearn 44, numpy 66,
xgboost 24, lightgbm 10.

**Conclusion for the Lead:** "< 150 MB agent" is reachable only as a **download** size
(xz: 67 MB), not on disk. Every cut that is safe in principle still leaves the agent at
~200 MB on disk: drop pycs (−48 MB, but 5× slower start-up: 0.07 → 0.32 s per command,
and the agent spawns Python children), delete wheels' `tests/` dirs (−15 MB) and
zstandard's CFFI backend (−12 MB) (both edit third-party wheels after install; RECORD
must be updated), move controller code out of the agent bundle (−7 MB, needs a wheel
split). The controller's 250 MB "incl. PostgreSQL" target is missed by pyarrow alone;
making the Parquet export lazy-import an optional extra would bring the controller to
~245 MB before PostgreSQL. Recommendation: restate D384's targets as **download
(compressed) size: agent < 100 MB, controller < 150 MB incl. PostgreSQL** and on-disk
size as a recorded number, not a gate; decide separately whether pyarrow stays in the
base controller install.

## Findings for G1 (not fixed in the spike - `src/` untouched)

* **F1 - the venv runtime does not use the bundled interpreter.** `EnvBuilder.find_interpreter`
  runs `uv python find --system 3.12`, which searches `PATH` and uv's managed-Python dir.
  From a bundle on a machine with a system Python it silently builds job venvs on **the
  system Python** (here `/usr/bin/python3.12`, 3.12.3, not the bundle's 3.12.14); on a
  fresh Windows laptop (no Python) every venv job fails `python_not_found` unless
  `allow_python_downloads` is on. Fix (G1): when the request matches the agent's own
  major.minor (the default), use `sys.executable` (a relocatable PBS interpreter), or
  add the bundle's `python/bin` to the lookup. The `ensure_target` path is unaffected.
* **F2 - venvs remember the interpreter's absolute path.** A venv built from the bundle
  has `home = /opt/meshforge/<ver>/python/bin` in `pyvenv.cfg`. After an upgrade removes
  the old version folder, cached envs point at a missing interpreter, but `env_state`
  only checks the env's own files and still says `ready`. G1 must either build venvs
  from the stable `current` path (`/opt/meshforge/current/python/bin`; Windows has no
  such pointer, D385) or put the interpreter path into the env hash / readiness check.
* **F3 - `SSL_CERT_FILE` reaches the build.** The component tier sets it to the test
  fabric CA; the bundle build then cannot download (`UnknownIssuer`). The test strips
  it; the builder keeps it on purpose (corporate proxies need it).
* **F4 - controller bundle needs the dashboard built first.** The wheel only carries
  `controller/web/dist` when `npm run build` ran; the builder refuses a controller
  bundle without it (`no_dashboard`, `--allow-missing-dashboard` to override). The
  agent bundle carries it too when present (1.2 MB, unused by the agent).
* **F5 - `compute` finds the agent by name.** `cli/fabric_commands.py` uses
  `shutil.which("meshforge-agent")`; it works when the package links `bin/` into
  `/usr/bin` (checked: a symlinked launcher runs), not from an unlinked bundle.

## Deviations from the task as written

* **No separate `uv` copy.** The pinned `uv` is the `uv` wheel from `uv.lock` (agent extra),
  installed and hash-checked like every other dependency; the agent's env builder
  already runs exactly that binary (`uv.find_uv_bin()`, D86). A second 50 MB copy at the
  top level would be dead weight, so POSIX gets a relative symlink `uv -> python/bin/uv`;
  Windows gets nothing at the top (no symlinks there; nothing needs it). The controller
  bundle has no uv (not in its extra).
* **Python version pinned to 3.12.14**, not "the one uv has": the lock-pinned uv 0.12.21
  offers 3.12.14 (PBS 20260929); the dev venvs here run 3.12.3/3.12.13. Same minor, so
  same wheels; bumping it is a deliberate one-line change (`PYTHON_VERSION`).
* **Pure helpers split into `scripts/bundle_lib.py`** (manifest, launchers, rewrites, leak
  scan) so they are unit-tested on their own; `pyproject.toml` adds `scripts/` to
  `mypy_path` for that sibling import.

## Open risks for Windows (what SP1 / the Windows CI must check)

1. **Console-script `.exe` trampolines.** On Windows, uv writes `python/Scripts/*.exe`
   launchers with the **absolute** interpreter path embedded; they break after a move.
   The bundle's own launchers do not use them, but `uv.find_uv_bin()` returns
   `Scripts\uv.exe`, which is a real binary (fine). Decide: delete the other trampolines
   or regenerate them; extend the leak scan (it already scans binaries) to prove none
   remain.
2. **`.cmd` launchers are written but never run.** Check quoting of `%*` with spaces and
   `&`, the exit code passthrough (`exit /b %ERRORLEVEL%`), and that the Windows service
   (pywin32, D386) runs `python\python.exe` directly rather than a `.cmd`.
3. **Layout assumptions:** `python/python.exe`, `python/Lib/site-packages`,
   `python/Scripts`; PBS's Windows build has no `_sysconfigdata` placeholder - confirm
   the leak scan stays clean without the `/install` rewrite.
4. **Strip list is POSIX-only.** The Windows PBS build ships Tcl/Tk, pip, idle under
   different paths; measure and add a `STRIP_WINDOWS`.
5. **Reproducibility on NTFS:** no mode bits, case-insensitive paths, and the
   `--python-platform x86_64-pc-windows-msvc` wheel set; build twice and `--diff`.
6. **Path length:** `C:\Program Files\MeshForge\<ver>\python\Lib\site-packages\…` -
   check the longest bundle path against `MAX_PATH` (260) and whether long-path support
   is needed.
7. **Antivirus / SmartScreen** on an unsigned `python.exe` + `uv.exe` in Program Files,
   and on `uv` spawning compilers during env builds.
8. **F1 is worse on Windows:** no system Python at all, so every venv job fails until
   the env builder uses the bundled interpreter.

## 🧑 lab step still needed

The env build ran offline from a hand-made wheelhouse. The brief's SP3 also asks for
"a real job with an env build" and GPU visibility; that needs the controller and a
GPU node:

* Laptop with a working NVIDIA driver (`nvidia-smi` prints the GPU): build or copy the
  agent bundle, then `env -i PATH=/usr/bin:/bin HOME=/tmp/h <bundle>/bin/meshforge-agent hardware show`
  - expected: the GPU listed under `gpus:`, probe `gpu ok`. Paste the output.
* Same laptop, already joined to a dev controller, Docker stopped (so the venv runtime
  builds the env): stop the dev agent, start `<bundle>/bin/meshforge-agent run` with the
  usual config. Copy `workloads/examples/cpu-deterministic` to a temp folder, add a
  `requirements.txt` containing `numpy==2.5.3` and `requirements: requirements.txt`
  under `env:` in its `compute.yaml`, then from the dev checkout
  `compute run <folder> --node <this laptop> -f`. Expected: the job succeeds; paste the
  output and `compute jobs <job_id>` (env build phase timing). F1 means the venv will be
  built on the system Python if that laptop has one; note which interpreter the logs
  show.
