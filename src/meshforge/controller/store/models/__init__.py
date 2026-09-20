"""Model registry: importing this package registers every table on ``Base.metadata``.

Alembic's ``env.py`` imports it before autogenerate / ``alembic check``, so a model
module that is not imported here is invisible to migrations. When adding a model
module, import it below (and write a migration with the next ``NNNN`` revision id).

Tables so far: ``node``, ``node_session`` (V0.3, :mod:`.node`).
"""

from meshforge.controller.store.base import Base
from meshforge.controller.store.models.node import Node, NodeSession, SessionEndReason

__all__ = ["Base", "Node", "NodeSession", "SessionEndReason"]
