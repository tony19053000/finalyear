"""Controller side of the ``AgentControl.Session`` stream (roadmap 0.3.6/0.3.7/0.3.10).

* :mod:`.servicer` - the gRPC handler: Hello admission, HelloAck, one stream = one
  reader task + one outbound queue.
* :mod:`.manager` - :class:`SessionManager`: the live-session table (one per
  ``node_id``, behind one lock), message dispatch, dedupe, stale-session drops,
  typed session close.
* :mod:`.leases` - :class:`LeaseTimer`: the single timer task that turns silence
  into SUSPECT/OFFLINE via :mod:`meshforge.controller.liveness` and sends the RTT
  pings.
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

__all__ = [
    "AdmitResult",
    "AgentControlServicer",
    "LeaseTimer",
    "LiveSession",
    "OutboundOverflowError",
    "SessionManager",
    "SessionProtocolError",
]
