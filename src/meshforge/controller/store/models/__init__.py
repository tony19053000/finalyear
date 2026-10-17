"""Model registry: importing this package registers every table on ``Base.metadata``.

Alembic's ``env.py`` imports it before autogenerate / ``alembic check``, so a model
module that is not imported here is invisible to migrations. When adding a model
module, import it below (and write a migration with the next ``NNNN`` revision id).

Tables so far: ``node``, ``node_session`` (V0.3, :mod:`.node`),
``resource_snapshot`` (V0.3 0.3.11, :mod:`.resource`), ``job``,
``job_transition``, ``attempt``, ``attempt_phase`` (V0.4 D49, :mod:`.job`),
``cache_location`` (V0.5 D101, :mod:`.cache`), ``named_input``, ``job_input``,
``transfer_event`` (V0.5 G5 D92/D93, :mod:`.data`), ``plan_decision`` (V0.6
G2 D112, :mod:`.plan`), ``network_link`` (V0.6 G4 D111, :mod:`.network`),
``join_token``, ``audit_event`` (V0.7 G2 D135/D136, :mod:`.enrollment`),
``revoked_cert`` (V0.7 G3 D138, :mod:`.revocation`), ``app_user``,
``api_token`` (V0.7 G4 D141, :mod:`.auth`), ``secret`` (V0.7 G6 D144,
:mod:`.secret`), ``run_record`` (V0.9 G1 D212, :mod:`.telemetry`).
"""

from meshforge.controller.store.base import Base
from meshforge.controller.store.models.auth import ApiToken, AppUser, UserRole
from meshforge.controller.store.models.cache import CacheLocation
from meshforge.controller.store.models.data import JobInput, NamedInput, TransferEvent
from meshforge.controller.store.models.enrollment import (
    AuditAction,
    AuditEvent,
    JoinToken,
)
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
from meshforge.controller.store.models.revocation import RevokedCert
from meshforge.controller.store.models.secret import Secret
from meshforge.controller.store.models.telemetry import RunRecord

__all__ = [
    "ApiToken",
    "AppUser",
    "Attempt",
    "AttemptPhaseRow",
    "AuditAction",
    "AuditEvent",
    "Base",
    "CacheLocation",
    "Job",
    "JobInput",
    "JobTransition",
    "JoinToken",
    "NamedInput",
    "NetworkLink",
    "Node",
    "NodeSession",
    "PlanDecision",
    "ResourceSnapshot",
    "RevokedCert",
    "RunRecord",
    "Secret",
    "SessionEndReason",
    "TransferEvent",
    "UserRole",
]
