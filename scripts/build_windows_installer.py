"""Build ``MeshForge-Agent-Setup-<version>-<revision>.exe`` from a Windows bundle
(V0.17 G4, D395). Runs on Windows (the ``package-windows`` CI job).

Steps:

1. **Bundle facts** from ``BUILDINFO.json`` (role ``agent``, ``windows``/``x86_64``)
   and the bundle checked against its own ``MANIFEST.sha256``.
2. **Names** (:func:`installer_names`, pure): the version folder
   ``<version>-<revision>`` (an upgrade test builds revision 2 of one bundle, like
   the ``.deb``), the numeric ``VersionInfoVersion`` and the output file name.
3. **ISCC** - the pinned Inno Setup (``packaging/pins.toml [innosetup]``, URL +
   sha256, never "latest"), downloaded with the bundle builder's checking
   ``fetch`` and installed once per user into the cache
   (``/VERYSILENT /CURRENTUSER /DIR=...``); ``--iscc`` overrides for a developer.
4. **Compile** ``packaging/windows/meshforge-agent.iss`` with ``/D`` defines,
   then check the result exists, write ``<exe>.sha256`` and enforce the agent's
   **download** budget (Q17, ``[size_budget_mb] agent``).

Exit codes: 0 ok; 1 build failed; 2 usage error.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

_SCRIPTS: Final = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from bundle_lib import (  # noqa: E402
    BUILDINFO_NAME,
    BundleError,
    check_no_secrets,
    check_size_budget,
    load_pins,
    pinned,
    sha256_file,
    verify_bundle,
)

REPO: Final = _SCRIPTS.parent
PINS: Final = REPO / "packaging" / "pins.toml"
ISS: Final = REPO / "packaging" / "windows" / "meshforge-agent.iss"
PRODUCT: Final = "MeshForge-Agent-Setup"


@dataclass(frozen=True, slots=True)
class InstallerNames:
    version_dir: str
    numeric_version: str
    output_name: str


def numeric_version(version: str, revision: int) -> str:
    """``a.b.c.<revision>`` from the PEP 440 release part (Windows file versions
    are four 16-bit numbers; pre/dev tags do not fit and are dropped here)."""
    match = re.match(r"^(\d+)(?:\.(\d+))?(?:\.(\d+))?", version)
    if match is None:
        raise BundleError(
            f"version {version!r} has no release number", reason="bad_version"
        )
    parts = [int(p) if p else 0 for p in match.groups()]
    if any(p > 65535 for p in [*parts, revision]):
        raise BundleError(
            f"version {version!r} does not fit a Windows file version",
            reason="bad_version",
        )
    return ".".join(str(p) for p in [*parts, revision])


def installer_names(version: str, revision: int) -> InstallerNames:
    if revision < 1:
        raise BundleError("--revision must be 1 or more", reason="usage")
    if not re.fullmatch(r"[0-9A-Za-z.+-]+", version):
        raise BundleError(f"unsafe version {version!r}", reason="bad_version")
    folder = f"{version}-{revision}"
    return InstallerNames(
        folder, numeric_version(version, revision), f"{PRODUCT}-{folder}"
    )


def iscc_defines(
    bundle: Path, version: str, names: InstallerNames, out: Path
) -> list[str]:
    """The ``/D`` arguments the ``.iss`` requires (it ``#error``\\ s without them)."""
    values: Mapping[str, str] = {
        "BundleDir": str(bundle),
        "AppVersion": version,
        "VersionDir": names.version_dir,
        "NumericVersion": names.numeric_version,
        "OutputDir": str(out),
        "OutputName": names.output_name,
    }
    for key, value in values.items():
        if '"' in value:
            raise BundleError(
                f"{key} contains a double quote: {value!r}", reason="usage"
            )
    return [f"/D{key}={value}" for key, value in values.items()]


def bundle_facts(bundle: Path) -> dict[str, object]:
    try:
        info = json.loads((bundle / BUILDINFO_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BundleError(
            f"{bundle} is not a built bundle ({exc})", reason="no_bundle"
        ) from None
    if not isinstance(info, dict):
        raise BundleError(
            f"{bundle}/{BUILDINFO_NAME} is not an object", reason="no_bundle"
        )
    want = {"role": "agent", "os": "windows", "arch": "x86_64"}
    wrong = {k: info.get(k) for k, v in want.items() if info.get(k) != v}
    if wrong:
        raise BundleError(
            f"{bundle} is not a windows x86_64 agent bundle ({wrong}); build one with "
            "scripts/build_bundle.py --role agent --os windows",
            reason="wrong_bundle",
        )
    if "version" not in info:
        raise BundleError(f"{BUILDINFO_NAME} has no version", reason="no_bundle")
    return info


def pinned_iscc(pins: Mapping[str, object], cache: Path) -> Path:
    """The pinned Inno Setup's ISCC.exe, installed once into the cache."""
    import build_bundle  # the shared, sha256-checking fetch

    download = pinned(pins, "innosetup", "windows-x86_64")
    home = cache / "tools" / f"innosetup-{download.version}"
    iscc = home / "ISCC.exe"
    if iscc.is_file():
        return iscc
    setup = build_bundle.fetch(download, cache)
    done = subprocess.run(
        [
            str(setup),
            "/VERYSILENT",
            "/SUPPRESSMSGBOXES",
            "/NORESTART",
            "/SP-",
            "/CURRENTUSER",
            "/NOICONS",
            f"/DIR={home}",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )
    if done.returncode != 0 or not iscc.is_file():
        raise BundleError(
            f"installing Inno Setup {download.version} failed (exit {done.returncode})",
            reason="tool_failed",
        )
    return iscc


def log(message: str) -> None:
    print(f"build_windows_installer: {message}", file=sys.stderr)


def build(
    bundle: Path,
    out: Path,
    *,
    revision: int,
    cache: Path,
    iscc: Path | None,
    size_budget: bool,
    pins_path: Path = PINS,
) -> Path:
    if platform.system() != "Windows":  # not sys.platform: mypy would skip the rest
        raise BundleError(
            "the installer is built on Windows (Inno Setup)", reason="unsupported"
        )
    bundle = bundle.resolve()
    info = bundle_facts(bundle)
    differences = verify_bundle(bundle)
    if differences:
        raise BundleError(
            f"{bundle} differs from its MANIFEST.sha256 ({len(differences)})",
            reason="bundle_modified",
        )
    # V1.0 G3 (D413): the installer's inputs - the bundle and packaging/windows
    # (the .exe is compressed, so its content cannot be scanned afterwards)
    check_no_secrets(bundle, ISS.parent)
    version = str(info["version"])
    names = installer_names(version, revision)
    pins = load_pins(pins_path)
    compiler = iscc or pinned_iscc(pins, cache)
    out = out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    argv = [str(compiler), "/Qp", *iscc_defines(bundle, version, names, out), str(ISS)]
    log(f"ISCC {names.output_name}.exe (version folder {names.version_dir})")
    done = subprocess.run(argv, capture_output=True, text=True, check=False)
    if done.returncode != 0:
        tail = (done.stdout + done.stderr)[-3000:]
        raise BundleError(
            f"ISCC failed (exit {done.returncode}):\n{tail}", reason="tool_failed"
        )
    exe = out / f"{names.output_name}.exe"
    if not exe.is_file():
        raise BundleError(
            f"ISCC reported success but {exe} is missing", reason="tool_failed"
        )
    check_no_secrets(exe)  # raw bytes: whatever Inno Setup stored uncompressed
    digest = sha256_file(exe)
    (out / f"{exe.name}.sha256").write_text(f"{digest}  {exe.name}\n", encoding="utf-8")
    size = exe.stat().st_size
    log(f"built {exe} ({size / 1e6:.1f} MB, sha256 {digest})")
    budget_table = pins.get("size_budget_mb")
    budget = budget_table.get("agent") if isinstance(budget_table, dict) else None
    if size_budget and isinstance(budget, int | float):
        check_size_budget(size, float(budget), exe.name)
    return exe


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--revision", type=int, default=1)
    parser.add_argument(
        "--cache",
        type=Path,
        default=Path(os.environ.get("LOCALAPPDATA", Path.home())) / "meshforge-bundle",
    )
    parser.add_argument("--iscc", type=Path, default=None, help="use this ISCC.exe")
    parser.add_argument("--no-size-budget", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        build(
            args.bundle,
            args.out,
            revision=args.revision,
            cache=args.cache,
            iscc=args.iscc,
            size_budget=not args.no_size_budget,
        )
    except BundleError as exc:
        print(f"build_windows_installer: error ({exc.reason}): {exc}", file=sys.stderr)
        return 2 if exc.reason == "usage" else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
