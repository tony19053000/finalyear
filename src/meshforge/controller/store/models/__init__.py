"""Model registry: importing this package registers every table on ``Base.metadata``.

Alembic's ``env.py`` imports it before autogenerate / ``alembic check``, so a model
module that is not imported here is invisible to migrations. When adding a model
module, import it below (and generate a migration with ``--rev-id NNNN``).

Empty in V0.1: domain tables (nodes, jobs, attempts, ...) arrive with the features
that own them (V0.3+), each with its own migration.
"""

from meshforge.controller.store.base import Base

__all__ = ["Base"]
