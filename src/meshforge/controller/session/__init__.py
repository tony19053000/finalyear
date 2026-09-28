"""Controller side of the ``AgentControl.Session`` stream (roadmap 0.3.6/0.3.7/0.3.10).

* :mod:`.servicer` - the gRPC handler: Hello admission, HelloAck, one stream = one
  reader task + one outbound queue.
* :mod:`.manager` - :class:`SessionManager`: the live-session table (one per
  ``node_id``, behind one lock), envelope checks, dedupe, stale-session drops,
  typed session close and the liveness arrow it fires.
* :mod:`.live` - :class:`LiveSession` and the admission results (the data model).
* :mod:`.admission` - the pure Hello checks, the Hello's §12.3 arrow, HelloAck.
* :mod:`.claims` - :class:`AdmissionClaims`: a pending Hello owns its node's state
  and defers the previous session's end (D28).
* :mod:`.dispatch` - :class:`DispatchTable`: payload name -> handler (D42).
* :mod:`.reports` - report handlers (resource updates, hardware scores) and their
  counters.
* :mod:`.heartbeats` - coalesced ``last_seen_at`` writes and RTT samples.
* :mod:`.leases` - :class:`LeaseTimer`: the single timer task that turns silence
  into SUSPECT/OFFLINE via :mod:`meshforge.controller.liveness` and sends the RTT
  pings.
* :mod:`.writer` - :class:`WriteChain`: one ordered single-writer task per
  session that carries every registry write, so neither a reader nor the lease
  timer ever awaits the database.
* :mod:`.limits` - caps on peer-controlled input (``message_id`` shape, Hello
  string lengths) applied before anything is kept or persisted.
* :mod:`.epoch` - ``MAX_SESSION_EPOCH``, the one place the controller's bound on
  ``Hello.session_epoch`` is defined.
* :mod:`.errors` - typed failures of the session layer.

The package imports gRPC and the generated stubs (core dependencies) but no
controller-extra package at import time; the registry it is given does.
"""

from meshforge.controller.session.errors import (
    OutboundOverflowError,
    SessionProtocolError,
)
from meshforge.controller.session.leases import LeaseTimer
from meshforge.controller.session.manager import (
    AdmitResult,
    LiveSession,
    SessionManager,
)
from meshforge.controller.session.servicer import AgentControlServicer
from meshforge.controller.session.writer import WriteChain

__all__ = [
    "AdmitResult",
    "AgentControlServicer",
    "LeaseTimer",
    "LiveSession",
    "OutboundOverflowError",
    "SessionManager",
    "SessionProtocolError",
    "WriteChain",
]
