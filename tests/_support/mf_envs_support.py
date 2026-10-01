"""Helpers for the V0.5 G2 environment tests: an offline wheelhouse and uv wrappers.

Nothing here touches the network: :func:`make_wheel` writes a tiny pure-Python
wheel by hand (a zip with METADATA/WHEEL/RECORD), and builds install from that
directory with ``--no-index`` - so the tests pass on a laptop with no internet.
"""

from __future__ import annotations

import base64
import hashlib
import sys
import zipfile
from pathlib import Path

from meshforge.agent.envs.builder import EnvBuilder, EnvsConfig, default_uv_argv


def _record_line(name: str, data: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
    return f"{name},sha256={digest.decode()},{len(data)}"


def make_wheel(folder: Path, name: str = "mfdemo", version: str = "1.0") -> Path:
    """A pure-Python wheel ``<name>-<version>-py3-none-any.whl`` in ``folder`` whose
    module has ``VERSION = "<version>"``."""
    folder.mkdir(parents=True, exist_ok=True)
    dist = f"{name}-{version}.dist-info"
    files = {
        f"{name}/__init__.py": f'VERSION = "{version}"\n'.encode(),
        f"{dist}/METADATA": (
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
        ).encode(),
        f"{dist}/WHEEL": (
            b"Wheel-Version: 1.0\nGenerator: mf-test\nRoot-Is-Purelib: true\n"
            b"Tag: py3-none-any\n"
        ),
    }
    record = [_record_line(n, d) for n, d in files.items()] + [f"{dist}/RECORD,,"]
    files[f"{dist}/RECORD"] = ("\n".join(record) + "\n").encode()
    path = folder / f"{name}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as zf:
        for item, data in files.items():
            zf.writestr(item, data)
    return path


SLOW_UV = """\
import os, subprocess, sys, time
delay = float(sys.argv[1]); real = sys.argv[2:3]; args = sys.argv[3:]
if "install" in args and "--target" in args:
    target = args[args.index("--target") + 1]
    os.makedirs(target, exist_ok=True)
    with open(os.path.join(target, "partial-from-slow-uv.txt"), "w") as fh:
        fh.write("half-built")
    time.sleep(delay)
sys.exit(subprocess.call(real + args))
"""


def slow_uv_argv(folder: Path, delay_s: float) -> tuple[str, ...]:
    """A uv wrapper that, for a ``--target`` install, first writes a partial file
    into the target and sleeps ``delay_s`` - long enough to observe or kill a build
    mid-way - then runs the real uv."""
    script = folder / "slow_uv.py"
    script.write_text(SLOW_UV)
    real = default_uv_argv()
    assert real, "the uv wheel is not installed (agent extra)"
    return (sys.executable, str(script), str(delay_s), *real)


def offline_builder(
    root: Path, wheelhouse: Path, *, uv_argv: tuple[str, ...] | None = None
) -> EnvBuilder:
    """A builder that installs only from ``wheelhouse`` (no network)."""
    return EnvBuilder(
        EnvsConfig(
            root=root,
            free_disk_floor_bytes=0,
            find_links=wheelhouse,
            no_index=True,
        ),
        uv_argv=uv_argv,
    )


BUILD_CHILD = """\
import asyncio, json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[4])
from mf_envs_support import offline_builder, slow_uv_argv

async def main():
    root, wheels, delay = Path(sys.argv[1]), Path(sys.argv[2]), float(sys.argv[3])
    argv = slow_uv_argv(root.parent, delay) if delay > 0 else None
    builder = offline_builder(root, wheels, uv_argv=argv)
    print("started", flush=True)
    built = await builder.ensure_target(requirements=b"mfdemo==1.0\\n", deadline=1e18)
    print(json.dumps({"cache": built.cache, "hash": built.env_hash}), flush=True)

asyncio.run(main())
"""


def build_child_argv(
    folder: Path, root: Path, wheelhouse: Path, delay_s: float
) -> list[str]:
    """argv of a child process that runs one ``ensure_target`` build and prints
    ``started`` then a JSON line ``{"cache", "hash"}``."""
    script = folder / "build_child.py"
    script.write_text(BUILD_CHILD)
    support = Path(__file__).resolve().parent
    return [
        sys.executable,
        str(script),
        str(root),
        str(wheelhouse),
        str(delay_s),
        str(support),
    ]
