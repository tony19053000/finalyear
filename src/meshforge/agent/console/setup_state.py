"""Has the owner finished the first-run wizard? (D392, Q26)

The wizard's policy step comes **after** the join, and the owner must see it
(it cannot be skipped). A laptop joined from the terminal, or a browser closed
right after the join, would otherwise land on Home with the policy never
shown. So the wizard's last step writes ``<data_dir>/console-setup-done``
(empty, private) and the console shows the wizard - from the policy step on -
until it exists. "Leave this fabric" removes it: the next join is a new node,
and its owner sees the policy again.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Final

from meshforge.agent.settings import AgentSettings
from meshforge.common.atomic import atomic_write_bytes
from meshforge.common.private_files import protect_private

__all__ = ["SETUP_FILE_NAME", "clear_setup", "mark_setup_complete", "setup_complete"]

SETUP_FILE_NAME: Final = "console-setup-done"


def _path(settings: AgentSettings) -> Path:
    return settings.data_dir / SETUP_FILE_NAME


def setup_complete(settings: AgentSettings) -> bool:
    try:
        return _path(settings).is_file()
    except OSError:
        return False


def mark_setup_complete(settings: AgentSettings) -> None:
    """Raises :class:`OSError` when it cannot be written."""
    path = _path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(path, b"", mode=0o600)
    protect_private(path)


def clear_setup(settings: AgentSettings) -> None:
    with contextlib.suppress(FileNotFoundError):
        _path(settings).unlink()
