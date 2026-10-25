"""V0.17 G4: the real Inno Setup compiler accepts packaging/windows/meshforge-agent.iss.

The installer can only be *run* on Windows (the ``package-windows`` CI job), but
the script itself - its sections, its preprocessor lines and its Pascal [Code] -
is compiled here by Inno Setup 6 under Wine, in a digest-pinned image, against
a tiny stand-in bundle. A syntax or Pascal error therefore fails on Linux,
before a Windows runner is ever involved. The script's ``#error`` guards are
checked too: without the builder's defines it must refuse to compile.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Final

import pytest

pytestmark = [pytest.mark.docker, pytest.mark.slow]

REPO: Final = Path(__file__).resolve().parents[3]
IMAGE: Final = (
    "amake/innosetup@sha256:"
    "e003376ba818547275fe10c95e2a29be0f2d12d45e9eb8f205b6672dc5685bb1"
)
"""Inno Setup 6.7.1 under Wine (the builder pins 6.7.3 on Windows; both 6.7)."""


def _iscc(work: Path, *defines: str) -> subprocess.CompletedProcess[str]:
    if shutil.which("docker") is None:
        pytest.skip("docker is not installed")
    return subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{work}:/work",
            IMAGE,
            *defines,
            "meshforge-agent.iss",
        ],
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )


@pytest.fixture
def work(tmp_path: Path) -> Path:
    for item in (REPO / "packaging" / "windows").iterdir():
        shutil.copy(item, tmp_path / item.name)
    bundle = tmp_path / "bundle" / "python"
    bundle.mkdir(parents=True)
    for name in ("python.exe", "pythonw.exe"):
        (bundle / name).write_bytes(b"stand-in")
    (tmp_path / "bundle" / "BUILDINFO.json").write_text("{}", encoding="utf-8")
    (tmp_path / "out").mkdir()
    tmp_path.chmod(0o777)
    for path in tmp_path.rglob("*"):
        path.chmod(0o777 if path.is_dir() else 0o666)
    return tmp_path


def test_the_installer_script_compiles(work: Path) -> None:
    done = _iscc(
        work,
        r"/DBundleDir=Z:\work\bundle",
        "/DAppVersion=0.1.0.dev0",
        "/DVersionDir=0.1.0.dev0-1",
        "/DNumericVersion=0.1.0.1",
        r"/DOutputDir=Z:\work\out",
        "/DOutputName=MeshForge-Agent-Setup-test",
    )
    out = done.stdout + done.stderr
    assert done.returncode == 0, out[-3000:]
    assert "Successful compile" in out
    assert "warning" not in out.lower(), out[-3000:]
    assert (work / "out" / "MeshForge-Agent-Setup-test.exe").stat().st_size > 100_000


def test_the_script_refuses_to_compile_without_the_builders_defines(
    work: Path,
) -> None:
    done = _iscc(work)
    assert done.returncode != 0
    assert "BundleDir is not defined" in done.stdout + done.stderr
