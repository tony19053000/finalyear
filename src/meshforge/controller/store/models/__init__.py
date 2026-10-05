"""Model registry: importing this package registers every table on ``Base.metadata``.

Alembic's ``env.py`` imports it before autogenerate / ``alembic check``, so a model
module that is not imported here is invisible to migrations. When adding a model
module, import it below (and write a migration with the next ``NNNN`` revision id).

Tables so far: ``node``, ``node_session`` (V0.3, :mod:`.node`),
``resource_snapshot`` (V0.3 0.3.11, :mod:`.resource`), ``job``,
``job_transition``, ``attempt``, ``attempt_phase`` (V0.4 D49, :mod:`.job`),
``cache_location`` (V0.5 D101, :mod:`.cache`), ``named_input``, ``job_input``,
``transfer_event`` (V0.5 G5 D92/D93, :mod:`.data`), ``plan_decision`` (V0.6
G2 D112, :mod:`.plan`), ``network_link`` (V0.6 G4 D111, :mod:`.network`).
"""

from meshforge.controller.store.base import Base
from meshforge.controller.store.models.cache import CacheLocation
from meshforge.controller.store.models.data import JobInput, NamedInput, TransferEvent
from meshforge.controller.store.models.job import (
    Attempt,
    AttemptPhaseRow,
    Job,
    JobTransition,
)
from meshforge.controller.store.models.network import NetworkLink
from meshforge.controller.store.models.node import Node, NodeSession, SessionEndReason
from meshforge.controller.store.models.plan import PlanDecision
from meshforge.controller.store.models.resource import ResourceSnapshot

__all__ = [
    "Attempt",
    "AttemptPhaseRow",
    "Base",
    "CacheLocation",
    "Job",
    "JobInput",
    "JobTransition",
    "NamedInput",
    "NetworkLink",
    "Node",
    "NodeSession",
    "PlanDecision",
    "ResourceSnapshot",
    "SessionEndReason",
    "TransferEvent",
]
