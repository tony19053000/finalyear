"""Manual placement validation (D50) and the local node-id helper, pure."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from mf_api_records import node_record

from meshforge.common.errors import FailureType
from meshforge.common.ids import new_node_id
from meshforge.common.local_node import LocalNodeError, read_local_node_id
from meshforge.common.manifest import parse_manifest_data
from meshforge.controller.jobs.placement import PlacementError, check_placement
from meshforge.controller.liveness import NodeState

GIB = 1024**3
READY: dict[str, Any] = {
    "capabilities": ["exec", "exec-docker", "exec-gpu"],
    "gpus": [{"index": 0, "vram_bytes": str(4 * GIB)}],
    "ram_bytes": str(16 * GIB),
    "cpu_threads": 16,
}


def _manifest(**resources: Any) -> Any:
    return parse_manifest_data({"entrypoint": "python a.py", "resources": resources})


def _check(manifest: Any, *, connected: bool = True, **node: Any) -> None:
    check_placement(manifest, node_record(**node), connected=connected)


def test_a_ready_node_accepts() -> None:
    _check(_manifest(gpu="required", vram_gb=4, ram_gb=16, cpus=16), hardware=READY)


def test_unknown_node_is_not_found() -> None:
    with pytest.raises(PlacementError) as info:
        check_placement(_manifest(), None, connected=False)
    assert info.value.failure_type is FailureType.NOT_FOUND


@pytest.mark.parametrize(
    ("resources", "node", "connected", "fragment"),
    [
        ({}, {"state": NodeState.OFFLINE, "hardware": READY}, True, "offline"),
        ({}, {"state": NodeState.SUSPECT, "hardware": READY}, True, "suspect"),
        ({}, {"hardware": READY}, False, "live session"),
        ({}, {"hardware": {"capabilities": ["exec-docker"]}}, True, "does not accept"),
        ({}, {"hardware": {}}, True, "does not accept"),
        (
            {"gpu": "required"},
            {"hardware": {**READY, "gpus": []}},
            True,
            "has none",
        ),
        (
            {"gpu": "required"},
            {"hardware": {**READY, "capabilities": ["exec", "exec-docker"]}},
            True,
            "cannot pass",
        ),
        ({"gpu": "required", "vram_gb": 4.01}, {"hardware": READY}, True, "VRAM"),
        ({"ram_gb": 16.01}, {"hardware": READY}, True, "RAM"),
        ({"cpus": 17}, {"hardware": READY}, True, "CPUs"),
    ],
)
def test_refusals_are_no_feasible_plan(
    resources: dict[str, Any], node: dict[str, Any], connected: bool, fragment: str
) -> None:
    with pytest.raises(PlacementError, match=fragment) as info:
        _check(_manifest(**resources), connected=connected, **node)
    assert info.value.failure_type is FailureType.NO_FEASIBLE_PLAN


def test_preferred_gpu_runs_on_a_cpu_only_node() -> None:
    _check(
        _manifest(gpu="preferred"),
        hardware={"capabilities": ["exec", "exec-subprocess"]},
    )


def test_unreported_hardware_is_not_held_against_the_node() -> None:
    _check(_manifest(ram_gb=64, cpus=64), hardware={"capabilities": ["exec"]})


# ------------------------------------------------------------ local node id


def _identity(tmp_path: Path, payload: object | bytes) -> Path:
    path = tmp_path / "identity" / "node.json"
    path.parent.mkdir(parents=True)
    data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    path.write_bytes(data)
    return tmp_path


def test_reads_the_agents_node_id(tmp_path: Path) -> None:
    node = str(new_node_id())
    data_dir = _identity(
        tmp_path,
        {
            "format": 1,
            "node_id": node,
            "session_epoch": 4,
            "created_at": "x",
            "key_fingerprint_sha256": "f",
        },
    )
    assert read_local_node_id(data_dir) == node


def test_missing_identity_is_not_found(tmp_path: Path) -> None:
    with pytest.raises(LocalNodeError, match="meshforge-agent run") as info:
        read_local_node_id(tmp_path)
    assert info.value.failure_type is FailureType.NOT_FOUND


@pytest.mark.parametrize(
    "payload",
    [
        b"not json",
        [1, 2],
        {"format": 2, "node_id": "node_x"},
        {"format": 1, "node_id": "job_01J8Z3K5Q9V6W2X4Y7A0B1C2D3"},
        {"format": 1},
        b"{" + b" " * (64 * 1024) + b"}",
    ],
)
def test_bad_identity_is_config_error(tmp_path: Path, payload: object) -> None:
    with pytest.raises(LocalNodeError) as info:
        read_local_node_id(_identity(tmp_path, payload))
    assert info.value.failure_type is FailureType.CONFIG_ERROR


def test_agrees_with_the_identity_the_real_agent_writes(tmp_path: Path) -> None:
    from meshforge.agent.identity import load_or_create

    identity = load_or_create(tmp_path / "identity")
    assert read_local_node_id(tmp_path) == identity.node_id
