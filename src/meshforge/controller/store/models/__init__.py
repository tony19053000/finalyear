"""Model registry: importing this package registers every table on ``Base.metadata``.

Alembic's ``env.py`` imports it before autogenerate / ``alembic check``, so a model
module that is not imported here is invisible to migrations. When adding a model
module, import it below (and write a migration with the next ``NNNN`` revision id).

Tables so far: ``node``, ``node_session`` (V0.3, :mod:`.node`),
``resource_snapshot`` (V0.3 0.3.11, :mod:`.resource`), ``job``,
``job_transition``, ``attempt``, ``attempt_phase`` (V0.4 D49, :mod:`.job`).
"""

from meshforge.controller.store.base import Base
from meshforge.controller.store.models.job import (
    Attempt,
    AttemptPhaseRow,
    Job,
    JobTransition,
)
from meshforge.controller.store.models.node import Node, NodeSession, SessionEndReason
from meshforge.controller.store.models.resource import ResourceSnapshot

__all__ = [
    "Attempt",
    "AttemptPhaseRow",
    "Base",
    "Job",
    "JobTransition",
    "Node",
    "NodeSession",
    "ResourceSnapshot",
    "SessionEndReason",
]
