"""Task 0.1.5: protobuf contracts and reproducible code generation.

Covers: generated modules import from their package path, oneof envelopes
round-trip, service stubs exist with the right streaming shape, proto style
rules hold, and ``scripts/gen_protos.py --check`` passes on the committed tree
but fails after a proto edit / stray file in a temporary copy (roadmap V0.1
success criterion 4). The script is run in a subprocess with the current
interpreter; no shell is involved, so this works on Linux and Windows.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import grpc
import pytest
from google.protobuf.descriptor import Descriptor, EnumDescriptor, FileDescriptor

from meshforge.common.hashing import Digest, sha256_bytes
from meshforge.common.proto.meshforge.v1 import (
    agent_pb2,
    agent_pb2_grpc,
    common_pb2,
    gpu_pb2,
    gpu_pb2_grpc,
)

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "gen_protos.py"
OUT_DIR = REPO_ROOT / "src" / "meshforge" / "common" / "proto"
FILE_DESCRIPTORS: list[FileDescriptor] = [
    common_pb2.DESCRIPTOR,
    agent_pb2.DESCRIPTOR,
    gpu_pb2.DESCRIPTOR,
]


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("gen_protos", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


@pytest.fixture
def repo_copy(tmp_path: Path) -> Path:
    """Minimal copy of the repo containing what gen_protos.py reads and writes."""
    shutil.copytree(REPO_ROOT / "proto", tmp_path / "proto")
    shutil.copytree(
        OUT_DIR,
        tmp_path / "src" / "meshforge" / "common" / "proto",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    shutil.copy2(REPO_ROOT / "pyproject.toml", tmp_path / "pyproject.toml")
    return tmp_path


# ---------------------------------------------------------------------------
# Generated code
# ---------------------------------------------------------------------------


def test_descriptor_names_are_canonical() -> None:
    assert [fd.name for fd in FILE_DESCRIPTORS] == [
        "meshforge/v1/common.proto",
        "meshforge/v1/agent.proto",
        "meshforge/v1/gpu.proto",
    ]
    assert {fd.package for fd in FILE_DESCRIPTORS} == {"meshforge.v1"}


def test_cross_file_types_resolve_to_one_module() -> None:
    # agent.proto imports common.proto; the field type must be the same class
    # as the one in common_pb2 (a broken import rewrite would fail to import or
    # register a duplicate file).
    status = agent_pb2.AttemptStatus(failure=common_pb2.Failure(message="x"))
    assert type(status.failure) is common_pb2.Failure


def test_agent_message_oneof_round_trip() -> None:
    msg = agent_pb2.AgentMessage(
        message_id="01J00000000000000000000000",
        hello=agent_pb2.Hello(
            node_id="node-1",
            protocol_version=common_pb2.PROTOCOL_VERSION_1,
            agent_version="0.1.0.dev0",
            hostname="laptop-1",
        ),
    )
    msg.sent_at.FromSeconds(1_700_000_000)
    parsed = agent_pb2.AgentMessage.FromString(msg.SerializeToString())
    assert parsed == msg
    assert parsed.WhichOneof("payload") == "hello"
    assert parsed.hello.protocol_version == common_pb2.PROTOCOL_VERSION_1
    assert parsed.sent_at.seconds == 1_700_000_000

    # Setting another member of the oneof clears the previous one.
    parsed.attempt_status.CopyFrom(
        agent_pb2.AttemptStatus(
            job_id="job",
            attempt_id="att",
            fencing_token=3,
            state=common_pb2.ATTEMPT_STATE_FAILED,
            failure=common_pb2.Failure(
                failure_type=common_pb2.FAILURE_TYPE_OOM, message="oom", retryable=True
            ),
        )
    )
    assert parsed.WhichOneof("payload") == "attempt_status"
    assert not parsed.HasField("hello")
    again = agent_pb2.AgentMessage.FromString(parsed.SerializeToString())
    assert again.attempt_status.failure.failure_type == common_pb2.FAILURE_TYPE_OOM
    assert again.attempt_status.fencing_token == 3


def test_controller_message_oneof_round_trip() -> None:
    msg = agent_pb2.ControllerMessage(
        message_id="cmd-1", ack=agent_pb2.Ack(acked_message_id="m-1")
    )
    parsed = agent_pb2.ControllerMessage.FromString(msg.SerializeToString())
    assert parsed.WhichOneof("payload") == "ack"
    assert parsed.ack.acked_message_id == "m-1"
    assert agent_pb2.ControllerMessage().WhichOneof("payload") is None


def test_envelopes_cover_architecture_payloads() -> None:
    def members(msg: Descriptor) -> set[str]:
        names: set[str] = set()
        for field in msg.oneofs_by_name["payload"].fields:
            assert field.message_type is not None
            names.add(field.message_type.name)
        return names

    assert members(agent_pb2.AgentMessage.DESCRIPTOR) == {
        "Hello",
        "Reconcile",
        "Heartbeat",
        "ResourceUpdate",
        "CacheReport",
        "NetProbeResult",
        "AttemptStatus",
        "LogChunk",
        "Ack",
    }
    assert members(agent_pb2.ControllerMessage.DESCRIPTOR) == {
        "StartAttempt",
        "CancelAttempt",
        "ProbeRequest",
        "PrepareEnv",
        "GcRequest",
        "Ack",
    }


def test_agent_can_ack_controller_commands() -> None:
    msg = agent_pb2.AgentMessage(
        message_id="m-2", ack=agent_pb2.Ack(acked_message_id="cmd-1")
    )
    parsed = agent_pb2.AgentMessage.FromString(msg.SerializeToString())
    assert parsed.WhichOneof("payload") == "ack"
    assert parsed.ack.acked_message_id == "cmd-1"


def _envelope_payloads() -> list[Descriptor]:
    payloads: list[Descriptor] = []
    for envelope in (agent_pb2.AgentMessage, agent_pb2.ControllerMessage):
        for field in envelope.DESCRIPTOR.oneofs_by_name["payload"].fields:
            assert field.message_type is not None
            payloads.append(field.message_type)
    return payloads


def test_attempt_payloads_carry_fencing_token() -> None:
    # Every envelope payload that names an attempt must carry its fencing token,
    # so stale reports/commands from a superseded attempt can be rejected.
    with_attempt = [m for m in _envelope_payloads() if "attempt_id" in m.fields_by_name]
    assert {m.name for m in with_attempt} >= {
        "AttemptStatus",
        "LogChunk",
        "StartAttempt",
        "CancelAttempt",
    }
    for msg in with_attempt:
        token = msg.fields_by_name.get("fencing_token")
        assert token is not None, f"{msg.full_name} has attempt_id but no fencing_token"
        assert token.type == token.TYPE_UINT64, msg.full_name


def test_blob_ref_digest_round_trips_with_hashing_digest() -> None:
    data = b"meshforge blob"
    digest = sha256_bytes(data)
    ref = common_pb2.BlobRef(digest=str(digest), size_bytes=len(data))
    parsed = common_pb2.BlobRef.FromString(ref.SerializeToString())
    assert Digest.parse(parsed.digest) == digest
    assert parsed.digest == f"sha256:{digest.hex}"
    assert parsed.size_bytes == len(data)
    assert set(common_pb2.BlobRef.DESCRIPTOR.fields_by_name) == {"digest", "size_bytes"}
    with pytest.raises(ValueError, match="sha256"):
        Digest.parse(common_pb2.BlobRef(digest=digest.hex).digest)  # missing prefix


def test_agent_control_session_is_bidi_stream() -> None:
    method = agent_pb2.DESCRIPTOR.services_by_name["AgentControl"].methods_by_name[
        "Session"
    ]
    assert method.client_streaming and method.server_streaming
    assert method.input_type.name == "AgentMessage"
    assert method.output_type.name == "ControllerMessage"


def test_gpu_service_methods() -> None:
    service = gpu_pb2.DESCRIPTOR.services_by_name["GpuService"]
    methods = {
        m.name: (m.client_streaming, m.server_streaming) for m in service.methods
    }
    assert methods == {
        "OpenSession": (False, False),
        "PutTensor": (True, False),
        "LoadModule": (False, False),
        "Call": (False, False),
        "GetTensor": (False, True),
        "Free": (False, False),
        "TrainStep": (False, False),
        "CloseSession": (False, False),
    }


def test_service_stubs_exist() -> None:
    with grpc.insecure_channel("localhost:1") as channel:  # lazy; never connects
        agent_stub = agent_pb2_grpc.AgentControlStub(channel)
        gpu_stub = gpu_pb2_grpc.GpuServiceStub(channel)
        assert callable(agent_stub.Session)
        for name in ("OpenSession", "PutTensor", "Call", "GetTensor", "CloseSession"):
            assert callable(getattr(gpu_stub, name))
    assert hasattr(agent_pb2_grpc, "AgentControlServicer")
    assert hasattr(agent_pb2_grpc, "add_AgentControlServicer_to_server")
    assert hasattr(gpu_pb2_grpc, "GpuServiceServicer")
    assert hasattr(gpu_pb2_grpc, "add_GpuServiceServicer_to_server")


def _all_enums() -> Iterator[EnumDescriptor]:
    def walk(msg: Descriptor) -> Iterator[EnumDescriptor]:
        yield from msg.enum_types
        for nested in msg.nested_types:
            yield from walk(nested)

    for fd in FILE_DESCRIPTORS:
        yield from fd.enum_types_by_name.values()
        for msg in fd.message_types_by_name.values():
            yield from walk(msg)


def test_enums_follow_style_guide() -> None:
    enums = list(_all_enums())
    assert len(enums) >= 5
    for enum in enums:
        zero = enum.values_by_number[0].name
        assert zero.endswith("_UNSPECIFIED"), f"{enum.full_name}: zero value is {zero}"
        prefix = zero.removesuffix("UNSPECIFIED")
        for value in enum.values:
            assert value.name.startswith(prefix), f"{enum.full_name}.{value.name}"


def test_generated_files_are_marked_lf_and_path_free() -> None:
    files = [p for p in OUT_DIR.rglob("*") if p.suffix in {".py", ".pyi"}]
    assert len(files) >= 3 * 4 + 3  # 3 protos x (pb2, pb2_grpc) x (.py, .pyi) + inits
    for path in files:
        data = path.read_bytes()
        assert b"\r" not in data, path
        assert data.startswith(b"# Code generated by scripts/gen_protos.py"), path
        assert str(REPO_ROOT).encode() not in data, path
        assert b"from meshforge.v1 import" not in data, path


# ---------------------------------------------------------------------------
# gen_protos.py
# ---------------------------------------------------------------------------


def test_check_passes_on_committed_tree() -> None:
    result = _run("--check")
    assert result.returncode == 0, result.stderr
    assert "up to date" in result.stdout


def test_check_fails_after_proto_edit_then_regenerate_fixes(repo_copy: Path) -> None:
    proto = repo_copy / "proto" / "meshforge" / "v1" / "agent.proto"
    text = proto.read_text(encoding="utf-8")
    proto.write_text(
        text.replace(
            "  uint64 sequence = 1;\n", "  uint64 sequence = 1;\n  string extra = 2;\n"
        ),
        encoding="utf-8",
    )
    assert "string extra = 2;" in proto.read_text(encoding="utf-8")

    result = _run("--check", "--repo-root", str(repo_copy))
    assert result.returncode == 1, result.stdout + result.stderr
    assert "differs: meshforge/v1/agent_pb2.py" in result.stderr

    regen = _run("--repo-root", str(repo_copy))
    assert regen.returncode == 0, regen.stderr
    again = _run("--check", "--repo-root", str(repo_copy))
    assert again.returncode == 0, again.stderr


def test_regeneration_is_byte_identical(repo_copy: Path) -> None:
    out = repo_copy / "src" / "meshforge" / "common" / "proto"
    before = {p.relative_to(out): p.read_bytes() for p in out.rglob("*") if p.is_file()}
    result = _run("--repo-root", str(repo_copy))
    assert result.returncode == 0, result.stderr
    assert "0 changed" in result.stdout
    after = {p.relative_to(out): p.read_bytes() for p in out.rglob("*") if p.is_file()}
    assert after == before


def test_check_detects_and_generate_removes_stale_files(repo_copy: Path) -> None:
    stale = (
        repo_copy
        / "src"
        / "meshforge"
        / "common"
        / "proto"
        / "meshforge"
        / "v1"
        / "old_pb2.py"
    )
    stale.write_text("# leftover\n", encoding="utf-8")
    result = _run("--check", "--repo-root", str(repo_copy))
    assert result.returncode == 1
    assert "stale" in result.stderr
    assert _run("--repo-root", str(repo_copy)).returncode == 0
    assert not stale.exists()


def test_invalid_proto_is_an_error_not_drift(repo_copy: Path) -> None:
    proto = repo_copy / "proto" / "meshforge" / "v1" / "gpu.proto"
    proto.write_text(
        proto.read_text(encoding="utf-8") + "\nmessage Broken {\n", encoding="utf-8"
    )
    result = _run("--check", "--repo-root", str(repo_copy))
    assert result.returncode == 2
    assert "protoc failed" in result.stderr


def test_version_compat_rejects_newer_gencode() -> None:
    gen = _load_script()
    gen.check_version_compat("protobuf", (7, 35, 1), (7, 36, 1), (7, 36, 0))  # ok
    with pytest.raises(gen.GenError, match="installed runtime"):
        gen.check_version_compat("protobuf", (7, 37, 0), (7, 36, 1), (7, 36, 0))
    with pytest.raises(gen.GenError, match=r"pyproject\.toml"):
        gen.check_version_compat("grpcio", (1, 85, 0), (1, 85, 0), (1, 84, 0))


def test_parse_version() -> None:
    gen = _load_script()
    assert gen.parse_version("7.36.1") == (7, 36, 1)
    assert gen.parse_version("1.84") == (1, 84, 0)
    assert gen.parse_version("1.84.0rc1") == (1, 84, 0)
