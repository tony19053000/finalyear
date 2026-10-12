"""V0.7 G6 item 0 (D161 tester follow-up, THREAT_MODEL WS-I4): wheels-only builds.

Real uv, offline wheelhouse. The wheelhouse holds an **sdist** whose in-tree
PEP 517 backend (no build requirements, so it builds offline) writes a marker
file when its build code runs:

* default ``[agent.envs] only_binary = true``: the build fails ``env_error``
  ``setup_failed`` naming the escape hatch, and the marker is never written -
  no package's build code ran;
* ``only_binary = false`` (the escape hatch): the same requirement builds from
  source (the marker proves the code ran) and installs;
* a wheel installs either way.
"""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest
from mf_envs_support import make_wheel

from meshforge.agent.envs.builder import EnvBuilder, EnvsConfig, default_uv_argv
from meshforge.agent.envs.errors import EnvError

pytestmark = [pytest.mark.component, pytest.mark.asyncio]

if not default_uv_argv():
    pytest.skip("the uv wheel is not installed (agent extra)", allow_module_level=True)

BACKEND = """\
import base64, hashlib, os, zipfile

MARKER = {marker!r}


def get_requires_for_build_wheel(config_settings=None):
    return []


def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    with open(MARKER, "w") as fh:
        fh.write("build code ran")
    name, version = "mfsrc", "1.0"
    dist = f"{{name}}-{{version}}.dist-info"
    files = {{
        f"{{name}}/__init__.py": b"SOURCE_BUILT = True\\n",
        f"{{dist}}/METADATA": (
            f"Metadata-Version: 2.1\\nName: {{name}}\\nVersion: {{version}}\\n"
        ).encode(),
        f"{{dist}}/WHEEL": (
            b"Wheel-Version: 1.0\\nGenerator: t\\n"
            b"Root-Is-Purelib: true\\nTag: py3-none-any\\n"
        ),
    }}
    lines = []
    for n, d in files.items():
        h = base64.urlsafe_b64encode(hashlib.sha256(d).digest()).rstrip(b"=").decode()
        lines.append(f"{{n}},sha256={{h}},{{len(d)}}")
    lines.append(f"{{dist}}/RECORD,,")
    files[f"{{dist}}/RECORD"] = ("\\n".join(lines) + "\\n").encode()
    wheel = f"{{name}}-{{version}}-py3-none-any.whl"
    with zipfile.ZipFile(os.path.join(wheel_directory, wheel), "w") as zf:
        for n, d in files.items():
            zf.writestr(n, d)
    return wheel
"""

PYPROJECT = b"""\
[build-system]
requires = []
build-backend = "mfback"
backend-path = ["."]

[project]
name = "mfsrc"
version = "1.0"
"""


def _make_sdist(folder: Path, marker: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "mfsrc-1.0.tar.gz"
    members = {
        "mfsrc-1.0/pyproject.toml": PYPROJECT,
        "mfsrc-1.0/mfback.py": BACKEND.format(marker=str(marker)).encode(),
        "mfsrc-1.0/PKG-INFO": b"Metadata-Version: 2.1\nName: mfsrc\nVersion: 1.0\n",
    }
    with tarfile.open(path, "w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return path


def _builder(root: Path, wheels: Path, *, only_binary: bool) -> EnvBuilder:
    return EnvBuilder(
        EnvsConfig(
            root=root,
            free_disk_floor_bytes=0,
            find_links=wheels,
            no_index=True,
            only_binary=only_binary,
        )
    )


async def test_the_default_never_runs_a_packages_build_code(tmp_path: Path) -> None:
    wheels = tmp_path / "wheels"
    marker = tmp_path / "marker.txt"
    _make_sdist(wheels, marker)
    builder = _builder(tmp_path / "envs", wheels, only_binary=True)
    with pytest.raises(EnvError) as exc:
        await builder.ensure_target(requirements=b"mfsrc==1.0\n", deadline=1e18)
    assert exc.value.reason == "setup_failed"
    assert "only_binary = false" in str(exc.value)
    assert not marker.exists()


async def test_the_escape_hatch_builds_from_source(tmp_path: Path) -> None:
    wheels = tmp_path / "wheels"
    marker = tmp_path / "marker.txt"
    _make_sdist(wheels, marker)
    builder = _builder(tmp_path / "envs", wheels, only_binary=False)
    built = await builder.ensure_target(requirements=b"mfsrc==1.0\n", deadline=1e18)
    assert marker.read_text() == "build code ran"
    assert (built.path / "mfsrc" / "__init__.py").read_text() == "SOURCE_BUILT = True\n"


async def test_a_wheel_installs_in_binary_only_mode(tmp_path: Path) -> None:
    wheels = tmp_path / "wheels"
    make_wheel(wheels)
    builder = _builder(tmp_path / "envs", wheels, only_binary=True)
    built = await builder.ensure_target(requirements=b"mfdemo==1.0\n", deadline=1e18)
    assert (built.path / "mfdemo" / "__init__.py").exists()
