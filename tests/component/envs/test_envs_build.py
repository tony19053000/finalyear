"""V0.5 G2 (D86): real ``uv`` builds - venv and ``--target`` kinds, reuse, failure,
two processes racing for one hash, and a build killed mid-way.

Offline: every install comes from a wheelhouse written by the test
(``mf_envs_support.make_wheel``) with ``--no-index``. Needs the ``uv`` wheel (agent
extra); skips with the reason without it.
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

from mf_envs_support import (
    build_child_argv,
    make_wheel,
    offline_builder,
)

from meshforge.agent.envs import EnvError
from meshforge.agent.envs._tool import tool_env
from meshforge.agent.envs.builder import EnvBuilder
from meshforge.common.errors import FailureType

pytestmark = [pytest.mark.timeout(180)]

REQ = b"mfdemo==1.0\n"


@pytest.fixture
def wheelhouse(tmp_path: Path) -> Path:
    folder = tmp_path / "wheels"
    make_wheel(folder, "mfdemo", "1.0")
    make_wheel(folder, "mfdemo", "2.0")
    return folder


def _run_python(python: Path | str, code: str, **env: str) -> str:
    isolated = [] if env else ["-I"]  # -I would ignore PYTHONPATH
    out = subprocess.run(
        [str(python), *isolated, "-c", code],
        capture_output=True,
        check=True,
        timeout=60,
        env={**tool_env(), **env},
    )
    return out.stdout.decode().strip()


def _visible(root: Path, kind: str) -> list[str]:
    folder = root / kind
    return sorted(p.name for p in folder.iterdir()) if folder.exists() else []


@pytest.mark.asyncio
async def test_venv_build_then_reuse(tmp_path: Path, wheelhouse: Path) -> None:
    builder = offline_builder(tmp_path / "envs", wheelhouse)
    started = time.monotonic()
    cold = await builder.ensure_venv(python=None, requirements=REQ, deadline=1e18)
    cold_s = time.monotonic() - started
    assert cold.cache == "built"
    assert _run_python(cold.python, "import mfdemo; print(mfdemo.VERSION)") == "1.0"
    # the venv's interpreter is isolated: the agent's own packages are not there
    probe = "import importlib.util as u; print(u.find_spec('pydantic') is None)"
    assert _run_python(cold.python, probe) == "True"
    started = time.monotonic()
    warm = await _fresh(builder).ensure_venv(
        python=None, requirements=REQ, deadline=1e18
    )
    warm_s = time.monotonic() - started
    assert warm.cache == "hit"
    assert warm.env_hash == cold.env_hash and warm.path == cold.path
    assert _visible(builder.root, "venv") == [cold.env_hash]
    print(f"venv cold {cold_s:.2f} s, warm {warm_s:.3f} s (not a benchmark claim)")  # noqa: T201
    assert warm_s < 3.0


def _fresh(builder: EnvBuilder) -> EnvBuilder:
    """A new builder on the same root (a later agent process: no in-memory state)."""
    return EnvBuilder(builder.config, uv_argv=builder.uv_argv)


@pytest.mark.asyncio
async def test_version_change_builds_a_second_env(
    tmp_path: Path, wheelhouse: Path
) -> None:
    builder = offline_builder(tmp_path / "envs", wheelhouse)
    one = await builder.ensure_venv(python=None, requirements=REQ, deadline=1e18)
    two = await builder.ensure_venv(
        python=None, requirements=b"mfdemo==2.0\n", deadline=1e18
    )
    assert one.env_hash != two.env_hash and two.cache == "built"
    assert _run_python(two.python, "import mfdemo; print(mfdemo.VERSION)") == "2.0"


@pytest.mark.asyncio
async def test_explicit_python_version_is_honoured(
    tmp_path: Path, wheelhouse: Path
) -> None:
    builder = offline_builder(tmp_path / "envs", wheelhouse)
    wanted = f"{sys.version_info[0]}.{sys.version_info[1]}"
    built = await builder.ensure_venv(python=wanted, requirements=b"", deadline=1e18)
    version = _run_python(
        built.python, "import sys; print('%d.%d' % sys.version_info[:2])"
    )
    assert version == wanted


@pytest.mark.asyncio
async def test_unknown_python_version_is_python_not_found(
    tmp_path: Path, wheelhouse: Path
) -> None:
    builder = offline_builder(tmp_path / "envs", wheelhouse)
    with pytest.raises(EnvError) as info:
        await builder.ensure_venv(python="3.2", requirements=b"", deadline=1e18)
    assert info.value.reason == "python_not_found"
    assert _visible(builder.root, "venv") == []


@pytest.mark.asyncio
async def test_target_build_then_reuse(tmp_path: Path, wheelhouse: Path) -> None:
    builder = offline_builder(tmp_path / "envs", wheelhouse)
    cold = await builder.ensure_target(requirements=REQ, deadline=1e18)
    assert cold.cache == "built"
    code = "import mfdemo; print(mfdemo.VERSION)"
    assert _run_python(sys.executable, code, PYTHONPATH=str(cold.path)) == "1.0"
    warm = await builder.ensure_target(requirements=REQ, deadline=1e18)
    assert warm.cache == "hit"


@pytest.mark.asyncio
async def test_failed_install_leaves_nothing(tmp_path: Path, wheelhouse: Path) -> None:
    builder = offline_builder(tmp_path / "envs", wheelhouse)
    output: list[bytes] = []

    async def sink(_stream: str, data: bytes) -> None:
        output.append(data)

    with pytest.raises(EnvError) as info:
        await builder.ensure_target(
            requirements=b"mfdemo==9.9\n", deadline=1e18, on_output=sink
        )
    assert info.value.reason == "setup_failed"
    assert info.value.failure_type is FailureType.ENV_ERROR
    assert b"mfdemo" in b"".join(output)  # the installer's own message reached the job
    assert _visible(builder.root, "subprocess") == []
    assert not any((builder.root / "staging").iterdir())


@pytest.mark.asyncio
async def test_build_over_the_deadline_is_env_timeout_and_leaves_nothing(
    tmp_path: Path, wheelhouse: Path
) -> None:
    from mf_envs_support import slow_uv_argv

    builder = offline_builder(
        tmp_path / "envs", wheelhouse, uv_argv=slow_uv_argv(tmp_path, 30)
    )
    with pytest.raises(EnvError) as info:
        await builder.ensure_target(requirements=REQ, deadline=time.monotonic() + 1.5)
    assert info.value.reason == "env_timeout"
    assert info.value.failure_type is FailureType.TIMEOUT
    assert _visible(builder.root, "subprocess") == []


def _start_child(argv: list[str]) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=os.environ.copy()
    )


def test_two_processes_one_hash_build_once(tmp_path: Path, wheelhouse: Path) -> None:
    root = tmp_path / "envs"
    argv = build_child_argv(tmp_path, root, wheelhouse, delay_s=1.0)
    children = [_start_child(argv), _start_child(argv)]
    results = []
    for child in children:
        out, err = child.communicate(timeout=120)
        assert child.returncode == 0, err.decode()[-2000:]
        results.append(json.loads(out.decode().strip().splitlines()[-1]))
    assert results[0]["hash"] == results[1]["hash"]
    # exactly one process built; the other waited on the lock and reused it
    assert sorted(r["cache"] for r in results) == ["built", "waited"]
    assert _visible(root, "subprocess") == [results[0]["hash"]]


@pytest.mark.skipif(sys.platform == "win32", reason="SIGKILL of a process (POSIX)")
@pytest.mark.asyncio
async def test_build_killed_mid_way_then_a_clean_rebuild(
    tmp_path: Path, wheelhouse: Path
) -> None:
    root = tmp_path / "envs"
    child = _start_child(build_child_argv(tmp_path, root, wheelhouse, delay_s=60))
    try:
        deadline = time.monotonic() + 60
        partial = None
        while time.monotonic() < deadline:
            found = list(
                (root / "subprocess").glob("*.building/partial-from-slow-uv.txt")
            )
            if found:
                partial = found[0]
                break
            await asyncio.sleep(0.05)
        assert partial is not None, "the slow build never started"
        child.kill()  # SIGKILL: no cleanup code runs in the child
        child.wait(timeout=30)
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate()
    digest = partial.parent.name.removesuffix(".building")
    builder = offline_builder(root, wheelhouse)
    assert not builder.is_ready("subprocess", digest)  # never visible half-built
    rebuilt = await builder.ensure_target(requirements=REQ, deadline=1e18)
    assert rebuilt.env_hash == digest and rebuilt.cache == "built"
    assert not (rebuilt.path / "partial-from-slow-uv.txt").exists()
    assert _visible(root, "subprocess") == [digest]


@pytest.mark.asyncio
async def test_hashed_requirements_file_builds_and_a_wrong_hash_fails(
    tmp_path: Path, wheelhouse: Path
) -> None:
    """pip-compile --generate-hashes output survives normalisation end to end:
    real uv installs it in hash-checking mode; a wrong hash is setup_failed."""
    import hashlib

    wheel = next(wheelhouse.glob("mfdemo-1.0-*.whl"))  # noqa: ASYNC240
    good = hashlib.sha256(wheel.read_bytes()).hexdigest()
    work = tmp_path / "work"
    work.mkdir()
    (work / "req.txt").write_text(
        f"# generated by pip-compile\nmfdemo==1.0 \\\n    --hash=sha256:{good}\n"
    )
    builder = offline_builder(tmp_path / "envs", wheelhouse)
    requirements = builder.read_requirements(work, "req.txt")
    assert b"1.0 --hash=sha256:" in requirements
    built = await builder.ensure_venv(
        python=None, requirements=requirements, deadline=1e18
    )
    assert built.cache == "built"
    assert _run_python(built.python, "import mfdemo; print(mfdemo.VERSION)") == "1.0"
    with pytest.raises(EnvError) as info:
        await builder.ensure_venv(
            python=None,
            requirements=f"mfdemo==1.0 --hash=sha256:{'0' * 64}\n".encode(),
            deadline=1e18,
        )
    assert info.value.reason == "setup_failed"


@pytest.mark.asyncio
async def test_a_changed_wheelhouse_gives_a_new_env(
    tmp_path: Path, wheelhouse: Path
) -> None:
    builder = offline_builder(tmp_path / "envs", wheelhouse)
    one = await builder.ensure_target(requirements=REQ, deadline=1e18)
    make_wheel(wheelhouse, "mfother", "1.0")  # the node's wheelhouse changed
    two = await builder.ensure_target(requirements=REQ, deadline=1e18)
    assert two.env_hash != one.env_hash and two.cache == "built"
