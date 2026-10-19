"""Shared leased work queue (ADR-0006, ADR-0019; V0.10 D262, roadmap 0.10.2).

One generic component shared by every execution mode that divides work into
pieces pulled by several workers (ADR-0006). A queue is owned by an opaque
``(kind, owner_id)`` pair; nothing here knows what an item *means* - its
payload is opaque JSON - and nothing here knows any caller (a unit test scans
this package for caller vocabulary).

Modules:

* :mod:`.core` - pure state machine (no database, no clock, stdlib only):
  item states ``pending -> leased -> done`` / ``failed``, per-item lease
  sequence numbers (fencing), visibility timeout, attempts, at most two live
  leases per item (original + one speculative copy) and idempotent completion
  (the first committed result wins).
* :mod:`.tables` - the ``work_queue`` / ``work_item`` / ``work_lease`` tables
  (migration 0025).
* :mod:`.store` - PostgreSQL operations on a **caller-owned** session, so a
  caller can commit a lease change together with its own rows in one
  transaction (V0.10 checklist 35/36).

Import policy (``pyproject.toml`` ``[tool.importlinter]``): this package imports
only :mod:`meshforge.common`; the controller imports it. This ``__init__``
imports nothing so the pure core stays importable without SQLAlchemy.
"""
