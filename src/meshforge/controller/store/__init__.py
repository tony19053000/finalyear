"""Persistence layer of the controller: SQLAlchemy models + Alembic migrations.

Modules:

* :mod:`.settings` - ``[database]`` settings (no SQLAlchemy import; base-install safe).
* :mod:`.base` - declarative ``Base`` with a constraint naming convention, column mixins.
* :mod:`.engine` - async engine / session factory built from settings (asyncpg).
* :mod:`.models` - model registry imported by Alembic's ``env.py``.
* :mod:`.migrate` - programmatic Alembic config (used by tests and future CLI commands).

This ``__init__`` imports nothing on purpose: the controller package must stay
importable on a base install (no ``controller`` extra).
"""
