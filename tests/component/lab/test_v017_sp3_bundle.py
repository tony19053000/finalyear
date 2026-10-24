"""Component test for spike SP3 (V0.17 G0): build the agent bundle for real, twice,
then move it and run it with no system Python and no uv reachable by name.

Slow (minutes on a cold cache: downloads python-build-standalone and the locked
wheels into ``~/.cache/meshforge-bundle``), Linux only for now (the Windows layout
is untested until SP1's runner). Checks:

* two builds into different folders -> byte-identical ``MANIFEST.sha256`` and
  ``.tar.xz`` (V0.17 G1), the download within the agent budget;
* the moved bundle: ``meshforge-agent --version``, ``hardware show``,
  ``compute --help``, the bundle ``uv``, and the interpreter's own report - with
  ``PATH=/usr/bin:/bin`` and with a toolbox ``PATH`` that holds only
  ``realpath``/``dirname``; a launcher reached through a symlink;
* a real offline env build through the agent's EnvBuilder with the bundle's uv,
  with NO Python on ``PATH``: the venv is built on the bundled interpreter (G1 F1);
* running all of that leaves the bundle byte-identical to its manifest.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Final

import pytest

pytest.importorskip("uv", reason="needs the agent extra (the lock-pinned uv wheel)")

REPO: Final = Path(__file__).resolve().parents[3]
LAB_DIR: Final = REPO / "lab"
BUILD: Final = REPO / "scripts" / "build_bundle.py"
if str(LAB_DIR) not in sys.path:
    sys.path.insert(0, str(LAB_DIR))

from spikes._sp3 import checks  # noqa: E402

pytestmark = [
    pytest.mark.slow,
    pytest.mark.timeout(1200),
    pytest.mark.skipif(sys.platform != "linux", reason="Linux bundle layout only"),
]


def _build(out: Path) -> Path:
    # tests/component/conftest.py points SSL_CERT_FILE at the test fabric CA;
    # the build downloads from the internet and needs the system trust store.
    env = {k: v for k, v in os.environ.items() if k != "SSL_CERT_FILE"}
    subprocess.run(
        [sys.executable, str(BUILD), "--role", "agent", "--out", str(out), "--force"],
        check=True,
        timeout=1100,
        env=env,
    )
    (bundle,) = [
        p for p in out.iterdir() if p.name.startswith("meshforge-") and p.is_dir()
    ]
    return bundle


def _version() -> str:
    from meshforge import __version__

    return __version__


def test_agent_bundle_is_reproducible_relocatable_and_builds_envs(
    tmp_path: Path,
) -> None:
    first = _build(tmp_path / "build one")
    second = _build(tmp_path / "build-two")
    assert (first / "MANIFEST.sha256").read_bytes() == (
        second / "MANIFEST.sha256"
    ).read_bytes()
    archive = first.with_name(first.name + ".tar.xz")
    assert (
        archive.read_bytes() == (second.with_name(second.name + ".tar.xz")).read_bytes()
    )
    assert archive.stat().st_size < 100_000_000  # Q17 agent download budget
    info = json.loads((first / "BUILDINFO.json").read_text(encoding="utf-8"))
    assert info["role"] == "agent" and info["python"]["version"] == "3.12.14"

    moved_root = tmp_path / "moved here" / "deeper"
    moved_root.mkdir(parents=True)
    bundle = moved_root / first.name
    first.rename(bundle)
    home = tmp_path / "home"
    toolbox = str(checks.make_toolbox(tmp_path / "toolbox"))

    for path_value, kind in ((checks.SYSTEM_PATH, "system"), (toolbox, "toolbox")):
        results = {
            r.name: r for r in checks.relocation_checks(bundle, home, path_value, kind)
        }
        for result in results.values():
            assert result.ok, (kind, result)
        assert (
            results["meshforge-agent --version"]
            .stdout.strip()
            .startswith(f"meshforge-agent {_version()} (git ")
        )
        assert "cpu:" in results["meshforge-agent hardware show"].stdout
        assert "Usage: compute" in results["compute --help"].stdout
        where = json.loads(results["python: where am I"].stdout)
        assert Path(where["executable"]).is_relative_to(bundle)
        assert Path(where["prefix"]) == bundle / "python"
        assert all(
            not p or Path(p).is_relative_to(bundle / "python") for p in where["path"]
        ), where["path"]
    via_link = checks.symlinked_launcher(bundle, tmp_path / "on-path", home, "toolbox")
    assert via_link.ok, via_link

    # (d) a real env build with the bundle's own uv and NO Python on PATH (the
    # toolbox holds only realpath/dirname): ensure_target installs for the
    # bundled interpreter itself, and since G1 (SP3 F1) ensure_venv builds on
    # it too instead of failing python_not_found.
    python_bin = bundle / "python" / "bin"
    probe = checks.env_probe(bundle, tmp_path / "probe", toolbox, "toolbox")
    assert probe["exit"] == 0, probe
    assert probe["uv_argv"] == [str(python_bin / "uv")]
    assert probe["venv_interpreter"] == str(python_bin / "python3.12")
    for kind in ("venv", "target"):
        assert probe[kind]["cache"] == "built", probe
        assert probe[kind]["run"]["exit"] == 0, probe
        assert probe[kind]["run"]["out"].startswith("1.0 "), probe

    # nothing above wrote into the program folder (it is read-only once installed)
    assert checks.verify_intact(bundle) == []
