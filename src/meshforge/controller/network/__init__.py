"""The network matrix (V0.6 G4, D111): peer probes -> ``network_link``.

* :mod:`.links` - EWMA and staleness arithmetic (pure);
* :mod:`.scheduler` - which pair to probe next within every bound (pure,
  injected clock);
* :mod:`.store` - ``network_link`` upserts, matrix reads, controller -> node
  bandwidth from ``transfer_event`` history;
* :mod:`.service` - :class:`NetworkProber`, the controller task that sends
  ``ProbeRequest`` and folds ``NetProbeResult`` into the matrix.
"""

from meshforge.controller.network.links import LinkRow, ewma, is_stale
from meshforge.controller.network.scheduler import ProbeScheduler
from meshforge.controller.network.service import NetworkProber
from meshforge.controller.network.store import NetworkStore

__all__ = [
    "LinkRow",
    "NetworkProber",
    "NetworkStore",
    "ProbeScheduler",
    "ewma",
    "is_stale",
]
