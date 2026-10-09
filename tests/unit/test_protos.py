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
        "Pong",
        "HardwareScores",
        "Goodbye",
        "PrepareEnvStatus",  # V0.5 G5
    }
    assert members(agent_pb2.ControllerMessage.DESCRIPTOR) == {
        "StartAttempt",
        "CancelAttempt",
        "ProbeRequest",
        "PrepareEnv",
        "GcRequest",
        "Ack",
        "HelloAck",
        "Ping",
        "RefreshFetchToken",  # V0.5 D95
        "RefreshUploadGrant",  # V0.6 D114
        "RefreshPeerTokens",  # V0.7 G3 D147
    }


# ---------------------------------------------------------------------------
# 0.3.1: additive extension of the V0.1 skeleton (ADR-0002 Updates 2026-09-20)
# ---------------------------------------------------------------------------

#: Every field shipped in V0.1 (task 0.1.5) with its number. V0.3 extends the
#: contract additively; renumbering any of these would silently break old agents.
V01_FIELD_NUMBERS: dict[str, dict[str, int]] = {
    "AgentMessage": {
        "message_id": 1,
        "sent_at": 2,
        "hello": 10,
        "reconcile": 11,
        "heartbeat": 12,
        "resource_update": 13,
        "cache_report": 14,
        "net_probe_result": 15,
        "attempt_status": 16,
        "log_chunk": 17,
        "ack": 18,
    },
    "ControllerMessage": {
        "message_id": 1,
        "sent_at": 2,
        "start_attempt": 10,
        "cancel_attempt": 11,
        "probe_request": 12,
        "prepare_env": 13,
        "gc": 14,
        "ack": 15,
    },
    "Hello": {"node_id": 1, "protocol_version": 2, "agent_version": 3, "hostname": 4},
    "Reconcile": {"running_attempts": 1},
    "Heartbeat": {"sequence": 1},
    "NetProbeResult": {"probe_id": 1},
    "AttemptStatus": {
        "job_id": 1,
        "attempt_id": 2,
        "fencing_token": 3,
        "state": 4,
        "failure": 5,
        "observed_at": 6,
    },
    "LogChunk": {
        "job_id": 1,
        "attempt_id": 2,
        "stream": 3,
        "offset": 4,
        "data": 5,
        "fencing_token": 6,
    },
    "StartAttempt": {"job_id": 1, "attempt_id": 2, "fencing_token": 3},
    "CancelAttempt": {"attempt_id": 1, "fencing_token": 2, "reason": 3},
    "ProbeRequest": {"probe_id": 1},
    "PrepareEnv": {"env_hash": 1},
    "Ack": {"acked_message_id": 1},
}


def test_v01_field_numbers_are_unchanged() -> None:
    for message, fields in V01_FIELD_NUMBERS.items():
        desc = agent_pb2.DESCRIPTOR.message_types_by_name[message]
        for name, number in fields.items():
            field = desc.fields_by_name.get(name)
            assert field is not None, f"{message}.{name} was removed"
            assert field.number == number, f"{message}.{name} renumbered"
    # GcRequest (V0.5 G5) and CacheReport (V0.5 D101) were filled in V0.5:
    # pinned in their own tests below.


def test_session_id_is_envelope_field_3_on_both_envelopes() -> None:
    for envelope in (agent_pb2.AgentMessage, agent_pb2.ControllerMessage):
        field = envelope.DESCRIPTOR.fields_by_name["session_id"]
        assert field.number == 3, envelope.DESCRIPTOR.name
        assert field.type == field.TYPE_STRING
        assert field.containing_oneof is None  # envelope metadata, not a payload


def test_hello_ack_is_reachable_in_controller_oneof() -> None:
    field = agent_pb2.ControllerMessage.DESCRIPTOR.fields_by_name["hello_ack"]
    assert field.number == 16
    assert field.containing_oneof is not None
    assert field.containing_oneof.name == "payload"
    ack = agent_pb2.HelloAck(
        accepted=False,
        reject_code=agent_pb2.HelloAck.REJECT_CODE_STALE_EPOCH,
        reject_reason="epoch 3 <= live 3",
    )
    msg = agent_pb2.ControllerMessage(message_id="m-1", hello_ack=ack)
    parsed = agent_pb2.ControllerMessage.FromString(msg.SerializeToString())
    assert parsed.WhichOneof("payload") == "hello_ack"
    assert parsed.hello_ack.reject_code == agent_pb2.HelloAck.REJECT_CODE_STALE_EPOCH
    assert set(agent_pb2.HelloAck.RejectCode.keys()) == {
        "REJECT_CODE_UNSPECIFIED",
        "REJECT_CODE_STALE_EPOCH",
        "REJECT_CODE_PROTOCOL_VERSION",
        "REJECT_CODE_DUPLICATE_SESSION",
        "REJECT_CODE_INTERNAL",
        "REJECT_CODE_UNAVAILABLE",  # V0.4 D41
    }


def test_v03_session_and_resource_fields() -> None:
    hello = agent_pb2.Hello.DESCRIPTOR.fields_by_name
    assert hello["session_epoch"].type == hello["session_epoch"].TYPE_UINT64
    assert {"os", "platform", "hardware"} <= set(hello)
    gpu = agent_pb2.HardwareSummary.Gpu.DESCRIPTOR.fields_by_name
    assert set(gpu) == {
        "index",
        "model",
        "vram_bytes",
        "driver_version",
        "compute_capability",
    }
    assert gpu["compute_capability"].type == gpu["compute_capability"].TYPE_STRING

    heartbeat = agent_pb2.Heartbeat.DESCRIPTOR.fields_by_name
    assert heartbeat["agent_time"].message_type.full_name == "google.protobuf.Timestamp"

    update = agent_pb2.ResourceUpdate.DESCRIPTOR
    assert {f.name: f.number for f in update.fields} == {
        "observed_at": 1,
        "cpu_percent": 2,
        "ram_used_bytes": 3,
        "ram_total_bytes": 4,
        "gpus": 5,
        "disks": 6,
        "sequence": 7,
    }
    assert (
        agent_pb2.Reconcile.DESCRIPTOR.fields_by_name["replay_from_sequence"].number
        == 2
    )

    # Round trip through the wire with every new message populated.
    msg = agent_pb2.AgentMessage(
        message_id="m-9",
        session_id="sess_01J00000000000000000000000",
        resource_update=agent_pb2.ResourceUpdate(
            cpu_percent=12.5,
            ram_used_bytes=1 << 30,
            ram_total_bytes=16 << 30,
            gpus=[agent_pb2.ResourceUpdate.GpuUsage(index=0, util_percent=50.0)],
            disks=[agent_pb2.ResourceUpdate.DiskUsage(mount="/", free_bytes=1)],
            sequence=7,
        ),
    )
    parsed = agent_pb2.AgentMessage.FromString(msg.SerializeToString())
    assert parsed.session_id == "sess_01J00000000000000000000000"
    assert parsed.resource_update.gpus[0].util_percent == 50.0
    assert parsed.resource_update.disks[0].mount == "/"


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
    anchor = "  string acked_message_id = 1;\n"
    assert text.count(anchor) == 1
    proto.write_text(
        text.replace(anchor, anchor + "  string extra = 99;\n"), encoding="utf-8"
    )
    assert "string extra = 99;" in proto.read_text(encoding="utf-8")

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


# ---------------------------------------------------------------------------
# 0.3.10: RTT ping/pong (additive, V0.3 G2)
# ---------------------------------------------------------------------------


def test_ping_pong_field_numbers_are_pinned() -> None:
    ping = agent_pb2.ControllerMessage.DESCRIPTOR.fields_by_name["ping"]
    assert ping.number == 17
    assert ping.containing_oneof is not None
    assert ping.containing_oneof.name == "payload"
    assert ping.message_type is agent_pb2.Ping.DESCRIPTOR
    pong = agent_pb2.AgentMessage.DESCRIPTOR.fields_by_name["pong"]
    assert pong.number == 19
    assert pong.containing_oneof is not None
    assert pong.containing_oneof.name == "payload"
    assert pong.message_type is agent_pb2.Pong.DESCRIPTOR
    for msg in (agent_pb2.Ping, agent_pb2.Pong):
        field = msg.DESCRIPTOR.fields_by_name["ping_id"]
        assert field.number == 1
        assert field.type == field.TYPE_STRING
    # A Pong echoes the Ping's id and round-trips through the envelope.
    wire = agent_pb2.AgentMessage(
        message_id="m-9", session_id="sess-1", pong=agent_pb2.Pong(ping_id="p-1")
    ).SerializeToString()
    parsed = agent_pb2.AgentMessage.FromString(wire)
    assert parsed.WhichOneof("payload") == "pong"
    assert parsed.pong.ping_id == "p-1"


# ---------------------------------------------------------------------------
# 0.3.4: hardware scores (additive, V0.3 G7, brief §5g D37)
# ---------------------------------------------------------------------------


def test_hardware_scores_field_numbers_are_pinned() -> None:
    hello = agent_pb2.Hello.DESCRIPTOR.fields_by_name["hardware_scores"]
    assert hello.number == 9
    assert hello.message_type is agent_pb2.HardwareScores.DESCRIPTOR
    report = agent_pb2.AgentMessage.DESCRIPTOR.fields_by_name["hardware_scores"]
    assert report.number == 20
    assert report.containing_oneof is not None
    assert report.containing_oneof.name == "payload"
    assert report.message_type is agent_pb2.HardwareScores.DESCRIPTOR
    assert {f.name: f.number for f in agent_pb2.HardwareScores.DESCRIPTOR.fields} == {
        "run_id": 1,
        "measured_at": 2,
        "score_version": 3,
        "fingerprint": 4,
        "conditions": 5,
        "scores": 6,
    }
    assert {f.name: f.number for f in agent_pb2.Score.DESCRIPTOR.fields} == {
        "metric": 1,
        "gpu_index": 2,
        "status": 3,
        "mean": 4,
        "stdev": 5,
        "n": 6,
        "backend": 7,
        "backend_version": 8,
        "threads_effective": 9,
        "lower_bound": 10,
        "throttling_suspected": 11,
        "reason": 12,
        "detail": 13,
    }
    # "Unknown" must be expressible as absence: these carry presence.
    score = agent_pb2.Score.DESCRIPTOR.fields_by_name
    for name in ("gpu_index", "mean", "stdev", "throttling_suspected"):
        assert score[name].has_presence, name
    conditions = agent_pb2.ScoreConditions.DESCRIPTOR.fields_by_name
    for name in ("on_battery", "cpu_load_percent_before", "background_load_high"):
        assert conditions[name].has_presence, name
    assert agent_pb2.ScoreMetric.Name(0) == "SCORE_METRIC_UNSPECIFIED"
    assert agent_pb2.ScoreStatus.Name(0) == "SCORE_STATUS_UNSPECIFIED"


# ---------------------------------------------------------------------------
# V0.4 G1: Goodbye (additive, brief V0.4 §2a D40)
# ---------------------------------------------------------------------------


def test_goodbye_field_numbers_are_pinned() -> None:
    field = agent_pb2.AgentMessage.DESCRIPTOR.fields_by_name["goodbye"]
    assert field.number == 21
    assert field.containing_oneof is not None
    assert field.containing_oneof.name == "payload"
    assert field.message_type is agent_pb2.Goodbye.DESCRIPTOR
    fields = agent_pb2.Goodbye.DESCRIPTOR.fields_by_name
    assert (fields["reason"].number, fields["detail"].number) == (1, 2)
    assert fields["detail"].type == fields["detail"].TYPE_STRING
    reason = agent_pb2.Goodbye.Reason
    assert (reason.Value("REASON_UNSPECIFIED"), reason.Value("REASON_SHUTDOWN")) == (
        0,
        1,
    )
    wire = agent_pb2.AgentMessage(
        message_id="m-1",
        session_id="sess-1",
        goodbye=agent_pb2.Goodbye(
            reason=agent_pb2.Goodbye.REASON_SHUTDOWN, detail="stop"
        ),
    ).SerializeToString()
    parsed = agent_pb2.AgentMessage.FromString(wire)
    assert parsed.WhichOneof("payload") == "goodbye"
    assert parsed.goodbye.detail == "stop"


# ---------------------------------------------------------------------------
# V0.5 G4: peers and cache reports (D98, D99, D101)
# ---------------------------------------------------------------------------


def test_v05_g4_field_numbers_are_pinned() -> None:
    hello_ack = agent_pb2.HelloAck.DESCRIPTOR.fields_by_name["peer_token_key"]
    assert hello_ack.number == 8
    assert hello_ack.type == hello_ack.TYPE_BYTES
    hello = agent_pb2.Hello.DESCRIPTOR.fields_by_name["peer_endpoint"]
    assert hello.number == 10
    assert hello.type == hello.TYPE_STRING
    refresh = agent_pb2.RefreshFetchToken.DESCRIPTOR.fields_by_name["sources"]
    assert refresh.number == 6
    assert refresh.is_repeated
    assert refresh.message_type is agent_pb2.BlobSource.DESCRIPTOR
    assert {f.name: f.number for f in agent_pb2.CacheReport.DESCRIPTOR.fields} == {
        "seq": 1,
        "snapshot": 2,
        "snapshot_last": 3,
        "added": 4,
        "removed": 5,
        "page": 6,  # fix round 2, additive
    }
    assert {f.name: f.number for f in agent_pb2.CacheItem.DESCRIPTOR.fields} == {
        "kind": 1,
        "key": 2,
        "size": 3,
    }
    kind = agent_pb2.CacheItem.Kind
    assert dict(kind.items()) == {
        "KIND_UNSPECIFIED": 0,
        "KIND_BLOB": 1,
        "KIND_TREE": 2,
        "KIND_ENV": 3,
        "KIND_IMAGE": 4,
    }
    wire = agent_pb2.AgentMessage(
        message_id="m-1",
        session_id="sess-1",
        cache_report=agent_pb2.CacheReport(
            seq=1,
            snapshot=True,
            snapshot_last=True,
            added=[
                agent_pb2.CacheItem(
                    kind=agent_pb2.CacheItem.KIND_TREE, key="sha256:" + "a" * 64
                )
            ],
        ),
    ).SerializeToString()
    parsed = agent_pb2.AgentMessage.FromString(wire)
    assert parsed.WhichOneof("payload") == "cache_report"
    assert parsed.cache_report.added[0].kind == agent_pb2.CacheItem.KIND_TREE


def test_v05_g5_field_numbers_are_pinned() -> None:
    def numbers(message: type) -> dict[str, int]:
        return {f.name: f.number for f in message.DESCRIPTOR.fields}  # type: ignore[attr-defined]

    assert numbers(agent_pb2.GcRequest) == {
        "run_now": 1,
        "set_pins": 2,
        "pinned_trees": 3,
    }
    assert numbers(agent_pb2.PrepareEnv) == {
        "env_hash": 1,
        "request_id": 2,
        "base_images": 3,
        "envs": 4,
    }
    assert numbers(agent_pb2.EnvSpecRequest) == {
        "base": 1,
        "python": 2,
        "requirements": 3,
        "runtime": 4,
    }
    assert numbers(agent_pb2.PrepareEnvStatus) == {
        "request_id": 1,
        "done": 2,
        "items": 3,
    }
    assert numbers(agent_pb2.InputTree) == {
        "tree_id": 1,
        "mount": 2,
        "ref": 3,
        "sources": 4,
    }
    assert numbers(agent_pb2.TransferSource) == {
        "node_id": 1,
        "bytes": 2,
        "duration_s": 3,
        "units": 4,
        "failures": 5,
        "dropped": 6,
    }
    assert numbers(agent_pb2.TransferSummary)["sources"] == 10
    spec = agent_pb2.ExecutionSpec.DESCRIPTOR.fields_by_name["inputs"]
    assert spec.number == 17 and spec.is_repeated
    refresh = agent_pb2.RefreshFetchToken.DESCRIPTOR.fields_by_name["inputs"]
    assert refresh.number == 7 and refresh.is_repeated
    status = agent_pb2.AttemptStatus.DESCRIPTOR.fields_by_name["transfers"]
    assert status.number == 15 and status.is_repeated
    payload = agent_pb2.AgentMessage.DESCRIPTOR.fields_by_name["prepare_env_status"]
    assert payload.number == 22 and payload.containing_oneof is not None
