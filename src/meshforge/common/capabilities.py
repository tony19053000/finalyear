"""Capability tags both sides of the wire must spell the same way.

``HardwareSummary.capabilities`` is a free-form list (``agent.proto``); most tags
are only *read* by the planner. A tag whose meaning the controller acts on lives
here, so the agent (which sends it) and the controller (which interprets it)
share one constant without importing each other.
"""

from __future__ import annotations

from typing import Final

__all__ = ["HARDWARE_SCORES_CAPABILITY"]

HARDWARE_SCORES_CAPABILITY: Final = "hardware-scores"
"""The agent measures hardware scores (roadmap 0.3.4): ``[agent.scores] enabled``
and hardware discovery succeeded. A node with this tag and no stored scores is
``pending`` in the REST ``scores.status``; a node without it is ``none`` - it will
not produce scores unless its configuration changes (brief §5g D37/D38)."""
