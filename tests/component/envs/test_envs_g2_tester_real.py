"""V0.5 G2 tester: real-uv, real-process checks of D85-D87 and criterion 5.

Offline (wheelhouse + --no-index). Covers: many processes racing for several hashes,
requirement lines uv accepts but the hash cannot cover (must fail, never build), a
warm cache from a *fresh process* within the 3 s bound with the cold build recorded,
a job trying to damage its env, and ``meshforge-agent prepare`` with no Docker.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("uv", reason="needs the agent extra (the uv wheel)")

from mf_envs_support import make_wheel, offline_builder

from meshforge.agent.envs import EnvError

pytestmark = [pytest.mark.timeout(240)]

SUPPORT = Path(__file__).resolve().parents[2] / "_support"

CHILD = """\
import asyncio, json, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[4])
from mf_envs_support import offline_builder

async def main():
    root, wheels, req = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3].encode()
    t0 = time.monotonic()
    b = offline_builder(root, wheels)
    built = await b.ensure_venv(python=None, requirements=req, deadline=1e18)
    code = "import mfdemo; print(mfdemo.VERSION)"
    import subprocess
    out = subprocess.run([str(built.python), "-I", "-c", code], capture_output=True)
    print(json.dumps({"cache": built.cache, "hash": built.env_hash,
                      "version": out.stdout.decode().strip(),
                      "total_s": time.monotonic() - t0}), flush=True)

asyncio.run(main())
"""


@pytest.fixture
def wheelhouse(tmp_path: Path) -> Path:
    folder = tmp_path / "wheels"
    for v in ("1.0", "2.0", "3.0"):
        make_wheel(folder, "mfdemo", v)
    return folder


def _spawn(script: Path, root: Path, wheels: Path, req: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, str(script), str(root), str(wheels), req, str(SUPPORT)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _finish(proc: subprocess.Popen[str]) -> dict[str, object]:
    out, err = proc.communicate(timeout=200)
    assert proc.returncode == 0, err
    return json.loads(out.strip().splitlines()[-1])  # type: ignore[no-any-return]


def test_processes_racing_over_several_hashes_build_each_hash_once(
    tmp_path: Path, wheelhouse: Path
) -> None:
    script = tmp_path / "child.py"
    script.write_text(CHILD)
    root = tmp_path / "envs"
    plan = ["mfdemo==1.0"] * 3 + ["mfdemo==2.0"] * 3 + ["mfdemo==3.0"] * 2
    procs = [_spawn(script, root, wheelhouse, req) for req in plan]
    results = [_finish(p) for p in procs]
    by_hash: dict[str, list[dict[str, object]]] = {}
    for req, res in zip(plan, results, strict=True):
        assert res["version"] == req.split("==")[1]
        by_hash.setdefault(str(res["hash"]), []).append(res)
    assert len(by_hash) == 3
    for group in by_hash.values():
        assert sum(r["cache"] == "built" for r in group) == 1, group
    venvs = sorted(p.name for p in (root / "venv").iterdir())
    assert len(venvs) == 3 and not any(n.endswith(".building") for n in venvs)


def test_cold_build_is_recorded_and_a_fresh_process_starts_warm_within_3s(
    tmp_path: Path, wheelhouse: Path
) -> None:
    """Criterion 5: warm env cache -> job start overhead <= 3 s; cold build recorded."""
    script = tmp_path / "child.py"
    script.write_text(CHILD)
    root = tmp_path / "envs"
    cold = _finish(_spawn(script, root, wheelhouse, "mfdemo==1.0"))
    assert cold["cache"] == "built"
    started = time.monotonic()
    warm = _finish(_spawn(script, root, wheelhouse, "mfdemo==1.0"))
    wall = time.monotonic() - started
    print(  # noqa: T201
        f"\nG2 tester: cold {cold['total_s']:.2f}s, warm env {warm['total_s']:.3f}s, "
        f"warm incl. interpreter start {wall:.2f}s"
    )
    assert warm["cache"] == "hit" and warm["version"] == "1.0"
    assert float(str(warm["total_s"])) <= 3.0
    assert wall <= 3.0 + 3.0  # process + imports + env + a python start, generous
    marker = next((root / "venv").glob("*/.meshforge-env.json"))
    assert json.loads(marker.read_text())["env_hash"] == cold["hash"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "line", ["mfdemo @ {wheel}", "mfdemo @ ./wheels/mfdemo-1.0-py3-none-any.whl"]
)
async def test_local_path_requirements_never_produce_an_env(
    tmp_path: Path, wheelhouse: Path, line: str
) -> None:
    """Lines the hash cannot cover must either be refused up front or fail the
    install; in no case may a venv appear for them."""
    builder = offline_builder(tmp_path / "envs", wheelhouse)
    wheel = next(wheelhouse.glob("*1.0*.whl"))  # noqa: ASYNC240
    req = line.format(wheel=wheel).encode()
    with pytest.raises(EnvError):
        await builder.ensure_venv(python=None, requirements=req, deadline=1e18)
    venvs = tmp_path / "envs" / "venv"
    assert not venvs.exists() or list(venvs.iterdir()) == []


JOB = """\
import os, shutil, sys, pathlib
import mfdemo
pkg = pathlib.Path(mfdemo.__file__).parent
res = {}
for name, fn in {
    "write": lambda: (pkg / "__init__.py").write_text("hacked"),
    "create": lambda: (pkg / "new.py").write_text("x"),
    "delete": lambda: (pkg / "__init__.py").unlink(),
    "rmtree": lambda: shutil.rmtree(pkg),
    "pip_style_install": lambda: (pathlib.Path(sys.prefix) / "lib" / "evil").mkdir(),
}.items():
    try:
        fn(); res[name] = "ALLOWED"
    except OSError:
        res[name] = "blocked"
print(res)
"""


@pytest.mark.asyncio
@pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="needs POSIX permission bits and a non-root user",
)
async def test_a_job_cannot_damage_a_real_venv_and_the_next_job_is_unaffected(
    tmp_path: Path, wheelhouse: Path
) -> None:
    builder = offline_builder(tmp_path / "envs", wheelhouse)
    built = await builder.ensure_venv(
        python=None, requirements=b"mfdemo==1.0\n", deadline=1e18
    )
    out = subprocess.run(  # noqa: ASYNC221
        [str(built.python), "-c", JOB], capture_output=True, text=True, timeout=60
    )
    assert "ALLOWED" not in out.stdout, out.stdout + out.stderr
    again = await builder.ensure_venv(
        python=None, requirements=b"mfdemo==1.0\n", deadline=1e18
    )
    assert again.cache == "hit"
    check = subprocess.run(  # noqa: ASYNC221
        [str(again.python), "-I", "-c", "import mfdemo; print(mfdemo.VERSION)"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert check.stdout.strip() == "1.0"


@pytest.mark.asyncio
async def test_building_dir_never_visible_to_readers_during_a_slow_build(
    tmp_path: Path, wheelhouse: Path
) -> None:
    """While a build is running, no ``<hash>`` dir (what a job would use) exists."""
    from mf_envs_support import slow_uv_argv

    builder = offline_builder(
        tmp_path / "envs", wheelhouse, uv_argv=slow_uv_argv(tmp_path, 1.5)
    )
    task = asyncio.create_task(
        builder.ensure_target(requirements=b"mfdemo==1.0\n", deadline=1e18)
    )
    saw_building = False
    for _ in range(40):
        await asyncio.sleep(0.05)
        kind = tmp_path / "envs" / "subprocess"
        if kind.exists():
            names = [p.name for p in kind.iterdir()]
            assert all(
                n.endswith(".building") or builder.is_ready("subprocess", n)
                for n in names
            ), names
            saw_building = saw_building or bool(names)
        if task.done():
            break
    built = await task
    assert saw_building and built.cache == "built"


def test_prepare_without_docker_reports_unavailable_and_does_not_fail(
    tmp_path: Path,
) -> None:
    env = {
        **os.environ,
        "DOCKER_HOST": "unix:///nonexistent/mf-no-docker.sock",
        "MESHFORGE_DATA_DIR": str(tmp_path / "data"),
    }
    out = subprocess.run(
        [sys.executable, "-m", "meshforge.agent", "prepare", "--json"],
        capture_output=True,
        text=True,
        timeout=90,
        env=env,
    )
    assert out.returncode == 0, out.stdout + out.stderr
    doc = json.loads(out.stdout)
    assert doc["ok"] is True and doc["images"] == []
    assert str(doc["docker"]).startswith("unavailable")
    assert "uv" in str(doc["uv"])
