"""Tester (task 0.1.5), V0.1 criterion: generated proto/gRPC code must import
with the *base* install (no ``controller``/``agent``/``gpu`` extras) since
``grpcio``/``protobuf`` are core dependencies (``sdk`` needs to talk gRPC too).

Mirrors ``tests/component/test_base_install.py``'s isolation (own copy of the
repository, own ``UV_CACHE_DIR``, real ``uv sync --locked --no-dev``) but
targets the generated proto package specifically. Never modifies ``src/``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.component

REPO_ROOT = Path(__file__).resolve().parents[2]

COPY_EXCLUDE = {
    ".venv",
    ".git",
    "__pycache__",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    "dist",
}


def _copy_repo(dest: Path) -> None:
    def ignore(_dir: str, names: list[str]) -> set[str]:
        return {n for n in names if n in COPY_EXCLUDE}

    shutil.copytree(REPO_ROOT, dest, ignore=ignore)


@pytest.fixture(scope="module")
def base_install(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("meshforge-base-install-protos")
    project_dir = root / "clean"
    cache_dir = root / "uv-cache"
    _copy_repo(project_dir)

    env = dict(os.environ)
    env["UV_CACHE_DIR"] = str(cache_dir)

    result = subprocess.run(
        ["uv", "sync", "--locked", "--no-dev"],
        cwd=project_dir,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert result.returncode == 0, (
        f"`uv sync --locked --no-dev` failed:\nstdout={result.stdout}\n"
        f"stderr={result.stderr}"
    )
    return project_dir


def _run_python(project_dir: Path, code: str) -> subprocess.CompletedProcess[str]:
    python = project_dir / ".venv" / "bin" / "python3"
    return subprocess.run(
        [str(python), "-c", code],
        cwd=project_dir,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


@pytest.mark.parametrize(
    "module",
    [
        "meshforge.common.proto.meshforge.v1.common_pb2",
        "meshforge.common.proto.meshforge.v1.agent_pb2",
        "meshforge.common.proto.meshforge.v1.agent_pb2_grpc",
        "meshforge.common.proto.meshforge.v1.gpu_pb2",
        "meshforge.common.proto.meshforge.v1.gpu_pb2_grpc",
    ],
)
def test_generated_proto_module_imports_on_base_install(
    base_install: Path, module: str
) -> None:
    result = _run_python(base_install, f"import {module}; print('OK')")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "OK"


def test_base_install_can_build_a_real_grpc_stub_and_message(
    base_install: Path,
) -> None:
    code = (
        "from meshforge.common.proto.meshforge.v1 import agent_pb2_grpc, agent_pb2, common_pb2\n"
        "import grpc\n"
        "channel = grpc.insecure_channel('127.0.0.1:0')\n"
        "stub = agent_pb2_grpc.AgentControlStub(channel)\n"
        "assert callable(stub.Session)\n"
        "msg = agent_pb2.AgentMessage(message_id='m', hello=agent_pb2.Hello(node_id='n'))\n"
        "assert msg.WhichOneof('payload') == 'hello'\n"
        "assert isinstance(common_pb2.FAILURE_TYPE_OOM, int)\n"
        "print('OK')\n"
    )
    result = _run_python(base_install, code)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "OK"


def test_proto_codec_imports_on_base_install(base_install: Path) -> None:
    """proto_codec depends on meshforge.common.errors + generated common_pb2;
    both must be reachable without the controller/agent/gpu extras."""
    code = (
        "from meshforge.common.proto_codec import failure_type_to_proto\n"
        "from meshforge.common.errors import FailureType\n"
        "assert failure_type_to_proto(FailureType.OOM) != 0\n"
        "print('OK')\n"
    )
    result = _run_python(base_install, code)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "OK"
