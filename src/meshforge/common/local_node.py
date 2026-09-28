"""Which node is *this* laptop? Read-only lookup of the local agent's ``node_id`` (D50).

``compute run --local`` places a job on the submitting laptop's own agent. The CLI
must not import ``meshforge.agent``, so this module reads the agent's
``<data_dir>/identity/node.json`` (ADR-0013 format 1: ``{"format": 1, "node_id",
...}``) itself - read-only, bounded, never the key file, never a lock (reading a
JSON file the agent replaces atomically cannot observe a half-written state).

Failures are typed :class:`LocalNodeError`: ``not_found`` when this laptop has no
agent identity yet (the agent never ran here), ``config_error`` when the file is
there but unreadable or not a format-1 identity.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final

from meshforge.common.errors import FailureType, InvalidIdError, MeshForgeError
from meshforge.common.ids import NodeId, validate_node_id

__all__ = [
    "IDENTITY_DIR_NAME",
    "IDENTITY_FORMAT",
    "MAX_NODE_JSON_BYTES",
    "NODE_JSON_NAME",
    "LocalNodeError",
    "local_node_json",
    "read_local_node_id",
]

IDENTITY_DIR_NAME: Final = "identity"
NODE_JSON_NAME: Final = "node.json"
IDENTITY_FORMAT: Final = 1
MAX_NODE_JSON_BYTES: Final = 64 * 1024
"""node.json is ~250 bytes; anything this large is not ours."""


class LocalNodeError(MeshForgeError):
    """No usable local agent identity."""

    default_failure_type = FailureType.CONFIG_ERROR


def local_node_json(data_dir: Path) -> Path:
    """``<data_dir>/identity/node.json`` (the same layout the agent writes)."""
    return data_dir / IDENTITY_DIR_NAME / NODE_JSON_NAME


def read_local_node_id(data_dir: Path) -> NodeId:
    """The ``node_id`` of the agent that uses ``data_dir`` on this machine."""
    path = local_node_json(data_dir)
    try:
        with path.open("rb") as fh:
            raw = fh.read(MAX_NODE_JSON_BYTES + 1)
    except FileNotFoundError:
        raise LocalNodeError(
            "this laptop has no MeshForge agent identity yet: start "
            "`meshforge-agent run` here once (or use --node NODE_ID)",
            failure_type=FailureType.NOT_FOUND,
        ) from None
    except OSError as exc:
        raise LocalNodeError(
            f"cannot read the local agent identity: {exc.strerror or exc}"
        ) from None
    if len(raw) > MAX_NODE_JSON_BYTES:
        raise LocalNodeError("the local agent identity file is too large")
    try:
        meta = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise LocalNodeError("the local agent identity file is not JSON") from None
    if not isinstance(meta, dict) or meta.get("format") != IDENTITY_FORMAT:
        raise LocalNodeError(
            "the local agent identity file has an unknown format; upgrade compute "
            "or the agent"
        )
    try:
        return validate_node_id(meta.get("node_id"))
    except InvalidIdError:
        raise LocalNodeError(
            "the local agent identity file holds no valid node_id"
        ) from None
