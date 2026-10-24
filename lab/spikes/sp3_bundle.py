"""Spike SP3 (V0.17 G0, roadmap 0.17.0): is the D384 bundle reproducible, small
enough, relocatable, and able to build a job env with no system Python / uv?

    uv run --extra agent python lab/spikes/sp3_bundle.py --role agent --work /tmp/sp3
    uv run --extra agent python lab/spikes/sp3_bundle.py --role controller --work /tmp/sp3 \\
        --skip-runtime

Steps (each recorded in ``<work>/sp3-<role>.json``, a summary on stdout):

a. build twice with ``scripts/build_bundle.py`` into different folders (the second
   with a cold download cache under ``--cold-cache``) and diff the manifests;
b. sizes (apparent bytes by part) and download estimates (tar gzip -6 / xz -6);
c. move bundle A to a path with a space, then run ``meshforge-agent --version``,
   ``meshforge-agent hardware show``, ``compute --help``, the bundle ``uv`` and an
   interpreter self-report with only ``PATH`` and ``HOME`` set - once with
   ``PATH=/usr/bin:/bin`` and once with a toolbox ``PATH`` that has no Python;
   plus a launcher reached through a symlink; plus start-up time with and without
   shipped pycs (read-only tree);
d. a real offline env build through the agent's EnvBuilder with the bundle's uv,
   for three ``PATH`` values (system, toolbox, bundle python first).

Exit codes: 0 every check passed; 1 a check failed (see the JSON); 2 usage error.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Final

_LAB_DIR: Final = Path(__file__).resolve().parents[1]
if str(_LAB_DIR) not in sys.path:
    sys.path.insert(0, str(_LAB_DIR))

from spikes._sp3 import checks  # noqa: E402

REPO: Final = _LAB_DIR.parent
BUILD: Final = REPO / "scripts" / "build_bundle.py"


def build(role: str, out: Path, cache: Path, extra: list[str]) -> tuple[Path, float]:
    """Build one bundle; returns it and the build's wall time in seconds."""
    argv = [
        sys.executable,
        str(BUILD),
        "--role",
        role,
        "--out",
        str(out),
        "--cache",
        str(cache),
        "--force",
    ]
    for name in extra:
        argv += ["--extra", name]
    started = time.monotonic()
    subprocess.run(argv, check=True)
    seconds = round(time.monotonic() - started, 1)
    (bundle,) = sorted(p for p in out.iterdir() if p.name.startswith("meshforge-"))
    return bundle, seconds


def diff(a: Path, b: Path) -> list[str]:
    done = subprocess.run(
        [
            sys.executable,
            str(BUILD),
            "--diff",
            str(a),
            str(b),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return [
        line for line in done.stdout.splitlines() if not line.endswith("difference(s)")
    ]


def cold_starts(bundle: Path, env: dict[str, str], role: str) -> dict[str, Any]:
    """Each CLI's start-up: cold (bundle evicted from the page cache first) and
    warm (median of 5 after a warm-up)."""
    commands: list[list[str | Path]] = [
        [bundle / "bin" / f"meshforge-{role}", "--version"],
        [bundle / "bin" / "compute", "--help"],
    ]
    if role == "agent":
        commands.append([bundle / "bin" / "meshforge-agent", "hardware", "show"])
    else:
        commands.append([bundle / "bin" / "meshforge-controller", "--help"])
    out: dict[str, Any] = {}
    for argv in commands:
        name = " ".join([Path(argv[0]).name, *map(str, argv[1:])])
        out[name] = {
            "cold": checks.cold_start_seconds(argv, env, bundle, 3),
            "warm": checks.startup_seconds(argv, env, 5),
        }
    return out


def controller_runtime(bundle: Path, work: Path) -> dict[str, Any]:
    home = work / "home"
    toolbox = str(checks.make_toolbox(work / "toolbox"))
    results = [
        *checks.relocation_checks(
            bundle, home, checks.SYSTEM_PATH, "system", "controller"
        ),
        *checks.relocation_checks(bundle, home, toolbox, "toolbox", "controller"),
    ]
    env = checks.clean_env(toolbox, home)
    return {
        "commands": checks.as_dicts(results),
        "cli_start": cold_starts(bundle, env, "controller"),
    }


def runtime(bundle: Path, work: Path) -> dict[str, Any]:
    home = work / "home"
    toolbox = str(checks.make_toolbox(work / "toolbox"))
    results = [
        *checks.relocation_checks(bundle, home, checks.SYSTEM_PATH, "system"),
        *checks.relocation_checks(bundle, home, toolbox, "toolbox"),
        checks.symlinked_launcher(bundle, work / "symlinked-bin", home, "toolbox"),
    ]
    python_bin = str(bundle / "python" / "bin")
    probes = [
        checks.env_probe(bundle, work / "probe-system", checks.SYSTEM_PATH, "system"),
        checks.env_probe(bundle, work / "probe-toolbox", toolbox, "toolbox"),
        checks.env_probe(
            bundle,
            work / "probe-bundle-first",
            f"{python_bin}:{toolbox}",
            "bundle-python-first",
        ),
    ]
    env = checks.clean_env(toolbox, home)
    agent: list[str | Path] = [bundle / "bin" / "meshforge-agent", "--version"]
    startup = {"with_pyc": checks.startup_seconds(agent, env, 5)}
    nopyc = work / "nopyc" / bundle.name
    if nopyc.exists():
        checks.make_writable(nopyc)
        shutil.rmtree(nopyc)
    shutil.copytree(bundle, nopyc, symlinks=True)
    checks.strip_bytecode_read_only(nopyc)
    try:
        startup["without_pyc_read_only"] = checks.startup_seconds(
            [nopyc / "bin" / "meshforge-agent", "--version"], env, 5
        )
    finally:
        checks.make_writable(nopyc)
    return {
        "commands": checks.as_dicts(results),
        "env_probes": probes,
        "startup": startup,
        "cli_start": cold_starts(bundle, env, "agent"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--role", choices=("agent", "controller"), required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--extra", action="append", default=[])
    parser.add_argument(
        "--cold-cache", action="store_true", help="build B with an empty cache"
    )
    parser.add_argument("--skip-runtime", action="store_true", help="only (a) and (b)")
    parser.add_argument("--skip-compress", action="store_true")
    args = parser.parse_args(argv)
    work: Path = args.work.resolve()
    tag = "-".join([args.role, *args.extra])
    cache = work / "cache"
    a, a_seconds = build(args.role, work / f"{tag}-a", cache, args.extra)
    b, b_seconds = build(
        args.role,
        work / f"{tag}-b",
        work / "cache-cold" if args.cold_cache else cache,
        args.extra,
    )
    report: dict[str, Any] = {
        "role": args.role,
        "extras": args.extra,
        "diff": diff(a, b),
        "build_seconds": {
            "a": a_seconds,
            "b_cold_cache" if args.cold_cache else "b": b_seconds,
        },
    }
    report["sizes"] = checks.sizes(a)
    if not args.skip_compress:
        report["compressed"] = checks.compressed_sizes(a, work / "compress")
    if not args.skip_runtime:
        moved_root = work / "moved here" / tag
        if moved_root.exists():
            shutil.rmtree(moved_root)
        moved_root.mkdir(parents=True)
        moved = moved_root / a.name
        a.rename(moved)
        report["moved_to"] = str(moved)
        run_work = work / f"run-{tag}"
        report["runtime"] = (
            runtime(moved, run_work)
            if args.role == "agent"
            else controller_runtime(moved, run_work)
        )
    out = work / f"sp3-{tag}.json"
    out.write_text(json.dumps(report, indent=1), encoding="utf-8")
    failed = bool(report["diff"]) or any(
        not c["ok"] for c in report.get("runtime", {}).get("commands", [])
    )
    summary = {k: v for k, v in report.items() if k != "runtime"}
    rt = report.get("runtime", {})
    summary["start_up"] = {k: rt[k] for k in ("startup", "cli_start") if k in rt}
    sys.stdout.write(json.dumps(summary, indent=1) + f"\nreport: {out}\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
