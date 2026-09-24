"""Crash-safe JSON checkpoints for the long lab runs (soak, RSS sampling).

Written with :func:`spikes._harness.atomic.atomic_write_text` (temp file + fsync +
``os.replace``), so after a crash or power cut the file holds either the previous
checkpoint or the new one, never half of each. A checkpoint that cannot be read
raises :class:`CheckpointError` instead of silently starting over - a 23-hour soak
must never be thrown away by accident (``--fresh`` starts over on purpose).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final

from spikes._harness.atomic import atomic_write_text

CHECKPOINT_VERSION: Final = 1


class CheckpointError(ValueError):
    """The checkpoint file exists but is not a checkpoint this tool wrote."""


def save(path: Path, kind: str, state: dict[str, Any]) -> None:
    doc = {"checkpoint_version": CHECKPOINT_VERSION, "kind": kind, "state": state}
    atomic_write_text(
        path, json.dumps(doc, indent=1, sort_keys=True, allow_nan=False) + "\n"
    )


def load(path: Path, kind: str) -> dict[str, Any] | None:
    """The saved state, ``None`` when there is no file. Raises :class:`CheckpointError`."""
    if not path.exists():
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CheckpointError(
            f"{path.name}: cannot read the checkpoint ({exc})"
        ) from None
    if (
        not isinstance(doc, dict)
        or doc.get("checkpoint_version") != CHECKPOINT_VERSION
        or doc.get("kind") != kind
        or not isinstance(doc.get("state"), dict)
    ):
        raise CheckpointError(
            f"{path.name}: not a {kind} checkpoint of version {CHECKPOINT_VERSION}"
        )
    state: dict[str, Any] = doc["state"]
    return state
