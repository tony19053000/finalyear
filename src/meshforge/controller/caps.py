"""Wire caps shared by the controller's admission checks and its resource store.

``MAX_GPUS`` / ``MAX_DISKS`` bound what one peer can put in a single message
(:mod:`meshforge.controller.session.limits`) *and* how many distinct GPU indices
or disk mounts one downsample bucket may accumulate
(:mod:`meshforge.controller.resources`). They live here - in a module that imports
nothing from either side - because ``controller.session`` imports
``controller.resources`` (the manager needs ``ResourceSink``), so keeping the caps
in ``session/limits.py`` forced ``Downsampler.__init__`` to defer its import to
dodge a cycle (V0.3 G4 review follow-up 5).

One source of truth: ``session/limits.py`` re-exports these names, and the agent's
deliberately separate copy (:mod:`meshforge.agent.hardware.limits` - the controller
never imports the agent) is pinned to the same numbers by
``test_the_agent_and_controller_caps_are_the_same_numbers``.
"""

from __future__ import annotations

from typing import Final

__all__ = ["MAX_DISKS", "MAX_GPUS"]

MAX_GPUS: Final = 16
"""Devices per node, in ``HardwareSummary.gpus`` and ``ResourceUpdate.gpus``."""

MAX_DISKS: Final = 16
"""Entries in ``ResourceUpdate.disks``."""
