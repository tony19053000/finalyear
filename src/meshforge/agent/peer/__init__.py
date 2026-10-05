"""The agent's read-only peer blob server (V0.5 G4, D97/D98).

* :mod:`.auth` - :class:`PeerAuthorizer`: checks controller-signed peer tokens
  (holder, expiry, tree membership from this node's own CAS).
* :mod:`.server` - :class:`PeerBlobServer`: HTTP/1.1 GET/HEAD + one Range on
  asyncio streams and ``h11``, serving only from the CAS, plus the
  ``/v1/probe`` route (V0.6 G4, D111).
* :mod:`.probe` - :class:`ProbeService`: measures a link when the controller
  sends ``ProbeRequest`` (V0.6 G4, D111).
"""

from meshforge.agent.peer.auth import AuthResult, PeerAuthorizer
from meshforge.agent.peer.server import PeerBlobServer, PeerServerStats

__all__ = ["AuthResult", "PeerAuthorizer", "PeerBlobServer", "PeerServerStats"]
